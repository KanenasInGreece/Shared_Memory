#!/usr/bin/env python3
"""
Generate AGENT_TOKENS for the gateway and per-agent AGENT_TOKEN values.

MINT FLOW (RULED, Credential_Custody_Plan_2026-08-14, PR A2): no secret
token value is ever printed to stdout. An agent-driven install captures
stdout into a durable transcript — "shown once" silently becomes "stored
forever" — so this script writes tokens straight into the files that need
them and prints only names, digests, and destination paths.

Operators run it through bootstrap_tokens.sh, which writes the registry lines it
prints into the gateway .env. Run directly, it only prints them, and the gateway
rejects a new token until they are copied in. The recovery commands it prints
therefore name bootstrap_tokens.sh.

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
       Studio takes AGENT_TOKEN from mcp.json's own env block) — nothing is
       written or printed; use --reveal to see that one token, on the SAME
       invocation. Without --reveal that agent is named, LOUDLY, as
       REGISTERED BUT UNDELIVERABLE, with the exact recovery command
       (--remint <name> --reveal <name>, which rotates nobody else).
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

  uv run python shared-memory/scripts/generate_tokens.py --add opencode --mcp \
      --install-path ~/.config/opencode/shared-memory-mcp/.env
    The same additive mint, registering an MCP CONNECTOR install instead of a
    CLI skill install (AGENT_INSTALLS kind `mcp`, written as
    `name:mcp:path`). The registered path is still an .env FILE — the walled
    connector directory's own. What the kind buys is delivery: sync_skills.sh
    ships the CONNECTOR package there (vector-skill.py,
    CONSTITUTION_SNIPPET_MCP.md, system-prompt.md) and never the CLI skill
    package. An entry with no kind (`name:path`) is a CLI skill install,
    permanently — nothing rewrites an existing line.

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
import fcntl
import hashlib
import os
import secrets
import stat
import sys
import tempfile


# First-mint roster only. A later bulk mint also keeps every name already in AGENT_TOKENS, so dropping a name here never revokes a live credential.
# monitor is absent: a fresh install cannot deliver it, and a digest nobody received is worse than not minting.
AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity"]

# The read-only roster lives in agent_roles.py, and the gateway enforces that same list. A mint-only list let an old AGENT_ROLES line keep full access.
from agent_roles import (                                    # noqa: E402
    READ_ONLY_AGENTS, VALID_ROLES, read_only_agents,
    role_for_mint, enforce_roster,
)

# Kept as a module-local alias: this name is the one the CLI and the tests call.
role_for = role_for_mint

# First bootstrap only, and only for CLI skill installs this script can see. lm_studio has no skill .env, and antigravity is not guessed onto gemini's path.
# The one escape hatch from the --reveal TTY guard, for a deliberate scripted reveal on a host with no terminal; named rather than silent so it shows up in a shell history and in review.
_REVEAL_TTY_OVERRIDE = "SHARED_MEMORY_ALLOW_REVEAL_WITHOUT_TTY"


def _normalise_role(role: "str | None") -> "str | None":
    """The registry role for what the caller typed.

    GET /health renders the "full" role as "write" (_health_role_for), so a role read
    off a live /health and passed straight back used to be refused for using the
    framework's own word. Accepted as an alias here rather than renaming the
    rendering, which the monitor reads. "write" is NOT a registry role, so without
    this mapping agent_roles rejects it before anything is minted.
    """
    return "full" if role == "write" else role


def _looks_like_a_digest(value: str) -> bool:
    """True for exactly the shape of a sha256 hex digest, which is never a token.

    Deliberately narrow, 64 hex characters and nothing else, so a long genuine token
    is unaffected: secrets.token_urlsafe gives 43 characters including non-hex ones,
    so the two shapes do not overlap.
    """
    v = value.strip()
    return len(v) == 64 and all(c in "0123456789abcdefABCDEF" for c in v)


def _reveal_output_is_a_terminal(fd: "int | None" = None) -> bool:
    """True if a human is plausibly watching the reveal's destination, or the operator overrode it.

    The destination is stdout, or `fd` when --reveal-fd names one. bootstrap_tokens.sh
    captures stdout to parse the registry lines, so it passes its own terminal as a
    separate fd and the token never enters the wrapper's memory.

    isatty is the only available signal separating a terminal from a pipe, a file or
    an agent harness. It is not a security boundary, since a caller can allocate a
    pty, and is not meant to be one: it stops the accident the published install path
    used to cause.
    """
    if os.environ.get(_REVEAL_TTY_OVERRIDE, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        return os.isatty(fd) if fd is not None else sys.stdout.isatty()
    except Exception:
        # A stdout that cannot answer is not a terminal.
        return False


def _reveal_stream(fd: "int | None"):
    """Where the --reveal block goes: stdout, or the descriptor --reveal-fd names, left open for its owner."""
    return sys.stdout if fd is None else os.fdopen(fd, "w", closefd=False)


# That one seed is recorded in AGENT_INSTALLS. Later mints read only the registry; a name with no path is remote, and --reveal is the only delivery.
LOCAL_SKILL_ENV_PATHS = {
    "claude": os.path.expanduser("~/.claude/skills/shared-memory/.env"),
    "codex":  os.path.expanduser("~/.codex/skills/shared-memory/.env"),
    "gemini": os.path.expanduser("~/.gemini/skills/shared-memory/.env"),
    "grok":   os.path.expanduser("~/.grok/skills/shared-memory/.env"),
}

# Same order as the other loaders: shared-memory/.env, then the repo-root .env.
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


# Comma, colon, newline, and NUL are refused, not escaped. These lines are comma-separated name:value pairs and also a compose env file, and an escape would need every reader to agree.
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


# Two fields means skill, forever. The default kind is emitted without ":skill:" so a bulk reprint is not a schema change in the operator's diff.
# skill delivers the CLI package; mcp delivers the connector and never the CLI package. A colon cannot appear in a path, so the middle field is the kind.
INSTALL_KINDS = ("skill", "mcp")
DEFAULT_INSTALL_KIND = "skill"


def _split_install_entry(rest: str) -> "tuple[str, str]":
    """Split the part of an AGENT_INSTALLS entry AFTER the agent name into
    (kind, path). Two arities, one rule: if what precedes the next colon is a
    KNOWN kind and something follows it, that is the kind; otherwise the whole
    remainder is the path and the kind is the default.

    Written as "known kind" rather than "has three fields" deliberately. A
    legacy path containing a colon of its own (registrable before
    _validate_registry_field forbade the character) would otherwise have its
    first path segment eaten as a bogus kind, and the install would silently
    become a truncated prefix — the exact failure the FIRST-colon-only rule
    existed to prevent."""
    head, sep, tail = rest.partition(":")
    if sep and head.strip() in INSTALL_KINDS and tail.strip():
        return head.strip(), tail.strip()
    return DEFAULT_INSTALL_KIND, rest.strip()


def _parse_agent_installs(raw: str) -> "dict[str, tuple[str, str]]":
    """Parse an AGENT_INSTALLS= value: comma-separated `name:path` or
    `name:kind:path` entries, into {name: (kind, path)}.

    The agent NAME is split on the FIRST colon only (str.partition) — mirrors
    _load_agent_tokens()'s own "everything after the first colon is the value"
    rule in coordinator.py for the equivalent case in AGENT_TOKENS. What that
    leaves is handed to _split_install_entry(), which decides whether the next
    field is an install kind or the start of the path."""
    installs: dict[str, tuple[str, str]] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, rest = pair.partition(":")
        name, rest = name.strip(), rest.strip()
        if not sep or not name or not rest:
            continue
        kind, path = _split_install_entry(rest)
        if not path:
            continue
        installs[name] = (kind, path)
    return installs


def _format_agent_installs(installs: "dict[str, tuple[str, str]]") -> str:
    """Render {name: (kind, path)} back into an AGENT_INSTALLS= value.

    ⛔ The default kind is emitted in the TWO-field form. Round-tripping a
    registry of ordinary skill installs must be byte-identical — see the
    INSTALL_KINDS comment: a mint is not a migration."""
    parts = []
    for name, (kind, path) in installs.items():
        if kind == DEFAULT_INSTALL_KIND:
            parts.append(f"{name}:{path}")
        else:
            parts.append(f"{name}:{kind}:{path}")
    return ",".join(parts)


def _load_agent_installs_registry(env_path: str) -> "tuple[dict[str, tuple[str, str]], bool]":
    """Read the AGENT_INSTALLS registry from the gateway .env.

    Returns (installs, registry_present). registry_present is True the
    instant a LIVE AGENT_INSTALLS= line exists at all — even one that parses
    to zero entries — because that is the signal that first bootstrap
    already happened and the registry, however sparse, is now authoritative.
    False (no line, or no file) is the ONLY state in which
    LOCAL_SKILL_ENV_PATHS's guessed paths are allowed to seed anything —
    every mint after that reads the registry and nothing else (see the
    module-level LOCAL_SKILL_ENV_PATHS docstring).

    Entries are {name: (kind, path)} — see INSTALL_KINDS."""
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
    if not leaf:
        # An empty leaf must fail here, before os.rename onto an empty name, because this writer is shared by every mint path.
        raise ValueError(
            f"install path {path!r} names a directory, not a file — "
            "refused before any write"
        )

    try:
        dir_fd = _resolve_symlink_free_dir_fd(skill_dir)
    except FileNotFoundError:
        return False
    except NotADirectoryError:
        return False

    try:
        # Refuse a symlinked leaf. lstat is on the already-open dir_fd, so this is not a fresh check-then-use window.
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
    revealing: "list[str] | None" = None,   # ⛔ see the note below: no TTY guard here
) -> "tuple[dict, dict, list]":
    """Mint a fresh token for every agent in `roster` (default: resolved by
    _resolve_roster() -- AGENTS union whatever's already registered),
    write-through every agent with a REGISTERED install path whose skill
    directory exists, and print only names/digests/destination paths.
    ⛔ NO TTY GUARD HERE. main() refuses --reveal when stdout is not a terminal;
    this function does not, so a Python caller that passes `revealing` prints raw
    tokens wherever its stdout goes. That is deliberate — mint() cannot know
    whether its caller is a human, and the three in-process tests that exercise
    reveal depend on calling it — but any NEW caller passing `revealing` owns that
    decision and should be checked.

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
        WITHOUT --reveal this is reported as UNDELIVERABLE -- loudly, per
        agent AND again in a closing block -- naming the recovery command
        (--remint <name> --reveal <name>, which rotates nobody else).
        This is why "monitor" was taken off the default AGENTS roster: on
        a fresh install the documented bulk invocation carries no --reveal,
        so every install registered a monitor digest nobody ever received.
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
        # Seeded once, and only as a CLI skill path. A naming convention cannot produce an MCP directory.
        installs = {n: (DEFAULT_INSTALL_KIND, p)
                    for n, p in LOCAL_SKILL_ENV_PATHS.items()}
    existing_entries = _parse_agent_tokens_line(_read_env_raw_value(env_path, "AGENT_TOKENS") or "")

    tokens: dict[str, str] = {}
    digests: dict[str, str] = {}
    persisted_installs: dict[str, tuple[str, str]] = {}
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
            _kind, _path = installs.get(name, (DEFAULT_INSTALL_KIND, "<path>"))
            _kind_flag = " --mcp" if _kind == "mcp" else ""
            lines.append(f"                   install the {name} skill package first, then re-run:")
            lines.append(f"                   bash shared-memory/scripts/bootstrap_tokens.sh --add {name}{_kind_flag} --install-path {_path}")

    for a in roster:
        entry = installs.get(a)
        path = entry[1] if entry else None

        if path is None:
            token = _mint_one()
            tokens[a] = token
            digests[a] = _digest(token)
            # Do not print --reveal as a later retrieval step. Run afterwards it rotates the whole fleet; it only shows a token from this same invocation.
            lines.append(f"  {a:15}  REMOTE — token minted and REGISTERED, but NOT DELIVERED")
            if a in (revealing or []):
                lines.append("                   (revealed on this run — capture it now)")
            else:
                # Any remote agent without --reveal, not one special name. The digest is registered and the plaintext is already gone.
                lines.append("                   ⛔ UNDELIVERABLE — this token was written NOWHERE and")
                lines.append("                      can never be retrieved: its digest is registered,")
                lines.append("                      its plaintext is already gone. Recovery (re-mints")
                lines.append("                      ONLY this agent, rotates nobody — run it YOURSELF,")
                lines.append("                      never through an agent):")
                lines.append(f"                        bash shared-memory/scripts/bootstrap_tokens.sh --remint {a} --reveal {a}")
            continue

        token = _mint_one()
        try:
            written = _write_agent_token_file(path, token)
        except AgentEnvIsSymlink as exc:
            _fail(a, f"{exc} is a symlink; not following it (same-uid agents are "
                     "treated as adversarial)")
            continue
        except OSError as exc:
            # A write failure must not abort the rest of the mint. The atomic writer left this agent's file untouched.
            _fail(a, f"write failed ({exc.__class__.__name__}: {exc})")
            continue

        if not written:
            # A registered path whose directory does not exist yet would mint a token nobody can receive.
            skill_dir = os.path.dirname(path)
            _fail(a, f"expected directory {skill_dir} does not exist "
                     f"-- create it and re-mint this one agent: "
                     f"mkdir -p {skill_dir} && bash shared-memory/scripts/bootstrap_tokens.sh --remint {a}")
            continue

        tokens[a] = token
        digests[a] = _digest(token)
        persisted_installs[a] = entry
        _kind_note = "" if entry[0] == DEFAULT_INSTALL_KIND else f"  [{entry[0]}]"
        lines.append(f"  {a:15}  written → {path}  (mode 600){_kind_note}")

    # Fresh digests, plus a failed agent's existing entry carried forward verbatim. A failure must not drop a working credential.
    final_entries: dict[str, str] = {}
    for a in roster:
        if a in digests:
            final_entries[a] = f"{a}:sha256:{digests[a]}"
        elif a in existing_entries:
            final_entries[a] = existing_entries[a]

    # Blank, KEY=VALUE, or "# " so the block pastes into an env file. The "# " prefix does not touch bootstrap's ^AGENT_ lines.
    print("# === Gateway .env — add this line (digest form; safe to print/paste) ===")
    print("AGENT_TOKENS=" + ",".join(final_entries.values()))
    print()
    # Merge the roles line; do not rebuild it from the roster. A rebuild erased operator confinements such as backup:admin and widened them to full access.
    _existing_roles = _parse_agent_roles_line(
        _read_env_raw_value(env_path, "AGENT_ROLES") or "")
    _merged_roles = dict(_existing_roles)
    for _a in read_only_agents():
        _merged_roles.setdefault(_a, "read")
    _merged_roles = enforce_roster(_merged_roles)
    print("# === Gateway .env — merged roles (read-only roster + what you declared) ===")
    print("AGENT_ROLES=" + ",".join(f"{n}:{r}" for n, r in _merged_roles.items()))
    print("# read-role agents may reach GET /memory/telemetry, POST /memory/search,")
    print("# and GET /memory/status/{pg_id}; POST /memory/graph is 403.")
    print("# GET /health is anonymous (not a read-role grant). All other routes → 403.")
    print()
    print("# === Gateway .env — install-path registry (sync exactly what's registered) ===")
    print("AGENT_INSTALLS=" + _format_agent_installs(persisted_installs))
    print()

    print("# === Per-agent tokens — written through, never printed ===")
    for line in lines:
        # One prefix covers every report shape above.
        print("# " + line)
    print()
    print("# Each agent must use its own distinct token — never share tokens across agents.")
    # An undelivered credential must show up as its own block. Absence here is what "every agent can authenticate" looks like.
    _undelivered = [a for a in roster
                    if installs.get(a) is None and a not in (revealing or [])]
    if _undelivered:
        print()
        print("# ⛔ REGISTERED BUT UNDELIVERABLE — these agents have a digest in")
        print("#    AGENT_TOKENS and NO WAY TO OBTAIN THEIR TOKEN:")
        for a in _undelivered:
            print(f"#      {a}")
        print()
        print("#    They will authenticate against nothing. The .env will show them")
        print("#    as provisioned, which is the misleading part. Fix now with")
        print("#    (operator-run — NEVER through an agent, a transcript stores it forever):")
        for a in _undelivered:
            print(f"#      bash shared-memory/scripts/bootstrap_tokens.sh --remint {a} --reveal {a}")
        print("#    (--remint re-mints ONE agent; it never touches anyone else.)")
        print()


    if failures:
        print()
        # bootstrap_tokens.sh greps the literal "PARTIAL FAILURE". The "# " prefix keeps that marker; do not reword the string below.
        print("# ⚠ PARTIAL FAILURE — the following agent(s) were NOT updated this mint:")
        for name, reason in failures:
            carried = " (existing token preserved, nothing revoked)" if name in existing_entries else " (never registered -- nothing to carry forward)"
            print(f"#   {name:15}  {reason}{carried}")
        print("#   The AGENT_TOKENS line above is still SAFE to apply as printed -- it")
        print("#   never drops a working credential, it only omits one that was never")
        print("#   delivered. Fix the underlying issue for the affected agent(s) and")
        print("#   re-run (bulk, or --add for just that one).")

    return tokens, digests, failures


def add_agent(
    name: str, install_path: "str | None" = None, env_path: "str | None" = None,
    role: "str | None" = None, replace: bool = False,
    install_kind: str = DEFAULT_INSTALL_KIND,
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
    # An unknown kind would parse back as part of a path. Refuse before anything is minted.
    if install_kind not in INSTALL_KINDS:
        print(f"✗ unknown install kind {install_kind!r} — expected one of "
              f"{', '.join(INSTALL_KINDS)}", file=sys.stderr)
        return 1, None
    # A non-default kind is a delivery target. Without a path the agent is remote and the kind would be dropped.
    if install_kind != DEFAULT_INSTALL_KIND and install_path is None:
        print(f"✗ --{install_kind} needs --install-path — an install kind says what to "
              f"deliver WHERE, and without a registered path there is nowhere. For a "
              f"remote MCP host with no local directory, register it with no kind and "
              f"deliver its token with --reveal (operator-run).", file=sys.stderr)
        return 1, None
    # Decided before anything is minted, so a refusal leaves no trace.
    try:
        # An agent already confined as name:read must not be widened, even if the code roster has never heard of the name.
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
            # --install-path is the .env file, not its directory. An empty basename is the trailing-slash signal; stripping the slash first hides it.
            if not os.path.basename(install_path) or os.path.isdir(install_path):
                raise ValueError(
                    "--install-path must be the .env FILE, not a directory "
                    "(e.g. …/shared-memory-mcp/.env)."
                )
    except ValueError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1, None

    existing_raw = _read_env_raw_value(env_path, "AGENT_TOKENS") or ""
    existing_entries = _parse_agent_tokens_line(existing_raw)
    # --add refuses an existing name so a live token is not rotated by accident. --remint is the deliberate re-issue, and it touches nobody else.
    if replace and name not in existing_entries:
        print(
            f"✗ {name!r} is not registered — --remint re-issues an EXISTING "
            "agent's token. Use --add for a new agent.",
            file=sys.stderr,
        )
        return 1, None
    if not replace and name in existing_entries:
        # Do not steer an agent at --reveal. A revealed token lands in a transcript. The path an agent may take is write-through; --reveal is the operator's own terminal.
        _kind_flag = "" if install_kind == DEFAULT_INSTALL_KIND else f" --{install_kind}"
        _path_hint = install_path or "<install-dir>/.env"
        print(
            f"✗ {name!r} is already registered in AGENT_TOKENS — --add never "
            "silently rotates an existing agent's token.\n"
            f"  To re-issue THIS agent only (every other digest untouched), writing\n"
            f"  the new token straight into its own .env — never printing it:\n"
            f"      bash shared-memory/scripts/bootstrap_tokens.sh --remint {name}{_kind_flag} --install-path {_path_hint}\n"
            f"  If {name} has NO local directory to write into, an OPERATOR (never an\n"
            f"  agent — a transcript stores a revealed token forever) can run instead:\n"
            f"      bash shared-memory/scripts/bootstrap_tokens.sh --remint {name} --reveal {name}\n"
            "  To rotate the whole fleet deliberately: bootstrap_tokens.sh --force.",
            file=sys.stderr,
        )
        return 1, None

    installs, _present = _load_agent_installs_registry(env_path)

    if install_path is not None:
        # Compared by normalized path, not string equality: two spellings of one file used to overwrite the first agent's token.
        # On a re-issue, this agent's own path is not a clobber. Writing a different agent's file would authenticate as that agent.
        clobbered = [
            n for n, (_k, p) in installs.items()
            if n in existing_entries and _same_registered_file(p, install_path)
            and not (replace and n == name)
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
            # A write failure changed nothing on disk, so report it as a refusal rather than a traceback.
            print(
                f"✗ REFUSED — write failed ({exc.__class__.__name__}: {exc}). "
                "Nothing was written or registered.",
                file=sys.stderr,
            )
            return 1, None
        if not written:
            skill_dir = os.path.dirname(install_path)
            _kind_flag = "" if install_kind == DEFAULT_INSTALL_KIND else f" --{install_kind}"
            print(
                f"✗ REFUSED — expected directory {skill_dir} does not exist. "
                f"Create it first: mkdir -p {skill_dir}. "
                f"Install the {name} skill package first, then re-run:\n"
                f"  bash shared-memory/scripts/bootstrap_tokens.sh --add {name}{_kind_flag} --install-path {install_path}",
                file=sys.stderr,
            )
            return 1, None

    digest = _digest(token)
    merged_entries = dict(existing_entries)
    merged_entries[name] = f"{name}:sha256:{digest}"

    # Blank, KEY=VALUE, or "# " so the block pastes. bootstrap's ^AGENT_ selectors are unchanged.
    print("# === Gateway .env — merged AGENT_TOKENS= line (write this in place) ===")
    print("AGENT_TOKENS=" + ",".join(merged_entries.values()))

    if effective_role is not None:
        # MERGED, never replaced: dropping an existing backup:admin entry here
        # would silently widen the one credential confined to /admin/*.
        merged_roles = _parse_agent_roles_line(
            _read_env_raw_value(env_path, "AGENT_ROLES") or "")
        merged_roles[name] = effective_role
        # A roster identity registered before this rule is still wrong in the file. The gateway confines it anyway; the .env should not lie.
        merged_roles = enforce_roster(merged_roles)
        print()
        print("# === Gateway .env — merged AGENT_ROLES= line (write this in place) ===")
        print("AGENT_ROLES=" + ",".join(f"{n}:{r}" for n, r in merged_roles.items()))
        if effective_role == "read":
            print(f"# {name} is a READ-ONLY identity: GET /memory/telemetry, POST /memory/search,")
            print("# and GET /memory/status/{pg_id}; POST /memory/graph is 403.")
            print("# GET /health is anonymous (not a read-role grant). Every other route → 403.")

    if install_path is not None:
        merged_installs = dict(installs)
        merged_installs[name] = (install_kind, install_path)
        print()
        print("# === Gateway .env — merged AGENT_INSTALLS= line (write this in place) ===")
        print("AGENT_INSTALLS=" + _format_agent_installs(merged_installs))
        print()
        _kind_note = "" if install_kind == DEFAULT_INSTALL_KIND else f"  [{install_kind}]"
        print(f"#   {name:15}  written → {install_path}  (mode 600){_kind_note}")
        if install_kind == "mcp":
            print("#   Registered as an MCP install: sync_skills.sh delivers the CONNECTOR")
            print("#   package here (vector-skill.py, CONSTITUTION_SNIPPET_MCP.md,")
            print("#   system-prompt.md) and never the CLI skill package.")
        # The token is read once, at import. A re-mint 401s the running process until it re-reads the file. Toggling the MCP server was enough.
        print()
        if install_kind == "mcp":
            # A fresh install has nothing running yet, so an unconditional "respawn" overclaimed.
            print("# ⚠ If this agent's memory MCP server is already running, respawn it so")
            print("#   it re-reads the rotated token — a full host restart works, or a")
            print("#   per-server reload/disable-enable if your host offers one; until then")
            print("#   it keeps the old token.")
        else:
            print("# ⚠ If this agent's process is already running, restart it so it")
            print("#   re-reads the token — it keeps presenting the previous one until")
            print("#   then, and every request (reads included) will fail auth.")
    else:
        print()
        print(f"#   {name:15}  REMOTE / no install path given.")
        print("#                    Prefer the write-through form when this agent HAS a")
        print("#                    local directory — it never prints the token:")
        print(f"#                      bash shared-memory/scripts/bootstrap_tokens.sh --remint {name} --install-path <dir>/.env")
        print("#                    Otherwise an OPERATOR must reveal it, in their OWN")
        print("#                    terminal — never through an agent, whose transcript")
        print("#                    turns \"shown once\" into \"stored forever\":")
        # Do not say --add --reveal. This name is already registered, so another --add refuses. --remint re-issues an existing name.
        print(f"#                    bash shared-memory/scripts/bootstrap_tokens.sh --remint {name} --reveal {name}")

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
        "--reveal-fd", type=int, default=None, metavar="FD",
        help="Write the --reveal block to this already-open file descriptor instead "
             "of stdout. It must be a terminal, as stdout would have to be. "
             "bootstrap_tokens.sh passes its own terminal this way, so the token "
             "never enters the output it captures.",
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
        "--remint", metavar="NAME",
        help="Re-issue the token for ONE agent that is ALREADY registered, "
             "leaving every other agent's digest byte-identical. This is the "
             "recovery path for a token that was registered but never "
             "delivered (a remote agent minted without --reveal). It DOES "
             "invalidate that agent's current token, so pair it with "
             "--reveal NAME or --install-path so the agent receives the new "
             "one. Use --add for an agent that is not registered yet.",
    )
    ap.add_argument(
        "--role", metavar="ROLE", choices=list(VALID_ROLES) + ["write"],
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
        help="With --add/--remint: this agent's .env FILE path — a CLI agent's "
             "skill .env (e.g. ~/.codex/skills/shared-memory/.env), or with "
             "--mcp an MCP connector's walled-directory .env (e.g. "
             "~/.config/opencode/shared-memory-mcp/.env). Recorded in the "
             "AGENT_INSTALLS registry. It is a FILE, never a directory: the "
             "mint splits it into dirname/basename and writes the leaf. "
             "Ignored without --add/--remint.",
    )
    ap.add_argument(
        "--force-hex", action="store_true",
        help="Register a 64-hex value as a token anyway. That shape is normally "
             "refused because it is the shape of a sha256 digest, and a digest "
             "pasted from the gateway's AGENT_TOKENS registers a credential nobody "
             "can present. Use this only for a token you generated yourself as hex.",
    )
    ap.add_argument(
        "--mcp", action="store_true",
        help="Register this agent's install as an MCP CONNECTOR install "
             "(AGENT_INSTALLS kind 'mcp') rather than a CLI skill install. "
             "sync_skills.sh then delivers the connector package "
             "(vector-skill.py, CONSTITUTION_SNIPPET_MCP.md, system-prompt.md) "
             "into that directory and NEVER the CLI skill package. Requires "
             "--install-path. Without this flag an entry is a CLI skill "
             "install, which is also what every two-field legacy entry means.",
    )
    args = ap.parse_args(argv)
    install_kind = "mcp" if args.mcp else DEFAULT_INSTALL_KIND

    args.role = _normalise_role(args.role)

    # ⛔ A REVEALED TOKEN IS ONLY EVER FOR A HUMAN'S OWN TERMINAL, so if stdout is
    # not a TTY nobody is watching and the reveal does not happen. README, OPERATE.md
    # Phase 6 and this script's own banner all said so in prose and it did not hold:
    # fact:1499 (following the published Quick Start put a live bearer token into an
    # agent transcript) and fact:1543 (the same token sat in a world-readable log for
    # 16+ hours, which provoked an agent into a filesystem-wide credential hunt).
    # A rotation fixes a leaked token; nothing un-writes a transcript. Checked once
    # here so --add, --remint and a bulk --force --reveal are all covered.
    # Checked before the mint, because the override skips the TTY check and a closed or read-only descriptor would only fail after the token exists.
    if args.reveal and args.reveal_fd is not None:
        try:
            writable = (fcntl.fcntl(args.reveal_fd, fcntl.F_GETFL) & os.O_ACCMODE) in (os.O_WRONLY, os.O_RDWR)
        except OSError:
            writable = False
        if not writable:
            print(f"\u2717 --reveal-fd {args.reveal_fd} is not an open, writable file descriptor. "
                  "Nothing was minted, revealed or written.", file=sys.stderr)
            return 1

    if args.reveal and not _reveal_output_is_a_terminal(args.reveal_fd):
        print(
            "\u2717 REFUSED: --reveal prints a live bearer token and stdout is not a "
            "terminal, so this is a pipe, a redirect, a log, or an agent's "
            "captured output -- exactly where a token must never land. Nothing "
            "was minted, revealed or written.\n"
            "  Run it yourself, in your own terminal, interactively.\n"
            "  If a token has ALREADY been revealed into a transcript or a log, "
            "treat it as disclosed: re-mint that identity and delete the log.\n"
            "  There is a documented override for a deliberate scripted reveal on a "
            "host with no terminal. It is named in shared-memory/.env.example, "
            "deliberately NOT here: a refusal that prints its own bypass is a refusal "
            "an agent satisfies by setting the bypass (fact:2055 A-1, the same reason "
            "gitguard's refusal never names its marker).",
            file=sys.stderr)
        return 1

    if args.mcp and args.add is None and not args.remint:
        # A kind belongs to one registration. --mcp on a bulk mint would read as making every entry MCP.
        print("✗ --mcp only makes sense together with --add or --remint: it "
              "declares what ONE registered install is, and a bulk mint carries "
              "each entry's kind forward from the registry already.",
              file=sys.stderr)
        return 1

    if args.digest is not None:
        raw_token = sys.stdin.read().strip()
        if not raw_token:
            print("✗ no token read from stdin", file=sys.stderr)
            return 1
        if len(raw_token) < 20:
            print("✗ token is too short (entropy floor: 20 characters minimum)", file=sys.stderr)
            return 1
        if _looks_like_a_digest(raw_token) and not args.force_hex:
            # fact:1543: a monitor install was given the sha256 DIGEST from the gateway's
            # AGENT_TOKENS line instead of the plaintext token, nothing validated the shape,
            # and it failed only later as an opaque 401 — diagnosing which sent an agent
            # hunting credentials across the filesystem. Name the mistake here instead.
            print("✗ that value is 64 hexadecimal characters, which is the shape of a "
                  "sha256 DIGEST, not of a token. The AGENT_TOKENS line in the gateway "
                  "shared-memory/.env holds DIGESTS; a client needs the PLAINTEXT token, "
                  "which is only ever written into that agent's own .env at mint time or "
                  "shown by an operator-run --reveal. Digesting a digest would register a "
                  "credential nobody can present.\n"
                  "  If this really is a token you generated yourself as 64 hex "
                  "characters (openssl rand -hex 32), re-run with --force-hex.",
                  file=sys.stderr)
            return 1
        print(f"{args.digest}:sha256:{_digest(raw_token)}")
        return 0

    if args.convert_digests is not None:
        return convert_digests(args.convert_digests)

    # --add and --remint share everything after the mint. Duplicating it is how the two paths drift.
    if args.add is not None or args.remint:
        if args.add is not None and args.remint:
            print("✗ --add and --remint are mutually exclusive: one registers a "
                  "NEW agent, the other re-issues an existing one.", file=sys.stderr)
            return 1
        # These paths mint one name, so --reveal can only name that one. Checking only --add would make --remint NAME --reveal NAME refuse itself.
        # Checked before the mint: afterwards the new token is already in the agent's .env, and refusing then leaves it unregistered.
        _minted_name = args.add or args.remint
        unknown = [n for n in args.reveal if n != _minted_name]
        if unknown:
            print(
                f"✗ this invocation only mints {_minted_name!r} -- "
                f"--reveal cannot show a token for: {', '.join(unknown)}",
                file=sys.stderr,
            )
            return 1
        if args.add is not None:
            rc, token = add_agent(args.add, install_path=args.install_path,
                                  role=args.role, install_kind=install_kind)
        else:
            rc, token = add_agent(args.remint, install_path=args.install_path,
                                  role=args.role, replace=True,
                                  install_kind=install_kind)
        if rc != 0:
            return rc
        if args.reveal:
            out = _reveal_stream(args.reveal_fd)
            print(file=out)
            print("⚠ REVEALING raw token value(s) below — run this yourself, NEVER through", file=out)
            print("  an agent. Agent transcripts are durable: piping this output through an", file=out)
            print("  agent turns \"shown once\" into \"stored forever\".", file=out)
            print(f"  {_minted_name}: AGENT_TOKEN={token}", file=out)
            out.flush()
        return 0

    roster = _resolve_roster(_DEFAULT_GATEWAY_ENV)
    unknown = [n for n in args.reveal if n not in roster]
    if unknown:
        print(f"✗ unknown agent(s) for --reveal: {', '.join(unknown)} "
              f"(known: {', '.join(roster)})", file=sys.stderr)
        return 1

    tokens, _digests, failures = mint(env_path=_DEFAULT_GATEWAY_ENV, roster=roster,
                                      revealing=args.reveal)

    if args.reveal:
        out = _reveal_stream(args.reveal_fd)
        print(file=out)
        print("⚠ REVEALING raw token value(s) below — run this yourself, NEVER through", file=out)
        print("  an agent. Agent transcripts are durable: piping this output through an", file=out)
        print("  agent turns \"shown once\" into \"stored forever\".", file=out)
        for name in args.reveal:
            if name not in tokens:
                print(f"  {name}: REFUSED this mint (no directory / not registered — see above)", file=out)
                continue
            print(f"  {name}: AGENT_TOKEN={tokens[name]}", file=out)
        out.flush()

    # Exit 0 even on a partial failure. A nonzero return makes bootstrap's set -e drop the safe merged line before it can be applied.
    # bootstrap greps "PARTIAL FAILURE" after applying that line, and exits nonzero itself.
    return 0


if __name__ == "__main__":
    sys.exit(main())
