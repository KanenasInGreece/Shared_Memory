"""A8 (reasoning-backend liveness, end to end) and D22 (A6's corpus-size
disclosure) -- pure-function, structural and live-stub-server tests.

Same technique as tests/test_postflight_rebaseline.py: extract the ACTUAL
shipped bash source (between its own `# >>> ... / # <<< ...` markers, or by
section-header text the way that file's `_extract_a5_section` does) and run
it standalone via subprocess, never a hand-written reimplementation that
could silently drift from the shipped script.

D23 context (v0.9.24, shared-memory/scripts/hive_mind_proxy.py): the
gateway's upstream URL join doubled `/v1` for any backend base already
ending in `/v1` (every cloud provider's documented shape), 404ing every real
reasoning completion -- completely silently, because a 404 is never billed
and /health's "llm" field is a bare /v1/models LIVENESS probe that happened
not to share the bug. REM retried the same dead completion every 30s for 45
minutes while every assertion that existed at the time passed green. A8
exists to close that hole: it drives a REAL completion through the exact
proxy path D23 broke, and this file's job is to prove A8 actually gates on
that (I-D1), never gates when there is honestly no backend to test (I-D2),
actually uses the proxy path rather than a liveness probe (I-D3), and that
A6's baseline states the corpus size its timings were measured against
(I-D4, D22).

Scope, matching the rest of this suite (mocked-only): the pure grading/
info/writer functions are extracted verbatim and unit- and mutation-tested
directly, including D22's actual JSON output (A6_BASELINE_WRITER runs
stand-alone with fixture argv/stdin, no live gateway needed). The
SKIP-never-gates / FAIL-always-gates wiring is verified by running A8's own
extracted section against a local stub HTTP server standing in for the
gateway -- no docker, no live gateway, no Postgres/Neo4j, unlike A1-A7's own
end-to-end verification against a reference install (see
test_postflight_rebaseline.py's docstring for that same split).

Fix round (operator ruling, post-build review): the first cut of
a8_backend_info() keyed "is a reasoning backend configured?" on /health's
`config.llm_backends` -- the CONFIGURED list, which hive_mind_proxy.py's
own _load_llm_backends() NEVER returns empty (an unset LLM_BACKENDS/
LLM_BACKENDS_JSON falls back to a single-entry list built from
LLM_DEFAULT_TARGET, itself defaulting to "http://localhost:5000"). That made
A8's SKIP branch effectively unreachable on any ordinary install: a
perfectly healthy LLM-less deployment (AGENTS.md Phase 7: "llm":"down"
blocks dreaming only, never saves/search) would fire a doomed completion at
the unconfigured default and FAIL postflight for lacking an LLM -- exactly
the outcome A8 exists to never cause. The fix re-keys on a DIFFERENT
top-level /health field, `llm_backends` (no `config.` prefix) -- the
per-backend STATUS MAP the gateway's own liveness probe already populates.
Confirmed vocabulary (hive_mind_proxy.py, the loop building `backend_status`
just above `checks["llm"] = ...`): "ok" (probe answered <400, or a
credentialed backend's unauthenticated 401/403 -- see that code's own
H-1/H-2 comment), "http_<code>" (any other status), "timeout", or "down"
(connect/other exception). "ok" is the ONLY healthy value. This file's tests
below cover BOTH reachable SKIP shapes: no backend reported at all, and
backends present but every one unhealthy (the realistic shape, since
config-resolved backends always exist by construction) -- the latter is the
case the fix round specifically required be testable.
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


# ── Extraction helpers ─────────────────────────────────────────────────────

def _extract_marked_block(begin: str, end: str) -> str:
    text = POSTFLIGHT.read_text()
    # The trailing [ \t]* tolerates an indented end marker (A6_BASELINE_
    # WRITER sits inside an `else` block, unlike the column-0 markers) --
    # the indentation itself is not part of the captured source.
    pattern = re.escape(begin) + r".*?\n(.*?)\n[ \t]*" + re.escape(end)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {begin!r} ... {end!r} block in {POSTFLIGHT} -- "
        f"the extraction markers moved or were removed"
    )
    return m.group(1)


A8_SECTION_START = "# ── A8 — reasoning-backend liveness, end to end"
A8_SECTION_END = "# ── Summary"
SUMMARY_SECTION_START = "# ── Summary"


def _extract_a8_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(A8_SECTION_START)
    end = text.find(A8_SECTION_END)
    assert start != -1, f"could not find {A8_SECTION_START!r} in {POSTFLIGHT}"
    assert end != -1 and end > start, (
        f"could not find {A8_SECTION_END!r} after the A8 header in {POSTFLIGHT}"
    )
    return text[start:end]


def _extract_summary_section() -> str:
    text = POSTFLIGHT.read_text()
    start = text.find(SUMMARY_SECTION_START)
    assert start != -1, f"could not find {SUMMARY_SECTION_START!r} in {POSTFLIGHT}"
    return text[start:]


def _extract_prefix() -> str:
    """Everything from the top of the file up to (not including) the first
    executed statement (the banner echo). Pure definitions and env-default
    variable assignments only -- no side effects -- so it is safe to run
    standalone ahead of a hand-set fixture state."""
    text = POSTFLIGHT.read_text()
    marker = 'echo "Shared Memory — postflight verification'
    idx = text.index(marker)
    return text[:idx]


# ── a8_backend_info (pure) ─────────────────────────────────────────────────

def run_a8_backend_info(health_json: str) -> subprocess.CompletedProcess:
    source = _extract_marked_block("# >>> A8_BACKEND_INFO", "# <<< A8_BACKEND_INFO")
    return subprocess.run(
        ["bash", "-c", source + "\na8_backend_info"],
        input=health_json, capture_output=True, text=True, timeout=15,
    )


def test_a8_backend_info_markers_present_exactly_once():
    text = POSTFLIGHT.read_text()
    assert text.count("# >>> A8_BACKEND_INFO") == 1
    assert text.count("# <<< A8_BACKEND_INFO") == 1


def test_backend_info_empty_health_yields_zero_healthy():
    result = run_a8_backend_info("")
    assert result.returncode == 0
    assert result.stdout.strip() == "0||"


def test_backend_info_missing_llm_backends_key_yields_zero_healthy():
    result = run_a8_backend_info(json.dumps({"status": "ok"}))
    assert result.returncode == 0
    assert result.stdout.strip() == "0||"


def test_backend_info_empty_llm_backends_map_yields_zero_healthy():
    health = {"llm_backends": {}}
    result = run_a8_backend_info(json.dumps(health))
    assert result.returncode == 0
    assert result.stdout.strip() == "0||"


def test_backend_info_one_healthy_backend_reports_count_url_and_summary():
    health = {"llm_backends": {"http://example-backend:5000": "ok"}}
    result = run_a8_backend_info(json.dumps(health))
    assert result.returncode == 0
    assert result.stdout.strip() == "1|http://example-backend:5000|http://example-backend:5000=ok"


def test_backend_info_only_ok_status_counts_as_healthy():
    # Confirmed vocabulary (hive_mind_proxy.py's backend_status loop): "ok",
    # "http_<code>", "timeout", "down" -- "ok" is the ONLY healthy value.
    # Every other value must be excluded from the healthy list/count, but
    # still show up in the full status summary (so a SKIP message can name
    # WHY, not just THAT).
    health = {"llm_backends": {
        "http://backend-a:5000": "ok",
        "http://backend-b:4000": "down",
        "http://backend-c:4001": "timeout",
        "http://backend-d:4002": "http_500",
    }}
    result = run_a8_backend_info(json.dumps(health))
    assert result.returncode == 0
    count, urls, summary = result.stdout.strip().split("|")
    assert count == "1"
    assert urls == "http://backend-a:5000"
    assert "http://backend-b:4000=down" in summary
    assert "http://backend-c:4001=timeout" in summary
    assert "http://backend-d:4002=http_500" in summary


def test_backend_info_multiple_healthy_backends_preserve_order():
    health = {"llm_backends": {
        "http://backend-a:5000": "ok",
        "http://backend-b:4000": "ok",
    }}
    result = run_a8_backend_info(json.dumps(health))
    assert result.returncode == 0
    assert result.stdout.strip() == (
        "2|http://backend-a:5000,http://backend-b:4000|"
        "http://backend-a:5000=ok,http://backend-b:4000=ok"
    )


def test_backend_info_reads_the_status_map_never_the_configured_list():
    # The fix-round bug: an earlier cut of this function read `config.
    # llm_backends` (the CONFIGURED list, never empty by construction) --
    # confirm the current version reads the top-level `llm_backends` STATUS
    # map instead, and never falls back to `config` even when both keys are
    # present with different, conflicting content. Also doubles as the
    # credential-safety check: a backend descriptor under `config` can
    # legitimately carry has_credential/token_env-shaped fields, and none of
    # that must ever reach this function's output.
    health = {
        "config": {"llm_backends": [
            {"url": "http://not-the-answer:9999", "has_credential": True,
             "token": "sk-should-never-appear-anywhere"},
        ]},
        "llm_backends": {"http://real-backend:5000": "ok"},
    }
    result = run_a8_backend_info(json.dumps(health))
    assert result.returncode == 0
    assert result.stdout.strip() == "1|http://real-backend:5000|http://real-backend:5000=ok"
    assert "sk-should-never-appear-anywhere" not in result.stdout
    assert "not-the-answer" not in result.stdout


# ── a8_grade_completion (pure) ─────────────────────────────────────────────

def run_a8_grade_completion(status: str, body: str, fault_origin: str = "") -> subprocess.CompletedProcess:
    source = _extract_marked_block("# >>> A8_GRADE_COMPLETION", "# <<< A8_GRADE_COMPLETION")
    cmd = source + f"\na8_grade_completion {shlex.quote(status)} {shlex.quote(fault_origin)}"
    return subprocess.run(
        ["bash", "-c", cmd],
        input=body, capture_output=True, text=True, timeout=15,
    )


def test_a8_grade_completion_markers_present_exactly_once():
    text = POSTFLIGHT.read_text()
    assert text.count("# >>> A8_GRADE_COMPLETION") == 1
    assert text.count("# <<< A8_GRADE_COMPLETION") == 1


def test_grade_200_with_real_content_is_OK():
    body = json.dumps({"choices": [{"message": {"content": "ok"}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "OK"


def test_grade_200_with_empty_string_content_is_EMPTY():
    # I-D1: a 200 with empty content is a failure, not a pass.
    body = json.dumps({"choices": [{"message": {"content": ""}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_whitespace_only_content_is_EMPTY():
    body = json.dumps({"choices": [{"message": {"content": "   \n\t "}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_missing_choices_is_EMPTY():
    result = run_a8_grade_completion("200", json.dumps({}))
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_unparseable_json_is_EMPTY():
    result = run_a8_grade_completion("200", "not json at all")
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_non_string_content_envelope_is_EMPTY():
    # Defensive against an unexpected envelope shape (list/dict content) --
    # must never crash or pass a non-string through as if it were usable.
    body = json.dumps({"choices": [{"message": {"content": ["not", "a", "string"]}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_reasoning_content_and_empty_content_is_OK():
    # Item B (W5): a thinking model at A8's max_tokens: 16 returns 16 tokens
    # of reasoning_content, EMPTY content, finish_reason: length -- that is
    # still proof a real completion crossed the gateway proxy join.
    body = json.dumps({"choices": [{"message": {"content": "",
                                                  "reasoning_content": "let me think..."},
                                     "finish_reason": "length"}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "OK"


def test_grade_200_with_structured_reasoning_content_is_EMPTY():
    # N1: the reasoning check mirrors the content guard exactly -- a
    # structured reasoning_content object ({"blocks": []}) must NOT pass.
    body = json.dumps({"choices": [{"message": {"content": "",
                                                  "reasoning_content": {"blocks": []}}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_both_content_and_reasoning_empty_is_EMPTY():
    body = json.dumps({"choices": [{"message": {"content": "",
                                                  "reasoning_content": ""}}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "EMPTY"


def test_grade_200_with_reasoning_content_and_content_filter_is_OK():
    # Accepted semantic shift (Item B): a finish_reason: content_filter
    # response carrying reasoning but no content now grades OK -- correct
    # for A8's question (did a completion cross the join?).
    body = json.dumps({"choices": [{"message": {"content": "",
                                                  "reasoning_content": "reasoning tokens here"},
                                     "finish_reason": "content_filter"}]})
    result = run_a8_grade_completion("200", body)
    assert result.returncode == 0
    assert result.stdout.strip() == "OK"


def test_grade_404_is_HTTP_404():
    result = run_a8_grade_completion("404", "")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_404"


def test_grade_500_is_HTTP_500():
    result = run_a8_grade_completion("500", "")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_500"


def _refusal_body(declaration="none", constraint="privacy", error="no_eligible_backend"):
    body = {"error": error, "constraint": constraint, "role": None}
    if declaration is not None:
        body["declaration"] = declaration
    return json.dumps(body)


@pytest.mark.parametrize("declaration", ["none", "no_role_less_opt_in"])
def test_grade_422_skips_on_both_declaration_values_when_gateway_origin(declaration):
    """Ruling B(i)/E(alpha2) (§6.7): a named non-fatal skip on EITHER
    declaration value, but ONLY when the gateway itself stamped the
    fault-origin header."""
    result = run_a8_grade_completion("422", _refusal_body(declaration=declaration), "gateway")
    assert result.returncode == 0
    assert result.stdout.strip() == f"SKIP_{declaration}"


def test_grade_422_stays_fatal_on_fit_constraint_even_with_declaration():
    """Fit and every other routing/join defect (the D23 class) stay FATAL —
    a genuinely oversized request must never be misread as an undeclared-
    fleet skip just because `declaration` happens to be present."""
    result = run_a8_grade_completion("422", _refusal_body(declaration="none", constraint="fit"), "gateway")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_422"


def test_grade_422_stays_fatal_when_no_declaration_key_present():
    """An explicitly-declared, correctly-scoped fleet's plain refusal
    (no `declaration` key at all) must stay FATAL — this is not
    misconfiguration, per Ruling E(alpha2) arm 1."""
    result = run_a8_grade_completion("422", _refusal_body(declaration=None), "gateway")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_422"


def test_grade_422_stays_fatal_when_fault_origin_is_not_gateway():
    """A passed-through UPSTREAM 422 (fault_origin != 'gateway', including
    the empty-header case) must never be misread as a gateway refusal —
    the same discipline rem_loop.py/consolidation_loop.py enforce."""
    result = run_a8_grade_completion("422", _refusal_body(), "upstream")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_422"


def test_grade_422_stays_fatal_when_fault_origin_header_absent():
    result = run_a8_grade_completion("422", _refusal_body(), "")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_422"


def test_grade_422_unparseable_body_stays_fatal_never_crashes():
    result = run_a8_grade_completion("422", "not json at all", "gateway")
    assert result.returncode == 0
    assert result.stdout.strip() == "HTTP_422"


def test_grade_000_is_NO_RESPONSE():
    result = run_a8_grade_completion("000", "")
    assert result.returncode == 0
    assert result.stdout.strip() == "NO_RESPONSE"


def test_grade_empty_status_is_NO_RESPONSE():
    result = run_a8_grade_completion("", "")
    assert result.returncode == 0
    assert result.stdout.strip() == "NO_RESPONSE"


# ── A8's own section, live against a local stub HTTP server ────────────────
# No docker, no real gateway: a plain http.server standing in for the one
# route A8 talks to. token_missing/gateway_down/auth_on/health_full are
# hand-set fixture inputs -- A1's own detection of them is NOT re-tested
# here (see test_a1_marks_afail_a8_on_missing_token below for that half).

class _StubHandler(http.server.BaseHTTPRequestHandler):
    status_code = 200
    body = b'{"choices":[{"message":{"content":"ok"}}]}'
    seen_auth_header = None
    extra_headers = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).seen_auth_header = self.headers.get("Authorization")
        type(self).seen_path = self.path
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        for name, value in self.extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format, *args):  # noqa: A002 -- stdlib signature
        pass


@contextlib.contextmanager
def _stub_server(status_code=200, body=b'{"choices":[{"message":{"content":"ok"}}]}',
                  extra_headers=None):
    handler_cls = type("Handler", (_StubHandler,), {
        "status_code": status_code, "body": body,
        "seen_auth_header": None, "seen_path": None,
        "extra_headers": extra_headers or {},
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler_cls
    finally:
        server.shutdown()
        server.server_close()


def _free_closed_port() -> int:
    """A port nothing is listening on, for the connection-refused case."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_a8_live(*, gateway_url, health_full="{}", auth_on="0", token_missing="0",
                gateway_down="0", agent_token=None, client_timeout="10"):
    prefix = _extract_prefix()
    a8 = _extract_a8_section()
    lines = [
        prefix,
        "set -uo pipefail",
        f"GATEWAY_URL={shlex.quote(gateway_url)}",
        f"CLIENT_TIMEOUT={shlex.quote(client_timeout)}",
        f"auth_on={shlex.quote(auth_on)}",
        f"token_missing={shlex.quote(token_missing)}",
        f"gateway_down={shlex.quote(gateway_down)}",
        f"health_full={shlex.quote(health_full)}",
    ]
    if agent_token is not None:
        lines.append(f"AGENT_TOKEN={shlex.quote(agent_token)}")
    lines.append(a8)
    lines.append('echo "AFAIL_A8=${afail[A8]:-0}"')
    harness = "\n".join(lines)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=30,
    )


