"""bootstrap_tokens.sh — the install-path registry + additive mint wiring
(fresh-host findings D19/D20, roster-registry-mint build).

bootstrap_tokens.sh resolves its own gateway .env from its OWN script
location (REPO_ROOT = two directories up from the script), so it cannot be
pointed at a fixture .env via an argument or env var -- exercising the
REAL, SHIPPED script (never a hand-rolled reimplementation of its bash
logic) means running it from a COPY of the repo's relevant scripts under an
isolated fake root, with generate_tokens.py's LOCAL_SKILL_ENV_PATHS patched
to point at fixture directories under that SAME root. This is the only way
to prove the actual shipped bash -- replace_registry_line(), the --add
branch, the bulk-mint guard -- without ever touching a real skill .env on
the machine running these tests (several of which hold this machine's own
live production tokens).

Every subprocess call uses a 30s timeout: a hang here would otherwise stall
the whole suite silently.
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BOOTSTRAP_SRC = REPO_ROOT / "shared-memory" / "scripts" / "bootstrap_tokens.sh"
GENERATE_SRC = REPO_ROOT / "shared-memory" / "scripts" / "generate_tokens.py"
# The read-only roster is a shipped sibling module, not a test fixture: both the
# minter and the gateway import it, which is the point of it existing.
ROLES_SRC = REPO_ROOT / "shared-memory" / "scripts" / "agent_roles.py"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _make_fake_root(tmp_path, local_paths: dict) -> Path:
    """Copies the ACTUAL shipped bootstrap_tokens.sh and generate_tokens.py
    into tmp_path/shared-memory/scripts, patching only the COPY of
    generate_tokens.py's LOCAL_SKILL_ENV_PATHS to point at fixture
    directories under tmp_path -- never the real ~/.claude, ~/.codex, etc.
    Returns the fake repo root; the caller creates shared-memory/.env there.
    """
    scripts_dir = tmp_path / "shared-memory" / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy(BOOTSTRAP_SRC, scripts_dir / "bootstrap_tokens.sh")
    shutil.copy(ROLES_SRC, scripts_dir / "agent_roles.py")
    os.chmod(scripts_dir / "bootstrap_tokens.sh", 0o755)

    src = GENERATE_SRC.read_text()
    marker = "LOCAL_SKILL_ENV_PATHS = {"
    start = src.index(marker)
    end = src.index("\n}\n", start) + len("\n}\n")
    entries = "\n".join(f'    "{name}": {path!r},' for name, path in local_paths.items())
    patched = src[:start] + "LOCAL_SKILL_ENV_PATHS = {\n" + entries + "\n}\n" + src[end:]
    assert patched != src, "LOCAL_SKILL_ENV_PATHS block not found/replaced -- fixture is stale"
    (scripts_dir / "generate_tokens.py").write_text(patched)

    return tmp_path


def _run(fake_root: Path, args: "list[str]") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(fake_root / "shared-memory" / "scripts" / "bootstrap_tokens.sh"), *args],
        cwd=fake_root, capture_output=True, text=True, timeout=30,
    )


def _env_path(fake_root: Path) -> Path:
    return fake_root / "shared-memory" / ".env"


# ── D20: exactly one live AGENT_TOKENS=/AGENT_ROLES= after a run against an
#    .env.example-shaped starting file ─────────────────────────────────────

def test_bulk_mint_against_env_example_shape_leaves_one_live_line_each(tmp_path):
    """I-A4/I-A5: starting from a .env carrying the SAME commented
    placeholders .env.example now ships (never a live assignment), a bulk
    mint must leave exactly one live AGENT_TOKENS= and one live
    AGENT_ROLES=."""
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {"claude": str(claude_dir / ".env")})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        "PG_PASSWORD=fake\n"
        "# GATEWAY .env: the agent token registry. Empty = auth DISABLED.\n"
        "# AGENT_TOKENS=\n"
        "# GATEWAY .env: optional read-only roles.\n"
        "# AGENT_ROLES=monitor:read\n"
    )

    proc = _run(fake_root, [])

    assert proc.returncode == 0, proc.stderr
    content = _env_path(fake_root).read_text()
    assert len([l for l in content.splitlines() if l.startswith("AGENT_TOKENS=")]) == 1
    assert len([l for l in content.splitlines() if l.startswith("AGENT_ROLES=")]) == 1
    assert "AGENT_TOKEN=" in (claude_dir / ".env").read_text()


def test_rerun_against_a_stale_pre_fix_env_still_converges_to_one_live_line(tmp_path):
    """D20's other half: an OLDER .env that still carries a LIVE, empty
    AGENT_TOKENS= placeholder (what .env.example shipped before this fix)
    must also converge to exactly one live line, not two, when
    bootstrap_tokens.sh --force runs against it."""
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_old')}\nAGENT_ROLES=\n",
    )

    proc = _run(fake_root, ["--force"])

    assert proc.returncode == 0, proc.stderr
    content = _env_path(fake_root).read_text()
    assert len([l for l in content.splitlines() if l.startswith("AGENT_TOKENS=")]) == 1


# ── D19: a registered-but-missing directory refuses, loudly, through to the
#    operator running THIS script (not just generate_tokens.py in isolation)

def test_bulk_mint_surfaces_the_refusal_for_a_missing_directory(tmp_path):
    fake_root = _make_fake_root(
        tmp_path, {"claude": str(tmp_path / "claude_skill" / ".env")},  # never mkdir'd
    )
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    proc = _run(fake_root, [])

    # F4/I-A10 (fix round 2): the safe merged registry IS still written (a
    # per-agent refusal never blocks the agents that succeeded), but the
    # script now exits 2 -- a distinguishable "partial failure, needs
    # attention" signal for automation -- rather than a bare 0. This is a
    # deliberate behaviour change from the first cut of D19: the refusal
    # itself was always non-fatal to the OTHER agents, but this run's exit
    # code used to say nothing was wrong at all.
    assert proc.returncode == 2
    assert "REFUSED" in proc.stdout
    assert "claude" in proc.stdout
    assert "PARTIAL FAILURE" in proc.stdout
    content = _env_path(fake_root).read_text()
    assert "claude:sha256:" not in content, "a refused agent's digest must not land in AGENT_TOKENS"


# ── --add: roster growth without rotation, and its refusal paths ───────────

def test_add_grows_the_roster_without_touching_the_first_agents_digest(tmp_path):
    claude_dir = tmp_path / "claude_skill"
    claude_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {"claude": str(claude_dir / ".env")})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    first = _run(fake_root, [])
    assert first.returncode == 0, first.stderr
    before = _env_path(fake_root).read_text()
    claude_line = next(l for l in before.splitlines() if l.startswith("AGENT_TOKENS="))
    claude_entries = claude_line[len("AGENT_TOKENS="):].split(",")
    claude_digest = next(p for p in claude_entries if p.startswith("claude:"))

    cursor_dir = tmp_path / "cursor_skill"
    cursor_dir.mkdir()
    second = _run(fake_root, ["--add", "cursor", "--install-path", str(cursor_dir / ".env")])

    assert second.returncode == 0, second.stderr
    after = _env_path(fake_root).read_text()
    assert len([l for l in after.splitlines() if l.startswith("AGENT_TOKENS=")]) == 1
    after_line = next(l for l in after.splitlines() if l.startswith("AGENT_TOKENS="))
    assert claude_digest in after_line, "I-A1: claude's digest must survive --add byte-identical"
    assert "cursor:sha256:" in after_line
    assert "AGENT_TOKEN=" in (cursor_dir / ".env").read_text()


def test_add_refuses_an_already_registered_name_and_touches_nothing(tmp_path):
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n")
    before = _env_path(fake_root).read_text()

    proc = _run(fake_root, ["--add", "codex"])

    assert proc.returncode != 0
    assert "already registered" in proc.stdout
    assert _env_path(fake_root).read_text() == before, "a refused --add must not modify the .env"


def test_add_refuses_when_the_skill_directory_does_not_exist_yet(tmp_path):
    """AGENTS.md's 'Add an agent later' runbook documents a required order
    (mkdir the skill dir, THEN --add, THEN sync_skills.sh) precisely because
    --add refuses a target whose directory is not there yet -- D19: minting a
    token into the registry that nobody actually received is worse than not
    minting at all. This pins that refusal so the documented order stays
    load-bearing rather than aspirational; if --add ever started silently
    creating the directory itself, the runbook's warning would go stale the
    same way the chown step did (D11)."""
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    never_created = tmp_path / "newagent_skill"  # deliberately never mkdir'd
    proc = _run(fake_root, ["--add", "newagent", "--install-path", str(never_created / ".env")])

    assert proc.returncode != 0
    assert "REFUSED" in proc.stdout
    assert "does not exist" in proc.stdout
    after = _env_path(fake_root).read_text()
    assert "newagent" not in after, "a refused --add must not register the agent either"
    assert not never_created.exists(), "--add must not create the directory itself"


def test_add_and_force_together_are_rejected_before_any_minting(tmp_path):
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n")

    proc = _run(fake_root, ["--add", "grok", "--force"])

    assert proc.returncode != 0
    assert "mutually exclusive" in proc.stdout + proc.stderr


def test_bulk_mint_refuses_without_force_when_already_registered(tmp_path):
    """Pre-existing safety guard (unchanged by this build) -- a bulk mint
    must still refuse outright when AGENT_TOKENS is already live, pointing
    at --add for the additive case."""
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n")
    before = _env_path(fake_root).read_text()

    proc = _run(fake_root, [])

    assert proc.returncode == 0  # refuses quietly (exit 0), same as before this build
    assert "refusing to regenerate" in proc.stdout
    assert _env_path(fake_root).read_text() == before


# ── The shipped bash must actually WRITE the roles line ──────────────────────
#
# generate_tokens.py printing AGENT_ROLES is only half the fix: bootstrap_tokens.sh
# greps for exactly the lines it knows about, and it used to know about
# AGENT_TOKENS and AGENT_INSTALLS only. These drive the REAL script, because the
# gap being closed lived in the bash, not in the Python.


def test_add_of_the_monitor_writes_the_read_role_into_the_env(tmp_path):
    """Operator rule: monitor always has a read-only token. Absence from
    AGENT_ROLES means FULL read/write in the gateway, so a missing line here is
    a write-capable dashboard."""
    mon_dir = tmp_path / "monitor_skill"
    mon_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')}\n")

    res = _run(fake_root, ["--add", "monitor", "--install-path", str(mon_dir / ".env")])

    assert res.returncode == 0, res.stderr
    after = _env_path(fake_root).read_text()
    roles = [l for l in after.splitlines() if l.startswith("AGENT_ROLES=")]
    assert len(roles) == 1, f"expected exactly one live AGENT_ROLES line, got {roles}"
    assert "monitor:read" in roles[0]


def test_add_of_the_monitor_preserves_an_existing_backup_admin_entry(tmp_path):
    """The backup credential is the one token confined to /admin/*. A roles line
    rebuilt from only the new agent would widen it to full access."""
    mon_dir = tmp_path / "monitor_skill"
    mon_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')}\n"
        "AGENT_ROLES=backup:admin\n")

    res = _run(fake_root, ["--add", "monitor", "--install-path", str(mon_dir / ".env")])

    assert res.returncode == 0, res.stderr
    roles = next(l for l in _env_path(fake_root).read_text().splitlines()
                 if l.startswith("AGENT_ROLES="))
    assert "backup:admin" in roles, "the admin-confined credential was dropped"
    assert "monitor:read" in roles


def test_add_of_an_ordinary_agent_writes_no_roles_line(tmp_path):
    """Full access is the absence of an entry; --add must not start emitting one
    for every agent, which would grow the line without narrowing anything."""
    d = tmp_path / "cursor_skill"
    d.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')}\n")

    res = _run(fake_root, ["--add", "cursor", "--install-path", str(d / ".env")])

    assert res.returncode == 0, res.stderr
    assert not [l for l in _env_path(fake_root).read_text().splitlines()
                if l.startswith("AGENT_ROLES=")]


def test_role_without_add_is_refused(tmp_path):
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    res = _run(fake_root, ["--role", "read"])

    assert res.returncode != 0
    assert "--role only makes sense together with --add" in (res.stdout + res.stderr)


def test_a_bulk_mint_does_not_erase_an_operator_declared_admin_role(tmp_path):
    """Review finding (Adversarial), and the more destructive half of the roles
    defect: the ADDITIVE path was fixed to merge AGENT_ROLES, while the BULK path
    still rebuilt the line from the read-only roster alone.

    A bulk mint therefore deleted `backup:admin` — the one credential confined to
    /admin/* — and absence from AGENT_ROLES means FULL read/write. The mint
    widened a restricted credential as a side effect of rotating tokens, silently.
    """
    d = tmp_path / "claude_skill"
    d.mkdir()
    fake_root = _make_fake_root(tmp_path, {"claude": str(d / ".env")})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\nAGENT_ROLES=backup:admin\n")

    res = _run(fake_root, [])

    assert res.returncode == 0, res.stderr
    roles = [l for l in _env_path(fake_root).read_text().splitlines()
             if l.startswith("AGENT_ROLES=")]
    assert len(roles) == 1, f"expected one live AGENT_ROLES line, got {roles}"
    assert "backup:admin" in roles[0], (
        "a bulk mint erased the admin-confined credential, widening it to full access")


def test_a_bulk_mint_still_writes_the_read_only_roster(tmp_path):
    """Merging must not lose the roster either — both halves of the line matter."""
    d = tmp_path / "claude_skill"
    d.mkdir()
    fake_root = _make_fake_root(tmp_path, {"claude": str(d / ".env")})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\nAGENT_ROLES=backup:admin\n")

    _run(fake_root, [])

    roles = next(l for l in _env_path(fake_root).read_text().splitlines()
                 if l.startswith("AGENT_ROLES="))
    assert "monitor:read" in roles


def test_a_bulk_mint_repairs_a_roster_agent_declared_too_widely(tmp_path):
    """Drift repair: an identity registered before the roster was enforced may be
    declared `full` in the file. The gateway confines it regardless, but the .env
    should not state something an operator would act on and be wrong about."""
    d = tmp_path / "claude_skill"
    d.mkdir()
    fake_root = _make_fake_root(tmp_path, {"claude": str(d / ".env")})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\nAGENT_ROLES=monitor:full\n")

    _run(fake_root, [])

    roles = next(l for l in _env_path(fake_root).read_text().splitlines()
                 if l.startswith("AGENT_ROLES="))
    assert "monitor:read" in roles
    assert "monitor:full" not in roles


def test_every_registry_line_lands_in_one_write(tmp_path):
    """Review finding: three sequential read-modify-write calls meant an
    interruption could leave a new agent with a TOKEN and no ROLE — and absence
    from AGENT_ROLES is FULL access, so a crash mid-run handed out an unconfined
    credential. All keys now apply to one temp file and one rename.

    Asserted through the observable consequence: after a successful --add there
    is exactly one live line per key and they agree with each other.
    """
    mon_dir = tmp_path / "monitor_skill"
    mon_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=claude:sha256:{_digest('tok_claude')}\n")

    res = _run(fake_root, ["--add", "monitor", "--install-path", str(mon_dir / ".env")])

    assert res.returncode == 0, res.stderr
    text = _env_path(fake_root).read_text()
    for key in ("AGENT_TOKENS", "AGENT_INSTALLS", "AGENT_ROLES"):
        live = [l for l in text.splitlines() if l.startswith(f"{key}=")]
        assert len(live) == 1, f"{key}: expected one live line, got {live}"
    assert "monitor:sha256:" in text
    assert "monitor:read" in text


def test_remint_reissues_one_agent_without_touching_the_others(tmp_path):
    """The missing operation. A bulk mint registers remote agents whose tokens
    are never delivered, and the only recovery was --force — rotating EVERY
    agent to recover one. Refusing accidental rotation must not also make
    deliberate re-issue impossible.
    """
    a_dir = tmp_path / "a_skill"; a_dir.mkdir()
    b_dir = tmp_path / "b_skill"; b_dir.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    assert _run(fake_root, ["--add", "alpha", "--install-path", str(a_dir / ".env")]).returncode == 0
    assert _run(fake_root, ["--add", "beta", "--install-path", str(b_dir / ".env")]).returncode == 0
    before = _env_path(fake_root).read_text()
    beta_digest = next(p for p in
                       next(l for l in before.splitlines() if l.startswith("AGENT_TOKENS="))
                       .split(",") if p.startswith("beta:"))
    alpha_token_before = (a_dir / ".env").read_text()

    res = _run(fake_root, ["--remint", "alpha", "--install-path", str(a_dir / ".env")])

    assert res.returncode == 0, res.stderr
    after = _env_path(fake_root).read_text()
    assert beta_digest in after, "--remint rotated an agent it was not asked to touch"
    assert (a_dir / ".env").read_text() != alpha_token_before, "alpha was not re-issued"


def test_remint_refuses_an_agent_that_is_not_registered(tmp_path):
    """--remint re-issues; --add registers. Conflating them would let a typo
    quietly create an agent nobody meant to exist."""
    d = tmp_path / "skill"; d.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text("PG_PASSWORD=fake\n")

    res = _run(fake_root, ["--remint", "ghost", "--install-path", str(d / ".env")])

    assert res.returncode != 0
    assert "not registered" in (res.stdout + res.stderr)


def test_add_points_at_remint_instead_of_a_fleet_rotation(tmp_path):
    """The refusal used to say "there is no single-agent rotation", leaving
    --force (rotate everyone) as the only documented way forward."""
    d = tmp_path / "skill"; d.mkdir()
    fake_root = _make_fake_root(tmp_path, {})
    _env_path(fake_root).parent.mkdir(parents=True, exist_ok=True)
    _env_path(fake_root).write_text(
        f"AGENT_TOKENS=codex:sha256:{_digest('tok_codex')}\n")

    res = _run(fake_root, ["--add", "codex", "--install-path", str(d / ".env")])

    assert res.returncode != 0
    out = res.stdout + res.stderr
    assert "--remint codex" in out
    assert "there is no single-agent rotation" not in out
