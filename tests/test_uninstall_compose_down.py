"""uninstall_framework.sh — compose_down_and_verify() (corpus fact:1515, F3/F5/F4).

MEASURED, on a live host, as one causal cascade:

  F3 (Critical) `docker compose ... down -v` ran WITHOUT `--env-file`. The
     compose file requires NEO4J_HOST_DIR / PG_DATA_DIR to interpolate at all
     (`${VAR:?set ... in shared-memory/.env}`), so config parsing itself
     failed before docker touched a single container -- `down` never ran.
  F5 (High)     The invocation was `docker compose ... down -v 2>&1 | tail -3`
     -- the pipe discards `down`'s exit code, so the script printed
     "compose stack down, volumes removed" UNCONDITIONALLY and exited 0.
  F4 (Critical) Result: the very next block deleted the data directories out
     from under four still-`restart: always` containers that were, in fact,
     still running.

THE FIX, all three in one function (`compose_down_and_verify()`, between the
`# >>> COMPOSE_DOWN_AND_VERIFY` / `# <<< COMPOSE_DOWN_AND_VERIFY` markers):

  1. Always pass `--env-file $ENV_FILE` when it exists (mirrors the install
     side's own invocation shape). When it does not (a re-run after a
     partial uninstall already removed it), fall back to explicit dummy
     values for ONLY the two required interpolation keys -- `down -v` never
     mounts or reads them, and this compose file declares no top-level
     `volumes:` at all (every volume is a bind mount), so the dummy values
     exist solely to satisfy compose's config parser.
  2. The exit code is CHECKED -- never piped away again.
  3. A post-condition: after a 0 exit, `docker ps -a` is checked against the
     compose file's own `container_name:` list. Compose exiting 0 is not, by
     itself, proof the containers are gone.

HOW THIS IS TESTED. Per this repo's own rule, no docker command may run
against the live system, and uninstall levels are exercised on a sacrificial
host, never in this suite (see test_uninstall_guards.py's own structural
self-check). So `compose_down_and_verify()` is lifted out between its
markers and run standalone -- the same idiom test_install_service_linger.py
and test_init_db_ledger_adoption.py use -- with a PATH-stubbed `docker` that
records every invocation's argv and answers `compose ... down` / `ps -a`
by env var. Nothing real is ever touched.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
UNINSTALL = REPO_ROOT / "shared-memory" / "scripts" / "uninstall_framework.sh"

BEGIN_MARKER = "# >>> COMPOSE_DOWN_AND_VERIFY"
END_MARKER = "# <<< COMPOSE_DOWN_AND_VERIFY"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_source() -> str:
    text = UNINSTALL.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{UNINSTALL} -- the extraction markers moved or were removed"
    )
    return m.group(1)


_COLOR_HELPERS = (
    "red() { printf '\\033[31m%s\\033[0m\\n' \"$*\"; }\n"
    "grn() { printf '\\033[32m%s\\033[0m\\n' \"$*\"; }\n"
    "ylw() { printf '\\033[33m%s\\033[0m\\n' \"$*\"; }\n"
)

_FIXTURE_COMPOSE = """\
name: shared-memory
services:
  neo4j:
    container_name: neo4j-memory
    volumes:
      - ${NEO4J_HOST_DIR:?set NEO4J_HOST_DIR in shared-memory/.env}/data:/data:z
  postgres:
    container_name: postgres-vector
    volumes:
      - ${PG_DATA_DIR:?set PG_DATA_DIR in shared-memory/.env}:/var/lib/postgresql/data:z
