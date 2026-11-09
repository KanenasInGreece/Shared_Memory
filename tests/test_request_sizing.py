"""Tests for request_sizing.py (batch H).

Covers:
1. Default value pins with all sizing environment variables unset.
2. Environment overrides and startup validation.
3. Single source verification: AST scan proving moved constants and env reads
   exist only in request_sizing.py, and NREM_MAX_TOKENS_SUMMARY is gone.
4. Wiring verification: ensuring rem_loop, consolidation_loop, and
   hive_mind_proxy call request_sizing getters.
5. Standard library only import check.
"""
import ast
import asyncio
import glob
import importlib
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
pytestmark = pytest.mark.usefixtures("chat_caller_is_consolidation")
from yarl import URL

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import request_sizing

# Names this module still reads. The removed bounds must not be read at all.
LIVE_SIZING_VARS = [
    "NREM_INSIGHT_SLOT_INPUT_CHARS",
    "LLM_CHARS_PER_TOKEN_RATIO",
    "FIT_MARGIN",
]
REMOVED_SIZING_VARS = [
    "REM_MAX_TOKENS_SOLO",
    "REM_MAX_TOKENS_PER_FACT",
    "REM_MAX_TOKENS_PER_SUMMARY",
    "REM_TRUNCATION_RETRY_FACTOR",
    "NREM_MAX_TOKENS_INSIGHT",
    "NREM_TRUNCATION_RETRY_FACTOR",
    "FIT_DEFAULT_OUTPUT_TOKENS",
]
SIZING_VARS = LIVE_SIZING_VARS + REMOVED_SIZING_VARS


@pytest.fixture(autouse=True)
def clean_sizing_env(monkeypatch):
    """Ensure tests start with all sizing environment variables unset by default."""
    for var in SIZING_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("NREM_MAX_TOKENS_SUMMARY", raising=False)


# ── Test 1: Value pins with every sizing variable unset ────────────────────────

def test_1_value_pins_and_edges(monkeypatch):
    for v in SIZING_VARS:
        monkeypatch.delenv(v, raising=False)

    # Defaults. fact:3394: the ratio is 2.7, the lowest real record.
    assert request_sizing.nrem_insight_item_chars() == 2000
    assert request_sizing.estimate_prompt_tokens(270) == 100.0
    assert request_sizing.context_room(10000, 1000) == 8000.0
    assert request_sizing.qualifies(8000.0, 1000) is True
    assert request_sizing.qualifies(8000.0, 8001) is False
    assert request_sizing.context_room(None, 1) is None
    assert request_sizing.context_room(0, 1) is None
    assert request_sizing.qualifies(None, 0) is False
    # Spend cap is min(max_output, context room). It does not decide qualifies.
    assert request_sizing.reply_room(10000, 500, 1000) == 500.0
    assert request_sizing.reply_room(10000, 9000, 1000) == 8000.0
    assert request_sizing.reply_room(1000, 500, 0.0) == 500.0
    assert request_sizing.reply_room(None, 500, 1) is None
    assert request_sizing.reply_room(1000, 0, 1) is None

    # A ratio that is not greater than 0 gives 0.0.
    monkeypatch.setenv("LLM_CHARS_PER_TOKEN_RATIO", "0")
    assert request_sizing.estimate_prompt_tokens(1200) == 0.0
    monkeypatch.setenv("LLM_CHARS_PER_TOKEN_RATIO", "-1")
    assert request_sizing.estimate_prompt_tokens(1200) == 0.0
    monkeypatch.delenv("LLM_CHARS_PER_TOKEN_RATIO", raising=False)

    # A bad margin stops a real context. A missing context does not read it into a qualify.
    monkeypatch.setenv("FIT_MARGIN", "x")
    assert request_sizing.context_room(None, 1) is None
    with pytest.raises(ValueError):
        request_sizing.context_room(1, 0.0)
    monkeypatch.delenv("FIT_MARGIN", raising=False)


# ── Test 2: Env override still works & validation ──────────────────────────────

