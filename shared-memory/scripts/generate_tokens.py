#!/usr/bin/env python3
"""
Generate AGENT_TOKENS for the gateway and per-agent AGENT_TOKEN values.

MINT FLOW (RULED, Credential_Custody_Plan_2026-08-14, PR A2): no secret
token value is ever printed to stdout. An agent-driven install captures
stdout into a durable transcript — "shown once" silently becomes "stored
forever" — so this script writes tokens straight into the files that need
them and prints only names, digests, and destination paths.

  uv run python shared-memory/scripts/generate_tokens.py
    1. Prints the AGENT_TOKENS=... line for the GATEWAY .env, in DIGEST
       form (name:sha256:<hex>) — a digest is not a secret, so it is safe
       to print and paste. Also prints an AGENT_INSTALLS=... line: the
       registry of name:path entries this mint actually wrote through to
       (see AGENT_INSTALLS / _load_agent_installs_registry() below) —
       bootstrap_tokens.sh persists this exactly like AGENT_TOKENS.
    2. For every agent with a REGISTERED install path whose skill directory
       already exists on this machine, writes that agent's plaintext token
       directly into its skill .env (mode 600 from the first byte) and
       prints only the destination path — never the token value.
    3. For every agent with no registered install path — a genuinely
       remote agent, or one this script has no fixed local path for (LM
       Studio takes AGENT_TOKEN from mcp.json's own env block; the monitor
       dashboard lives in its own repo) — nothing is written or printed;
       use --reveal to see that one token, on the SAME invocation.
    4. For every agent with a REGISTERED install path whose skill
       directory does NOT YET exist (install-path hardening, fresh-host
       finding D19): REFUSES that one agent outright — prints the exact
       directory expected and mints nothing for it, rather than silently
       discarding the plaintext while its digest still lands in
       AGENT_TOKENS. The old behaviour left an unrecoverable entry: the
       operator was told a token existed that no agent ever received, and
       the only fix was rotating everyone. Recovery now is: install the
       skill package (create that directory) and re-run — the bulk mint
       for a rotation, or --add for a single agent.

  uv run python shared-memory/scripts/generate_tokens.py --add codex \
      --install-path ~/.codex/skills/shared-memory/.env
    Additive mint (roster growth without rotation): mints exactly ONE new
    token for the named agent. Every OTHER agent's digest in AGENT_TOKENS
    is reproduced byte-identical — this does not touch them at all, unlike
    a bulk mint (which mints a fresh set for the WHOLE roster every time).
    Refuses loudly if the name is already registered (no silent rotation of
    one agent — the only rotation this framework has is bootstrap_tokens.sh
    --force, which is deliberately all-or-nothing). --install-path is
    optional — omit it for a remote agent and use --reveal instead. Two
    agents MAY legitimately share one install path (one tool reading
    another's skill directory), but a write-through mint into a path
    another REGISTERED agent already holds a live token at would clobber
    that token — refused, naming both agents, rather than silently
    overwritten. Prints the MERGED AGENT_TOKENS= (and, with --install-path,
    AGENT_INSTALLS=) line for bootstrap_tokens.sh to write in place.

  uv run python shared-memory/scripts/generate_tokens.py --reveal codex
    Mints as normal, but ALSO prints the codex token's raw value —
    labelled with a loud warning. Run this yourself; NEVER pipe it through
    an agent (agent transcripts are durable, so "shown once" becomes
    "stored forever"). Repeatable: --reveal codex --reveal grok. Works
    with --add too: --add codex --reveal codex reveals the one agent just
    added.
    IMPORTANT: --reveal only ever shows a token from the SAME mint this
    invocation performs. There is no way to reveal a token minted by an
    EARLIER invocation — a bulk mint (with or without --reveal) mints a
    fresh set of tokens for every agent in the roster, so running
    `--reveal <name>` later, as a separate bulk-mint command, is a FULL
    ROTATION of every agent's token, not a free peek at one already
    registered. (--add mints only the one new agent — it never rotates
    anyone else regardless of --reveal.)

  uv run python shared-memory/scripts/generate_tokens.py --convert-digests
    Rewrites the GATEWAY .env's existing AGENT_TOKENS line from plaintext
    (or mixed) form to pure digest form (name:sha256:<hex>), IN PLACE,
    idempotent — does not mint anything new. Prints only names + digests.
    RULED, Xenofon 2026-08-14: as of v0.9.3 the gateway REFUSES TO START
    with even one plaintext AGENT_TOKENS entry present (SEC-11) — this is
    the one-command fix. Existing client tokens are unaffected; only the
    gateway's own storage format changes.

  uv run python shared-memory/scripts/generate_tokens.py --digest backup
    Prints ONLY a digest entry (name:sha256:<hex>) for an OPERATOR-SUPPLIED
    token you already chose yourself — e.g. the BACKUP_ADMIN_TOKEN in
    .env.example — read from STDIN, never argv (argv is visible to `ps`
    and shell history). Mints nothing, writes nothing:
      printf '%s' tok_backup_xxx | uv run python \\
        shared-memory/scripts/generate_tokens.py --digest backup
"""
import argparse
import errno
import hashlib
import os
import secrets
import sys
import tempfile


