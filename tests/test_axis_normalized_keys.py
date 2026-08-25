"""PR-C — projects and domains resolve BY CONCEPT, at save and at search.

WHY THIS EXISTS. Both axis registries answered on exact strings. A save naming
`Orbit_Relay` where the registry holds `orbit-relay` was an unregistered
stranger — refused, with a proposal that was character-for-character what the
caller already meant — and a SEARCH filtering on it matched the literal string
and therefore matched nothing, so the corpus answered "there is nothing here" to
a filter that was merely spelled the way the asker's folder is spelled. The
gateway has always known those are one name: `axis_key()` is what fact:1047's
spelling guard compares on. This makes the key the resolution, not just the
refusal, and does it on BOTH sides of the store.

Coverage:
  - `axis_key` and the SQL `axis_normalize` are asserted against ONE fixture
    list, in this suite and again in migration 035's own apply-time DO block
  - `resolve_axis_value` — the four steps, their order, and the `via` each
    reports; `expand_axis_spellings` — the read-side set, and what it refuses
    to pull in
  - save ingress, projects and domains: each `via` path, and the response
    fields that disclose a rewrite (`project_resolved`, `domains_resolved`)
  - a `new_project` declaration is REFUSED on a key match — against a
    registered name and against a retired one — rather than silently resolved
  - the search predicate binds the expanded SET (`= ANY`), the supplied-entry
    cap is not applied to the expansion, and `filters_resolved` reports what
    was actually searched on every one of handle_search's exits
  - migration 035: the invariant is structural, and the one index that would
    contradict migration 024's shared-alias design is deliberately absent

⛔ NO LIVE DATABASE. Every SQL string here is stubbed, exactly like the axis
tests this sits beside — which is precisely why the Python/SQL key agreement is
enforced by a fixture list the migration re-asserts at apply time rather than by
anything in this file.
"""

import importlib.util
import json
import os
import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCRIPTS = os.path.normpath(os.path.join(_ROOT, "shared-memory", "scripts"))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from project_axis import (  # noqa: E402
    AXIS_KEY_FIXTURES, axis_key, spelling_key,
    resolve_axis_value, expand_axis_spellings,
    VIA_EXACT, VIA_ALIAS, VIA_NORMALISED,
)

_MIGRATION = os.path.normpath(os.path.join(
    _ROOT, "shared-memory", "migrations", "035_axis_normalized_keys.sql"))


