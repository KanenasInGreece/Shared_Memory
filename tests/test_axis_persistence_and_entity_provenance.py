"""v0.8.73 — decision:1214 (canonical top-level axis key) + fact:1215 (per-
entity provenance stamping, and the stale entities/consolidation contract).

Coverage:
  (a) a decision saved with blob domains materialises top-level
      metadata['domains'] (decision:1214)
  (b) a retrospective asserting a domain is still refused 400 — untouched by
      the materialisation change, which lives entirely in handle_save and a
      retrospective never reaches it
  (c) entities_provenance: valid shape passes and persists verbatim; an
      unknown entity name in the mapping is refused; a bad value is refused;
      entities present without provenance succeeds and carries an advisory
      note in the response
  (d) the save response for an entity-less fact carries the HONEST
      Tier-3-still-eligible note, never the stale "ineligible" claim
  (e) the part-1 outbox question: materialising the top-level key does not
      change (or double) what the outbox row's cypher_params["domains"]
      carries, because resolve_domains prefers the `decision` blob over the
      top level

All SQL/Cypher is stubbed here — see CLAUDE.md's "green suite is not an
all-clear" rule; nothing new is exercised on the live database by this PR.
"""
import importlib.util
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def load_coordinator():
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator


# ── Helpers (mirrors tests/test_coordinator.py) ────────────────────────────────

class _async_ctx:
    """Minimal async context manager wrapping a value."""
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _make_request(body: dict, authenticated_agent: str | None = None,
                   principal: dict | None = None) -> MagicMock:
    state = {"authenticated_agent": authenticated_agent, "principal": principal}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator_with_mocks():
    """A MemoryCoordinator whose pool/neo4j are mocked. `conn.fetchval` is left
    UNCONFIGURED (default AsyncMock), so every registry lookup on the domain/
    project axis (`_domain_registered`, `_project_identity`, ...) answers
    "not None" — a decision naming any section passes ingress trivially,
    exactly like the plain-fact tests in test_coordinator.py rely on."""
    c = MemoryCoordinator()

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"id": 99})
    mock_conn.execute = AsyncMock(return_value="DELETE 0")
    mock_conn.fetch = AsyncMock(return_value=[])
    mock_conn.transaction = MagicMock(return_value=_async_ctx(None))

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_async_ctx(mock_conn))
    c._pool = mock_pool

    mock_session = AsyncMock()
    mock_session.run = AsyncMock()
    mock_neo4j = MagicMock()
    mock_neo4j.session = MagicMock(return_value=_async_ctx(mock_session))
    c._neo4j = mock_neo4j

    # Entity vocabulary ingress gate (fact:1375): unless a test overrides it,
    # every name resolves to ITSELF — the same "passes trivially" convention
    # the unconfigured `fetchval` above gives every other axis lookup here,
    # but scoped to this one method so a generic AsyncMock return value never
    # masquerades as a canonical name (a Mock object landing in `entities`/
    # `entities_provenance` broke the exact-value assertions below).
    #
    # FIX ROUND (S-5, security review fact:1412): the gate now batches
    # resolution through `_entity_vocab_resolve_many` (one round trip,
    # `conn.fetch`) instead of one `_entity_vocab_resolve` call per name —
    # stub the batched method with the same identity-resolves-trivially
    # convention.
    c._entity_vocab_resolve_many = AsyncMock(
        side_effect=lambda names: {n: n for n in names})

    return c, mock_conn, mock_session


def _outbox_params(mock_conn):
    call = next(
        (c for c in mock_conn.execute.call_args_list if "neo4j_outbox" in c.args[0]),
        None,
    )
    assert call is not None, "outbox INSERT not found"
    return call.args[2]


def _decision_request(domains=None, **decision_extra):
    decision = {
        "decided_by": "Xenofon",
        "project": "shared_memory",
        "rationale": "test rationale for axis materialisation",
        **decision_extra,
    }
    if domains is not None:
        decision["domains"] = domains
    return _make_request({
        "content": "We decided to split the axis.",
        "metadata": {
            "source": "claude-code",
            "type": "decision",
            "decision": decision,
        },
    })


# ── (a) decision:1214 — materialisation to the top level ──────────────────────

