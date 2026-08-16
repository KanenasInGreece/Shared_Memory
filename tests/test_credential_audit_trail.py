"""Credential_Custody_Plan_2026-08-14, PR A3 — credential-use audit trail.

Coverage (coordinator.py side — see tests/test_llm_fault_origin.py for the
hive_mind_proxy.py proxy-path side):

  1. The credential-audit AsyncLineWriter: ON by default, disabled by an
     empty CREDENTIAL_AUDIT_LOG_PATH, relocatable.
  2. _classify_llm_fault — the 401/403/429(+insufficient_quota) vs
     transient rule, including the "unparseable 429 is transient" clause.
  3. _parse_upstream_error_type / _bounded_error_label — best-effort
     OpenAI-shaped body parsing, bounded and type-coerced (R-2/R-4).
  4. _decompress_prefix_for_parse — bounded gzip/deflate decompression so
     the 429/insufficient_quota rule survives a compressed body (R-3).
  5. record_llm_upstream_fault / record_llm_gateway_fault — counters +
     credentialed-only credential-audit lines, origin-ownership discipline.
  6. record_daemon_token_issued — counter + name-only log line.
  7. _llm_faults_snapshot (deep-copied, N-5) / _credentials_snapshot — the
     /memory/telemetry render.
  8. token_verify_failed — counter, no-line-for-no-token (C-1), rate limit +
     suppression summary (C-1), drop-newest queue policy (R-5), attribution
     (O-3), digest-prefix-not-raw-token property, claimed_agent
     always-None-today shape, reserved-key precedence (N-3).

Governing property tested throughout: no secret/token VALUE ever reaches a
log line (SEC-08) — proven end to end via real file writes, not just by
reading call sites.

SECURITY REVIEW R-1 (2026-08-15 fix round): this file's own load_coordinator
now defaults CREDENTIAL_AUDIT_LOG_PATH to "" (disabled) rather than popping
the variable — a test that wants the writer armed must say so explicitly.
Combined with tests/conftest.py's autouse fixture (a backstop for tests that
forget entirely), no test in this file can reach the real $HOME.
"""

import hashlib
import importlib.util
import json
import os
import sys

import pytest


# Sentinel distinct from "" (explicit disable): means "leave the env var
# genuinely absent so the module computes its OWN unset-default". Only used
# by the one test that needs to prove the on-by-default contract — always
# paired with redirecting HOME so even that "genuinely absent" case can never
# resolve under the real home directory.
_LEAVE_ENV_UNSET = object()


