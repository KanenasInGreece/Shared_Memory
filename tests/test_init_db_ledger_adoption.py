"""init_db.sh — adopt_ledger() (fresh-install first-upgrade fix, corpus
fact:1511/fact:1512, fix A).

MEASURED ON A LIVE FRESH-INSTALL HOST: a database born fresh via init_db.sh
has NO schema_migrations ledger — schema_init.sql deliberately does not
create it (it is generated FROM the migrations, not a migration itself), and
nothing else adopted it either. The FIRST-ever `apply.py` upgrade run on
such a host therefore dies with exit 2, whose message claims the database
"predates migration tracking / came from a backup taken before v0.8.35" —
false for a fresh install, which is the COMMON origin, not the rare one.

THE FIX. init_db.sh now calls `apply.py --adopt` itself, immediately after
schema_init.sql succeeds — the one moment adoption is not a guess: the
schema was created HERE, by THIS run, from the migrations this checkout
ships, so recording every migration file as already applied restates what
just happened rather than vouching for an unknown database's history.

WHY THIS IS TESTED AS AN EXTRACTED FUNCTION, NOT END TO END. init_db.sh's
surrounding steps talk to Postgres/Neo4j via `docker exec` — reaching them
requires live containers, which this suite must never touch (SQL/daemon
behaviour is verified on the running system by hand, per this repo's
CLAUDE.md). adopt_ledger() itself is self-contained (it takes no arguments,
depends only on the pre-existing `grn`/`ylw` helpers and `$MIGRATIONS_DIR`,
and its one external effect is invoking `uv`), so it is lifted out between
the `# >>> ADOPT_LEDGER` / `# <<< ADOPT_LEDGER` markers and run standalone —
the same idiom test_install_service_linger.py uses for enable_linger() —
with a PATH-stubbed `uv` that records its invocation and returns a
controlled exit code instead of touching a real database. Nothing real is
ever touched: no docker, no Postgres, no network.
"""
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

INIT_DB = Path(__file__).parent.parent / "shared-memory" / "scripts" / "init_db.sh"

BEGIN_MARKER = "# >>> ADOPT_LEDGER"
END_MARKER = "# <<< ADOPT_LEDGER"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _extract_adopt_ledger_source() -> str:
    text = INIT_DB.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{INIT_DB} -- the extraction markers moved or were removed"
    )
    return m.group(1)


# grn/ylw copied VERBATIM from init_db.sh's own definitions (lines ~56-58) so
# the extracted function's calls to them behave exactly as they do in the
# real script -- this is intentionally the same ANSI-wrapped output, stripped
# by _strip_ansi() below rather than by redefining them away.
_COLOR_HELPERS = (
    "red() { printf '\\033[31m%s\\033[0m\\n' \"$*\"; }\n"
    "grn() { printf '\\033[32m%s\\033[0m\\n' \"$*\"; }\n"
    "ylw() { printf '\\033[33m%s\\033[0m\\n' \"$*\"; }\n"
)

def _uv_stub_body(bash_bin: str) -> str:
    # Simulates `uv run --with psycopg2-binary python .../apply.py --adopt`.
    # $UV_ADOPT_MODE controls the outcome; the invocation is always logged so
    # tests can confirm --adopt was actually the command reached.
    #
    # The shebang names bash by its ABSOLUTE path rather than
    # `#!/usr/bin/env bash` deliberately: on this workstation (and any host
    # that symlinks uv onto the system PATH per this repo's own preflight.sh
    # guidance) the real `uv` and the real `bash` live in the SAME directory
    # (/usr/bin), so adding that directory to PATH -- the natural way to make
    # `env bash` resolve -- would silently leak the real `uv` back in and
    # defeat the "uv absent" scenario below. Naming bash directly needs no
    # directory added to PATH at all, so PATH can stay exactly `bin_dir`
    # (with or without this stub in it) in every scenario.
    return (
        f"#!{bash_bin}\n"
        'echo "uv $*" >> "$INVOCATION_LOG"\n'
        'case "${UV_ADOPT_MODE:-success}" in\n'
        "    success) exit 0 ;;\n"
        '    fail)    echo "apply.py: simulated failure" >&2; exit 1 ;;\n'
        "esac\n"
    )


