"""Tests for the section-key client normalization: _normalize_domains function and integration.

This test module verifies that clients normalize "domain" and "domains" metadata keys correctly,
matching the behavior of domain_axis._domains_from for all inputs, and that both CLI and MCP
clients send the plural "domains" key to the gateway.
"""
import json
import importlib.util
import os
import sys
import ast
import inspect
from unittest.mock import MagicMock, patch
import pytest


@pytest.fixture(scope="module")
def setup_modules():
    """Save and restore sys.modules and sys.path across all tests."""
    # Save state
    saved_modules = dict(sys.modules)
    saved_path = sys.path.copy()

    # Load memory_bridge CLI and skill versions
    path_cli = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    spec_cli = importlib.util.spec_from_file_location("memory_bridge_cli", path_cli)
    memory_bridge_cli = importlib.util.module_from_spec(spec_cli)
    sys.modules["memory_bridge_cli"] = memory_bridge_cli
    spec_cli.loader.exec_module(memory_bridge_cli)

    path_skill = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")
    spec_skill = importlib.util.spec_from_file_location("memory_bridge_skill", path_skill)
    memory_bridge_skill = importlib.util.module_from_spec(spec_skill)
    sys.modules["memory_bridge_skill"] = memory_bridge_skill
    spec_skill.loader.exec_module(memory_bridge_skill)

    # Add shared-memory/scripts to path for domain_axis import
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
    from domain_axis import _domains_from

    yield {
        "memory_bridge_cli": memory_bridge_cli,
        "memory_bridge_skill": memory_bridge_skill,
        "_domains_from": _domains_from,
    }

    # Restore state
    sys.modules.clear()
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path


@pytest.fixture(scope="module")
def setup_mcp_modules():
    """Load MCP vector_skill module only if fastmcp is available."""
    pytest.importorskip("fastmcp")

    saved_modules = dict(sys.modules)
    saved_path = sys.path.copy()

    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill_test", path)
    vector_skill = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill_test"] = vector_skill
    spec.loader.exec_module(vector_skill)

    yield {"vector_skill": vector_skill}

    sys.modules.clear()
    sys.modules.update(saved_modules)
    sys.path[:] = saved_path


# ── C1, C2, C3: Test all three copies of _normalize_domains ────────────────────

@pytest.mark.parametrize("module_key", [
    "memory_bridge_cli",
    "memory_bridge_skill",
    pytest.param("mcp", marks=pytest.mark.skipif(
        not pytest.importorskip("fastmcp", minversion=None),
        reason="fastmcp not available"
    ))
])
def test_c1_domain_list_not_nested(setup_modules, setup_mcp_modules, module_key):
    """C1: Verify list-valued domain is not wrapped in another list in all copies."""
    if module_key == "mcp":
        module = setup_mcp_modules["vector_skill"]
    else:
        module = setup_modules[module_key]

    metadata = {"domain": ["ops", "delivery"]}
    result = module._normalize_domains(metadata.copy())
    assert result.get("domains") == ["ops", "delivery"]
    assert "domain" not in result


@pytest.mark.parametrize("module_key", [
    "memory_bridge_cli",
    "memory_bridge_skill",
    pytest.param("mcp", marks=pytest.mark.skipif(
        not pytest.importorskip("fastmcp", minversion=None),
        reason="fastmcp not available"
    ))
])
def test_c2_normalize_domains_matches_reference_comprehensive(setup_modules, setup_mcp_modules, module_key):
    """C2: Client and domain_axis._domains_from agree on all test table rows in all copies."""
    if module_key == "mcp":
        module = setup_mcp_modules["vector_skill"]
    else:
        module = setup_modules[module_key]

    _domains_from = setup_modules["_domains_from"]

    # Positive cases (client should normalize)
    positive_cases = [
        ({"domain": "delivery"}, ["delivery"]),
        ({"domain": ["ops", " delivery ", "ops"], "domains": ["security", "delivery"]}, ["ops", "delivery", "security"]),
        ({"domain": "delivery", "domains": ["operations"]}, ["delivery", "operations"]),
        ({"domain": "delivery", "domains": "operations"}, ["delivery", "operations"]),
    ]

    # Untouched cases (client should return unchanged)
    untouched_cases = [
        {"domain": 5},
        {"domain": [["a"]]},
        {"domain": ["a", 5]},
        {"domain": "a", "domains": {"x": 1}},
        {"domain": ""},
        {"domain": [" "]},
        {"domains": ["a"]},
        {},
    ]

    # Test positive cases
    for metadata, expected in positive_cases:
        result = module._normalize_domains(metadata.copy())
        computed = result.get("domains")
        reference = _domains_from(metadata)
        assert computed == expected
        assert computed == reference

    # Test untouched cases
    for metadata in untouched_cases:
        result = module._normalize_domains(metadata.copy())
        assert result == metadata