def run_a8_and_summary_live(*, gateway_url, health_full="{}", auth_on="0",
                             token_missing="0", gateway_down="0", agent_token=None,
                             client_timeout="10"):
    # ND6: A8's own section PLUS the real Summary section, run together --
    # the summary must carry a named skip's declaration itself, from a
    # variable the SKIP_* arm sets (never re-derived from afail[], which a
    # skip deliberately leaves untouched -- see postflight.sh's own ND6
    # comment at the SKIP_* case).
    prefix = _extract_prefix()
    a8 = _extract_a8_section()
    summary = _extract_summary_section()
    lines = [
        prefix,
        "set -uo pipefail",
        f"GATEWAY_URL={shlex.quote(gateway_url)}",
        f"CLIENT_TIMEOUT={shlex.quote(client_timeout)}",
        f"auth_on={shlex.quote(auth_on)}",
        f"token_missing={shlex.quote(token_missing)}",
        f"gateway_down={shlex.quote(gateway_down)}",
        f"health_full={shlex.quote(health_full)}",
    ]
    if agent_token is not None:
        lines.append(f"AGENT_TOKEN={shlex.quote(agent_token)}")
    lines.append(a8)
    lines.append(summary)
    harness = "\n".join(lines)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=30,
    )


def test_summary_names_the_declaration_on_a_named_a8_skip():
    body = json.dumps({"error": "no_eligible_backend", "constraint": "privacy",
                        "role": None, "declaration": "no_role_less_opt_in"}).encode()
    with _stub_server(422, body, extra_headers={"X-SM-Fault-Origin": "gateway"}) as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_and_summary_live(gateway_url=url, health_full=health)
    assert result.returncode == 0
    assert "no_role_less_opt_in" in result.stdout
    # Named in the SUMMARY block itself (after "A8 skipped:" -- the check's
    # own per-check line), not only in the per-check warn() line above it.
    summary_text = result.stdout[result.stdout.index("Postflight passed"):]
    assert "no_role_less_opt_in" in summary_text