def load_coordinator():
    path = os.path.join(_SCRIPTS, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coordinator_mod = load_coordinator()
MemoryCoordinator = coordinator_mod.MemoryCoordinator
_axis_filter_predicate = coordinator_mod._axis_filter_predicate


# ══════════════════════════════════════════════════════════════════════════
# (1) ONE key, two implementations, held together by one fixture list
# ══════════════════════════════════════════════════════════════════════════

def test_the_python_key_answers_the_fixture_list():
    for supplied, expected in AXIS_KEY_FIXTURES:
        assert axis_key(supplied) == expected, supplied


def test_spelling_key_and_axis_key_are_the_same_function():
    """fact:1047's guard and the axis resolver must never be able to disagree
    about whether two names are one name — so they are not two functions."""
    assert spelling_key is axis_key


def test_the_migration_asserts_the_same_fixture_list_verbatim():
    """THE AGREEMENT CHECK. The suite proves the Python key; the migration's own
    DO block proves the SQL key at apply time. Neither proves the OTHER — so
    what this asserts is that both are being held to the SAME list, in the same
    order. Edit one side's expectations and this dies; edit the rule itself and
    the migration raises on the merger's live apply.
    """
    sql = open(_MIGRATION, encoding="utf-8").read()
    block = re.search(r"fixture\s+text\[\]\[\]\s*:=\s*ARRAY\[(.*?)\];",
                      sql, re.S)
    assert block, "migration 035 no longer carries a fixture array"
    pairs = re.findall(r"\['((?:[^']|'')*)'\s*,\s*'((?:[^']|'')*)'\]",
                       block.group(1))
    decoded = tuple((a.replace("''", "'"), b.replace("''", "'"))
                    for a, b in pairs)
    assert decoded == AXIS_KEY_FIXTURES


def test_the_key_ignores_separators_case_and_surrounding_space():
    assert axis_key("Orbit_Relay") == axis_key("orbit-relay") == \
        axis_key("  ORBIT RELAY  ")


def test_a_name_of_pure_punctuation_keys_to_nothing():
    """An empty key must never match another empty key — a name that reduces to
    nothing is not a spelling of every other such name."""
    assert axis_key("---") == ""
    canonical, via = resolve_axis_value("###", ["---"], {})
    assert (canonical, via) == (None, None)


# ══════════════════════════════════════════════════════════════════════════
# (2) resolve_axis_value — the four steps and their order
# ══════════════════════════════════════════════════════════════════════════

REGISTERED = ["orbit-relay", "alpha-service"]
ALIASES = {"Orbit_Relay_Old": "orbit-relay"}


def test_step_1_the_registry_exactly():
    assert resolve_axis_value("orbit-relay", REGISTERED, ALIASES) == \
        ("orbit-relay", VIA_EXACT)


def test_step_2_an_active_alias_exactly():
    assert resolve_axis_value("Orbit_Relay_Old", REGISTERED, ALIASES) == \
        ("orbit-relay", VIA_ALIAS)


def test_step_3_the_registry_by_key():
    """MUTATION CHECK ANCHOR: delete the registry by-key loop in
    `resolve_axis_value` and this dies — a separator/case variant of a live
    project goes back to being an unregistered stranger."""
    assert resolve_axis_value("Orbit_Relay", REGISTERED, ALIASES) == \
        ("orbit-relay", VIA_NORMALISED)
    assert resolve_axis_value("ORBIT RELAY", REGISTERED, ALIASES) == \
        ("orbit-relay", VIA_NORMALISED)


def test_step_4_an_active_alias_by_key():
    """MUTATION CHECK ANCHOR: delete the alias by-key loop and this dies. It is
    the likelier of the two in practice — the machine that still carries the old
    folder name is also the one spelling it with its own separators."""
    assert resolve_axis_value("orbit relay old", REGISTERED, ALIASES) == \
        ("orbit-relay", VIA_NORMALISED)


def test_an_exact_registry_hit_beats_a_key_hit_on_another_name():
    """Order, not coincidence. A name that IS on file must be answered by
    itself — never by something that merely keys the same as it."""
    registered = ["Ops_2026", "ops2026"]
    assert resolve_axis_value("ops2026", registered, {}) == \
        ("ops2026", VIA_EXACT)


def test_an_exact_alias_hit_beats_a_key_hit_elsewhere():
    canonical, via = resolve_axis_value(
        "legacy", ["Legacy", "current"], {"legacy": "current"})
    assert (canonical, via) == ("current", VIA_ALIAS)


def test_an_unknown_value_resolves_to_nothing():
    assert resolve_axis_value("no-such-thing", REGISTERED, ALIASES) == (None, None)


def test_non_strings_and_blanks_resolve_to_nothing_rather_than_raising():
    """This sits on the ingress path, where metadata is client-supplied and
    untrusted."""
    for value in (None, 7, "", "   ", ["orbit-relay"]):
        assert resolve_axis_value(value, REGISTERED, ALIASES) == (None, None)


def test_resolution_never_walks_a_chain():
    """A3 — chains are collapsed when a rename is WRITTEN, so an alias always
    points DIRECTLY at a canonical. Following a second hop here would put a walk
    that can cycle on the ingress path."""
    aliases = {"a": "b", "b": "c"}
    assert resolve_axis_value("a", ["c"], aliases) == ("b", VIA_ALIAS)


# ══════════════════════════════════════════════════════════════════════════
# (3) expand_axis_spellings — the read side
# ══════════════════════════════════════════════════════════════════════════

def test_the_expansion_is_canonical_first_then_stable():
    got = expand_axis_spellings(
        "orbit-relay", ["orbit-relay", "alpha-service"],
        {"Orbit_Relay_Old": "orbit-relay", "other": "alpha-service"})
    assert got[0] == "orbit-relay"
    assert got == ["orbit-relay", "Orbit_Relay_Old"]


def test_the_expansion_includes_a_registered_variant_sharing_the_key():
    """Legacy data: migration 035 stops a second spelling being REGISTERED, but
    a database that has not applied it yet may still hold one, and the filter
    must reach records written under it."""
    got = expand_axis_spellings("orbit-relay",
                                ["orbit-relay", "Orbit_Relay"], {})
    assert set(got) == {"orbit-relay", "Orbit_Relay"}


def test_the_expansion_refuses_an_alias_that_means_something_else():
    """⛔ An alias keying the same as this canonical but pointing at a DIFFERENT
    one is an ambiguity, not a synonym. Sweeping it in would put another
    project's records inside this project's answer."""
    got = expand_axis_spellings(
        "orbit-relay", ["orbit-relay", "orbit-relay-monitor"],
        {"orbit relay": "orbit-relay-monitor"})
    assert got == ["orbit-relay"]


def test_expanding_nothing_yields_nothing():
    assert expand_axis_spellings(None, REGISTERED, ALIASES) == []
    assert expand_axis_spellings("  ", REGISTERED, ALIASES) == []


# ══════════════════════════════════════════════════════════════════════════
# (4) Save ingress — projects
# ══════════════════════════════════════════════════════════════════════════

def _acquire_stub(fetch):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=fetch)
    conn.fetchval = AsyncMock(return_value=None)
    acq = MagicMock()
    acq.__aenter__ = AsyncMock(return_value=conn)
    acq.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=acq), conn


