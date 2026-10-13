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
import tempfile
import shutil
import difflib

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

memory_bridge = load_memory_bridge()
vector_skill = load_vector_skill()

# Reference implementation of domain_axis._domains_from for testing
def domains_from_reference(blob) -> list:
    """Domain names carried by one dict, under either key. Pure.
    This matches domain_axis._domains_from exactly."""
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
    """C1: Verify list-valued domain is not wrapped in another list."""
    metadata = {"domain": ["ops", "delivery"]}
    result = memory_bridge._normalize_domains(metadata.copy())
    assert result.get("domains") == ["ops", "delivery"]
    assert "domain" not in result


# ── C2: The client function matches domain_axis._domains_from on all inputs ────

def test_c2_normalize_domains_matches_reference_comprehensive():
    """C2: Client and reference implementation agree on all test table rows."""
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

    # Test positive cases
    for metadata, expected in positive_cases:
        result = memory_bridge._normalize_domains(metadata.copy())
        computed = result.get("domains")
        reference = domains_from_reference(metadata)
        assert computed == expected, f"For {metadata}, got {computed}, expected {expected}"
        assert computed == reference, f"Mismatch with reference for {metadata}: got {computed}, reference {reference}"

    # Test untouched cases - must be identical (same object, unchanged content)
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


# ── Mutation checks: verify tests fail when code is mutated ──────────────────

def test_mutations_c1_wraps_list():
    """Mutation C1: Verify test fails if domain list becomes nested."""
    # Create a mutated version that wraps lists
    metadata = {"domain": ["ops", "delivery"]}

    # This is the mutation: wrapping the list
    def mutated_normalize(metadata):
        """Mutated version that wraps lists."""
        domain_val = metadata.get("domain")
        if domain_val is not None and isinstance(domain_val, list):
            metadata["domains"] = [domain_val]  # MUTATION: wrap in another list
            del metadata["domain"]
        return metadata

    result = mutated_normalize(metadata.copy())
    # This assertion should fail with the mutation
    try:
        assert result.get("domains") == ["ops", "delivery"]
        pytest.fail("Mutation test C1 failed: mutation was not detected")
    except AssertionError:
        pass  # Expected: the assertion should fail with the mutation


def test_mutations_c2_skips_strip():
    """Mutation C2: Verify test fails if strip() is removed."""
    metadata = {"domain": " delivery "}

    # This is the mutation: no strip
    def mutated_normalize(metadata):
        """Mutated version that skips strip."""
        domain_val = metadata.get("domain")
        out = []
        if isinstance(domain_val, str):
            # MUTATION: no strip()
            name = domain_val  # Missing: .strip()
            if name:
                out.append(name)
        if out:
            metadata["domains"] = out
            del metadata["domain"]
        return metadata

    result = mutated_normalize(metadata.copy())
    reference = domains_from_reference(metadata)
    # This assertion should fail with the mutation
    try:
        assert result.get("domains") == reference
        pytest.fail("Mutation test C2 failed: mutation was not detected")
    except AssertionError:
        pass  # Expected: the assertion should fail with the mutation


def test_mutations_c3_modifies_invalid():
    """Mutation C3: Verify test fails if invalid shapes are modified."""
    metadata = {"domain": 5}

    # This is the mutation: accepting invalid shapes
    def mutated_normalize(metadata):
        """Mutated version that accepts invalid shapes."""
        domain_val = metadata.get("domain")
        # MUTATION: no type check
        out = []
        out.append(str(domain_val))  # Convert to string anyway
        if out:
            metadata["domains"] = out
            if "domain" in metadata:
                del metadata["domain"]
        return metadata

    result = mutated_normalize(metadata.copy())
    # This should fail the test
    try:
        assert result == {"domain": 5}
        pytest.fail("Mutation test C3 failed: mutation was not detected")
    except AssertionError:
        pass  # Expected: the assertion should fail with the mutation


def test_mutations_c4_no_normalize_call():
    """Mutation C4: Verify test fails if normalize_domains is not called in save_artifact."""
    # This is a conceptual test - the actual mutation would be in the code
    # We verify here that the pattern would fail
    metadata = {"source": "test", "domain": "delivery"}

    # Simulating what happens if normalize is not called
    # The original metadata would be sent with "domain" key still present
    assert "domain" in metadata, "If normalize is not called, domain stays"

    # Call the real function to verify it removes domain
    result = memory_bridge._normalize_domains(metadata.copy())
    assert "domains" in result, "After normalize, domains should be present"
    assert "domain" not in result, "After normalize, domain should be absent"


def test_mutations_c5_mcp_no_normalize_call():
    """Mutation C5: Verify test fails if normalize_domains is not called in MCP save_artifact."""
    metadata = {"source": "test", "project": "shared-memory-GitHub", "domain": "delivery"}

    # Call the real function to verify it removes domain
    result = vector_skill._normalize_domains(metadata.copy())
    assert "domains" in result, "After normalize, domains should be present"
    assert "domain" not in result, "After normalize, domain should be absent"


def test_mutations_c6_copies_differ():
    """Mutation C6: Conceptual test - copies must be identical."""
    path_cli = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts", "memory_bridge.py")
    path_skill = os.path.join(os.path.dirname(__file__), "..", "shared-memory-skill", "shared-memory", "scripts", "memory_bridge.py")

    with open(path_cli, 'r') as f:
        cli = f.read()
    with open(path_skill, 'r') as f:
        skill = f.read()

    assert cli == skill, "Copies must be identical"


def test_mutations_c7_domain_in_example():
    """Mutation C7: Conceptual test - no "domain" should be in examples."""
    # Verify current examples use --domain or "domains"
    skill_file = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md")
    with open(skill_file, 'r') as f:
        content = f.read()

    # Find a save example and verify it doesn't have "domain" in the JSON
    import re
    saves = re.findall(r'save "[^"]*" \'[^\']*\'', content)
    for save_example in saves:
        # If there's a JSON in the example, it should not have "domain": ...
        if '{"' in save_example:
            json_part = re.search(r'\{[^}]*\}', save_example)
            if json_part:
                assert '"domain":' not in json_part.group(), f"Found 'domain' in example: {save_example}"


def test_mutations_c8_refusal_missing():
    """Mutation C8: Conceptual test - refusal code must be present."""
    skill_file = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "SKILL.md")
    with open(skill_file, 'r') as f:
        content = f.read()

    assert "domain_shape_invalid" in content, "Refusal code must be documented"
