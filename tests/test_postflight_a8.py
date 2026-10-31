"""A8 reads the last daemon chat from telemetry. A6 still records corpus size.

A8 does not post a completion. The extracted ``a8_last_call`` prints
``status|backend|age`` or ``NOT_EXERCISED``. An idle gateway warns and does
not set ``afail[A8]``.
"""
import contextlib
import http.server
import json
import os
import re
import shlex
import subprocess
import threading

import pytest
from pathlib import Path

POSTFLIGHT = (Path(__file__).parent.parent / "shared-memory" / "scripts"
              / "postflight.sh")
POSTFLIGHT_MD = (Path(__file__).parent.parent / "shared-memory"
                 / "Documentation" / "postflight.md")


def _extract_marked_block(begin: str, end: str) -> str:
    text = POSTFLIGHT.read_text()
    pattern = re.escape(begin) + r".*?\n(.*?)\n[ \t]*" + re.escape(end)
    m = re.search(pattern, text, re.S)
    assert m, f"could not find a {begin!r} ... {end!r} block in {POSTFLIGHT}"
    return m.group(1)


A8_SECTION_START = "# ── A8 — last daemon chat"
A8_SECTION_END = "# ── A9 — encoder window contract"
SUMMARY_SECTION_START = "# ── Summary"


def _extract_a8_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(A8_SECTION_START)
    end = text.find(A8_SECTION_END)
    assert start != -1 and end > start
    return text[start:end]


def _extract_summary_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(SUMMARY_SECTION_START)
    assert start != -1
    return text[start:]


def _run_last_call(payload: str) -> str:
    block = _extract_marked_block("# >>> A8_LAST_CALL", "# <<< A8_LAST_CALL")
    proc = subprocess.run(
        ["bash", "-c", block + "\ncat | a8_last_call"],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_a8_last_call_markers_present_exactly_once():
    text = POSTFLIGHT.read_text()
    assert text.count("# >>> A8_LAST_CALL") == 1
    assert text.count("# <<< A8_LAST_CALL") == 1


def test_a8_last_call_not_exercised_when_no_timestamp():
    assert _run_last_call("") == "UNPARSEABLE"
    assert _run_last_call("not-json") == "UNPARSEABLE"
    assert _run_last_call('{"status":"success","telemetry":{"llm":{"pool":{}}}}') == "NOT_EXERCISED"


def test_a8_last_call_walks_the_telemetry_wrapper_and_keeps_the_newest():
    payload = json.dumps({
        "status": "success",
        "telemetry": {"llm": {"pool": {
            "http://old:1": {"last_call": {"ts": "2026-01-01T00:00:00+00:00", "status": 500}},
            "http://new:1": {"last_call": {"ts": "2026-06-01T00:00:00+00:00", "status": 200}},
        }}},
    })
    status, backend, age = _run_last_call(payload).split("|")
    assert status == "200"
    assert backend == "http://new:1"
    assert age.isdigit()


def test_a8_section_reads_telemetry_and_warns_when_idle():
    a8 = _extract_a8_section()
    assert "a8_last_call" in a8
    assert "/memory/telemetry" in a8
    assert "curl -s --compressed --max-time 15 -o" in a8
    assert '-K -' in a8
    assert "Authorization: Bearer" in a8
    assert "HTTP ${a8_http:-000}" in a8
    assert "telemetry unparseable" in a8
    assert "is not 2xx" in a8
    assert "not exercised yet" in a8
    assert "bad A8" not in a8
    assert "v1/chat/completions" not in a8
    assert "afail[A8]" not in POSTFLIGHT.read_text()


def test_a8_stays_named_in_the_exit_loops_without_failing_when_idle():
    summary = _extract_summary_section()
    loops = re.findall(r"for a in ([A-Za-z0-9 ]+); do", summary)
    assert len(loops) == 2
    for loop in loops:
        assert "A8" in loop.split()
    assert "A1–A5 and A9" in summary
    assert "A8 reports and does not decide" in summary
    assert "A8 not exercised yet" in summary


def test_a1_still_names_the_missing_token_and_does_not_fail_a8():
    text = POSTFLIGHT.read_text()
    assert "afail[A8]=1" not in text
    assert "A5, A6 and A8 are skipped for this same missing token" in text
    assert "A4, A5, A6 and A8 are skipped for this same missing token" in text


def test_postflight_md_says_a8_does_not_prove_a_completion_when_idle():
    doc = POSTFLIGHT_MD.read_text()
    assert "What this does not prove" in doc
    assert "not exercised yet" in doc
    assert "A1–A5 and A9" in doc
    assert "A8 reports and does not decide" in doc
    assert "does not mean a completion was posted" in doc
    assert "The install works end to end" not in POSTFLIGHT.read_text()


def _extract_prefix() -> str:
    text = POSTFLIGHT.read_text()
    marker = 'echo "Shared Memory — postflight verification'
    return text[: text.index(marker)]


class _TelemetryHandler(http.server.BaseHTTPRequestHandler):
    status_code = 200
    body = b"{}"
    seen_auth_header = None
    seen_path = None

    def do_GET(self):
        type(self).seen_auth_header = self.headers.get("Authorization")
        type(self).seen_path = self.path
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, fmt, *args):
        return