def test_summary_plain_pass_unchanged_when_a8_actually_passes():
    with _stub_server(200, b'{"choices":[{"message":{"content":"ok"}}]}') as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_and_summary_live(gateway_url=url, health_full=health)
    assert result.returncode == 0
    assert "Postflight passed (A1" in result.stdout
    # QA LOW L4: scoped to the SUMMARY line itself (like its sibling test
    # above), never the whole combined stdout -- a future line anywhere in
    # the A8 or Summary sections containing "skipped" would break a global
    # assertion spuriously.
    summary_text = result.stdout[result.stdout.index("Postflight passed"):]
    assert "skipped" not in summary_text.lower()


def _afail(result: subprocess.CompletedProcess) -> str:
    m = re.search(r"^AFAIL_A8=(\S*)$", result.stdout, re.M)
    assert m, f"AFAIL_A8 marker missing from stdout:\n{result.stdout}\n{result.stderr}"
    return m.group(1)


def test_a8_gateway_down_fails_and_gates():
    result = run_a8_live(gateway_url="http://127.0.0.1:1", gateway_down="1")
    assert "✗ A8" in result.stdout or "A8 skipped — gateway unreachable" in result.stdout
    assert _afail(result) == "1"


def test_a8_token_missing_skips_without_gating_its_own_section():
    # A8's OWN section only warns on a missing token -- the gate for this
    # case is set at A1 (see test_a1_marks_afail_a8_on_missing_token), never
    # duplicated here.
    result = run_a8_live(gateway_url="http://127.0.0.1:1", token_missing="1")
    assert "A8 skipped" in result.stdout
    assert "AGENT_TOKEN missing" in result.stdout
    assert _afail(result) == "0"


