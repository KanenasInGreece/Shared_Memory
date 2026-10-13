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


coordinator_mod = _load("coordinator_section_key", _SCRIPTS / "coordinator.py")
MemoryCoordinator = coordinator_mod.MemoryCoordinator


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
    c = MemoryCoordinator()
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
    # A `domains` key the caller sent stays as sent; one it did not send is never added.
    assert stored.get("domains", "absent") == sections.get("domains", "absent")


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
    assert json.loads(resp.text)["error"] == "domain_not_allowed_on_judgement"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["domain_of", "project_of", "supersede", "", "  ", "Domain_Of", " Supersede "])
async def test_g8_other_types_are_stored_as_sent(sent):
    resp, _, conn = await _run({"type": sent})
    if resp.status == 200:
        assert _stored(conn)["type"] == sent
    else:
        # a refused type is refused as sent, never rewritten into one that passes
        assert json.loads(resp.text).get("error") in ("unknown_type", None)


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
async def test_g9_the_shape_refusal_wins_over_the_judgement_refusal():
    resp, _, _ = await _run({"type": "retrospective", "domain": {"a": 1}})
    assert json.loads(resp.text)["error"] == "domain_shape_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["domain", "domains"])
@pytest.mark.parametrize("ok", [None, "", [], ["", " "], "assembly", ["assembly"]])
async def test_g10_empty_and_null_sections_are_not_refused(key, ok):
    resp, _, conn = await _run({key: ok})
    assert resp.status == 200, resp.text


@pytest.mark.asyncio
async def test_g11_a_shape_refusal_is_counted():
    c, conn = _coordinator()
    before = c._registry_refusals.snapshot()
    resp, _, _ = await _run({"domain": 5}, c, conn)
    assert resp.status == 400
    after = c._registry_refusals.snapshot()
    assert json.dumps(after) != json.dumps(before)
    assert c._REFUSAL_COUNTER["domain_shape_invalid"] == "new_domain_refused"


# ── G12: the stored side of the conflict check reads `domains` ────────────────

@pytest.mark.asyncio
async def test_g12_a_resave_naming_the_plural_is_not_a_conflict_beside_a_stale_singular():
    c, _ = _coordinator()
    stored = {"project": "widget_works", "domain": "A", "domains": ["B"]}
    assert await c._axis_conflict_error(stored, "widget_works", ["B"], [], False) is None


@pytest.mark.asyncio
async def test_g12_a_resave_naming_a_different_section_is_still_a_conflict():
    c, _ = _coordinator()
    stored = {"project": "widget_works", "domain": "A", "domains": ["B"]}
    err = await c._axis_conflict_error(stored, "widget_works", ["A"], [], False)
    assert err is not None and err["axis"] == "domains"


@pytest.mark.asyncio
async def test_g12_a_stored_singular_alone_still_counts():
    c, _ = _coordinator()
    stored = {"project": "widget_works", "domain": "A"}
    assert await c._axis_conflict_error(stored, "widget_works", ["A"], [], False) is None
    err = await c._axis_conflict_error(stored, "widget_works", ["B"], [], False)
    assert err is not None


@pytest.mark.asyncio
async def test_g12_a_decisions_blob_still_wins_over_the_plural():
    c, _ = _coordinator()
    stored = {"type": "decision", "project": "widget_works", "domains": ["B"],
              "decision": {"project": "widget_works", "domains": ["A"]}}
    assert await c._axis_conflict_error(
        stored, "widget_works", ["A"], [], True, incoming_type="decision",
        incoming_metadata={"type": "decision"}) is None


# ── outbox and graph payload unchanged ────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_outbox_payload_carries_the_same_sections():
    _, _, conn = await _run({"domain": "assembly"})
    params = _outbox_domains(conn)
    assert "assembly" in str(params)


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


def test_g14_the_step_runs_soft_after_the_restart_and_is_not_a_rewrite():
    lines = _SCRIPT.splitlines()
    restart = next(i for i, l in enumerate(lines) if l.startswith('run "restart the gateway"'))
    step = next(i for i, l in enumerate(lines) if l.startswith('run_soft "count facts still carrying'))
    assert step > restart
    block = "\n".join(lines[step:step + 3])
    assert "count_singular_domain.py" in block
    assert "apply.py" not in block
    assert not re.search(r"^\s*(run|run_soft)\b.*apply\.py.*--force", _SCRIPT, re.M)


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
