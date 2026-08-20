"""Leg 1 — the save-time entity vocabulary ingress gate (fact:1375, EG_LEG1).

The gate lives in `MemoryCoordinator._entity_ingress_error`, called from both
writers of caller-supplied entity names: handle_save (facts + decisions share
this generic path) and handle_retrospective (its own endpoint, its own
`entities` field). It resolves every name `sanitize_entity_name` (ontology.py)
would treat as a genuine candidate against `entity_vocabulary` +
`entity_vocab_aliases` (migration 033), rewriting a hit to its canonical
spelling and refusing an unknown one (400 `entity_unknown`) unless
`metadata.new_entities` explicitly names it for minting.

Invariants under test (each with its own mutation-check disposition recorded
in EG_LEG1_HANDOFF.md):

  I1  lookup-never-create — an unknown name with no `new_entities` cover is
      REFUSED, and `_entity_vocab_mint` is never called for it
  I2  canonical rewrite reaches `metadata['entities']` before anything reads
      it downstream (rule 3)
  I3  noise `sanitize_entity_name` rejects is gate-exempt: no lookup, no
      refusal, left verbatim (Tier 1 pristine unaffected) — and by
      construction can never be minted (mint only ever sees `candidates`,
      sanitize's own survivors)
  I4  `entities_provenance` keys track the canonical rewrite, so its own
      "name must be in entities" check never spuriously fires on a name this
      gate just rewrote
  I5  an unknown name explicitly covered by `new_entities` is minted and the
      save proceeds with the canonical name mint returned
  I6  entities stay OPTIONAL (fact:1215) — an empty/absent list short-circuits
      before any lookup, including before validating a malformed
      `new_entities`
  I7  a malformed `new_entities` (not a list of strings) is refused
      `new_entities_invalid`, but ONLY when there are candidates to check it
      against

All SQL is stubbed here (`_entity_vocab_resolve`/`_entity_vocab_mint` are
monkeypatched directly) — see CLAUDE.md's "green suite is not an all-clear"
rule. The raw SQL (`ENTITY_VOCAB_RESOLVE_SQL`/`ENTITY_VOCAB_MINT_SQL`) is
verified against the live database separately; see the handoff.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import (  # noqa: E402
    ENTITY_VOCAB_RESOLVE_SQL, ENTITY_VOCAB_MINT_SQL, MemoryCoordinator,
)


def _coord(vocabulary=None):
    """A coordinator whose entity vocabulary answers from a fixed dict —
    {raw_name: canonical_name} — mirroring what a real resolve would find.
    A name absent from the dict is UNKNOWN (resolves to None), exactly what
    an unregistered name gets from the live SQL.
    """
    c = MemoryCoordinator()
    vocab = dict(vocabulary or {})
    c._entity_vocab_resolve = AsyncMock(side_effect=lambda n: vocab.get(n))
    # A clean mint always succeeds and returns the name as sent — the
    # ordinary, non-racing case. Tests that care about the race path stub
    # this differently.
    c._entity_vocab_mint = AsyncMock(side_effect=lambda n, agent: n)
    return c


# ── I6 — entities stay optional, unconditionally ─────────────────────────────

@pytest.mark.asyncio
async def test_no_entities_short_circuits_before_any_lookup():
    c = _coord()
    assert await c._entity_ingress_error({"project": "p"}, "claude") is None
    c._entity_vocab_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_empty_entities_list_short_circuits():
    c = _coord()
    assert await c._entity_ingress_error(
        {"project": "p", "entities": []}, "claude") is None
    c._entity_vocab_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_new_entities_is_ignored_when_entities_is_empty():
    """I6/I7 interaction: a malformed new_entities never gets validated on an
    entity-less save — the field is meaningless without entities, and this
    gate must never fire on a record that named none (fact:1215)."""
    c = _coord()
    err = await c._entity_ingress_error(
        {"project": "p", "entities": [], "new_entities": "not-a-list"}, "claude")
    assert err is None


# ── I3 — noise sanitize_entity_name rejects is gate-exempt ───────────────────

@pytest.mark.asyncio
async def test_numeric_only_entity_is_never_looked_up_or_refused():
    """A leaked pg_id like "254" is noise per sanitize_entity_name, not a
    candidate for this gate at all (rule 7: additive AFTER it). MUTATION
    CHECK: replace `sanitize_entity_names(raw_entities)` with `raw_entities`
    in `_entity_ingress_error` and this test fails — "254" becomes a
    candidate, fails vocabulary resolution, and the save is wrongly refused."""
    c = _coord()
    metadata = {"project": "p", "entities": ["254"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    c._entity_vocab_resolve.assert_not_called()
    c._entity_vocab_mint.assert_not_called()
    # Left verbatim — Tier 1 pristine, unaffected by this gate.
    assert metadata["entities"] == ["254"]


@pytest.mark.asyncio
async def test_noise_entity_cannot_be_minted_even_if_declared():
    """I3's mint-safety corollary: sanitize-rejected names are never
    candidates, so naming one in new_entities cannot smuggle it into the
    vocabulary — mint is never called at all for an entities-list containing
    only noise."""
    c = _coord()
    metadata = {
        "project": "p", "entities": ["0"], "new_entities": ["0"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    c._entity_vocab_mint.assert_not_called()


# ── I1 — lookup-never-create outside the explicit mint path ──────────────────

@pytest.mark.asyncio
async def test_unknown_entity_with_no_mint_declaration_is_refused():
    c = _coord()
    metadata = {"project": "p", "entities": ["Kubernetes"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["status"] == "error"
    assert err["error"] == "entity_unknown"
    assert err["unknown_entities"] == ["Kubernetes"]
    c._entity_vocab_mint.assert_not_called()


@pytest.mark.asyncio
async def test_only_the_uncovered_unknown_names_block_the_save():
    """new_entities covering SOME but not all unknowns still refuses — every
    unknown name needs its own explicit cover, one flag cannot wave through
    a name it never named.

    MUTATION CHECK: change `still_unknown = [n for n in unknown if n not in
    mint_requested]` to `still_unknown = []` (treat any new_entities as
    blanket cover) and this test fails — the save would proceed and silently
    mint "Prometheus" too."""
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Kubernetes", "Prometheus"],
        "new_entities": ["Kubernetes"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entity_unknown"
    assert err["unknown_entities"] == ["Prometheus"]
    # The covered name must not have been minted either — the save as a
    # whole was refused, so nothing partially commits.
    c._entity_vocab_mint.assert_not_called()


# ── I5 — the explicit mint path ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_entities_mints_the_named_unknown_and_the_save_proceeds():
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Kubernetes"],
        "new_entities": ["Kubernetes"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    c._entity_vocab_mint.assert_awaited_once_with("Kubernetes", "claude")
    assert metadata["entities"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_mint_returning_a_different_canonical_is_what_gets_stored():
    """A racing mint (or a canonical seeded a moment earlier by another save)
    can hand back a DIFFERENT canonical than the name sent — the gate must
    store THAT, not the raw name, exactly like the ordinary resolve path."""
    c = _coord()
    c._entity_vocab_mint = AsyncMock(return_value="Kubernetes")
    metadata = {
        "project": "p",
        "entities": ["kubernetes"],
        "new_entities": ["kubernetes"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_new_entities_not_a_list_of_strings_is_refused():
    c = _coord()
    metadata = {"project": "p", "entities": ["Kubernetes"], "new_entities": "Kubernetes"}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "new_entities_invalid"
    c._entity_vocab_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_new_entities_with_a_non_string_element_is_refused():
    c = _coord()
    metadata = {"project": "p", "entities": ["Kubernetes"], "new_entities": ["Kubernetes", 5]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "new_entities_invalid"


# ── I2 — canonical rewrite reaches metadata['entities'] ──────────────────────

@pytest.mark.asyncio
async def test_a_case_variant_is_rewritten_to_the_registered_canonical():
    """MUTATION CHECK: comment out the `self._rewrite_entities(metadata,
    resolved)` call at the end of `_entity_ingress_error` and this test
    fails — metadata['entities'] stays ["k8s"] instead of ["Kubernetes"]."""
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"project": "p", "entities": ["k8s"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_an_already_canonical_name_is_left_exactly_as_is():
    c = _coord(vocabulary={"Kubernetes": "Kubernetes"})
    metadata = {"project": "p", "entities": ["Kubernetes"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_mixed_known_and_unknown_only_rewrites_the_known_half():
    """A resolved name is rewritten in place; an unknown name in the SAME
    list still refuses the whole save (rule 4 — no partial acceptance)."""
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"project": "p", "entities": ["k8s", "Grafana"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["unknown_entities"] == ["Grafana"]
    # Refused — the list must not have been partially rewritten either.
    assert metadata["entities"] == ["k8s", "Grafana"]


@pytest.mark.asyncio
async def test_a_noise_name_beside_a_resolved_alias_keeps_the_noise_verbatim():
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"project": "p", "entities": ["k8s", "42"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes", "42"]


# ── I4 — entities_provenance keys track the rewrite ──────────────────────────

@pytest.mark.asyncio
async def test_entities_provenance_keys_are_rewritten_with_the_canonical():
    """MUTATION CHECK: remove the `entities_provenance` branch from
    `_rewrite_entities` and this test fails — the provenance dict keeps the
    key 'k8s' while `entities` has moved on to 'Kubernetes', which would then
    make handle_save's own "name not in this save's entities list" check
    refuse a save that should succeed."""
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {
        "project": "p",
        "entities": ["k8s"],
        "entities_provenance": {"k8s": "operator"},
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities_provenance"] == {"Kubernetes": "operator"}


@pytest.mark.asyncio
async def test_entities_provenance_absent_is_a_harmless_no_op():
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"project": "p", "entities": ["k8s"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert "entities_provenance" not in metadata


# ── Decision/retrospective scope — same generic gate, same rules ────────────

@pytest.mark.asyncio
async def test_a_decision_s_entities_are_gated_identically():
    """A decision's `entities` never reaches the graph (it inherits from its
    grounding facts there), but it DOES reach Postgres metadata — so it is in
    scope for canonicalization exactly like a fact's, per
    `_entity_ingress_error`'s docstring."""
    c = _coord()
    metadata = {"type": "decision", "entities": ["Kubernetes"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entity_unknown"


@pytest.mark.asyncio
async def test_a_retrospective_shaped_metadata_is_gated_identically():
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"type": "retrospective", "entities": ["k8s"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes"]


# ── The refusal shape itself ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_refusal_names_every_unknown_and_says_how_to_mint():
    c = _coord()
    metadata = {"project": "p", "entities": ["Grafana", "Loki"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err["unknown_entities"] == ["Grafana", "Loki"]
    assert "new_entities" in err["message"]
    assert "ASK THE OPERATOR" in err["message"]


# ── The raw SQL shape (verified against the live DB separately; see handoff) ─

def test_the_resolve_and_mint_sql_reference_entity_normalize():
    """A cheap, no-DB guard against the SQL drifting away from the migration's
    OWN normalization function — every reader/writer of this vocabulary must
    call `entity_normalize`, never reimplement it (033's own comment)."""
    assert "entity_normalize" in ENTITY_VOCAB_RESOLVE_SQL
    assert "entity_vocabulary" in ENTITY_VOCAB_RESOLVE_SQL
    assert "entity_vocab_aliases" in ENTITY_VOCAB_RESOLVE_SQL


def test_the_mint_sql_never_touches_the_alias_table():
    """I1's other half, at the SQL level: the only INSERT this gate issues
    targets entity_vocabulary alone (rule 5) — asserting it here means a
    future edit that starts writing entity_vocab_aliases from this same
    statement cannot slip past a docstring-only guarantee."""
    assert "INSERT INTO entity_vocabulary" in ENTITY_VOCAB_MINT_SQL
    assert "entity_vocab_aliases" not in ENTITY_VOCAB_MINT_SQL
    assert "ON CONFLICT (normalized_key) DO NOTHING" in ENTITY_VOCAB_MINT_SQL


# ── The DB-facing primitives themselves, against a stubbed connection ───────

@pytest.mark.asyncio
async def test_resolve_queries_via_acquire_and_returns_fetchval():
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value="Kubernetes")
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)

    result = await c._entity_vocab_resolve("k8s")
    assert result == "Kubernetes"
    conn.fetchval.assert_awaited_once_with(ENTITY_VOCAB_RESOLVE_SQL, "k8s")


@pytest.mark.asyncio
async def test_mint_returns_the_row_name_on_a_clean_insert():
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 1, "name": "Kubernetes"})
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)

    result = await c._entity_vocab_mint("Kubernetes", "claude")
    assert result == "Kubernetes"
    conn.fetchrow.assert_awaited_once_with(
        ENTITY_VOCAB_MINT_SQL, "Kubernetes", "claude")