# The DEFAULT roster for a first-ever bulk mint (no AGENT_INSTALLS/AGENT_TOKENS
# registry on disk yet). NOT the whole story any more (install-path hardening,
# D19/roster fix): a bulk mint after that point rolls in every name already
# registered in the gateway .env's AGENT_TOKENS too (see _resolve_roster()
# below), so an agent added later via --add is never silently dropped from a
# --force rotation just because it is absent from this fixed list.
AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity", "monitor"]

# Read-only identities: registered like any agent, but confined by AGENT_ROLES
# to GET /health, GET /memory/telemetry, and POST /memory/graph (read-only
# Cypher). "monitor" is the shared-memory-monitor dashboard — a read-only ops
# client that must not borrow a write-capable agent token.
READ_ONLY_AGENTS = ["monitor"]

# SEED DEFAULTS offered at first bootstrap ONLY — one per CLI agent this
# framework ships a thin-client skill to (mirrors sync_skills.sh's default
# AGENTS list: ~/.claude, ~/.codex, ~/.gemini, ~/.grok). Deliberately does NOT
# include every name in AGENTS: LM Studio takes AGENT_TOKEN from mcp.json's
# own env block, never a skill .env; "antigravity" and "gemini" both
# plausibly resolve to ~/.gemini/skills/shared-memory — ambiguous, so left
# OUT rather than guessed (a wrong guess here writes a token into the wrong
# install); "monitor" (the dashboard) lives in a sibling repo whose install
# path this script has no visibility into.
#
# Install-path hardening (D19/roster fix, ruled): an install path is OWNED
# information about a host, not something a naming convention can be trusted
# to reproduce — so this dict is consulted exactly ONCE, the very first time
# mint() runs against a gateway .env with no AGENT_INSTALLS line at all (see
# _load_agent_installs_registry()). That one seeding is itself recorded back
# into AGENT_INSTALLS as an explicit registration; every mint after that reads
# ONLY the registry — an agent absent from it is REMOTE, full stop, never
# re-guessed from its name. --reveal is the only way to deliver a remote
# agent's token.
LOCAL_SKILL_ENV_PATHS = {
    "claude": os.path.expanduser("~/.claude/skills/shared-memory/.env"),
    "codex":  os.path.expanduser("~/.codex/skills/shared-memory/.env"),
    "gemini": os.path.expanduser("~/.gemini/skills/shared-memory/.env"),
    "grok":   os.path.expanduser("~/.grok/skills/shared-memory/.env"),
}

# Gateway .env candidate order — matches every other loader in this family
# (apply.py / secure_env.py / bootstrap_tokens.sh): shared-memory/.env
# first, the repo-root .env as the pre-0.6 fallback.
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_GATEWAY_ENV = os.path.join(_HERE, "..", ".env")
if not os.path.exists(_DEFAULT_GATEWAY_ENV):
    _fallback = os.path.join(_HERE, "..", "..", ".env")
    if os.path.exists(_fallback):
        _DEFAULT_GATEWAY_ENV = _fallback


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _mint_one() -> str:
    return f"tok_{secrets.token_urlsafe(24)}"


def _read_env_raw_value(env_path: str, key: str) -> "str | None":
    """Return the raw (unstripped-of-quotes, but whitespace-stripped) value
    of `key`'s LIVE (non-comment) assignment in env_path, or None when the
    file doesn't exist or carries no such line. Parsed by hand, one
    `key=val` per line — same form as apply.py's _load_env() and every other
    loader in this family; never `import dotenv` (CLAUDE.md Group 4: an env
    loader must not depend on a library that might not be installed). A
    commented-out placeholder (`# AGENT_TOKENS=`) is indistinguishable here
    from an absent line, by design — both mean "nothing registered yet"."""
    if not env_path or not os.path.isfile(env_path):
        return None
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip()
    return None


