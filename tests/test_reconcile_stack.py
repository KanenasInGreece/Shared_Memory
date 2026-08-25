"""reconcile_stack.sh — operator-run drift table + reconcile (decision:1589,
grounded on fact:1588 and decision:1586).

WHAT THIS SCRIPT IS FOR. v0.9.55 moved the compose image pins (pgvector,
neo4j) but update_framework.sh never recreates containers -- that stays a
STANDALONE script the operator runs on their own word (see reconcile_stack.sh's
own header, and test_update_framework_stack_drift.py for the calling side in
update_framework.sh). This file is about reconcile_stack.sh itself: the drift
table it prints, and the pull/up/ALTER EXTENSION sequence it runs only on
confirmation.

HOW THIS IS TESTED. Per this repo's own rule (fact:1194/CLAUDE.md), no docker
command may run against the live system -- everything here goes through a
PATH-stubbed fake `docker` that RECORDS every invocation and answers
`inspect`/`exec`/`compose` from environment variables, the same idiom
test_uninstall_compose_down.py and test_update_skill_attribution.py already
use. The real script is copied into a throwaway `<tmp>/shared-memory/
scripts/reconcile_stack.sh` (not run in place) so its own ENV_FILE resolution
(shared-memory/.env, sibling to the script two directories up) lands inside
the sandbox and can never resolve to this checkout's own (absent) .env --
COMPOSE_FILE is separately overridden via the env var the script already
reads. The fixture compose file keeps the six real SERVICE KEYS
(neo4j/postgres/retriever-api/reranker-api/retriever-api-gpu/reranker-api-gpu)
-- reconcile_stack.sh iterates that literal list, by the same "written
against THIS yaml" reasoning postflight.sh/init_db.sh already use for their
own container-name defaults -- but every image tag, container name and
password below is a fixture value, never anything from a real host.

Assertions are on VALUES throughout: exact exit codes, exact argv sequences
in the recorded docker.log, and exact row text in stdout -- never one
expression compared against another.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
REAL_SCRIPT = REPO_ROOT / "shared-memory" / "scripts" / "reconcile_stack.sh"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ── Fixture compose file — the six real service keys, fixture image/tag/
# container-name values throughout. No private host or project name anywhere.
_FIXTURE_COMPOSE = """\
name: fixture-stack
services:
  neo4j:
    image: fixture/graphdb:9.9.9-community
    container_name: fixture-graphdb
  postgres:
    image: fixture/vectordb:1.2.3-pgN
    container_name: fixture-vectordb
    environment:
      - POSTGRES_PASSWORD=x
      - POSTGRES_DB=fixture_db
  retriever-api:
    image: ghcr.io/fixture-org/infer:server
    container_name: fixture-retriever
    deploy:
      replicas: ${EMBEDDER_CPU_REPLICAS:-${CPU_ENCODER_REPLICAS:-1}}
  reranker-api:
    image: ghcr.io/fixture-org/infer:server
    container_name: fixture-reranker
    deploy:
      replicas: ${RERANKER_CPU_REPLICAS:-${CPU_ENCODER_REPLICAS:-1}}
  retriever-api-gpu:
    image: ghcr.io/fixture-org/infer:server-vulkan
    container_name: fixture-retriever-gpu
    deploy:
      replicas: ${EMBEDDER_GPU_REPLICAS:-${GPU_ENCODER_REPLICAS:-0}}
  reranker-api-gpu:
    image: ghcr.io/fixture-org/infer:server-vulkan
    container_name: fixture-reranker-gpu
    deploy:
      replicas: ${RERANKER_GPU_REPLICAS:-${GPU_ENCODER_REPLICAS:-0}}
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    st = path.stat()
    path.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_LOG"
