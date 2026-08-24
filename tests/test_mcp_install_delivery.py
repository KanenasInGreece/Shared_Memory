"""sync_skills.sh is KIND-AWARE — an `mcp` install gets the connector, never the skill.

Executable, not source-reading: every test drives the REAL shipped script
against a throwaway tree. HOME is redirected so the historical hardcoded
candidate directories cannot resolve to this machine's own installs,
SHARED_MEMORY_ENV_FILE points at a fixture registry, and
SHARED_MEMORY_SYNC_SKIP_TRACKED=1 keeps phase 1 from writing into the repo's own
tracked skill copy. COORDINATOR_URL is pointed at a closed port so the gateway
compatibility probe takes its unreachable branch instead of contacting whatever
happens to be listening on :8888 during a test run.

⛔ THE HAZARD THIS FILE EXISTS FOR IS REAL AND WAS MEASURED. Before install
kinds, an MCP connector registered its walled `.env` in AGENT_INSTALLS like any
other agent, and sync delivered the CLI skill package into it: SKILL.md and
memory_bridge.py, which no MCP host can run, dumped beside a live bearer token.
"""
import os
import subprocess

_REPO = os.path.join(os.path.dirname(__file__), "..")
_SYNC = os.path.join(_REPO, "shared-memory", "scripts", "sync_skills.sh")

# What an `mcp` install must receive — and nothing else.
_MCP_FILES = ("vector-skill.py", "CONSTITUTION_SNIPPET_MCP.md", "system-prompt.md")
# What it must NEVER receive: the CLI package, and the config TEMPLATE.
_FORBIDDEN = ("SKILL.md", "MANIFEST.txt", "CONSTITUTION_SNIPPET.md", "mcp.json",
              "scripts/memory_bridge.py", "scripts/update_skill.sh",
              "Documentation/schema.md", ".env.example")


def _run_sync(tmp_path, registry_env, extra_args=()):
    env = dict(os.environ)
    env.pop("SHARED_MEMORY_SYNC_AGENTS", None)   # the registry must be what decides
    env["SHARED_MEMORY_ENV_FILE"] = str(registry_env)
    env["SHARED_MEMORY_SYNC_SKIP_TRACKED"] = "1"
    env["HOME"] = str(tmp_path / "home")
    env["COORDINATOR_URL"] = "http://127.0.0.1:1"
    os.makedirs(env["HOME"], exist_ok=True)
    return subprocess.run(["bash", _SYNC, *extra_args], capture_output=True,
                          text=True, env=env, cwd=_REPO, timeout=180)


def _mcp_registry(tmp_path, walled):
    reg = tmp_path / "gateway.env"
    reg.write_text(f"AGENT_INSTALLS=opencode:mcp:{walled}/.env\n")
    return reg


def test_an_mcp_install_receives_exactly_the_three_connector_files(tmp_path):
    walled = tmp_path / "walled"
    walled.mkdir()
    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr

    for rel in _MCP_FILES:
        assert (walled / rel).is_file(), (
            f"{rel} was not delivered to the mcp install:\n{result.stdout}")


def test_an_mcp_install_never_receives_the_cli_skill_package(tmp_path):
    """The measured hazard, asserted directly."""
    walled = tmp_path / "walled"
    walled.mkdir()
    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr

    landed = [rel for rel in _FORBIDDEN if (walled / rel).exists()]
    assert not landed, (
        f"the CLI skill package (or the mcp.json template) was delivered into an "
        f"MCP install: {landed}. mcp.json carries YOUR_* placeholders and a repo "
        f"path; the CLI package is a skill no MCP host can run, beside a live "
        f"token.\n{result.stdout}")


def test_an_mcp_install_keeps_its_token_env_untouched(tmp_path):
    """The token .env is the one file in a walled install that sync must never
    write. It holds the plaintext the mint wrote through; there is no version of
    it to merge in and nothing to copy over it."""
    walled = tmp_path / "walled"
    walled.mkdir()
    env_file = walled / ".env"
    env_file.write_text("AGENT_TOKEN=tok_fixture_not_a_real_token\n")
    os.chmod(env_file, 0o600)
    before = env_file.read_bytes()

    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.read_bytes() == before, "sync rewrote an MCP install's token .env"


