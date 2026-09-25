"""Following the recovery command the mint prints must leave a working agent.

generate_tokens.py only PRINTS the gateway's registry lines; bootstrap_tokens.sh
is what writes them into the gateway .env. A printed recovery that named the
direct generate_tokens.py call wrote the agent's new token into its .env and left
the gateway holding the old digest, so a working agent started failing auth.

Each test provokes a refusal through the shipped wrapper (a copy under tmp_path,
the harness of test_bootstrap_tokens_registry.py), extracts the command the
refusal prints, runs it exactly as printed, and checks the agent's token against
the gateway's digest. ⛔ Never the real entry point (fact:1471).

Failure modes (decision:2671):
  F1 the REFUSED hint (skill directory missing) leaves the new agent unregistered
  F2 the already-registered hint (--add of a known name) leaves the re-issued token unregistered
  F3 the missing-directory hint drops --mcp, re-registering a connector as a CLI skill install
Mutations: restore either printed command to the generate_tokens.py form, or drop the kind flag -> its test dies.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_bootstrap_tokens_registry import _env_path, _make_fake_root  # noqa: E402

HINT = re.compile(r"(bootstrap_tokens\.sh|generate_tokens\.py) (--(?:add|remint) \S+(?: --mcp)? --install-path \S+)")


def _run(root: Path, argv: list) -> "tuple[int, str]":
    env = {k: v for k, v in os.environ.items() if k != "SHARED_MEMORY_ALLOW_REVEAL_WITHOUT_TTY"}
    p = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def _wrapper(root: Path, args: list) -> "tuple[int, str]":
    return _run(root, ["bash", str(root / "shared-memory" / "scripts" / "bootstrap_tokens.sh"), *args])


def _follow(root: Path, out: str) -> "tuple[str, int, str]":
    """Run the printed recovery exactly as printed: the wrapper for bootstrap_tokens.sh, the mint for generate_tokens.py."""
    m = HINT.search(out)
    assert m, f"no recovery command printed:\n{out}"
    tool, args = m.group(1), m.group(2).split()
    if tool == "bootstrap_tokens.sh":
        rc, followed = _wrapper(root, args)
    else:
        rc, followed = _run(root, ["uv", "run", "python",
                                   str(root / "shared-memory" / "scripts" / "generate_tokens.py"), *args])
    return f"{tool} {m.group(2)}", rc, followed


def _token_in(env_file: Path) -> str:
    return next(l.split("=", 1)[1].strip() for l in env_file.read_text().splitlines()
                if l.startswith("AGENT_TOKEN="))


def _registered(root: Path, name: str) -> "str | None":
    line = next((l for l in _env_path(root).read_text().splitlines() if l.startswith("AGENT_TOKENS=")), "")
    for entry in line[len("AGENT_TOKENS="):].split(","):
        n, _, digest = entry.partition(":")
        if n == name:
            return digest
    return None


def _artifact(tmp_path: Path, scenario: str, **fields) -> None:
    d = Path(os.environ.get("SM_E2E_ARTIFACT_DIR") or tmp_path / "e2e-artifacts")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{scenario}.json").write_text(json.dumps({"scenario": scenario, **fields}, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def root(tmp_path):
    local = tmp_path / "claude_skill"
    local.mkdir()
    fake = _make_fake_root(tmp_path, {"claude": str(local / ".env")})
    _env_path(fake).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake).write_text("PG_PASSWORD=fake\n")
    _wrapper(fake, [])
    assert _registered(fake, "claude"), "fixture: the bulk mint did not register claude"
    return fake


def test_following_the_missing_directory_advice_registers_the_agent(root, tmp_path):
    skill_env = tmp_path / "newagent_skill" / ".env"
    rc, out = _wrapper(root, ["--add", "newagent", "--install-path", str(skill_env)])
    assert rc != 0 and "does not exist" in out, out

    skill_env.parent.mkdir()
    command, rc2, followed = _follow(root, out)
    match = (rc2 == 0 and skill_env.exists()
             and _registered(root, "newagent") == f"sha256:{hashlib.sha256(_token_in(skill_env).encode()).hexdigest()}")
    _artifact(tmp_path, "follow_missing_directory_advice", command=command, exit=rc2, match=match)
    assert rc2 == 0, followed
    assert match, f"following `{command}` left newagent's token unregistered:\n{followed}"


def test_following_the_missing_directory_advice_keeps_an_mcp_install_an_mcp_install(root, tmp_path):
    """F3: the printed command dropped --mcp, so the connector re-registered as a CLI skill install."""
    skill_env = tmp_path / "connector" / ".env"
    rc, out = _wrapper(root, ["--add", "conn", "--mcp", "--install-path", str(skill_env)])
    assert rc != 0 and "does not exist" in out, out

    skill_env.parent.mkdir()
    command, rc2, followed = _follow(root, out)
    installs = next((l for l in _env_path(root).read_text().splitlines()
                     if l.startswith("AGENT_INSTALLS=")), "")
    kept = f"conn:mcp:{skill_env}" in installs
    _artifact(tmp_path, "follow_missing_directory_advice_mcp", command=command, exit=rc2, kind_kept=kept)
    assert rc2 == 0, followed
    assert kept, f"following `{command}` did not register conn as an MCP install:\n{installs}"


def test_following_the_already_registered_advice_reissues_a_working_token(root, tmp_path):
    skill_env = tmp_path / "claude_skill" / ".env"
    old = _token_in(skill_env)
    rc, out = _wrapper(root, ["--add", "claude", "--install-path", str(skill_env)])
    assert rc != 0 and "already registered" in out, out

    command, rc2, followed = _follow(root, out)
    new = _token_in(skill_env)
    match = rc2 == 0 and _registered(root, "claude") == f"sha256:{hashlib.sha256(new.encode()).hexdigest()}"
    _artifact(tmp_path, "follow_already_registered_advice", command=command, exit=rc2,
              rotated=new != old, match=match)
    assert rc2 == 0, followed
    assert new != old
    assert match, f"following `{command}` left claude holding a token the gateway rejects:\n{followed}"