"""


def _docker_stub_body(bash_bin: str) -> str:
    # Records every call (one line of argv per invocation) and answers the
    # two calls compose_down_and_verify() makes: `compose ... down ...` and
    # `ps -a --format {{.Names}}`. Absolute-path shebang for the same
    # determinism reason test_init_db_ledger_adoption.py's stubs use -- PATH
    # never falls back to the real one, so a real `docker` on this dev/CI
    # host can never leak in.
    return (
        f"#!{bash_bin}\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$1" == "compose" ]]; then\n'
        '    for a in "$@"; do\n'
        '        if [[ "$a" == "down" ]]; then\n'
        '            printf "%s" "${DOCKER_DOWN_OUT:-}"\n'
        '            exit "${DOCKER_DOWN_RC:-0}"\n'
        "        fi\n"
        "    done\n"
        "    exit 0\n"
        'elif [[ "$1" == "ps" ]]; then\n'
        '    printf "%s\\n" "${DOCKER_PS_OUTPUT:-}"\n'
        "    exit 0\n"
        "else\n"
        "    exit 0\n"
        "fi\n"
    )


def _run(tmp_path: Path, *, env_file_present: bool, env_overrides: dict = None,
          compose_text: str = _FIXTURE_COMPOSE):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "invocations.log"
    log_path.write_text("")

    bash_bin = shutil.which("bash")
    assert bash_bin, "bash not found on the harness's own PATH"

    docker_stub = bin_dir / "docker"
    docker_stub.write_text(_docker_stub_body(bash_bin))
    docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC)

    compose_file = tmp_path / "postgres_neo4j_limits.yaml"
    compose_file.write_text(compose_text)

    env_file = tmp_path / ".env"
    if env_file_present:
        env_file.write_text("NEO4J_HOST_DIR=/data/neo4j\nPG_DATA_DIR=/data/pg\n")

    source = _extract_source()
    script = (
        _COLOR_HELPERS
        + f'COMPOSE_FILE="{compose_file}"\n'
        + f'ENV_FILE="{env_file}"\n'
        + 'NEO4J_HOST_DIR=""\nPG_DATA_DIR=""\n'
        + source
        + "\ncompose_down_and_verify\n"
    )

    env = {
        # bin_dir FIRST so the stubbed `docker` always wins over any real one
        # later in PATH; the real PATH is still needed for grep/sed/mapfile's
        # own coreutils, which compose_down_and_verify() genuinely calls.
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
    text = UNINSTALL.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_source()
    assert "compose_down_and_verify()" in source


# ── F3: --env-file is passed, exactly the shape the install side uses ───────

def test_env_file_present_down_is_invoked_with_env_file_and_compose_file(tmp_path):
    """Pinned by ARGV, not by exit code: the down call must actually carry
    --env-file <the .env> alongside -f <the compose file> -- the exact
    invocation that was measured missing on the live host."""
    proc, log_path = _run(tmp_path, env_file_present=True)
    log_text = log_path.read_text()

    assert proc.returncode == 0, _strip_ansi(proc.stdout + proc.stderr)
    down_calls = [line for line in log_text.splitlines() if "down" in line]
    assert down_calls, f"no docker compose ... down call was made:\n{log_text}"
    call = down_calls[0]
    assert "--env-file" in call, f"--env-file missing from the down call: {call!r}"
    assert str(tmp_path / ".env") in call, f".env path missing from the down call: {call!r}"
    assert "postgres_neo4j_limits.yaml" in call, f"compose file missing from the down call: {call!r}"
    assert "down" in call and "-v" in call


def test_env_file_absent_falls_back_env_less_but_still_calls_down(tmp_path):
    """F3's second half: a re-run after a partial uninstall (.env already
    gone). The fallback must still reach `docker compose ... down`, WITHOUT
    --env-file (there is nothing to point it at), and must not crash."""
    proc, log_path = _run(tmp_path, env_file_present=False)
    log_text = log_path.read_text()
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    down_calls = [line for line in log_text.splitlines() if "down" in line]
    assert down_calls, f"no docker compose ... down call was made:\n{log_text}"
    call = down_calls[0]
    assert "--env-file" not in call, (
        f"--env-file was passed despite no .env existing -- it would point "
        f"at a nonexistent file: {call!r}"
    )
    assert "env-less" in out or "not found" in out, (
        "the operator must be told this is a degraded, env-less down"
    )


# ── F5: the exit code is checked, BY VALUE, not just "some error text" ──────

def test_down_failure_is_not_reported_as_success_and_exits_nonzero(tmp_path):
    """THE mutation-relevant guard. Make the stubbed `down` fail (as the real
    one did on the live host, for lack of --env-file) and assert BOTH that
    the success line never prints AND that the exit code is nonzero."""
    proc, _log = _run(
        tmp_path, env_file_present=True,
        env_overrides={
            "DOCKER_DOWN_RC": "1",
            "DOCKER_DOWN_OUT": "service \"neo4j\" variable is not set: NEO4J_HOST_DIR\n",
        },
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "compose stack down, verified gone" not in out, (
        f"success was printed despite `down` failing:\n{out}"
    )
    assert "FAILED" in out
    assert "NEO4J_HOST_DIR" in out, "the real compose error must reach the operator"


def test_down_success_with_no_leftovers_prints_the_verified_success_line(tmp_path):
    proc, _log = _run(
        tmp_path, env_file_present=True,
        env_overrides={"DOCKER_DOWN_RC": "0", "DOCKER_PS_OUTPUT": ""},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "compose stack down, verified gone" in out


# ── F4: a 0 exit is not trusted -- containers must be MEASURED gone ─────────

def test_leftover_container_after_a_clean_exit_is_named_and_fails(tmp_path):
    """compose exits 0 (nothing wrong with the invocation) but a container
    from THIS stack is still present -- the exact shape of F4: containers
    surviving what was reported as a completed teardown."""
    proc, _log = _run(
        tmp_path, env_file_present=True,
        env_overrides={
            "DOCKER_DOWN_RC": "0",
            "DOCKER_PS_OUTPUT": "neo4j-memory\nsome-other-unrelated-container",
        },
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "neo4j-memory" in out, f"the leftover container must be named:\n{out}"
    assert "some-other-unrelated-container" not in out, (
        "a container that is not part of this compose file must not be "
        "reported as a leftover of this stack"
    )
    assert "STILL PRESENT" in out
    assert "compose stack down, verified gone" not in out


def test_two_leftover_containers_are_both_named(tmp_path):
    proc, _log = _run(
        tmp_path, env_file_present=True,
        env_overrides={
            "DOCKER_DOWN_RC": "0",
            "DOCKER_PS_OUTPUT": "neo4j-memory\npostgres-vector",
        },
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "neo4j-memory" in out
    assert "postgres-vector" in out


def test_container_list_is_read_from_the_compose_file_not_hardcoded(tmp_path):
    """A single-service fixture proves the container list is DERIVED from
    the compose file's own container_name: lines, not a hardcoded copy that
    could silently drift from it."""
    one_service = """\
