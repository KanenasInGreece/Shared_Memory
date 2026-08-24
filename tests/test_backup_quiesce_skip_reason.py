"""ops/backup.sh — quiesce() names WHICH of three reasons it skipped
(measured on a documented upgrade run).

Before this, "proceeding WITHOUT quiesce (gateway unreachable or no admin
token) — online dumps, restore self-heals" folded two structurally different
situations into one string: an operator reading it afterward could not tell
whether they had no token at all, a token that was rejected/lacked the role,
or a genuinely unreachable gateway — each needs a different fix.

Only the deterministic quiesce() function is testable here. It is embedded in
shared-memory/ops/backup.sh (between the `# >>> QUIESCE_FN` / `# <<<
QUIESCE_FN` markers) rather than duplicated: this file extracts that block
VERBATIM and runs it standalone via subprocess, with `qcurl` stubbed to
control the HTTP response — so the test exercises the actual shipped
source, never a hand-written reimplementation that could silently drift
from it.
"""
import re
import subprocess
from pathlib import Path

BACKUP_SH = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "backup.sh"
)

BEGIN_MARKER = "# >>> QUIESCE_FN"
END_MARKER = "# <<< QUIESCE_FN"


def _extract_quiesce_source() -> str:
    text = BACKUP_SH.read_text()
    pattern = re.escape(BEGIN_MARKER) + r"\n(.*?\n)" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{BACKUP_SH} -- the extraction markers moved or were removed"
    )
    return m.group(1)


def test_markers_present_exactly_once():
    text = BACKUP_SH.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_quiesce_source()
    assert "quiesce()" in source


# qcurl stub: prints "<body>\n<http_code>" the same shape backup.sh's own
# `qcurl -w $'\n%{http_code}' ...` produces, so tail -n1 / sed '$d' inside
# quiesce() parse it exactly the way they parse a real curl response.
# `__CURL_FAIL__` as the code means "the curl process itself failed"
# (connection refused / DNS / timeout) -- returns nonzero instead of printing.
_QCURL_STUB = '''
qcurl() {
  if [[ "$FAKE_HTTP_CODE" == "__CURL_FAIL__" ]]; then
    return 7
  fi
  printf '%s\\n%s' "$FAKE_BODY" "$FAKE_HTTP_CODE"
}
grn() { :; }
ylw() { :; }
json_get() { echo ""; }
'''


def _run(env_extra: dict) -> subprocess.CompletedProcess:
    """Run quiesce() standalone with the given extra shell vars exported,
    then print rc/QUIESCED/QUIESCE_MODE/QUIESCE_SKIP_REASON so the test can
    assert on them without needing the rest of backup.sh's machinery."""
    source = _extract_quiesce_source()
    script = _QCURL_STUB + "\n" + source + '''
QUIESCED=0
QUIESCE_MODE=""
QUIESCE_SKIP_REASON=""
GATEWAY_URL="${GATEWAY_URL:-http://gateway.example:8888}"
BACKUP_QUIESCE_MAX_SECONDS="${BACKUP_QUIESCE_MAX_SECONDS:-120}"
_AUTH_HEADER_FILE=/dev/null
quiesce
rc=$?
echo "RC=$rc"
echo "QUIESCED=$QUIESCED"
echo "QUIESCE_MODE=$QUIESCE_MODE"
echo "QUIESCE_SKIP_REASON=$QUIESCE_SKIP_REASON"
'''
    env = {"PATH": "/usr/bin:/bin", **env_extra}
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, timeout=15, env=env,
    )


def _field(stdout: str, name: str) -> str:
    m = re.search(rf"^{name}=(.*)$", stdout, re.M)
    assert m, f"{name}= not found in stdout:\n{stdout}"
    return m.group(1)


def test_no_admin_token_names_that_reason():
    proc = _run({"BACKUP_ADMIN_TOKEN": ""})
    assert _field(proc.stdout, "RC") == "1"
    assert _field(proc.stdout, "QUIESCED") == "0"
    reason = _field(proc.stdout, "QUIESCE_SKIP_REASON")
    assert "no admin token" in reason
    assert "BACKUP_ADMIN_TOKEN" in reason


def test_gateway_unreachable_names_that_reason_distinct_from_no_token():
    proc = _run({
        "BACKUP_ADMIN_TOKEN": "tok_x",
        "FAKE_HTTP_CODE": "__CURL_FAIL__",
        "FAKE_BODY": "",
    })
    assert _field(proc.stdout, "RC") == "1"
    reason = _field(proc.stdout, "QUIESCE_SKIP_REASON")
    assert "unreachable" in reason
    assert "no admin token" not in reason
    assert "gateway.example:8888" in reason


def test_token_rejected_401_names_that_reason():
    proc = _run({
        "BACKUP_ADMIN_TOKEN": "tok_x",
        "FAKE_HTTP_CODE": "401",
        "FAKE_BODY": "{}",
    })
    assert _field(proc.stdout, "RC") == "1"
    reason = _field(proc.stdout, "QUIESCE_SKIP_REASON")
    assert "401" in reason
    assert "rejected" in reason
    assert "unreachable" not in reason
    assert "no admin token" not in reason


def test_token_lacks_role_403_names_that_reason_distinct_from_others():
    proc = _run({
        "BACKUP_ADMIN_TOKEN": "tok_x",
        "FAKE_HTTP_CODE": "403",
        "FAKE_BODY": "{}",
    })
    assert _field(proc.stdout, "RC") == "1"
    reason = _field(proc.stdout, "QUIESCE_SKIP_REASON")
    assert "403" in reason
    assert "lacks the admin role" in reason
    assert "unreachable" not in reason
    assert "rejected" not in reason  # distinct wording from the 401 case


def test_successful_quiesce_200_sets_full_and_no_skip_reason():
    proc = _run({
        "BACKUP_ADMIN_TOKEN": "tok_x",
        "FAKE_HTTP_CODE": "200",
        "FAKE_BODY": "{}",
    })
    assert _field(proc.stdout, "RC") == "0"
    assert _field(proc.stdout, "QUIESCED") == "1"
    assert _field(proc.stdout, "QUIESCE_MODE") == "full"
    assert _field(proc.stdout, "QUIESCE_SKIP_REASON") == ""


def test_drain_timeout_202_sets_timeout_mode():
    proc = _run({
        "BACKUP_ADMIN_TOKEN": "tok_x",
        "FAKE_HTTP_CODE": "202",
        "FAKE_BODY": '{"daemons":"drain_timeout"}',
    })
    assert _field(proc.stdout, "RC") == "0"
    assert _field(proc.stdout, "QUIESCED") == "1"
    assert _field(proc.stdout, "QUIESCE_MODE") == "timeout"