def _project_coord(registered=(), aliases=None, near=()):
    """A coordinator whose project registry answers as a SET.

    `_project_registered` and `_resolve_project_alias` are the EXACT-match
    primitives (steps 1 and 2); the connection answers the set queries the by-key
    steps read (steps 3 and 4). Keeping them separate is what lets a test put a
    name in the set without also making the exact lookups find it.
    """
    aliases = dict(aliases or {})
    c = MemoryCoordinator()
    c._project_registered = AsyncMock(side_effect=lambda n: n in registered)
    c._resolve_project_alias = AsyncMock(side_effect=lambda n: aliases.get(n))
    c._register_project = AsyncMock()
    c._project_proposals = AsyncMock(return_value=[])

    alias_rows = [{"alias": a, "canonical": t} for a, t in aliases.items()]

    def fetch(sql, *args):
        if "project_aliases" in sql:
            return alias_rows
        if "similarity" in sql:
            return [{"name": n} for n in near]
        return [{"name": n} for n in registered]

    c._acquire, _conn = _acquire_stub(fetch)
    return c


@pytest.mark.asyncio
async def test_an_exact_registry_hit_changes_nothing_and_reports_nothing():
    c = _project_coord(registered=("orbit-relay",))
    md = {"source": "c", "project": "orbit-relay"}
    report = {}
    assert await c._project_ingress_error(md, "c", report) is None
    assert md["project"] == "orbit-relay"
    assert report == {}


@pytest.mark.asyncio
async def test_an_alias_is_rewritten_and_disclosed_as_via_alias():
    c = _project_coord(registered=("orbit-relay",),
                       aliases={"Orbit_Relay_Old": "orbit-relay"})
    md = {"source": "c", "project": "Orbit_Relay_Old"}
    report = {}
    assert await c._project_ingress_error(md, "c", report) is None
    assert md["project"] == "orbit-relay"
    assert report["project_resolved"] == {
        "supplied": "Orbit_Relay_Old", "canonical": "orbit-relay",
        "via": VIA_ALIAS,
    }


