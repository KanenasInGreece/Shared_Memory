"""Fix round batch G: OVERFLOW_TOKEN_SLACK is gone (decision:3258) and migrate_env no
longer reports a credential it cannot see as the gateway's own failure."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "shared-memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check_config  # noqa: E402
import encoder_window  # noqa: E402
import framework_defaults  # noqa: E402
import migrate_env as m  # noqa: E402


def _load_coordinator():
    spec = importlib.util.spec_from_file_location("coordinator", SCRIPTS / "coordinator.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


def _body(advertised, requested):
    return (f"This model's maximum context length is {advertised} tokens. "
            f"However, you requested {requested} tokens in the messages.")


# ── 1. classify_overflow has no slack band ──────────────────────────────────

@pytest.mark.parametrize("requested", [8193, 8200, 8300, 20000])
def test_any_overshoot_of_a_sufficient_window_is_an_overrun(requested):
    res = encoder_window.classify_overflow(400, _body(8192, requested), 8192)
    assert (res.kind, res.advertised, res.requested) == ("overrun", 8192, requested)


def test_short_window_is_still_a_mismatch():
    assert encoder_window.classify_overflow(400, _body(4096, 5000), 8192).kind == "mismatch"


def test_not_an_overshoot_or_unparseable_is_other():
    assert encoder_window.classify_overflow(400, _body(8192, 8000), 8192).kind == "other"
    assert encoder_window.classify_overflow(400, "Bad request: invalid JSON", 8192).kind == "other"


def test_the_slack_constant_is_gone():
    assert not hasattr(encoder_window, "OVERFLOW_TOKEN_SLACK")


# ── 2. coordinator shrink is proportional ──────────────────────────────────

class _Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code = status
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, answers):
        self.answers = list(answers)
        self.posts = []

    async def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]


def _over(requested, advertised=8192):
    return _Resp(400, _body(advertised, requested))


def test_embed_shrinks_in_proportion_and_retries_once():
    coord = _load_coordinator()
    client = _Client([_over(9000), _Resp(200, payload={"data": [{"embedding": [1.0]}]})])
    text = "x" * 20000
    assert asyncio.run(coord.MemoryCoordinator._embed(None, text, client)) == [1.0]
    reserved = int((coord.EMBED_MAX_CONTEXT_TOKENS - coord.EMBED_SPECIAL_TOKEN_RESERVE)
                   * coord.EMBED_CHARS_PER_TOKEN)
    assert len(client.posts) == 2
    assert len(client.posts[1]["input"]) == min(reserved, 20000 * 8192 // 9000)


def test_embed_all_overshoot_raises_the_overrun_error_with_nonempty_inputs():
    coord = _load_coordinator()
    client = _Client([_over(9000)])
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(coord.MemoryCoordinator._embed(None, "x" * 20000, client))
    msg = str(exc.value)
    assert f"Embedding context overrun after {coord.EMBED_RETRIES} attempts" in msg
    assert "exceeds slack" not in msg and "EMBED_CHARS_PER_TOKEN" in msg
    assert len(client.posts) == coord.EMBED_RETRIES
    assert all(len(p["input"]) >= 1 for p in client.posts)


def test_embed_zero_proportional_length_raises_without_a_zero_length_post():
    coord = _load_coordinator()
    client = _Client([_over(8192 * 1000)])
    with pytest.raises(RuntimeError, match="Embedding context overrun after 1 attempts"):
        asyncio.run(coord.MemoryCoordinator._embed(None, "hello", client))
    assert len(client.posts) == 1


def test_embed_many_shrinks_the_longest_in_proportion_and_keeps_siblings():
    coord = _load_coordinator()
    ok = _Resp(200, payload={"data": [{"embedding": [1], "index": 0},
                                      {"embedding": [2], "index": 1}]})
    client = _Client([_over(9000), ok])
    asyncio.run(coord.MemoryCoordinator._embed_many(None, ["short", "x" * 20000], client))
    reserved = int((coord.EMBED_MAX_CONTEXT_TOKENS - coord.EMBED_SPECIAL_TOKEN_RESERVE)
                   * coord.EMBED_CHARS_PER_TOKEN)
    retry = client.posts[1]["input"]
    assert retry[0] == "short"
    assert len(retry[1]) == min(reserved, 20000 * 8192 // 9000)


def test_embed_many_all_overshoot_and_zero_length_raise_the_batch_error():
    coord = _load_coordinator()
    client = _Client([_over(9000)])
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(coord.MemoryCoordinator._embed_many(None, ["short", "x" * 20000], client))
    msg = str(exc.value)
    assert f"Batch embedding context overrun after {coord.EMBED_RETRIES} attempts" in msg
    assert "exceeds slack" not in msg and "EMBED_CHARS_PER_TOKEN" in msg
    assert all(len(t) >= 1 for p in client.posts for t in p["input"])
    zero = _Client([_over(8192 * 1000)])
    with pytest.raises(RuntimeError, match="Batch embedding context overrun after 1 attempts"):
        asyncio.run(coord.MemoryCoordinator._embed_many(None, ["hello"], zero))
    assert len(zero.posts) == 1


def test_embed_many_selected_item_with_zero_proportion_raises_not_length_one(monkeypatch):
    coord = _load_coordinator()
    # reserved clamp = 10 chars; longest (100) keeps a positive proportion, the
    # mid item (50, at/above the clamp) has proportion 0 and must not be posted at length 1.
    monkeypatch.setattr(coord, "EMBED_CHARS_PER_TOKEN", 1)
    monkeypatch.setattr(coord, "EMBED_MAX_CONTEXT_TOKENS", coord.EMBED_SPECIAL_TOKEN_RESERVE + 10)
    monkeypatch.setattr(coord, "EMBED_MAX_CHARS", 1000)
    client = _Client([_over(8192 * 75)])
    with pytest.raises(RuntimeError, match="Batch embedding context overrun after 1 attempts"):
        asyncio.run(coord.MemoryCoordinator._embed_many(None, ["short", "y" * 50, "x" * 100], client))
    assert len(client.posts) == 1


# ── 3. the knob is gone everywhere ─────────────────────────────────────────

def test_overflow_token_slack_appears_nowhere():
    assert "OVERFLOW_TOKEN_SLACK" not in framework_defaults.FRAMEWORK_DEFAULTS
    assert "OVERFLOW_TOKEN_SLACK" not in check_config.ENV_ROW_ORDER
    for rel in ("shared-memory/.env.example", "shared-memory/scripts/encoder_window.py",
                "shared-memory/scripts/coordinator.py", "shared-memory/scripts/check_config.py",
                "shared-memory/scripts/framework_defaults.py"):
        assert "OVERFLOW_TOKEN_SLACK" not in (REPO / rel).read_text(), rel
    assert "slack-bounded" not in (SCRIPTS / "telemetry_contract.py").read_text()
    assert "slack-bounded" not in (SCRIPTS / "encoder_window.py").read_text()


# ── 4. migrate_env ──────────────────────────────────────────────────────────

_JSON = json.dumps([{"url": "http://a:5000", "token_env": "BATCH_G_ABSENT_TOKEN"}])


def test_loader_stderr_drops_only_the_no_usable_backend_error(tmp_path):
    snippet = m._LOADER_SNIPPET.format(here=str(SCRIPTS), loader_semantics=m.LOADER_SEMANTICS)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LLM_BACKENDS_JSON": _JSON,
           "SECURE_ENV_FILE": ""}
    proc = subprocess.run([sys.executable, "-c", snippet], env=env, capture_output=True,
                          text=True, timeout=60)
    image = json.loads(proc.stdout.strip().splitlines()[-1])
    assert image["fallback_reason"]  # the scenario really produced the fallback
    assert "produced no usable backend" not in proc.stderr
    assert "BATCH_G_ABSENT_TOKEN" in proc.stderr  # the token_env warning is kept


def _unit(monkeypatch, **kw):
    monkeypatch.setattr(m, "query_gateway_unit", lambda unit: m.UnitQuery("ok", **kw))


_REASON = "LLM_BACKENDS_JSON produced no usable backend (every entry excluded) — SENTINEL"


def _run_report(tmp_path, monkeypatch, capsys):
    env_path = tmp_path / ".env"
    env_path.write_text(f"LLM_BACKENDS_JSON={_JSON}\n")
    monkeypatch.setenv("SECURE_ENV_FILE", str(env_path))
    pre = tmp_path / "pre.json"
    pre.write_text(json.dumps({"capture_schema": m.CAPTURE_SCHEMA,
                               "image": {"urls": [], "fallback_reason": _REASON,
                                         "config_empty": False}}))
    rc = m.do_apply_or_dryrun(str(pre), False, "unit.service")
    assert rc == m.EXIT_OK
    return capsys.readouterr().out


def test_hidden_load_credential_prints_one_plain_sentence(tmp_path, monkeypatch, capsys):
    _unit(monkeypatch, load_credentials=True)
    out = _run_report(tmp_path, monkeypatch, capsys)
    assert m.HIDDEN_CREDENTIAL_NOTE in out
    assert "SENTINEL" not in out and "produced no usable backend" not in out
    assert "deepseek" not in out.lower() and "/run/credentials" not in out


def test_withheld_secret_environment_key_prints_the_sentence_without_the_value(
        tmp_path, monkeypatch, capsys):
    _unit(monkeypatch, environment={"DEEPSEEK_API_KEY": "sk-VALUE-NOT-PRINTED"})
    out = _run_report(tmp_path, monkeypatch, capsys)
    assert m.HIDDEN_CREDENTIAL_NOTE in out
    assert "sk-VALUE-NOT-PRINTED" not in out and "SENTINEL" not in out


def test_empty_load_credential_keeps_the_old_report(tmp_path, monkeypatch, capsys):
    _unit(monkeypatch, load_credentials=False)
    out = _run_report(tmp_path, monkeypatch, capsys)
    assert m.HIDDEN_CREDENTIAL_NOTE not in out
    assert "is present but unusable" in out and "SENTINEL" in out


def test_load_credential_property_only_counts_when_non_empty():
    assert not m._unit_declares_load_credential("LoadCredential=\nLoadCredentialEncrypted=\n")
    assert m._unit_declares_load_credential("LoadCredential=deepseek_api_key:/x/y\n")
    assert m._unit_declares_load_credential("LoadCredentialEncrypted=k:/x\n")


def test_query_asks_systemctl_for_the_credential_properties(monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0
        stdout = "Environment=\nEnvironmentFiles=\nLoadCredential=k:/p\n"
        stderr = ""

    def _run(argv, **kw):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(m.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(m.subprocess, "run", _run)
    q = m.query_gateway_unit("u.service")
    assert q.load_credentials is True
    assert "LoadCredential" in seen["argv"] and "LoadCredentialEncrypted" in seen["argv"]


def test_capture_output_carries_the_sentence(tmp_path, monkeypatch, capsys):
    _unit(monkeypatch, load_credentials=True)
    monkeypatch.setattr(m, "run_loader", lambda env: {"urls": [], "fallback_reason": _REASON,
                                                      "config_empty": False})
    assert m.do_capture(str(tmp_path / "pre.json"), "unit.service") == m.EXIT_OK
    out = capsys.readouterr().out
    assert m.HIDDEN_CREDENTIAL_NOTE in out and "SENTINEL" not in out
