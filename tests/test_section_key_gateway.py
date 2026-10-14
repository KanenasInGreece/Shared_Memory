"""Gateway half of the section-key change: a record's sections are stored under
`domains` only, a wrong-shaped section is refused, and the record type is
normalised once. Each test names the failure it kills (G1..G14 of the brief).

All SQL is stubbed here; what migration 044 changes is proven on a real
Postgres by the coordinator, not by this file.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_SCRIPTS = _REPO / "shared-memory" / "scripts"
_MIGRATIONS = _REPO / "shared-memory" / "migrations"


def _load(name: str, path: Path):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_STATE: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _gateway_modules():
    """Loading the gateway and apply.py edits sys.path and sys.modules; both are put back afterwards."""
    saved_path = list(sys.path)
    had_apply = "apply" in sys.modules
    saved_apply = sys.modules.get("apply")
    _STATE["coordinator"] = _load("coordinator_section_key", _SCRIPTS / "coordinator.py")
    yield
    _STATE.clear()
    sys.path[:] = saved_path
    if had_apply:
        sys.modules["apply"] = saved_apply
    else:
        sys.modules.pop("apply", None)


class _ctx:
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _request(body: dict) -> MagicMock:
    state = {"authenticated_agent": None, "principal": None}
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.rel_url.query.get = MagicMock(return_value=None)
    req.get = MagicMock(side_effect=lambda k, d=None: state.get(k, d))
    req.__getitem__ = MagicMock(side_effect=lambda k: state.get(k))
    return req


def _coordinator():
    c = _STATE["coordinator"].MemoryCoordinator()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": 99})
    conn.execute = AsyncMock(return_value="DELETE 0")
    conn.fetch = AsyncMock(return_value=[])
    conn.transaction = MagicMock(return_value=_ctx(None))
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_ctx(conn))
    c._pool = pool
    session = AsyncMock()
    neo = MagicMock()
    neo.session = MagicMock(return_value=_ctx(session))
    c._neo4j = neo
    c._entity_vocab_resolve_many = AsyncMock(
        side_effect=lambda names: {n: n for n in names})
    return c, conn


def _save(metadata: dict, content: str = "a plain observation about widgets"):
    meta = {"source": "claude-code", "project": "widget_works", **metadata}
    return _request({"content": content, "metadata": meta})


async def _run(metadata: dict, c=None, conn=None, content="a plain observation about widgets"):
    if c is None:
        c, conn = _coordinator()
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_save(metadata, content))
    return resp, c, conn


def _stored(conn) -> dict:
    return conn.fetchrow.await_args.args[2]


def _outbox_domains(conn):
    call = next(x for x in conn.execute.call_args_list if "neo4j_outbox" in x.args[0])
    return call.args[2]


def _decision(**blob):
    return {
        "type": "decision",
        "decision": {"decided_by": "Operator", "project": "widget_works",
                     "rationale": "a reason", **blob},
    }


# ── G1, G2, G3, G5: where a fact's sections are stored ────────────────────────

@pytest.mark.asyncio
async def test_g1_g2_a_fact_sent_with_only_domain_is_stored_under_domains_only():
    resp, _, conn = await _run({"domain": "assembly"})
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["assembly"]
    assert "domain" not in stored


@pytest.mark.asyncio
@pytest.mark.parametrize("sections", [
    {"domain": ""}, {"domain": "  "}, {"domains": []}, {"domains": ["", " "]},
    {"domain": None}, {"domains": None},
])
async def test_g3_a_record_that_names_no_section_gains_no_domains_and_keeps_no_blank_domain(sections):
    resp, _, conn = await _run(sections)
    assert resp.status == 200
    stored = _stored(conn)
    assert "domain" not in stored
    assert "domains" not in stored


@pytest.mark.asyncio
async def test_g5_the_stored_domains_hold_the_registered_spelling_not_the_one_sent():
    c, conn = _coordinator()
    c._domain_registered = AsyncMock(return_value=False)
    c._resolve_domain_alias = AsyncMock(return_value="Assembly-Line")
    resp, _, conn = await _run({"domain": "old assembly"}, c, conn)
    assert resp.status == 200
    assert _stored(conn)["domains"] == ["Assembly-Line"]


@pytest.mark.asyncio
async def test_g1_both_keys_on_one_fact_are_merged_into_domains():
    resp, _, conn = await _run({"domain": "assembly", "domains": ["paint"]})
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["paint", "assembly"] or sorted(stored["domains"]) == ["assembly", "paint"]
    assert "domain" not in stored


# ── G4: a decision is stored as before ────────────────────────────────────────

@pytest.mark.asyncio
async def test_g4_a_decisions_stored_domains_and_blob_are_unchanged():
    resp, _, conn = await _run(_decision(domains=["assembly", "paint"]))
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["assembly", "paint"]
    assert stored["decision"]["domains"] == ["assembly", "paint"]
    assert stored["decision"] == {
        "decided_by": "Operator", "project": "widget_works",
        "rationale": "a reason", "domains": ["assembly", "paint"],
    }
    assert "domain" not in stored["decision"]


@pytest.mark.asyncio
async def test_g4_a_decision_blob_with_the_singular_key_keeps_the_blob_as_sent():
    resp, _, conn = await _run(_decision(domain="assembly"))
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["assembly"]
    assert stored["decision"]["domain"] == "assembly"


# ── G6, G7, G8: the record type is normalised once ────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["Retrospective", " retrospective ", "RETROSPECTIVE"])
async def test_g6_a_retrospective_in_another_spelling_with_a_section_is_refused(spelling):
    resp, _, conn = await _run({"type": spelling, "domain": "assembly"})
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "use_retrospective_endpoint"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_g7_a_decision_in_another_spelling_faces_the_decision_shape_check():
    resp, _, conn = await _run({"type": "Decision"})
    assert resp.status == 400
    assert "decision save missing" in json.loads(resp.text)["message"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_g7_a_decision_in_another_spelling_is_stored_as_decision():
    meta = _decision(domains=["assembly"])
    meta["type"] = " Decision "
    resp, _, conn = await _run(meta)
    assert resp.status == 200
    assert _stored(conn)["type"] == "decision"


_INTERNAL = ["supersede", "project_of", "domain_of", "Supersede", " domain_of ", "PROJECT_OF", "bogus"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", _INTERNAL)
async def test_t1a_an_internal_or_unknown_type_is_refused_and_nothing_is_stored_or_enqueued(sent):
    resp, _, conn = await _run({"type": sent})
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "unknown_type"
    conn.fetchrow.assert_not_awaited()
    assert not [x for x in conn.execute.call_args_list if "neo4j_outbox" in x.args[0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["", "  ", "fact", "Fact", " FACT ", 5, ["fact"]])
async def test_t1b_a_fact_is_accepted_and_a_client_type_is_stored_canonical(sent):
    resp, _, conn = await _run({"type": sent})
    assert resp.status == 200, resp.text
    expected = sent.strip().lower() if isinstance(sent, str) and sent.strip().lower() == "fact" else sent
    assert _stored(conn)["type"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["decision", "Decision"])
async def test_t1b_a_well_formed_decision_is_accepted(sent):
    meta = _decision(domains=["assembly"])
    meta["type"] = sent
    resp, _, _ = await _run(meta)
    assert resp.status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["retrospective", "Retrospective", " RETROSPECTIVE "])
async def test_t1b_a_well_formed_retrospective_is_rejected(sent):
    resp, _, conn = await _run({"type": sent})
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "use_retrospective_endpoint"
    conn.fetchrow.assert_not_awaited()


def test_t1d_the_outbox_whitelist_still_admits_the_internal_row_types():
    for t in ("fact", "decision", "retrospective", "supersede", "project_of", "domain_of"):
        _STATE["coordinator"]._require_outbox_type({"type": t})


@pytest.mark.asyncio
async def test_g8_a_missing_type_is_not_added():
    resp, _, conn = await _run({})
    assert resp.status == 200
    assert "type" not in _stored(conn)


# ── G9, G10, G11: the shape check ─────────────────────────────────────────────

_BAD = [{"a": 1}, 5, True, 1.5, [["nested"]], ["a", 5], [None], [{"a": 1}]]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["domain", "domains"])
@pytest.mark.parametrize("bad", _BAD)
async def test_g9_a_wrong_shaped_section_at_the_top_level_is_refused_and_nothing_is_stored(key, bad):
    resp, _, conn = await _run({key: bad})
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "domain_shape_invalid"
    assert key in body["message"]
    assert "a section is a string or a list of strings" in body["message"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["domain", "domains"])
@pytest.mark.parametrize("bad", _BAD)
async def test_g9_a_wrong_shaped_section_in_a_decision_blob_is_refused(key, bad):
    resp, _, conn = await _run(_decision(**{key: bad}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "domain_shape_invalid"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["domain", "domains"])
@pytest.mark.parametrize("bad", _BAD)
async def test_g9_a_fact_whose_decision_blob_alone_carries_a_wrong_shaped_section_is_refused(key, bad):
    resp, _, conn = await _run({"decision": {key: bad}})
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "fact_cannot_carry_decision"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_g9_the_save_route_refuses_a_retrospective_before_the_section_shape():
    resp, _, _ = await _run({"type": "retrospective", "domain": {"a": 1}})
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "use_retrospective_endpoint"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["domain", "domains"])
@pytest.mark.parametrize("ok", [None, "", [], ["", " "], "assembly", ["assembly"]])
async def test_g10_empty_and_null_sections_are_not_refused(key, ok):
    resp, _, conn = await _run({key: ok})
    assert resp.status == 200, resp.text


@pytest.mark.asyncio
async def test_g11_a_shape_refusal_is_counted_on_the_counter_it_maps_to():
    c, conn = _coordinator()
    key = c._REFUSAL_COUNTER["domain_shape_invalid"]
    before = c._registry_refusals.snapshot()
    resp, _, _ = await _run({"domain": 5}, c, conn)
    assert resp.status == 400
    after = c._registry_refusals.snapshot()
    assert key == "new_domain_refused"
    assert after[key] == before.get(key, 0) + 1
    assert {k: v for k, v in after.items() if k != key} == \
        {k: v for k, v in before.items() if k != key}


# ── the stored side of the conflict check is unchanged: the union of both keys ──

@pytest.mark.asyncio
async def test_a_stored_singular_beside_an_empty_plural_still_conflicts_with_a_save_naming_nothing():
    c, _ = _coordinator()
    stored = {"project": "widget_works", "domain": "A", "domains": []}
    err = await c._axis_conflict_error(stored, "widget_works", [], [], False)
    assert err is not None and err["error"] == "axis_conflict" and err["axis"] == "domains"


# ── outbox and graph payload unchanged ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_outbox_payload_carries_the_same_sections():
    _, _, conn = await _run({"domain": "assembly"})
    assert _outbox_domains(conn)["domains"] == ["assembly"]


# ── T2: a decision that names a project faces the project check ──────────────
# Retrospectives no longer reach this check on handle_save. The same project
# outcomes are pinned on a decision, which still owns its project.

def _registry(c, registered=(), aliases=None):
    aliases = dict(aliases or {})
    c._project_registered = AsyncMock(side_effect=lambda n: n in registered)
    c._resolve_project_alias = AsyncMock(side_effect=lambda n: aliases.get(n))
    c._project_spellings = AsyncMock(return_value=(list(registered), aliases, None))
    c._register_project = AsyncMock()
    return c


def _decision_on(project, spelling="decision"):
    return {
        "type": spelling,
        "project": project,
        "decision": {
            "decided_by": "Operator",
            "project": project,
            "rationale": "a reason",
        },
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["decision", "Decision"])
async def test_t2a_a_decision_naming_an_unregistered_project_is_refused(spelling):
    c, conn = _coordinator()
    _registry(c, registered=("orbit-relay",))
    resp, _, _ = await _run(_decision_on("unlisted_thing", spelling), c, conn)
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "project_unknown"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["decision", "Decision"])
async def test_t2b_a_decision_naming_a_variant_or_alias_is_stored_under_the_registered_name(spelling):
    for sent in ("Orbit_Relay", "old_orbit"):
        c, conn = _coordinator()
        _registry(c, registered=("orbit-relay",), aliases={"old_orbit": "orbit-relay"})
        resp, _, _ = await _run(_decision_on(sent, spelling), c, conn)
        assert resp.status == 200, resp.text
        assert _stored(conn)["project"] == "orbit-relay"


@pytest.mark.asyncio
async def test_t2_a_decision_naming_a_registered_project_is_accepted():
    c, conn = _coordinator()
    _registry(c, registered=("orbit-relay",))
    body = {"content": "a verdict", "metadata": {
        "source": "claude-code", **_decision_on("orbit-relay"),
    }}
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(body))
    assert resp.status == 200, resp.text


_MALFORMED_PROJECTS = [12345, ["unlisted_thing"], "   ", "", {"a": 1}, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["decision", "Decision"])
@pytest.mark.parametrize("bad", _MALFORMED_PROJECTS)
async def test_t2e_a_decision_with_a_malformed_or_blank_project_is_refused(spelling, bad):
    c, conn = _coordinator()
    _registry(c, registered=("orbit-relay",))
    body = {"content": "a verdict", "metadata": {
        "source": "claude-code",
        "type": spelling,
        "project": bad,
        "decision": {"decided_by": "Operator", "project": bad, "rationale": "a reason"},
    }}
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(body))
    assert resp.status == 400
    assert "decision save missing" in json.loads(resp.text)["message"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _MALFORMED_PROJECTS)
async def test_t2e_a_decision_with_a_malformed_project_in_its_decision_blob_is_refused(bad):
    c, conn = _coordinator()
    _registry(c, registered=("orbit-relay",))
    body = {"content": "a verdict", "metadata": {
        "source": "claude-code", "type": "decision", "project": "orbit-relay",
        "decision": {"decided_by": "Operator", "project": bad, "rationale": "a reason"}}}
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(body))
    assert resp.status == 400
    assert "decision save missing" in json.loads(resp.text)["message"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("meta", [{"project": None}, {"decision": {"project": None}},
                                  {"project": None, "decision": {"note": "x"}}])
async def test_t2e_a_decision_whose_project_is_null_or_absent_is_refused(meta):
    c, conn = _coordinator()
    _registry(c, registered=("orbit-relay",))
    body = {"content": "a verdict", "metadata": {"source": "claude-code", "type": "decision", **meta}}
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_save(_request(body))
    assert resp.status == 400
    assert "decision save missing" in json.loads(resp.text)["message"]
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_t2d_the_dedicated_retrospective_endpoint_never_runs_the_project_check():
    c, conn = _coordinator()
    _registry(c, registered=())
    c._project_ingress_error = AsyncMock(side_effect=AssertionError("project check called"))
    conn.fetchrow = AsyncMock(side_effect=[
        {"id": 240, "type": "decision", "project": "unlisted_thing"},
        {"id": 911},
    ])
    with patch.object(c, "_embed", new=AsyncMock(return_value=[0.1] * 1024)):
        resp = await c.handle_retrospective(_request(
            {"pg_id": 240, "rating": "validated", "notes": "held up", "grounded_in": [700]}))
    assert resp.status == 200, resp.text
    c._project_ingress_error.assert_not_awaited()


# ── T3: another project, a new project and its sections, near-matches ────────

@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["fact", "decision"])
async def test_t3a_a_save_can_create_a_new_project_and_its_section_from_another_project(spelling):
    c, conn = _coordinator()
    _registry(c, registered=("home-project",))
    c._new_project_refusal = AsyncMock(return_value=None)
    c._register_domain = AsyncMock()
    c._project_identity = AsyncMock(return_value=77)
    meta = {"project": "brand_new_thing", "new_project": True,
            "domain": "telemetry", "new_domain": True, "cwd": "/work/home-project"}
    if spelling == "decision":
        meta.update(_decision(project="brand_new_thing"))
        meta["decision"]["project"] = "brand_new_thing"
    resp, _, _ = await _run(meta, c, conn)
    assert resp.status == 200, resp.text
    c._register_project.assert_awaited_once()
    assert c._register_project.await_args.args[0] == "brand_new_thing"
    c._register_domain.assert_awaited_once()


# ── T3b/T3c through handle_save, with the real _new_project_refusal ──────────

def _real_refusal_coordinator(registered=("orbit-relay", "alpha-service"), near=()):
    c, conn = _coordinator()
    _registry(c, registered=registered)
    mod = _STATE["coordinator"]

    async def fetch(sql, *args):
        if sql == mod.CONFUSABLE_SQL:
            return [{"name": n} for n in near]
        if sql == mod.PROJECT_NAMES_SQL:
            return [{"name": n} for n in registered]
        if sql == mod.ACTIVE_ALIASES_SQL:
            return []
        return []

    conn.fetch = AsyncMock(side_effect=fetch)
    return c, conn


def _typed(spelling, **meta):
    body = dict(meta) if spelling == "fact" else {"type": spelling, **meta}
    if spelling == "decision":
        body["decision"] = {
            "decided_by": "Operator",
            "project": meta["project"],
            "rationale": "a reason",
        }
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["fact", "decision"])
async def test_t3b_a_key_identical_spelling_declared_new_is_refused_naming_the_registered_one(spelling):
    c, conn = _real_refusal_coordinator()
    resp, _, _ = await _run(_typed(spelling, project="Orbit_Relay", new_project=True), c, conn)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "project_spelling_variant"
    assert "orbit-relay" in body["message"]
    c._register_project.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["fact", "decision"])
async def test_t3c_a_confusable_name_declared_new_is_challenged(spelling):
    c, conn = _real_refusal_coordinator(near=("alpha-service",))
    resp, _, _ = await _run(_typed(spelling, project="alpha-servize", new_project=True), c, conn)
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["error"] == "project_confusable"
    assert "alpha-service" in body["message"]
    c._register_project.assert_not_awaited()
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["fact", "decision"])
async def test_t3c_naming_the_neighbour_lets_a_confusable_name_register(spelling):
    c, conn = _real_refusal_coordinator(near=("alpha-service",))
    resp, _, _ = await _run(
        _typed(spelling, project="alpha-servize", new_project=True,
               confirm_distinct_from=["alpha-service"]), c, conn)
    assert resp.status == 200, resp.text
    c._register_project.assert_awaited_once()
    assert c._register_project.await_args.args[0] == "alpha-servize"


# ── decisions: top level and blob together ───────────────────────────────────

@pytest.mark.asyncio
async def test_a_decision_naming_a_section_only_at_the_top_level_gets_domains_and_loses_domain():
    meta = _decision()
    meta["domain"] = "assembly"
    resp, _, conn = await _run(meta)
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["assembly"]
    assert "domain" not in stored
    assert stored["decision"] == _decision()["decision"]


@pytest.mark.asyncio
async def test_a_decision_with_sections_in_the_blob_and_the_top_level_stores_the_blobs():
    meta = _decision(domains=["assembly"])
    meta["domain"] = "paint"
    resp, _, conn = await _run(meta)
    assert resp.status == 200
    stored = _stored(conn)
    assert stored["domains"] == ["assembly"]
    assert "domain" not in stored
    assert stored["decision"]["domains"] == ["assembly"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sections", [
    {"domains": []}, {"domains": ["", " "]}, {"domains": ""}, {"domains": " "},
    {"domains": None}, {"domain": "", "domains": []}, {"domain": None, "domains": None},
])
async def test_a_client_sent_domains_that_names_nothing_is_not_stored(sections):
    resp, _, conn = await _run(sections)
    assert resp.status == 200
    stored = _stored(conn)
    assert "domains" not in stored
    assert "domain" not in stored


# ── G13: apply.py prints a migration's notices, and only its own ──────────────

class _Cur:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if sql.startswith("DO") or "NOTICE" in sql:
            self._conn.notices.append(sql.split("NOTICE:")[-1])


class _Conn:
    def __init__(self):
        self.notices = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self)


def _apply_mod():
    sys.modules.pop("apply", None)
    fake = type(sys)("psycopg2")
    with patch.dict(sys.modules, {"psycopg2": fake}):
        return _load("apply_section_key", _MIGRATIONS / "apply.py")


def test_g13_apply_prints_the_notice_a_migration_raised(tmp_path, capsys):
    mod = _apply_mod()
    f = tmp_path / "900_x.sql"
    f.write_text("DO NOTICE: 044: 3 moved\n")
    conn = _Conn()
    mod.apply(conn, f)
    out = capsys.readouterr().out
    assert "applied: 900_x.sql" in out
    assert "044: 3 moved" in out


def test_g13_apply_does_not_show_an_earlier_files_notice(tmp_path, capsys):
    mod = _apply_mod()
    first = tmp_path / "900_a.sql"
    first.write_text("DO NOTICE: first file notice\n")
    second = tmp_path / "901_b.sql"
    second.write_text("SELECT 1\n")
    conn = _Conn()
    mod.apply(conn, first)
    capsys.readouterr()
    mod.apply(conn, second)
    out = capsys.readouterr().out
    assert "applied: 901_b.sql" in out
    assert "first file notice" not in out


# ── G14: the update only reports after the restart ────────────────────────────

_SCRIPT = (_SCRIPTS / "update_framework.sh").read_text()


def _function_source() -> str:
    m = re.search(r"^report_singular_domain_count\(\) \{\n.*?^\}\n", _SCRIPT, re.S | re.M)
    assert m, "report_singular_domain_count not found in update_framework.sh"
    return m.group(0)


def _run_report(command: str) -> subprocess.CompletedProcess:
    prog = (
        'ylw() { echo "$*"; }\nREPO_ROOT=/sandbox\n' + _function_source()
        + f'\nreport_singular_domain_count {command}\necho "rc=$?"\n'
    )
    return subprocess.run(["bash", "-c", prog], capture_output=True, text=True, timeout=30)


def test_g14_a_nonzero_count_prints_the_number_and_the_repair_command():
    r = _run_report("echo 7")
    assert "7" in r.stdout
    assert "apply.py 044_fact_domains_plural_key.sql --force" in r.stdout
    assert "uv run --with psycopg2-binary python" in r.stdout
    assert "disagree" in r.stdout
    assert "rc=0" in r.stdout


def test_g14_a_zero_count_prints_no_repair_command():
    r = _run_report("echo 0")
    assert ": 0" in r.stdout
    assert "--force" not in r.stdout


def test_g14_a_failing_count_never_fails_the_update():
    r = _run_report("false")
    assert "rc=0" in r.stdout
    assert "--force" not in r.stdout


def _executed_lines(text: str) -> list[str]:
    """Statements with backslash continuations joined; echo/printf/comment lines dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l.strip() for l in joined.splitlines()
            if l.strip() and not re.match(r"\s*(echo|printf|#)", l)]