def test_2_env_overrides_and_validation(monkeypatch):
    monkeypatch.setenv("NREM_INSIGHT_SLOT_INPUT_CHARS", "1500")
    assert request_sizing.nrem_insight_item_chars() == 1500

    monkeypatch.setenv("LLM_CHARS_PER_TOKEN_RATIO", "2.0")
    assert request_sizing.estimate_prompt_tokens(1000) == 500.0

    monkeypatch.setenv("FIT_MARGIN", "0.20")
    # n_ctx 1000 holds back 0.20, so room is 800. Qualifies when room >= estimate.
    assert request_sizing.qualifies(request_sizing.context_room(1000, 400), 400) is True
    assert request_sizing.qualifies(request_sizing.context_room(1000, 401), 401) is False

    monkeypatch.setenv("NREM_INSIGHT_SLOT_INPUT_CHARS", "x")
    with pytest.raises(ValueError):
        request_sizing.validate_nrem()
    monkeypatch.delenv("NREM_INSIGHT_SLOT_INPUT_CHARS", raising=False)
    monkeypatch.setenv("NREM_MAX_TOKENS_INSIGHT", "x")
    request_sizing.validate_nrem()
    monkeypatch.delenv("NREM_MAX_TOKENS_INSIGHT", raising=False)

    for var in ("LLM_CHARS_PER_TOKEN_RATIO", "FIT_MARGIN"):
        monkeypatch.setenv(var, "x")
        with pytest.raises(ValueError):
            request_sizing.validate_fit()
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("FIT_DEFAULT_OUTPUT_TOKENS", "2048.5")
    request_sizing.validate_fit()
    monkeypatch.delenv("FIT_DEFAULT_OUTPUT_TOKENS", raising=False)

    monkeypatch.setenv("REM_MAX_TOKENS_SOLO", "x")
    monkeypatch.setenv("NREM_MAX_TOKENS_INSIGHT", "x")
    request_sizing.validate_fit()
    monkeypatch.delenv("REM_MAX_TOKENS_SOLO", raising=False)
    monkeypatch.delenv("NREM_MAX_TOKENS_INSIGHT", raising=False)


def test_2b_gateway_import_refuses_a_malformed_sizing_value(monkeypatch):
    """Importing the gateway or daemons with a malformed sizing variable refuses at startup."""
    monkeypatch.setenv("SECURE_ENV_FILE", "")

    # 1. Gateway (hive_mind_proxy) refuses at import with malformed FIT_MARGIN
    monkeypatch.setenv("FIT_MARGIN", "x")
    try:
        with pytest.raises(ValueError):
            if "hive_mind_proxy" in sys.modules:
                importlib.reload(sys.modules["hive_mind_proxy"])
            else:
                importlib.import_module("hive_mind_proxy")
    finally:
        monkeypatch.delenv("FIT_MARGIN", raising=False)
        if "hive_mind_proxy" in sys.modules:
            importlib.reload(sys.modules["hive_mind_proxy"])

    # 2. REM ignores a leftover token bound. The daemon still imports.
    monkeypatch.setenv("REM_MAX_TOKENS_SOLO", "x")
    try:
        if "rem_loop" in sys.modules:
            importlib.reload(sys.modules["rem_loop"])
        else:
            importlib.import_module("rem_loop")
    finally:
        monkeypatch.delenv("REM_MAX_TOKENS_SOLO", raising=False)
        if "rem_loop" in sys.modules:
            importlib.reload(sys.modules["rem_loop"])

    # 3. NREM ignores a leftover output bound. A bad body cap still refuses.
    monkeypatch.setenv("NREM_MAX_TOKENS_INSIGHT", "x")
    try:
        if "consolidation_loop" in sys.modules:
            importlib.reload(sys.modules["consolidation_loop"])
        else:
            importlib.import_module("consolidation_loop")
    finally:
        monkeypatch.delenv("NREM_MAX_TOKENS_INSIGHT", raising=False)
    monkeypatch.setenv("NREM_INSIGHT_SLOT_INPUT_CHARS", "x")
    try:
        with pytest.raises(ValueError):
            importlib.reload(sys.modules["consolidation_loop"])
    finally:
        monkeypatch.delenv("NREM_INSIGHT_SLOT_INPUT_CHARS", raising=False)
        if "consolidation_loop" in sys.modules:
            importlib.reload(sys.modules["consolidation_loop"])

    # 4. Gateway (hive_mind_proxy) reload with REM_MAX_TOKENS_SOLO=x must SUCCEED (then restore)
    monkeypatch.setenv("REM_MAX_TOKENS_SOLO", "x")
    try:
        if "hive_mind_proxy" in sys.modules:
            importlib.reload(sys.modules["hive_mind_proxy"])
        else:
            importlib.import_module("hive_mind_proxy")
    finally:
        monkeypatch.delenv("REM_MAX_TOKENS_SOLO", raising=False)
        if "hive_mind_proxy" in sys.modules:
            importlib.reload(sys.modules["hive_mind_proxy"])