def _parse_agent_installs(raw: str) -> "dict[str, str]":
    """Parse an AGENT_INSTALLS= value: comma-separated name:path entries.
    Split on the FIRST colon only (str.partition), so a path containing a
    colon of its own still parses whole — mirrors _load_agent_tokens()'s own
    "everything after the first colon is the value" rule in coordinator.py
    for the equivalent case in AGENT_TOKENS."""
    installs: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, path = pair.partition(":")
        name, path = name.strip(), path.strip()
        if not sep or not name or not path:
            continue
        installs[name] = path
    return installs


def _load_agent_installs_registry(env_path: str) -> "tuple[dict[str, str], bool]":
    """Read the AGENT_INSTALLS registry from the gateway .env.

    Returns (installs, registry_present). registry_present is True the
    instant a LIVE AGENT_INSTALLS= line exists at all — even one that parses
    to zero entries — because that is the signal that first bootstrap
    already happened and the registry, however sparse, is now authoritative.
    False (no line, or no file) is the ONLY state in which
    LOCAL_SKILL_ENV_PATHS's guessed paths are allowed to seed anything —
    every mint after that reads the registry and nothing else (see the
    module-level LOCAL_SKILL_ENV_PATHS docstring)."""
    raw = _read_env_raw_value(env_path, "AGENT_INSTALLS")
    if raw is None:
        return {}, False
    return _parse_agent_installs(raw), True


def _parse_agent_tokens_line(raw: str) -> "dict[str, str]":
    """Parse an AGENT_TOKENS= value (digest form, as this script always
    emits) into {name: 'name:sha256:hex'} -- the WHOLE entry, verbatim, so a
    caller can reproduce another agent's registration byte-identical without
    ever recomputing or re-deriving it. Anything not a clean 3-part
    name:sha256:hex entry is skipped -- this script never writes any other
    shape, and the only caller (add_agent(), for I-A1) only needs to
    preserve what a PRIOR run of this same script produced."""
    entries: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        parts = pair.split(":", 2)
        if len(parts) == 3 and parts[1].strip().lower() == "sha256":
            entries[parts[0].strip()] = pair
    return entries


def _resolve_roster(env_path: str) -> "list[str]":
    """The names to mint for in a BULK mint: every name already registered
    in the gateway .env's AGENT_TOKENS, UNION the default AGENTS list --
    never AGENTS alone. Without the union, an agent added later via --add
    would be silently dropped the next time someone rotates everyone
    (bootstrap_tokens.sh --force): --force calls this same bulk mint() path,
    and a name --force's own roster doesn't know about never gets a fresh
    token, never gets re-registered, and quietly stops being trusted -- the
    exact "roster is hardcoded" defect this fix exists for. Order: AGENTS
    first (stable, documented), then any additional registered names in the
    order the registry lists them.
    """
    raw = _read_env_raw_value(env_path, "AGENT_TOKENS") or ""
    existing_names = list(_parse_agent_tokens_line(raw).keys())
    roster = list(AGENTS)
    for n in existing_names:
        if n not in roster:
            roster.append(n)
    return roster


class AgentEnvIsSymlink(Exception):
    """Raised by _write_agent_token_file when `path` is a symlink. This
    framework's threat model treats other same-uid agent processes as
    adversarial (S-01/S-10's whole premise), so writing a live bearer token
    through a symlink -- which could point anywhere another process placed
    it -- is refused outright rather than followed."""


