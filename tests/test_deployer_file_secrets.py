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
  - ALL trailing CR/LF stripped (v0.9.63 — the run is a file-write artefact),
    never a full .strip()/.rstrip(): spaces stay. Any OTHER control character
    surviving that normalisation refuses the secret at LOAD, with a warning
    naming the pointer, the path, the byte and its offset — never the value.
"""
import importlib
import os
import signal
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import secure_env  # noqa: E402


class _Timeout(Exception):
    pass


def _with_timeout(seconds, fn, *a, **kw):
    """Run fn under a SIGALRM timeout — used only for the FIFO regression
    test, so a reintroduced R1 hang fails that ONE test loudly instead of
    hanging the whole suite forever (mirrors the Opus probe's own
    `timeout 10` methodology)."""
    def _handler(signum, frame):
        raise _Timeout()
    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        return fn(*a, **kw)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


@pytest.fixture(autouse=True)
def _isolated_secure_env_state(monkeypatch):
    # The suite-wide conftest pins SECURE_ENV_FILE="" (hermeticity: never
    # read the deployer's live .env). THIS file tests the loader itself
    # against env files it constructs via the faked-__file__ candidate walk,
    # so the pin must come off here — the fake walk IS the subject.
    monkeypatch.delenv("SECURE_ENV_FILE", raising=False)
    monkeypatch.setattr(secure_env, "_secrets", {})
    monkeypatch.setattr(secure_env, "_dynamic_secret_names", set())
    monkeypatch.setattr(secure_env, "_advised_exec_env_names", set())
    monkeypatch.setattr(secure_env, "_advised_ignored_file_pointer_names", set())
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


def _resolve_via_file(monkeypatch, tmp_path, raw: bytes, name: str = "secret_file"):
    """Write `raw` verbatim to a tmp file, point PG_PASSWORD_FILE at it, and
    run the loader. Returns what get_secret() resolved to (None = refused/
    unset). Fake values only — never a real-looking provider key."""
    secret_file = tmp_path / name
    secret_file.write_bytes(raw)
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    secure_env.load_split_env()
    return secret_file, secure_env.get_secret("PG_PASSWORD")


@pytest.mark.parametrize("raw", [
    b"sk-test\n",          # every editor / heredoc / `pass show >`
    b"sk-test\r\n",        # a Windows paste, or a CRLF-saving editor
    b"sk-test\n\n",        # `echo` into a file that already ended in \n
    b"sk-test\r\n\r\n",    # both at once — the measured 2026-08-26 shape
    b"sk-test",            # the printf recipe: nothing to strip
])
def test_all_trailing_cr_lf_are_stripped(monkeypatch, tmp_path, raw):
    """v0.9.63: the trailing CR/LF RUN is a file-write artefact, not secret
    content — strip all of it, not exactly one `\\n`. Stripping one left a
    `\\r` in the value, which then reached an Authorization header and made
    aiohttp refuse EVERY upstream request instead of failing once at load."""
    _, value = _resolve_via_file(monkeypatch, tmp_path, raw)
    assert value == "sk-test"


def test_spaces_around_a_secret_are_preserved(monkeypatch, tmp_path):
    """The standing invariant the CR/LF widening must NOT break: a space can
    be part of a literal secret, so this is never `.strip()`/`.rstrip()`.
    Pinned as an explicit VALUE, not an equality between two expressions."""
    _, value = _resolve_via_file(monkeypatch, tmp_path, b" sk-test \n")
    assert value == " sk-test "


def test_an_internal_newline_is_not_a_write_artefact_and_is_refused(
    monkeypatch, tmp_path
):
    """Only the TRAILING run is a write artefact. A newline with content
    AFTER it is corruption (before v0.9.63 the loader carried it silently
    into an Authorization header), so it takes the refusal path instead."""
    _, value = _resolve_via_file(
        monkeypatch, tmp_path, b"line-one\nline-two\n"
    )
    assert value is None


@pytest.mark.parametrize("raw,expect_hex,expect_offset", [
    (b"sk-\rtest\n", "\\x0d", 3),     # embedded CR — the measured failure
    (b"sk-test\t\n", "\\x09", 7),     # trailing TAB: NOT a write artefact
    (b"sk-\x00test\n", "\\x00", 3),   # NUL
    (b"sk-\x1btest\n", "\\x1b", 3),   # ESC
    (b"sk-\x7ftest\n", "\\x7f", 3),   # DEL
])
def test_control_character_refuses_the_secret_and_names_the_file(
    monkeypatch, tmp_path, capsys, raw, expect_hex, expect_offset
):
    """Refuse ONCE at load, with a line an operator can act on: the pointer,
    the path, the byte, its offset, and the fix. Never the secret itself."""
    secret_file, value = _resolve_via_file(monkeypatch, tmp_path, raw)

    assert value is None
    err = capsys.readouterr().err
    assert "PG_PASSWORD_FILE" in err          # names the pointer/source
    assert str(secret_file) in err            # names the FILE
    assert expect_hex in err                  # names the offending byte
    assert f"offset {expect_offset}" in err   # ...and where it is
    assert "printf '%s'" in err               # ...and the recipe
    # No secret content, ever — not the whole value, not any fragment of it.
    assert "sk-test" not in err
    assert "sk-" not in err


def test_control_character_refusal_falls_through_to_the_next_source(
    monkeypatch, tmp_path
):
    """A refused file is 'unset', not 'fatal' — the caller's existing
    fall-through is unchanged (here: the plaintext .env value below it)."""
    secret_file = tmp_path / "corrupt_secret"
    secret_file.write_bytes(b"sk-\rtest\n")
    fake_file = _write_env_file(tmp_path, "PG_PASSWORD=fallback-plaintext\n")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("PG_PASSWORD_FILE", str(secret_file))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") == "fallback-plaintext"


def test_credentials_directory_refuses_control_characters_identically(
    monkeypatch, tmp_path, capsys
):
    """One reader, one rule: `$CREDENTIALS_DIRECTORY` and `<KEY>_FILE` must
    not diverge — a fix applied to one path only would leave the systemd
    deployment shape carrying the defect."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "pg_password").write_bytes(b"sk-\rtest\n")
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("PG_PASSWORD", raising=False)
    monkeypatch.delenv("PG_PASSWORD_FILE", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("PG_PASSWORD") is None
    err = capsys.readouterr().err
    assert "$CREDENTIALS_DIRECTORY" in err
    assert "\\x0d" in err
    assert "sk-" not in err


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


# ── R1 (fix round 1): fd-safe file type + size guard, probe-confirmed ───────

def test_fifo_does_not_hang_and_is_refused(tmp_path):
    """The Opus probe: `timeout 10` against `_read_secret_file()` on a
    scratch FIFO exited 124 (still blocked) before this fix. Guarded here
    with a SIGALRM so a regression fails loudly instead of hanging the
    suite."""
    fifo_path = tmp_path / "pg_password_fifo"
    os.mkfifo(fifo_path)
    try:
        result = _with_timeout(5, secure_env._read_secret_file, fifo_path, source="TEST_FIFO")
    except _Timeout:
        pytest.fail("_read_secret_file() hung on a FIFO — R1 regression")
    assert result is None


def test_character_device_is_refused_not_read_unbounded():
    """The Opus probe: /dev/zero read unbounded into memory, exiting 124
    after only the loose-mode warning. Now refused before any read call."""
    dev_zero = Path("/dev/zero")
    if not dev_zero.exists():
        pytest.skip("/dev/zero not present on this platform")
    result = _with_timeout(5, secure_env._read_secret_file, dev_zero, source="TEST_DEV_ZERO")
    assert result is None


def test_directory_is_refused(tmp_path):
    result = secure_env._read_secret_file(tmp_path, source="TEST_DIR")
    assert result is None


def test_oversized_secret_file_is_refused_and_warns(tmp_path, capsys):
    """NEW-3 (fix round 2): over-cap is now decided from st.st_size FIRST,
    before any read — this file is refused on that path, not the length-
    based backstop (see test_oversized_by_st_size_never_reads_a_byte and
    test_backstop_length_check_catches_a_lying_st_size below for each path
    tested in isolation)."""
    big = tmp_path / "big_secret"
    big.write_bytes(b"x" * (secure_env._SECRET_FILE_MAX_BYTES + 1))
    result = secure_env._read_secret_file(big, source="TEST_BIG")
    assert result is None
    err = capsys.readouterr().err
    assert "over the" in err and "byte cap" in err


# ── NEW-3 (fix round 2, probe-confirmed reasoning): over-cap decided from ───
# st.st_size (the primary check, before any read) rather than from
# len(os.read(...)) alone (a single read() may legitimately return fewer
# bytes than requested — a short first read on a genuinely over-cap file
# would have been silently accepted as the whole, truncated secret).

def test_oversized_by_st_size_never_reads_a_byte(monkeypatch, tmp_path):
    """The st_size check must refuse BEFORE any os.read() call — mock
    os.read to raise if it's ever invoked, so this test fails loudly if the
    primary check regresses to reading first."""
    big = tmp_path / "big_secret_never_read"
    big.write_bytes(b"x" * (secure_env._SECRET_FILE_MAX_BYTES + 1))

    def _read_should_not_be_called(fd, n):
        raise AssertionError("os.read() was called — st_size check did not refuse first")

    monkeypatch.setattr(os, "read", _read_should_not_be_called)

    result = secure_env._read_secret_file(big, source="TEST_NEVER_READ")
    assert result is None


def test_backstop_length_check_catches_a_lying_st_size(monkeypatch, tmp_path):
    """The rare case st_size does NOT reflect the true readable content
    (mocked here, since a real procfs-style pseudo-file isn't portably
    reproducible in a test) — the length-based backstop after the read loop
    must still catch it."""
    big = tmp_path / "lying_st_size_secret"
    big.write_bytes(b"x" * (secure_env._SECRET_FILE_MAX_BYTES + 1))

    real_fstat = os.fstat

    def _fake_fstat(fd):
        st = real_fstat(fd)
        import types
        return types.SimpleNamespace(st_mode=st.st_mode, st_size=0)  # lies: reports empty

    monkeypatch.setattr(os, "fstat", _fake_fstat)

    result = secure_env._read_secret_file(big, source="TEST_LYING_SIZE")
    assert result is None


def test_short_reads_are_not_mistaken_for_a_truncated_secret(monkeypatch, tmp_path):
    """A single os.read() call returning fewer bytes than requested (a
    signal, a network filesystem) must not be accepted as the whole file —
    the read loop must keep reading until EOF. Mocks os.read to return the
    content one byte at a time, well under the cap, and confirms the FULL
    content survives rather than being truncated to whatever the first
    short read happened to return."""
    secret_content = "full-secret-value-not-truncated"
    f = tmp_path / "short_read_secret"
    f.write_text(secret_content)

    real_read = os.read
    call_count = {"n": 0}

    def _one_byte_at_a_time(fd, n):
        call_count["n"] += 1
        return real_read(fd, 1)  # always returns at most 1 byte, regardless of n

    monkeypatch.setattr(os, "read", _one_byte_at_a_time)

    result = secure_env._read_secret_file(f, source="TEST_SHORT_READS")
    assert result == secret_content
    assert call_count["n"] > 1  # confirms the loop actually iterated


def test_secret_file_at_exactly_the_cap_is_accepted(tmp_path):
    """The cap is inclusive — a file of EXACTLY _SECRET_FILE_MAX_BYTES bytes
    is a legitimate secret, not an over-cap one. The +1 read only ever
    detects going PAST the cap."""
    exact = tmp_path / "exact_secret"
    content = "x" * secure_env._SECRET_FILE_MAX_BYTES
    exact.write_text(content)
    result = secure_env._read_secret_file(exact, source="TEST_EXACT")
    assert result == content


def test_secret_file_max_bytes_env_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("SECURE_ENV_SECRET_FILE_MAX_BYTES", "10")
    importlib.reload(secure_env)
    try:
        small = tmp_path / "small_cap_secret"
        small.write_text("12345678901")  # 11 bytes, over the 10-byte cap
        assert secure_env._read_secret_file(small, source="TEST_CAP") is None
    finally:
        monkeypatch.delenv("SECURE_ENV_SECRET_FILE_MAX_BYTES", raising=False)
        importlib.reload(secure_env)


def test_symlink_to_regular_file_is_still_followed_no_nofollow(tmp_path):
    """Deliberately NO O_NOFOLLOW — Kubernetes mounts a Secret as a symlink
    chain through an atomically-swapped ..data directory (its rotation
    mechanism); this shape must keep resolving."""
    real_dir = tmp_path / "..2024_01_01_00_00_00.000000000"
    real_dir.mkdir()
    real_secret = real_dir / "pg_password"
    real_secret.write_text("k8s-style-secret")
    data_link = tmp_path / "..data"
    data_link.symlink_to(real_dir, target_is_directory=True)
    mount_point = tmp_path / "pg_password"
    mount_point.symlink_to(Path("..data") / "pg_password")

    result = secure_env._read_secret_file(mount_point, source="TEST_K8S_SYMLINK")
    assert result == "k8s-style-secret"


def test_s_isreg_guard_isolated_via_mocked_fstat(monkeypatch, tmp_path):
    """Isolates the S_ISREG check from the size-cap/OSError paths the
    FIFO/device/directory tests above can incidentally trip too (a FIFO with
    no writer raises EAGAIN; a directory raises EISDIR on read(); both
    return None via a DIFFERENT branch even without the type check). A REAL
    regular file, well under the cap, WOULD read fine and return its content
    if S_ISREG were removed — mock os.fstat to report a non-regular mode on
    the same fd so only that one guard is exercised."""
    f = tmp_path / "small_secret_but_wrong_type"
    f.write_text("should-never-surface")

    import types
    real_fstat = os.fstat

    def _fake_fstat(fd):
        st = real_fstat(fd)
        fake_mode = stat.S_IFCHR | stat.S_IMODE(st.st_mode)
        return types.SimpleNamespace(st_mode=fake_mode)

    monkeypatch.setattr(os, "fstat", _fake_fstat)

    result = secure_env._read_secret_file(f, source="TEST_MOCKED_TYPE")
    assert result is None


# ── R4 / QF-3 (fix round 1): candidate keys derived from the pointers ───────

def test_file_derived_candidate_with_no_env_at_all(monkeypatch, tmp_path):
    """AGENT_TOKEN_FILE alone, with NO shared-memory/.env, NO KNOWN_SECRET_NAMES
    membership required beforehand (AGENT_TOKEN now IS on that list too, but
    this specifically proves the DERIVATION path — a name outside the fixed
    list, e.g. a provider key, must also resolve). Opus probe-confirmed this
    resolved to None before the fix."""
    secret_file = tmp_path / "deepseek_key_secret"
    secret_file.write_text("sk-derived-from-file-pointer")
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(secret_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("DEEPSEEK_API_KEY") == "sk-derived-from-file-pointer"
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_agent_token_file_resolves_with_no_env_at_all(monkeypatch, tmp_path):
    """The specific casualty Opus named: AGENT_TOKEN is never written to
    shared-memory/.env by design, so it depended entirely on this fix."""
    secret_file = tmp_path / "agent_token_secret"
    secret_file.write_text("tok_from_file_delivery")
    monkeypatch.setenv("AGENT_TOKEN_FILE", str(secret_file))
    monkeypatch.delenv("AGENT_TOKEN", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("AGENT_TOKEN") == "tok_from_file_delivery"
    assert "AGENT_TOKEN" not in os.environ


def test_creddir_derived_candidate_outside_known_names(monkeypatch, tmp_path):
    """$CREDENTIALS_DIRECTORY containing an entry for a key outside
    KNOWN_SECRET_NAMES (e.g. a provider key) must still resolve — proves the
    _derive_credentials_directory_candidates() path specifically."""
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "deepseek_api_key").write_text("sk-from-creddir")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY_FILE", raising=False)

    fake_file = tmp_path / "shared-memory" / "scripts" / "secure_env.py"
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))

    secure_env.load_split_env()

    assert secure_env.get_secret("DEEPSEEK_API_KEY") == "sk-from-creddir"
    assert "DEEPSEEK_API_KEY" not in os.environ


