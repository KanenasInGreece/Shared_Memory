"""AGENTS.md ordering fix (decision:1473, grounded on fact:1472): a fresh
host with no agent skill directories present produced 0 minted tokens, 0
digests, no file written, and an empty AGENT_TOKENS= line under the
PREVIOUSLY documented order -- Phase 6 minted before Phase 8 (which
creates the skill directories a local agent's token write-through needs)
ever ran. An empty AGENT_TOKENS= parses identically to an absent one, both
meaning auth OFF (.env.example's S-05 note), so an operator following the
documented order silently ended up with an unauthenticated gateway.

The ruling: Phase 6 mints only remote/registry identities (no fixed local
install path in generate_tokens.py's LOCAL_SKILL_ENV_PATHS); Phase 8 mints
each local agent, via generate_tokens.py's --add + --install-path, right
after installing that agent's skill package. Phase 7's verification must
also stop reading an auth-OFF install's inevitable HTTP 200 as "auth
verified".

Every test here is purely structural -- it parses AGENTS.md prose and the
shipped scripts' SOURCE TEXT, and never imports or executes
generate_tokens.py/bootstrap_tokens.sh (that execution path is
categorically off-limits: generate_tokens.py's mint() resolves its
destination paths from a module-level dict pointing at this machine's REAL
~/.claude, ~/.codex, ~/.gemini, ~/.grok regardless of any argument passed
in-process). Other test files already exercise that mint flow safely, via
subprocess against a copied+patched fake root (test_bootstrap_tokens_
registry.py, test_generate_tokens_mint_flow.py) -- this file's job is only
to hold the DOCUMENTATION to the CODE's actual contract, which needs
neither.
"""
import os
import re

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts: str) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _section(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def _extract_local_skill_agents(gen_tokens_src: str) -> set:
    """The agent names generate_tokens.py has a FIXED local skill-directory
    guess for -- these are exactly the names a bulk mint will try to
    write-through to, and REFUSE (D19) when that directory does not exist
    yet, on a fresh host."""
    m = re.search(r"LOCAL_SKILL_ENV_PATHS = \{(.*?)\n\}", gen_tokens_src, re.S)
    assert m, "LOCAL_SKILL_ENV_PATHS's dict shape changed -- update this regex"
    names = set(re.findall(r'"(\w+)":\s*os\.path\.expanduser', m.group(1)))
    assert names, "no agent names extracted from LOCAL_SKILL_ENV_PATHS -- the regex has rotted"
    return names


def _extract_agents_roster(gen_tokens_src: str) -> list:
    m = re.search(r'^AGENTS = \[(.*?)\]', gen_tokens_src, re.M)
    assert m, "the AGENTS roster's shape changed -- update this regex"
    names = re.findall(r'"(\w+)"', m.group(1))
    assert names, "no agent names extracted from AGENTS -- the regex has rotted"
    return names


# ── I-1 — Phase 6 no longer claims to write local agents' tokens ────────────

def test_phase6_does_not_claim_to_write_local_agent_tokens():
    """I-1. The false claim measured against v0.9.31: Phase 6 said
    generate_tokens.py's write-through mint "writes each LOCAL agent's
    token straight into that agent's own skill .env" -- true only once a
    local skill directory exists, which nothing has created yet at this
    point in the (previously) documented order. Phase 6 must describe what
    it can actually deliver on a fresh host, and say where a local agent's
    token now comes from instead.
    """
    agents_md = _read("AGENTS.md")
    section = _section(agents_md, "### Phase 6", "### Phase 7")

    assert "writes each LOCAL agent's token straight into" not in section, (
        "Phase 6 still claims to write local agents' tokens into their own "
        "skill directories -- false on a fresh host, since Phase 8 (which "
        "creates those directories) is documented to run AFTER this phase."
    )
    assert "Phase 8" in section, (
        "Phase 6 no longer points to where a local agent's token actually "
        "gets minted (Phase 8) -- an operator reading only this phase would "
        "have no idea the four CLI agents are handled elsewhere."
    )


def test_phase6_names_are_derived_from_generate_tokens_own_roster():
    """I-1 (continued). Cross-checks Phase 6's prose against generate_
    tokens.py's OWN data, rather than a hardcoded agent list here: every
    name with no LOCAL_SKILL_ENV_PATHS entry (a REMOTE/registry identity,
    always mintable on a fresh host) must be named as something Phase 6
    mints; every name WITH one (a local CLI agent, always REFUSED here on a
    fresh host) must be named as something Phase 6 does NOT deliver. If the
    roster or the local-path dict grows a new name, this test forces Phase
    6's prose to be revisited instead of silently going stale.
    """
    gen = _read("shared-memory", "scripts", "generate_tokens.py")
    local_agents = _extract_local_skill_agents(gen)
    roster = _extract_agents_roster(gen)
    remote_agents = [a for a in roster if a not in local_agents]
    assert remote_agents, "every roster agent has a local path -- update this test's premise"

    agents_md = _read("AGENTS.md")
    section = _section(agents_md, "### Phase 6", "### Phase 7")

    missing_remote = [a for a in remote_agents if a not in section]
    assert not missing_remote, (
        f"generate_tokens.py has no LOCAL_SKILL_ENV_PATHS entry for "
        f"{missing_remote} (so a bulk mint always succeeds for it, even on "
        "a bare fresh host) -- Phase 6 must name it among what it mints."
    )
    missing_local = [a for a in local_agents if a not in section]
    assert not missing_local, (
        f"{missing_local} have a LOCAL_SKILL_ENV_PATHS entry, so Phase 6's "
        "bulk mint will try to write through to them and get REFUSED on a "
        "fresh host -- Phase 6 must name them among what it does NOT "
        "deliver there, not silently omit them."
    )


# ── I-2 — Phase 8 mints each local agent AFTER installing its package ───────

def test_phase8_mints_each_local_agent_after_installing_its_package():
    """I-2. Phase 8 must sequence, per local agent: (1) create the skill
    directory, (2) mint via --add with an explicit --install-path, (3)
    install the rest of the package. Reversing (1) and (2) hits generate_
    tokens.py's own D19 refusal (_write_agent_token_file returns False for
    a directory that doesn't exist -- mint()/add_agent() then REFUSE that
    agent outright). Reversing (2) and (3) would let update_skill.sh/
    sync_skills.sh's .env merge run before the token it needs to preserve
    even exists.
    """
    gen = _read("shared-memory", "scripts", "generate_tokens.py")
    assert "--add" in gen and "--install-path" in gen, (
        "generate_tokens.py's --add/--install-path flags were renamed -- "
        "update this test before trusting it"
    )

    agents_md = _read("AGENTS.md")
    section = _section(agents_md, "### Phase 8 —", "### Phase 8b")

    code_block = re.search(r"```bash\n(.*?)```", section, re.S)
    assert code_block, "Phase 8 no longer shows a command block -- update this test"
    body = code_block.group(1)

    mkdir_pos = body.find("mkdir")
    add_pos = body.find("--add")
    install_path_pos = body.find("--install-path")
    sync_pos = max(body.find("sync_skills.sh"), body.find("update_skill.sh"))

    assert -1 not in (mkdir_pos, add_pos, install_path_pos, sync_pos), (
        "Phase 8's command block is missing one of mkdir / --add / "
        f"--install-path / sync_skills.sh|update_skill.sh (positions: "
        f"mkdir={mkdir_pos}, --add={add_pos}, --install-path={install_path_pos}, "
        f"sync={sync_pos})"
    )
    assert mkdir_pos < add_pos < install_path_pos < sync_pos, (
        "Phase 8's commands are no longer in mkdir -> --add --install-path "
        "-> sync_skills.sh/update_skill.sh order. --add refuses a directory "
        "that does not exist yet (D19), and the package sync must come "
        "after the token is written so it MERGES into .env instead of "
        "racing to create it first."
    )

    add_line = next(ln for ln in body.splitlines() if "--add" in ln)
    assert "--install-path" in add_line, (
        "Phase 8's --add invocation does not pass --install-path on the "
        "SAME command -- a local agent's mint must always register an "
        "explicit install path, never rely on a seeded guess (which "
        "generate_tokens.py only ever makes ONCE, on the very first bulk "
        "mint, and Phase 6's own fresh-host refusals consume without "
        "persisting)."
    )


# ── I-3 — Phase 7 cannot be satisfied by an auth-off install ────────────────

def test_phase7_check_keys_on_payload_shape_not_status_or_bare_auth_required_true():
    """I-3. Derives the actual /health contract from hive_mind_proxy.py's
    own source (never executed): the slim {"status","version","api_version"}
    shape (no "auth_required" key at all) is served to an anonymous caller
    ONLY when AUTH_CONFIGURED_AT_STARTUP is true; an auth-OFF install
    (AUTH_CONFIGURED_AT_STARTUP false) serves the FULL payload -- including
    "auth_required": false spelled out in the JSON -- to every caller,
    always at HTTP 200 (even a REJECTED bearer token gets the slim shape at
    200, never a 401 -- see handle_health's own docstring). So a documented
    pass condition of "the bare curl should show auth_required:true" can
    never be satisfied by ANY real state, and a pass condition that merely
    checks for HTTP 200 is satisfied by BOTH real states. Phase 7 must key
    its check on payload SHAPE.
    """
    proxy_src = _read("shared-memory", "scripts", "hive_mind_proxy.py")

    handler = re.search(r"async def handle_health\(.*?\n(?:async def |\Z)", proxy_src, re.S)
    assert handler, "handle_health has moved or been renamed -- update this test"
    body = handler.group(0)

    # ⚠ THE GATE'S SHAPE CHANGED IN v0.9.54 AND THIS GUARD FIRED, exactly as it
    # is designed to: `handle_health` now resolves the identity ONCE into a
    # local (it needs the name itself for the A-4 `agent`/`role` keys, not just
    # its truthiness) and the slim gate tests that local. The CONTRACT is
    # unchanged and is what these two lines pin: the identity is resolved only
    # when auth is configured, and the slim payload is served exactly when auth
    # is configured AND no identity resolved. Re-verified by hand before the
    # regex was updated — which is the whole point of the guard failing loudly
    # rather than matching loosely.
    assert re.search(
        r"identity = _safe_resolve_identity\(request\) if AUTH_CONFIGURED_AT_STARTUP else None",
        body,
    ), "the identity resolution changed shape -- update the regex before trusting it"
    assert re.search(
        r"if AUTH_CONFIGURED_AT_STARTUP and not identity:",
        body,
    ), "the slim-response gate condition changed shape -- update the regex before trusting it"
    assert '"status": checks["status"], "version": checks["version"],' in body, (
        "the slim response's key set changed shape -- update the regex before trusting it"
    )
    assert 'checks["auth_required"] = AUTH_CONFIGURED_AT_STARTUP' in proxy_src, (
        "the full payload's auth_required field is no longer derived from "
        "AUTH_CONFIGURED_AT_STARTUP -- update this test before trusting it"
    )

    agents_md = _read("AGENTS.md")
    section = _section(agents_md, "### Phase 7", "### Phase 8")

    assert "auth_required" in section and re.search(r"auth_required.{0,10}false", section), (
        "Phase 7 must describe the auth-OFF signature explicitly -- the "
        "full anonymous payload spells out auth_required:false, which is "
        "how ONE bare curl proves auth is off, not merely 'not yet "
        "verified'."
    )
    assert re.search(r"never distinguish|does not distinguish|status codes never", section, re.I), (
        "Phase 7 must say plainly that HTTP status (200 either way, even "
        "for a REJECTED bearer token) cannot be used to tell "
        "auth-configured apart from auth-off."
    )

    # The insufficient pass condition this invariant exists to kill: reading
    # auth_required:true as the expected outcome of the UNAUTHENTICATED
    # (bare) call. That call can only ever produce the 3-key slim shape
    # (auth on, no auth_required key at all) or the full payload with
    # auth_required:FALSE (auth off) -- auth_required:true requires a VALID
    # bearer token, which this phase cannot assume exists yet.
    stale_pass_condition = re.search(r'expect\s+`?"?auth_required"?:\s*true', section, re.I)
    assert not stale_pass_condition, (
        "Phase 7 still reads like it expects auth_required:true from the "
        "bare/anonymous curl -- that response is never able to produce it; "
        "it is either the 3-key slim shape or auth_required:false."
    )


# ── I.1 (SEC round, ADV1-9): no `export AGENT_TOKEN=<...>`-shaped paste ─────

def test_agents_md_has_no_export_agent_token_paste_shape():
    """The old instructional shape -- `export AGENT_TOKEN=...` or
    `export AGENT_TOKEN=<an agent token>` -- required an operator (or an
    agent) to already HOLD the raw token value to paste in, which for an
    agent means reading it out of the skill .env first: `. file` EXECUTES
    the file rather than just reading it, and cat/grep puts the raw
    credential in the agent's own transcript (fact:1499). AGENTS.md now
    reads the value via `sed` into a shell variable, never sourcing the
    file and never printing the value anywhere. This test greps the whole
    document for any surviving `export AGENT_TOKEN=<`-shaped paste (the
    ellipsis/placeholder forms this fix removed), rather than pinning the
    two known line numbers, so a THIRD site introduced later fails here
    too."""
    agents_md = _read("AGENTS.md")
    assert not re.search(r"export AGENT_TOKEN=", agents_md), (
        "AGENTS.md still has an `export AGENT_TOKEN=<value>`-shaped paste -- "
        "read it via the AGENT_ENV + sed shape instead (see Phase 9 / "
        "Upgrade), never `. file` (executes it) and never cat/grep it "
        "(fact:1499: a raw credential must never pass through an agent's "
        "own transcript)"
    )


# ── Fix round F9 (QA MED-4): the same no-paste grep, widened past AGENTS.md
#    to the rest of the agent-readable doc/script set. README.md:459 is
#    deliberately excluded -- it is the human-voice Quick Start, which per
#    this repo's own ground rules a builder never rewrites (a fix there is
#    a proposal in the operator's own voice, left for the merger).

@pytest.mark.parametrize("relpath", [
    "shared-memory/Documentation/postflight.md",
    "shared-memory/scripts/postflight.sh",
])
def test_doc_set_has_no_export_agent_token_paste_shape(relpath):
    """postflight.md's Quick Start and postflight.sh's own header comment +
    runtime A1 failure messages used to instruct the same unsafe paste
    AGENTS.md's fix already closed -- an agent following postflight.md, or
    reading postflight.sh's own error message after a failed run, would do
    exactly what fact:1499 forbids. Same grep, same property, different
    file."""
    text = _read(relpath)
    assert not re.search(r"export AGENT_TOKEN=", text), (
        f"{relpath} still has an `export AGENT_TOKEN=<value>`-shaped paste -- "
        "read it via the AGENT_ENV + sed shape instead (see AGENTS.md Phase 9 "
        "/ Upgrade for the pattern), never `. file` (executes it) and never "
        "cat/grep it (fact:1499)"
    )


def test_agents_md_agent_token_reads_use_the_non_executing_sed_shape():
    """Positive pin: both surviving AGENT_TOKEN reads (Phase 9, Upgrade) use
    the `sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV"` shape -- never `. file`
    (which EXECUTES the env file as shell) and never cat/grep the file
    (which would put the raw value in a tool-call transcript)."""
    agents_md = _read("AGENTS.md")
    occurrences = re.findall(
        r"""AGENT_TOKEN=\$\(sed -n 's/\^AGENT_TOKEN=//p' "\$AGENT_ENV" \| head -1\); export AGENT_TOKEN""",
        agents_md,
    )
    # Fix round finding 11 (QA LOW): a count pin (`== 2`) fails a
    # legitimately ADDED third site for no reason -- the property that
    # actually matters ("every AGENT_TOKEN read uses this safe shape") is
    # what the sibling negative test above already covers; this positive
    # pin only needs to confirm the two KNOWN sites (Phase 9, Upgrade)
    # still use it, not that nothing else ever will.
    assert len(occurrences) >= 2, (
        f"expected at least 2 AGENT_ENV/sed-shaped AGENT_TOKEN reads (Phase 9 + "
        f"Upgrade), found {len(occurrences)}"
    )
    assert not re.search(r"^\s*\.\s+\$?\{?AGENT_ENV\}?\s*$", agents_md, re.M), (
        "AGENTS.md sources the skill .env with `. file` somewhere -- that "
        "EXECUTES the credential file rather than reading one value out of "
        "it (ADV1-9)"
    )