def load_coordinator(agent_tokens: str = "", credential_audit_log_path=""):
    """Import a fresh coordinator.py with env pre-set. Mirrors tests/test_auth.py's
    loader — a fresh module per call so counters/writer state never leak
    between tests, and secure_env's process-lifetime secrets cache is cleared
    for AGENT_TOKENS so "unset" means what it says regardless of import order.

    `credential_audit_log_path` defaults to `""` (writer DISABLED) — never
    "pop the variable" (security review R-1): a caller that wants the
    default real-home behaviour must pass `_LEAVE_ENV_UNSET` explicitly,
    and should also redirect $HOME (see
    test_credential_audit_log_path_default_on_and_expands_under_fake_home).
    """
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

    if credential_audit_log_path is _LEAVE_ENV_UNSET:
        os.environ.pop("CREDENTIAL_AUDIT_LOG_PATH", None)
    else:
        os.environ["CREDENTIAL_AUDIT_LOG_PATH"] = credential_audit_log_path

    path = os.path.join(scripts_dir, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator_credaudit_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _FakeRequest:
    """Minimal stand-in for aiohttp.web.Request, just enough for
    _record_token_verify_failed / _peer_identity / _transport_kind: no real
    transport (so both report TCP/no-principal, same as this repo's other
    request doubles), a settable `.path`."""
    def __init__(self, path: str = "/memory/save"):
        self.path = path
        self.transport = None


# ── 1. The credential-audit writer: default-on, disable, relocate ───────────

def test_credential_audit_log_path_default_on_and_expands_under_fake_home(monkeypatch, tmp_path):
    """Proves the ON-BY-DEFAULT contract without ever touching the real
    $HOME (security review R-1): redirect HOME to a scratch dir, leave
    CREDENTIAL_AUDIT_LOG_PATH genuinely unset, and confirm the writer is
    armed and its path expands under the FAKE home. Never calls .write(), so
    even the writer's presence carries no disk-write risk on its own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = load_coordinator(credential_audit_log_path=_LEAVE_ENV_UNSET)
    assert mod._credential_audit_writer is not None
    assert mod.CREDENTIAL_AUDIT_LOG_PATH == "~/.shared-memory/logs/credential-audit.jsonl"
    assert str(tmp_path) in mod._credential_audit_writer.path
    assert mod._credential_audit_writer.path.endswith("credential-audit.jsonl")


def test_credential_audit_writer_disabled_by_empty_env_var():
    mod = load_coordinator(credential_audit_log_path="")
    assert mod._credential_audit_writer is None


def test_credential_audit_writer_disabled_is_the_load_coordinator_default():
    """MUTATION TARGET (R-1): calling load_coordinator() with no argument at
    all must be exactly as safe as passing "" explicitly."""
    mod = load_coordinator()
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


# ── 3. _parse_upstream_error_type / _bounded_error_label (R-2/R-4) ──────────

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


def test_parse_upstream_error_type_5mb_code_yields_bounded_label():
    """⚑ Security review R-2, empirically confirmed by the reviewer's own
    probe (a 5MB error.code reached telemetry and the log verbatim). MUTATION
    TARGET: remove _ERROR_BODY_PARSE_CAP or _ERROR_TYPE_LABEL_CAP and this
    must fail."""
    mod = load_coordinator()
    huge = "A" * 5_000_000
    body = json.dumps({"error": {"code": huge}}).encode()
    result = mod._parse_upstream_error_type(body)
    # The chunk itself exceeds _ERROR_BODY_PARSE_CAP (64 KiB), so the parse
    # refuses outright — bounded either way, but confirm the SPECIFIC gate:
    assert result is None
    assert len(body) > mod._ERROR_BODY_PARSE_CAP


def test_bounded_error_label_truncates_a_long_but_under_cap_string():
    """Exercises the LABEL cap directly (as opposed to the body-size cap
    above): a string short enough to parse but long enough to need
    truncation."""
    mod = load_coordinator()
    value = "x" * 500
    result = mod._bounded_error_label(value)
    assert result is not None
    assert len(result) <= mod._ERROR_TYPE_LABEL_CAP + len("…[truncated]")
    assert result.startswith("x" * 50)


def test_bounded_error_label_does_not_repr_wrap_a_short_string():
    """MUTATION TARGET: literally reusing `_short()` (repr()-based) would
    wrap the value in quotes and silently break the exact
    `error_type == "insufficient_quota"` match _classify_llm_fault depends
    on — this proves the returned string is the RAW value, not its repr."""
    mod = load_coordinator()
    assert mod._bounded_error_label("insufficient_quota") == "insufficient_quota"


def test_parse_upstream_error_type_dict_valued_code_yields_none():
    """⚑ Security review R-4, empirically confirmed by the reviewer's own
    probe. MUTATION TARGET: remove the isinstance guard in
    _bounded_error_label and this must fail."""
    mod = load_coordinator()
    body = json.dumps({"error": {"code": {"nested": [1, 2, 3]}}}).encode()
    assert mod._parse_upstream_error_type(body) is None


def test_parse_upstream_error_type_list_valued_code_yields_none():
    mod = load_coordinator()
    body = json.dumps({"error": {"code": [1, 2, 3]}}).encode()
    assert mod._parse_upstream_error_type(body) is None


def test_parse_upstream_error_type_int_valued_code_is_accepted():
    """int/float ARE accepted (R-4's fix is "reject non-str/int/float", not
    "reject non-str") — a numeric error code is a real shape some APIs use."""
    mod = load_coordinator()
    body = json.dumps({"error": {"code": 42}}).encode()
    assert mod._parse_upstream_error_type(body) == "42"


# ── 4. _decompress_prefix_for_parse (R-3) ────────────────────────────────────

def test_decompress_prefix_for_parse_gzip_recovers_json():
    import gzip
    mod = load_coordinator()
    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    compressed = gzip.compress(body)
    recovered = mod._decompress_prefix_for_parse(compressed, "gzip")
    assert json.loads(recovered)["error"]["code"] == "insufficient_quota"


def test_decompress_prefix_for_parse_deflate_recovers_json():
    import zlib
    mod = load_coordinator()
    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    compressed = zlib.compress(body)
    recovered = mod._decompress_prefix_for_parse(compressed, "deflate")
    assert json.loads(recovered)["error"]["code"] == "insufficient_quota"


def test_decompress_prefix_for_parse_no_encoding_returns_body_unchanged():
    mod = load_coordinator()
    body = b'{"error":{"code":"x"}}'
    assert mod._decompress_prefix_for_parse(body, None) == body


def test_decompress_prefix_for_parse_unknown_encoding_returns_body_unchanged():
    mod = load_coordinator()
    body = b'{"error":{"code":"x"}}'
    assert mod._decompress_prefix_for_parse(body, "identity") == body


def test_decompress_prefix_for_parse_garbage_gzip_never_raises():
    mod = load_coordinator()
    garbage = b"not actually gzip data at all"
    result = mod._decompress_prefix_for_parse(garbage, "gzip")
    assert result == garbage  # falls back to the original bytes, unparseable downstream


def test_gzip_body_end_to_end_classifies_as_credential():
    """⚑ Security review R-3, the end-to-end proof: a gzip-compressed
    insufficient_quota body, decompressed then parsed then classified,
    yields "credential" on a 429 — the exact case the reviewer's probe
    showed silently degrading to "transient" before this fix."""
    import gzip
    mod = load_coordinator()
    body = json.dumps({"error": {"code": "insufficient_quota"}}).encode()
    compressed = gzip.compress(body)
    recovered = mod._decompress_prefix_for_parse(compressed, "gzip")
    error_type = mod._parse_upstream_error_type(recovered)
    assert mod._classify_llm_fault(429, error_type) == "credential"


# ── 5. record_llm_upstream_fault / record_llm_gateway_fault ─────────────────

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


# ── 6. record_daemon_token_issued ────────────────────────────────────────────

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


# ── 7. telemetry snapshots (N-5) ──────────────────────────────────────────────

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


def test_llm_faults_snapshot_is_truly_read_only():
    """MUTATION TARGET (N-5): mutating the returned snapshot's nested `last`
    dict must NOT corrupt the live counters — proves the copy is deep, not
    shallow (dict(entry["gateway"]) shares the nested `last` dict by
    reference)."""
    mod = load_coordinator()
    mod.record_llm_gateway_fault("http://a:5000", "ClientError")
    snap = mod._llm_faults_snapshot()
    snap["http://a:5000"]["gateway"]["last"]["class"] = "TAMPERED"
    snap["http://a:5000"]["gateway"]["count"] = 999
    live = mod._llm_faults_snapshot()
    assert live["http://a:5000"]["gateway"]["last"]["class"] == "ClientError"
    assert live["http://a:5000"]["gateway"]["count"] == 1


def test_credentials_snapshot_includes_all_three_counters():
    mod = load_coordinator()
    mod._record_token_verify_failed(_FakeRequest(), "some_bad_token")
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


# ── 7b. counter ↔ last_ts pairing ────────────────────────────────────────────
# The counters reset with the gateway process and two of the three event
# classes produce no log line at all (the no-token 401 by C-1, a rate-limited
# failure until its lazy summary flushes), so the snapshot is the ONLY place
# an age can come from. Each assertion below pins the TIMESTAMP'S OWN VALUE —
# never `count > 0 == (ts is not None)`, which is the half-guard shape
# `fact:1309` records: both sides can move together to a wrong answer (a
# snapshot that stamped nothing and counted nothing would satisfy it).

def _parse_iso_utc(value):
    """Parse and require a tz-aware ISO-8601 stamp — the format the sibling
    llm_faults `last.ts` already uses."""
    from datetime import datetime, timezone
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None, f"last_ts must be tz-aware, got {value!r}"
    return parsed.astimezone(timezone.utc)


def test_credentials_snapshot_last_ts_keys_are_none_before_any_event():
    """Absence is the honest state, and it is distinguishable from 'just now'."""
    mod = load_coordinator()
    snap = mod._credentials_snapshot()
    assert snap["token_verify_failed"] == 0
    assert snap["token_verify_failed_last_ts"] is None
    assert snap["daemon_tokens_issued_last_ts"] is None
    assert snap["audit_log_dropped_last_ts"] is None


def test_token_verify_failed_last_ts_is_a_real_timestamp_bounded_by_the_call():
    from datetime import datetime, timezone
    mod = load_coordinator()
    before = datetime.now(timezone.utc)
    mod._record_token_verify_failed(_FakeRequest(), "some_bad_token")
    after = datetime.now(timezone.utc)
    snap = mod._credentials_snapshot()
    assert snap["token_verify_failed"] == 1
    stamped = _parse_iso_utc(snap["token_verify_failed_last_ts"])
    assert before <= stamped <= after, (
        f"{stamped} not inside [{before}, {after}] — the stamp is not the event's own time")


def test_token_verify_failed_last_ts_stamped_even_when_no_token_presented():
    """C-1's no-token class writes no log line, so the snapshot is the only
    record that it happened at all. If this ever regresses, the one class an
    unauthenticated caller can trigger becomes the one with no timing."""
    mod = load_coordinator()
    mod._record_token_verify_failed(_FakeRequest(), None)
    snap = mod._credentials_snapshot()
    assert snap["token_verify_failed"] == 1
    _parse_iso_utc(snap["token_verify_failed_last_ts"])


def test_token_verify_failed_last_ts_advances_on_a_later_failure():
    """Pins that the stamp tracks the LATEST event, not the first — a
    write-once stamp would age forever while failures kept arriving."""
    mod = load_coordinator()
    mod._record_token_verify_failed(_FakeRequest(), "bad_one")
    first = _parse_iso_utc(mod._credentials_snapshot()["token_verify_failed_last_ts"])
    mod._record_token_verify_failed(_FakeRequest(), "bad_two")
    second = _parse_iso_utc(mod._credentials_snapshot()["token_verify_failed_last_ts"])
    assert second >= first
    assert mod._credentials_snapshot()["token_verify_failed"] == 2


def test_daemon_tokens_issued_last_ts_is_a_real_timestamp_bounded_by_the_call():
    from datetime import datetime, timezone
    mod = load_coordinator()
    before = datetime.now(timezone.utc)
    mod.record_daemon_token_issued("rem_daemon")
    after = datetime.now(timezone.utc)
    snap = mod._credentials_snapshot()
    assert snap["daemon_tokens_issued"] == 1
    stamped = _parse_iso_utc(snap["daemon_tokens_issued_last_ts"])
    assert before <= stamped <= after


def test_audit_log_dropped_last_ts_comes_from_the_writer_drop_site(tmp_path):
    """The drop stamp is owned by AsyncLineWriter, where the drop happens —
    not recomputed by the snapshot, which would only know polling time."""
    from datetime import datetime, timezone
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    assert mod._credentials_snapshot()["audit_log_dropped_last_ts"] is None
    before = datetime.now(timezone.utc)
    mod._credential_audit_writer.dropped += 1
    mod._credential_audit_writer.last_dropped_ts = datetime.now(timezone.utc).isoformat()
    after = datetime.now(timezone.utc)
    snap = mod._credentials_snapshot()
    assert snap["audit_log_dropped"] == 1
    assert before <= _parse_iso_utc(snap["audit_log_dropped_last_ts"]) <= after


def test_audit_log_dropped_last_ts_none_when_writer_disabled():
    mod = load_coordinator(credential_audit_log_path="")
    assert mod._credentials_snapshot()["audit_log_dropped_last_ts"] is None


# ── 8. token_verify_failed ────────────────────────────────────────────────────

def test_token_verify_failed_bumps_counter_when_token_presented():
    mod = load_coordinator()
    mod._record_token_verify_failed(_FakeRequest(), "tok_bad")
    assert mod._credential_counters["token_verify_failed"] == 1


def test_token_verify_failed_bumps_counter_even_when_no_token_presented():
    """The COUNTER is the complete, unthrottled signal (security review
    C-1) — it increments regardless of whether a log line is written."""
    mod = load_coordinator()
    mod._record_token_verify_failed(_FakeRequest(), None)
    assert mod._credential_counters["token_verify_failed"] == 1


def test_token_verify_failed_no_line_written_when_no_token_presented(tmp_path):
    """⚑ Security review C-1, the core fix: a no-token 401 (the cheapest,
    fully anonymous, zero-forensic-value case) must never write a line at
    all — only the counter moves. MUTATION TARGET: remove the `presented_
    token is None` early return and this must fail."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(_FakeRequest(), None)
    assert not target.exists()


def test_token_verify_failed_line_written_when_token_presented(tmp_path):
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(_FakeRequest(), "tok_x")
    assert target.exists()
    line = json.loads(target.read_text().strip())
    assert line["event"] == "token_verify_failed"


def test_token_verify_failed_digest_prefix_is_8_hex_chars_never_the_raw_token(tmp_path):
    """SEC-08 property, proven end to end through a real file write — the
    presented secret value must NEVER appear in any log line."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(_FakeRequest(), "tok_super_secret_value")
    content = target.read_text()
    assert "tok_super_secret_value" not in content
    line = json.loads(content.strip())
    assert line["digest_prefix"] == _digest("tok_super_secret_value")[:8]
    assert len(line["digest_prefix"]) == 8


def test_token_verify_failed_claimed_agent_is_always_none_today(tmp_path):
    """Documents the current shape: bearer tokens carry no separate name
    claim, so claimed_agent is always None (the field exists for the planned
    PoP resolver)."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(_FakeRequest(), "tok_x")
    line = json.loads(target.read_text().strip())
    assert line["claimed_agent"] is None


def test_token_verify_failed_carries_path_and_transport_attribution(tmp_path):
    """Security review O-3: the surviving line names the request path and
    transport kind, even with no kernel-attested principal (TCP, this
    test's _FakeRequest)."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._record_token_verify_failed(_FakeRequest(path="/memory/save"), "tok_x")
    line = json.loads(target.read_text().strip())
    assert line["path"] == "/memory/save"
    assert line["transport"] == "tcp"
    assert "principal" not in line  # no kernel credential on TCP — honestly absent


def test_token_verify_failed_carries_principal_when_peer_identity_available(tmp_path, monkeypatch):
    """Security review O-3: when _peer_identity resolves (a UDS connection),
    the principal and connection fingerprint are included — same shape as
    the existing gateway audit line (_audit)."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    monkeypatch.setattr(mod, "_peer_identity",
                         lambda request: {"user": "xenofon", "uid": 1000, "gid": 1000, "pid": 4242})
    mod._record_token_verify_failed(_FakeRequest(), "tok_x")
    line = json.loads(target.read_text().strip())
    assert line["principal"] == "xenofon"
    assert line["connected_from"]["uid"] == 1000
    assert line["connected_from"]["pid"] == 4242