def test_g14_the_step_runs_soft_after_the_restart_and_is_not_a_rewrite():
    lines = _SCRIPT.splitlines()
    restart = next(i for i, l in enumerate(lines) if l.startswith('run "restart the gateway"'))
    step = next(i for i, l in enumerate(lines) if l.startswith('run_soft "count facts still carrying'))
    assert step > restart
    end = step
    while lines[end].rstrip().endswith("\\"):
        end += 1
    statement = "\n".join(lines[step:end + 1])
    assert "count_singular_domain.py" in statement
    for text in (_function_source(), statement):
        executed = _executed_lines(text)
        assert executed
        offenders = [l for l in executed if "apply.py" in l]
        assert not offenders, offenders


def test_g14_the_nonzero_hint_names_the_list_command_and_the_unchanged_count_sentence():
    r = _run_report("echo 7")
    assert "count_singular_domain.py --list" in r.stdout
    assert "unchanged after the repair" in r.stdout
    assert "left for you to judge" in r.stdout


def test_g14_the_count_script_only_reads():
    src = (_MIGRATIONS / "count_singular_domain.py").read_text()
    assert "readonly=True" in src
    assert not re.search(r"\b(UPDATE|INSERT|DELETE|ALTER|DROP)\b", src)


def test_g14_a_dry_run_prints_the_step_and_runs_nothing(tmp_path):
    sys.path.insert(0, str(Path(__file__).parent))
    import test_update_framework_no_domain_backfill as helper
    repo = helper._make_sandbox(tmp_path)
    r = helper._run(repo, "--dry-run", "--skip-backup")
    out = helper._strip_ansi(r.stdout)
    steps = helper._step_lines(r.stdout)
    assert any(label.startswith("count facts still carrying") for _, label in steps), steps
    assert "count_singular_domain.py" in out
    assert "facts still carrying a top-level domain key:" not in out
    assert "--force" not in out


