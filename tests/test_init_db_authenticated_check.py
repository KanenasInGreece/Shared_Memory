"""init_db.sh — authenticated_connectivity_check() (corpus fact:1515, F7/F8).

MEASURED, on a live host: a fresh install's compose recreate found a STALE
POSTGRES CLUSTER left behind by a failed uninstall ("Skipping initialization"
in the container log confirms the image never re-read POSTGRES_PASSWORD),
carrying a PREVIOUS cluster's password that the new .env cannot authenticate
against. init_db.sh reported everything green anyway (F8, High), because
every Postgres step it runs -- pg_isready, schema_init.sql, the ledger
queries -- goes through `docker exec ... psql -U postgres` with NO `-h`,
which Postgres's pg_hba.conf routes through the Unix-socket `local ... trust`
line: no password is ever checked. The mismatch stayed invisible until the
gateway crash-looped on asyncpg.InvalidPasswordError, minutes or days later
and in a different process entirely.

THE FIX: one explicit AUTHENTICATED check per store, after all schema and
constraint work, over the SAME password-checked path the gateway itself will
use. For Postgres that means forcing the TCP path (`-h 127.0.0.1`, which
pg_hba.conf routes through the image's real password method) instead of the
peer-trust socket every other call in this script uses -- still via
`docker exec` (no psql/psycopg2/uv needed on the host), just with the one
flag that changes which pg_hba.conf line applies. Neo4j's cypher-shell was
already authenticating with the real NEO4J_PASSWORD throughout, so a stale
Neo4j data directory was never actually silent -- the check below just makes
that verification explicit and gives it the same wording as Postgres's,
rather than leaving it as a side effect of unrelated work.

A failure names the likely origin plainly ("this data directory pre-existed
this install") and points at the uninstall/backup paths -- never at editing
the .env, which would be treating a data-directory problem as a credentials
problem.

HOW THIS IS TESTED. Per this repo's rule, no docker command may run against
the live system. `pg_authenticated_check()`, `neo4j_authenticated_check()`
and `authenticated_connectivity_check()` are lifted out between the
`# >>> AUTHENTICATED_CONNECTIVITY_CHECK` / `# <<< ...` markers -- the same
idiom test_install_service_linger.py and test_init_db_ledger_adoption.py
use -- and run standalone with a PATH-stubbed `docker` that answers the two
underlying queries by env var. Nothing real is ever touched.
"""
import re
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
INIT_DB = REPO_ROOT / "shared-memory" / "scripts" / "init_db.sh"

BEGIN_MARKER = "# >>> AUTHENTICATED_CONNECTIVITY_CHECK"
END_MARKER = "# <<< AUTHENTICATED_CONNECTIVITY_CHECK"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_source() -> str:
    text = INIT_DB.read_text()
    pattern = re.escape(BEGIN_MARKER) + r"\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{INIT_DB} -- the extraction markers moved or were removed"
    )
    return m.group(1)


_COLOR_HELPERS = (
    "red() { printf '\\033[31m%s\\033[0m\\n' \"$*\"; }\n"
    "grn() { printf '\\033[32m%s\\033[0m\\n' \"$*\"; }\n"
    "ylw() { printf '\\033[33m%s\\033[0m\\n' \"$*\"; }\n"
)


def _docker_stub_body(bash_bin: str) -> str:
    # Answers the two authenticated queries by env var and logs every call.
    # $PG_AUTH_RC / $NEO4J_AUTH_RC control the (simulated) auth outcome.
    return (
        f"#!{bash_bin}\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case "$*" in\n'
        '    *"SELECT 1"*) exit "${PG_AUTH_RC:-0}" ;;\n'
        '    *"RETURN 1"*) exit "${NEO4J_AUTH_RC:-0}" ;;\n'
        "    *) exit 0 ;;\n"
        "esac\n"
    )


def _run(tmp_path: Path, script_suffix: str, env_overrides: dict = None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "invocations.log"
    log_path.write_text("")

    bash_bin = shutil.which("bash")
    assert bash_bin, "bash not found on the harness's own PATH"

    docker_stub = bin_dir / "docker"
    docker_stub.write_text(_docker_stub_body(bash_bin))
    docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC)

    source = _extract_source()
    script = (
        _COLOR_HELPERS
        + 'PG_CONTAINER="postgres-vector"\n'
        + 'PG_DB="agent_data"\n'
        + 'PG_PASSWORD="the-real-pg-password"\n'
        + 'NEO4J_CONTAINER="neo4j-memory"\n'
        + 'NEO4J_PASSWORD="the-real-neo4j-password"\n'
        + source
        + "\n" + script_suffix
    )

    import os
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "DOCKER_LOG": str(log_path),
        "HOME": os.environ.get("HOME", str(tmp_path)),
    }
    env.update(env_overrides or {})

    proc = subprocess.run(
        [bash_bin, "-c", script],
        capture_output=True, text=True, timeout=15, env=env,
    )
    return proc, log_path


