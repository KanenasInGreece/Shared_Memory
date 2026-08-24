"""A refusal an AGENT will read must never lead with `--reveal`.

⛔ MEASURED, not hypothetical (a live MCP conversion on a test host, finding 6). An installing agent
followed the documented `--add <name> --install-path ...`, hit the
already-registered refusal, and the refusal's very next line told it to run
`--remint <name> --reveal <name>`. `--reveal` PRINTS A LIVE BEARER TOKEN. An
agent transcript is durable, so obeying that message turns "shown once" into
"stored forever" — the exact outcome the whole write-through mint flow exists to
prevent. The message was steering a caller into the one thing it must never do.

The fix is not to delete `--reveal` from the text: for an identity with no local
directory it is genuinely the only delivery path. The fix is ORDER and LABEL —
lead with the write-through form (`--remint <name> --install-path <file>`), which
puts the plaintext in a 600 file and prints nothing, and name `--reveal` only
afterwards, marked as the OPERATOR's own-terminal alternative.

These assertions run against the SHIPPED messages: the refusal is provoked by
calling add_agent() with the suite's isolated loader (a default gateway .env that
cannot exist), never by executing the mint against a real $HOME.
"""
import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

_REPO = os.path.join(os.path.dirname(__file__), "..")


def load_generate_tokens():
    path = os.path.join(_REPO, "shared-memory", "scripts", "generate_tokens.py")
    spec = importlib.util.spec_from_file_location("generate_tokens_refusal_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._DEFAULT_GATEWAY_ENV = "/nonexistent/refusal-text-test.env"
    return mod


def _already_registered_refusal(tmp_path, capsys, **kw) -> str:
    """Provoke the exact refusal the live conversion hit, and return its text."""
    gt = load_generate_tokens()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_TOKENS=opencode:sha256:" + "0" * 64 + "\n")
    rc, token = gt.add_agent("opencode", env_path=str(env), **kw)
    assert rc == 1 and token is None, "the already-registered guard stopped refusing"
    captured = capsys.readouterr()
    return captured.err + captured.out


def _command_lines(text: str) -> list:
    """Every line that offers a runnable generate_tokens.py invocation, in order."""
    return [l.strip() for l in text.splitlines() if "generate_tokens.py" in l]


def test_the_first_command_offered_is_write_through_not_reveal(tmp_path, capsys):
    """THE regression guard. An agent acts on the FIRST command it is given."""
    text = _already_registered_refusal(tmp_path, capsys,
                                       install_path=str(tmp_path / "w" / ".env"))
    commands = _command_lines(text)
    assert commands, f"the refusal offers no command at all:\n{text}"
    first = commands[0]
    assert "--install-path" in first, (
        f"the refusal's first command is not the write-through form: {first!r}")
    assert "--reveal" not in first, (
        f"the refusal STILL steers an agent to --reveal first: {first!r}\n"
        "A revealed token lands in the transcript, permanently.")


def test_every_reveal_mention_is_labelled_operator_only(tmp_path, capsys):
    """`--reveal` may appear — it is a real recovery for an identity with no
    local directory — but never unlabelled. An unqualified mention is an
    instruction; a labelled one is a note about who may run it."""
    text = _already_registered_refusal(tmp_path, capsys,
                                       install_path=str(tmp_path / "w" / ".env"))
    if "--reveal" not in text:
        return
    assert re.search(r"OPERATOR|never an\s*\n?\s*agent|never through an agent",
                     text, re.I), (
        f"--reveal is named with no operator-only qualification:\n{text}")


def test_the_refusal_carries_the_install_kind_it_was_called_with(tmp_path, capsys):
    """A recovery command that drops --mcp re-registers an MCP install as a CLI
    skill install, and the next sync delivers the wrong package into a directory
    holding a live token. The refusal has to hand back the flags it was given."""
    text = _already_registered_refusal(tmp_path, capsys,
                                       install_path=str(tmp_path / "w" / ".env"),
                                       install_kind="mcp")
    first = _command_lines(text)[0]
    assert "--mcp" in first, (
        f"the mcp kind was dropped from the recovery command: {first!r}")


def test_the_remote_branch_labels_reveal_and_offers_write_through_first(tmp_path, capsys):
    """The other message that names --reveal: add_agent()'s report for an agent
    registered with NO install path. There the write-through form is advice
    rather than an executable next step, but the ORDER and the label still
    hold."""
    gt = load_generate_tokens()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_TOKENS=claude:sha256:" + "0" * 64 + "\n")
    rc, token = gt.add_agent("newremote", env_path=str(env))
    assert rc == 0 and token
    out = capsys.readouterr().out
    assert token not in out, "the raw token leaked into stdout"

    commands = _command_lines(out)
    assert commands and "--install-path" in commands[0], (
        f"the remote report leads with something other than write-through: {commands}")
    assert re.search(r"OPERATOR", out), (
        f"--reveal offered without an operator-only label:\n{out}")


def test_the_bulk_mints_undeliverable_block_still_labels_reveal(tmp_path, capsys):
    """The third point of use. A withdrawal in one message withdraws nothing —
    the rule has to be checked at EVERY place that prints the flag."""
    gt = load_generate_tokens()
    env = tmp_path / "gateway.env"
    env.write_text("AGENT_INSTALLS=\n")     # registry present but empty -> all REMOTE
    gt.mint(env_path=str(env), roster=["someremote"])
    out = capsys.readouterr().out
    assert "--reveal" in out, "fixture stale: this path no longer mentions --reveal"
    assert re.search(r"never through an agent|operator-run", out, re.I), (
        f"the UNDELIVERABLE block names --reveal with no operator-only label:\n{out}")


def test_no_runnable_reveal_command_in_agents_md_is_unlabelled():
    """The same rule, one level up: AGENTS.md is the AGENT path.

    ⚠ SCOPED TO FENCED CODE BLOCKS ON PURPOSE, and the scope is the finding, not
    a convenience. A first cut asserted over every PARAGRAPH containing the
    string and reported four failures that were not defects — prose explaining
    what `--reveal` is, and why a bulk mint needs it, in sections whose
    operator-only warning sits twenty lines further down. What an agent COPIES
    is a runnable line in a fenced block; descriptive prose is not an
    instruction, and an assertion that cannot tell them apart reports the
    document's shape rather than its safety.
    """
    with open(os.path.join(_REPO, "AGENTS.md"), encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    in_fence = False
    offenders = []
    checked = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence or "--reveal" not in line:
            continue
        checked += 1
        window = "\n".join(lines[max(0, i - 12):i + 13])
        if not re.search(r"operator|yourself|never through an agent", window, re.I):
            offenders.append(f"{i + 1}: {line.strip()}")

    assert checked, "fixture stale: AGENTS.md has no runnable --reveal command at all"
    assert not offenders, (
        "AGENTS.md offers a runnable --reveal command with no operator-only "
        "warning within twelve lines:\n  " + "\n  ".join(offenders))