@pytest.mark.asyncio
async def test_a_spelling_variant_now_resolves_instead_of_being_refused():
    """THE POINT OF THE RELEASE. `Orbit_Relay` used to be an unregistered
    stranger refused with a proposal identical in meaning to what was sent.

    MUTATION CHECK: delete the by-key block at the end of
    `_project_ingress_error` (the `_project_spellings` / `resolve_axis_value`
    pair) and this test dies with `project_unknown`."""
    c = _project_coord(registered=("orbit-relay",))
    md = {"source": "c", "project": "Orbit_Relay"}
    report = {}
    assert await c._project_ingress_error(md, "c", report) is None
    assert md["project"] == "orbit-relay"
    assert report["project_resolved"] == {
        "supplied": "Orbit_Relay", "canonical": "orbit-relay",
        "via": VIA_NORMALISED,
    }


@pytest.mark.asyncio
async def test_a_variant_of_a_RETIRED_spelling_resolves_too():
    c = _project_coord(registered=("orbit-relay",),
                       aliases={"Orbit_Relay_Old": "orbit-relay"})
    md = {"source": "c", "project": "orbit relay old"}
    report = {}
    assert await c._project_ingress_error(md, "c", report) is None
    assert md["project"] == "orbit-relay"
    assert report["project_resolved"]["via"] == VIA_NORMALISED


@pytest.mark.asyncio
async def test_every_carrier_of_the_supplied_spelling_moves():
    """A record whose top-level project and decision blob disagree about which
    project it belongs to is a record whose Postgres metadata and graph axis
    disagree — the shadowed-field defect PROJECT_MATCH_SQL exists to warn about."""
    c = _project_coord(registered=("orbit-relay",))
    md = {"type": "decision", "project": "Orbit_Relay",
          "decision": {"project": "Orbit_Relay"}}
    assert await c._project_ingress_error(md, "c", {}) is None
    assert md["project"] == "orbit-relay"
    assert md["decision"]["project"] == "orbit-relay"


@pytest.mark.asyncio
async def test_a_carrier_naming_a_DIFFERENT_project_is_never_clobbered():
    c = _project_coord(registered=("orbit-relay", "alpha-service"))
    md = {"type": "decision", "project": "alpha-service",
          "decision": {"project": "Orbit_Relay"}}
    assert await c._project_ingress_error(md, "c", {}) is None
    assert md["decision"]["project"] == "orbit-relay"
    assert md["project"] == "alpha-service"


@pytest.mark.asyncio
async def test_a_genuinely_unknown_project_is_still_refused_unchanged():
    c = _project_coord(registered=("orbit-relay",))
    err = await c._project_ingress_error(
        {"source": "c", "project": "something-else"}, "c", {})
    assert err["error"] == "project_unknown"


@pytest.mark.asyncio
async def test_a_registry_read_failure_degrades_to_the_old_behaviour():
    """The by-key step must never turn a transient database fault into a
    different ANSWER — it degrades to the exact-match behaviour that shipped
    before it, which is the refusal the caller would have got anyway."""
    c = _project_coord(registered=("orbit-relay",))
    c._acquire = MagicMock(side_effect=RuntimeError("pool is gone"))
    err = await c._project_ingress_error(
        {"source": "c", "project": "Orbit_Relay"}, "c", {})
    assert err["error"] == "project_unknown"


# ── new_project is TOLD, not silently resolved ───────────────────────────────

@pytest.mark.asyncio
async def test_declaring_a_key_variant_as_NEW_is_refused_not_resolved():
    """⛔ THE ORDER RULE. A caller that DECLARES a new project and sends a
    variant of one that exists is asserting something false about the registry,
    and must be told — resolving it silently would store the record correctly and
    lose the only signal that an agent believes it is creating projects that
    already exist."""
    c = _project_coord(registered=("orbit-relay",))
    err = await c._project_ingress_error(
        {"source": "c", "project": "Orbit_Relay", "new_project": True}, "c", {})
    assert err["error"] == "project_spelling_variant"
    assert "orbit-relay" in err["message"]
    c._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_declaring_a_key_variant_of_a_RETIRED_spelling_as_NEW_is_refused():
    """A name that was aliased away has ALREADY been adjudicated on this
    deployment, so a variant of it is the same mistake as a variant of a live
    name. The refusal names the project the alias resolves to, never the alias —
    an alias is not somewhere a record may be saved."""
    c = _project_coord(registered=("orbit-relay",),
                       aliases={"Orbit_Relay_Old": "orbit-relay"})
    err = await c._project_ingress_error(
        {"source": "c", "project": "orbit relay old", "new_project": True},
        "c", {})
    assert err["error"] == "project_spelling_variant"
    assert "orbit-relay" in err["message"]
    c._register_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_genuinely_new_project_still_registers_with_no_extra_step():
    """The guard must stay quiet for the ordinary case, or it trains the reflex
    to override it."""
    c = _project_coord(registered=("orbit-relay",))
    assert await c._project_ingress_error(
        {"source": "c", "project": "unrelated-thing", "new_project": True},
        "c", {}) is None
    c._register_project.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════
