"""Credential_Custody_Plan_2026-08-14, PR A3 — credential-use audit trail.

Coverage (coordinator.py side — see tests/test_llm_fault_origin.py for the
hive_mind_proxy.py proxy-path side):

  1. The credential-audit AsyncLineWriter: ON by default, disabled by an
     empty CREDENTIAL_AUDIT_LOG_PATH, relocatable.
  2. _classify_llm_fault — the 401/403/429(+insufficient_quota) vs
     transient rule, including the "unparseable 429 is transient" clause.
  3. _parse_upstream_error_type — best-effort OpenAI-shaped body parsing.
  4. record_llm_upstream_fault / record_llm_gateway_fault — counters +
     credentialed-only credential-audit lines, origin-ownership discipline.
  5. record_daemon_token_issued — counter + name-only log line.
  6. _llm_faults_snapshot / _credentials_snapshot — the /memory/telemetry
     render.
  7. token_verify_failed — counter, digest-prefix-not-raw-token property,
     claimed_agent always-None-today shape.

Governing property tested throughout: no secret/token VALUE ever reaches a
log line (SEC-08) — proven end to end via real file writes, not just by
reading call sites.
"""

import hashlib
import importlib.util
import json
import os
import sys

import pytest


def load_coordinator(agent_tokens: str = "", credential_audit_log_path=None):
    """Import a fresh coordinator.py with env pre-set. Mirrors tests/test_auth.py's
    loader — a fresh module per call so counters/writer state never leak
    between tests, and secure_env's process-lifetime secrets cache is cleared
    for AGENT_TOKENS so "unset" means what it says regardless of import order."""
    scripts_dir = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
    )
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import secure_env
    secure_env._secrets.pop("AGENT_TOKENS", None)

    if agent_tokens:
        os.environ["AGENT_TOKENS"] = agent_tokens
    else:
        os.environ.pop("AGENT_TOKENS", None)

    if credential_audit_log_path is not None:
        os.environ["CREDENTIAL_AUDIT_LOG_PATH"] = credential_audit_log_path
    else:
        os.environ.pop("CREDENTIAL_AUDIT_LOG_PATH", None)

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_credaudit_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── 1. The credential-audit writer: default-on, disable, relocate ───────────

def test_credential_audit_writer_on_by_default():
    """Unlike GATEWAY_AUDIT_LOG_PATH (opt-in), the credential log is ON with
    no env var set at all — a baseline control, not a diagnostic. Does not
    write anything (AsyncLineWriter.__init__ touches no filesystem), so this
    is safe to run against the real default path."""
    mod = load_coordinator()
    assert mod._credential_audit_writer is not None
    assert mod.CREDENTIAL_AUDIT_LOG_PATH.endswith("credential-audit.jsonl")


def test_credential_audit_writer_disabled_by_empty_env_var():
    mod = load_coordinator(credential_audit_log_path="")
    assert mod._credential_audit_writer is None


def test_credential_audit_writer_relocatable(tmp_path):
    target = tmp_path / "custom" / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    assert mod._credential_audit_writer is not None
    assert mod._credential_audit_writer.path == str(target)


def test_write_credential_audit_line_noop_when_writer_disabled(tmp_path):
    """A disabled writer must not raise and must not create the file."""
    mod = load_coordinator(credential_audit_log_path="")
    mod._write_credential_audit_line("token_verify_failed", origin="gateway")
    # nothing to assert on disk — the point is that this didn't raise


# ── 2. _classify_llm_fault ───────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403])
def test_classify_401_403_always_credential(status):
    mod = load_coordinator()
    assert mod._classify_llm_fault(status, None) == "credential"
    assert mod._classify_llm_fault(status, "some_other_code") == "credential"


def test_classify_429_insufficient_quota_is_credential():
    mod = load_coordinator()
    assert mod._classify_llm_fault(429, "insufficient_quota") == "credential"


def test_classify_429_without_insufficient_quota_is_transient():
    mod = load_coordinator()
    assert mod._classify_llm_fault(429, "rate_limit_exceeded") == "transient"


def test_classify_429_unparseable_body_is_transient():
    """MUTATION TARGET: the brief's explicit rule — 'an unparseable 429
    counts as transient (false quiet beats false alarm)'. error_type=None
    models a body that could not be parsed."""
    mod = load_coordinator()
    assert mod._classify_llm_fault(429, None) == "transient"


