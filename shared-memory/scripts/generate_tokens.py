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
import stat
import sys
import tempfile


# The DEFAULT roster for a first-ever bulk mint (no AGENT_INSTALLS/AGENT_TOKENS
# registry on disk yet). NOT the whole story any more (install-path hardening,
# D19/roster fix): a bulk mint after that point rolls in every name already
# registered in the gateway .env's AGENT_TOKENS too (see _resolve_roster()
# below), so an agent added later via --add is never silently dropped from a
# --force rotation just because it is absent from this fixed list.
AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity", "monitor"]

# Read-only identities: registered like any agent, but confined to GET /health,
# GET /memory/telemetry, and POST /memory/graph (read-only Cypher).
#
# ⛔ THE ROSTER LIVES IN agent_roles.py, NOT HERE. It used to be defined in this
# file, which made the guarantee a minting convention: the gateway believed
# whatever AGENT_ROLES said, and an identity registered before the rule was
# honoured — or a line rewritten by an older tool — kept full access silently.
# The gateway now enforces the same roster on every request, so this module and
# the gateway can no longer disagree about who is read-only.
from agent_roles import (                                    # noqa: E402
    READ_ONLY_AGENTS, VALID_ROLES, read_only_agents,
    role_for_mint, enforce_roster,
)

# Kept as a module-local alias: this name is the one the CLI and the tests call.
role_for = role_for_mint

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


# Characters that can never appear in a registered agent NAME or install
# PATH (security-review findings F2/F2b, ruled I-A8): AGENT_TOKENS and
# AGENT_INSTALLS are comma-separated name:value pairs, so a `,` forges a
# second entry and a `:` (beyond the one splitting name from value) forges
# a bogus value -- reproduced: --install-path
# "/legit/.env,victim:/attacker/victim.env" made a SECOND registry entry,
# "victim", pointing at an attacker-controlled path, which the next bulk
# rotation would write a real agent's fresh token straight into (token
# theft). A newline forges a WHOLE SECOND .env assignment line -- this same
# file is passed to `docker compose --env-file`. A NUL byte terminates a C
# string early in anything that eventually shells out to it.
#
# Validated at INPUT, before anything is minted, written, or registered --
# CLAUDE.md's own rule applies here verbatim ("a separator that can occur
# in the data is not a delimiter, on either side"): an ESCAPE scheme would
# need a matching UNESCAPE in _parse_agent_installs, in bootstrap_tokens.sh's
# own grep/cut handling of these lines, and in any future reader -- one of
# those would eventually be missed. Refusing the character outright has no
# distributed contract to keep in sync.
_FORBIDDEN_REGISTRY_CHARS = (",", ":", "\n", "\r", "\x00")


def _validate_registry_field(value: str, what: str) -> None:
    """Refuse `value` (an agent NAME or an install PATH about to be written
    into AGENT_TOKENS/AGENT_INSTALLS) if it could inject a second registry
    entry or a second .env assignment line. Raises ValueError naming
    `what`, the value, and the offending character -- callers turn this
    into a loud, non-crashing refusal (see add_agent())."""
    if not value:
        raise ValueError(f"{what} is empty — refused")
    if value != value.strip():
        raise ValueError(
            f"{what} {value!r} has leading/trailing whitespace — refused "
            "(a registry entry is parsed by splitting on ',' after a bare "
            ".strip(), so padding here could silently merge with a "
            "neighbouring entry)"
        )
    for ch in _FORBIDDEN_REGISTRY_CHARS:
        if ch in value:
            raise ValueError(
                f"{what} {value!r} contains {ch!r}, which is a registry "
                "delimiter or line-injection character (',' and ':' "
                "separate AGENT_TOKENS/AGENT_INSTALLS entries; a newline "
                "would forge a second .env assignment; a NUL terminates a "
                "C string early) — refused"
            )