@pytest.mark.asyncio
async def test_decision_domains_materialise_to_top_level_metadata():
    """decision:1214: every operator-asserted axis lives at metadata TOP LEVEL
    on every record type — a fact already complies; a decision's asserted
    domains have only ever lived in the `decision` blob. The coordinator now
    materialises the same list to the top level before the row is persisted.

    MUTATION CHECK: delete the materialisation block in handle_save (the
    `if metadata.get("type") == "decision": ... metadata["domains"] = ...`)
    and this assertion fails — the stored row's top-level metadata carries no
    'domains' key at all, even though the blob does."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _decision_request(domains=["architecture", "capture"])
        resp = await c.handle_save(req)
    assert resp.status == 200

    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert metadata_arg.get("domains") == ["architecture", "capture"], (
        "the decision's asserted domains must reach the canonical top-level "
        "key, not only the `decision` blob"
    )
    # ADDITIVE, not a rewrite: the blob is left exactly as the client sent it.
    assert metadata_arg["decision"]["domains"] == ["architecture", "capture"]


@pytest.mark.asyncio
async def test_a_decision_with_no_asserted_domain_gets_no_top_level_key():
    """A decision that names no section must not gain a fabricated top-level
    `domains` key — materialisation mirrors what was asserted, it never
    invents a value where none was given."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _decision_request(domains=None)
        resp = await c.handle_save(req)
    assert resp.status == 200
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert "domains" not in metadata_arg