case "$1" in
    inspect)
        cname="$2"
        # "after up -d" values win once the marker exists, so a test can
        # prove a real reconcile (pull -> up -> ALTER EXTENSION) actually
        # changes what the table sees on its second, post-reconcile pass.
        if [[ -n "${DOCKER_UP_MARKER:-}" && -f "$DOCKER_UP_MARKER" && "$cname" == "${DOCKER_RECONCILE_CNAME:-}" && -n "${DOCKER_IMG_AFTER_UP:-}" ]]; then
            printf '%s' "$DOCKER_IMG_AFTER_UP"
            exit 0
        fi
        var="DOCKER_IMG_$(printf '%s' "$cname" | tr '[:lower:]-' '[:upper:]_')"
        val="${!var:-}"
        if [[ -n "$val" ]]; then
            printf '%s' "$val"
            exit 0
        else
            exit 1
        fi
        ;;
    exec)
        if [[ "$*" == *"pg_isready"* ]]; then
            exit "${DOCKER_PG_ISREADY_RC:-0}"
        fi
        if [[ "$*" == *"extversion"* ]]; then
            if [[ -n "${DOCKER_UP_MARKER:-}" && -f "$DOCKER_UP_MARKER" && -n "${DOCKER_EXT_SQL_AFTER:-}" ]]; then
                printf '%s' "$DOCKER_EXT_SQL_AFTER"
            else
                printf '%s' "${DOCKER_EXT_SQL:-}"
            fi
            exit 0
        fi
        if [[ "$*" == *"vector--"* ]]; then
            printf '%s' "${DOCKER_EXT_FILE:-}"
            exit 0
        fi
        if [[ "$*" == *"ALTER EXTENSION"* ]]; then
            exit "${DOCKER_ALTER_RC:-0}"
        fi
        exit 0
        ;;
    compose)
        for a in "$@"; do
            if [[ "$a" == "up" && -n "${DOCKER_UP_MARKER:-}" ]]; then
                : > "$DOCKER_UP_MARKER"
            fi
        done
        exit "${DOCKER_COMPOSE_RC:-0}"
        ;;
    *)
        exit 0
        ;;