# (5) Save ingress — domains, the same four steps inside one project
# ══════════════════════════════════════════════════════════════════════════

def _domain_coord(registered=(), aliases=None, near=(), project_id=6):
    """`registered` is a set of section names of project id `project_id`;
    `aliases` maps alias → canonical section."""
    aliases = dict(aliases or {})
    c = MemoryCoordinator()
    c._project_identity = AsyncMock(return_value=project_id)
    c._project_registered = AsyncMock(return_value=True)
    c._domain_registered = AsyncMock(
        side_effect=lambda pid, n: n in registered)
    c._resolve_domain_alias = AsyncMock(
        side_effect=lambda pid, n: aliases.get(n))
    c._register_domain = AsyncMock()
    c._domain_proposals = AsyncMock(return_value=[])

    alias_rows = [{"alias": a, "canonical": t} for a, t in aliases.items()]

    def fetch(sql, *args):
        if "domain_aliases" in sql:
            return alias_rows
        if "similarity" in sql:
            return [{"name": n} for n in near]
        return [{"name": n} for n in registered]

    c._acquire, _conn = _acquire_stub(fetch)
    return c


@pytest.mark.asyncio
async def test_a_registered_section_changes_nothing_and_reports_nothing():
    c = _domain_coord(registered=("graph-quality",))
    md = {"project": "p", "domain": "graph-quality"}
    report = {}
    assert await c._domain_ingress_error(md, "c", report) is None
    assert report == {}


@pytest.mark.asyncio
async def test_a_section_alias_is_rewritten_and_disclosed():
    c = _domain_coord(registered=("graph-quality",),
                      aliases={"graphs": "graph-quality"})
    md = {"project": "p", "domain": "graphs"}
    report = {}
    assert await c._domain_ingress_error(md, "c", report) is None
    assert md["domain"] == "graph-quality"
    assert report["domains_resolved"] == [
        {"supplied": "graphs", "canonical": "graph-quality", "via": VIA_ALIAS},
    ]


@pytest.mark.asyncio
async def test_a_section_spelling_variant_resolves_instead_of_being_refused():
    """MUTATION CHECK: delete the by-key block at the end of
    `_domain_value_error` and this dies with `domain_unknown`. Section names are
    ordinary words typed by different people at different times, so this is the
    axis where the variant is the COMMON case, not the exotic one."""
    c = _domain_coord(registered=("graph-quality",))
    md = {"project": "p", "domain": "Graph Quality"}
    report = {}
    assert await c._domain_ingress_error(md, "c", report) is None
    assert md["domain"] == "graph-quality"
    assert report["domains_resolved"] == [
        {"supplied": "Graph Quality", "canonical": "graph-quality",
         "via": VIA_NORMALISED},
    ]


@pytest.mark.asyncio
async def test_only_the_values_that_MOVED_are_reported():
    """A record may name several sections. The caller needs to know which of the
    values IT sent moved — reporting the ones that did not would bury that."""
    c = _domain_coord(registered=("graph-quality", "operations"))
    md = {"project": "p", "domains": ["operations", "Graph_Quality"]}
    report = {}
    assert await c._domain_ingress_error(md, "c", report) is None
    assert md["domains"] == ["operations", "graph-quality"]
    assert [e["supplied"] for e in report["domains_resolved"]] == ["Graph_Quality"]