def _same_registered_file(path_a: str, path_b: str) -> bool:
    """Whether two install-path STRINGS name the same file on disk
    (security-review finding F3, ruled I-A9): the clobber check in
    add_agent() used to compare with literal string equality, which a
    `..`-aliased or symlink-aliased spelling of the identical path defeats
    trivially — two agents register "different" paths that are actually
    one file, a write-through mint silently overwrites the first agent's
    live token, and that agent starts authenticating AS the second (the
    gateway stamps `source` from the presented token's identity, so this
    is silent provenance corruption of every record that agent's identity
    ever touches afterward).

    os.path.realpath() resolves BOTH '..'/'.' components and any symlink
    in the chain to the SAME canonical form for two different spellings of
    one target. Safe to call on a path that doesn't exist yet (or no
    longer exists) — realpath degrades to abspath-style lexical resolution
    for a missing component rather than raising, so this never throws.

    Deliberately NOT the mechanism _write_agent_token_file() uses to REFUSE
    a symlink at write time (that check exists precisely to catch and
    refuse aliasing, not resolve through it) — this function answers a
    different, narrower question asked BEFORE any write is attempted:
    "would writing to path_b land on the exact same file path_a already
    points at". The write-time refusal still fires independently if a
    symlink is genuinely involved in resolving either path.
    """
    return os.path.realpath(path_a) == os.path.realpath(path_b)


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


def _parse_agent_roles_line(raw: str) -> "dict[str, str]":
    """Parse an AGENT_ROLES= value into {name: role}.

    Same hand-rolled shape as _parse_agent_tokens_line, and for the same reason:
    an --add must MERGE into whatever is already registered, never replace it.
    Emitting a roles line built only from the agent being added would silently
    drop `backup:admin` — turning the one credential confined to /admin/* into a
    full-access token, which is the exact inverse of what this line is for.
    Entries that are not a clean name:role pair are skipped, as elsewhere here.
    """
    roles: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        name, role = (p.strip() for p in pair.split(":", 1))
        if name and role:
            roles[name] = role
    return roles


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
    """Raised by _write_agent_token_file when ANY component of the
    registered path is a symlink — not only the leaf `.env` file itself.

    CRITICAL fix (security review, execution-reproduced): the ORIGINAL
    version of this guard applied `os.O_NOFOLLOW` to the final path
    component only. `os.path.isdir(skill_dir)` happily followed a symlink
    at the PARENT directory, so a same-uid process that replaced the
    parent with a symlink (e.g. `<skill>/` -> `/tmp/attacker/`) defeated
    the guard completely: the write reported SUCCESS and the live bearer
    token landed in the attacker-controlled directory, mode 600, readable
    only by the same uid that put it there — which is exactly the
    adversary this framework's threat model (S-01/S-10) says to assume.
    The docstring claimed "refused outright"; the code did not do that,
    which is worse than no guard, because the next reader trusts the claim
    and stops checking.

    Fixed by resolving the parent directory ONE path component at a time
    via `openat(..., O_NOFOLLOW)` (see _resolve_symlink_free_dir_fd()) —
    every hop is refused atomically if it is itself a symlink, with no
    separate check-then-open window for another same-uid process to win by
    swapping a component in between (this is why a `realpath()` COMPARISON
    was rejected as the fix: comparing before opening is still a
    check-then-use race under this framework's own threat model, which
    treats a racing same-uid process as an active adversary, not a
    theoretical one)."""


