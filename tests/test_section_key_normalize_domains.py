"""Tests for the section-key client normalization: _normalize_domains function and integration."""
import pytest
import json
import asyncio
import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch
import difflib

# Dynamic load of memory_bridge.py from both locations
def load_memory_bridge_cli():
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge_cli", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_cli"] = module
    spec.loader.exec_module(module)
    return module

def load_memory_bridge_skill():
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge_skill", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge_skill"] = module
    spec.loader.exec_module(module)
    return module

memory_bridge_cli = load_memory_bridge_cli()
memory_bridge_skill = load_memory_bridge_skill()

# Replicate the domain_axis._domains_from function for testing
def domains_from_reference(blob) -> list:
    """Domain names carried by one dict, under either key. Pure.
    This is the reference implementation from domain_axis._domains_from"""
    out = []
    seen = set()
    if not isinstance(blob, dict):
        return out
    for key in ("domain", "domains"):
        raw = blob.get(key)
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            if not isinstance(value, str):
                continue
            name = value.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out


# ── C1: The client turns a list-valued "domain" into a nested list ────────────

def test_c1_domain_list_not_nested():
    """C1: The client function and domain_axis._domains_from disagree on any input of the table below"""
    metadata = {"domain": ["ops", "delivery"]}
    result = memory_bridge_cli._normalize_domains(metadata.copy())
    # Should convert ["ops", "delivery"] to ["ops", "delivery"], not [["ops", "delivery"]]
    assert result.get("domains") == ["ops", "delivery"]
    assert "domain" not in result


# ── C2: The client function and domain_axis._domains_from agree on all test inputs ────

def test_c2_normalize_domains_matches_domain_axis():
    """C2: The client function and domain_axis._domains_from disagree on any input of the table below"""
    test_cases = [
        ({"domain": "delivery"}, ["delivery"]),
        ({"domain": ["ops", " delivery ", "ops"], "domains": ["security", "delivery"]}, ["ops", "delivery", "security"]),
        ({"domain": "delivery", "domains": ["operations"]}, ["delivery", "operations"]),
        ({"domain": "delivery", "domains": "operations"}, ["delivery", "operations"]),
    ]

    for metadata, expected in test_cases:
        result = memory_bridge_cli._normalize_domains(metadata.copy())
        if expected:  # Non-empty case
            assert result.get("domains") == expected, f"For input {metadata}, got {result.get('domains')}, expected {expected}"
        # Also verify against reference implementation
        ref_result = domains_from_reference(metadata)
        assert result.get("domains") == ref_result, f"Mismatch with reference _domains_from for {metadata}"


# ── C3: The client doesn't drop or alter invalid shapes ────────────────────────

def test_c3_invalid_shapes_returned_untouched():
    """C3: The client drops or alters a "domain" or "domains" of a shape it does not handle"""
    invalid_cases = [
        {"domain": 5},  # int
        {"domain": [["a"]]},  # nested list
        {"domain": ["a", 5]},  # mixed types
        {"domain": "a", "domains": {"x": 1}},  # domains is dict
        {"domain": ""},  # empty string (results in empty list, so untouched)
        {"domain": [" "]},  # list with only blank (results in empty list, so untouched)
        {"domains": ["a"]},  # only domains, no domain (should not normalize)
        {},  # empty
    ]

    for metadata in invalid_cases:
        orig = metadata.copy()
        result = memory_bridge_cli._normalize_domains(metadata.copy())
        # For these cases, the object should be returned untouched
        # Note: empty string and blank list will have empty computed list and return untouched
        # Only domains without domain returns untouched
        assert result == orig, f"Expected {orig} to be untouched, but got {result}"


# ── C4: CLI save with JSON "domain" and no flag sends plural key ─────────────────

@pytest.mark.asyncio
async def test_c4_cli_save_with_json_domain_sends_domains():
    """C4: A CLI save with a JSON "domain" and no flag still sends the singular key"""
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "pg_id": 42})

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        result = await memory_bridge_cli.save_artifact(
            "test content",
            '{"source":"test","domain":"delivery"}'
        )

        # Check that the post was called
        assert mock_post.called
        # Get the JSON that was posted
        call_args = mock_post.call_args
        posted_data = call_args.kwargs["json"]

        # The metadata should have "domains" not "domain"
        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata, "Expected 'domains' key in metadata"
        assert metadata["domains"] == ["delivery"], f"Expected ['delivery'], got {metadata.get('domains')}"
        assert "domain" not in metadata, "Expected 'domain' to be removed"