@pytest.mark.asyncio
async def test_an_unknown_section_is_still_refused_unchanged():
    c = _domain_coord(registered=("graph-quality",))
    err = await c._domain_ingress_error(
        {"project": "p", "domain": "something-else"}, "c", {})
    assert err["error"] == "domain_unknown"


@pytest.mark.asyncio
async def test_declaring_a_section_key_variant_as_NEW_is_refused():
    c = _domain_coord(registered=("graph-quality",))
    err = await c._domain_ingress_error(
        {"project": "p", "domain": "Graph_Quality", "new_domain": True}, "c", {})
    assert err["error"] == "domain_spelling_variant"
    c._register_domain.assert_not_awaited()


@pytest.mark.asyncio
async def test_declaring_a_variant_of_a_RETIRED_section_as_NEW_is_refused():
    c = _domain_coord(registered=("graph-quality",),
                      aliases={"graph_quality_old": "graph-quality"})
    err = await c._domain_ingress_error(
        {"project": "p", "domain": "Graph Quality Old", "new_domain": True},
        "c", {})
    assert err["error"] == "domain_spelling_variant"
    c._register_domain.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════
# (6) The save RESPONSE discloses what was rewritten
# ══════════════════════════════════════════════════════════════════════════

class _AsyncCtx:
    def __init__(self, val):
        self._val = val
    async def __aenter__(self):
        return self._val
    async def __aexit__(self, *_):
        pass


def _save_request(body: dict) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(return_value=None)
    return req


def _full_coord(registered=(), aliases=None):
    """Enough pool/neo4j to drive handle_save end to end (the fixture shape
    test_entity_vocabulary_ingress.py uses), with the project axis answering
    from a set."""
    aliases = dict(aliases or {})
    c = MemoryCoordinator()
    c._project_registered = AsyncMock(side_effect=lambda n: n in registered)
    c._resolve_project_alias = AsyncMock(side_effect=lambda n: aliases.get(n))
    c._register_project = AsyncMock()
    c._project_proposals = AsyncMock(return_value=[])
    c._project_spellings = AsyncMock(return_value=(list(registered), aliases))
    c._entity_vocab_resolve_many = AsyncMock(return_value={})

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.fetchval = AsyncMock(return_value=1)
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
    return c


async def _save(c, metadata):
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_save_request(
            {"content": "a fact", "agent_id": "claude-code",
             "metadata": metadata}))
    assert resp.status == 200, resp.body
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_the_save_response_discloses_a_rewritten_project():
    """MUTATION CHECK: drop `project_resolved` from handle_save's success body
    and this dies — the caller's record lands under a name it never sent and it
    has no way to learn that, which is the disclosure gap `entities_rewritten`
    was added to close on the entity axis."""
    c = _full_coord(registered=("orbit-relay",))
    data = await _save(c, {"source": "claude-code", "project": "Orbit_Relay"})
    assert data["project_resolved"] == {
        "supplied": "Orbit_Relay", "canonical": "orbit-relay",
        "via": VIA_NORMALISED,
    }


@pytest.mark.asyncio
async def test_the_save_response_is_null_when_nothing_was_rewritten():
    """A caller sending the canonical value has nothing to reconcile, and must
    not be handed a field that looks like it does."""
    c = _full_coord(registered=("orbit-relay",))
    data = await _save(c, {"source": "claude-code", "project": "orbit-relay"})
    assert data["project_resolved"] is None
    assert data["domains_resolved"] is None


# ══════════════════════════════════════════════════════════════════════════
# (7) Search — the filter matches the SET, and says what it searched
# ══════════════════════════════════════════════════════════════════════════