@pytest.mark.parametrize("status", [500, 502, 503, 529])
def test_classify_5xx_and_529_are_transient(status):
    mod = load_coordinator()
    assert mod._classify_llm_fault(status, None) == "transient"
    assert mod._classify_llm_fault(status, "insufficient_quota") == "transient", (
        "insufficient_quota only classifies 429 as credential, not 5xx — the "
        "rule is status-scoped, not body-scoped alone"
    )


# ── 3. _parse_upstream_error_type ────────────────────────────────────────────

def test_parse_upstream_error_type_extracts_code():
    mod = load_coordinator()
    body = json.dumps({"error": {"code": "insufficient_quota", "type": "x"}}).encode()
    assert mod._parse_upstream_error_type(body) == "insufficient_quota"


def test_parse_upstream_error_type_falls_back_to_type_when_no_code():
    mod = load_coordinator()
    body = json.dumps({"error": {"type": "invalid_request_error"}}).encode()
    assert mod._parse_upstream_error_type(body) == "invalid_request_error"


def test_parse_upstream_error_type_none_on_non_json():
    mod = load_coordinator()
    assert mod._parse_upstream_error_type(b"not json at all") is None


def test_parse_upstream_error_type_none_on_foreign_shape():
    mod = load_coordinator()
    assert mod._parse_upstream_error_type(b'{"message": "plain text error"}') is None


def test_parse_upstream_error_type_none_on_truncated_chunk():
    """Models a body split across chunks — the peek sees only the first
    (invalid, truncated) fragment. Must degrade to None, never raise."""
    mod = load_coordinator()
    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    assert mod._parse_upstream_error_type(body[:10]) is None


# ── 4. record_llm_upstream_fault / record_llm_gateway_fault ─────────────────

def test_record_llm_upstream_fault_bumps_counter_and_last():
    mod = load_coordinator()
    cls = mod.record_llm_upstream_fault("http://a:5000", 401, "invalid_api_key")
    assert cls == "credential"
    entry = mod._llm_fault_counters["http://a:5000"]["llm"]["credential"]
    assert entry["count"] == 1
    assert entry["last"]["status"] == 401
    assert entry["last"]["error_type"] == "invalid_api_key"


def test_record_llm_upstream_fault_transient_bumps_transient_bucket_only():
    mod = load_coordinator()
    mod.record_llm_upstream_fault("http://a:5000", 503, None)
    entry = mod._llm_fault_counters["http://a:5000"]["llm"]
    assert entry["transient"]["count"] == 1
    assert entry["credential"]["count"] == 0


