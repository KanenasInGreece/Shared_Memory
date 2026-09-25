"""bootstrap_tokens.sh --reveal from a real terminal, end to end.

v1.0.4 refused every --reveal through the wrapper: the wrapper runs the mint
inside $(…) to parse the registry lines, and there the mint's stdout is always a
pipe (fact:2759). The wrapper now hands the mint a copy of its own stdout as fd 3
(--reveal-fd 3), and only the reveal block goes there, so the token never enters
the captured output (decision:2762).

These tests run the SHIPPED wrapper from a copy under tmp_path (the harness of
test_bootstrap_tokens_registry.py, LOCAL_SKILL_ENV_PATHS patched onto tmp_path),
once on a pseudo-terminal and once on a pipe. ⛔ Never the real entry point: a
mint writes live skill .env files (fact:1471).

Failure modes, named before the tests (decision:2671):
  F1 a reveal from a real terminal is refused (the v1.0.4 defect)
  F2 a reveal whose output is captured is unlocked
  F3 the printed token is not the one the registry accepts
  F4 the token reaches an xtrace log
  F5 one branch (add, remint, bulk) keeps a defect the others lost
  F6 a mismatched --reveal name overwrites an agent's .env and leaves it unregistered
  F7 a closed or read-only --reveal-fd fails only after the token exists
  F8 the wrapper discards its own stderr, hiding the bulk refusal (bare exec at the mint lock, 0.9.35-1.0.4)

Every scenario writes a JSON record (never the token) to SM_E2E_ARTIFACT_DIR, or
to tmp_path when that is unset, so the outcome can be read without pytest.
"""
import hashlib
import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_bootstrap_tokens_registry import _env_path, _make_fake_root  # noqa: E402

TOKEN_LINE = re.compile(r"(\w+): AGENT_TOKEN=(\S+)")
OVERRIDE = "SHARED_MEMORY_ALLOW_REVEAL_WITHOUT_TTY"
TIMEOUT = 60


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _env() -> dict:
    # An exported override would make every refusal test measure nothing.
    return {k: v for k, v in os.environ.items() if k != OVERRIDE}


def _script(root: Path) -> str:
    return str(root / "shared-memory" / "scripts" / "bootstrap_tokens.sh")


def _run_on_pty(root: Path, args: list, bash_flags: "list[str] | None" = None,
                stderr_path: "Path | None" = None) -> "tuple[int, str]":
    """The wrapper with stdout (and stderr, unless redirected) on a pseudo-terminal,
    as in an operator's own shell. Drains the terminal while the child runs."""
    master, slave = os.openpty()
    err = open(stderr_path, "wb") if stderr_path else slave
    try:
        proc = subprocess.Popen(
            ["bash", *(bash_flags or []), _script(root), *args],
            cwd=root, env=_env(), stdin=subprocess.DEVNULL, stdout=slave, stderr=err,
            close_fds=True,
        )
    finally:
        os.close(slave)
        if stderr_path:
            err.close()
    chunks, deadline = [], time.monotonic() + TIMEOUT
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if not ready:
                if proc.poll() is not None:
                    break
                continue
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO on Linux: every writer has closed the terminal
                break
            if not data:
                break
            chunks.append(data)
        rc = proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        # The child can write and exit between a select timeout and poll(); take what is left.
        while select.select([master], [], [], 0)[0]:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    return rc, b"".join(chunks).decode("utf-8", "replace")


def _run_on_pipe(root: Path, args: list) -> "tuple[int, str]":
    p = subprocess.run(["bash", _script(root), *args], cwd=root, env=_env(),
                       stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=TIMEOUT)
    return p.returncode, p.stdout + p.stderr


def _registry(root: Path) -> dict:
    line = next(l for l in _env_path(root).read_text().splitlines()
                if l.startswith("AGENT_TOKENS="))
    out = {}
    for entry in line[len("AGENT_TOKENS="):].split(","):
        name, _, digest = entry.partition(":")
        out[name] = digest
    return out


