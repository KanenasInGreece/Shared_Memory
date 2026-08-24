"""update_framework.sh — U1: the update path re-CHECKS an already-installed
host's systemd linger flag (fact:1492). It never enables it (that stays owned
by install_service.sh); it only reads the flag and reports one of three
verdicts:

    yes            linger is on (measured directly, or corroborated)
    no             linger is DEFINITIVELY off -- loud, with the remedy, but
                    NON-FATAL
    not-applicable genuinely unknown -- no linger mechanism reachable at all
                    -- stated plainly, NOT a failure

PRIMARY instrument: /var/lib/systemd/linger/<user> -- systemd-logind creates
this file the instant linger is enabled and removes it the instant it is
disabled, so its existence IS the flag whenever the parent directory exists
at all. The directory is a FUNCTION PARAMETER to check_linger() (default the
literal /var/lib/systemd/linger) -- RULING 3 (this branch) removed the
earlier `$LINGER_DIR` environment read entirely, because reading it from the
live environment made it a silent bypass reachable from a REAL production
run, not just from this test suite. Tests that call check_linger() directly
(below) now pass the directory as an explicit positional argument instead;
see test_linger_dir_env_var_has_no_effect_on_verdict for the dedicated proof
that the environment variable is now inert. (fact: linger IS enabled on
THIS workstation for its real login user, which would silently mask every
"no" case below if these tests fell through to reading the real path for
the real user -- the explicit-argument tests avoid that by construction;
the full end-to-end tests near the bottom of this file, which exercise the
shipped script's own bare `check_linger` call site and so can no longer
redirect the directory at all, avoid it via $USER instead -- see there.)

SECONDARY / corroborating instrument: `loginctl show-user <user>
--property=Linger`, consulted only when the linger directory itself does
not exist. RULING 1 (the core defect this file exists to cover): rc=1 with
"is not logged in or lingering" is logind ANSWERING a definitive negative
-- measured on this host (`loginctl show-user nobody --property=Linger` ->
rc=1, "User ID 65534 is not logged in or lingering") -- not a failure to
answer. An EARLIER version of this check treated every nonzero exit as
"logind did not answer" and returned a silent `not-applicable`, exactly on
the population (a cron job, `systemd-run`, `sudo -u svc ...` -- no session,
no linger) the check exists to catch. That version's test baked the
misreading in (a stubbed "Failed to get user: No such process" asserted as
not-applicable, with no case for the real message at all); this file
distinguishes the two rc=1 message shapes explicitly.

The check lives as a SELF-CONTAINED function (`check_linger`) between the
`# >>> LINGER_CHECK` / `# <<< LINGER_CHECK` markers in update_framework.sh,
depending on no script-level state (no colors, no run_soft, no DRY_RUN, no
$step) -- exactly the pattern test_install_service_linger.py already uses
for install_service.sh's enable_linger(): extract the block verbatim and run
it standalone via subprocess, with fake `loginctl` placed first on PATH, so
the test exercises the actual shipped source rather than a reimplementation
that could drift from it.

The verdict-reaches-the-closing-banner tests below reuse the hermetic live-
execution harness from test_update_framework_live_execution.py (real script,
PATH-stubbed git/uv/systemctl/curl/loginctl, nothing real ever touched) --
see that file's module docstring for the full stub inventory. Because those
tests run the REAL script end to end, they hit its bare (no-argument)
`check_linger` call site directly, which -- since RULING 3 -- can no longer
be redirected via LINGER_DIR at all. They control the verdict via $USER
instead: a freshly-generated, guaranteed-absent username reads as a
DEFINITIVE "no" from the PRIMARY instrument alone whenever the real
/var/lib/systemd/linger directory exists on the host running the suite (and
falls through harmlessly to the still-PATH-stubbed loginctl -- also "no" --
on a host where it does not), so it is deterministic either way; a "yes"
verdict can only be produced truthfully for a user that genuinely has linger
enabled on the real host, which this suite cannot fabricate without root, so
those specific tests skip on a host where the ambient user has no real
marker file, rather than asserting something false.
"""
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

