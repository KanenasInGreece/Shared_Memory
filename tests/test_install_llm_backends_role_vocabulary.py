"""install_llm_backends.sh must never drift from the gateway's own role
vocabulary (W0 item ①, T2).

The installer's role-elicitation prompt (ask_backend_roles(), between the
`# >>> BACKEND_ACCESS` / `# <<< BACKEND_ACCESS` markers) hand-lists the
roles it is willing to write into a `roles: [...]` entry. The single source
of truth for that vocabulary is hive_mind_proxy.ROUTING_ROLE_NAMES --
"summarize" is RESERVED_ROLE_NAMES there and fatal if a backend ever
offered it.

M4 (fix round, QA review): a comment-line pin alone proves the DOCUMENTED
vocabulary matches the gateway's -- it says nothing about whether the
script's actual validation ARMS (the code that accepts or rejects an
answer) agree with that comment. This file now does both: the comment-pin
tests below, plus tests that extract the real `# >>> BACKEND_ACCESS` block
(same technique as tests/test_install_llm_backends.py) and DRIVE
ask_backend_roles() directly -- extract/judge must be ACCEPTED,
summarize/garbage must be REJECTED (re-ask) -- against the shipped source,
never a reimplementation that could silently drift from it.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

INSTALL_LLM_BACKENDS = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "install_llm_backends.sh"
)

VOCAB_LINE_RE = re.compile(r"^#\s*ROLE_VOCABULARY:\s*(.+)$", re.MULTILINE)
BEGIN_MARKER = "# >>> BACKEND_ACCESS"
END_MARKER = "# <<< BACKEND_ACCESS"


def _installer_role_vocabulary() -> set[str]:
    text = INSTALL_LLM_BACKENDS.read_text()
    m = VOCAB_LINE_RE.search(text)
    assert m, "no '# ROLE_VOCABULARY: ...' line found in install_llm_backends.sh"
    return set(m.group(1).split())


def _extract_backend_access_block() -> str:
    text = INSTALL_LLM_BACKENDS.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, f"could not find a {BEGIN_MARKER} ... {END_MARKER} block"
    return m.group(1)


def _run_ask_backend_roles(stdin_text: str, timeout: float = 15) -> subprocess.CompletedProcess:
    """Drive the REAL ask_backend_roles() (defined inside the extracted
    block) standalone -- role-value cases only, no ENV_FILE/token_env
    plumbing needed since this function doesn't touch either."""
    script = "set -euo pipefail\n" + _extract_backend_access_block() + "\nask_backend_roles"
    return subprocess.run(
        ["bash", "-c", script],
        input=stdin_text, capture_output=True, text=True, timeout=timeout,
    )


pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="jq not installed (this script's own hard prerequisite)"
)


def test_vocabulary_line_present_exactly_once():
    text = INSTALL_LLM_BACKENDS.read_text()
    assert len(VOCAB_LINE_RE.findall(text)) == 1


def test_installer_vocabulary_equals_routing_role_names():
    import hive_mind_proxy as proxy

    assert _installer_role_vocabulary() == set(proxy.ROUTING_ROLE_NAMES)


def test_installer_vocabulary_disjoint_from_reserved_role_names():
    import hive_mind_proxy as proxy

    assert _installer_role_vocabulary().isdisjoint(proxy.RESERVED_ROLE_NAMES)


def test_summarize_never_offered_by_the_installer():
    assert "summarize" not in _installer_role_vocabulary()


# ---------------------------------------------------------------------------
# M4: drive the ACTUAL validation arms, not just the comment.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", ["extract", "judge", "extract judge", "EXTRACT"])
def test_valid_vocabulary_members_accepted(answer):
    proc = _run_ask_backend_roles(answer + "\n")
    assert proc.returncode == 0, proc.stderr
    accepted = set(proc.stdout.split())
    assert accepted, f"empty acceptance for a valid answer: {answer!r}"
    assert accepted <= {"extract", "judge"}


@pytest.mark.parametrize("answer", ["summarize", "garbage", "summarize garbage"])
def test_reserved_and_garbage_rejected_then_recovers(answer):
    """A rejected answer must re-ask (never silently accept it, never crash)
    -- proven by pairing it with a valid follow-up and confirming the loop
    actually reached that second line."""
    proc = _run_ask_backend_roles(f"{answer}\njudge\n")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "judge"
    assert "extract" in proc.stderr and "judge" in proc.stderr  # the re-ask guidance