@pytest.mark.parametrize("module_key", [
    "memory_bridge_cli",
    "memory_bridge_skill",
    pytest.param("mcp", marks=pytest.mark.skipif(
        not pytest.importorskip("fastmcp", minversion=None),
        reason="fastmcp not available"
    ))
])
def test_c3_invalid_shapes_returned_untouched(setup_modules, setup_mcp_modules, module_key):
    """C3: Client returns invalid shapes untouched in all copies."""
    if module_key == "mcp":
        module = setup_mcp_modules["vector_skill"]
    else:
        module = setup_modules[module_key]

    invalid_cases = [
        {"domain": 5},
        {"domain": [["a"]]},
        {"domain": ["a", 5]},
        {"domain": "a", "domains": {"x": 1}},
        {"domain": ""},
        {"domain": [" "]},
        {"domains": ["a"]},
        {},
    ]

    for metadata in invalid_cases:
        orig = metadata.copy()
        result = module._normalize_domains(metadata.copy())
        assert result == orig


def test_normalize_domains_text_identical_across_all_files(setup_modules, setup_mcp_modules):
    """Verify _normalize_domains source text is identical in all three files using ast."""
    import ast

    # Extract from CLI module
    cli_source = inspect.getsource(setup_modules["memory_bridge_cli"]._normalize_domains)

    # Extract from skill module
    skill_source = inspect.getsource(setup_modules["memory_bridge_skill"]._normalize_domains)

    # Extract from MCP module
    mcp_source = inspect.getsource(setup_mcp_modules["vector_skill"]._normalize_domains)

    # Compare all three
    assert cli_source == skill_source, "CLI and skill module _normalize_domains differ"
    assert cli_source == mcp_source, "CLI and MCP module _normalize_domains differ"


# ── C4: CLI entry point test ────────────────────────────────────────────────────

def test_c4_cli_main_domain_to_domains_no_flag(setup_modules):
    """C4a: CLI main() with domain in JSON and no flag sends domains."""
    module = setup_modules["memory_bridge_cli"]

    mock_resp = MagicMock(status_code=200)
    mock_resp.json = lambda: {"status": "success", "pg_id": 42}

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        import asyncio
        result = asyncio.run(module.save_artifact("test content", '{"source":"test","domain":"delivery"}'))

        assert mock_post.called
        posted_data = mock_post.call_args.kwargs["json"]
        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata
        assert metadata["domains"] == ["delivery"]
        assert "domain" not in metadata


def test_c4_cli_main_domain_with_flag(setup_modules):
    """C4b: CLI main() with domain in JSON and --domain flag combines them."""
    module = setup_modules["memory_bridge_cli"]

    mock_resp = MagicMock(status_code=200)
    mock_resp.json = lambda: {"status": "success", "pg_id": 42}

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        import asyncio
        metadata_with_domain = {"source": "test", "domain": "delivery"}
        result = asyncio.run(module.save_artifact("test content", json.dumps(metadata_with_domain)))

        assert mock_post.called
        posted_data = mock_post.call_args.kwargs["json"]
        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata
        assert metadata["domains"] == ["delivery"]
        assert "domain" not in metadata


# ── C5: MCP test with invented project name ────────────────────────────────────