# ── Test 3: Single source (AST scan) ──────────────────────────────────────────

def test_3_single_source_ast_scan():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    scripts_dir = os.path.join(repo_root, "shared-memory", "scripts")

    targets = ["rem_loop.py", "consolidation_loop.py", "hive_mind_proxy.py"]
    moved_names = set(SIZING_VARS)

    for fname in targets:
        fpath = os.path.join(scripts_dir, fname)
        with open(fpath, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=fpath)

        # 1. No direct read of moved names via os.environ.get, os.getenv, or os.environ[...]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # os.environ.get(...) or os.getenv(...)
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault"):
                    if node.args and isinstance(node.args[0], ast.Constant):
                        assert node.args[0].value not in moved_names, (
                            f"{fname} reads moved env var {node.args[0].value!r} via {func.attr}"
                        )
                elif isinstance(func, ast.Attribute) and func.attr == "getenv":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        assert node.args[0].value not in moved_names, (
                            f"{fname} reads moved env var {node.args[0].value!r} via getenv"
                        )
                elif isinstance(func, ast.Name) and func.id == "getenv":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        assert node.args[0].value not in moved_names, (
                            f"{fname} reads moved env var {node.args[0].value!r} via getenv"
                        )
            elif isinstance(node, ast.Subscript):
                # os.environ[...]
                if isinstance(node.slice, ast.Constant):
                    assert node.slice.value not in moved_names, (
                        f"{fname} reads moved env var {node.slice.value!r} via subscript"
                    )

        # 2. Assert none of the moved module constants is still defined (A10: ast.Assign / ast.AnnAssign)
        for node in tree.body:
            assigned_names = []
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        assigned_names.append(target.id)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    assigned_names.append(node.target.id)
            for name in assigned_names:
                assert name not in moved_names, (
                    f"{fname} still defines module constant {name}"
                )

        # 3. Top-level request_sizing.validate_*() must be called after load_split_env() (V4)
        load_env_lineno = None
        validate_lineno = None
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if (isinstance(call.func, ast.Name) and call.func.id == "load_split_env") or \
                   (isinstance(call.func, ast.Attribute) and call.func.attr == "load_split_env"):
                    load_env_lineno = node.lineno
                elif (isinstance(call.func, ast.Attribute)
                      and getattr(call.func.value, "id", None) == "request_sizing"
                      and call.func.attr.startswith("validate")):
                    validate_lineno = node.lineno
        assert load_env_lineno is not None, f"{fname} must call load_split_env at top-level"
        # REM no longer sizes its own reply. The gateway validates the fit settings.
        if fname == "rem_loop.py":
            assert validate_lineno is None
            continue
        assert validate_lineno is not None, f"{fname} must call request_sizing.validate_* at top-level"
        assert validate_lineno > load_env_lineno, (
            f"{fname}: request_sizing.validate_* (line {validate_lineno}) must appear "
            f"after load_split_env (line {load_env_lineno})"
        )

    # 4. request_sizing.py reads each live name once. A removed bound is not read.
    rs_path = os.path.join(scripts_dir, "request_sizing.py")
    with open(rs_path, encoding="utf-8") as f:
        rs_tree = ast.parse(f.read(), filename=rs_path)

    counts = {v: 0 for v in SIZING_VARS}
    for node in ast.walk(rs_tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if val in counts:
                    counts[val] += 1

    for v in LIVE_SIZING_VARS:
        assert counts[v] == 1, f"request_sizing.py must read {v} exactly once; found {counts[v]}"
    for v in REMOVED_SIZING_VARS:
        assert counts[v] == 0, f"request_sizing.py still reads removed {v}"

    # 5. NREM_MAX_TOKENS_SUMMARY appears nowhere in shared-memory/scripts/ or .env.example
    scripts_glob = glob.glob(os.path.join(scripts_dir, "*.py"))
    env_example = os.path.join(repo_root, "shared-memory", ".env.example")
    for path in scripts_glob + [env_example]:
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        assert "NREM_MAX_TOKENS_SUMMARY" not in content, (
            f"NREM_MAX_TOKENS_SUMMARY still present in {os.path.relpath(path, repo_root)}"
        )


# ── Test 4: Wiring verification ───────────────────────────────────────────────

class _MockResp:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(json_data)

    def json(self):
        return self._json


class _AsyncIter:
    """Minimal async iterable yielding zero rows — empty Neo4j result."""
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _async_ctx:
    """Minimal async context manager wrapping a fixed value."""
    def __init__(self, val):
        self._val = val

    async def __aenter__(self):
        return self._val

    async def __aexit__(self, *_):
        pass


def _make_rem_daemon():
    """Return a REMDaemon with driver mocked out."""
    import rem_loop
    d = rem_loop.REMDaemon.__new__(rem_loop.REMDaemon)
    d.is_running = True
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=_AsyncIter())
    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=_async_ctx(mock_session))
    d.driver = mock_driver
    return d, mock_session