name: shared-memory
services:
  solo:
    container_name: solo-only-container
"""
    proc, _log = _run(
        tmp_path, env_file_present=True, compose_text=one_service,
        env_overrides={"DOCKER_DOWN_RC": "0", "DOCKER_PS_OUTPUT": "solo-only-container"},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode != 0, out
    assert "solo-only-container" in out


def test_the_real_compose_file_container_names_are_discoverable(tmp_path):
    """Sanity check against the SHIPPED compose file (read-only, never
    invoked): the extraction's own container_name: grep/sed must find the
    same names a live docker ps would need to match against."""
    real_compose = (REPO_ROOT / "shared-memory" / "ops"
                     / "postgres_neo4j_limits.yaml")
    text = real_compose.read_text()
    names = [
        line.split(":", 1)[1].strip()
        for line in text.splitlines()
        if line.strip().startswith("container_name:")
    ]
    assert "neo4j-memory" in names
    assert "postgres-vector" in names
    assert len(names) >= 2


# ── Ops & Release Integrity review, Critical (Ops-14), merger-verified ─────
#
# compose_down_and_verify()'s post-condition check FAILS OPEN when the
# container_name: list it parses out of $COMPOSE_FILE comes back EMPTY (the
# compose file's syntax changed, it was renamed, or it is simply missing by
# the time this runs): the leftover-detection loop below then iterates zero
# times, finds zero leftovers, and the function claims VERIFIED success --
# the exact unearned checkmark this whole function exists to remove,
# reintroduced one layer up in its own verification step. The fix refuses to
# claim success on an empty parse; it reports "cannot verify" as a FAILURE.

_NO_CONTAINER_NAMES_COMPOSE = """\
name: shared-memory
services:
  neo4j:
    image: neo4j:5-community
  postgres:
    image: pgvector/pgvector:pg17
"""


def test_empty_container_list_refuses_to_claim_success(tmp_path):
    """(a) A compose file with zero container_name: lines must NOT be read
    as "nothing to check" -- it must be refused as unverifiable, by value:
    the success line absent, the cannot-verify wording present, nonzero."""
    proc, log_path = _run(
        tmp_path, env_file_present=True, compose_text=_NO_CONTAINER_NAMES_COMPOSE,
        env_overrides={"DOCKER_DOWN_RC": "0", "DOCKER_PS_OUTPUT": ""},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode != 0, out
    assert "compose stack down, verified gone" not in out, (
        f"success was claimed despite an unparseable container list:\n{out}"
    )
    assert "could not parse any container_name" in out, out
    assert "CANNOT be verified" in out, out
    assert "docker ps -a" in out, "the operator must be told how to check by hand"
    # And the down itself DID run (this is a verification failure, not a
    # down failure) -- confirms the guard fires after a real, successful
    # down, not instead of attempting one.
    assert any("down" in line for line in log_text.splitlines()), log_text


def test_empty_container_list_is_distinct_from_a_leftover_failure(tmp_path):
    """The wording must not be confusable with the leftover-container
    failure path -- an operator reading the output needs to know WHICH
    problem they have."""
    proc, _log = _run(
        tmp_path, env_file_present=True, compose_text=_NO_CONTAINER_NAMES_COMPOSE,
        env_overrides={"DOCKER_DOWN_RC": "0", "DOCKER_PS_OUTPUT": ""},
    )
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert "STILL PRESENT" not in out, (
        "an unparseable list must not be reported as a leftover-container "
        f"failure -- they are different problems:\n{out}"
    )