@pytest.mark.asyncio
async def test_a_plain_fact_is_unaffected_by_decision_materialisation():
    """The materialisation branch is gated on type == 'decision' — a plain
    fact's own top-level `domain`/`domains` (already canonical) must pass
    through completely untouched.

    ⚠ RE-RULED at v0.9.69 (O-3): the entity was `SharedMemory`, whose
    `axis_key` is `sharedmemory` — IDENTICAL to this fixture's own project
    `shared_memory`. The reserved-name check now refuses a save that names its
    own project as an entity, so the fixture was asserting 200 on a record that
    is a `fact:1215` violation. Renamed to a name that is a CONCEPT rather than
    this record's axis; the domain-passthrough property under test is untouched.

    ⚠ This is exactly the collision the plan's v2 findings predicted for
    `SKILL.md:357`/`:484`, which ship `"SharedMemory"` as an example entity —
    surfaced here by the suite rather than by a reader. Builder B owns those
    lines."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a plain fact naming its own section",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "domain": "architecture",
                "entities": ["OutboxPattern"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert metadata_arg.get("domain") == "architecture"
    assert "domains" not in metadata_arg


# ── (b) a retrospective is still refused for asserting a domain ──────────────

@pytest.mark.asyncio
async def test_a_retrospective_naming_a_domain_is_still_refused():
    """P17, unchanged by decision:1214: the materialisation code lives only in
    handle_save, and a retrospective is saved through handle_retrospective —
    a structurally different endpoint a retrospective never bypasses. This
    pins that the refusal still fires after this release.

    MUTATION CHECK: relax `handle_retrospective`'s `names_a_domain(body)`
    guard (e.g. remove the early return) and this test fails — the save
    would succeed with a domain the retrospective has no business asserting."""
    c, mock_conn, _ = _coordinator_with_mocks()
    req = _make_request({
        "pg_id": 42,
        "rating": "validated",
        "notes": "measured the outcome",
        "domain": "architecture",
        "grounded_in": [7],
    })
    resp = await c.handle_retrospective(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "domain_not_allowed_on_judgement"


# ── (c) entities_provenance ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_valid_entities_provenance_passes_and_persists_verbatim():
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact with stamped entity provenance",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern", "Coordinator"],
                "entities_provenance": {
                    "OutboxPattern": "operator",
                    "Coordinator": "agent",
                },
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    metadata_arg = mock_conn.fetchrow.await_args.args[2]
    assert metadata_arg["entities_provenance"] == {
        "OutboxPattern": "operator", "Coordinator": "agent",
    }
    body = json.loads(resp.text)
    assert body["entities_provenance_note"] is None


@pytest.mark.asyncio
async def test_entities_provenance_naming_an_unknown_entity_is_refused():
    """MUTATION CHECK: drop the `name not in entity_set` check and this test
    fails — a provenance mapping could name any string, not only entities the
    save actually declared."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as mock_embed:
        req = _make_request({
            "content": "a fact",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern"],
                "entities_provenance": {"NotDeclared": "operator"},
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "entities_provenance_invalid"
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_entities_provenance_with_a_bad_value_is_refused():
    """MUTATION CHECK: drop the `value not in ENTITIES_PROVENANCE_VALUES`
    check and this test fails — any string would be accepted as a
    provenance value instead of the closed operator|agent enum."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as mock_embed:
        req = _make_request({
            "content": "a fact",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern"],
                "entities_provenance": {"OutboxPattern": "guessed"},
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "entities_provenance_invalid"
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_entities_provenance_overlong_key_yields_a_bounded_400_message():
    """SEC-03 (six-role milestone audit, Required) — 400 error messages must
    not echo a caller-supplied value unbounded: an unbounded echo turns a
    validation error into an amplification vector. `_short()` caps the repr
    of any interpolated value at 200 chars (plus an ellipsis marker).

    RE-RULED at v0.9.69 (item 8): the overlong value is now carried by the
    provenance KEY alone, not by `entities` as well. The entity gate's
    validation half — including its S-5 `ENTITY_NAME_MAX_LEN` cap — moved in
    front of the project axis and therefore in front of this check, so a
    5000-char string sitting in `entities` is now refused as
    `entity_name_too_long` before provenance is ever examined. The MEMBERSHIP
    branch exercised here is the interpolation site that can still be handed
    an unbounded caller value — a key naming something that is NOT in
    `entities` is under no length cap at all — so it is the site SEC-03's
    property actually needs pinned.

    MUTATION CHECK: replace `_short(name)` back with `name!r` at the
    entities_provenance[...] interpolation site and this test's length
    assertion fails — the message balloons to the full 5000-char key."""
    c, mock_conn, _ = _coordinator_with_mocks()
    overlong = "X" * 5000
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as mock_embed:
        req = _make_request({
            "content": "a fact",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern"],
                "entities_provenance": {overlong: "operator"},
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    # Still names the error code — bounding the echo must not lose the
    # machine-readable classification.
    assert body["error"] == "entities_provenance_invalid"
    assert len(body["message"]) < 300
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_entities_provenance_non_dict_is_refused():
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)) as mock_embed:
        req = _make_request({
            "content": "a fact",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern"],
                "entities_provenance": ["operator"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "entities_provenance_invalid"
    mock_embed.assert_not_called()


@pytest.mark.asyncio
async def test_entities_without_provenance_succeeds_with_an_advisory_note():
    """MUTATION CHECK: hard-code `entities_provenance_missing = False` and
    this test fails — the response would silently omit the advisory instead
    of naming the gap at capture time."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact with unstamped entities",
            "metadata": {
                "source": "claude-code",
                "project": "shared_memory",
                "entities": ["OutboxPattern"],
            },
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert body["entities_provenance_note"], (
        "entities named with no entities_provenance must carry a non-empty "
        "advisory note in the response"
    )


# ── (d) the honest Tier-3 note replaces the stale claim ────────────────────────

@pytest.mark.asyncio
async def test_entity_less_fact_response_carries_the_honest_note():
    """fact:1215 — the fold now keys on project+domain, not entities, so an
    entity-less fact is fully consolidatable. The save response must say so
    and must NOT repeat the pre-1215 'ineligible for Tier 3' claim.

    MUTATION CHECK: restore the old `save_response_warning` string ("no
    'entities' in metadata — fact ineligible for Tier 3 consolidation") and
    this test fails on the second assertion."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _make_request({
            "content": "a fact naming no entities",
            "metadata": {"source": "claude-code", "project": "shared_memory"},
        })
        resp = await c.handle_save(req)
    assert resp.status == 200
    body = json.loads(resp.text)
    assert "fine for Tier 3 consolidation" in body["message"]
    assert "ineligible" not in body["message"]


def test_save_response_warning_pure_function_no_longer_claims_ineligibility():
    """Direct unit test of the pure function, independent of the handle_save
    wiring above — pins the exact contract fact:1215 corrects."""
    warn = coordinator_mod.save_response_warning("fact", [], None)
    assert "fine for Tier 3 consolidation" in warn
    assert "ineligible" not in warn
    # entities present → no note at all, same as before
    assert coordinator_mod.save_response_warning("fact", ["X"], None) == ""


# ── (e) the outbox path is unaffected by the top-level materialisation ───────

@pytest.mark.asyncio
async def test_decision_domain_materialisation_does_not_change_the_outbox_row():
    """Part 1's outbox question, answered without a gate: `resolve_domains`
    (domain_axis.py) reads the `decision` blob FIRST and only falls back to
    the top level when the blob is empty. The blob already carries the
    asserted list, so materialising it at the top level too changes nothing
    `resolve_domains` returns — the outbox row's cypher_params["domains"] is
    identical to what it was before this release, not doubled.

    MUTATION CHECK: swap the outbox's `resolve_domains(metadata)` call for a
    plain `metadata.get("domains")` read and this test still passes today —
    but if a future change reordered `_domains_from`'s precedence to prefer
    the top level, this test would then see the SAME list (still correct
    here) while a divergent-blob variant (not exercised, deliberately out of
    scope) would catch the regression. This test's job is narrower and
    exact: no doubling, no drift, for the case this PR actually changes."""
    c, mock_conn, _ = _coordinator_with_mocks()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        req = _decision_request(domains=["architecture", "capture"])
        resp = await c.handle_save(req)
    assert resp.status == 200

    params = _outbox_params(mock_conn)
    assert params["domains"] == ["architecture", "capture"]