def _resolve_symlink_free_dir_fd(dir_path: str) -> int:
    """Open `dir_path` as a directory file descriptor, walking it ONE path
    component at a time via `openat(..., O_NOFOLLOW)` from the filesystem
    root — so EVERY component (not just the leaf, not just the immediate
    parent) is refused, atomically, if it is a symlink. Each hop's
    O_NOFOLLOW is enforced by the kernel on that single openat() call, so
    there is no separate stat-then-open step for a same-uid adversarial
    process to win a race on by swapping a component after it was checked
    but before it was used — see AgentEnvIsSymlink's docstring for why a
    realpath() comparison does not give this guarantee.

    Returns an open fd to the fully-resolved, symlink-free directory;
    caller is responsible for os.close()ing it once done (typically after
    also opening/writing the leaf file relative to this SAME fd via
    `dir_fd=`, so the leaf write inherits the identical guarantee instead
    of re-resolving the path — and re-resolving would itself reopen a
    check-then-use window).

    Raises AgentEnvIsSymlink naming the exact offending path prefix when
    any component is a symlink. Raises FileNotFoundError /
    NotADirectoryError (standard os.open semantics, unchanged) when a
    component doesn't exist, or genuinely isn't a directory (a plain file
    sitting where one was expected), — callers translate FileNotFoundError
    into "not installed locally" (D19), matching what the old
    `os.path.isdir()` pre-check used to signal, but now as part of the
    SAME atomic resolution instead of a separate non-atomic check.

    Linux quirk, probe-confirmed: `O_NOFOLLOW | O_DIRECTORY` on a symlink
    raises **ENOTDIR**, not ELOOP — a symlink node is never itself a
    directory, and O_NOFOLLOW blocks resolving it to find out what it
    points to, so the kernel reports "not a directory" rather than "too
    many levels of symbolic links". Reproduced: a symlinked skill directory
    (the exact attack this function exists to close) raised NotADirectoryError,
    which the FIRST version of this function let fall through to the
    generic `raise`, silently reported as "not installed locally" (D19)
    instead of the CRITICAL symlink refusal it actually is. ENOTDIR/ELOOP
    are therefore both treated as "possibly a symlink" and disambiguated by
    an `lstat()` of the SAME component, relative to the SAME still-open
    `fd` — this lstat is purely diagnostic (it only decides which
    EXCEPTION to raise for an attempt the kernel has already refused
    atomically), so it introduces no new race: nothing is written, and no
    security decision depends on what the lstat observes.
    """
    abs_path = os.path.abspath(dir_path)
    parts = [p for p in abs_path.split(os.sep) if p]
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    walked = ""
    try:
        for part in parts:
            walked += os.sep + part
            try:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd,
                )
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    try:
                        component_stat = os.lstat(part, dir_fd=fd)
                    except OSError:
                        raise exc from None
                    if stat.S_ISLNK(component_stat.st_mode):
                        raise AgentEnvIsSymlink(walked) from exc
                raise
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _write_agent_token_file(path: str, token: str) -> bool:
    """Write-through: set AGENT_TOKEN=<token> in the skill .env at `path`.

    Symlink safety (see AgentEnvIsSymlink / _resolve_symlink_free_dir_fd
    docstrings for the CRITICAL finding this fixes): resolves the PARENT
    directory component-by-component with O_NOFOLLOW at every hop, then
    opens/writes the leaf ONLY relative to that already-verified,
    already-open directory fd (`dir_fd=`) — never by re-resolving the full
    path string a second time, which would reopen the exact check-then-use
    race this function exists to close. The leaf itself is separately
    lstat'd (also relative to the verified dir_fd, also O_NOFOLLOW-safe)
    and refused if it is a symlink, preserving the original intent: a
    symlink where a live bearer token belongs is treated as tampering
    evidence to surface, not a target to write through OR to silently
    clobber.

    Atomicity (security-review finding F4 — a partial bulk-mint failure
    must never leave a HALF-written skill .env, which is a worse state
    than the file it replaced): writes to a fresh temp name in the SAME
    verified directory, fsyncs, then atomically renames it over the leaf
    (`os.rename(..., src_dir_fd=..., dst_dir_fd=...)`, both relative to the
    SAME fd) — a mid-write failure (ENOSPC, EPERM) leaves the ORIGINAL file
    completely untouched; the caller's failure handling (mint() / add_agent())
    can therefore trust that a raised exception here means NOTHING changed
    on disk for this agent, not "changed to something unknown".

    Mode 600 from the first byte (S-01, tightened per finding 4 of the A2
    security review): the temp file is created with mode 600 directly, and
    `os.fchmod()`'d again immediately after creation before any content is
    written — belt and braces against a hostile umask, no create-then-chmod
    window and no write-then-chmod window.

    Preserves every other line already in the file; replaces only an
    existing AGENT_TOKEN= line (or appends one).

    Returns False without writing anything when the skill directory itself
    (or any ancestor) doesn't exist yet — nothing to write through to; this
    agent is treated as not-installed-locally (D19), same as a genuinely
    remote one. Raises AgentEnvIsSymlink when any component of the parent
    directory, OR the leaf itself, is a symlink. Any OTHER OSError (EPERM,
    ENOSPC, EROFS, ...) propagates to the caller UNCAUGHT — this function
    does not decide how a genuine write failure should be reported; see
    mint()'s and add_agent()'s own handling (security-review finding F4).
    """
    skill_dir = os.path.dirname(path)
    leaf = os.path.basename(path)

    try:
        dir_fd = _resolve_symlink_free_dir_fd(skill_dir)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False

    try:
        # Refuse a symlinked LEAF explicitly (rather than letting the
        # rename below silently clobber it) — lstat here is relative to
        # the already-verified, already-open dir_fd, so this is not a
        # fresh check-then-use window: dir_fd cannot itself be swapped for
        # something else by another process (a process can only replace a
        # directory ENTRY, not the inode an already-open fd refers to).
        try:
            leaf_stat = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            leaf_stat = None
        if leaf_stat is not None and stat.S_ISLNK(leaf_stat.st_mode):
            raise AgentEnvIsSymlink(path)

        lines: list[str] = []
        if leaf_stat is not None:
            try:
                read_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise AgentEnvIsSymlink(path) from exc
                raise
            with os.fdopen(read_fd) as f:
                for line in f:
                    if line.startswith("AGENT_TOKEN="):
                        continue
                    lines.append(line.rstrip("\n"))
        lines.append(f"AGENT_TOKEN={token}")
        content = "\n".join(lines) + "\n"

        tmp_name = f".{leaf}.mint_tmp_{secrets.token_hex(8)}"
        tmp_fd = os.open(
            tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=dir_fd,
        )
        try:
            os.fchmod(tmp_fd, 0o600)  # belt and braces against a hostile umask
            with os.fdopen(tmp_fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            # Atomic replace, relative to the SAME verified dir_fd on both
            # sides — never re-resolves the leaf's path string.
            os.rename(tmp_name, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
            raise
        return True
    finally:
        os.close(dir_fd)


def mint(
    env_path: "str | None" = None, roster: "list[str] | None" = None,
) -> "tuple[dict, dict, list]":
    """Mint a fresh token for every agent in `roster` (default: resolved by
    _resolve_roster() -- AGENTS union whatever's already registered),
    write-through every agent with a REGISTERED install path whose skill
    directory exists, and print only names/digests/destination paths.
    Returns (tokens, digests, failures) so main() can serve --reveal from
    the SAME minted set without re-parsing anything, and report a partial
    failure without a stack trace (security-review finding F4 / I-A10 --
    see the per-agent failure handling below). `failures` is a list of
    (name, reason) pairs, empty when nothing went wrong.

    Install-path resolution (D19/roster fix, ruled -- see the
    LOCAL_SKILL_ENV_PATHS and AGENTS docstrings): reads the AGENT_INSTALLS
    registry from env_path. If no AGENT_INSTALLS line exists there at all
    (first bootstrap), seeds from LOCAL_SKILL_ENV_PATHS's guessed defaults
    -- that seeding is itself printed as this mint's AGENT_INSTALLS= line,
    turning a one-time guess into an explicit registration bootstrap_tokens.sh
    persists. Every mint after that reads ONLY the registry; an agent
    missing from it is REMOTE, full stop, never re-guessed from its name.

    Per agent, in `roster` order:
      - no registered path              -> REMOTE: token minted, digest
        registered, nothing written; --reveal is the only delivery path.
      - registered path, write succeeds -> written through (mode 600,
        atomically -- see _write_agent_token_file), digest registered,
        AGENT_INSTALLS entry carried forward.
      - registered path, write FAILS (directory missing -- D19; any
        component is a symlink -- the CRITICAL fix; or a genuine OSError,
        e.g. EPERM/ENOSPC) -> REFUSED, loudly, naming the reason. Nothing
        is written for this agent (_write_agent_token_file's atomicity
        guarantees that on ANY failure the existing file, if any, is
        untouched). The printed AGENT_TOKENS entry for this agent is then:
          * the agent's EXISTING registered digest, UNCHANGED, if this is a
            rotation of an already-registered agent -- security-review
            finding F4: the old behaviour either dropped the entry
            (revoking a still-working credential the agent's own file
            never lost) or, worse, registered a digest for a plaintext
            that was silently discarded (D19's original defect). Carrying
            the OLD entry forward means a partial failure never revokes a
            credential that still authenticates against the file it
            actually lives in.
          * OMITTED entirely if this is the agent's FIRST-EVER mint --
            nothing to carry forward, matches D19's original intent (never
            register a digest nobody holds the matching plaintext for).
    """
    if env_path is None:
        env_path = _DEFAULT_GATEWAY_ENV
    roster = _resolve_roster(env_path) if roster is None else roster
    installs, registry_present = _load_agent_installs_registry(env_path)
    if not registry_present:
        installs = dict(LOCAL_SKILL_ENV_PATHS)  # first-bootstrap seed, once
    existing_entries = _parse_agent_tokens_line(_read_env_raw_value(env_path, "AGENT_TOKENS") or "")

    tokens: dict[str, str] = {}
    digests: dict[str, str] = {}
    persisted_installs: dict[str, str] = {}
    failures: list[tuple[str, str]] = []
    lines: list[str] = []  # per-agent report lines, printed after the header blocks

    def _fail(name: str, reason: str) -> None:
        failures.append((name, reason))
        if name in existing_entries:
            lines.append(f"  {name:15}  REFUSED — {reason}")
            lines.append("                   existing registered token for this agent is UNCHANGED "
                          "(nothing was revoked)")
        else:
            lines.append(f"  {name:15}  REFUSED — {reason}")
            lines.append(f"                   install the {name} skill package first, then re-run:")
            lines.append(f"                   generate_tokens.py --add {name} --install-path {installs.get(name, '<path>')}")

    for a in roster:
        path = installs.get(a)

        if path is None:
            token = _mint_one()
            tokens[a] = token
            digests[a] = _digest(token)
            lines.append(f"  {a:15}  REMOTE / no local install found — reveal with:")
            lines.append(f"                   generate_tokens.py --reveal {a}")
            continue

        token = _mint_one()
        try:
            written = _write_agent_token_file(path, token)
        except AgentEnvIsSymlink as exc:
            _fail(a, f"{exc} is a symlink; not following it (same-uid agents are "
                     "treated as adversarial)")
            continue
        except OSError as exc:
            # A genuine write failure (EPERM, ENOSPC, EROFS, ...) --
            # security-review finding F4: this must NOT crash the whole
            # mint with a stack trace and leave every OTHER agent
            # unprocessed. _write_agent_token_file's atomicity means the
            # agent's existing file (if any) is untouched by this failure.
            _fail(a, f"write failed ({exc.__class__.__name__}: {exc})")
            continue

        if not written:
            # D19: a REGISTERED path whose directory (or an ancestor)
            # doesn't exist yet -- minting a token nobody can receive, then
            # registering its digest anyway, is exactly the fresh-host
            # defect this fix exists for.
            skill_dir = os.path.dirname(path)
            _fail(a, f"expected directory {skill_dir} does not exist")
            continue

        tokens[a] = token
        digests[a] = _digest(token)
        persisted_installs[a] = path
        lines.append(f"  {a:15}  written → {path}  (mode 600)")

    # Final AGENT_TOKENS entries: every successful mint's fresh digest, plus
    # -- for a FAILED agent that was already registered -- its existing
    # entry carried forward VERBATIM (never recomputed; I-A1's byte-identical
    # guarantee extends to this carry-forward path too).
    final_entries: dict[str, str] = {}
    for a in roster:
        if a in digests:
            final_entries[a] = f"{a}:sha256:{digests[a]}"
        elif a in existing_entries:
            final_entries[a] = existing_entries[a]

    print("=== Gateway .env — add this line (digest form; safe to print/paste) ===")
    print("AGENT_TOKENS=" + ",".join(final_entries.values()))
    print()
    # ⛔ MERGE, NEVER REBUILD. This line used to be generated from the roster
    # alone, so a bulk mint ERASED every operator-declared confinement it did
    # not know about — most damagingly `backup:admin`, the one credential
    # restricted to /admin/*, which absence from AGENT_ROLES widens to full
    # read/write. The additive path was fixed first; this is the same defect on
    # the other path, and it is the more destructive of the two because a bulk
    # mint rewrites the whole roster in one go.
    _existing_roles = _parse_agent_roles_line(
        _read_env_raw_value(env_path, "AGENT_ROLES") or "")
    _merged_roles = dict(_existing_roles)
    for _a in read_only_agents():
        _merged_roles.setdefault(_a, "read")
    _merged_roles = enforce_roster(_merged_roles)
    print("=== Gateway .env — merged roles (read-only roster + what you declared) ===")
    print("AGENT_ROLES=" + ",".join(f"{n}:{r}" for n, r in _merged_roles.items()))
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

    if failures:
        print()
        print("⚠ PARTIAL FAILURE — the following agent(s) were NOT updated this mint:")
        for name, reason in failures:
            carried = " (existing token preserved, nothing revoked)" if name in existing_entries else " (never registered -- nothing to carry forward)"
            print(f"  {name:15}  {reason}{carried}")
        print("  The AGENT_TOKENS line above is still SAFE to apply as printed -- it")
        print("  never drops a working credential, it only omits one that was never")
        print("  delivered. Fix the underlying issue for the affected agent(s) and")
        print("  re-run (bulk, or --add for just that one).")

    return tokens, digests, failures


def add_agent(
    name: str, install_path: "str | None" = None, env_path: "str | None" = None,
    role: "str | None" = None,
) -> "tuple[int, str | None]":
    """Additive mint (roster growth without rotation, item 2): mint exactly
    ONE new token for `name`, leaving every OTHER agent's digest in
    AGENT_TOKENS byte-identical (I-A1) -- this never re-derives or
    recomputes another agent's entry, it copies it verbatim off disk. Prints
    the MERGED AGENT_TOKENS= (and, with install_path, AGENT_INSTALLS=) line
    for bootstrap_tokens.sh to write into the gateway .env in place; this
    function itself never touches the gateway .env, exactly like mint() --
    the per-agent skill .env is the only file written directly.

    env_path defaults to _DEFAULT_GATEWAY_ENV resolved AT CALL TIME, never as
    a default argument value. A module constant bound into a signature is read
    once at import, so a caller (or a test) that rebinds the constant afterwards
    is silently ignored -- which is exactly how three tests came to assert an
    isolation they did not have, passing only because the tree they ran in
    happened to have no gateway .env at all.

    Returns (rc, token): token is the raw minted value (needed so main() can
    serve --reveal for the SAME invocation, same contract as mint()) or None
    when nothing was minted. rc is 0 on success, 1 on refusal -- every
    refusal path below returns BEFORE anything is minted, written, or
    registered, so a refused --add leaves no trace at all.

    Input validation (security-review findings F2/F2b, ruled I-A8) runs
    FIRST, before any registry is even read: a name or path containing a
    registry delimiter or line-injection character is refused outright --
    see _validate_registry_field()'s docstring for why this is validated
    at input rather than escaped on output.
    """
    if env_path is None:
        env_path = _DEFAULT_GATEWAY_ENV
    # Least privilege is decided BEFORE anything is minted, written or
    # registered, so a refusal here leaves no trace — same contract as every
    # other refusal in this function.
    try:
        # `declared` matters: an agent the operator already confined with
        # `name:read` must not be widened by a later mint, even though it is not
        # on the code roster — the roster cannot enumerate names this framework
        # has never heard of.
        _declared_now = _parse_agent_roles_line(
            _read_env_raw_value(env_path, "AGENT_ROLES") or "").get(name)
        effective_role = role_for(name, role, declared=_declared_now)
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1, None
    try:
        _validate_registry_field(name, "agent name")
        if install_path is not None:
            _validate_registry_field(install_path, "install path")
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1, None

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
        # I-A2/I-A9: two agents MAY legitimately share one install path (one
        # tool reading another's skill directory) -- but
        # _write_agent_token_file() REPLACES any existing AGENT_TOKEN= line
        # at that path wholesale, so writing THIS agent's token there would
        # clobber whichever registered agent already has a live token at
        # the SAME file. Compared by NORMALIZED identity (_same_registered_
        # file, resolving '..' and any symlink), not literal string
        # equality -- security-review finding F3: two spellings of the
        # identical file (".../shared-memory/.env" vs
        # ".../other/../shared-memory/.env") defeated a `==` comparison,
        # letting a second mint silently overwrite the first agent's token
        # and start authenticating AS the second agent (the gateway stamps
        # `source` from token identity -- silent provenance corruption).
        clobbered = [
            n for n, p in installs.items()
            if n in existing_entries and _same_registered_file(p, install_path)
        ]
        if clobbered:
            print(
                f"✗ install path {install_path} is already registered to "
                f"{', '.join(sorted(clobbered))} with a live token — writing "
                f"{name}'s token there would overwrite it. Use a distinct "
                "path, or rotate both deliberately.",
                file=sys.stderr,
            )
            return 1, None

    token = _mint_one()
    if install_path is not None:
        try:
            written = _write_agent_token_file(install_path, token)
        except AgentEnvIsSymlink as exc:
            print(
                f"✗ REFUSED — {exc} is a symlink; not following it "
                "(same-uid agents are treated as adversarial). Replace it "
                "with a real file and re-run.",
                file=sys.stderr,
            )
            return 1, None
        except OSError as exc:
            # Security-review finding F4: a genuine write failure (EPERM,
            # ENOSPC, ...) must report cleanly, not crash with a stack
            # trace -- _write_agent_token_file's atomicity means nothing on
            # disk changed, so this is a clean refusal, not a
            # partially-applied one.
            print(
                f"✗ REFUSED — write failed ({exc.__class__.__name__}: {exc}). "
                "Nothing was written or registered.",
                file=sys.stderr,
            )
            return 1, None
        if not written:
            skill_dir = os.path.dirname(install_path)
            print(
                f"✗ REFUSED — expected directory {skill_dir} does not exist. "
                f"Install the {name} skill package first, then re-run:\n"
                f"  generate_tokens.py --add {name} --install-path {install_path}",
                file=sys.stderr,
            )
            return 1, None

    digest = _digest(token)
    merged_entries = dict(existing_entries)
    merged_entries[name] = f"{name}:sha256:{digest}"

    print("=== Gateway .env — merged AGENT_TOKENS= line (write this in place) ===")
    print("AGENT_TOKENS=" + ",".join(merged_entries.values()))

    if effective_role is not None:
        # MERGED, never replaced: dropping an existing backup:admin entry here
        # would silently widen the one credential confined to /admin/*.
        merged_roles = _parse_agent_roles_line(
            _read_env_raw_value(env_path, "AGENT_ROLES") or "")
        merged_roles[name] = effective_role
        # Repair drift while we are rewriting the line anyway: a roster identity
        # registered before this rule existed is still declared wrong in the
        # file. The gateway confines it regardless, but the .env should not lie.
        merged_roles = enforce_roster(merged_roles)
        print()
        print("=== Gateway .env — merged AGENT_ROLES= line (write this in place) ===")
        print("AGENT_ROLES=" + ",".join(f"{n}:{r}" for n, r in merged_roles.items()))
        if effective_role == "read":
            print(f"# {name} is a READ-ONLY identity: GET /health, GET /memory/telemetry")
            print("# and read-only Cypher on POST /memory/graph. Every other route → 403.")

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
        "--role", metavar="ROLE", choices=list(VALID_ROLES),
        help="Role for the agent being added with --add: read | full | admin. "
             "Roles only ever NARROW access. Omit it and the role is derived: "
             "a name in READ_ONLY_AGENTS always gets 'read', anything else "
             "gets full access (no AGENT_ROLES entry). ⛔ A read-only identity "
             "cannot be widened here — --role full on one is REFUSED before "
             "anything is minted.",
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
        rc, token = add_agent(args.add, install_path=args.install_path,
                              role=args.role)
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

    tokens, _digests, failures = mint(env_path=_DEFAULT_GATEWAY_ENV, roster=roster)

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

    # Security-review finding F4 / I-A10: a partial per-agent failure does
    # NOT abort this exit code as 0 -- the printed AGENT_TOKENS line is
    # still SAFE to write into the gateway .env as-is (a failed agent's
    # existing entry is carried forward unchanged, never dropped; see
    # mint()'s docstring). Returning nonzero here would make bootstrap_
    # tokens.sh's `out="$(... )"` capture (running under `set -e`) abort
    # BEFORE it ever echoes this output or applies the safe merged line --
    # exactly backwards from what an operator needs to see. Instead,
    # bootstrap_tokens.sh itself greps this stdout for the "PARTIAL
    # FAILURE" marker AFTER applying the (safe) merged registry, and exits
    # nonzero itself at that point -- so automation still gets a
    # distinguishable exit code, without suppressing the report.
    return 0


if __name__ == "__main__":
    sys.exit(main())
