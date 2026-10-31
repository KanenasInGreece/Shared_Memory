"""The installer no longer asks for a role. The marker block must not offer one."""
import re
from pathlib import Path

INSTALL = Path(__file__).parent.parent / "shared-memory" / "ops" / "install_llm_backends.sh"


def test_backend_access_block_does_not_offer_roles():
    text = INSTALL.read_text()
    match = re.search(r"# >>> BACKEND_ACCESS\n(.*)\n# <<< BACKEND_ACCESS", text, re.S)
    assert match
    block = match.group(1)
    assert "ask_backend_roles" not in block
    assert "ROLE_VOCABULARY" not in block
    assert "private_ok" not in block
    assert "n_ctx" in block
    assert "max_output_tokens" in block