def _run_adopt_ledger(tmp_path: Path, *, uv_present: bool, env_overrides: dict = None):
    """Run adopt_ledger() standalone. When uv_present is False, PATH is built
    with NO fallback to the real PATH at all (an absolute path is used for
    bash itself), so `command -v uv` genuinely fails regardless of whether
    this dev/CI host happens to have a real uv installed -- the same
    determinism concern test_install_service_linger.py's stubs solve for
    loginctl/sudo, just pushed one step further here because uv is commonly
    present on the host actually running this suite."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log_path = tmp_path / "invocations.log"
    log_path.write_text("")

    bash_bin = shutil.which("bash")
    assert bash_bin, "bash not found on the harness's own PATH"

    if uv_present:
        uv_stub = bin_dir / "uv"
        uv_stub.write_text(_uv_stub_body(bash_bin))
        uv_stub.chmod(uv_stub.stat().st_mode | stat.S_IEXEC)

    source = _extract_adopt_ledger_source()
    script = (
        _COLOR_HELPERS
        + f'MIGRATIONS_DIR="{tmp_path / "migrations"}"\n'
        + source
        + "\nadopt_ledger\n"
    )

    env = {
        "PATH": str(bin_dir),  # deliberately NO fallback to the real PATH
        "INVOCATION_LOG": str(log_path),
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


def test_extracted_block_defines_the_function():
    source = _extract_adopt_ledger_source()
    assert "adopt_ledger()" in source


def test_the_function_is_actually_called_after_schema_applied():
    """The extraction proves adopt_ledger() itself behaves correctly; this
    proves init_db.sh actually CALLS it after the schema succeeds, rather
    than defining a function nothing invokes. A mutation deleting the bare
    `adopt_ledger` call line at the bottom of the block's surrounding
    context would leave every other test in this file green."""
    text = INIT_DB.read_text()
    schema_idx = text.index('grn "✓ Postgres schema applied"')
    call_idx = text.index("\nadopt_ledger\n", schema_idx)
    assert call_idx > schema_idx, (
        "adopt_ledger is not called after the Postgres schema success line"
    )
    # And it must run BEFORE Neo4j is touched -- Postgres ledger adoption is
    # independent of Neo4j succeeding or failing.
    neo4j_idx = text.index("Waiting for Neo4j", schema_idx)
    assert call_idx < neo4j_idx, (
        "adopt_ledger is called after Neo4j setup begins, not right after "
        "the Postgres schema succeeds"
    )


def test_uv_present_and_adopt_succeeds_reports_success(tmp_path):
    proc, log_path = _run_adopt_ledger(tmp_path, uv_present=True,
                                        env_overrides={"UV_ADOPT_MODE": "success"})
    out = _strip_ansi(proc.stdout + proc.stderr)
    log_text = log_path.read_text()

    assert proc.returncode == 0, out
    assert "Migration ledger populated" in out, out
    assert "apply.py" in log_text and "--adopt" in log_text, (
        f"apply.py --adopt was not invoked:\n{log_text}"
    )


def test_uv_present_but_adopt_fails_warns_but_does_not_fail_the_script(tmp_path):
    """apply.py --adopt failing (e.g. Postgres unreachable from the host)
    must not be treated as fatal by init_db.sh -- the Postgres/Neo4j schema
    work already succeeded; this is a convenience step layered on top."""
    proc, log_path = _run_adopt_ledger(tmp_path, uv_present=True,
                                        env_overrides={"UV_ADOPT_MODE": "fail"})
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Could not populate the migration ledger automatically" in out, out
    assert "apply.py --adopt" in out, (
        "the manual remedy command must still be printed on failure"
    )


def test_uv_absent_warns_and_names_the_manual_command(tmp_path):
    proc, _log_path = _run_adopt_ledger(tmp_path, uv_present=False)
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "uv not found on PATH" in out, out
    assert "apply.py --adopt" in out, (
        "the manual remedy command must be printed when uv is unreachable"
    )


def test_header_documents_the_new_step():
    """The file's own top-of-file docstring describes what init_db.sh does;
    it must mention the ledger step, not just the two stores, or a reader
    scanning only the header believes init_db.sh does less than it does."""
    header_lines = []
    for line in INIT_DB.read_text().splitlines()[1:]:  # skip shebang
        if not line.startswith("#"):
            break
        header_lines.append(line)
    header = "\n".join(header_lines)
    assert "schema_migrations" in header or "ledger" in header, (
        "init_db.sh's header does not mention the migration-ledger step"
    )