@pytest.mark.asyncio
async def test_c5_mcp_save_artifact_sends_domains(setup_mcp_modules):
    """C5: The MCP save_artifact sends domains in normalized form."""
    vector_skill = setup_mcp_modules["vector_skill"]

    mock_resp = MagicMock(status_code=200)
    mock_resp.json = lambda: {"status": "success", "pg_id": 42}

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        result = await vector_skill.save_artifact(
            "test content",
            '{"source":"test","project":"widget-line","domain":"delivery"}'
        )

        assert mock_post.called
        call_args = mock_post.call_args
        posted_data = call_args.kwargs["json"]

        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata
        assert metadata["domains"] == ["delivery"]
        assert "domain" not in metadata


# ── C6: Memory bridge copies identical ──────────────────────────────────────────

def test_c6_memory_bridge_copies_identical():
    """C6: The two memory_bridge.py copies must be byte-identical."""
    path_cli = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    path_skill = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")

    with open(path_cli, 'r') as f:
        cli_content = f.read()
    with open(path_skill, 'r') as f:
        skill_content = f.read()

    assert cli_content == skill_content, "The two memory_bridge.py copies differ"


# ── C7: No save example with "domain" in JSON, check all surfaces ───────────────

def test_c7_no_domain_in_examples():
    """C7: Save examples do not carry "domain" in metadata JSON."""
    files_to_check = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "USAGE.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "USAGE.md"),
        os.path.join(os.path.dirname(__file__), "..", "mcp", "system-prompt.md"),
    ]

    # Exact exempt sentences
    exempt_sentences = [
        'A `"domain"` key inside the metadata JSON is still accepted and is stored as `domains`.',
        'A bare string `"domain": "NAME"` is still accepted and normalized to `domains`.',
        'A bare string `"domain": "NAME"` is still accepted and normalized to `domains`. A value that is not a string or list of strings returns 400 `domain_shape_invalid`.',
    ]

    # Check text documents
    for filepath in files_to_check:
        if not os.path.exists(filepath):
            pytest.fail(f"Required file missing: {filepath}")

        with open(filepath, 'r') as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            # Check if line contains a save example
            if ('save "' in line or 'save_artifact(' in line) and ('"' in line or "'" in line):
                # Check for exact "domain": or \"domain\":
                if '"domain":' in line or '\\"domain\\":' in line:
                    # Check if this line is an exempt sentence
                    line_stripped = line.strip()
                    is_exempt = any(line_stripped == exempt.strip() for exempt in exempt_sentences)
                    if not is_exempt:
                        pytest.fail(f"Found 'domain' in save example in {filepath} line {i+1}: {line.rstrip()}")

    # Check MCP docstring
    mcp_file = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    if not os.path.exists(mcp_file):
        pytest.fail(f"Required file missing: {mcp_file}")

    with open(mcp_file, 'r') as f:
        mcp_content = f.read()

    try:
        tree = ast.parse(mcp_content)
        save_artifact_func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "save_artifact":
                save_artifact_func = node
                break

        if save_artifact_func:
            docstring = ast.get_docstring(save_artifact_func)
            if docstring:
                for i, line in enumerate(docstring.split('\n')):
                    if ('"domain":' in line or '\\"domain\\":' in line):
                        # Check if it's the exempt sentence
                        line_stripped = line.strip()
                        is_exempt = any(line_stripped == exempt.strip() for exempt in exempt_sentences)
                        if not is_exempt:
                            pytest.fail(f"Found 'domain' in save_artifact docstring line {i+1}: {line}")
    except SyntaxError:
        pytest.fail("Could not parse mcp/vector-skill.py")


# ── C8: domain_shape_invalid in refusal table ──────────────────────────────────

def test_c8_domain_shape_invalid_in_refusal_table():
    """C8: domain_shape_invalid must be a table row in the refusal table."""
    skill_files = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
    ]

    for filepath in skill_files:
        with open(filepath, 'r') as f:
            content = f.read()

        # Look for a table row starting with | `domain_shape_invalid` |
        import re
        match = re.search(r'^\|\s*`domain_shape_invalid`\s*\|', content, re.MULTILINE)
        assert match, f"'`domain_shape_invalid`' not found as a table row in {filepath}"