def test_an_mcp_install_ends_at_dir_700_and_files_600(tmp_path):
    walled = tmp_path / "walled"
    walled.mkdir(mode=0o755)
    os.chmod(walled, 0o755)   # a deliberately loose starting mode
    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr

    assert oct(os.stat(walled).st_mode & 0o777) == "0o700", (
        f"walled directory mode not tightened:\n{result.stdout}")
    for rel in _MCP_FILES:
        assert oct(os.stat(walled / rel).st_mode & 0o777) == "0o600", rel


def test_the_bytecode_cache_is_removed_after_the_compile_check(tmp_path):
    """py_compile is the sanity check that does not need the token — but it
    drops a world-readable __pycache__ into a 700 directory if nothing clears
    it."""
    walled = tmp_path / "walled"
    walled.mkdir()
    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "byte-compiles" in result.stdout, result.stdout
    assert not (walled / "__pycache__").exists(), (
        "py_compile's __pycache__ was left behind in the walled install")


def test_sync_states_which_deliverable_each_host_kind_applies(tmp_path):
    """⛔ SYNC DELIVERS; IT NEVER CONFIGURES. No constitution file is spliced and
    no host config is edited — so the output has to say what is still owed, or
    "sync said done" reads as "the host is wired up"."""
    walled = tmp_path / "walled"
    walled.mkdir()
    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    out = result.stdout
    assert "DELIVERED, not CONFIGURED" in out, out
    assert "CONSTITUTION_SNIPPET_MCP.md" in out and "Phase 8b" in out, out
    assert "system-prompt.md" in out, out
    assert "startup-frozen" in out, (
        "the output does not warn that the gateway must restart — a 'done' "
        "install without it is a 401 next session")


def test_a_skill_install_is_unaffected_by_the_kind_branch(tmp_path):
    """The `skill` path must be byte-for-byte what it always was: a two-field
    entry still gets the whole CLI package, and none of the connector files."""
    install = tmp_path / "cli" / "shared-memory"
    install.mkdir(parents=True)
    reg = tmp_path / "gateway.env"
    reg.write_text(f"AGENT_INSTALLS=codex:{install}/.env\n")

    result = _run_sync(tmp_path, reg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (install / "SKILL.md").is_file(), result.stdout
    assert (install / "scripts" / "memory_bridge.py").is_file(), result.stdout
    for rel in _MCP_FILES:
        assert not (install / rel).exists(), (
            f"{rel} leaked into a CLI skill install")


def test_the_two_kinds_are_delivered_side_by_side_in_one_run(tmp_path):
    """Composition, not two isolated branches: one registry naming both kinds
    must send each package to its own target and neither to the other."""
    cli = tmp_path / "cli" / "shared-memory"
    cli.mkdir(parents=True)
    walled = tmp_path / "walled"
    walled.mkdir()
    reg = tmp_path / "gateway.env"
    reg.write_text(f"AGENT_INSTALLS=codex:{cli}/.env,opencode:mcp:{walled}/.env\n")

    result = _run_sync(tmp_path, reg)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (cli / "SKILL.md").is_file(), result.stdout
    assert not (cli / "vector-skill.py").exists(), result.stdout
    assert (walled / "vector-skill.py").is_file(), result.stdout
    assert not (walled / "SKILL.md").exists(), result.stdout


def test_a_pre_kind_cli_package_in_an_mcp_install_is_reported_not_deleted(tmp_path):
    """What a sync that predates kinds left behind is REPORTED. Deleting files
    beside a live token without being asked is not sync's call — but leaving
    them unmentioned is how they stay there."""
    walled = tmp_path / "walled"
    walled.mkdir()
    (walled / "SKILL.md").write_text("stale\n")

    result = _run_sync(tmp_path, _mcp_registry(tmp_path, walled))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CLI-skill / template files found" in result.stdout, result.stdout
    assert (walled / "SKILL.md").exists(), "sync deleted a file it only had to report"
