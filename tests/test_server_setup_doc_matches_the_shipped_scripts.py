"""`shared-memory/Documentation/server-setup.md` must describe the scripts this
repository actually ships (W7/F5).

Five separate drifts were measured in that one file, and each is asserted here
separately. A proof that verifies a fifth of a fix is not a proof: pinning only
the upgrade row would leave a stranger being told to mint with a script the
docs elsewhere call operator-only, and every one of these is a command someone
types on a fresh host.

  1. THE UPGRADE ROW claimed `git pull` -> `apply.py` -> restart. That skips
     the backup, the `.env` migration, the Neo4j constraints (Neo4j has NO
     migration ledger, so nothing else creates them), the project-identity
     reconciliation and the postflight. `update_framework.sh` IS the
     procedure.
  2. THE PROMPT LIST promised prompts for `TAVILY_API_KEY` and four
     "optional" keys the installer has never asked for. It asks for paths,
     two encoder devices, a render gid and the two database passwords.
     `TAVILY_API_KEY` is real -- five files read it -- but it is edited into
     `shared-memory/.env` afterwards.
  3. THE REMOTE-TOKEN RECOVERY named `generate_tokens.py --reveal`.
     `bootstrap_tokens.sh` is the shipped entry point, and `--reveal` prints a
     LIVE token, so the line must also say it is an operator-only step:
     a credential that passes through an agent is in a transcript, and a
     transcript is kept forever.
  4. THE MCP MINT/RE-ISSUE STEPS named `generate_tokens.py --add/--remint`
     for the same reason and with the same fix.
  5. THE API_VERSION SECTION still said `git pull` and restart, which is the
     same upgrade the first item is about.
"""
import os
import re

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
DOC = os.path.join(REPO_ROOT, "shared-memory", "Documentation", "server-setup.md")


def _text():
    with open(DOC, encoding="utf-8") as f:
        return f.read()


def _table_row_for(label):
    for line in _text().split("\n"):
        if line.startswith("|") and label in line:
            return line
    return ""


def test_the_upgrade_row_names_the_update_script_not_a_bare_pull():
    row = _table_row_for("Upgraded by")
    assert row, "server-setup.md no longer has an 'Upgraded by' row"
    assert "update_framework.sh" in row, (
        f"the Operations upgrade path must be update_framework.sh, which is "
        f"the whole procedure (backup, env migration, Postgres migrations, "
        f"Neo4j constraints, identity reconciliation, restart, sync, "
        f"postflight): {row!r}"
    )
    assert "apply.py" not in row, (
        f"the upgrade row still presents apply.py as the upgrade — it is one "
        f"step of it, and naming it alone is what let four other steps be "
        f"skipped: {row!r}"
    )


def test_the_installer_prompt_list_matches_what_the_script_asks():
    text = _text()
    block = text.split("bash shared-memory/scripts/install_framework.sh", 1)
    assert len(block) > 1, "server-setup.md no longer runs install_framework.sh"
    after = block[1][:1200]
    assert "Fill in when prompted: NEO4J_PASSWORD, PG_PASSWORD, TAVILY_API_KEY" not in text, (
        "server-setup.md still promises a TAVILY_API_KEY prompt the installer "
        "does not have"
    )
    assert re.search(r"TAVILY_API_KEY is real but is NOT prompted", after), (
        "the corrected prompt list must still say TAVILY_API_KEY is a real "
        "variable (five files read it), edited in afterwards — dropping it "
        "entirely would trade one wrong statement for a missing one"
    )
    for asked in ("password", "device"):
        assert asked in after.lower(), (
            f"the corrected prompt list does not mention the {asked} prompts "
            f"the installer actually asks"
        )


def test_the_remote_token_recovery_uses_the_shipped_wrapper_and_says_reveal_is_operator_only():
    text = _text()
    reveal_lines = [ln for ln in text.split("\n") if "--reveal" in ln]
    assert reveal_lines, "server-setup.md no longer documents --reveal at all"
    for line in reveal_lines:
        assert "generate_tokens.py --reveal" not in line, (
            f"--reveal is documented through generate_tokens.py again; "
            f"bootstrap_tokens.sh is the shipped entry point: {line!r}"
        )
    assert re.search(r"OPERATOR-ONLY|operator-only", text), (
        "the --reveal lines must state that it is an operator-only step, run "
        "in the operator's own terminal and never through an agent — it "
        "prints a live token, and a transcript is kept forever"
    )


def test_the_mcp_mint_steps_use_bootstrap_tokens():
    text = _text()
    offenders = [
        ln
        for ln in text.split("\n")
        if re.search(r"generate_tokens\.py\s+--(add|remint)", ln)
    ]
    assert not offenders, (
        f"server-setup.md still tells a stranger to mint with "
        f"generate_tokens.py directly rather than through "
        f"bootstrap_tokens.sh: {offenders}"
    )
    assert "bootstrap_tokens.sh --add" in text and "bootstrap_tokens.sh --remint" in text, (
        "the MCP end-to-end order must name the bootstrap wrapper for both "
        "the new-agent and the re-issue case"
    )


def test_the_api_version_section_points_at_the_update_script():
    text = _text()
    match = re.search(r"When you bump `API_VERSION`.*?(?=\n\n)", text, re.S)
    assert match, "server-setup.md no longer has the API_VERSION bump paragraph"
    paragraph = match.group(0)
    assert "update_framework.sh" in paragraph, (
        f"deploying an API_VERSION bump is the ordinary upgrade and must go "
        f"through update_framework.sh: {paragraph!r}"
    )
    assert "git pull" not in paragraph, (
        f"the API_VERSION paragraph still tells the operator to pull and "
        f"restart by hand: {paragraph!r}"
    )