_BASH = shutil.which("bash") or "/bin/bash"
_REAL_LINGER_DIR = Path("/var/lib/systemd/linger")

sys.path.insert(0, os.path.dirname(__file__))

from test_update_framework_live_execution import (  # noqa: E402
    _make_live_sandbox,
    _stub_path_env,
    _run_live,
    _strip_ansi,
)

UPDATE_FRAMEWORK = (
    Path(__file__).parent.parent / "shared-memory" / "scripts" / "update_framework.sh"
)

BEGIN_MARKER = "# >>> LINGER_CHECK"
END_MARKER = "# <<< LINGER_CHECK"


def _extract_check_linger_source() -> str:
    text = UPDATE_FRAMEWORK.read_text()
    pattern = re.escape(BEGIN_MARKER) + r".*?\n(.*?)\n" + re.escape(END_MARKER)
    m = re.search(pattern, text, re.S)
    assert m, (
        f"could not find a {BEGIN_MARKER} ... {END_MARKER} block in "
        f"{UPDATE_FRAMEWORK} -- the extraction markers moved or were removed"
    )
    return m.group(1)


# `${LOGINCTL_MODE:-yes}` selects what the fake `loginctl show-user ...
# --property=Linger` answers with:
#   yes         -- rc=0, Linger=yes                    -> verdict "yes"
#   no          -- rc=0, Linger=no                      -> verdict "no"
#   no_session  -- rc=1, "...is not logged in or lingering" (the EXACT
#                  message measured on this host for a user with no session
#                  and no linger) -> verdict "no" (RULING 1)
#   unknown     -- rc=1, an UNRECOGNISED refusal (e.g. unknown user, no
#                  D-Bus) -> verdict "not-applicable" -- genuinely unanswered
#   empty       -- rc=0 but prints nothing (malformed property)
#                  -> verdict "not-applicable"
_LOGINCTL_STUB = """#!/usr/bin/env bash
case "$1" in
    show-user)
        case "${LOGINCTL_MODE:-yes}" in
            yes)        echo "Linger=yes"; exit 0 ;;
            no)         echo "Linger=no"; exit 0 ;;
            no_session) echo "Failed to get user: User ID 65534 is not logged in or lingering" >&2; exit 1 ;;
            unknown)    echo "Failed to get user: No such process" >&2; exit 1 ;;
            empty)      exit 0 ;;
        esac
        ;;
    *)
        echo "fake loginctl: unhandled args: $*" >&2
        exit 1
        ;;
esac
"""


def _run_check_linger(
    tmp_path: Path,
    env_overrides: dict,
    loginctl_present: bool = True,
    set_u: bool = False,
    linger_dir: Path | None = None,
) -> subprocess.CompletedProcess:
    source = _extract_check_linger_source()
    if set_u:
        source = "set -u\n" + source

    # RULING 3: the directory is a FUNCTION PARAMETER now, never an
    # environment read -- check_linger() ignores $LINGER_DIR entirely.
    # Isolate from THIS WORKSTATION's own real linger state (it is enabled
    # here) by passing an explicit path this sandbox never creates unless a
    # test supplies its own, forcing the PRIMARY (file-existence) branch to
    # be inapplicable by default.
    effective_dir = (
        linger_dir if linger_dir is not None else tmp_path / "no-such-linger-dir"
    )

    env = dict(os.environ)
    env.pop("USER", None)
    # A LINGER_DIR the test process's OWN environment happens to carry must
    # have zero effect -- popped here (in addition to the dedicated
    # test_linger_dir_env_var_has_no_effect_on_verdict below) so every
    # scenario in this file is proof the parameter, not the environment, is
    # what governs the verdict.
    env.pop("LINGER_DIR", None)

    if loginctl_present:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        loginctl = bin_dir / "loginctl"
        loginctl.write_text(_LOGINCTL_STUB)
        loginctl.chmod(loginctl.stat().st_mode | stat.S_IEXEC)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    else:
        # `loginctl` absent means absent from the WHOLE PATH -- an isolated,
        # empty directory, deliberately excluding the real PATH so this can
        # never accidentally fall through to a real loginctl on the host
        # running the suite.
        bin_dir = tmp_path / "empty_bin"
        bin_dir.mkdir(exist_ok=True)
        env["PATH"] = str(bin_dir)

    env.update(env_overrides)

    # The directory is passed as $1 to check_linger(), a real positional
    # argument -- never via the environment (RULING 3). bash -c's own
    # positional parameters start at $1 with the string after -c acting as
    # $0, so "check_linger" here is just a conventional $0 placeholder.
    return subprocess.run(
        [_BASH, "-c", source + '\ncheck_linger "$1"', "check_linger", str(effective_dir)],
        capture_output=True, text=True, timeout=15, env=env,
    )


