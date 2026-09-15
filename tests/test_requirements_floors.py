"""Verify minimum dependency floors in requirements.txt (Slice 6)."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_requirements_floors():
    req = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "aiohttp>=3.14.3" in req
    assert "fastmcp>=3.4.7" in req
    assert "starlette>=1.6.0" in req
