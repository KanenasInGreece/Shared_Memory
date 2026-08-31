"""install_llm_backends.sh must never drift from the gateway's own role
vocabulary (W0 item ①, T2).

The installer's role-elicitation prompt (ask_backend_roles(), between the
`# >>> BACKEND_ACCESS` / `# <<< BACKEND_ACCESS` markers) hand-lists the
roles it is willing to write into a `roles: [...]` entry. The single source
of truth for that vocabulary is hive_mind_proxy.ROUTING_ROLE_NAMES --
"summarize" is RESERVED_ROLE_NAMES there and fatal if a backend ever
offered it. This test pins the installer's `# ROLE_VOCABULARY:` comment
line against the real module constants, via a plain import (no gateway
startup -- require_valid_llm_routing_config()'s refusals are deferred to
main(), so importing hive_mind_proxy is always safe in tests).
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

INSTALL_LLM_BACKENDS = (
    Path(__file__).parent.parent / "shared-memory" / "ops" / "install_llm_backends.sh"
)

VOCAB_LINE_RE = re.compile(r"^#\s*ROLE_VOCABULARY:\s*(.+)$", re.MULTILINE)


def _installer_role_vocabulary() -> set[str]:
    text = INSTALL_LLM_BACKENDS.read_text()
    m = VOCAB_LINE_RE.search(text)
    assert m, "no '# ROLE_VOCABULARY: ...' line found in install_llm_backends.sh"
    return set(m.group(1).split())


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