def _revealed(out: str, name: str) -> "str | None":
    found = [tok for n, tok in TOKEN_LINE.findall(out) if n == name]
    return found[0] if len(found) == 1 else None


def _artifact(tmp_path: Path, scenario: str, **fields) -> None:
    d = Path(os.environ.get("SM_E2E_ARTIFACT_DIR") or tmp_path / "e2e-artifacts")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.json").write_text(
        json.dumps({"scenario": scenario, **fields}, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def root(tmp_path):
    """A fake install with one local agent (claude) and one remote (remote1),
    both registered, before any test's own command runs."""
    local = tmp_path / "claude_skill"
    local.mkdir()
    fake = _make_fake_root(tmp_path, {"claude": str(local / ".env")})
    _env_path(fake).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake).write_text("PG_PASSWORD=fake\n")
    rc, out = _run_on_pipe(fake, [])
    assert "AGENT_TOKENS=" in _env_path(fake).read_text(), out
    rc, out = _run_on_pipe(fake, ["--add", "remote1"])
    assert rc == 0, out
    return fake


def _check_reveal(tmp_path, root, scenario, args, name):
    before = _registry(root)
    rc, out = _run_on_pty(root, args)
    token = _revealed(out, name)
    after = _registry(root)
    others = ({k: v for k, v in before.items() if k != name}
              == {k: v for k, v in after.items() if k != name})
    match = token is not None and after.get(name) == f"sha256:{_sha(token)}"
    _artifact(tmp_path, scenario, exit=rc, token_printed=token is not None,
              printed_token_sha256=_sha(token) if token else None,
              registry_digest=after.get(name), match=match,
              others_unchanged=others)
    return rc, out, token, before, after, others, match


def test_add_reveal_on_a_terminal_registers_the_printed_token(root, tmp_path):
    rc, out, token, _b, _a, others, match = _check_reveal(
        tmp_path, root, "add_reveal_pty", ["--add", "remote2", "--reveal", "remote2"], "remote2")
    assert rc == 0, out
    assert match, out
    assert others


def test_remint_reveal_on_a_terminal_replaces_only_that_digest(root, tmp_path):
    """The command that was refused at 1.0.4."""
    rc, out, token, before, after, others, match = _check_reveal(
        tmp_path, root, "remint_reveal_pty", ["--remint", "remote1", "--reveal", "remote1"], "remote1")
    assert rc == 0, out
    assert match, out
    assert after["remote1"] != before["remote1"]
    assert others


def test_bulk_force_reveal_on_a_terminal_registers_the_printed_token(root, tmp_path):
    rc, out, token, _b, _a, _o, match = _check_reveal(
        tmp_path, root, "bulk_reveal_pty", ["--force", "--reveal", "remote1"], "remote1")
    # 2 is the wrapper's PARTIAL FAILURE exit for roster names with no fixture directory; the reveal is what is under test.
    assert rc in (0, 2), out
    assert match, out


@pytest.mark.parametrize("args", [
    ["--add", "remote2", "--reveal", "remote2"],
    ["--remint", "remote1", "--reveal", "remote1"],
    ["--force", "--reveal", "remote1"],
], ids=["add", "remint", "bulk"])
def test_a_captured_wrapper_refuses_and_changes_nothing(root, tmp_path, args):
    gateway = _env_path(root).read_bytes()
    skill = (tmp_path / "claude_skill" / ".env").read_bytes()
    rc, out = _run_on_pipe(root, args)
    unchanged = (_env_path(root).read_bytes() == gateway
                 and (tmp_path / "claude_skill" / ".env").read_bytes() == skill)
    _artifact(tmp_path, f"pipe_refused_{args[0].lstrip('-')}", exit=rc,
              refused="REFUSED" in out, token_printed=bool(TOKEN_LINE.search(out)),
              env_unchanged=unchanged)
    assert rc != 0, out
    # On the bulk branch the refusal reaches the caller only on the wrapper's own stderr (F8).
    assert "REFUSED" in out
    assert not TOKEN_LINE.search(out)
    assert unchanged


