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
       to print and paste.
    2. For every agent with a LOCAL skill install found on this machine
       (LOCAL_SKILL_ENV_PATHS below), writes that agent's plaintext token
       directly into its skill .env (mode 600 from the first byte) and
       prints only the destination path — never the token value.
    3. For every agent with no local install found — a genuinely remote
       agent, or one this script has no fixed local path for (LM Studio
       takes AGENT_TOKEN from mcp.json's own env block; the monitor
       dashboard lives in its own repo) — nothing is written or printed;
       use --reveal to see that one token, on the SAME invocation.

  uv run python shared-memory/scripts/generate_tokens.py --reveal codex
    Mints as normal, but ALSO prints the codex token's raw value —
    labelled with a loud warning. Run this yourself; NEVER pipe it through
    an agent (agent transcripts are durable, so "shown once" becomes
    "stored forever"). Repeatable: --reveal codex --reveal grok.
    IMPORTANT: --reveal only ever shows a token from the SAME mint this
    invocation performs. There is no way to reveal a token minted by an
    EARLIER invocation — every invocation of this script (with or without
    --reveal) mints a fresh set of tokens for every agent, so running
    `--reveal <name>` later, as a separate command, is a FULL ROTATION of
    every agent's token, not a free peek at one already registered.

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

AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity", "monitor"]

# Read-only identities: registered like any agent, but confined by AGENT_ROLES
# to GET /health, GET /memory/telemetry, and POST /memory/graph (read-only
# Cypher). "monitor" is the shared-memory-monitor dashboard — a read-only ops
# client that must not borrow a write-capable agent token.
READ_ONLY_AGENTS = ["monitor"]

# Known LOCAL skill-install .env paths — one per CLI agent this framework
# ships a thin-client skill to (mirrors sync_skills.sh's default AGENTS
# list: ~/.claude, ~/.codex, ~/.gemini, ~/.grok). Deliberately does NOT
# include every name in AGENTS: LM Studio takes AGENT_TOKEN from mcp.json's
# own env block, never a skill .env; "antigravity" and "gemini" both
# plausibly resolve to ~/.gemini/skills/shared-memory — ambiguous, so left
# OUT rather than guessed (a wrong guess here writes a token into the wrong
# install); "monitor" (the dashboard) lives in a sibling repo whose install
# path this script has no visibility into. All agents absent from this map
# are treated as REMOTE for the mint flow — --reveal is the only way to see
# their token.
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


def _mint_all() -> dict:
    return {a: f"tok_{secrets.token_urlsafe(24)}" for a in AGENTS}


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


def mint() -> "tuple[dict, dict]":
    """Mint a fresh token for every agent, write-through every LOCAL one,
    and print only names/digests/destination paths. Returns (tokens,
    digests) so main() can serve --reveal from the SAME minted set without
    re-parsing anything."""
    tokens = _mint_all()
    digests = {a: _digest(t) for a, t in tokens.items()}

    print("=== Gateway .env — add this line (digest form; safe to print/paste) ===")
    print("AGENT_TOKENS=" + ",".join(f"{a}:sha256:{digests[a]}" for a in AGENTS))
    print()
    print("=== Gateway .env — optional read-only roles ===")
    print("AGENT_ROLES=" + ",".join(f"{a}:read" for a in READ_ONLY_AGENTS))
    print("# read-role agents may reach only GET /health, GET /memory/telemetry,")
    print("# and POST /memory/graph (read-only Cypher). All other routes → 403.")
    print()

    print("=== Per-agent tokens — written through, never printed ===")
    for a in AGENTS:
        path = LOCAL_SKILL_ENV_PATHS.get(a)
        written = False
        if path:
            try:
                written = _write_agent_token_file(path, tokens[a])
            except AgentEnvIsSymlink:
                print(f"  {a:15}  REFUSED — {path} is a symlink; not following it")
                print("                   (same-uid agents are treated as adversarial —")
                print("                   replace it with a real file and re-run, or reveal:")
                print(f"                   generate_tokens.py --reveal {a}")
                continue
        if written:
            print(f"  {a:15}  written → {path}  (mode 600)")
        else:
            print(f"  {a:15}  REMOTE / no local install found — reveal with:")
            print(f"                   generate_tokens.py --reveal {a}")
    print()
    print("Each agent must use its own distinct token — never share tokens across agents.")

    return tokens, digests


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

    unknown = [n for n in args.reveal if n not in AGENTS]
    if unknown:
        print(f"✗ unknown agent(s) for --reveal: {', '.join(unknown)} "
              f"(known: {', '.join(AGENTS)})", file=sys.stderr)
        return 1

    tokens, _digests = mint()

    if args.reveal:
        print()
        print("⚠ REVEALING raw token value(s) below — run this yourself, NEVER through")
        print("  an agent. Agent transcripts are durable: piping this output through an")
        print("  agent turns \"shown once\" into \"stored forever\".")
        for name in args.reveal:
            print(f"  {name}: AGENT_TOKEN={tokens[name]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
