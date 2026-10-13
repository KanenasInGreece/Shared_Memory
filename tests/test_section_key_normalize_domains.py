"""Tests for the section-key client normalization: _normalize_domains function and integration.

This test module verifies that clients normalize "domain" and "domains" metadata keys correctly,
matching the behavior of domain_axis._domains_from for all inputs, and that both CLI and MCP
clients send the plural "domains" key to the gateway.
"""
import pytest
pytest.importorskip("fastmcp")

import json
import asyncio
import importlib.util
import os
import sys
import httpx
from unittest.mock import MagicMock, patch, AsyncMock

# Load memory_bridge exactly as test_memory_bridge.py does
def load_memory_bridge():
    path = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")
    spec = importlib.util.spec_from_file_location("memory_bridge", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_bridge"] = module
    spec.loader.exec_module(module)
    return module

# Load vector_skill exactly as test_vector_skill.py does
def load_vector_skill():
    path = os.path.join(os.path.dirname(__file__), "..", "mcp", "vector-skill.py")
    spec = importlib.util.spec_from_file_location("vector_skill", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["vector_skill"] = module
    spec.loader.exec_module(module)
    return module

# Import the real domain_axis._domains_from for comparison
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from domain_axis import _domains_from

memory_bridge = load_memory_bridge()
vector_skill = load_vector_skill()


# ── C1: The client turns a list-valued "domain" into a nested list ────────────

def test_c1_domain_list_not_nested():
    """C1: Verify list-valued domain is not wrapped in another list."""
    metadata = {"domain": ["ops", "delivery"]}
    result = memory_bridge._normalize_domains(metadata.copy())
    assert result.get("domains") == ["ops", "delivery"]
    assert "domain" not in result


# ── C2: The client function matches domain_axis._domains_from on all inputs ────

def test_c2_normalize_domains_matches_reference_comprehensive():
    """C2: Client and domain_axis._domains_from agree on all test table rows."""
    # Positive cases (client should normalize)
    positive_cases = [
        ({"domain": "delivery"}, ["delivery"]),
        ({"domain": ["ops", " delivery ", "ops"], "domains": ["security", "delivery"]}, ["ops", "delivery", "security"]),
        ({"domain": "delivery", "domains": ["operations"]}, ["delivery", "operations"]),
        ({"domain": "delivery", "domains": "operations"}, ["delivery", "operations"]),
    ]

    # Untouched cases (client should return unchanged)
    untouched_cases = [
        {"domain": 5},  # int
        {"domain": [["a"]]},  # nested list
        {"domain": ["a", 5]},  # mixed types
        {"domain": "a", "domains": {"x": 1}},  # domains is dict
        {"domain": ""},  # empty string (computed list is empty)
        {"domain": [" "]},  # list with only blank
        {"domains": ["a"]},  # only domains, no domain
        {},  # empty
    ]

    # Test positive cases - verify client computes same list as domain_axis
    for metadata, expected in positive_cases:
        result = memory_bridge._normalize_domains(metadata.copy())
        computed = result.get("domains")
        reference = _domains_from(metadata)
        assert computed == expected, f"For {metadata}, got {computed}, expected {expected}"
        assert computed == reference, f"Mismatch with _domains_from for {metadata}: got {computed}, reference {reference}"

    # Test untouched cases - verify they return unchanged
    for metadata in untouched_cases:
        result = memory_bridge._normalize_domains(metadata.copy())
        # For untouched cases, the result should be unchanged
        assert result == metadata, f"Expected {metadata} to be untouched, but got {result}"


# ── C3: The client doesn't drop or alter invalid shapes ────────────────────────

def test_c3_invalid_shapes_returned_untouched():
    """C3: Client returns invalid shapes untouched."""
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
        result = memory_bridge._normalize_domains(metadata.copy())
        assert result == orig, f"Expected {orig} to be untouched, but got {result}"


# ── C4: CLI save with JSON "domain" sends plural key ─────────────────────────

@pytest.mark.asyncio
async def test_c4_cli_save_with_json_domain_sends_domains():
    """C4: A CLI save with a JSON "domain" normalizes to "domains" before posting."""
    mock_resp = MagicMock(status_code=200,
                          json=lambda: {"status": "success", "pg_id": 42})

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        result = await memory_bridge.save_artifact(
            "test content",
            '{"source":"test","domain":"delivery"}'
        )

        # Verify the POST was called
        assert mock_post.called
        # Get the JSON that was posted
        call_args = mock_post.call_args
        posted_data = call_args.kwargs["json"]

        # The metadata should have "domains" not "domain"
        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata, "Expected 'domains' key in metadata"
        assert metadata["domains"] == ["delivery"], f"Expected ['delivery'], got {metadata.get('domains')}"
        assert "domain" not in metadata, "Expected 'domain' to be removed"


# ── C5: MCP save_artifact sends plural key (using proper loader) ──────────────

@pytest.mark.asyncio
async def test_c5_mcp_save_artifact_sends_domains():
    """C5: The MCP save_artifact sends domains in normalized form."""
    # Create a mock response exactly like test_vector_skill.py does
    mock_resp = MagicMock(status_code=200)
    mock_resp.json = lambda: {"status": "success", "pg_id": 42}

    with patch("httpx.AsyncClient.post", return_value=mock_resp) as mock_post:
        result = await vector_skill.save_artifact(
            "test content",
            '{"source":"test","project":"shared-memory-GitHub","domain":"delivery"}'
        )

        # Verify the POST was called
        assert mock_post.called
        # Get the JSON that was posted
        call_args = mock_post.call_args
        posted_data = call_args.kwargs["json"]

        # The metadata should have "domains" not "domain"
        metadata = posted_data.get("metadata", {})
        assert "domains" in metadata, "Expected 'domains' key in metadata"
        assert metadata["domains"] == ["delivery"], f"Expected ['delivery'], got {metadata.get('domains')}"
        assert "domain" not in metadata, "Expected 'domain' to be removed"


# ── C6: The two memory_bridge.py copies are byte-identical ─────────────────────

def test_c6_memory_bridge_copies_identical():
    """C6: The two memory_bridge.py copies must be identical."""
    path_cli = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    path_skill = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")

    with open(path_cli, 'r') as f:
        cli_content = f.read()
    with open(path_skill, 'r') as f:
        skill_content = f.read()

    assert cli_content == skill_content, "The two memory_bridge.py copies differ"


# ── C7: No save example carries top-level "domain" in JSON ───────────────────

def test_c7_no_domain_in_examples():
    """C7: Save examples do not carry top-level "domain" in metadata JSON."""
    files_to_check = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "USAGE.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "USAGE.md"),
    ]

    import re
    for filepath in files_to_check:
        if not os.path.exists(filepath):
            continue

        with open(filepath, 'r') as f:
            content = f.read()

        # Look for save examples with "domain": ... in JSON (but not in sentences about accepting it)
        lines = content.split('\n')
        for i, line in enumerate(lines):
            # Look for JSON with "domain": in save commands
            if 'save ' in line and '"domain":' in line:
                # Check if this is a commented explanation about accepting domain
                context = '\n'.join(lines[max(0, i-2):min(len(lines), i+3)])
                if 'still accepted' not in context and 'JSON' not in context:
                    pytest.fail(f"Found save example with 'domain' in JSON in {filepath} line {i+1}: {line}")


# ── C8: domain_shape_invalid is in refusal table ──────────────────────────────

def test_c8_domain_shape_invalid_in_skill_md():
    """C8: domain_shape_invalid refusal code is documented."""
    skill_files = [
        os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md"),
        os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "SKILL.md"),
    ]

    for filepath in skill_files:
        with open(filepath, 'r') as f:
            content = f.read()

        assert "domain_shape_invalid" in content, f"'domain_shape_invalid' not found in {filepath}"
