"""Credential_Custody_Plan, PR A4 (SEC-06) — deployer file-based secrets.

Covers the two NEW ingestion paths secure_env.load_split_env() gained on top
of PR A1's split (plaintext .env vs os.environ):

  1. `<KEY>_FILE` (Docker official-images convention) — read the secret from
     the file the pointer names.
  2. `$CREDENTIALS_DIRECTORY/<key, lowercased>` (systemd LoadCredential=) —
     read the secret from the systemd-managed credentials directory.

And the workstream's standing invariant, EXTENDED to both: no framework
process ever exports a secret into os.environ or a child env — mutation-
checked in this file's own tests (see Local_Documentation/A4_HANDOFF.md for
the manual mutation-check log; the automated tests below assert the same
property directly, which is what a mutation of the guard would break).

Also covers:
  - PRECEDENCE: $CREDENTIALS_DIRECTORY > <KEY>_FILE > .env plaintext value,
    each tier below an operator's direct os.environ export (unchanged from
    PR A1). The test that fails on inversion is
    test_precedence_credentials_directory_beats_file_beats_plaintext below —
    it pins all three tiers present at once and asserts which one wins.
  - SEC-06 (ii): the advisory log line when a known-secret key already sits
    directly in the process's own exec environment.
  - File-read discipline: refuse-vs-warn on missing/unreadable/loose-mode/
    empty secret files (never raises; always falls through instead).
  - Exactly one trailing newline stripped, never a full .strip()/.rstrip().
"""
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import secure_env  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_secure_env_state(monkeypatch):
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    monkeypatch.setattr(secure_env, "_advised_exec_env_names", set())
    yield


@pytest.fixture(autouse=True)
def _isolated_process_env():
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


def _write_env_file(tmp_path, contents: str):
    """Same layout convention as test_secrets_out_of_process_env.py: point
    secure_env's __file__ at shared-memory/scripts/secure_env.py inside
    tmp_path so load_split_env()'s candidate resolution finds it."""
    (tmp_path / "shared-memory").mkdir(exist_ok=True)
    (tmp_path / "shared-memory" / ".env").write_text(contents)
    return tmp_path / "shared-memory" / "scripts" / "secure_env.py"


# ── Tier 3: <KEY>_FILE (Docker official-images convention) ──────────────────

