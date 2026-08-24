"""AGENT_INSTALLS carries an install KIND — and both arities parse, everywhere.

An entry is `name:path` (kind `skill`, permanently — the two-field form is the
shorthand for the default, not a legacy spelling awaiting migration) or
`name:kind:path`. The kind decides what `sync_skills.sh` DELIVERS to that
directory, so a reader that gets the arity wrong does not merely misparse: it
ships the wrong package into a directory holding a live token.

Every READER of the registry is covered here, because a reader that copies or
re-emits the line is a writer too:

  * generate_tokens.py  — `_parse_agent_installs` / `_format_agent_installs`,
    `_load_agent_installs_registry`, and the two paths that re-emit the line
    (`mint`, `add_agent`).
  * sync_skills.sh      — target selection (covered by
    test_mcp_install_delivery.py, which drives the real script).
  * uninstall_framework.sh — the inventory/removal parse, covered below by
    running the real script's own pipeline.
  * bootstrap_tokens.sh — forwards `--mcp` and persists the line
    (test_bootstrap_tokens_registry.py).

⛔ NOTHING HERE EXECUTES THE MINT AGAINST A REAL $HOME. The pure parse/format
functions are called directly; the registry-reading paths use the suite's
isolated loader, whose `_DEFAULT_GATEWAY_ENV` is a path that cannot exist.
"""
import importlib.util
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

_REPO = os.path.join(os.path.dirname(__file__), "..")


