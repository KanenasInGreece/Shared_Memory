"""Unit tests for client-side project derivation and the domain-key chain.

Two linked fixes:

* The canonical project is the PROJECT FOLDER NAME, so a project's tag is
  identical across every session on it regardless of which agent wrote the
  record. The gateway cannot derive this (a server never sees a client's working
  directory), so the client does it. Previously nothing derived it at all: the
  gateway only NORMALISED a value an agent had already supplied, which left
  records saved without the field untagged forever.

* The consolidation domain key must not fall back through `scope`, which is an
  access-control axis rather than a topical one.

No DB, Neo4j or gateway required.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
import memory_bridge
from memory_bridge import derive_project


@pytest.fixture(autouse=True)
def _clear_override(monkeypatch):
    monkeypatch.delenv("SHARED_MEMORY_PROJECT", raising=False)


def _project(tmp_path, name, marker=".git"):
    root = tmp_path / name
    root.mkdir()
    (root / marker).mkdir() if marker == ".git" else (root / marker).write_text("x")
    return root


# ── the walk ────────────────────────────────────────────────────────────────

def test_derives_folder_name_at_project_root(tmp_path, monkeypatch):
    root = _project(tmp_path, "my-project")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert derive_project(str(root)) == "my-project"


def test_walks_up_from_a_subdirectory(tmp_path, monkeypatch):
    """The whole reason for a walk: a save from <project>/tests must tag the
    PROJECT, not the subdirectory. A bare cwd basename would tag 'tests' and
    scatter one project's facts across as many tags as it has folders."""
    root = _project(tmp_path, "my-project")
    deep = root / "shared-memory" / "scripts"
    deep.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert derive_project(str(deep)) == "my-project"


def test_nearest_marker_wins_for_a_nested_project(tmp_path, monkeypatch):
    outer = _project(tmp_path, "workspace")
    inner = outer / "inner-project"
    inner.mkdir()
    (inner / "CLAUDE.md").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert derive_project(str(inner)) == "inner-project"


def test_non_git_marker_is_honoured(tmp_path, monkeypatch):
    root = _project(tmp_path, "docs-project", marker="AGENTS.md")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert derive_project(str(root)) == "docs-project"


# ── the guards: an empty tag beats a confidently wrong one ──────────────────

def test_unmarked_directory_derives_nothing(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    assert derive_project(str(scratch)) == ""


def test_home_is_a_boundary_not_a_project(tmp_path, monkeypatch):
    """$HOME commonly holds a CLAUDE.md; without this guard every stray save from
    the home directory would be tagged with the account name."""
    home = tmp_path / "user"
    home.mkdir()
    (home / "CLAUDE.md").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    assert derive_project(str(home)) == ""


def test_env_override_wins(tmp_path, monkeypatch):
    root = _project(tmp_path, "my-project")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SHARED_MEMORY_PROJECT", "explicit-name")
    assert derive_project(str(root)) == "explicit-name"


# ── the fill: gap-filling, never overriding ─────────────────────────────────

@pytest.mark.asyncio
async def test_save_fills_missing_project(tmp_path, monkeypatch):
    root = _project(tmp_path, "derived-project")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root)
    sent = {}

    class _Resp:
        status_code = 200
        def json(self): return {"status": "success", "pg_id": 1}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(memory_bridge, "_async_client", lambda *_a, **_k: _Client())
    await memory_bridge.save_artifact("content", '{"source":"test"}')
    assert sent["metadata"]["project"] == "derived-project"


@pytest.mark.asyncio
async def test_save_never_overrides_an_explicit_project(tmp_path, monkeypatch):
    root = _project(tmp_path, "derived-project")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(root)
    sent = {}

    class _Resp:
        status_code = 200
        def json(self): return {"status": "success", "pg_id": 1}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            sent.update(json or {})
            return _Resp()

    monkeypatch.setattr(memory_bridge, "_async_client", lambda *_a, **_k: _Client())
    await memory_bridge.save_artifact("content", '{"source":"test","project":"chosen"}')
    assert sent["metadata"]["project"] == "chosen"


# ── the domain key must not fall back through an access-control axis ────────

def test_domain_key_chain_excludes_scope():
    """`scope` pairs with visibility='scope' on the read path. Keying summaries
    through it partitions them by who may SEE a record instead of what it is
    ABOUT — invisible here because our own scope column is constant, but wrong on
    any deployment that uses scopes."""
    import inspect
    import consolidation_loop
    import coordinator

    for mod in (consolidation_loop, coordinator):
        src = inspect.getsource(mod)
        for line in src.splitlines():
            if "metadata->>'domain'" in line or "metadata->>'project'" in line:
                assert "scope" not in line, (
                    f"{mod.__name__}: domain-key chain still falls back through "
                    f"scope: {line.strip()}"
                )