def test_a8_no_backend_at_all_skips_and_never_gates():
    health = json.dumps({"llm_backends": {}})
    result = run_a8_live(gateway_url="http://127.0.0.1:1", health_full=health)
    assert "A8 skipped" in result.stdout
    assert "no reasoning backend reported healthy" in result.stdout
    assert "✗ A8" not in result.stdout
    assert _afail(result) == "0"


def test_a8_backends_present_but_none_healthy_skips_and_never_gates():
    # I-D2, the case the fix round exists for: this is the REALISTIC shape
    # (config always resolves to at least one backend by construction, per
    # a8_backend_info's own fix-round comment) -- backends ARE present, none
    # currently answer "ok". This must SKIP exactly like the empty-map case
    # above, never fail, and the message should show WHY (the actual
    # statuses), matching AGENTS.md Phase 7: "llm":"down" blocks dreaming
    # only, never saves/search -- a healthy LLM-less-right-now install must
    # not fail postflight for it.
    health = json.dumps({"llm_backends": {
        "http://localhost:5000": "down",
        "https://api.deepseek.com": "timeout",
    }})
    result = run_a8_live(gateway_url="http://127.0.0.1:1", health_full=health)
    assert "A8 skipped" in result.stdout
    assert "no reasoning backend reported healthy" in result.stdout
    assert "http://localhost:5000=down" in result.stdout
    assert "https://api.deepseek.com=timeout" in result.stdout
    assert "✗ A8" not in result.stdout
    assert _afail(result) == "0"


