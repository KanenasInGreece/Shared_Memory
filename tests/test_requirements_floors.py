"""Verify minimum dependency floors in requirements.txt (Slice 6)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_requirements_floors():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "aiohttp>=3.14.4" in req
    # 3.14.4 fixes the 2026-10 aiohttp server advisories; the gateway installs from its own file.
    assert "aiohttp>=3.14.4" in (REPO_ROOT / "requirements-gateway.txt").read_text(encoding="utf-8")
    assert "fastmcp>=3.4.7" in req
    assert "starlette>=1.6.0" in req
    # The compromised emSign ECC Root CA - G3 (GHSA-6q48-6c7w-9gw2) is gone from
    # certifi 2026.10.19 on; every install surface carries the floor.
    for name in ("requirements.txt", "requirements-gateway.txt", "requirements-mcp.txt"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "certifi>=2026.10.19" in text, name