def _search_coord(registered=(), aliases=None, domains=(), domain_aliases=None):
    c = MemoryCoordinator()
    c._project_spellings = AsyncMock(
        return_value=(list(registered), dict(aliases or {})))
    # ⚠ THE STUB HONOURS THE SCOPE ARGUMENT. Returning the section set for a
    # None project id would fake the one thing this axis must not do — answer a
    # section name outside any project — and would let a test pass that the real
    # `_domain_spellings` fails.
    c._domain_spellings = AsyncMock(side_effect=lambda pid: (
        (list(domains), dict(domain_aliases or {})) if pid is not None
        else ([], {})))
    c._project_identity = AsyncMock(return_value=6)

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_AsyncCtx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    c._pool = pool

    session = AsyncMock()

    class _Rows:
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration

    session.run = AsyncMock(return_value=_Rows())
    neo4j = MagicMock()
    neo4j.session = MagicMock(return_value=_AsyncCtx(session))
    c._neo4j = neo4j
    return c, conn


async def _search(c, body):
    reranker = MagicMock()
    reranker.raise_for_status = MagicMock()
    reranker.json = MagicMock(return_value={"results": []})
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        with patch("httpx.AsyncClient") as mock_cls:
            http = AsyncMock()
            http.post = AsyncMock(return_value=reranker)
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(_save_request(body))
    assert resp.status == 200, resp.text
    return json.loads(resp.text)


@pytest.mark.asyncio
async def test_a_filter_spelled_differently_now_finds_the_project():
    """THE READ-SIDE POINT. Before this, a search filtered on `Orbit_Relay`
    matched the literal string and therefore matched nothing — an empty answer
    that reads as "there is nothing filed there"."""
    c, conn = _search_coord(registered=("orbit-relay",),
                            aliases={"Orbit_Relay_Old": "orbit-relay"})
    result = await _search(c, {"query": "status", "project": "Orbit_Relay"})
    assert result["filters_resolved"]["project"] == {
        "supplied": "Orbit_Relay", "canonical": "orbit-relay",
        "matched": ["orbit-relay", "Orbit_Relay_Old"],
    }
    calls = [call for call in conn.fetch.call_args_list
             if "FROM technical_docs" in call.args[0]]
    assert calls
    for call in calls:
        assert "metadata->>'project' = ANY($" in call.args[0]
        assert ["orbit-relay", "Orbit_Relay_Old"] in call.args


@pytest.mark.asyncio
async def test_an_unresolvable_filter_keeps_todays_behaviour_and_says_so():
    """Not an error, not widened: the literal string, matching whatever carries
    it. `canonical: null` is what lets a reader tell "nothing is filed there"
    from "that is not a place"."""
    c, conn = _search_coord(registered=("orbit-relay",))
    result = await _search(c, {"query": "status", "project": "no-such-project"})
    assert result["filters_resolved"]["project"] == {
        "supplied": "no-such-project", "canonical": None,
        "matched": ["no-such-project"],
    }


@pytest.mark.asyncio
async def test_domain_filters_expand_within_the_resolved_project():
    c, conn = _search_coord(registered=("orbit-relay",),
                            domains=("graph-quality",),
                            domain_aliases={"graphs": "graph-quality"})
    result = await _search(c, {"query": "status", "project": "orbit-relay",
                               "domains": ["Graph Quality"]})
    assert result["filters_resolved"]["domains"] == [
        {"supplied": "Graph Quality", "canonical": "graph-quality",
         "matched": ["graph-quality", "graphs"]},
    ]
    calls = [call for call in conn.fetch.call_args_list
             if "FROM technical_docs" in call.args[0]]
    for call in calls:
        assert ["graph-quality", "graphs"] in call.args