def load_generate_tokens():
    """Same isolation contract as test_generate_tokens_mint_flow.load_generate_tokens:
    a fresh module whose default gateway .env CANNOT exist, so nothing here can
    reach this machine's real registry."""
    path = os.path.join(_REPO, "shared-memory", "scripts", "generate_tokens.py")
    spec = importlib.util.spec_from_file_location("generate_tokens_kinds_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._DEFAULT_GATEWAY_ENV = "/nonexistent/agent-installs-kinds-test.env"
    return mod


# ── The parser: both arities ─────────────────────────────────────────────────

def test_two_field_entry_parses_as_kind_skill():
    """The permanent meaning of `name:path`. If this ever stops holding, every
    registry written before kinds existed silently changes what it delivers."""
    gt = load_generate_tokens()
    assert gt._parse_agent_installs("claude:/home/a/.claude/skills/shared-memory/.env") == {
        "claude": ("skill", "/home/a/.claude/skills/shared-memory/.env")
    }


def test_three_field_entry_parses_its_kind():
    gt = load_generate_tokens()
    assert gt._parse_agent_installs("opencode:mcp:/home/a/.config/x/shared-memory-mcp/.env") == {
        "opencode": ("mcp", "/home/a/.config/x/shared-memory-mcp/.env")
    }


def test_an_explicit_skill_kind_parses_too():
    """`name:skill:path` is legal even though nothing writes it — a reader that
    accepted only the shorthand would break on a hand-edited registry."""
    gt = load_generate_tokens()
    assert gt._parse_agent_installs("codex:skill:/home/a/.codex/skills/shared-memory/.env") == {
        "codex": ("skill", "/home/a/.codex/skills/shared-memory/.env")
    }


def test_both_arities_parse_in_one_line():
    gt = load_generate_tokens()
    parsed = gt._parse_agent_installs(
        "claude:/a/.env,opencode:mcp:/b/.env,codex:/c/.env")
    assert parsed == {
        "claude": ("skill", "/a/.env"),
        "opencode": ("mcp", "/b/.env"),
        "codex": ("skill", "/c/.env"),
    }


def test_a_legacy_path_containing_a_colon_is_not_eaten_as_a_kind():
    """⛔ THE REGRESSION THE KIND FIELD COULD HAVE CAUSED. A path registered
    before ':' was refused as a delimiter still has to parse WHOLE. Splitting on
    "three fields" rather than "a KNOWN kind" would turn `/od:d/x/.env` into
    kind `/od` and path `d/x/.env` — the truncated-prefix failure the
    first-colon-only rule existed to prevent, reintroduced by its successor."""
    gt = load_generate_tokens()
    assert gt._parse_agent_installs("weird:/od:d/x/.env") == {
        "weird": ("skill", "/od:d/x/.env")
    }


def test_an_unknown_middle_field_is_path_not_kind():
    """A future kind this version has never heard of must not be silently
    accepted as one — it is treated as part of the path, so the entry is
    visibly wrong rather than invisibly mis-delivered."""
    gt = load_generate_tokens()
    assert gt._parse_agent_installs("x:sidecar:/p/.env") == {
        "x": ("skill", "sidecar:/p/.env")
    }


def test_a_nameless_or_pathless_entry_is_skipped():
    gt = load_generate_tokens()
    assert gt._parse_agent_installs(":/p/.env,name:, ,ok:/q/.env") == {
        "ok": ("skill", "/q/.env")
    }


# ── The formatter: a mint is not a migration ─────────────────────────────────

def test_skill_entries_round_trip_byte_identical():
    """⛔ A BULK MINT RE-EMITS THE WHOLE REGISTRY. If the default kind were
    written back as `name:skill:path`, every rotation would restyle every line
    in the operator's .env — a schema change disguised as a token rotation, and
    a diff nobody asked for on the one file that decides who the gateway
    trusts."""
    gt = load_generate_tokens()
    raw = "claude:/a/.env,codex:/c/.env"
    assert gt._format_agent_installs(gt._parse_agent_installs(raw)) == raw


def test_an_mcp_entry_round_trips_with_its_kind():
    gt = load_generate_tokens()
    raw = "claude:/a/.env,opencode:mcp:/b/.env"
    assert gt._format_agent_installs(gt._parse_agent_installs(raw)) == raw


# ── The registry loader, and the two paths that re-emit the line ─────────────

def test_the_loader_reports_kinds_and_presence(tmp_path):
    gt = load_generate_tokens()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_INSTALLS=claude:/a/.env,opencode:mcp:/b/.env\n")
    installs, present = gt._load_agent_installs_registry(str(env))
    assert present is True
    assert installs["claude"][0] == "skill"
    assert installs["opencode"][0] == "mcp"


def test_add_agent_emits_the_mcp_kind_and_preserves_a_skill_neighbour(tmp_path, capsys):
    """add_agent() re-emits the WHOLE registry, so it is a writer of every
    entry, not only the one being added. The neighbour must come back in the
    two-field form it went in as."""
    gt = load_generate_tokens()
    walled = tmp_path / "walled"
    walled.mkdir()
    env = tmp_path / "gateway.env"
    env.write_text(
        "AGENT_TOKENS=claude:sha256:" + "0" * 64 + "\n"
        "AGENT_INSTALLS=claude:/a/.claude/skills/shared-memory/.env\n"
    )

    rc, token = gt.add_agent("opencode", install_path=str(walled / ".env"),
                             env_path=str(env), install_kind="mcp")
    out = capsys.readouterr().out
    assert rc == 0 and token
    line = next(l for l in out.splitlines() if l.startswith("AGENT_INSTALLS="))
    assert "claude:/a/.claude/skills/shared-memory/.env" in line, (
        "an existing skill entry was rewritten or dropped by an unrelated --add")
    assert f"opencode:mcp:{walled / '.env'}" in line, (
        "the mcp kind did not reach the registry line — sync would deliver the "
        "CLI package into a walled MCP directory")
    assert token not in out, "the raw token leaked into stdout"


def test_mint_carries_an_mcp_kind_forward(tmp_path, capsys):
    """A bulk mint re-emits AGENT_INSTALLS from what it wrote through. An mcp
    entry must come back OUT as an mcp entry — losing the kind here silently
    downgrades the install on the next rotation."""
    gt = load_generate_tokens()
    walled = tmp_path / "walled"
    walled.mkdir()
    env = tmp_path / "gateway.env"
    env.write_text(f"AGENT_INSTALLS=opencode:mcp:{walled / '.env'}\n")

    gt.mint(env_path=str(env), roster=["opencode"])
    out = capsys.readouterr().out
    line = next(l for l in out.splitlines() if l.startswith("AGENT_INSTALLS="))
    assert line == f"AGENT_INSTALLS=opencode:mcp:{walled / '.env'}"


def test_an_unknown_kind_is_refused_before_anything_is_minted(tmp_path, capsys):
    gt = load_generate_tokens()
    d = tmp_path / "d"
    d.mkdir()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_TOKENS=claude:sha256:" + "0" * 64 + "\n")

    rc, token = gt.add_agent("x", install_path=str(d / ".env"), env_path=str(env),
                             install_kind="sidecar")
    assert rc == 1 and token is None
    assert not (d / ".env").exists(), "a refused --add still wrote a token file"


def test_a_non_default_kind_without_a_path_is_refused(tmp_path):
    """An install kind says what to deliver WHERE. With no registered path the
    kind is dropped along with it, so the operator would be told an MCP install
    was registered when nothing was."""
    gt = load_generate_tokens()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_TOKENS=claude:sha256:" + "0" * 64 + "\n")
    rc, token = gt.add_agent("x", install_path=None, env_path=str(env),
                             install_kind="mcp")
    assert rc == 1 and token is None


def test_mcp_without_add_or_remint_is_refused_at_the_cli():
    """A bulk mint re-emits every entry with its OWN kind; accepting --mcp there
    would read as "convert them all"."""
    gt = load_generate_tokens()
    rc = gt.main(["--mcp"])
    assert rc == 1


# ── uninstall_framework.sh's own parse of the same line ──────────────────────

def _uninstall_registry_dirs(env_text: str, tmp_path) -> list:
    """Run uninstall_framework.sh's REAL registry-parsing pipeline, lifted
    verbatim out of the script, against a fixture .env.

    Lifted rather than invoked because the script's own entry point tears down
    a live install. The lift is guarded by the assertion below that the exact
    text still appears in the shipped script — so this cannot quietly become a
    test of a reimplementation.
    """
    env_file = tmp_path / "gateway.env"
    env_file.write_text(env_text)
    script = os.path.join(_REPO, "shared-memory", "scripts", "uninstall_framework.sh")
    with open(script, encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("mapfile -t _registry_entries")
    end = source.index("SKILL_DIRS=(); SKILL_KINDS=()")
    snippet = source[start:end]
    assert "_registry_dirs+=" in snippet, "lifted the wrong block — fixture is stale"
    proc = subprocess.run(
        ["bash", "-c",
         f'ENV_FILE={env_file!s}\n{snippet}\n'
         'for i in "${!_registry_dirs[@]}"; do '
         'printf "%s\\t%s\\n" "${_registry_kinds[$i]}" "${_registry_dirs[$i]}"; done'],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return [tuple(l.split("\t")) for l in proc.stdout.splitlines() if l.strip()]


def test_uninstall_reads_both_arities(tmp_path):
    """⛔ THE MEASURED BREAK. The old parse stripped only the agent name and ran
    `dirname` on the rest, so `opencode:mcp:/w/.env` became the literal directory
    `mcp:` — the walled directory holding that agent's raw token was neither
    inventoried nor removed, and a nonsense path was listed in its place."""
    rows = _uninstall_registry_dirs(
        "AGENT_INSTALLS=claude:/a/skills/shared-memory/.env,opencode:mcp:/w/shared-memory-mcp/.env\n",
        tmp_path,
    )
    assert rows == [("skill", "/a/skills/shared-memory"),
                    ("mcp", "/w/shared-memory-mcp")], rows