@pytest.mark.asyncio
async def test_4_wiring_rem_sends_no_max_tokens_and_does_not_retry(monkeypatch):
    """The daemon sends one call and no max_tokens. A cut-off is not parsed and not retried."""
    import rem_loop

    daemon, _ = _make_rem_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    calls = []

    async def _mock_post(self, url, **kwargs):
        body = kwargs.get("json", {})
        calls.append(body)
        if len(calls) == 1:
            return _MockResp({"choices": [{"finish_reason": "stop", "message": {"content": '{"summary":"s","relationships":[]}'}}]})
        return _MockResp({"choices": [{"finish_reason": "length", "message": {"content": '{"summary":"cut"'}}]})

    monkeypatch.setattr("httpx.AsyncClient.post", _mock_post)
    long_content = "x" * (2001)
    parsed, _model = await daemon._llm_process(long_content, rem_loop.KIND_FACT, pg_id=1)
    assert parsed["summary"] == "s"
    assert "max_tokens" not in calls[0]

    calls.clear()

    async def _mock_length(self, url, **kwargs):
        calls.append(kwargs.get("json", {}))
        return _MockResp({"choices": [{"finish_reason": "length", "message": {"content": '{"summary":"cut"'}}]})

    monkeypatch.setattr("httpx.AsyncClient.post", _mock_length)
    with patch.object(rem_loop, "_parse_llm_json") as parse:
        result, _model = await daemon._llm_process(long_content, rem_loop.KIND_FACT, pg_id=2)
    assert result is None
    assert len(calls) == 1
    assert "max_tokens" not in calls[0]
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_4_wiring_rem_batch_sends_no_max_tokens(monkeypatch):
    import rem_loop

    daemon, _ = _make_rem_daemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    captured = {}

    async def _mock_post(self, url, **kwargs):
        captured.update(kwargs.get("json", {}))
        return _MockResp({"choices": [{"finish_reason": "stop", "message": {"content": '{"idx":0,"summary":"s"}\n{"idx":1,"summary":"s2"}'}}]})

    monkeypatch.setattr("httpx.AsyncClient.post", _mock_post)
    long_content = "x" * (2001)
    items = [{"pg_id": 1, "content": long_content}, {"pg_id": 2, "content": long_content}]
    await daemon._llm_process_batch(items)
    assert "max_tokens" not in captured