def test_record_llm_upstream_fault_credential_class_writes_audit_line_when_credentialed(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_llm_upstream_fault("http://a:5000", 401, "invalid_api_key", credentialed=True)
    line = json.loads(target.read_text().strip())
    assert line["event"] == "upstream_credential_fault"
    assert line["origin"] == "llm"
    assert line["backend"] == "http://a:5000"
    assert line["status"] == 401


def test_record_llm_upstream_fault_credential_class_no_audit_line_when_not_credentialed(tmp_path):
    """MUTATION TARGET: an upstream 401 from a backend the gateway never
    attached a key to is not a credential-use event by this framework's own
    definition — must not write the high-signal log line."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_llm_upstream_fault("http://a:5000", 401, "invalid_api_key", credentialed=False)
    assert not target.exists()


def test_record_llm_upstream_fault_transient_class_never_writes_audit_line_even_when_credentialed(tmp_path):
    """High-signal only: transient faults (429 rate-limit, 5xx) are common
    and must not flood the credential-events log, even on a credentialed
    call."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_llm_upstream_fault("http://a:5000", 503, None, credentialed=True)
    assert not target.exists()


def test_record_llm_gateway_fault_bumps_counter_and_last():
    mod = load_coordinator()
    mod.record_llm_gateway_fault("http://a:5000", "ClientError")
    entry = mod._llm_fault_counters["http://a:5000"]["gateway"]
    assert entry["count"] == 1
    assert entry["last"]["class"] == "ClientError"


def test_record_llm_gateway_fault_writes_audit_line_when_credentialed(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_llm_gateway_fault("http://a:5000", "TimeoutError", credentialed=True,
                                  request_id="req123")
    line = json.loads(target.read_text().strip())
    assert line["event"] == "gateway_fault"
    assert line["origin"] == "gateway"
    assert line["error_class"] == "TimeoutError"
    assert line["request_id"] == "req123"


def test_record_llm_gateway_fault_no_audit_line_when_not_credentialed(tmp_path):
    """MUTATION TARGET: the design says 'the gateway's own failure on a
    credentialed call' — an uncredentialed local backend's connect hiccup is
    not a credential-use event."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_llm_gateway_fault("http://a:5000", "ClientError", credentialed=False)
    assert not target.exists()


def test_gateway_and_llm_fault_counts_are_independent_buckets():
    """Origin-ownership invariant: nothing is ever counted in both groups
    for the same cause."""
    mod = load_coordinator()
    mod.record_llm_gateway_fault("http://a:5000", "ClientError")
    mod.record_llm_upstream_fault("http://a:5000", 401, "x")
    entry = mod._llm_fault_counters["http://a:5000"]
    assert entry["gateway"]["count"] == 1
    assert entry["llm"]["credential"]["count"] == 1
    assert entry["llm"]["transient"]["count"] == 0


# ── 5. record_daemon_token_issued ────────────────────────────────────────────

def test_record_daemon_token_issued_bumps_counter():
    mod = load_coordinator()
    mod.record_daemon_token_issued("rem_daemon")
    assert mod._credential_counters["daemon_tokens_issued"] == 1


def test_record_daemon_token_issued_logs_name_only_never_token_material(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod.record_daemon_token_issued("consolidation")
    line = json.loads(target.read_text().strip())
    assert line["event"] == "daemon_token_issued"
    assert line["daemon"] == "consolidation"
    assert "token" not in line and "digest" not in line


# ── 6. telemetry snapshots ───────────────────────────────────────────────────

def test_llm_faults_snapshot_shape():
    mod = load_coordinator()
    mod.record_llm_gateway_fault("http://a:5000", "ClientError")
    mod.record_llm_upstream_fault("http://a:5000", 429, "insufficient_quota")
    snap = mod._llm_faults_snapshot()
    assert set(snap["http://a:5000"].keys()) == {"gateway", "llm"}
    assert set(snap["http://a:5000"]["llm"].keys()) == {"credential", "transient"}


def test_llm_faults_snapshot_empty_when_no_faults_recorded():
    """'Backends with zero faults may be omitted' — the chosen behaviour
    here is omission (never initialised until a fault occurs)."""
    mod = load_coordinator()
    assert mod._llm_faults_snapshot() == {}


def test_credentials_snapshot_includes_all_three_counters():
    mod = load_coordinator()
    mod._record_token_verify_failed("some_bad_token")
    mod.record_daemon_token_issued("rem_daemon")
    snap = mod._credentials_snapshot()
    assert snap["token_verify_failed"] == 1
    assert snap["daemon_tokens_issued"] == 1
    assert "audit_log_dropped" in snap


def test_credentials_snapshot_audit_log_dropped_reflects_writer_dropped_counter(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._credential_audit_writer.dropped = 3
    assert mod._credentials_snapshot()["audit_log_dropped"] == 3


def test_credentials_snapshot_zero_when_writer_disabled():
    mod = load_coordinator(credential_audit_log_path="")
    assert mod._credentials_snapshot()["audit_log_dropped"] == 0


# ── 7. token_verify_failed — counters, SEC-08 property, claimed_agent shape ─

def test_token_verify_failed_bumps_counter():
    mod = load_coordinator()
    mod._record_token_verify_failed("tok_bad")
    assert mod._credential_counters["token_verify_failed"] == 1


def test_token_verify_failed_digest_prefix_is_8_hex_chars_never_the_raw_token(tmp_path):
    """SEC-08 property, proven end to end through a real file write — the
    presented secret value must NEVER appear in any log line."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed("tok_super_secret_value")
    content = target.read_text()
    assert "tok_super_secret_value" not in content
    line = json.loads(content.strip())
    assert line["digest_prefix"] == _digest("tok_super_secret_value")[:8]
    assert len(line["digest_prefix"]) == 8


def test_token_verify_failed_digest_prefix_none_when_no_token_presented(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(None)
    line = json.loads(target.read_text().strip())
    assert line["digest_prefix"] is None


def test_token_verify_failed_claimed_agent_is_always_none_today(tmp_path):
    """Documents the current shape: bearer tokens carry no separate name
    claim, so claimed_agent is always None (the field exists for the planned
    PoP resolver)."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed("tok_x")
    line = json.loads(target.read_text().strip())
    assert line["claimed_agent"] is None