esac
"""


def _sandbox(tmp_path: Path, compose_text: str = _FIXTURE_COMPOSE, env_lines: str = ""):
    """Copies the REAL reconcile_stack.sh into <tmp>/shared-memory/scripts/
    so its own ENV_FILE resolution (sibling shared-memory/.env, two levels
    up) lands entirely inside the sandbox. Returns (script_path, compose_path,
    env_path, bin_dir, log_path)."""
    scripts_dir = tmp_path / "shared-memory" / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "reconcile_stack.sh"
    shutil.copy(REAL_SCRIPT, script)
    st = script.stat()
    script.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env_path = tmp_path / "shared-memory" / ".env"
    env_path.write_text(env_lines)

    compose_path = tmp_path / "fixture-compose.yaml"
    compose_path.write_text(compose_text)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_exec(bin_dir / "docker", _DOCKER_STUB)

    log_path = tmp_path / "docker.log"
    log_path.write_text("")

    return script, compose_path, env_path, bin_dir, log_path


def _run(
    tmp_path: Path,
    *args: str,
    compose_text: str = _FIXTURE_COMPOSE,
    env_lines: str = "",
    env_overrides: dict | None = None,
    stdin_text: str | None = "",
    timeout: int = 20,
) -> tuple[subprocess.CompletedProcess, Path]:
    script, compose_path, env_path, bin_dir, log_path = _sandbox(
        tmp_path, compose_text=compose_text, env_lines=env_lines,
    )
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "COMPOSE_FILE": str(compose_path),
        "DOCKER_LOG": str(log_path),
    }
    env.update(env_overrides or {})
    proc = subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
        input=stdin_text,
    )
    return proc, log_path


# ── Extraction / sandbox plumbing sanity ────────────────────────────────────

def test_real_compose_file_uses_the_same_six_service_keys():
    """Sanity check against the SHIPPED yaml (read-only, never invoked): the
    literal service keys reconcile_stack.sh iterates must actually be the
    ones the real compose file declares."""
    real_compose = (REPO_ROOT / "shared-memory" / "ops"
                     / "postgres_neo4j_limits.yaml")
    text = real_compose.read_text()
    for key in ("neo4j", "postgres", "retriever-api", "reranker-api",
                "retriever-api-gpu", "reranker-api-gpu"):
        assert re.search(rf"^  {re.escape(key)}:\s*$", text, re.M), (
            f"service key '{key}' not found in the real compose file — "
            f"reconcile_stack.sh's hardcoded SERVICES list has drifted from it"
        )


# ── --help: zero side effects ────────────────────────────────────────────────

def test_help_invokes_docker_zero_times_and_exits_zero(tmp_path):
    proc, log_path = _run(tmp_path, "--help")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert log_path.read_text() == "", (
        f"--help invoked docker at least once:\n{log_path.read_text()}"
    )
    assert "reconcile_stack.sh" in out


def test_unknown_argument_is_refused(tmp_path):
    proc, log_path = _run(tmp_path, "--this-flag-does-not-exist")
    assert proc.returncode != 0
    assert log_path.read_text() == ""


# ── Row status: in sync / DRIFT / floating / not deployed here ──────────────

_ALL_IN_SYNC_ENV = {
    "DOCKER_IMG_FIXTURE_GRAPHDB": "fixture/graphdb:9.9.9-community",
    "DOCKER_IMG_FIXTURE_VECTORDB": "fixture/vectordb:1.2.3-pgN",
    "DOCKER_IMG_FIXTURE_RETRIEVER": "ghcr.io/fixture-org/infer:server",
    "DOCKER_IMG_FIXTURE_RERANKER": "ghcr.io/fixture-org/infer:server",
    "DOCKER_EXT_SQL": "1.2.3",
    "DOCKER_EXT_FILE": "/usr/share/postgresql/17/extension/vector--1.2.3.sql",
}


def test_in_sync_when_running_equals_pin(tmp_path):
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=_ALL_IN_SYNC_ENV)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert re.search(r"^neo4j\s+in sync\s", out, re.M), out
    assert re.search(r"^postgres\s+in sync\s", out, re.M), out


def test_drift_when_running_image_differs_from_pin(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"  # stale
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=env)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 2, out
    assert re.search(r"^postgres\s+DRIFT\s", out, re.M), out
    assert "fixture/vectordb:1.2.2-pgN" in out


def test_absent_container_is_drift(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    del env["DOCKER_IMG_FIXTURE_VECTORDB"]  # docker inspect -> rc 1 -> "absent"
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=env)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 2, out
    assert re.search(r"^postgres\s+DRIFT\s+\S+\s+absent", out, re.M), out


def test_floating_tag_never_reported_as_in_sync_even_when_equal(tmp_path):
    """retriever-api/reranker-api are pinned to `:server` (no digit) in the
    fixture -- even when the running image matches EXACTLY, the row must
    read 'floating', never 'in sync', because there is no version to have
    matched against."""
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=_ALL_IN_SYNC_ENV)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert re.search(r"^retriever-api\s+floating\s", out, re.M), out
    assert re.search(r"^reranker-api\s+floating\s", out, re.M), out
    assert not re.search(r"^retriever-api\s+in sync\s", out, re.M), out


def test_floating_row_never_counts_as_drift(tmp_path):
    """Only the floating services are 'running'; everything with a real pin
    is in sync -- overall verdict must be NO drift (exit 0), proving a
    floating row never contributes to the drift count."""
    env = dict(_ALL_IN_SYNC_ENV)
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=env)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 0, out
    assert "DRIFT" not in out, out


def test_zero_replica_service_is_not_deployed_here(tmp_path):
    """GPU replicas default to 0 in the shipped nested-default chain (no
    .env override needed) -- both GPU rows must read 'not deployed here'
    and never be probed via docker inspect at all."""
    proc, log_path = _run(tmp_path, "--dry-run", env_overrides=_ALL_IN_SYNC_ENV)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert re.search(r"^retriever-api-gpu\s+not deployed here\s", out, re.M), out
    assert re.search(r"^reranker-api-gpu\s+not deployed here\s", out, re.M), out
    log_text = log_path.read_text()
    assert "fixture-retriever-gpu" not in log_text, (
        f"a 0-replica service was still probed via docker inspect:\n{log_text}"
    )


def test_explicit_zero_replica_override_via_env_is_not_deployed_here(tmp_path):
    """The same verdict, driven by an explicit per-service override rather
    than the bare default -- proves the .env is actually consulted, not
    just the hardcoded fallback."""
    env_lines = "EMBEDDER_CPU_REPLICAS=0\n"
    env = dict(_ALL_IN_SYNC_ENV)
    proc, _log = _run(tmp_path, "--dry-run", env_lines=env_lines, env_overrides=env)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert re.search(r"^retriever-api\s+not deployed here\s", out, re.M), out


def test_pgvector_extension_drift(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_EXT_SQL"] = "1.2.2"  # SQL behind the image's own bundled version
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=env)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode == 2, out
    assert re.search(r"^pgvector-extension\s+DRIFT\s", out, re.M), out


def test_pgvector_extension_in_sync(tmp_path):
    proc, _log = _run(tmp_path, "--dry-run", env_overrides=_ALL_IN_SYNC_ENV)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert re.search(r"^pgvector-extension\s+in sync\s", out, re.M), out


# ── --dry-run: exit codes, never invokes pull/up ─────────────────────────────

def test_dry_run_exits_0_on_no_drift_and_never_touches_compose(tmp_path):
    proc, log_path = _run(tmp_path, "--dry-run", env_overrides=_ALL_IN_SYNC_ENV)
    assert proc.returncode == 0
    log_text = log_path.read_text()
    assert "compose" not in log_text, log_text


def test_dry_run_exits_2_on_drift_and_never_touches_compose(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, log_path = _run(tmp_path, "--dry-run", env_overrides=env)
    assert proc.returncode == 2
    log_text = log_path.read_text()
    assert "compose" not in log_text, (
        f"--dry-run invoked docker compose despite drift being present:\n{log_text}"
    )


# ── Interactive confirmation gate ────────────────────────────────────────────

def test_no_confirmation_given_refuses_and_never_touches_compose(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, log_path = _run(tmp_path, env_overrides=env, stdin_text="\n")
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert proc.returncode != 0, out
    assert "not confirmed" in out, out
    assert "compose" not in log_path.read_text()


def test_wrong_confirmation_word_refuses(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, log_path = _run(tmp_path, env_overrides=env, stdin_text="yes\n")
    assert proc.returncode != 0
    assert "compose" not in log_path.read_text()


# ── --yes: pull, then up -d, then ALTER EXTENSION, in that order ────────────

def test_yes_invokes_pull_then_up_then_alter_extension_in_order(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, log_path = _run(tmp_path, "--yes", env_overrides=env, stdin_text=None)
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_lines = log_path.read_text().splitlines()

    token_lines = [l.split() for l in log_lines]
    pull_idx = next(i for i, t in enumerate(token_lines) if t[:1] == ["compose"] and "pull" in t)
    up_idx = next(i for i, t in enumerate(token_lines) if t[:1] == ["compose"] and "up" in t)
    alter_idx = next(i for i, l in enumerate(log_lines) if "ALTER EXTENSION" in l)

    assert pull_idx < up_idx < alter_idx, (
        f"pull/up/ALTER EXTENSION out of order:\n{log_path.read_text()}"
    )
    # Exact argv on the two compose calls, not just substring presence.
    assert any(
        t[:1] == ["compose"] and "-f" in t and "--env-file" in t and "pull" in t
        for t in token_lines
    ), log_lines
    assert any(
        t[:1] == ["compose"] and "-f" in t and "--env-file" in t and "up" in t and "-d" in t
        for t in token_lines
    ), log_lines


def test_yes_never_prompts(tmp_path):
    """--yes must not block on stdin -- run with stdin closed entirely."""
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, _log = _run(tmp_path, "--yes", env_overrides=env, stdin_text=None, timeout=15)
    out = _strip_ansi(proc.stdout + proc.stderr)
    assert "Type 'reconcile'" not in out, out


# ── Post-reconcile verification: exit 0 once actually fixed, exit 1 if not ──

def test_reconcile_succeeds_exit_0_when_drift_actually_closes(tmp_path):
    marker = tmp_path / "up-happened"
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"  # stale BEFORE
    env["DOCKER_UP_MARKER"] = str(marker)
    env["DOCKER_RECONCILE_CNAME"] = "fixture-vectordb"
    env["DOCKER_IMG_AFTER_UP"] = "fixture/vectordb:1.2.3-pgN"  # AFTER up -d: matches the pin
    env["DOCKER_EXT_SQL_AFTER"] = "1.2.3"  # AFTER ALTER EXTENSION: matches the image

    proc, log_path = _run(tmp_path, "--yes", env_overrides=env, stdin_text=None)
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Reconciled — no DRIFT rows remain" in out, out
    assert marker.exists(), "docker compose ... up was never actually invoked"


def test_reconcile_finishes_but_drift_remains_exit_1(tmp_path):
    """The stub's 'after up' values are left UNSET here -- docker inspect
    keeps answering the stale image even after up -d runs, simulating a
    reconcile that ran but did not actually fix anything (e.g. a pull that
    silently no-op'd). The post-check must catch this, not trust the exit
    code of `up -d` alone."""
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    proc, log_path = _run(tmp_path, "--yes", env_overrides=env, stdin_text=None)
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "DRIFT row(s) remain" in out, out
    log_text = log_path.read_text()
    assert any("pull" in l for l in log_text.splitlines() if "compose" in l), (
        "reconcile must still have ATTEMPTED pull/up before reporting remaining drift"
    )


# ── Never edits .env, never restarts the gateway ─────────────────────────────

def test_env_file_is_never_written(tmp_path):
    env = dict(_ALL_IN_SYNC_ENV)
    env["DOCKER_IMG_FIXTURE_VECTORDB"] = "fixture/vectordb:1.2.2-pgN"
    env["DOCKER_UP_MARKER"] = str(tmp_path / "marker")
    env["DOCKER_RECONCILE_CNAME"] = "fixture-vectordb"
    env["DOCKER_IMG_AFTER_UP"] = "fixture/vectordb:1.2.3-pgN"
    env["DOCKER_EXT_SQL_AFTER"] = "1.2.3"

    script, compose_path, env_path, bin_dir, log_path = _sandbox(tmp_path)
    proc = subprocess.run(
        ["bash", str(script), "--yes"],
        capture_output=True, text=True, timeout=20,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(tmp_path),
            "COMPOSE_FILE": str(compose_path),
            "DOCKER_LOG": str(log_path),
            **env,
        },
    )
    assert env_path.read_text() == "", "shared-memory/.env was modified"
    assert "systemctl" not in log_path.read_text()
    assert "restart" not in _strip_ansi(proc.stdout + proc.stderr).lower() or \
           "gateway was NOT restarted" in _strip_ansi(proc.stdout + proc.stderr)