@contextlib.contextmanager
def _stub_server(status_code=200, body=b"{}"):
    handler = type("Handler", (_TelemetryHandler,), {
        "status_code": status_code,
        "body": body,
        "seen_auth_header": None,
        "seen_path": None,
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler
    finally:
        server.shutdown()
        server.server_close()


def _run_a8_section(*, gateway_url, auth_on="0", token_missing="0", gateway_down="0", agent_token=None):
    lines = [
        _extract_prefix(),
        "set -uo pipefail",
        f"GATEWAY_URL={shlex.quote(gateway_url)}",
        f"auth_on={shlex.quote(auth_on)}",
        f"token_missing={shlex.quote(token_missing)}",
        f"gateway_down={shlex.quote(gateway_down)}",
    ]
    if agent_token is not None:
        lines.append(f"AGENT_TOKEN={shlex.quote(agent_token)}")
    lines.append(_extract_a8_section())
    lines.append('echo "AFAIL_A8=${afail[A8]:-0}"')
    return subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=30)


def _chat(status):
    return json.dumps({"telemetry": {"llm": {"pool": {
        "http://b:1": {"last_call": {"ts": "2026-06-01T00:00:00+00:00", "status": status}},
    }}}}).encode()


def test_a8_sends_bearer_token_when_auth_on():
    with _stub_server(200, _chat(200)) as (url, handler):
        result = _run_a8_section(gateway_url=url, auth_on="1", agent_token="test-token-fixture-value")
    assert result.returncode == 0, result.stderr
    assert handler.seen_path == "/memory/telemetry"
    assert handler.seen_auth_header == "Bearer test-token-fixture-value"
    assert "A8 last daemon chat: status 200" in result.stdout
    assert "AFAIL_A8=0" in result.stdout


def test_a8_never_sends_a_bearer_header_when_auth_off():
    with _stub_server(200, _chat(200)) as (url, handler):
        result = _run_a8_section(gateway_url=url, auth_on="0", agent_token="test-token-fixture-value")
    assert result.returncode == 0, result.stderr
    assert handler.seen_auth_header is None
    assert "A8 last daemon chat: status 200" in result.stdout


def test_a8_non_2xx_last_call_warns_with_the_status():
    with _stub_server(200, _chat(500)) as (url, _handler):
        result = _run_a8_section(gateway_url=url, auth_on="0")
    assert result.returncode == 0, result.stderr
    assert "status 500 is not 2xx" in result.stdout
    assert "✓" not in result.stdout
    assert "AFAIL_A8=0" in result.stdout


def test_a8_http_401_is_not_called_not_exercised():
    with _stub_server(401, b'{"error":"unauthorized"}') as (url, _handler):
        result = _run_a8_section(gateway_url=url, auth_on="1", agent_token="test-token-fixture-value")
    assert "telemetry read failed: HTTP 401" in result.stdout
    assert "not exercised yet" not in result.stdout


def test_a8_unparseable_telemetry_is_not_called_not_exercised():
    with _stub_server(200, b"not-json") as (url, _handler):
        result = _run_a8_section(gateway_url=url, auth_on="0")
    assert "telemetry unparseable" in result.stdout
    assert "not exercised yet" not in result.stdout


# ── D22: A6's baseline states the corpus size it was measured against ─────

def run_a6_baseline_writer(tmp_path, *, health_full="{}", mode="install",
                            corpus_scope="project:install-verification",
                            corpus_technical_docs="1", live_summary_count="",
                            telemetry_full=""):
    source = _extract_marked_block("# >>> A6_BASELINE_WRITER", "# <<< A6_BASELINE_WRITER")
    base_file = tmp_path / "baseline.json"
    lines = [
        "set -uo pipefail",
        f"health_full={shlex.quote(health_full)}",
        f"anon_health={shlex.quote(health_full)}",
        f"telemetry_full={shlex.quote(telemetry_full)}",
        f"base_file={shlex.quote(str(base_file))}",
        'short_ms=""', 'big_ms=""', 'search_ms=""', 'search_rebaseline_ms=""',
        'checkout_fw="9.9.9"',
        f"POSTFLIGHT_MODE={shlex.quote(mode)}",
        f"corpus_scope={shlex.quote(corpus_scope)}",
        f"corpus_technical_docs={shlex.quote(corpus_technical_docs)}",
        f"live_summary_count={shlex.quote(live_summary_count)}",
        source,
        'echo "WRITTEN=$written"',
    ]
    harness = "\n".join(lines)
    result = subprocess.run(["bash", "-c", harness], capture_output=True,
                             text=True, timeout=15)
    return result, base_file


def test_the_a1_shape_check_names_only_keys_the_gateway_keeps_serving():
    """⛔ POSTFLIGHT IS THE DEPLOY GATE. A1 proves authentication by requiring a
    handful of `/health` keys — so a key the contract has scheduled for removal
    would make every install fail the gate on the release it goes, from a script
    nobody thought to re-read. The list is checked against the contract rather
    than against a memory of what `/health` used to carry."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]
                           / "shared-memory" / "scripts"))
    import telemetry_contract as tc

    calls = re.findall(r"k for k in \(([^)]*)\) if k not in d",
                       POSTFLIGHT.read_text())
    assert len(calls) == 2, (
        "expected A1's shape check in both the authenticated and the auth-off "
        f"branch, found {len(calls)}")
    for call in calls:
        keys = re.findall(r'"([^"]+)"', call)
        assert keys, f"A1 shape check names no keys: {call!r}"
        for key in keys:
            rows = [p for p in tc.HEALTH if p == key or p.startswith(key + ".")]
            assert rows, f"A1 requires {key!r}, which /health does not document"
            stamped = sorted(p for p in rows if tc.HEALTH[p]["removed_in"])
            assert not stamped, (
                f"A1 requires {key!r}, and the contract stops serving "
                f"{stamped} — the gate would fail on every install")


def test_a6_baseline_writer_markers_present_exactly_once():
    text = POSTFLIGHT.read_text()
    assert text.count("# >>> A6_BASELINE_WRITER") == 1
    assert text.count("# <<< A6_BASELINE_WRITER") == 1


def test_a6_baseline_records_the_capacity_record_from_the_numbers_endpoint(tmp_path):
    """The whole capacity record — fingerprint and measured probe — is what makes
    one baseline comparable with another, and it lives on `/memory/telemetry`.
    `/health` keeps only the sizing a client needs, so the baseline carries
    both blocks rather than assuming one contains the other."""
    result, base_file = run_a6_baseline_writer(
        tmp_path,
        health_full=json.dumps({"capacity": {"derived": {"s_mean_s": 1.0}}}),
        telemetry_full=json.dumps({"status": "success", "telemetry": {
            "capacity": {"trigger": "probe",
                         "fingerprint": {"hardware": {"nproc": 8}},
                         "derived": {"s_mean_s": 1.0, "queue_bound": 4}}}}),
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(base_file.read_text())
    assert doc["capacity"] == {"derived": {"s_mean_s": 1.0}}
    assert doc["capacity_telemetry"]["trigger"] == "probe"
    assert doc["capacity_telemetry"]["fingerprint"]["hardware"]["nproc"] == 8


def test_a6_baseline_records_no_capacity_record_without_a_token(tmp_path):
    """⛔ ABSENT, NOT INVENTED. No token means the numbers endpoint was never
    fetched — the field says so rather than reporting an empty record."""
    result, base_file = run_a6_baseline_writer(tmp_path, telemetry_full="")
    assert result.returncode == 0, result.stderr
    assert json.loads(base_file.read_text())["capacity_telemetry"] is None


def test_a6_baseline_states_corpus_size_canary_mode(tmp_path):
    result, base_file = run_a6_baseline_writer(
        tmp_path, mode="install", corpus_scope="project:install-verification",
        corpus_technical_docs="1",
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(base_file.read_text())
    assert doc["corpus_size"]["scope"] == "project:install-verification"
    assert doc["corpus_size"]["technical_docs"] == 1
    assert doc["corpus_size"]["community_summaries_live"] is None


def test_a6_baseline_states_corpus_size_rebaseline_mode(tmp_path):
    result, base_file = run_a6_baseline_writer(
        tmp_path, mode="re-baseline", corpus_scope="global",
        corpus_technical_docs="547", live_summary_count="21",
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(base_file.read_text())
    assert doc["corpus_size"]["scope"] == "global"
    assert doc["corpus_size"]["technical_docs"] == 547
    assert doc["corpus_size"]["community_summaries_live"] == 21


def test_a6_baseline_corpus_size_is_null_when_undeterminable(tmp_path):
    # Docker missing/unreachable -- corpus_technical_docs arrives empty.
    # D22's field must degrade to null, never a false zero (a real empty
    # corpus and "could not measure" must stay distinguishable).
    result, base_file = run_a6_baseline_writer(
        tmp_path, mode="install", corpus_scope="", corpus_technical_docs="",
    )
    assert result.returncode == 0, result.stderr
    doc = json.loads(base_file.read_text())
    assert doc["corpus_size"]["scope"] is None
    assert doc["corpus_size"]["technical_docs"] is None


def test_a6_prints_corpus_size_summary_line():
    text = POSTFLIGHT.read_text()
    assert "A6 corpus size at this baseline" in text


def test_every_postflight_curl_declares_it_accepts_compression():
    """A8 blamed the backend for postflight's own missing flag.

    MEASURED on a host whose only reasoning backend is an external API:
    DeepSeek gzips its responses, the gateway proxies the encoding through,
    and A8's curl wrote gzip bytes to its response file. It then found no
    content and reported "gateway returned HTTP 200 but no usable completion
    content — healthy backend(s) at request time: <the API>", naming the
    backend for a defect that was entirely local. A direct call with
    --compressed to the same gateway, same model, same moment, returned
    content 'ok' with finish_reason 'stop'.

    It passed on the reference workstation throughout, because a local
    llama-server does not gzip — so this is invisible until an install
    routes A8 at an external backend, which is exactly the configuration
    A8 exists to prove.

    memory_bridge.py is unaffected: httpx negotiates and decodes content
    encoding on its own. Only postflight's raw curl had to ask.

    ⚠ The matcher anchors on the WORD curl, not on `curl -s`. An earlier
    version keyed on `curl -s` and let three real forms through —
    `curl --silent`, `curl -fsSL` (which update_skill.sh actually uses) and
    `curl  -s` with two spaces. It also counted `--compressed` anywhere in
    the file, so prose mentions alone could have satisfied it.
    """
    path = os.path.join(os.path.dirname(__file__), "..",
                        "shared-memory", "scripts", "postflight.sh")
    raw = open(path, encoding="utf-8").read()
    # Join backslash continuations so a multi-line invocation is one unit.
    joined = re.sub(r"\\\n\s*", " ", raw)

    invocations = []
    for line in joined.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        # `curl` at a command position: start, or after a shell operator.
        if re.search(r"(?:^|[|;&(]|\$\()\s*!?\s*curl\b", line):
            invocations.append(line.strip())

    assert invocations, "no curl invocation found in postflight.sh at all"
    missing = [i for i in invocations if "--compressed" not in i]
    assert not missing, (
        "postflight curl invocation(s) without --compressed — a gzipping "
        f"backend makes these read as an empty response: {missing}"
    )