def test_transport_kind_tcp_when_no_transport():
    mod = load_coordinator()
    assert mod._transport_kind(_FakeRequest()) == "tcp"


# ── 8b. token_verify_failed rate limit + suppression summary (C-1) ──────────

def test_token_verify_failed_rate_limit_suppresses_beyond_burst(tmp_path, monkeypatch):
    """⚑ Security review C-1: an attacker presenting a fresh random token
    every attempt (so digest_prefix differs each time, defeating any
    per-token dedup) must still be bounded — the Nth-plus-one line within
    the window is suppressed, not written."""
    target = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("TOKEN_VERIFY_FAILED_LOG_RATE", "5")
    monkeypatch.setenv("TOKEN_VERIFY_FAILED_LOG_WINDOW", "60")
    mod = load_coordinator(credential_audit_log_path=str(target))
    for i in range(10):
        mod._record_token_verify_failed(_FakeRequest(), f"tok_{i}")
    lines = [json.loads(l) for l in target.read_text().splitlines()]
    tvf_lines = [l for l in lines if l["event"] == "token_verify_failed"]
    assert len(tvf_lines) == 5  # exactly the burst capacity, not 10
    assert mod._credential_counters["token_verify_failed"] == 10  # counter still saw all 10


def test_token_verify_failed_suppression_summary_line_emitted_on_next_allowed(tmp_path, monkeypatch):
    """MUTATION TARGET: remove the suppression-summary flush and this fails
    — the summary line must appear once the bucket has room again, naming
    how many were dropped."""
    target = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("TOKEN_VERIFY_FAILED_LOG_RATE", "2")
    monkeypatch.setenv("TOKEN_VERIFY_FAILED_LOG_WINDOW", "0.05")  # refills fast for the test
    mod = load_coordinator(credential_audit_log_path=str(target))
    for i in range(6):
        mod._record_token_verify_failed(_FakeRequest(), f"tok_{i}")
    import time as _time
    _time.sleep(0.1)  # let the bucket refill past the window
    mod._record_token_verify_failed(_FakeRequest(), "tok_after_refill")
    lines = [json.loads(l) for l in target.read_text().splitlines()]
    events = [l["event"] for l in lines]
    assert "token_verify_failed_suppressed" in events
    summary = next(l for l in lines if l["event"] == "token_verify_failed_suppressed")
    assert summary["count"] >= 1
    assert "window_s" in summary


