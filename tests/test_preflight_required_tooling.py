"""preflight.sh names the tools the shipped scripts actually run.

WHY THIS EXISTS. preflight verified docker, uv and git and stopped there, while
the shipped scripts also execute curl, python3, timeout, gzip, gunzip,
sha256sum and flock. A host missing one passed preflight and failed later,
elsewhere, reporting whatever the script happened to be doing rather than the
missing tool.

THE SPLIT IS BY WHAT BREAKS. curl, python3 and timeout carry the verification
surface and postflight guards none of them, so their absence FAILS. The backup
and restore four WARN under Recommended and name what is lost, because the
gateway runs without them and that trade is the operator's to accept.

THE CLAIMS ARE PINNED, AND THE FIRST DRAFT GOT ONE WRONG. It asserted that
nothing shipped runs node/npm/npx; mcp/mcp.json launches two servers with npx.
The test that was supposed to prove it walked only *.sh under scripts/ and ops/,
so the file that falsified the claim was invisible to it — the check read as
proof and was not one. _shipped_files() therefore covers mcp/ and *.json too.

⚠ THE DETECTOR IS ONE-DIRECTIONAL, and this is the part that was stated
wrongly before. `_invokes` is a heuristic, not a shell parser: it drops
comments and lines whose first word is an output command, then looks for the
tool as a bare word. A FALSE POSITIVE (prose read as a call) makes _callers()
too big. For test_node_claim_matches_what_is_actually_shipped (`assert not
_callers`) that fails loudly, which is the safe direction. For
test_no_message_names_a_script_that_does_not_call_the_tool (`assert named <=
callers`) it fails QUIETLY — a bigger callers set makes a wrong claim pass. So the second test is a guard against typos and stale names, NOT proof
that a named script really calls the tool; only reading it proves that.

Two false positives were measured here and are handled: `ask_secret 'e.g.
openssl rand -hex 20'` (prose in single quotes) and `QUIESCE_MODE="timeout"`
(a bare value in double quotes).

WHAT IS STILL UNCOVERED, and this list is not claimed to be complete — the
instrument has been wrong in BOTH directions across three rounds, so treat it
as a useful filter rather than an authority. Known gaps: a real command inside
a literal (`sh -c 'curl ...'`); a tool reached through a variable or an alias;
a tool named in a heredoc. Anything beyond a bare `"tool"` inside double quotes
is deliberately NOT scrubbed, because the general version of that scrub was
tried and reverted — shell quoting nests inside `$( )`, so it paired quotes
across spans and deleted real code, dropping backup.sh from awk's callers.

⚠ NO LIVE INFRASTRUCTURE. Assertions are on preflight's STDOUT markers, never
its exit code: the exit code folds in docker reachability and a populated
.env, so asserting on it would make these fail on any un-provisioned host and
pass tautologically on a broken one. tests/test_preflight_uv_path_check.py sets
this precedent.
"""
import os
import re
import subprocess

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..")
_PREFLIGHT = os.path.join(_REPO, "shared-memory", "scripts", "preflight.sh")
_SCRIPT_DIRS = (os.path.join(_REPO, "shared-memory", "scripts"),
                os.path.join(_REPO, "shared-memory", "ops"))
_EXTRA_SHIPPED = (os.path.join(_REPO, "mcp", "mcp.json"),)

# Anything whose first word emits prose to the operator. Beyond the obvious
# echo/printf, this project defines its own colour and prompt helpers, and the
# prompt helpers are where the openssl false positive lives.
_OUTPUT_CMDS = ("echo", "printf", "cat", "red", "grn", "ylw", "note",
                "warn", "need", "ok", "bad", "die",
                "ask", "ask_required", "ask_secret", "yesno")


def _shipped_files():
    """Every shipped file a tool claim could be true or false about — not just
    *.sh. mcp/mcp.json is the file that falsified the first node claim."""
    for d in _SCRIPT_DIRS:
        for name in sorted(os.listdir(d)):
            if name.endswith(".sh"):
                yield name, os.path.join(d, name)
    for path in _EXTRA_SHIPPED:
        if os.path.exists(path):
            yield os.path.basename(path), path


def _invokes(path, tool):
    """Does this file actually RUN `tool`? See the one-directional limit above."""
    word = re.compile(r"(?:^|[^A-Za-z0-9_./-])" + re.escape(tool) + r"(?=[\s\"',]|$)")
    if path.endswith(".json"):
        # A JSON launcher names its binary in a "command" field.
        import json
        try:
            cfg = json.load(open(path, encoding="utf-8"))
        except ValueError:
            return False
        blob = json.dumps(cfg)
        return bool(re.search(r'"command"\s*:\s*"' + re.escape(tool) + r'"', blob))
    # Single-quoted spans are LITERAL in POSIX sh — no substitution happens
    # inside them, so a tool name there is prose, not a call. This is what the
    # first-word check could not catch: the openssl false positive lives inside
    # `VAR="$(ask_secret 'e.g. openssl rand -hex 20')"`, whose first word is the
    # assignment. (Limit: `sh -c 'real command'` would be missed. No tool this
    # file checks is invoked that way — asserted by the "really invoked" test.)
    # Single-quoted spans are LITERAL in POSIX sh and cannot nest or be
    # escaped, so pairing them left-to-right is exact. This is what catches
    # prose like `ask_secret 'e.g. openssl rand -hex 20'`.
    literal = re.compile(r"'[^']*'")
    # Double quotes get a DELIBERATELY NARROW rule: drop only a span that is
    # exactly the tool name, which is the one false-positive shape measured
    # here (QUIESCE_MODE="timeout" in backup.sh). A general double-quote
    # scrubber was tried and REVERTED: shell quoting nests inside $( ), so
    # `pg_sha="$(sha256sum "$base.pgdump" | awk '{print $1}')"` had its
    # closing quote paired with the next opening one, deleting `| awk` and
    # silently dropping backup.sh from awk's callers. That is a false
    # NEGATIVE, which shrinks the caller set and makes `assert not _callers`
    # pass quietly — the same blindness that let the first node claim through.
    bare_value = re.compile(r'"\s*' + re.escape(tool) + r'\s*"')
    for line in open(path, encoding="utf-8"):
        line = bare_value.sub(" ", literal.sub(" ", line))
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split()[0].strip("!(){}") in _OUTPUT_CMDS:
            continue
        if re.sub(r"command\s+-v\s+" + re.escape(tool), " ", line) != line:
            continue          # `command -v X` ASKS whether X exists
        if word.search(line):
            return True
    return False