def _write_agent_token_file(path: str, token: str) -> bool:
    """Write-through: set AGENT_TOKEN=<token> in the skill .env at `path`,
    mode 600 BEFORE any content is written (S-01, tightened per finding 4
    of the A2 security review) — no create-then-chmod window and no
    write-then-chmod window either. Preserves every other line already in
    the file; replaces only an existing AGENT_TOKEN= line (or appends one).

    `os.open()`'s mode argument only takes effect when the file is CREATED
    — for a PRE-EXISTING file (the entire measured population at review
    time: every installed skill .env was already 0644) the old mode governs
    until something changes it. Writing the token first and `chmod`ing
    after therefore left it world-readable for the whole write; this now
    calls `os.fchmod()` on the open fd immediately, before the first byte
    of content, closing that window.

    Refuses (raises AgentEnvIsSymlink) rather than following a symlink at
    `path` — `os.O_NOFOLLOW` makes the kernel enforce this atomically, with
    no check-then-open race for another same-uid process to win.

    Returns False without writing anything when the skill directory itself
    doesn't exist — nothing to write through to; this agent is treated as
    not-installed-locally, same as a genuinely remote one.
    """
    skill_dir = os.path.dirname(path)
    if not os.path.isdir(skill_dir):
        return False
    if os.path.islink(path):
        raise AgentEnvIsSymlink(path)
    lines: list[str] = []
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.startswith("AGENT_TOKEN="):
                    continue
                lines.append(line.rstrip("\n"))
    lines.append(f"AGENT_TOKEN={token}")
    try:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AgentEnvIsSymlink(path) from exc
        raise
    os.fchmod(fd, 0o600)  # BEFORE any write — closes the world-readable window
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return True


def mint(
    env_path: str = _DEFAULT_GATEWAY_ENV, roster: "list[str] | None" = None,
) -> "tuple[dict, dict]":
    """Mint a fresh token for every agent in `roster` (default: resolved by
    _resolve_roster() -- AGENTS union whatever's already registered),
    write-through every agent with a REGISTERED install path whose skill
    directory exists, and print only names/digests/destination paths.
    Returns (tokens, digests) so main() can serve --reveal from the SAME
    minted set without re-parsing anything.

    Install-path resolution (D19/roster fix, ruled -- see the
    LOCAL_SKILL_ENV_PATHS and AGENTS docstrings): reads the AGENT_INSTALLS
    registry from env_path. If no AGENT_INSTALLS line exists there at all
    (first bootstrap), seeds from LOCAL_SKILL_ENV_PATHS's guessed defaults
    -- that seeding is itself printed as this mint's AGENT_INSTALLS= line,
    turning a one-time guess into an explicit registration bootstrap_tokens.sh
    persists. Every mint after that reads ONLY the registry; an agent
    missing from it is REMOTE, full stop, never re-guessed from its name.

    Per agent, in `roster` order:
      - no registered path            -> REMOTE: token minted, digest
        registered, nothing written; --reveal is the only delivery path.
      - registered path, dir missing  -> REFUSED (D19): nothing minted for
        this agent at all -- no token, no digest, no AGENT_INSTALLS entry.
        The old behaviour minted anyway and silently discarded the
        plaintext, leaving an AGENT_TOKENS entry nobody could ever satisfy;
        the only recovery was rotating everyone. Now the operator gets the
        exact expected directory and a re-run command, and the recovery is
        cheap: install the skill package, then re-run (bulk, or --add).
      - registered path, dir exists   -> written through (mode 600),
        digest registered, AGENT_INSTALLS entry carried forward.
      - registered path is a symlink  -> REFUSED (pre-existing S-01/S-10
        threat model, unchanged): digest still registered (so --reveal can
        recover it), but nothing written and the AGENT_INSTALLS entry is
        dropped -- there is nowhere trustworthy that path could mean.
    """
    roster = _resolve_roster(env_path) if roster is None else roster
    installs, registry_present = _load_agent_installs_registry(env_path)
    if not registry_present:
        installs = dict(LOCAL_SKILL_ENV_PATHS)  # first-bootstrap seed, once

    tokens: dict[str, str] = {}
    digests: dict[str, str] = {}
    persisted_installs: dict[str, str] = {}
    lines: list[str] = []  # per-agent report lines, printed after the header blocks

    for a in roster:
        path = installs.get(a)

        if path is None:
            token = _mint_one()
            tokens[a] = token
            digests[a] = _digest(token)
            lines.append(f"  {a:15}  REMOTE / no local install found — reveal with:")
            lines.append(f"                   generate_tokens.py --reveal {a}")
            continue

        skill_dir = os.path.dirname(path)
        if not os.path.isdir(skill_dir):
            # D19: a REGISTERED path whose directory doesn't exist yet must
            # refuse outright -- minting a token nobody can receive, then
            # registering its digest anyway, is exactly the fresh-host
            # defect this fix exists for.
            lines.append(f"  {a:15}  REFUSED — expected directory {skill_dir} does not exist")
            lines.append(f"                   install the {a} skill package first, then re-run:")
            lines.append(f"                   generate_tokens.py --add {a} --install-path {path}")
            continue

        token = _mint_one()
        try:
            _write_agent_token_file(path, token)
        except AgentEnvIsSymlink:
            tokens[a] = token
            digests[a] = _digest(token)
            lines.append(f"  {a:15}  REFUSED — {path} is a symlink; not following it")
            lines.append("                   (same-uid agents are treated as adversarial —")
            lines.append("                   replace it with a real file and re-run, or reveal:")
            lines.append(f"                   generate_tokens.py --reveal {a}")
            continue

        tokens[a] = token
        digests[a] = _digest(token)
        persisted_installs[a] = path
        lines.append(f"  {a:15}  written → {path}  (mode 600)")

    print("=== Gateway .env — add this line (digest form; safe to print/paste) ===")
    print("AGENT_TOKENS=" + ",".join(f"{a}:sha256:{digests[a]}" for a in tokens))
    print()
    print("=== Gateway .env — optional read-only roles ===")
    print("AGENT_ROLES=" + ",".join(f"{a}:read" for a in READ_ONLY_AGENTS))
    print("# read-role agents may reach only GET /health, GET /memory/telemetry,")
    print("# and POST /memory/graph (read-only Cypher). All other routes → 403.")
    print()
    print("=== Gateway .env — install-path registry (sync exactly what's registered) ===")
    print("AGENT_INSTALLS=" + ",".join(f"{n}:{p}" for n, p in persisted_installs.items()))
    print()

    print("=== Per-agent tokens — written through, never printed ===")
    for line in lines:
        print(line)
    print()
    print("Each agent must use its own distinct token — never share tokens across agents.")

    return tokens, digests