# -- migration 044 text (its effect is proven on a real Postgres by the coordinator) --

def test_migration_044_is_one_do_block_on_technical_docs_only():
    sql = (_MIGRATIONS / "044_fact_domains_plural_key.sql").read_text()
    code = "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))
    assert not re.search(r"\b(BEGIN|COMMIT)\s*;", code)
    assert "community_summaries" not in code
    assert code.count("UPDATE technical_docs") == 2
    assert "RAISE NOTICE" in code


# -- the guard is one text, in every step and in the count script --

def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _guard_from_count_script() -> str:
    src = (_MIGRATIONS / "count_singular_domain.py").read_text()
    m = re.search(r'GUARD = """(.*?)"""', src, re.S)
    assert m
    return _squash(m.group(1).replace("\\\\", "\\"))


def test_the_migration_guard_is_the_count_scripts_guard_in_all_three_steps():
    sql = _squash((_MIGRATIONS / "044_fact_domains_plural_key.sql").read_text())
    guard = _guard_from_count_script()
    assert sql.count(guard) == 3


def test_the_guard_excludes_a_decision_blob_only_when_it_resolves_to_a_section():
    guard = _guard_from_count_script()
    assert "IS NOT NULL" in guard
    assert "?|" not in guard


def test_migration_044_step_one_treats_an_empty_plural_as_absent_and_step_two_rewrites_a_string():
    sql = _squash((_MIGRATIONS / "044_fact_domains_plural_key.sql").read_text())
    assert "jsonb_typeof(t.metadata->'domains') IN ('array', 'string') AND (SELECT jsonb_agg" in sql
    assert "WHEN jsonb_typeof(t.metadata->'domains') = 'string'" in sql


def test_the_count_script_lists_ids_read_only():
    src = (_MIGRATIONS / "count_singular_domain.py").read_text()
    assert '"--list"' in src and "SELECT t.id" in src and "readonly=True" in src