@pytest.mark.asyncio
async def test_mint_re_resolves_on_a_conflict_race():
    """RETURNING comes back empty when ON CONFLICT DO NOTHING fired — a
    concurrent mint (or a pre-existing canonical) won the same normalized
    key. The caller must not assume its own mint won; it re-resolves.

    MUTATION CHECK: change `if row is not None: return row["name"]` ...
    `return name` (i.e. always trust the raw name on conflict) and this test
    fails — it would return "kubernetes" instead of the actually-registered
    "Kubernetes"."""
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)  # ON CONFLICT DO NOTHING fired
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)
    c._entity_vocab_resolve = AsyncMock(return_value="Kubernetes")

    result = await c._entity_vocab_mint("kubernetes", "claude")
    assert result == "Kubernetes"
    c._entity_vocab_resolve.assert_awaited_once_with("kubernetes")


@pytest.mark.asyncio
async def test_mint_falls_back_to_the_raw_name_if_re_resolve_also_misses():
    """Genuinely unexpected (a conflict fired but nothing resolves) — surface
    the raw name rather than raising, so a save does not 500 over a race
    outcome a following save would simply redo."""
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)
    c._entity_vocab_resolve = AsyncMock(return_value=None)

    result = await c._entity_vocab_mint("kubernetes", "claude")
    assert result == "kubernetes"