def add_agent(
    name: str, install_path: "str | None" = None, env_path: str = _DEFAULT_GATEWAY_ENV,
) -> "tuple[int, str | None]":
    """Additive mint (roster growth without rotation, item 2): mint exactly
    ONE new token for `name`, leaving every OTHER agent's digest in
    AGENT_TOKENS byte-identical (I-A1) -- this never re-derives or
    recomputes another agent's entry, it copies it verbatim off disk. Prints
    the MERGED AGENT_TOKENS= (and, with install_path, AGENT_INSTALLS=) line
    for bootstrap_tokens.sh to write into the gateway .env in place; this
    function itself never touches the gateway .env, exactly like mint() --
    the per-agent skill .env is the only file written directly.

    Returns (rc, token): token is the raw minted value (needed so main() can
    serve --reveal for the SAME invocation, same contract as mint()) or None
    when nothing was minted. rc is 0 on success, 1 on refusal -- every
    refusal path below returns BEFORE anything is minted, written, or
    registered, so a refused --add leaves no trace at all.
    """
    existing_raw = _read_env_raw_value(env_path, "AGENT_TOKENS") or ""
    existing_entries = _parse_agent_tokens_line(existing_raw)
    if name in existing_entries:
        print(
            f"✗ {name!r} is already registered in AGENT_TOKENS — --add never "
            "silently rotates an existing agent's token. There is no "
            "single-agent rotation; to replace it, rotate EVERY agent "
            "deliberately: bootstrap_tokens.sh --force.",
            file=sys.stderr,
        )
        return 1, None

    installs, _present = _load_agent_installs_registry(env_path)

    if install_path is not None:
        # I-A2: two agents MAY legitimately share one install path (one tool
        # reading another's skill directory) -- but _write_agent_token_file()
        # REPLACES any existing AGENT_TOKEN= line at that path wholesale, so
        # writing THIS agent's token there would clobber whichever registered
        # agent already has a live token at the same path. Refuse, naming
        # both, rather than silently overwriting a working credential.
        clobbered = [n for n, p in installs.items() if p == install_path and n in existing_entries]
        if clobbered:
            print(
                f"✗ install path {install_path} is already registered to "
                f"{', '.join(sorted(clobbered))} with a live token — writing "
                f"{name}'s token there would overwrite it. Use a distinct "
                "path, or rotate both deliberately.",
                file=sys.stderr,
            )
            return 1, None

        skill_dir = os.path.dirname(install_path)
        if not os.path.isdir(skill_dir):
            print(
                f"✗ REFUSED — expected directory {skill_dir} does not exist. "
                f"Install the {name} skill package first, then re-run:\n"
                f"  generate_tokens.py --add {name} --install-path {install_path}",
                file=sys.stderr,
            )
            return 1, None

    token = _mint_one()
    if install_path is not None:
        try:
            _write_agent_token_file(install_path, token)
        except AgentEnvIsSymlink:
            print(
                f"✗ REFUSED — {install_path} is a symlink; not following it "
                "(same-uid agents are treated as adversarial). Replace it "
                "with a real file and re-run.",
                file=sys.stderr,
            )
            return 1, None

    digest = _digest(token)
    merged_entries = dict(existing_entries)
    merged_entries[name] = f"{name}:sha256:{digest}"

    print("=== Gateway .env — merged AGENT_TOKENS= line (write this in place) ===")
    print("AGENT_TOKENS=" + ",".join(merged_entries.values()))

    if install_path is not None:
        merged_installs = dict(installs)
        merged_installs[name] = install_path
        print()
        print("=== Gateway .env — merged AGENT_INSTALLS= line (write this in place) ===")
        print("AGENT_INSTALLS=" + ",".join(f"{n}:{p}" for n, p in merged_installs.items()))
        print()
        print(f"  {name:15}  written → {install_path}  (mode 600)")
    else:
        print()
        print(f"  {name:15}  REMOTE / no install path given — reveal with:")
        print(f"                   generate_tokens.py --add {name} --reveal {name}")

    return 0, token


