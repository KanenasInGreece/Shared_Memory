"""The MCP connector and the system prompt stay on the same contract as the CLI skill.

vector-skill.py is what an MCP host runs. system-prompt.md is what that host's
model reads. A tool the connector registers but the prompt never calls is
unreachable. A docstring that still teaches a retired write rule is what the
model will follow, because the host does not load SKILL.md.
"""
import ast
import os
import re

_REPO = os.path.join(os.path.dirname(__file__), "..")
_CONNECTOR = os.path.join(_REPO, "mcp", "vector-skill.py")
_PROMPT = os.path.join(_REPO, "mcp", "system-prompt.md")
_COORDINATOR = os.path.join(_REPO, "shared-memory", "scripts", "coordinator.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _is_tool(node):
    for deco in node.decorator_list:
        func = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(func, ast.Attribute) and func.attr == "tool":
            return True
    return False


def _tools(text):
    tree = ast.parse(text)
    return [node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and _is_tool(node)]


def test_connector_version_matches_the_framework():
    connector = _read(_CONNECTOR)
    framework = _read(_COORDINATOR)
    got = re.search(r'^VERSION = "([^"]+)"', connector, re.M).group(1)
    want = re.search(r'^FRAMEWORK_VERSION = "([^"]+)"', framework, re.M).group(1)
    assert got == want, (
        f"vector-skill.py VERSION {got} != FRAMEWORK_VERSION {want} — "
        "an MCP host would report a different release than the gateway"
    )


def test_save_decision_docstring_matches_the_writer():
    text = _read(_CONNECTOR)
    doc = ast.get_docstring(_tools(text)[[n.name for n in _tools(text)].index("save_decision")])
    assert "stores no section" in doc
    assert "does not take the" in doc and "grounding facts' sections" in doc
    assert "Naming none means" not in doc


def test_system_prompt_shows_a_call_for_every_connector_tool():
    names = [n.name for n in _tools(_read(_CONNECTOR))]
    prompt = _read(_PROMPT)
    missing = [name for name in names if f"{name}(" not in prompt]
    assert not missing, (
        f"system-prompt.md names but does not show a call for: {missing}"
    )