def _callers(tool, exclude=("preflight.sh",)):
    """Shipped files that invoke `tool`. preflight.sh is excluded by default:
    it is the CHECKER, so running `node --version` to report a version is not a
    dependency on node."""
    return {name for name, path in _shipped_files()
            if name not in exclude and _invokes(path, tool)}


def _hide(tmp_path, *hidden):
    """A PATH farm mirroring the real one minus `hidden`, so `command -v`
    genuinely fails for those names while everything else still resolves."""
    farm = tmp_path / "farm"
    farm.mkdir(exist_ok=True)
    seen = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name in hidden or name in seen:
                continue
            seen.add(name)
            try:
                os.symlink(os.path.join(d, name), farm / name)
            except OSError:
                pass
    env = dict(os.environ)
    env["PATH"] = str(farm)
    return env


def _run(env):
    return subprocess.run(["bash", _PREFLIGHT], capture_output=True, text=True,
                          env=env, cwd=_REPO, timeout=120)


# ── the three that fail the run: reported as ✗, and inside Required ──────────

@pytest.mark.parametrize("tool", ["curl", "python3", "timeout"])
def test_a_missing_core_tool_is_reported_as_a_failure(tmp_path, tool):
    out = _run(_hide(tmp_path, tool)).stdout
    line = next((l for l in out.split("\n") if f"{tool} not found" in l), None)
    assert line, f"preflight did not report a missing {tool}:\n{out}"
    assert "✗" in line, (
        f"a missing {tool} must be a ✗, not a warning — postflight runs through "
        f"it and guards it nowhere, so an install cannot be proven without it"
    )
    assert out.index(line) < out.index("Recommended:"), (
        f"the {tool} failure printed under Recommended; it is a Required check"
    )


# ── the four that cost backup/restore: warnings, under Recommended ───────────

@pytest.mark.parametrize("tool", ["gzip", "gunzip", "sha256sum", "flock"])
def test_a_missing_backup_tool_warns_under_recommended(tmp_path, tool):
    out = _run(_hide(tmp_path, tool)).stdout
    line = next((l for l in out.split("\n") if f"{tool} not found" in l), None)
    assert line, f"preflight said nothing about a missing {tool}:\n{out}"
    assert "✗" not in line, (
        f"a missing {tool} must not be a ✗: the gateway runs without it, and "
        f"failing here removes the operator's choice"
    )
    assert re.search(r"backup|restore", line, re.I), (
        f"the {tool} warning does not name what stops working — an operator "
        f"cannot make an informed choice from a bare 'not found'"
    )
    assert out.index(line) > out.index("Recommended:"), (
        f"the optional {tool} finding printed under Required:"
    )


# ── the messages must keep telling the truth about the code ──────────────────

_CHECKED = ["curl", "python3", "timeout", "gzip", "gunzip", "sha256sum", "flock"]


@pytest.mark.parametrize("tool", _CHECKED)
def test_the_tool_is_really_invoked_by_a_shipped_file(tool):
    assert _callers(tool), (
        f"preflight checks for {tool}, but no shipped file invokes it — either "
        f"the check is obsolete or the detector needs revisiting"
    )


@pytest.mark.parametrize("tool", _CHECKED)
def test_no_message_names_a_script_that_does_not_call_the_tool(tool):
    """Catches typos and stale script names. NOT proof of the positive claim —
    see the one-directional note in the module docstring."""
    src = open(_PREFLIGHT, encoding="utf-8").read()
    line = next((l for l in src.split("\n") if f"{tool} not found" in l), None)
    assert line, f"no message for {tool} in preflight.sh"
    named = set(re.findall(r"(?:ops/)?([a-z_0-9]+\.sh)", line))
    assert named, f"the {tool} message names no script — say what breaks"
    callers = _callers(tool)
    assert named <= callers, (
        f"preflight's {tool} message names {sorted(named - callers)}, which do "
        f"not call {tool}. Measured callers: {sorted(callers)}"
    )


def test_node_claim_matches_what_is_actually_shipped():
    """The first draft claimed nothing shipped runs node/npm/npx. mcp/mcp.json
    launches two servers with npx. The claim now has to survive a detector that
    can actually see that file."""
    src = open(_PREFLIGHT, encoding="utf-8").read()
    assert src.index("command -v node") > src.index('echo "Recommended:"'), (
        "the node check sits in Required — that claims a dependency no "
        "framework script has"
    )
    assert "No framework or helper script runs node" in src, (
        "the node rationale must be the narrow, true one"
    )
    npx_users = _callers("npx")
    assert "mcp.json" in npx_users, (
        "mcp/mcp.json launches servers with npx — if that stopped being true "
        "the wording around it should change too"
    )
    for tool in ("node", "npm", "npx"):
        shell_callers = {n for n in _callers(tool) if n.endswith(".sh")}
        assert not shell_callers, (
            f"{sorted(shell_callers)} now invoke {tool} — a shell script needing "
            f"node makes it more than a Recommended check"
        )
