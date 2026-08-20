"""Leg 1 — the save-time entity vocabulary ingress gate (fact:1375, EG_LEG1),
including the FIX ROUND from the security review (Opus, fact:1412,
decision:1413 rules the disposition).

The gate lives in `MemoryCoordinator._entity_ingress_error`, called from both
writers of caller-supplied entity names: handle_save (facts + decisions share
this generic path) and handle_retrospective (its own endpoint, its own
`entities` field). It resolves every name `sanitize_entity_name` (ontology.py)
would treat as a genuine candidate against `entity_vocabulary` +
`entity_vocab_aliases` (migration 033), rewriting a hit to its canonical
spelling and refusing an unknown one (400 `entity_unknown`) unless
`metadata.new_entities` explicitly names it for minting.

Original invariants (I1-I7, EG LEG-1 build) plus the fix-round's new ones
(S-1/S-2/S-4/S-5/S-6/S-8/S-10), each with its own mutation-check disposition
recorded in EG_LEG1_HANDOFF.md:

  I1  lookup-never-create
  I2  canonical rewrite reaches `metadata['entities']`
  I3  noise sanitize_entity_name rejects is gate-exempt
  I4  entities_provenance keys track the canonical rewrite
  I5  the explicit new_entities mint path
  I6  entities stay optional
  I7  new_entities shape validation
  S-1 the rewrite matches RAW names via their SANITIZED form, not
      themselves — a whitespace variant now canonicalizes correctly
      (previously the primary invariant's own blind spot: every I2/I4 test
      before this fix used sanitize-stable names, so raw == candidate
      throughout and the bug was invisible)
  S-2 a mint whose name normalizes to empty is a clean 400, never an
      unhandled 500
  S-5 candidate resolution is ONE batched round trip; entities/new_entities
      list length and per-name length are capped
  S-8 `new_entities` never survives into `metadata` after a successful gate
  S-10 every `new_entities` name must appear in `entities`, ENFORCED

S-4 (call the gate LAST, after every other 400-capable check) and S-6 (echo
the final canonical entities in the response) are properties of the
handle_save/handle_retrospective call sites, not of `_entity_ingress_error`
itself — tested end-to-end against a mocked coordinator in the
"END-TO-END" section below, mirroring test_axis_persistence_and_entity_
provenance.py's `_coordinator_with_mocks()` style.

All SQL is stubbed here (`_entity_vocab_resolve_many`/`_entity_vocab_mint`
are monkeypatched directly) — see CLAUDE.md's "green suite is not an
all-clear" rule. The raw SQL is verified against the live database
separately; see EG_LEG1_HANDOFF.md's FIX ROUND section for the numbers.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, _SCRIPTS)

from coordinator import (  # noqa: E402
    ENTITY_LIST_MAX_LEN, ENTITY_NAME_MAX_LEN, ENTITY_VOCAB_MINT_SQL,
    ENTITY_VOCAB_RESOLVE_MANY_SQL, ENTITY_VOCAB_RESOLVE_SQL, MemoryCoordinator,
)


def _coord(vocabulary=None):
    """A coordinator whose entity vocabulary answers from a fixed dict —
    {name: canonical_name} — via the BATCHED resolve
    (`_entity_vocab_resolve_many`), mirroring what the live SQL returns: a
    dict covering only the names it recognises. A name absent from the dict
    is UNKNOWN, exactly what an unregistered name gets from the live query.
    """
    c = MemoryCoordinator()
    vocab = dict(vocabulary or {})

    async def _resolve_many(names):
        return {n: vocab[n] for n in names if n in vocab}

    c._entity_vocab_resolve_many = AsyncMock(side_effect=_resolve_many)
    # A clean mint always succeeds and returns the name as sent — the
    # ordinary, non-racing, non-empty-normalization case. Tests that care
    # about a different outcome stub this differently.
    c._entity_vocab_mint = AsyncMock(side_effect=lambda n, agent: n)
    return c


# ── I6 — entities stay optional, unconditionally ─────────────────────────────

@pytest.mark.asyncio
async def test_no_entities_short_circuits_before_any_lookup():
    c = _coord()
    assert await c._entity_ingress_error({"project": "p"}, "claude") is None
    c._entity_vocab_resolve_many.assert_not_called()


@pytest.mark.asyncio
async def test_empty_entities_list_short_circuits():
    c = _coord()
    assert await c._entity_ingress_error(
        {"project": "p", "entities": []}, "claude") is None
    c._entity_vocab_resolve_many.assert_not_called()


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
    c._entity_vocab_resolve_many.assert_not_called()
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
    c._entity_vocab_resolve_many.assert_not_called()


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


# ══════════════════════════════════════════════════════════════════════════
# FIX ROUND (security review, fact:1412, disposition ruled by decision:1413)
# ══════════════════════════════════════════════════════════════════════════

# ── S-1 (Required) — canonicalization survives sanitize's TRANSFORM ─────────
#
# The headline finding: every test above this line uses sanitize-STABLE
# names (raw == its own sanitized form throughout), which is exactly why the
# bug was invisible to the original 26. These fixtures are deliberately
# sanitize-UNSTABLE — a trailing space, a doubled internal space — the class
# the review measured live against a synthetic vocabulary.

@pytest.mark.asyncio
async def test_a_trailing_space_variant_is_still_canonicalized():
    """MUTATION CHECK: in `_rewrite_entities`, replace `_canonical_of` (which
    re-sanitizes each raw name before the `resolved` lookup) with the
    original `resolved.get(e, e)` (looking the RAW string up directly) and
    this test fails — 'k8s ' (trailing space) is not a key in `resolved`
    (which is keyed on the SANITIZED candidate 'k8s'), so it is left
    unchanged instead of becoming 'Kubernetes' — reproducing the exact S-1
    defect (security review probe case B)."""
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {"project": "p", "entities": ["k8s "]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Kubernetes"], (
        "a trailing-space variant of a known alias must still canonicalize"
    )


@pytest.mark.asyncio
async def test_a_doubled_internal_space_variant_is_still_canonicalized():
    """Security review probe case C."""
    c = _coord(vocabulary={"Alpha Widget": "Alpha Widget"})
    metadata = {"project": "p", "entities": ["Alpha  Widget"]}  # doubled space
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Alpha Widget"]


@pytest.mark.asyncio
async def test_two_raw_spellings_collapsing_to_one_candidate_both_canonicalize():
    """Security review probe case D: a canonical spelling and a padded alias
    of the SAME entity in one save — both raw strings must end up canonical,
    not just the one that happened to already match its sanitized form."""
    c = _coord(vocabulary={"Alpha Widget": "Alpha Widget", "AW": "Alpha Widget"})
    metadata = {"project": "p", "entities": ["Alpha Widget", "AW "]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities"] == ["Alpha Widget", "Alpha Widget"]


@pytest.mark.asyncio
async def test_entities_provenance_key_with_trailing_space_is_also_rewritten():
    """S-1's fix extends to entities_provenance keys too — the same raw
    string, the same bug, the same fix (`_canonical_of` is shared by both
    branches of `_rewrite_entities`)."""
    c = _coord(vocabulary={"k8s": "Kubernetes"})
    metadata = {
        "project": "p",
        "entities": ["k8s "],
        "entities_provenance": {"k8s ": "operator"},
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert metadata["entities_provenance"] == {"Kubernetes": "operator"}


@pytest.mark.asyncio
async def test_a_padded_unknown_name_is_still_refused():
    """Security review probe case E: the refusal path was never affected by
    S-1 (only the rewrite was) — confirming that stays true after the fix."""
    c = _coord()
    metadata = {"project": "p", "entities": ["Grafana "]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entity_unknown"
    # The refusal echoes the SANITIZED form (what the vocabulary was
    # actually asked about), not the padded raw string.
    assert err["unknown_entities"] == ["Grafana"]


# ── S-2 (Required) — a mint that normalizes to empty is a clean 400 ─────────

@pytest.mark.asyncio
async def test_mint_normalizing_to_empty_string_is_a_400_not_a_500():
    """`_entity_vocab_mint` returning None (Postgres refused the insert — see
    the dedicated primitive-level test below) must turn into a structured
    400, never an unhandled exception the gate lets propagate.

    MUTATION CHECK: in `_entity_ingress_error`'s mint loop, remove the
    `if canonical is None: return {...}` branch (i.e., always do
    `resolved[name] = canonical`) and this test fails with a TypeError
    (None cannot be assigned as a valid canonical without later blowing up
    `_rewrite_entities`'s dict lookups) instead of a clean refusal — this is
    the exact S-2 defect at the gate's own boundary."""
    c = _coord()
    c._entity_vocab_mint = AsyncMock(return_value=None)  # simulates the caught RaiseError
    metadata = {"project": "p", "entities": ["!!"], "new_entities": ["!!"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["status"] == "error"
    assert err["error"] == "new_entities_invalid"


@pytest.mark.asyncio
async def test_entity_vocab_mint_catches_raiseerror_and_returns_none():
    """The primitive itself, against a stubbed connection whose `fetchrow`
    raises `asyncpg.RaiseError` — exactly what migration 033's
    `entity_vocabulary_before_write` trigger does for a name that normalizes
    to the empty string (security review probe: '!!', '..', '🔥🔥').

    MUTATION CHECK: remove the `try/except asyncpg.RaiseError` around the
    `conn.fetchrow` call in `_entity_vocab_mint` and this test fails — the
    exception propagates out of the coroutine instead of being caught and
    turned into `None`, reproducing S-2's unhandled-500 exactly."""
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        side_effect=asyncpg.RaiseError(
            'entity "!!" normalizes to the empty string — every character '
            "was stripped, so it cannot be registered as a canonical spelling"
        )
    )
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)

    result = await c._entity_vocab_mint("!!", "claude")
    assert result is None


@pytest.mark.asyncio
async def test_entity_vocab_mint_lets_other_exceptions_propagate():
    """The catch is SPECIFIC to `asyncpg.RaiseError` — a genuine connection
    failure (or any other exception class) must still surface as a real
    error rather than being silently swallowed and misreported as "bad
    entity name". A bare `except Exception` here would be its own defect."""
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetchrow = AsyncMock(side_effect=ConnectionResetError("connection lost"))
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)

    with pytest.raises(ConnectionResetError):
        await c._entity_vocab_mint("Kubernetes", "claude")