@pytest.mark.asyncio
async def test_4_wiring_nrem_insight(monkeypatch):
    import consolidation_loop as cl

    daemon = cl.ConsolidationDaemon()
    monkeypatch.delenv("MOCK_LLM", raising=False)
    sent = []

    async def _fake_post_nrem(client, json_payload, ceiling_s=None, prompt_chars=0):
        sent.append(json_payload)
        return _MockResp({"choices": [{"finish_reason": "stop", "message": {"content": "SLOT 1: a.\nPRINCIPLE: p."}}]})

    monkeypatch.setattr(cl, "_post_nrem", _fake_post_nrem)
    items = [{"pg_id": 1, "type": "decision", "title": "T", "body": "B"}]
    res = await daemon._call_insight_llm("prompt text", "EntityA", units=1, items=items)
    assert res is not None
    assert len(sent) == 1
    assert "max_tokens" not in sent[0]

    monkeypatch.setattr(request_sizing, "nrem_insight_item_chars", lambda **kw: 5)
    captured_prompt = []
    captured_items = []

    async def _mock_call_insight(prompt, entity, units=1, items=None):
        captured_prompt.append(prompt)
        captured_items.append(items)
        return "SLOT 10: Mock distillate.\nPRINCIPLE: Mock principle."

    daemon._call_insight_llm = _mock_call_insight
    rows = [(10, "Title Line\nFull long body exceeding five characters", "proj", "decision", {})]
    res_slots = await daemon.generate_insight_slots("EntityA", rows)
    assert res_slots is not None
    assert captured_items[0][0]["body"] == "Full "
    assert "Body: Full " in captured_prompt[0]
    assert "Full long body" not in captured_prompt[0]


@pytest.mark.asyncio
async def test_4_wiring_gateway_fit_greek(monkeypatch):
    import secure_env
    monkeypatch.delenv("AGENT_TOKENS", raising=False)
    secure_env._secrets.pop("AGENT_TOKENS", None)

    monkeypatch.setenv("LLM_BACKENDS_JSON", json.dumps([
        {"url": "http://a:5000", "n_ctx": 4096, "private_ok": True, "max_output_tokens": 2048},
    ]))

    import coordinator
    importlib.reload(coordinator)
    import hive_mind_proxy as g
    importlib.reload(g)

    monkeypatch.setattr(request_sizing, "qualifies", lambda room, est: False)

    recorded_prompt_lens = []
    real_estimate = request_sizing.estimate_prompt_tokens

    def _mock_estimate_prompt_tokens(n_bytes, **kw):
        recorded_prompt_lens.append(n_bytes)
        return real_estimate(n_bytes, **kw)

    monkeypatch.setattr(request_sizing, "estimate_prompt_tokens", _mock_estimate_prompt_tokens)

    proxy = g.AsyncHiveMindProxy()

    class _RaisingSession:
        def request(self, *a, **kw):
            raise AssertionError("upstream must not be called")

    proxy.session = _RaisingSession()
    proxy.llm_session = proxy.session

    greek_text = "Καλημέρα κόσμε! Αυτό είναι ένα δοκιμαστικό σώμα στα ελληνικά."
    payload = {
        "messages": [{"role": "user", "content": greek_text}],
        "model": "local-model",
        "max_tokens": 128,
    }
    raw_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(raw_bytes) > len(raw_bytes.decode("utf-8"))

    class _MockReq(dict):
        method = "POST"
        path = "/v1/chat/completions"
        rel_url = URL("/v1/chat/completions", encoded=True)
        headers = {}
        can_read_body = True

        async def read(self):
            return raw_bytes

    resp = await proxy.handle_proxy(_MockReq())
    assert resp.status == 422
    body = json.loads(resp.body.decode())
    assert body["error"] == "backlog"
    assert body["reason"] == "no_context"

    assert len(recorded_prompt_lens) >= 1
    char_count = len(raw_bytes.decode("utf-8"))
    assert recorded_prompt_lens[0] == len(raw_bytes)
    assert recorded_prompt_lens[0] != char_count


# ── Test 5: Standard library only import check ─────────────────────────────────

def test_5_standard_library_only():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    rs_path = os.path.join(repo_root, "shared-memory", "scripts", "request_sizing.py")
    with open(rs_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=rs_path)

    imported_modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                imported_modules.append(name.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.append(node.module.split(".")[0])

    # sys.stdlib_module_names is available in Python 3.10+
    stdlib_names = getattr(sys, "stdlib_module_names", {"os", "sys", "json", "re", "math", "typing"})
    for mod in imported_modules:
        assert mod in stdlib_names, f"request_sizing.py imports non-stdlib module {mod!r}"
