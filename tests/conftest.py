"""Session-wide safety net (security review R-1, PR A3 fix round).

No test in this suite may ever write to the real ``$HOME``. An autouse,
function-scoped fixture points ``CREDENTIAL_AUDIT_LOG_PATH`` at a per-test
``tmp_path`` before every single test runs — so a test that forgets to manage
the variable explicitly, or one that reloads ``coordinator`` after deleting
it (re-arming the module-level default), still lands somewhere disposable
rather than in the operator's real credential-audit log.

This is a BACKSTOP, not the only line of defence: ``tests/test_credential_
audit_trail.py``'s own ``load_coordinator()`` also defaults to an explicit
disabled state rather than "pop the variable", and ``tests/test_llm_fault_
origin.py``'s isolation fixture resets the shared ``coordinator`` module's
writer at teardown regardless of what an individual test did to it. Belt and
braces: any one of the three would have caught the contamination the review
found (138 synthetic lines in the real ``~/.shared-memory/logs/credential-
audit.jsonl`` from a suite run with no explicit protection at all).
"""
import pytest


@pytest.fixture(autouse=True)
def _credential_audit_log_path_never_touches_real_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(tmp_path / "credential-audit.jsonl"))
    yield