def test_a8_real_completion_passes_and_never_gates():
    with _stub_server(200, b'{"choices":[{"message":{"content":"ok"}}]}') as (url, handler):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert "✓" in result.stdout and "A8 real completion returned" in result.stdout
    assert _afail(result) == "0"
    assert handler.seen_path == "/v1/chat/completions"


def test_a8_reasoning_only_completion_passes_end_to_end():
    # ND4: the existing end-to-end stub above returns *content* -- pin the
    # reasoning-only shape through the REAL caller path (curl -> a8_status ->
    # a8_grade_completion), not just the pure function in isolation.
    body = b'{"choices":[{"message":{"content":"","reasoning_content":"thinking..."},"finish_reason":"length"}]}'
    with _stub_server(200, body) as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert "✓" in result.stdout and "A8 real completion returned" in result.stdout
    assert _afail(result) == "0"


def test_a8_200_with_empty_content_fails_and_gates():
    # I-D1: a 200 with empty content is a failure, not a pass. This is
    # exactly the D23 signature the fix round called out: the gateway's own
    # liveness probe says "ok", but the real work path does not work.
    with _stub_server(200, b'{"choices":[{"message":{"content":""}}]}') as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert "✗ A8" in result.stdout
    assert "empty" in result.stdout.lower()
    assert _afail(result) == "1"