# ── Extraction plumbing ───────────────────────────────────────────────────

def test_markers_present_exactly_once():
    text = UPDATE_FRAMEWORK.read_text()
    assert text.count(BEGIN_MARKER) == 1
    assert text.count(END_MARKER) == 1


def test_extracted_block_defines_the_function():
    source = _extract_check_linger_source()
    assert "check_linger()" in source


# ── RULING 3: the environment has ZERO influence on the verdict ─────────
#
# check_linger() used to read "${LINGER_DIR:-/var/lib/systemd/linger}"
# straight from the process environment -- so anyone who could set
# LINGER_DIR before a REAL (production) run of update_framework.sh could
# silently redirect the whole check to a directory of their choosing and
# make it answer "yes" regardless of the host's actual linger state. The
# fix makes the directory a function parameter instead; the call site in
# the shipped script passes none, so a real run always resolves the
# hardcoded literal. This test proves the environment variable is now
# inert by calling check_linger() BARE (no argument, exactly like the
# production call site) with LINGER_DIR pointed at an attacker-controlled
# directory carrying a marker file that WOULD flip the verdict to "yes" if
# the environment still had any effect, and comparing against an identical
# bare call with LINGER_DIR simply absent.

def test_linger_dir_env_var_has_no_effect_on_verdict(tmp_path):
    who = "probe-user-for-env-backdoor-test"

    # A directory an "attacker" (or a stale shell export) points LINGER_DIR
    # at, carrying exactly the marker file that would make the PRIMARY
    # instrument answer "yes" for `who` -- if the environment were still
    # consulted at all.
    attacker_dir = tmp_path / "attacker_controlled_linger_dir"
    attacker_dir.mkdir()
    (attacker_dir / who).touch()

    source = _extract_check_linger_source()
    # loginctl absent from PATH on both runs: if the environment variable
    # really has no effect, the bare call falls back to the hardcoded
    # /var/lib/systemd/linger literal, and the secondary instrument must
    # never be needed to explain either result below.
    empty_bin = tmp_path / "empty_bin"
    empty_bin.mkdir()

    def _bare_call(with_attacker_env: bool) -> str:
        env = dict(os.environ)
        env.pop("USER", None)
        env["USER"] = who
        env["PATH"] = str(empty_bin)
        if with_attacker_env:
            env["LINGER_DIR"] = str(attacker_dir)
        else:
            env.pop("LINGER_DIR", None)
        proc = subprocess.run(
            [_BASH, "-c", source + "\ncheck_linger"],  # bare call, no $1 -- matches production
            capture_output=True, text=True, timeout=15, env=env,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    baseline = _bare_call(with_attacker_env=False)
    attacked = _bare_call(with_attacker_env=True)

    assert attacked == baseline, (
        f"LINGER_DIR in the environment changed the bare check_linger() "
        f"verdict (baseline={baseline!r}, with LINGER_DIR set={attacked!r}) "
        f"-- the environment must have NO effect now that the directory is "
        f"a function parameter"
    )
    # And specifically: the marker file for $USER sitting in the
    # env-pointed directory must NOT have flipped the verdict to "yes" --
    # that outcome IS what the backdoor would have produced.
    assert attacked != "yes", (
        "an attacker-controlled LINGER_DIR pointing at a directory with a "
        "marker file for $USER still produced verdict 'yes' -- the "
        "environment backdoor is still live"
    )


# ── PRIMARY instrument: /var/lib/systemd/linger/<user> ──────────────────

def test_primary_file_present_is_yes(tmp_path):
    """The primary instrument alone is sufficient: the linger dir exists
    AND the user's file exists -> "yes", with loginctl deliberately ABSENT
    to prove no secondary consultation happens."""
    linger_dir = tmp_path / "linger"
    linger_dir.mkdir()
    (linger_dir / "tester").touch()
    proc = _run_check_linger(
        tmp_path, {"USER": "tester"}, loginctl_present=False, linger_dir=linger_dir,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "yes"


def test_primary_dir_present_file_absent_is_no(tmp_path):
    """The linger directory exists (this host's logind supports the
    mechanism at all) but carries no file for this user -- a DEFINITIVE
    "no" from the primary instrument alone, again with loginctl absent."""
    linger_dir = tmp_path / "linger"
    linger_dir.mkdir()
    proc = _run_check_linger(
        tmp_path, {"USER": "tester"}, loginctl_present=False, linger_dir=linger_dir,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no"


def test_primary_dir_absent_falls_back_to_secondary(tmp_path):
    """The linger directory itself does not exist -- can't tell "never
    enabled for anyone" from "no logind at all" by file test alone, so the
    function must fall through to loginctl."""
    proc = _run_check_linger(
        tmp_path,
        {"USER": "tester", "LOGINCTL_MODE": "yes"},
        linger_dir=tmp_path / "does-not-exist",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "yes"


# ── SECONDARY instrument (loginctl) — the three-way verdict ─────────────

def test_loginctl_absent_is_not_applicable(tmp_path):
    """`loginctl` missing from PATH entirely, and no primary file either --
    the framework must support non-systemd hosts without treating that as
    a failure."""
    proc = _run_check_linger(tmp_path, {"USER": "tester"}, loginctl_present=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "not-applicable"


def test_logind_answers_linger_on(tmp_path):
    proc = _run_check_linger(
        tmp_path, {"USER": "tester", "LOGINCTL_MODE": "yes"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "yes"


def test_logind_answers_linger_off(tmp_path):
    """logind ANSWERED (rc=0, Linger=no) -- the loud, non-fatal 'no' case,
    never 'not-applicable'."""
    proc = _run_check_linger(
        tmp_path, {"USER": "tester", "LOGINCTL_MODE": "no"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no"


def test_logind_rc1_not_logged_in_or_lingering_is_no(tmp_path):
    """RULING 1 -- the core defect. rc=1 with the exact message measured on
    this host ("User ID N is not logged in or lingering") is logind
    ANSWERING a definitive negative, not a failure to answer. Must be "no",
    never "not-applicable" -- the false green that used to cover exactly
    the population (a cron job, systemd-run, sudo -u svc ...) this check
    exists to catch."""
    proc = _run_check_linger(
        tmp_path, {"USER": "tester", "LOGINCTL_MODE": "no_session"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "no"


def test_logind_unrecognised_refusal_is_not_applicable(tmp_path):
    """`loginctl` is present and runs, but logind refuses the query with a
    message that is NOT the "is not logged in or lingering" negative (e.g.
    unknown user, no D-Bus) -- genuinely unanswered, must stay
    not-applicable rather than being misread as "no"."""
    proc = _run_check_linger(
        tmp_path, {"USER": "tester", "LOGINCTL_MODE": "unknown"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "not-applicable"


def test_logind_answers_empty_property_is_not_applicable(tmp_path):
    """logind exits 0 but the property comes back empty/malformed -- still
    "did logind answer usefully", not a linger failure."""
    proc = _run_check_linger(
        tmp_path, {"USER": "tester", "LOGINCTL_MODE": "empty"},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "not-applicable"


def test_bare_user_unset_falls_back_to_id_un_under_set_u(tmp_path):
    """${USER:-$(id -un)}, never bare $USER -- under `set -u` (which the
    real script runs with) an unbound USER is fatal regardless of `set -e`,
    and USER is unset under env -i / in some container contexts. USER is
    deliberately left unset here; the function must still complete."""
    proc = _run_check_linger(
        tmp_path, {"LOGINCTL_MODE": "yes"}, set_u=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "yes"


# ── RULING 6: bounded, never able to stall an upgrade ────────────────────

def test_loginctl_hang_is_bounded_by_timeout(tmp_path):
    """A check documented as read-only and non-fatal must not be able to
    stall an upgrade on a wedged or absent D-Bus. Stub loginctl to sleep
    well past the 5s bound and assert check_linger returns in bounded time
    rather than hanging for the stub's full sleep -- and that a timed-out
    call reads as the honest 'not-applicable' (genuinely unanswered), never
    'no'."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    loginctl = bin_dir / "loginctl"
    loginctl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        "    show-user) sleep 20; echo 'Linger=yes'; exit 0 ;;\n"
        "    *) exit 0 ;;\n"
        "esac\n"
    )
    loginctl.chmod(loginctl.stat().st_mode | stat.S_IEXEC)

    source = _extract_check_linger_source()
    env = dict(os.environ)
    env.pop("USER", None)
    env["USER"] = "tester"
    env.pop("LINGER_DIR", None)  # RULING 3: must have no effect either way
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    linger_dir = tmp_path / "no-such-linger-dir"

    start = time.monotonic()
    proc = subprocess.run(
        [_BASH, "-c", source + '\ncheck_linger "$1"', "check_linger", str(linger_dir)],
        capture_output=True, text=True, timeout=15, env=env,
    )
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 10, (
        f"check_linger took {elapsed:.1f}s -- the 'timeout 5' bound around "
        f"loginctl did not fire"
    )
    assert proc.stdout.strip() == "not-applicable", (
        f"a timed-out loginctl call must read as 'not-applicable' "
        f"(genuinely unanswered), got: {proc.stdout.strip()!r}"
    )


# ── The verdict reaches the CLOSING output of a full run ────────────────
#
# The extraction tests above only prove check_linger() itself is correct.
# They say nothing about whether the calling code in update_framework.sh
# actually surfaces a "no" verdict at every TERMINAL PATH of a run --
# exactly the "decorative ✗ buried mid-transcript" (or dropped entirely)
# failure mode the build brief calls out. These tests run the REAL script
# end to end (hermetic: git/uv/systemctl/curl/loginctl all PATH-stubbed,
# nothing real touched OR written -- see the $USER-based verdict control
# below, needed since RULING 3 removed the LINGER_DIR redirect these tests
# used to rely on).


def _force_no_verdict_user(env: dict) -> None:
    """RULING 3 follow-on: the real script's own `check_linger` call site
    passes no argument, so a full end-to-end run always consults the
    literal /var/lib/systemd/linger on whatever host runs this suite --
    there is no environment lever left to redirect it. Pick a freshly
    generated username that is certain to have no marker file there: on a
    host where that real directory exists (this workstation), the PRIMARY
    instrument alone answers a definitive "no" for it, without ever
    consulting loginctl; on a host where the directory does not exist, the
    check falls through to the (still PATH-stubbed) loginctl below, which
    is set to "no" too. Either way the verdict is "no", deterministically,
    and nothing is created or written -- this only ever reads a real path
    that exists (or doesn't) independently of the test."""
    env["USER"] = f"no-such-linger-user-{uuid.uuid4().hex[:8]}"


def _skip_unless_ambient_user_has_real_linger() -> None:
    """The mirror problem for a "yes" verdict: the PRIMARY instrument can
    only answer "yes" for a $USER that genuinely has a marker file under
    the real /var/lib/systemd/linger, which requires root to create and
    this suite will never write. A "yes"-verdict end-to-end test can
    therefore only run truthfully on a host where the AMBIENT user already
    has linger enabled for real (true on this workstation). On a host
    where that is not the case, skip rather than assert something this
    suite has no way to make true without touching real system state."""
    who = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not who or not (_REAL_LINGER_DIR / who).is_file():
        pytest.skip(
            "ambient user has no real systemd-linger marker on this host -- "
            "cannot exercise the PRIMARY instrument's 'yes' path end to end "
            "without root, now that RULING 3 removed the LINGER_DIR seam"
        )

def _write_loginctl_stub(env: dict, mode: str) -> None:
    """Overwrite the loginctl stub _stub_path_env() already placed on PATH
    (first PATH entry) with one that answers `mode` -- same PATH-stub
    mechanism the shared harness uses for git/uv/systemctl/curl, just
    aimed at a specific linger verdict for this test."""
    stub_dir = Path(env["PATH"].split(os.pathsep)[0])
    loginctl = stub_dir / "loginctl"
    loginctl.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$1\" in\n"
        f'    show-user) echo "Linger={mode}"; exit 0 ;;\n'
        "    *) exit 0 ;;\n"
        "esac\n"
    )
    st = loginctl.stat()
    loginctl.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_postflight_stub(repo: Path, log_path: Path, rc: int) -> None:
    """Overwrite the postflight.sh stand-in _make_live_sandbox() already
    placed at the exact REPO_ROOT-relative path the script invokes
    directly, so a live run's postflight step FAILS (rc != 0) instead of
    the sandbox's default always-succeeds stub."""
    postflight = repo / "shared-memory" / "scripts" / "postflight.sh"
    postflight.write_text(
        f'#!/usr/bin/env bash\necho "postflight.sh $*" >> "{log_path}"\nexit {rc}\n'
    )
    st = postflight.stat()
    postflight.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def test_live_run_linger_off_reaches_closing_banner_after_success(tmp_path):
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "no")
    _force_no_verdict_user(env)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    # Not just present anywhere -- present AFTER the success banner, so an
    # operator reading only the tail of the transcript still sees it.
    trailer = out.rsplit("Update complete and VERIFIED", 1)[1]
    assert "linger is NOT enabled" in trailer, (
        f"linger failure did not survive to the CLOSING banner (it must "
        f"appear after 'Update complete and VERIFIED', not just somewhere "
        f"mid-transcript):\n{out}"
    )
    # RULING 2: the claim must be CONDITIONAL, never asserted as fact --
    # true regardless of how this host actually supervises the gateway.
    # Whitespace-normalised because the message wraps across printed lines.
    normalized = " ".join(out.lower().split())
    assert "if the gateway runs as a systemd --user service" in normalized, out


def test_live_run_linger_on_closing_banner_is_quiet(tmp_path):
    """The non-failure verdict must not manufacture a warning."""
    _skip_unless_ambient_user_has_real_linger()
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "yes")

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "linger is NOT enabled" not in out, out


def test_live_run_linger_off_survives_agent_token_early_exit(tmp_path):
    """A missing AGENT_TOKEN makes the script exit 1 BEFORE postflight even
    runs (documented behaviour, unrelated to linger). A linger failure
    detected earlier in the same run must still be named there -- otherwise
    it is lost on exactly the run that never reaches the success banner at
    all."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "no")
    _force_no_verdict_user(env)
    env.pop("AGENT_TOKEN", None)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "UNVERIFIED" in out, out
    assert "Also: linger is NOT enabled" in out, out


def test_live_run_dry_run_still_checks_and_reports_linger(tmp_path):
    """Read-only, so it must run even under --dry-run -- it changes no
    system state and its answer does not depend on anything else the
    script would otherwise skip."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "no")
    _force_no_verdict_user(env)

    proc = _run_live(repo, env, "--dry-run", "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Dry run complete" in out, out
    assert "linger is NOT enabled" in out, out


def test_live_run_linger_off_reaches_postflight_failure_die_path(tmp_path):
    """RULING 0 -- the postflight-failure `die` path used to drop the
    linger verdict entirely, which is the run an operator is LEAST likely
    to scroll back on (they are staring at the failure). Assert the
    verdict is still reported when the run dies here."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "no")
    _force_no_verdict_user(env)
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "postflight FAILED" in out, out
    assert "linger is NOT enabled" in out, out


def test_live_run_linger_on_postflight_failure_die_path_is_quiet(tmp_path):
    """The non-failure verdict must not manufacture a warning on the
    postflight-failure path either."""
    _skip_unless_ambient_user_has_real_linger()
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "yes")
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "postflight FAILED" in out, out
    assert "linger is NOT enabled" not in out, out


def test_no_message_ever_points_at_a_step_number(tmp_path):
    """RULING 0 -- the linger check is no longer a numbered step, so no
    message about it may say "See Step N above" or "Step N above" (measured
    drift: one review found the banner said "See Step 8" while the step
    actually emitted was 9 in the default path and 8 only under
    --from-restore). This does NOT forbid "Step 8" appearing at all --
    postflight itself legitimately prints "── Step 8: postflight ..." as
    its own step header; only a BACKWARD reference to a step number is
    disallowed."""
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "no")
    _force_no_verdict_user(env)
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)
    lower = out.lower()

    assert "see step" not in lower, out
    assert "step 8 above" not in lower, out
    assert "step 9 above" not in lower, out


# ── RULING 5 (fix/uninstall-reverse-and-help): "yes" and "not-applicable"
# verdicts used to print NOTHING at any terminal path -- only "no" was ever
# reported, even though the v0.9.39 CHANGELOG already claimed all three were.
# A passing check and a check that silently never ran looked identical. The
# fix adds a brief (single-line, non-red) confirmation for "yes" via a new
# `_linger_brief()` helper, called from an `else` branch alongside every
# existing `if [[ "$LINGER_VERDICT" == "no" ]]` site. These tests prove the
# brief line actually reaches the two closing-banner terminal paths already
# covered above by the "quiet" tests -- "quiet" meant "no *warning*", not
# "no output at all", and that distinction was never actually exercised
# until now.


def test_live_run_linger_yes_closing_banner_shows_the_brief_confirmation(tmp_path):
    """The mirror of test_live_run_linger_on_closing_banner_is_quiet: no
    WARNING is manufactured (that test still holds unchanged), but the
    verdict must not be silent either -- a reader must be able to see the
    check ran and passed, not merely infer it from the absence of a
    complaint."""
    _skip_unless_ambient_user_has_real_linger()
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "yes")

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 0, out
    assert "Update complete and VERIFIED" in out, out
    assert "linger is NOT enabled" not in out, out  # still no false warning
    trailer = out.rsplit("Update complete and VERIFIED", 1)[1]
    assert "linger check: enabled" in trailer, (
        f"a passing linger check produced NO confirmation at the closing "
        f"banner -- indistinguishable from a check that never ran:\n{out}"
    )


def test_live_run_linger_yes_postflight_failure_die_path_shows_the_brief_confirmation(tmp_path):
    """Same property on the die() terminal path, which builds its message
    differently (the "no" branch embeds linger text INSIDE the die() string
    itself, so the "yes"/not-applicable" branch has to print its own line
    BEFORE calling die -- easy to get backwards)."""
    _skip_unless_ambient_user_has_real_linger()
    repo, log_path = _make_live_sandbox(tmp_path)
    env = _stub_path_env(tmp_path, log_path)
    _write_loginctl_stub(env, "yes")
    _write_postflight_stub(repo, log_path, rc=1)

    proc = _run_live(repo, env, "--skip-backup")
    out = _strip_ansi(proc.stdout + proc.stderr)

    assert proc.returncode == 1, out
    assert "postflight FAILED" in out, out
    assert "linger is NOT enabled" not in out, out
    assert "linger check: enabled" in out, out