def test_key_file_indirection_resolves_the_secret(monkeypatch, tmp_path):
    secret_file = tmp_path / "pg_password_secret"
    secret_file.write_text("from-the-file\n")
    monkeypatch.setenv("PG_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "from-the-file"
    assert "PG_PASSWORD" not in os.environ


def test_key_file_indirection_works_with_no_env_file_at_all(monkeypatch, tmp_path):
    """A headless systemd deployment may have NO plaintext shared-memory/.env
    at all — LoadCredential=/_FILE alone must still resolve KNOWN_SECRET_NAMES
    (candidate_secret_keys includes the fixed list, not just what raw_pairs
    happens to contain)."""
    secret_file = tmp_path / "neo4j_pw"
    secret_file.write_text("neo-from-file")
    monkeypatch.setenv("NEO4J_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    # No shared-memory/.env written at all under tmp_path.
    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("NEO4J_PASSWORD") == "neo-from-file"
    assert "NEO4J_PASSWORD" not in os.environ


def test_key_file_pointer_itself_may_live_in_the_env_file(monkeypatch, tmp_path):
    """The _FILE pointer's VALUE follows os.environ-first-then-file, same as
    LLM_BACKENDS_JSON — a deployer may set the pointer in shared-memory/.env
    rather than exporting it."""
    secret_file = tmp_path / "tavily_key"
    secret_file.write_text("tvly-from-file")
    fake_file = _write_env_file(
        tmp_path, f"TAVILY_API_KEY_FILE={secret_file}\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("TAVILY_API_KEY") == "tvly-from-file"
    assert "TAVILY_API_KEY" not in os.environ
    # The POINTER itself (a path string) is not secret-shaped (doesn't match
    # any suffix in _SECRET_SUFFIXES) and flows through the ordinary config
    # path into os.environ, same as any other _FILE-suffixed var — only the
    # secret VALUE it points at is withheld. That's the intended split.
    assert os.environ.get("TAVILY_API_KEY_FILE") == str(secret_file)


# ── Tier 2: $CREDENTIALS_DIRECTORY/<key> (systemd LoadCredential=) ──────────

def test_credentials_directory_resolves_the_secret_lowercase_name(monkeypatch, tmp_path):
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "pg_password").write_text("from-load-credential\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)

    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "from-load-credential"
    assert "PG_PASSWORD" not in os.environ


def test_credentials_directory_uppercase_key_name_is_not_matched(monkeypatch, tmp_path):
    """Documents the convention rather than silently accepting either case:
    only the lowercase filename resolves."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "PG_PASSWORD").write_text("wrong-case")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)

    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") is None


# ── Precedence: CREDENTIALS_DIRECTORY > _FILE > .env plaintext ──────────────

def test_precedence_credentials_directory_beats_file_beats_plaintext(monkeypatch, tmp_path):
    """THE test that fails on inversion. All three tiers are present at once
    for the SAME key; only the highest-precedence one may win."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "pg_password").write_text("level2-credentials-directory")

    file_secret = tmp_path / "pg_password_file_secret"
    file_secret.write_text("level3-key-file")

    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=level4-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(file_secret))
    monkeypatch.delenv("PG_PASSWORD", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "level2-credentials-directory"


def test_precedence_file_beats_plaintext_when_no_credentials_directory(monkeypatch, tmp_path):
    file_secret = tmp_path / "pg_password_file_secret"
    file_secret.write_text("level3-key-file")

    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=level4-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("PG_PASSWORD_FILE", str(file_secret))
    monkeypatch.delenv("PG_PASSWORD", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "level3-key-file"


def test_precedence_operator_os_environ_export_beats_every_file_tier(monkeypatch, tmp_path):
    """Tier 1 (an operator's direct os.environ export) still wins over all
    three file-based/plaintext tiers — unchanged since PR A1 review fix #1."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "pg_password").write_text("level2-credentials-directory")
    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=level4-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.setenv("PG_PASSWORD", "level1-operator-export")

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "level1-operator-export"


# ── THE INVARIANT, extended: neither new path ever touches os.environ ───────

def test_neither_new_ingestion_path_ever_exports_to_os_environ(monkeypatch, tmp_path):
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "neo4j_password").write_text("neo-secret")

    file_secret = tmp_path / "tavily_secret_file"
    file_secret.write_text("tavily-secret")

    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.setenv("TAVILY_API_KEY_FILE", str(file_secret))
    for key in ("NEO4J_PASSWORD", "TAVILY_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("NEO4J_PASSWORD") == "neo-secret"
    assert secure_env.get_secret("TAVILY_API_KEY") == "tavily-secret"
    for key in ("NEO4J_PASSWORD", "TAVILY_API_KEY"):
        assert key not in os.environ, f"{key} leaked into os.environ"


def test_daemon_env_still_excludes_file_delivered_secrets(monkeypatch, tmp_path):
    """Closes the loop with hive_mind_proxy._daemon_env(): a secret delivered
    through either new tier must be just as absent from a spawned daemon's
    child environment as a plaintext-.env secret already is (A1's test)."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "neo4j_password").write_text("neo-secret-file-delivered")

    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("AGENT_TOKENS", raising=False)

    secure_env.load_split_env()

    import importlib
    import hive_mind_proxy as g
    importlib.reload(g)
    env = g._daemon_env("consolidation")

    assert "NEO4J_PASSWORD" not in env
    assert "neo-secret-file-delivered" not in env.values()


# ── SEC-06 (ii): advisory on a secret found directly in the exec env ────────

def test_advisory_printed_when_known_secret_is_in_exec_environment(monkeypatch, tmp_path, capsys):
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("PG_PASSWORD", "exported-directly")

    secure_env.load_split_env()

    err = capsys.readouterr().err
    assert "PG_PASSWORD" in err
    assert "ADVISORY" in err
    assert "exported-directly" not in err  # never the value


def test_advisory_not_printed_for_a_key_delivered_only_via_file(monkeypatch, tmp_path, capsys):
    file_secret = tmp_path / "pg_password_file_secret"
    file_secret.write_text("level3-key-file")
    file_secret.chmod(0o600)  # avoid the unrelated loose-mode WARNING, which
    # legitimately mentions "PG_PASSWORD_FILE" in its own text — this test is
    # about the ADVISORY line specifically, not the loose-mode one.
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("PG_PASSWORD_FILE", str(file_secret))
    monkeypatch.delenv("PG_PASSWORD", raising=False)

    secure_env.load_split_env()

    err = capsys.readouterr().err
    assert "ADVISORY: PG_PASSWORD " not in err


def test_advisory_deduplicated_across_repeated_load_split_env_calls(monkeypatch, tmp_path, capsys):
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("PG_PASSWORD", "exported-directly")

    secure_env.load_split_env()
    capsys.readouterr()  # drain the first advisory
    secure_env.load_split_env()

    err = capsys.readouterr().err
    assert "PG_PASSWORD" not in err  # not repeated


# ── File-read discipline: missing / unreadable / loose-mode / empty ─────────

def test_missing_key_file_falls_through_to_plaintext_value(monkeypatch, tmp_path):
    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=fallback-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "fallback-plaintext"


def test_empty_secret_file_treated_as_unset_and_warns(monkeypatch, tmp_path, capsys):
    empty_file = tmp_path / "empty_secret"
    empty_file.write_text("   \n")
    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=fallback-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(empty_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "fallback-plaintext"
    assert "empty" in capsys.readouterr().err.lower()


def test_loose_mode_secret_file_still_read_but_warns(monkeypatch, tmp_path, capsys):
    """Docker secrets are commonly mounted 0444 — this must NOT refuse to
    read, only warn, or the Docker convention item 1 exists to support would
    break on its own reference deployment shape."""
    loose_file = tmp_path / "loose_secret"
    loose_file.write_text("loose-but-readable")
    loose_file.chmod(0o644)
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(loose_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "loose-but-readable"
    err = capsys.readouterr().err
    assert "group/world-accessible" in err


def test_exactly_one_trailing_newline_is_stripped(monkeypatch, tmp_path):
    secret_file = tmp_path / "multi_newline_secret"
    secret_file.write_bytes(b"secret-with-blank-line\n\n")
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    # Exactly ONE trailing newline stripped, not all trailing whitespace —
    # the inner blank line survives.
    assert secure_env.get_secret("PG_PASSWORD") == "secret-with-blank-line\n"


def test_unreadable_secret_file_falls_through_without_raising(monkeypatch, tmp_path):
    """A file that exists but cannot be opened (permission denied at the OS
    level, not just loose mode) must warn and fall through, never raise."""
    unreadable = tmp_path / "unreadable_secret"
    unreadable.write_text("cannot-touch-this")
    unreadable.chmod(0o000)
    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=fallback-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(unreadable))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    try:
        # Root (some CI/dev containers) ignores file mode bits entirely —
        # skip rather than false-fail in that environment.
        if os.access(unreadable, os.R_OK):
            pytest.skip("running as a user that can read 0000 files (e.g. root)")
        secure_env.load_split_env()
        assert secure_env.get_secret("PG_PASSWORD") == "fallback-plaintext"
    finally:
        unreadable.chmod(0o600)