def test_token_verify_failed_rate_limit_allows_a_fresh_bucket_from_the_start():
    mod = load_coordinator()
    assert mod._tvf_rate_limit_allow() is True


# ── 8c. drop-newest queue backstop for attacker-triggerable events (R-5) ────

def test_write_credential_audit_line_uses_drop_newest_for_token_verify_failed(monkeypatch, tmp_path):
    """MUTATION TARGET: _write_credential_audit_line must pass
    drop_newest_when_full=True for token_verify_failed (and its suppression
    summary) — spy on AsyncLineWriter.write to confirm the flag actually
    reaches it, independent of queue-full behaviour itself (covered in
    test_log_hygiene.py)."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    calls = []
    real_write = mod._credential_audit_writer.write

    def _spy(line, **kw):
        calls.append(kw.get("drop_newest_when_full", False))
        return real_write(line, **kw)

    monkeypatch.setattr(mod._credential_audit_writer, "write", _spy)
    mod._record_token_verify_failed(_FakeRequest(), "tok_x")
    mod.record_daemon_token_issued("rem_daemon")  # a lifecycle event — NOT attacker-triggerable
    assert calls == [True, False]


# ── 9. _write_credential_audit_line reserved-key precedence (N-3) ───────────

def test_write_credential_audit_line_reserved_keys_cannot_be_shadowed(tmp_path):
    """MUTATION TARGET (N-3): `event` and `origin` are already explicit named
    parameters, so Python's own signature binding refuses a **fields entry
    with either name (a duplicate-keyword TypeError, not a silent shadow) —
    the ACTUAL exposure the finding describes is `ts`, which is not an
    explicit parameter and was only ever protected by dict-literal ORDER.
    Swap the reserved keys back before `**fields` in the record literal and
    this fails."""
    target = tmp_path / "credential-audit.jsonl"
    mod = load_coordinator(credential_audit_log_path=str(target))
    mod._write_credential_audit_line("real_event", origin="gateway",
                                      ts="SHADOW_TS", extra="ok")
    line = json.loads(target.read_text().strip())
    assert line["event"] == "real_event"
    assert line["origin"] == "gateway"
    assert line["ts"] != "SHADOW_TS"
    assert line["extra"] == "ok"


def test_write_credential_audit_line_event_origin_are_protected_by_signature_binding():
    """event/origin can't even be SENT twice — Python raises before the
    function body runs, which is a stronger guarantee than the dict-literal
    ordering alone (that part of N-3 was already true; this documents it)."""
    mod = load_coordinator()
    with pytest.raises(TypeError):
        mod._write_credential_audit_line("real_event", origin="gateway", event="shadow")
