"""Credential_Custody_Plan, PR A4, item 7 — SEC-05-class sweep of one-off
scripts.

`sync_project_registry.py:53-67` (and eleven siblings) had a module-level
`_load_env()` that dumped EVERY key from shared-memory/.env into os.environ
via `setdefault` — AGENT_TOKEN included — the exact class PR A1 closed for
the three long-running daemons. All twelve now delegate their `_load_env()`
to `secure_env.load_split_env()` / `secure_env.get_secret()` instead, so a
secret-classified key never lands in os.environ regardless of which
standalone script parses it.

`backfill_project_of.py` (fix round 1, item 9(a); Opus O4) was a thirteenth,
different-shaped case: it had NO `.env`-parsing loader at all — it read
NEO4J_PASSWORD/PG_PASSWORD via a bare `os.environ.get(...)`, so it never
itself leaked a secret into os.environ, but it also got none of the
file-based delivery its twelve siblings gained in this same PR, and it kept
teaching the deprecated "export a secret in your shell" pattern. It now
gets its own `_load_env()` delegating to `secure_env.load_split_env()`
too, joining the structural check below.

Two layers of coverage:

  1. STRUCTURAL (all thirteen) — the fix is "delegate to secure_env", so the
     regression this guards against is someone re-inlining a hand-rolled
     `os.environ.setdefault(key, val)` loop over parsed .env lines, OR (for
     backfill_project_of.py specifically) a direct os.environ.get() of a
     secret name with no loader at all. A static source check catches this
     cheaply across all thirteen without importing each one (several have
     real import-time side effects / hard dependencies that make importing
     all of them in one process fragile).
  2. BEHAVIOURAL (sync_project_registry.py, the file item 7 names
     explicitly, as one representative) — actually imports the module with a
     fake shared-memory/.env and asserts PG_PASSWORD never reaches
     os.environ, mirroring test_secrets_out_of_process_env.py's own
     invariant test for the daemons.

The mutation check for the underlying guard itself
(secure_env.load_split_env() never exporting a secret to os.environ) is
already covered exhaustively by test_secrets_out_of_process_env.py and
test_deployer_file_secrets.py (see Local_Documentation/A4_HANDOFF.md for the
manual mutation-check log). What this file adds is delegation coverage: that
these twelve callers actually reach that guard rather than re-implementing
their own.
"""
import importlib
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")

SWEPT_SCRIPTS = [
    "backfill_domain_of.py",
    "backfill_project_of.py",
    "backfill_promote_grounded.py",
    "cleanup_entity_noise.py",
    "cleanup_foreign_schema.py",
    "entity_fragment_rate.py",
    "migrate_retro_edges.py",
    "normalize_projects.py",
    "reconcile_project_edges.py",
    "reconcile_project_identity.py",
    "relation_sweep.py",
    "resolve_references.py",
    "sync_project_registry.py",
]

# The defect's SHAPE (CLAUDE.md: a fixture states the form, never the
# instance) — a hand-rolled loop that setdefaults a parsed .env line straight
# into os.environ with no secret classification at all.
_HAND_ROLLED_LEAK_PATTERN = re.compile(
    r"os\.environ\.setdefault\(\s*key(?:\.strip\(\))?\s*,\s*val(?:\.strip\(\))?\s*\)"
)


@pytest.mark.parametrize("filename", SWEPT_SCRIPTS)
def test_load_env_delegates_to_secure_env(filename):
    """Structural: _load_env() calls secure_env.load_split_env() and the
    hand-rolled leak pattern is gone from the file entirely."""
    path = os.path.join(SCRIPTS_DIR, filename)
    src = open(path).read()
    assert "secure_env.load_split_env()" in src, (
        f"{filename}: _load_env() no longer delegates to secure_env's split loader"
    )
    assert not _HAND_ROLLED_LEAK_PATTERN.search(src), (
        f"{filename}: a hand-rolled os.environ.setdefault(key, val) loop is back — "
        f"this is the exact SEC-05 defect class the sweep removed"
    )


@pytest.mark.parametrize("filename", SWEPT_SCRIPTS)
def test_no_direct_secret_os_environ_get(filename):
    """Structural: no remaining direct os.environ.get(...) read of a known
    secret name — everything must route through secure_env.get_secret()."""
    path = os.path.join(SCRIPTS_DIR, filename)
    src = open(path).read()
    leak = re.search(
        r'os\.environ\.get\(\s*["\'](PG_PASSWORD|NEO4J_PASSWORD|PG_CONN|AGENT_TOKEN|'
        r'AGENT_TOKENS|TAVILY_API_KEY|BACKUP_ADMIN_TOKEN)["\']',
        src,
    )
    assert leak is None, f"{filename}: direct os.environ.get() read of {leak.group(1) if leak else ''}"


# ── Behavioural: sync_project_registry.py, named explicitly in item 7 ───────

@pytest.fixture(autouse=True)
def _isolated_secure_env_state(monkeypatch):
    import secure_env
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    monkeypatch.setattr(secure_env, "_advised_exec_env_names", set())
    monkeypatch.setattr(secure_env, "_advised_ignored_file_pointer_names", set())


@pytest.fixture(autouse=True)
def _isolated_process_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def test_sync_project_registry_never_leaks_pg_password_to_os_environ(monkeypatch, tmp_path):
    # Un-pin the suite-wide SECURE_ENV_FILE="" hermeticity guard: this test
    # exercises the faked-__file__ candidate walk against its own env file.
    monkeypatch.delenv("SECURE_ENV_FILE", raising=False)
    (tmp_path / "shared-memory").mkdir()
    (tmp_path / "shared-memory" / ".env").write_text(
        "PG_PASSWORD=super-secret-pg\n"
        "AGENT_TOKENS=claude:tok_abc\n"
        "PG_USER=postgres\n"
    )
    fake_secure_env_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"

    import secure_env
    monkeypatch.setattr(secure_env, "__file__", str(fake_secure_env_file))
    for key in ("PG_PASSWORD", "AGENT_TOKENS", "PG_USER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    import sync_project_registry as spr
    importlib.reload(spr)
    try:
        assert "PG_PASSWORD" not in os.environ, "PG_PASSWORD leaked into os.environ"
        assert "AGENT_TOKENS" not in os.environ, "AGENT_TOKENS leaked into os.environ"
        assert secure_env.get_secret("PG_PASSWORD") == "super-secret-pg"
        assert "super-secret-pg" in spr.PG_CONN  # DSN still built correctly
    finally:
        # Leave the module in a state that won't poison later tests importing it.
        monkeypatch.delenv("PG_PASSWORD", raising=False)