def test_markers_present_exactly_once():
    text = INIT_DB.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_all_three_functions():
    source = _extract_source()
    assert "pg_authenticated_check()" in source
    assert "neo4j_authenticated_check()" in source
    assert "authenticated_connectivity_check()" in source


# ── Postgres ─────────────────────────────────────────────────────────────

def test_pg_check_success_reports_authenticated(tmp_path):
    proc, log_path = _run(
        tmp_path, 'authenticated_connectivity_check "Postgres" pg_authenticated_check\n',
        env_overrides={"PG_AUTH_RC": "0"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "authenticated with this .env's credentials" in out
    # Pinned by argv: the TCP path, not the peer-trust socket every other
    # call in this script uses.
    assert "-h 127.0.0.1" in log_text, f"the check must force TCP auth:\n{log_text}"
    assert "SELECT 1" in log_text


def test_pg_check_failure_fails_loudly_with_the_stale_cluster_wording(tmp_path):
    """THE pinned wording -- the exact phrase the build brief specifies, so
    an operator sees the LIKELY cause, not a bare auth error."""
    proc, log_path = _run(
        tmp_path, 'authenticated_connectivity_check "Postgres" pg_authenticated_check\n',
        env_overrides={"PG_AUTH_RC": "1"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "this data directory pre-existed this install" in out
    assert "a previous" in out and "credentials are still in force" in out
    # Must not suggest editing the .env -- it's a data-directory problem.
    assert "not a credentials problem" in out
    assert "uninstall_framework.sh --level data" in out
    assert "authenticated with this .env's credentials" not in out


def test_pg_check_uses_pgpassword_not_the_scripts_own_var_name(tmp_path):
    """psql's client reads PGPASSWORD, not PG_PASSWORD -- confirm the docker
    exec actually carries the right env var name, not just any password."""
    proc, log_path = _run(
        tmp_path, 'authenticated_connectivity_check "Postgres" pg_authenticated_check\n',
        env_overrides={"PG_AUTH_RC": "0"},
    )
    log_text = log_path.read_text()
    assert "-e PGPASSWORD" in log_text, log_text


# ── Neo4j ────────────────────────────────────────────────────────────────

def test_neo4j_check_success_reports_authenticated(tmp_path):
    proc, log_path = _run(
        tmp_path, 'authenticated_connectivity_check "Neo4j" neo4j_authenticated_check\n',
        env_overrides={"NEO4J_AUTH_RC": "0"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "authenticated with this .env's credentials" in out
    assert "RETURN 1" in log_text
    assert "-e NEO4J_PASSWORD" in log_text


def test_neo4j_check_failure_uses_the_same_wording_as_postgres(tmp_path):
    proc, _log = _run(
        tmp_path, 'authenticated_connectivity_check "Neo4j" neo4j_authenticated_check\n',
        env_overrides={"NEO4J_AUTH_RC": "1"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "this data directory pre-existed this install" in out
    assert "Neo4j" in out


# ── Composition: both checks run, and a single failure fails the pair ──────

def test_one_store_failing_still_reports_that_store_by_name(tmp_path):
    proc, _log = _run(
        tmp_path,
        'authenticated_connectivity_check "Postgres" pg_authenticated_check\n'
        'authenticated_connectivity_check "Neo4j" neo4j_authenticated_check\n',
        env_overrides={"PG_AUTH_RC": "1", "NEO4J_AUTH_RC": "0"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert "Postgres REFUSED" in out
    assert "Neo4j authenticated with this .env's credentials" in out


# ── Structural: init_db.sh actually CALLS both checks, in the right place ──

def test_both_checks_are_invoked_after_neo4j_constraints_and_gate_the_final_message():
    """The extraction proves the functions behave correctly; this proves
    init_db.sh actually reaches them, in order, and that a failure prevents
    the final "Both stores initialised" line -- not just that the functions
    exist unreferenced."""
    text = INIT_DB.read_text()

    neo4j_ok_idx = text.index('grn "✓ Neo4j constraints applied"')
    pg_call_idx = text.index(
        'authenticated_connectivity_check "Postgres" pg_authenticated_check')
    neo4j_call_idx = text.index(
        'authenticated_connectivity_check "Neo4j"    neo4j_authenticated_check')
    exit_idx = text.index('if [[ "$_auth_failures" -gt 0 ]]', pg_call_idx)
    final_idx = text.index('Both stores initialised')

    assert neo4j_ok_idx < pg_call_idx < neo4j_call_idx < exit_idx < final_idx, (
        "the authenticated checks must run after schema/constraint work and "
        "gate the final success message, in that order"
    )


def test_pg_password_is_read_and_required_like_neo4j_password():
    """PG_PASSWORD must actually be read from .env and refused if empty --
    otherwise pg_authenticated_check() would silently authenticate with an
    empty password rather than genuinely testing this .env's credential."""
    text = INIT_DB.read_text()
    assert 'PG_PASSWORD="$(read_env PG_PASSWORD)"' in text
    assert 'PG_PASSWORD not set in .env' in text