def test_an_xtrace_log_never_holds_the_token(root, tmp_path):
    """bash -x with stdout on the terminal and the trace in a file: what an operator
    types while debugging a refusal."""
    trace = tmp_path / "trace.log"
    rc, out = _run_on_pty(root, ["--remint", "remote1", "--reveal", "remote1"],
                          bash_flags=["-x"], stderr_path=trace)
    token = _revealed(out, "remote1")
    leaked = token is not None and token in trace.read_text(errors="replace")
    _artifact(tmp_path, "xtrace_pty", exit=rc, token_printed=token is not None,
              trace_bytes=trace.stat().st_size, token_in_trace=leaked)
    assert rc == 0 and token, out
    assert "AGENT_TOKEN=" not in trace.read_text(errors="replace")
    assert not leaked


def test_a_mismatched_reveal_name_leaves_the_agent_and_registry_untouched(root, tmp_path):
    skill_env = tmp_path / "claude_skill" / ".env"
    gateway, skill = _env_path(root).read_bytes(), skill_env.read_bytes()
    rc, out = _run_on_pty(root, ["--remint", "claude", "--install-path", str(skill_env),
                                 "--reveal", "claud"])
    unchanged = _env_path(root).read_bytes() == gateway and skill_env.read_bytes() == skill
    _artifact(tmp_path, "mismatched_reveal_name_pty", exit=rc, env_unchanged=unchanged,
              token_printed=bool(TOKEN_LINE.search(out)))
    assert rc != 0, out
    assert "cannot show a token for: claud" in out
    assert unchanged


def test_a_closed_reveal_fd_refuses_before_minting(root, tmp_path):
    """The override skips the terminal check, so the descriptor's existence is
    checked on its own. Run against the fake root's copy, never the real one."""
    gateway = _env_path(root).read_bytes()
    env = _env()
    env[OVERRIDE] = "1"
    p = subprocess.run(
        ["uv", "run", "python", str(root / "shared-memory" / "scripts" / "generate_tokens.py"),
         "--add", "remote3", "--reveal", "remote3", "--reveal-fd", "57"],
        cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=TIMEOUT,
    )
    out = p.stdout + p.stderr
    unchanged = _env_path(root).read_bytes() == gateway
    _artifact(tmp_path, "closed_reveal_fd", exit=p.returncode, env_unchanged=unchanged,
              token_printed=bool(TOKEN_LINE.search(out)))
    assert p.returncode == 1, out
    assert "--reveal-fd 57 is not an open, writable file descriptor" in out
    assert "AGENT_TOKENS=" not in p.stdout
    assert unchanged


def test_a_read_only_reveal_fd_refuses_before_minting(root, tmp_path):
    gateway = _env_path(root).read_bytes()
    env = _env()
    env[OVERRIDE] = "1"
    ro = os.open(os.devnull, os.O_RDONLY)
    try:
        p = subprocess.run(
            ["uv", "run", "python", str(root / "shared-memory" / "scripts" / "generate_tokens.py"),
             "--add", "remote3", "--reveal", "remote3", "--reveal-fd", str(ro)],
            cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=TIMEOUT, pass_fds=(ro,),
        )
    finally:
        os.close(ro)
    out = p.stdout + p.stderr
    unchanged = _env_path(root).read_bytes() == gateway
    _artifact(tmp_path, "read_only_reveal_fd", exit=p.returncode, env_unchanged=unchanged,
              token_printed=bool(TOKEN_LINE.search(out)))
    assert p.returncode == 1, out
    assert "is not an open, writable file descriptor" in out
    assert "AGENT_TOKENS=" not in p.stdout
    assert unchanged