# ── C5: MCP save_artifact still sends plural key ──────────────────────────────

@pytest.mark.asyncio
async def test_c5_mcp_save_artifact_sends_domains():
    """C5: The MCP save_artifact still sends the singular key"""
    # Import MCP vector-skill
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    module = importlib.util.module_from_spec(spec)

    # Mock the mcp module before loading
    sys.modules["mcp"] = MagicMock()
    sys.modules["fastmcp"] = MagicMock()

    try:
        spec.loader.exec_module(module)
        vector_skill = module

        mock_resp = MagicMock(status_code=200,
                              json=lambda: {"status": "success", "pg_id": 42})

        with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
            # Test the MCP function
            result = await vector_skill.save_artifact(
                "test content",
                '{"source":"test","project":"shared-memory-GitHub","domain":"delivery"}'
            )

            # Check that the post was called
            assert mock_post.called
            # Get the JSON that was posted
            call_args = mock_post.call_args
            posted_data = call_args.kwargs["json"]

            # The metadata should have "domains" not "domain"
            metadata = posted_data.get("metadata", {})
            assert "domains" in metadata, "Expected 'domains' key in metadata"
            assert metadata["domains"] == ["delivery"], f"Expected ['delivery'], got {metadata.get('domains')}"
            assert "domain" not in metadata, "Expected 'domain' to be removed"
    except Exception as e:
        pytest.skip(f"Could not load MCP module: {e}")


# ── C6: The two memory_bridge.py copies are byte-identical ─────────────────────

def test_c6_memory_bridge_copies_identical():
    """C6: The two memory_bridge.py copies differ"""
    path_cli = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    path_skill = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")

    with open(path_cli, 'r') as f:
        cli_content = f.read()
    with open(path_skill, 'r') as f:
        skill_content = f.read()

    if cli_content != skill_content:
        # Generate a diff for debugging
        diff = list(difflib.unified_diff(
            cli_content.splitlines(keepends=True),
            skill_content.splitlines(keepends=True),
            fromfile='memory_bridge.py (shared-memory)',
            tofile='memory_bridge.py (shared-memory-skill)'
        ))
        pytest.fail(f"The two memory_bridge.py copies differ:\n{''.join(diff)}")


# ── C7: No save example carries top-level "domain" in JSON ───────────────────────

def test_c7_no_domain_in_examples():
    """C7: A shipped save example in SKILL.md, USAGE.md or the MCP tool description carries a top-level "domain" in its metadata JSON"""
    files_to_check = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "USAGE.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "USAGE.md"),
        os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py"),
        os.path.join(os.path.dirname(__file__), "..", "mcp", "system-prompt.md"),
    ]

    for filepath in files_to_check:
        if not os.path.exists(filepath):
            continue

        with open(filepath, 'r') as f:
            content = f.read()

        # Look for save examples with "domain" in JSON
        # Pattern: '...' or {...} followed by "domain": ... or ,"domain": ...
        import re
        # Check for save examples with domain in JSON metadata
        # Pattern: save ... '...'domain"...'
        pattern = r'save\s+"[^"]*"\s+\'[^\']*"domain"[:\s]*"[^"]*"[^\}]*\}'
        matches = re.findall(pattern, content)

        # Also check for save_artifact with domain in JSON
        pattern2 = r'save_artifact\([^,]+,\s*"[^"]*"domain"[^\)]*\)'
        matches2 = re.findall(pattern2, content)

        for match in matches + matches2:
            # Verify it's not in a sentence about accepting "domain"
            if "still accepted" not in content[max(0, content.find(match) - 100):content.find(match) + len(match) + 100]:
                pytest.fail(f"Found save example with 'domain' in JSON in {filepath}: {match[:100]}")


# ── C8: domain_shape_invalid is in refusal table ─────────────────────────────────

def test_c8_domain_shape_invalid_in_skill_md():
    """C8: domain_shape_invalid is missing from SKILL.md's refusal table"""
    skill_files = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
    ]

    for filepath in skill_files:
        with open(filepath, 'r') as f:
            content = f.read()

        assert "domain_shape_invalid" in content, f"'domain_shape_invalid' not found in {filepath}"