# ── S-5 (Required) — batched resolve + env-overridable caps ─────────────────

@pytest.mark.asyncio
async def test_resolve_is_batched_into_exactly_one_call_regardless_of_count():
    """MUTATION CHECK: revert `_entity_ingress_error`'s resolution step to a
    `for name in candidates: await self._entity_vocab_resolve(name)` loop
    and this test fails — `_entity_vocab_resolve_many` would never be called
    at all (0 calls, not N), or a per-candidate loop calling the single-name
    primitive would leave `_entity_vocab_resolve_many.call_count == 0`."""
    c = _coord(vocabulary={"AA": "AA", "BB": "BB", "CC": "CC", "DD": "DD", "EE": "EE"})
    metadata = {"project": "p", "entities": ["AA", "BB", "CC", "DD", "EE"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert c._entity_vocab_resolve_many.await_count == 1
    (called_with,), _ = c._entity_vocab_resolve_many.await_args
    assert called_with == ["AA", "BB", "CC", "DD", "EE"]


@pytest.mark.asyncio
async def test_an_oversized_entities_list_is_refused_before_any_lookup():
    """MUTATION CHECK: remove the `len(raw_entities) > ENTITY_LIST_MAX_LEN`
    check and this test fails — the oversized list reaches
    `_entity_vocab_resolve_many` instead of being refused up front."""
    c = _coord()
    metadata = {"project": "p", "entities": [f"Name{i}" for i in range(ENTITY_LIST_MAX_LEN + 1)]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entities_list_too_long"
    c._entity_vocab_resolve_many.assert_not_called()


@pytest.mark.asyncio
async def test_an_entities_list_at_exactly_the_cap_is_not_refused_for_length():
    c = _coord(vocabulary={f"Name{i}": f"Name{i}" for i in range(ENTITY_LIST_MAX_LEN)})
    metadata = {"project": "p", "entities": [f"Name{i}" for i in range(ENTITY_LIST_MAX_LEN)]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None


@pytest.mark.asyncio
async def test_an_oversized_new_entities_list_is_refused():
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Kubernetes"],
        "new_entities": [f"Name{i}" for i in range(ENTITY_LIST_MAX_LEN + 1)],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entities_list_too_long"


@pytest.mark.asyncio
async def test_an_overlength_entity_name_is_refused_and_never_echoed():
    """MUTATION CHECK: remove the per-name `len(e) > ENTITY_NAME_MAX_LEN`
    check and this test fails — the oversized name reaches resolution
    instead of being refused (S-5's headline live-fire: a 200 000-character
    name minted without complaint)."""
    c = _coord()
    long_name = "x" * (ENTITY_NAME_MAX_LEN + 1)
    metadata = {"project": "p", "entities": [long_name]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entity_name_too_long"
    # The refusal must not amplify the oversized input back into the response.
    assert long_name not in err["message"]
    c._entity_vocab_resolve_many.assert_not_called()


@pytest.mark.asyncio
async def test_an_overlength_new_entities_name_is_refused():
    c = _coord()
    long_name = "y" * (ENTITY_NAME_MAX_LEN + 1)
    metadata = {"project": "p", "entities": ["Kubernetes"], "new_entities": [long_name]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "entity_name_too_long"


# ── S-8 (Optional, ruled fix-now) — new_entities never persists ─────────────

@pytest.mark.asyncio
async def test_new_entities_is_popped_from_metadata_after_a_successful_gate():
    """MUTATION CHECK: remove `metadata.pop("new_entities", None)` from the
    end of `_entity_ingress_error` and this test fails — 'new_entities'
    remains a key in `metadata`, exactly S-8's finding (probe 2, case G:
    "metadata keys -> ['entities', 'new_entities']" after a clean pass)."""
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Kubernetes"],
        "new_entities": ["Kubernetes"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    assert "new_entities" not in metadata


@pytest.mark.asyncio
async def test_new_entities_is_popped_even_when_never_consulted():
    """A `new_entities` field the save didn't end up needing (every named
    entity already resolved) is still a control field, not record content —
    it must not persist either."""
    c = _coord(vocabulary={"Kubernetes": "Kubernetes"})
    metadata = {
        "project": "p",
        "entities": ["Kubernetes"],
        "new_entities": ["SomethingElseEntirely"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None  # S-10 refuses this (SomethingElseEntirely not in entities)
    # On a REFUSAL nothing is persisted anyway — new_entities is only
    # popped on the success path, which is what matters (S-8's finding was
    # about the record that gets WRITTEN, and a refused save writes nothing).


@pytest.mark.asyncio
async def test_new_entities_absent_to_begin_with_is_a_harmless_no_op():
    c = _coord()
    metadata = {"project": "p", "entities": ["Kubernetes"]}
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None  # unknown, no new_entities to cover it
    assert "new_entities" not in metadata


# ── S-10 (Nit, ruled fix-now) — new_entities ⊆ entities, ENFORCED ───────────

@pytest.mark.asyncio
async def test_new_entities_naming_something_absent_from_entities_is_refused():
    """Security review probe case F: previously `new_entities=['Zzz
    Unknown','Other Thing']` with only 'Zzz Unknown' actually unknown in
    `entities` silently dropped 'Other Thing' with `err: None` — a typo in
    the mint declaration looked identical to success. Now refused.

    MUTATION CHECK: remove the S-10 loop in `_entity_ingress_error` (the one
    building `mint_requested` via the subset check) and replace it with the
    original `mint_requested = set(new_entities_raw or [])` and this test
    fails — 'Other Thing' is silently ignored instead of refusing the save."""
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Zzz Unknown"],
        "new_entities": ["Zzz Unknown", "Other Thing"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "new_entities_invalid"
    assert "Other Thing" in err["message"]
    c._entity_vocab_mint.assert_not_called()


@pytest.mark.asyncio
async def test_new_entities_matching_entities_via_a_whitespace_variant_is_accepted():
    """S-10's subset check is matched in SANITIZED-candidate space (like the
    S-1 rewrite fix) — a whitespace variant in `new_entities` still matches
    its counterpart in `entities` rather than being wrongly refused as
    "absent"."""
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Brand New Thing"],
        "new_entities": ["Brand  New Thing"],  # doubled space, same candidate
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is None
    c._entity_vocab_mint.assert_awaited_once_with("Brand New Thing", "claude")


@pytest.mark.asyncio
async def test_new_entities_that_sanitizes_to_noise_is_refused_not_ignored():
    """A `new_entities` entry that is itself noise (e.g. numeric-only) can
    never correspond to a real candidate — refused, not silently dropped."""
    c = _coord()
    metadata = {
        "project": "p",
        "entities": ["Kubernetes"],
        "new_entities": ["42"],
    }
    err = await c._entity_ingress_error(metadata, "claude")
    assert err is not None
    assert err["error"] == "new_entities_invalid"


# ── The refusal codes and SQL shape (structural, no DB) ──────────────────────

def test_the_resolve_sql_variants_reference_entity_normalize():
    for sql in (ENTITY_VOCAB_RESOLVE_SQL, ENTITY_VOCAB_RESOLVE_MANY_SQL):
        assert "entity_normalize" in sql
        assert "entity_vocabulary" in sql
        assert "entity_vocab_aliases" in sql


def test_the_batched_resolve_sql_drives_off_unnest_not_a_loop():
    """Structural guard for S-5's batching claim — the SQL itself must be
    the array/ANY form, not a per-name statement a caller could accidentally
    still invoke in a loop."""
    assert "unnest" in ENTITY_VOCAB_RESOLVE_MANY_SQL
    assert "$1::text[]" in ENTITY_VOCAB_RESOLVE_MANY_SQL


def test_the_mint_sql_never_touches_the_alias_table():
    """I1's other half, at the SQL level: the only INSERT this gate issues
    targets entity_vocabulary alone (rule 5)."""
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
async def test_resolve_many_queries_once_via_acquire_and_returns_a_dict():
    c = MemoryCoordinator()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"raw_name": "k8s", "canonical_name": "Kubernetes"},
        {"raw_name": "bogus", "canonical_name": None},
    ])
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    c._acquire = MagicMock(return_value=acq)

    result = await c._entity_vocab_resolve_many(["k8s", "bogus"])
    assert result == {"k8s": "Kubernetes"}  # NULL canonical dropped, not kept as None
    conn.fetch.assert_awaited_once_with(
        ENTITY_VOCAB_RESOLVE_MANY_SQL, ["k8s", "bogus"])


@pytest.mark.asyncio
async def test_resolve_many_with_no_names_never_touches_the_connection():
    c = MemoryCoordinator()
    c._acquire = MagicMock(side_effect=AssertionError("must not acquire for []"))
    result = await c._entity_vocab_resolve_many([])
    assert result == {}


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
    key. The caller must not assume its own mint won; it re-resolves."""
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


# ══════════════════════════════════════════════════════════════════════════
# END-TO-END — S-4 (call-site ordering) and S-6 (response echo)
# ══════════════════════════════════════════════════════════════════════════
#
# These properties belong to handle_save/handle_retrospective, not to
# `_entity_ingress_error` itself, so they need the full request path with a
# mocked coordinator — mirroring test_axis_persistence_and_entity_
# provenance.py's `_coordinator_with_mocks()`.

class _AsyncCtx:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_):
        pass


def _make_request(body: dict) -> MagicMock:
    """handle_save reads `agent_id` from the TOP-LEVEL body (falling back to
    "unknown"), separately from `metadata.source` — auto-fill it from
    `metadata.source` when the caller didn't set one explicitly, so a test
    asserting a mint's attribution doesn't have to know that plumbing
    detail to get a non-"unknown" agent_id."""
    body = dict(body)
    if "agent_id" not in body:
        md = body.get("metadata") or {}
        if isinstance(md, dict) and md.get("source"):
            body["agent_id"] = md["source"]
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(return_value=None)
    return req


def _full_coord(vocabulary=None):
    """A MemoryCoordinator with pool/neo4j mocked well enough to drive
    handle_save/handle_retrospective end to end. `conn.fetchval` UNCONFIGURED
    answers "not None" for every OTHER registry lookup (project/domain axis),
    the same convention test_axis_persistence_and_entity_provenance.py's
    fixture uses — so a save with a registered-looking project sails through
    project/domain ingress without further stubbing. The entity vocabulary
    gate's own two DB primitives are stubbed directly (never through the
    shared `fetchval`), matching `_coord()` above.
    """
    c = MemoryCoordinator()
    vocab = dict(vocabulary or {})

    async def _resolve_many(names):
        return {n: vocab[n] for n in names if n in vocab}

    c._entity_vocab_resolve_many = AsyncMock(side_effect=_resolve_many)
    c._entity_vocab_mint = AsyncMock(side_effect=lambda n, agent: n)

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetchval = AsyncMock(return_value=1)  # "registered"/"exists" for every other lookup
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool

    session = AsyncMock()
    session.run = AsyncMock()
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j

    return c, conn


# ── S-4 — the gate runs LAST; a save refused for another reason never mints ─

@pytest.mark.asyncio
async def test_invalid_entities_provenance_refuses_before_the_gate_ever_runs():
    """MUTATION CHECK: move the `entity_error = await
    self._entity_ingress_error(...)` call in handle_save back to its
    pre-fix-round position (immediately after the `entities` list-type
    check, BEFORE the entities_provenance validation block) and this test
    fails — `_entity_vocab_resolve_many`/`_entity_vocab_mint` get called
    (and in this fixture, `_entity_vocab_mint` would succeed) before the
    entities_provenance shape check ever has a chance to 400 the save,
    reproducing S-4's exact defect: a mint surviving the refusal of the
    save that requested it."""
    c, conn = _full_coord()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact with both a mintable entity and bad provenance",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["BrandNewThing"],
                "new_entities": ["BrandNewThing"],
                # Invalid shape — must 400 BEFORE the gate ever mints.
                "entities_provenance": {"BrandNewThing": "not-a-valid-value"},
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 400
    body = await resp.json() if hasattr(resp, "json") else None
    c._entity_vocab_resolve_many.assert_not_called()
    c._entity_vocab_mint.assert_not_called()


@pytest.mark.asyncio
async def test_a_clean_save_with_a_mint_still_succeeds_after_the_move():
    """Confirms the reordering didn't just make everything fail — a
    well-formed save that mints a genuinely new entity still succeeds, with
    the gate running (and minting) once, last."""
    c, conn = _full_coord()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact naming a brand new concept",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["BrandNewThing"],
                "new_entities": ["BrandNewThing"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    c._entity_vocab_mint.assert_awaited_once_with("BrandNewThing", "claude-code")


# ── S-6 — the response echoes the final canonical entities when rewritten ──

@pytest.mark.asyncio
async def test_response_echoes_canonical_entities_when_the_gate_rewrote_them():
    """MUTATION CHECK: remove the `entities_rewritten` field from
    handle_save's success response (or stop computing `entities_before`) and
    this test fails — the caller has no way to learn its entity name was
    silently changed (S-6's disclosure gap)."""
    c, conn = _full_coord(vocabulary={"k8s": "Kubernetes"})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact naming an alias variant",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["k8s"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    payload = resp.body if hasattr(resp, "body") else None
    import json as _json
    data = _json.loads(resp.body)
    assert data["entities_rewritten"] == ["Kubernetes"]


@pytest.mark.asyncio
async def test_response_omits_the_echo_when_nothing_was_rewritten():
    c, conn = _full_coord(vocabulary={"Kubernetes": "Kubernetes"})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact naming an already-canonical entity",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["Kubernetes"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    import json as _json
    data = _json.loads(resp.body)
    assert data["entities_rewritten"] is None


@pytest.mark.asyncio
async def test_response_omits_the_echo_when_the_save_named_no_entities():
    c, conn = _full_coord()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact with no entities",
            "metadata": {"source": "claude-code", "project": "shared_memory"},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    import json as _json
    data = _json.loads(resp.body)
    assert data["entities_rewritten"] is None


# ── S-8 end-to-end — new_entities never reaches the stored PG metadata ─────

@pytest.mark.asyncio
async def test_new_entities_is_absent_from_the_metadata_actually_written():
    c, conn = _full_coord()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact minting a new concept",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["BrandNewThing"],
                "new_entities": ["BrandNewThing"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    stored_metadata = conn.fetchrow.await_args.args[2]
    assert "new_entities" not in stored_metadata
    assert stored_metadata["entities"] == ["BrandNewThing"]