@pytest.mark.asyncio
async def test_domains_stay_literal_with_no_project_to_scope_them_by():
    """⚠ A section is identified by (project, name) and by nothing else, so with
    no project filter there is no scope to resolve one in. The same absence
    `domain_axis` calls load-bearing: the one way this axis reproduces the
    project axis' original defect is by letting a name answer on its own."""
    c, conn = _search_coord(registered=("orbit-relay",),
                            domains=("graph-quality",))
    result = await _search(c, {"query": "status", "domains": ["Graph Quality"]})
    assert result["filters_resolved"]["domains"] == [
        {"supplied": "Graph Quality", "canonical": None,
         "matched": ["Graph Quality"]},
    ]
    c._domain_spellings.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_the_cap_bounds_what_the_CALLER_SENT_not_what_the_server_expanded():
    """⛔ The cap exists to bound what an UNTRUSTED caller can make the database
    scan. The expansion is the server's own answer from its own registry.
    Applying the cap after expansion would let a deployment that has recorded a
    few renames silently lose filter entries — a partial filter whose empty
    result reads as authoritative, which is the failure the cap was written to
    prevent."""
    cap = coordinator_mod.SEARCH_DOMAINS_FILTER_CAP
    supplied = [f"section-{i}" for i in range(cap)]
    # Every supplied entry expands to two spellings — 2x the cap after expansion.
    domains = supplied
    aliases = {f"section_{i}_old": f"section-{i}" for i in range(cap)}
    c, conn = _search_coord(registered=("orbit-relay",), domains=domains,
                            domain_aliases=aliases)
    result = await _search(c, {"query": "status", "project": "orbit-relay",
                               "domains": supplied})
    assert [e["supplied"] for e in result["filters_resolved"]["domains"]] == supplied
    calls = [call for call in conn.fetch.call_args_list
             if "FROM technical_docs" in call.args[0]]
    assert calls
    bound = [a for a in calls[0].args if isinstance(a, list)]
    expanded = next(b for b in bound if "section-0" in b)
    assert len(expanded) == 2 * cap


@pytest.mark.asyncio
async def test_an_unfiltered_search_carries_no_filters_resolved_at_all():
    """Additive, and ABSENT rather than null — an unfiltered search's body is
    byte-for-byte what it was before the key existed, which is why api_version
    does not move."""
    c, conn = _search_coord(registered=("orbit-relay",))
    result = await _search(c, {"query": "status"})
    assert "filters_resolved" not in result
    c._project_spellings.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_keyword_fallback_reports_the_same_resolution():
    """The path a reader is least likely to have tested, and the one where a
    missing account would matter most — the embedder being down must not also
    silently change what the filter meant."""
    c, conn = _search_coord(registered=("orbit-relay",))
    with patch.object(c, "_embed", new=AsyncMock(side_effect=RuntimeError("down"))):
        with patch("httpx.AsyncClient") as mock_cls:
            http = AsyncMock()
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=http)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            resp = await c.handle_search(
                _save_request({"query": "status", "project": "Orbit_Relay"}))
    result = json.loads(resp.text)
    assert result["fallback"] == "keyword"
    assert result["filters_resolved"]["project"]["canonical"] == "orbit-relay"


# ══════════════════════════════════════════════════════════════════════════
# (8) Migration 035 — the invariant is structural, and one index is absent
# ══════════════════════════════════════════════════════════════════════════

def _migration_sql():
    return open(_MIGRATION, encoding="utf-8").read()


def test_the_migration_declares_the_key_as_an_immutable_function():
    sql = _migration_sql()
    assert "CREATE OR REPLACE FUNCTION axis_normalize(name text)" in sql
    assert "IMMUTABLE" in sql


def test_two_registered_names_never_share_a_key_is_structural():
    """The new invariant, as an index rather than as a hope. `projects` globally;
    `project_domains` within its project, because two projects may both have a
    `graph-quality` section and they are different sections."""
    sql = _migration_sql()
    assert "UNIQUE INDEX IF NOT EXISTS projects_axis_key_uniq" in sql
    assert "ON projects (axis_normalize(name))" in sql
    assert "UNIQUE INDEX IF NOT EXISTS project_domains_axis_key_uniq" in sql
    assert "ON project_domains (project_id, axis_normalize(name))" in sql


def test_there_is_deliberately_no_unique_key_index_on_the_shared_alias_table():
    """⛔ `aliases` is a shared string-intern table, and migration 024 states in
    the table's own comment that one spelling may legitimately alias on BOTH
    axes. A global key-unique index would forbid that by construction — not
    because the data collides, but because the design allows what the index
    would refuse. What must actually hold is narrower and cross-table, so it is
    asserted at apply time instead."""
    sql = _migration_sql()
    assert "ON aliases (axis_normalize" not in sql
    assert "by-key resolution would answer by luck" in sql