def test_a8_404_fails_and_names_the_D23_known_cause():
    # The backend reports "ok" on /health's liveness probe (that's what
    # makes it eligible for A8's real completion at all) while the real
    # proxy path 404s -- exactly the D23 shape.
    with _stub_server(404, b'{"error":"not found"}') as (url, _):
        health = json.dumps({"llm_backends": {"https://provider.example/v1": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert "✗ A8" in result.stdout
    assert "404" in result.stdout
    assert "doubled" in result.stdout
    assert "https://provider.example/v1" in result.stdout  # names the healthy backend
    assert _afail(result) == "1"


def test_a8_422_with_gateway_declaration_skips_not_fails():
    """Ruling B(i) (§6.7), end to end through the SECOND edit site (curl
    header capture, outside the verbatim-extracted A8_GRADE_COMPLETION
    block): a live 422 stamped X-SM-Fault-Origin: gateway with a
    `declaration` key must WARN and pass (afail=0), never FAIL."""
    body = json.dumps({"error": "no_eligible_backend", "constraint": "privacy",
                        "role": None, "declaration": "no_role_less_opt_in"}).encode()
    with _stub_server(422, body, extra_headers={"X-SM-Fault-Origin": "gateway"}) as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert _afail(result) == "0"
    assert "A8 skipped" in result.stdout
    assert "no_role_less_opt_in" in result.stdout
    assert "✗ A8" not in result.stdout


def test_a8_422_upstream_origin_still_fails():
    """The same 422 body, but stamped X-SM-Fault-Origin: upstream — a REAL
    provider refusal passed through must never be misread as the
    documented undeclared-fleet state."""
    body = json.dumps({"error": "no_eligible_backend", "constraint": "privacy",
                        "role": None, "declaration": "no_role_less_opt_in"}).encode()
    with _stub_server(422, body, extra_headers={"X-SM-Fault-Origin": "upstream"}) as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert _afail(result) == "1"
    assert "✗ A8" in result.stdout


def test_a8_422_without_declaration_still_fails():
    """A genuinely-scoped, explicitly-declared fleet's plain 422 (no
    `declaration` key) must stay FATAL — Ruling E(alpha2) arm 1."""
    body = json.dumps({"error": "no_eligible_backend", "constraint": "privacy", "role": None}).encode()
    with _stub_server(422, body, extra_headers={"X-SM-Fault-Origin": "gateway"}) as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert _afail(result) == "1"
    assert "✗ A8" in result.stdout


def test_a8_500_fails_with_status_named():
    with _stub_server(500, b"internal error") as (url, _):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health)
    assert "✗ A8" in result.stdout
    assert "500" in result.stdout
    assert _afail(result) == "1"


def test_a8_connection_refused_fails_as_no_response():
    dead_port = _free_closed_port()
    health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
    result = run_a8_live(gateway_url=f"http://127.0.0.1:{dead_port}", health_full=health,
                          client_timeout="3")
    assert "✗ A8" in result.stdout
    assert "no response" in result.stdout.lower()
    assert _afail(result) == "1"


def test_a8_sends_bearer_token_when_auth_on():
    with _stub_server(200, b'{"choices":[{"message":{"content":"ok"}}]}') as (url, handler):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health, auth_on="1",
                              agent_token="test-token-fixture-value")
    assert _afail(result) == "0"
    assert handler.seen_auth_header == "Bearer test-token-fixture-value"


def test_a8_never_sends_a_bearer_header_when_auth_off():
    with _stub_server(200, b'{"choices":[{"message":{"content":"ok"}}]}') as (url, handler):
        health = json.dumps({"llm_backends": {"http://example:5000": "ok"}})
        result = run_a8_live(gateway_url=url, health_full=health, auth_on="0")
    assert _afail(result) == "0"
    assert handler.seen_auth_header is None


# ── Structural: A8 is wired into the exit-code computation ────────────────

def test_a8_is_in_both_exit_code_loops():
    summary = _extract_summary_section()
    loops = re.findall(r"for a in ([A-Za-z0-9 ]+); do", summary)
    assert len(loops) == 2, (
        f"expected exactly 2 `for a in ...` loops in the Summary section, "
        f"found {len(loops)}: {loops}"
    )
    for loop in loops:
        assert "A8" in loop.split(), (
            f"A8 missing from an exit-code loop: 'for a in {loop}' -- A8 "
            f"would never gate the run at all"
        )


def test_a1_marks_afail_a8_on_missing_token_both_modes():
    text = POSTFLIGHT.read_text()
    # Both branches (re-baseline / canary) of A1's missing-token handling
    # must mark A8, mirroring A5 (which A8 matches: needed in EVERY mode,
    # unlike A4 which re-baseline mode exempts).
    assert "afail[A8]=1" in text
    assert "A5, A6 and A8 are skipped for this same missing token" in text
    assert "A4, A5, A6 and A8 are skipped for this same missing token" in text


def test_a8_no_backend_branch_uses_warn_never_bad():
    a8 = _extract_a8_section()
    # Structural guarantee behind I-D2: the specific line that fires when no
    # backend is reported healthy must call warn(), never bad() -- a
    # mutation here would silently turn a legitimate no-working-LLM install
    # into a failing one.
    m = re.search(r'no reasoning backend reported healthy[^\n]*', a8)
    assert m, "could not find the no-backend-healthy message in the A8 section"
    line_start = a8.rfind("\n", 0, m.start()) + 1
    line = a8[line_start:m.end()]
    assert line.strip().startswith("warn "), (
        f"the no-backend-configured line does not start with warn(): {line!r}"
    )


def test_a8_calls_the_extracted_pure_functions():
    a8 = _extract_a8_section()
    assert "a8_backend_info" in a8
    assert "a8_grade_completion" in a8


def test_a8_exercises_the_real_proxy_path_not_a_bare_probe():
    # I-D3: A8 must POST to the actual chat/completions route (the join
    # D23 broke), not GET /health or /v1/models (the two liveness surfaces
    # that stayed green throughout the D23 incident -- named in this
    # section's own explanatory comment, hence checking the actual curl
    # TARGET rather than banning the substring "/v1/models" outright).
    a8 = _extract_a8_section()
    assert '"$GATEWAY_URL/v1/chat/completions"' in a8
    assert "--data-binary" in a8  # confirms a POST body, not a bare GET
    assert '"$GATEWAY_URL/v1/models"' not in a8
    assert '"$GATEWAY_URL/health"' not in a8
    # Every curl invocation in this section targets the same one route --
    # never a second, different endpoint slipped in alongside it.
    curl_targets = re.findall(r'"\$GATEWAY_URL[^"]*"', a8)
    assert curl_targets, "no $GATEWAY_URL-targeted curl call found in the A8 section"
    assert set(curl_targets) == {'"$GATEWAY_URL/v1/chat/completions"'}


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