def test_non_secret_key_file_pointer_is_ignored_and_warns(monkeypatch, tmp_path, capsys):
    """A _FILE pointer for a NON-secret key (e.g. a typo'd config var), set
    in the .env FILE, must be ignored, not silently treated as a secret,
    and must warn — the pointer is a line in shared-memory/.env, addressed
    to this framework (NEW-1: the source-split condition for the warning)."""
    pointless_file = tmp_path / "not_a_secret"
    pointless_file.write_text("irrelevant")
    fake_file = _write_env_file(
        tmp_path, f"EMBEDDER_URL_FILE={pointless_file}\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("EMBEDDER_URL_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("EMBEDDER_URL") is None
    err = capsys.readouterr().err
    assert "EMBEDDER_URL_FILE" in err
    assert "not classified as a secret" in err


# ── NEW-1 (fix round 2, Opus review, probe-confirmed): the warning above ────
# fired for EVERY unrelated ambient env var ending in _FILE (SSL_CERT_FILE,
# GIT_INDEX_FILE probe-confirmed live), un-deduplicated, on every
# load_split_env() call. Candidates are still DERIVED from both os.environ
# and the .env file; only the WARNING is now restricted to the .env file
# source, and de-duplicated per process.

def test_ambient_non_secret_file_pointer_produces_no_warning(monkeypatch, tmp_path, capsys):
    """An unrelated ambient env var ending in _FILE (SSL_CERT_FILE is the
    live probe case — every Python process using `requests`/`httpx` with a
    custom CA bundle can be carrying this) is not this framework's business
    and must never warn, no matter how many times load_split_env() runs."""
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt")
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()
    secure_env.load_split_env()  # twice — must still never warn

    err = capsys.readouterr().err
    assert "SSL_CERT_FILE" not in err


def test_file_sourced_non_secret_pointer_warns_exactly_once_across_two_calls(
    monkeypatch, tmp_path, capsys
):
    """A non-secret _FILE pointer that IS a line in shared-memory/.env still
    warns (it's addressed to this framework) — but only ONCE across
    multiple load_split_env() calls in the same process, mirroring
    _advised_exec_env_names' own de-duplication."""
    pointless_file = tmp_path / "not_a_secret_2"
    pointless_file.write_text("irrelevant")
    fake_file = _write_env_file(
        tmp_path, f"EMBEDDER_URL_FILE={pointless_file}\n"
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.delenv("EMBEDDER_URL_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()
    capsys.readouterr()  # drain the first warning
    secure_env.load_split_env()

    err = capsys.readouterr().err
    assert "EMBEDDER_URL_FILE" not in err  # not repeated on the second call


def test_ambient_pointer_for_a_secret_key_still_resolves_despite_no_warning(
    monkeypatch, tmp_path
):
    """The other half of NEW-1's split: an operator's own ambient export of
    a SECRET-shaped _FILE pointer must still resolve the secret — only the
    WARNING is restricted to the .env-file source, never the candidate
    derivation itself."""
    secret_file = tmp_path / "ambient_secret_file"
    secret_file.write_text("ambient-exported-secret")
    fake_file = _write_env_file(tmp_path, "")
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("DEEPSEEK_API_KEY_FILE", str(secret_file))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret("DEEPSEEK_API_KEY") == "ambient-exported-secret"


def test_hostile_key_name_traversal_is_skipped_via_credentials_directory(monkeypatch, tmp_path):
    """O7: a hostile candidate key (path-traversal-shaped) must never become
    a path component. A REAL file is planted exactly ONE level above
    cred_dir (matching the traversal depth) so that, if the O7 guard were
    ever removed, this test would observe the LEAKED content instead of
    None — not merely a path that happens not to exist."""
    outside_target = tmp_path / "outside_secret"
    outside_target.write_text("should-never-be-read")
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))

    hostile_key = f"../{outside_target.name}"
    result = secure_env._credentials_directory_secret(hostile_key)
    assert result is None


def test_hostile_key_name_traversal_is_skipped_via_file_indirection(monkeypatch, tmp_path):
    """O7: same gate, the other path-building function. The _FILE pointer
    for the hostile key name is wired to a REAL file (a real shell couldn't
    name an env var this way, but LLM_BACKENDS_JSON's token_env is arbitrary
    JSON string content flowing through as a Python string, exactly this
    shape) so a removed guard would return its content, not None by luck."""
    outside_target = tmp_path / "outside_secret_via_file"
    outside_target.write_text("should-never-be-read-via-file")
    hostile_key = "../../etc/passwd"
    monkeypatch.setenv(f"{hostile_key}_FILE", str(outside_target))

    result = secure_env._file_indirection_secret(hostile_key, {})
    assert result is None


def test_hostile_token_env_name_never_reaches_the_filesystem(monkeypatch, tmp_path, capsys):
    """End-to-end: a hostile token_env name from LLM_BACKENDS_JSON is
    classified secret (SEC-09's third clause) and therefore lands in
    candidate_secret_keys, but the O7 gate refuses it before it becomes a
    path — proven by planting a file at the WOULD-BE traversal target and
    confirming it never gets read."""
    import json as _json

    outside_target = tmp_path / "outside_credentials_directory_secret"
    outside_target.write_text("should-never-be-read")

    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    hostile_name = f"../{outside_target.name}"

    fake_file = _write_env_file(
        tmp_path,
        'LLM_BACKENDS_JSON=' + _json.dumps([
            {"url": "https://x", "token_env": hostile_name}
        ]) + "\n",
    )
    monkeypatch.setattr(secure_env, "__file__", str(fake_file))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    monkeypatch.delenv(hostile_name, raising=False)

    secure_env.load_split_env()

    assert secure_env.get_secret(hostile_name) is None
    err = capsys.readouterr().err
    assert "safe-name check" in err