def convert_digests(env_path: str) -> int:
    """Rewrite the gateway .env's AGENT_TOKENS line from plaintext (or
    mixed) form to pure digest form (name:sha256:<hex>), in place.
    Idempotent — an entry already in digest form is left unchanged. Prints
    only names + digests, never a token value.

    Two fixes from the A2 security review:

    Finding 3 — this file also holds PG_PASSWORD, NEO4J_PASSWORD, and every
    provider key, so it is written ATOMICALLY: a temp file in the SAME
    directory (same filesystem, so the final `os.rename()` is atomic — no
    reader ever observes a partially-written .env), `fchmod`'d 600 before
    any content is written, `fsync`'d, then renamed over the original. And
    ANY malformed AGENT_TOKENS entry ABORTS the whole operation before a
    single byte is written — silently dropping a registry entry here would
    lock that agent out with no record of why, on the file that decides
    who the gateway trusts.

    Finding 12 — matches (and rebuilds) the AGENT_TOKENS line after a full
    `.strip()`, the same normalisation secure_env.load_split_env() applies
    when the gateway itself parses this file. The old right-strip-only
    match disagreed with the gateway on a leading-whitespace line: the
    gateway would parse and (correctly) refuse to start on a plaintext
    entry there, while this function reported "no AGENT_TOKENS= line
    found" — the one-command fix the refusal names would not have worked.
    """
    if not os.path.isfile(env_path):
        print(f"✗ {env_path} not found", file=sys.stderr)
        return 1
    with open(env_path) as f:
        lines = f.readlines()

    out_lines: list[str] = []
    converted: list[tuple[str, str]] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("AGENT_TOKENS="):
            found = True
            raw = stripped[len("AGENT_TOKENS="):]
            new_pairs = []
            for pair in raw.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                parts = pair.split(":", 2)
                if len(parts) == 3 and parts[1].strip().lower() == "sha256":
                    name, digest = parts[0].strip(), parts[2].strip().lower()
                    new_pairs.append(f"{name}:sha256:{digest}")   # already digest form
                    converted.append((name, digest))
                elif len(parts) == 2:
                    name, token = parts[0].strip(), parts[1].strip()
                    digest = _digest(token)
                    new_pairs.append(f"{name}:sha256:{digest}")
                    converted.append((name, digest))
                else:
                    print(
                        f"✗ malformed AGENT_TOKENS entry: {pair!r} — aborting, "
                        "nothing was written. Fix or remove this entry and re-run.",
                        file=sys.stderr,
                    )
                    return 1
            out_lines.append("AGENT_TOKENS=" + ",".join(new_pairs))
        else:
            out_lines.append(line.rstrip("\n"))

    if not found:
        print(f"✗ no AGENT_TOKENS= line found in {env_path}", file=sys.stderr)
        return 1

    content = "\n".join(out_lines) + "\n"
    env_dir = os.path.dirname(os.path.abspath(env_path)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=env_dir, prefix=".agent_tokens_convert_")
        os.fchmod(fd, 0o600)  # before any content -- no world-readable window
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp_path, env_path)  # atomic on the same filesystem
        tmp_path = None
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    print(f"✓ AGENT_TOKENS in {env_path} converted to digest form:")
    for name, digest in converted:
        print(f"  {name:15}  sha256:{digest}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--reveal", action="append", default=[], metavar="NAME",
        help="Print this agent's raw token to stdout after minting -- on THIS "
             "invocation ONLY. Run it yourself, NEVER through an agent. "
             "Repeatable (--reveal codex --reveal grok). NOTE: every "
             "invocation of this script mints a FRESH set of tokens for "
             "every agent -- running --reveal NAME later, as a separate "
             "command, is a FULL ROTATION of every agent's token, not a "
             "free peek at one already registered.",
    )
    ap.add_argument(
        "--convert-digests", nargs="?", const=_DEFAULT_GATEWAY_ENV,
        metavar="ENV_PATH",
        help="Convert an existing gateway .env's AGENT_TOKENS to digest form "
             "in place, instead of minting new tokens. Defaults to the "
             "gateway .env this script resolves the same way apply.py does.",
    )
    ap.add_argument(
        "--digest", metavar="NAME",
        help="Print a digest entry (NAME:sha256:<hex>) for an OPERATOR-"
             "SUPPLIED token read from STDIN -- never argv (argv is visible "
             "via `ps` and shell history). Mints nothing, writes nothing. "
             "Use for a token you chose yourself (e.g. the backup admin "
             "token in .env.example), not one this script minted. Usage: "
             "printf '%s' <token> | generate_tokens.py --digest <name>",
    )
    ap.add_argument(
        "--add", metavar="NAME",
        help="Additive mint: register exactly ONE new agent without "
             "rotating anyone else's existing token (every other digest in "
             "AGENT_TOKENS is reproduced byte-identical). Refuses if NAME "
             "is already registered -- there is no single-agent rotation, "
             "only bootstrap_tokens.sh --force for everyone. Combine with "
             "--install-path to write through to a local skill .env, or "
             "omit it (and use --reveal) for a remote agent.",
    )
    ap.add_argument(
        "--install-path", metavar="PATH",
        help="With --add: this agent's skill .env path (e.g. "
             "~/.codex/skills/shared-memory/.env), recorded in the "
             "AGENT_INSTALLS registry. Ignored without --add.",
    )
    args = ap.parse_args(argv)

    if args.digest is not None:
        raw_token = sys.stdin.read().strip()
        if not raw_token:
            print("✗ no token read from stdin", file=sys.stderr)
            return 1
        print(f"{args.digest}:sha256:{_digest(raw_token)}")
        return 0

    if args.convert_digests is not None:
        return convert_digests(args.convert_digests)

    if args.add is not None:
        rc, token = add_agent(args.add, install_path=args.install_path)
        if rc != 0:
            return rc
        unknown = [n for n in args.reveal if n != args.add]
        if unknown:
            print(
                f"✗ --add only mints {args.add!r} on this invocation -- "
                f"--reveal cannot show a token for: {', '.join(unknown)}",
                file=sys.stderr,
            )
            return 1
        if args.reveal:
            print()
            print("⚠ REVEALING raw token value(s) below — run this yourself, NEVER through")
            print("  an agent. Agent transcripts are durable: piping this output through an")
            print("  agent turns \"shown once\" into \"stored forever\".")
            print(f"  {args.add}: AGENT_TOKEN={token}")
        return 0

    roster = _resolve_roster(_DEFAULT_GATEWAY_ENV)
    unknown = [n for n in args.reveal if n not in roster]
    if unknown:
        print(f"✗ unknown agent(s) for --reveal: {', '.join(unknown)} "
              f"(known: {', '.join(roster)})", file=sys.stderr)
        return 1

    tokens, _digests = mint(env_path=_DEFAULT_GATEWAY_ENV, roster=roster)

    if args.reveal:
        print()
        print("⚠ REVEALING raw token value(s) below — run this yourself, NEVER through")
        print("  an agent. Agent transcripts are durable: piping this output through an")
        print("  agent turns \"shown once\" into \"stored forever\".")
        for name in args.reveal:
            if name not in tokens:
                print(f"  {name}: REFUSED this mint (no directory / not registered — see above)")
                continue
            print(f"  {name}: AGENT_TOKEN={tokens[name]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
