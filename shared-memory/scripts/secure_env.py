"""Shared split-env loader for the framework's three long-running processes —
hive_mind_proxy.py, rem_loop.py, consolidation_loop.py (Credential Custody
workstream (a), PR A1 — secrets out of the process environment).

Every prior release had each process copy the whole framework `.env` into
`os.environ`, which meant every secret it held (PG_PASSWORD, NEO4J_PASSWORD,
AGENT_TOKENS, provider keys) was visible to `/proc/<pid>/environ` for that
process AND to any child it spawned (`os.environ.copy()` in the proxy's old
`_daemon_env()`). This module replaces that with a SPLIT:

  - config keys  -> os.environ, via setdefault, exactly as every loader in
    this family already did. An operator-exported value always wins.
  - secret keys  -> held ONLY in this module's in-process dict (`_secrets`),
    read back exclusively through get_secret(). Never exported to os.environ,
    so a secret can never appear in this process's own /proc/<pid>/environ
    (that was already true, since it never lived there) NOR leak into a
    child's environment through a wholesale os.environ.copy().

Classification is deliberately BOTH a known-name list and a suffix pattern
(SEC-09, ruled 2026-08-14): a new secret key must be caught even when nobody
remembers to extend the list.

Security-review fix round (2026-08-14, same-day re-review of this PR) folded
in below — each fix is flagged where it lands.

PR A2 (SEC-10) adds `read_daemon_token_from_fd()`: the daemon's own
AGENT_TOKEN, which PR A1 still passed via the child environment as one
named interim exception, now crosses only through an inherited pipe fd —
see hive_mind_proxy._daemon_env_and_token_fd() for the write side.

PR A4 (SEC-06) adds two DEPLOYER file-based ingestion paths for every
secret-classified key, both feeding this module's internal store directly —
neither may ever reach os.environ or a child env, extending the same
invariant PR A1 established for the plaintext .env case:

  - `<KEY>_FILE`         — Docker official-images convention: if set (in the
    process environment or the framework .env), its value is a path; the
    secret is read from that file.
  - `$CREDENTIALS_DIRECTORY/<key, lowercased>` — systemd `LoadCredential=`:
    if the systemd-managed credentials directory is present and contains a
    file named after the key (lowercase is the systemd norm), the secret is
    read from there.

PRECEDENCE (highest first), and this is the ENTIRE precedence — nothing
above it is skipped, nothing below it is consulted once a tier resolves:

  1. An operator's own os.environ export — unchanged since PR A1 review fix
     #1 (get_secret() checks os.environ FIRST, always). SEC-06 (ii) below
     makes this path advisory-flagged, not forbidden.
  2. $CREDENTIALS_DIRECTORY/<key>   — systemd-managed delivery, the most
     operationally locked-down of the three; a deployment that configures it
     did so deliberately.
  3. <KEY>_FILE                     — Docker official-images convention; a
     deployer named a specific mount.
  4. shared-memory/.env plaintext value — what every prior release did; the
     fallback of last resort.

Tiers 2-4 all land in this module's in-process store, never os.environ,
exactly like the plaintext case PR A1 already covered — see
_credentials_directory_secret() / _file_indirection_secret() /
load_split_env() below, and test_secrets_out_of_process_env.py /
test_deployer_file_secrets.py for the mutation-checked coverage.

SEC-06 (ii): a known-secret key found ALREADY SET in this process's own exec
environment when load_split_env() runs (EnvironmentFile=, an exported shell
var) prints one advisory log line naming the KEY NAME ONLY — never the
value — pointing at the _FILE/LoadCredential= alternative. Advisory, not a
refusal: the value is still honoured (tier 1 above).
"""
import json
import os
import re
import stat
import sys
from pathlib import Path

# PG_CONN embeds the Postgres password, so treating it as config would export it. AGENT_TOKEN is already secret by suffix; it is listed so the advisory can see a key that is never written to shared-memory/.env.
KNOWN_SECRET_NAMES = {
    "PG_PASSWORD",
    "NEO4J_PASSWORD",
    "TAVILY_API_KEY",
    "AGENT_TOKENS",
    "AGENT_TOKEN",
    "BACKUP_ADMIN_TOKEN",
    "PG_CONN",
}

# Suffix matches that are config, checked before the pattern. EMBED_CHARS_PER_TOKEN is a ratio; the advisory-lock keys are integers. AGENT_TOKEN matches _TOKEN and stays a secret so it is never exported.
KNOWN_CONFIG_NAMES = {
    "EMBED_CHARS_PER_TOKEN",
    "BACKUP_ADVISORY_LOCK_KEY",
    "NREM_PRIORITY_ADVISORY_LOCK_KEY",
}

# Catches provider keys the explicit list does not name. _KEY is wide enough to hit the advisory-lock ids, which is why those are excluded first.
_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)

# Names from token_env that match no suffix. Once seen they stay secret for the process; a reload must not forget a credential name.
_dynamic_secret_names: set[str] = set()

# A bad LLM_BACKENDS_JSON must not raise at import. This flag makes the same failure loud at the entrypoint, and a later good value clears it.
_llm_backends_json_parse_failed: bool = False

# In-process only. Populated by load_split_env(); read by get_secret(). Never
# written to os.environ and never handed to a subprocess env dict wholesale —
# see hive_mind_proxy._daemon_env(), which filters by is_secret_key() instead
# of passing this dict (or os.environ) through.
_secrets: dict[str, str] = {}

# SEC-06 (ii): names already advised-on in THIS process, so a module that
# calls load_split_env() more than once (every test in this file reloads
# daemons repeatedly) does not spam the same advisory on every call. Cleared
# only by a test harness that owns the module's lifetime, same as
# _dynamic_secret_names above.
_advised_exec_env_names: set[str] = set()

# NEW-1 (fix round 2): the same de-duplication for _derive_file_pointer_
# candidates()'s "non-secret _FILE pointer ignored" warning — see that
# function's docstring.
_advised_ignored_file_pointer_names: set[str] = set()


def _normalize_key(name: str) -> str:
    """Fix round F11 (SEC1 MED-7 + LOW-8): THE one key normaliser every
    classification/lookup site in this module uses — is_secret_key(),
    _token_env_names()'s storage insert, get_secret()'s canonical fallback,
    load_split_env()'s canonical collapse, and both derived-candidate
    helpers. Strips BOM (U+FEFF) and whitespace from both ends, in EITHER
    order and any interleaving, then upper-cases.

    Why a loop instead of one `.strip().lstrip("﻿")` (or the reverse)
    pass: a single fixed order only handles ONE of the two orderings a raw
    line can carry. Probed (SEC1 finding 8): "﻿ AGENT_TOKENS" (BOM
    THEN space) needs BOM stripped first, or the space survives as a
    leftover middle character once lstrip("﻿") alone is applied
    second; " ﻿AGENT_TOKENS" (space THEN BOM) needs the reverse order.
    A fixed single-order strip therefore classifies the SAME logical key
    (AGENT_TOKENS with incidental BOM/whitespace noise) as secret on one
    side and config on the other, depending only on which order that
    particular caller happened to use. This loop strips whichever
    character is at each end, repeatedly, so both orderings — and any
    number of repeats — normalise identically."""
    s = name
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    while s and (s[-1].isspace() or s[-1] == "﻿"):
        s = s[:-1]
    return s.upper()


_EXPORT_PREFIX_RE = re.compile(r"^export\s+", re.IGNORECASE)


def _strip_export_prefix(key: str) -> str:
    """Fix round F5 (SEC1 HIGH-3 + MED-5): strip an optional leading shell
    `export ` keyword — CASE-INSENSITIVELY — from a raw .env line's key
    text, at PARSE time, before it is stored anywhere or classified.
    "export AGENT_TOKENS=..." / "EXPORT AGENT_TOKENS=..." both become the
    classifiable key "AGENT_TOKENS", exactly as a plain "AGENT_TOKENS=..."
    line would. Without this, the stored key was the literal string
    "export AGENT_TOKENS" — not in KNOWN_SECRET_NAMES (exact-match only)
    and not matching the "_TOKEN" suffix (it ends in "TOKENS", the plural,
    with "export " still attached) — so a shell-sourceable .env using this
    legitimate form had its registry exported straight into os.environ,
    fail-OPEN on the exact form D.4 had just finished blessing on the MCP
    detection side. BOM/whitespace is stripped before AND after the prefix
    check, since either can precede or follow the keyword."""
    s = name = key
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    s = _EXPORT_PREFIX_RE.sub("", s, count=1)
    while s and (s[0].isspace() or s[0] == "﻿"):
        s = s[1:]
    return s


def _strip_balanced_quotes(value: str) -> str:
    """S16g (HYG round, R-G'): a `.env` VALUE wrapped in ONE balanced pair of
    surrounding quotes — `"v"` or `'v'` — has that pair stripped; everything
    else (an unbalanced leading quote with no matching trailing one, a bare
    quote embedded in the value, mismatched quote characters, or no quotes
    at all) is kept VERBATIM. Applied identically here, in memory_bridge.py's
    manual fallback parser, and in mcp/vector-skill.py's manual fallback
    parser — three independent copies of the same rule, since none of the
    three may import from another (Group 1: the client/server surface
    split). Does NOT apply to _read_secret_file()'s `<KEY>_FILE` /
    $CREDENTIALS_DIRECTORY path (~line 460 above) — that reads a mounted
    secret FILE, not a `.env` line, and strips only trailing CR/LF, never
    quotes or spaces; a recorded inconsistency, not fixed by this round."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def _parse_env_val(val: str) -> str:
    """Parse a .env value, stripping balanced quotes and inline comments.

    Order matters: quote protection is checked first so that hashes embedded
    in balanced quotes (e.g. KEY="foo # bar") are preserved verbatim rather
    than truncated by comment stripping. Inverting this order breaks quoted
    hash values.
    """
    val = val.strip()
    if not val:
        return ""
    if val[0] in ('"', "'"):
        q = val[0]
        closing = val.find(q, 1)
        if closing != -1:
            rest = val[closing + 1:].strip()
            if not rest or rest.startswith("#"):
                return val[1:closing]
    m = re.search(r"\s+#.*$", val)
    if m:
        val = val[:m.start()].strip()
    elif val.startswith("#"):
        return ""
    return _strip_balanced_quotes(val)


def read_env_value(
    env_path: "Path | str | None", key: str, default: "str | None" = None
) -> "str | None":
    """Return the parsed value of `key` from the .env file at `env_path`.

    Uses the single Python parser for framework environment files:
    normalises CRLF, strips balanced quotes, strips inline comments
    outside quotes, and resolves deterministically with last-definition-wins.
    Returns `default` if the file does not exist or the key is not present.
    """
    if env_path is None:
        p = _select_env_file()
    else:
        p = Path(env_path)
    if p is None or not p.is_file():
        return default

    target_canonical = _normalize_key(key)
    found = False
    result = default

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r\n").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, val = line.partition("=")
        k = _strip_export_prefix(k).strip()
        if not k:
            continue
        if _normalize_key(k) == target_canonical:
            found = True
            result = _parse_env_val(val)

    return result if found else default


read_env_key = read_env_value



def is_secret_key(name: str) -> bool:
    """True if `name` must never be exported to os.environ or forwarded into
    a child process environment — the known-config allowlist (checked first,
    so it always wins), then the known-name list / dynamically-discovered
    token_env names, then the suffix pattern (case-insensitive, fix #6).

    D.2 (SEC round, ADV1-3): `name` is normalised ONCE, here, before every
    comparison, via the shared _normalize_key() (F11 — strip surrounding
    whitespace and a stray leading BOM, U+FEFF, in either order; upper-
    case). Before this fix the exact-match checks (KNOWN_CONFIG_NAMES /
    KNOWN_SECRET_NAMES / _dynamic_secret_names) compared the RAW candidate,
    so a lowercase or BOM-prefixed spelling of a well-known name (a
    lowercase `agent_tokens=` line, in particular — AGENT_TOKENS does not
    even match the suffix pattern, since it ends in the PLURAL "_TOKENS",
    not "_TOKEN") fell through every check and was misclassified as
    ordinary config, then exported to os.environ by load_split_env()'s
    config loop and forwarded into a daemon's child env by _daemon_env().
    Both callers already pass this same normalised form to
    _dynamic_secret_names (see _token_env_names below) and to
    load_split_env()'s own storage key (see the CANONICAL-key comment
    there), so this is the matching normalisation on the lookup side —
    fixing only one side would be a classification INVERSION (ADV1-3), not
    a fix."""
    key_norm = _normalize_key(name)
    if key_norm in KNOWN_CONFIG_NAMES:
        return False
    if key_norm in KNOWN_SECRET_NAMES or key_norm in _dynamic_secret_names:
        return True
    return key_norm.endswith(_SECRET_SUFFIXES)


def _token_env_names(raw_json: str) -> set[str]:
    """Every `token_env` name LLM_BACKENDS_JSON references — SEC-09's 'every
    token_env name from backend config' clause, for the case such a name
    doesn't happen to match the suffix pattern. Malformed/absent JSON yields
    an empty set; hive_mind_proxy._load_llm_backends() does the real
    (stricter) validation later — this is classification only, and must not
    raise on input it will reject anyway.

    D.1 (SEC round, ADV1-2): a genuine parse failure of a NON-EMPTY
    raw_json — invalid JSON, or valid JSON that isn't a list — sets the
    module-level _llm_backends_json_parse_failed flag (still returning the
    empty set; this function must never raise). An absent/empty raw_json is
    NOT a failure (nothing was ever declared) and clears the flag, as does a
    later call that parses cleanly — so a corrected value (an operator's
    fix, or a test's own re-invocation with a clean env) un-wedges the
    fatal-at-main() check require_llm_backends_json_parses() enforces below.

    D.2 (ADV1-3): every discovered name is normalised (see is_secret_key's
    docstring) BEFORE it lands in _dynamic_secret_names, so a lowercase or
    BOM-prefixed `token_env` value classifies correctly regardless of the
    case is_secret_key() is later asked about."""
    global _llm_backends_json_parse_failed
    names: set[str] = set()
    if not raw_json:
        _llm_backends_json_parse_failed = False
        return names
    try:
        entries = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        _llm_backends_json_parse_failed = True
        return names
    if not isinstance(entries, list):
        _llm_backends_json_parse_failed = True
        return names
    _llm_backends_json_parse_failed = False
    for entry in entries:
        if isinstance(entry, dict):
            token_env = entry.get("token_env")
            if isinstance(token_env, str) and token_env:
                names.add(_normalize_key(token_env))
    return names


def llm_backends_json_parse_failed() -> bool:
    """True iff the most recent load_split_env() call found a non-empty
    LLM_BACKENDS_JSON that failed to parse as a JSON array (D.1). Consulted
    by require_llm_backends_json_parses(), never by classification logic
    itself."""
    return _llm_backends_json_parse_failed


def require_llm_backends_json_parses(daemon_name: str) -> None:
    """FATAL, one line, naming LLM_BACKENDS_JSON — D.1 (SEC round, ADV1-2).

    Call from hive_mind_proxy.main() and each daemon's own __main__ guard
    ONLY, matching require_db_credentials()'s established placement: every
    test in this repo imports these modules with a malformed
    LLM_BACKENDS_JSON on purpose (to test hive_mind_proxy's OWN, separate,
    non-fatal fallback-to-legacy-pool handling of the same malformed value —
    see _load_llm_backends() and check_config.py's
    test_llm_pool_fallback_reason_is_rendered_prominently_and_exit_stays_0),
    so an unconditional check at import time would kill test collection AND
    contradict that RULED, exit-stays-0 behaviour. This function is never
    called from check_config.py for exactly that reason — check_config is an
    audit tool that must keep reporting on a config the gateway would
    refuse, per its own established architecture (A.4's identical exclusion
    for require_no_backend_url_credentials()).

    Why this refusal exists at all rather than leaving the existing
    fallback-to-legacy behaviour as the whole story: a malformed
    LLM_BACKENDS_JSON silently loses every dynamically-discovered
    token_env name (_token_env_names() returns an empty set on a parse
    failure, by design — it must never raise), so a provider key whose name
    doesn't happen to match the suffix pattern stays classified as ordinary
    config and can reach _daemon_env()'s copy set into a spawned daemon's
    child environment — a fail-OPEN specifically because the developer typo'd
    a comma, not because they intended plaintext delivery. Refusing to start
    at all closes that path outright rather than degrading it.
    """
    if llm_backends_json_parse_failed():
        raise SystemExit(
            f"FATAL ({daemon_name}): LLM_BACKENDS_JSON is set but is not "
            "valid JSON (or not a JSON array) — fix the syntax (a trailing "
            "comma or an unescaped quote is the usual cause) or unset the "
            "variable entirely."
        )


# R1 (fix round 1, Opus review, probe-confirmed): hard ceiling on a single
# secret file's size, env-overridable per the portability rule (our 64 KiB
# default is generous — the largest thing this ever holds is a provider API
# key or a DSN, both far under 1 KiB in practice; a deployment with a larger
# legitimate secret can raise it).
_SECRET_FILE_MAX_BYTES = int(
    os.environ.get("SECURE_ENV_SECRET_FILE_MAX_BYTES", str(64 * 1024))
)


def _first_control_character(value: str) -> "tuple[int, str] | None":
    """(offset, character) of the first C0 control character or DEL in
    `value`, else None. Deliberately NOT `str.isprintable()` and not a
    unicodedata category test: those also reject non-ASCII letters and
    non-breaking spaces, which a secret may legitimately contain. The only
    class this refuses is the one that cannot survive the journey a secret
    read from a file actually makes — into an HTTP header value, where
    aiohttp raises `ValueError: Forbidden control character detected in
    headers` per request rather than at load."""
    for offset, ch in enumerate(value):
        if ord(ch) < 0x20 or ch == "\x7f":
            return offset, ch
    return None


def _read_secret_file(path: Path, *, source: str) -> "str | None":
    """Read one secret value from `path` (SEC-06 i, PR A4). Never raises: an
    unreadable, missing, non-regular, oversized, or empty file WARNS to
    stderr and returns None so the caller falls through to the next
    precedence tier — a mount that came and went (or a deployer who has not
    wired this tier yet) must not crash a daemon's startup.

    R1 (fix round 1, Opus review, probe-confirmed): the original cut
    `stat()`'d the PATH (mode check only) then called `path.read_text()`
    unconditionally — no regular-file check, no size ceiling. A FIFO hangs
    the open() forever (probe: `timeout 10` against a scratch FIFO exited
    124, still blocked); `/dev/zero` reads unbounded into memory (probe:
    exit 124 after only the loose-mode warning). Both are reachable from a
    typo'd `_FILE` pointer or a same-uid `systemctl --user set-environment
    PG_PASSWORD_FILE=/path/to/fifo` (still the documented LLM_BACKENDS_JSON
    delivery channel, and it persists in the user manager across restarts) —
    a silent, permanent denial of service on the memory hive: the gateway
    never reaches its listener, never logs, and systemd sees a start that
    neither succeeds nor fails.

    Fixed with the fd-safe pattern, in this order:
      1. `os.open(path, O_RDONLY | O_NONBLOCK)` — O_NONBLOCK is what stops
         the OPEN itself blocking on a FIFO with no writer (open(2): a
         non-blocking read-only open of a FIFO returns immediately instead
         of waiting for a writer to connect). Harmless on a regular file —
         O_NONBLOCK has no effect on regular-file I/O per POSIX.
      2. `os.fstat(fd)` — fstat the OPEN FD, never re-stat the path. This is
         also what closes the stat-then-read TOCTOU Opus flagged (O1): the
         type/mode check and the eventual read both operate on the exact
         same kernel object, so nothing can be swapped in between.
      3. `stat.S_ISREG` required, else WARN + return `None` — a FIFO,
         character device, block device, directory, or socket is refused
         BEFORE a single byte is read. This alone is what stops the
         `/dev/zero` scenario: the read call is never reached.
      4. Over-cap decided from `st.st_size` (already in hand from the same
         `fstat`) FIRST, before a single byte is read — NEW-3 (fix round 2):
         the original cut decided over-cap from `len(os.read(fd, cap + 1))`
         alone, a SINGLE read call. `read(2)` is permitted to return FEWER
         bytes than requested (a signal, a network filesystem, a pipe) — a
         short first read on a file genuinely over the cap would have been
         silently accepted as the WHOLE secret, truncated, with no warning
         at all. The read itself is now a LOOP that continues until EOF (an
         empty read) or the running total exceeds the cap, so a short
         individual `read()` can never be mistaken for end-of-file. The
         length-based check (`len(raw_bytes) > _SECRET_FILE_MAX_BYTES`)
         stays as a BACKSTOP after the loop, for a file whose `st_size` lies
         (a procfs-style pseudo-file reporting 0 while still yielding
         content). Either path WARNS and is treated as unset rather than
         partially/silently truncated.

    Deliberately NO `O_NOFOLLOW`. A `_FILE` pointer is the Docker/Kubernetes
    convention this function exists to serve, and Kubernetes mounts a
    Secret as a chain of symlinks through an atomically-swapped `..data`
    directory (that indirection is how it rotates a mounted Secret without
    the consuming process seeing a torn file) — `O_NOFOLLOW` would make
    every Kubernetes Secret mount unreadable by this loader, which is a
    bigger and more common failure than the credential-substitution risk it
    would close (Opus O1's broader point — real substitution defence needs
    an owner/parent-directory check too, not just `O_NOFOLLOW`, and is
    deferred past this fix round; see the handoff). This is a considered
    decision, not an oversight — read this paragraph before adding
    `O_NOFOLLOW` here.

    Loose permissions (group/world read or write) WARN but do NOT refuse to
    read: the Docker official-images `_FILE` convention itself commonly
    mounts secrets 0444 (world-readable inside the container, by design), so
    a hard refusal here would break the very convention this function exists
    to support. There is no existing hard-refuse posture anywhere else in
    this codebase for a file this framework did not itself create (only a
    tighten-or-warn posture, e.g. log_hygiene.append_secure) — this mirrors
    that, staying consistent rather than inventing a stricter rule for one
    ingestion path.

    Strips ALL trailing CR/LF characters (a run of `\\r` and/or `\\n` in any
    order) — and ONLY those two. Never .strip() / .rstrip(), which would
    also eat leading/trailing spaces that could be part of the literal
    secret; that reason is unchanged. What changed (v0.9.63) is the COUNT:
    stripping exactly one `\\n` left a `\\r` behind on the single most common
    way an operator produces this file. Every editor that saves with a
    final newline, `echo`, `pass show > file`, a heredoc, and any Windows
    or terminal paste appends one or more of these two characters — and
    NEITHER can ever be part of a legitimate HTTP header value, which is
    what a bearer token read through here becomes. So the run is a
    file-write artefact by construction, not secret content.

    Any OTHER control character that survives that normalisation
    (`ord(ch) < 0x20` or DEL `\\x7f` — an EMBEDDED CR/LF, TAB, NUL, ESC)
    is NOT an artefact of writing the file: it is a corrupt secret. The
    read refuses it, returns None, and WARNS with the source, the path,
    the offending byte as `\\xNN` and its CHARACTER offset (never the
    value's length), plus the `printf` recipe
    — never the secret's content. Measured cause (2026-08-26): a 37-byte
    file holding a 35-char key put one surviving control character into
    the `Authorization` header, and aiohttp rejected EVERY upstream
    request with "Forbidden control character detected in headers",
    per-request, with nothing naming the key file. Refusing at LOAD makes
    the same misconfiguration one journal line that names the file, and
    the caller's existing unresolved-secret path (a backend excluded from
    the pool) takes it from there.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        print(f"[secure_env] WARNING: {source} ({path}) not readable ({exc}) "
              f"— falling through to the next credential source",
              file=sys.stderr)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            print(f"[secure_env] WARNING: {source} ({path}) is not a regular "
                  f"file (FIFO/device/directory/socket) — refusing to read, "
                  f"falling through to the next credential source",
                  file=sys.stderr)
            return None
        loose = stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        if loose:
            print(f"[secure_env] WARNING: {source} ({path}) is group/world-accessible "
                  f"(mode {oct(stat.S_IMODE(st.st_mode))}) — reading it anyway (a "
                  f"Docker secrets mount is commonly 0444 by design); tighten it "
                  f"if this is not a container mount", file=sys.stderr)
        # NEW-3 (fix round 2, probe-confirmed reasoning): st_size from the
        # SAME fstat call above is the PRIMARY over-cap decision, checked
        # before any byte is read — no reason to touch the file at all once
        # its own reported size already exceeds the cap.
        if st.st_size > _SECRET_FILE_MAX_BYTES:
            print(f"[secure_env] WARNING: {source} ({path}) is {st.st_size} "
                  f"bytes, over the {_SECRET_FILE_MAX_BYTES}-byte cap "
                  f"(SECURE_ENV_SECRET_FILE_MAX_BYTES) — refusing to read, "
                  f"treating as unset", file=sys.stderr)
            return None
        try:
            # Loop until EOF or the running total exceeds the cap — a
            # SINGLE os.read() call may legitimately return fewer bytes than
            # requested (signal, network filesystem, pipe), and treating
            # that short read as "the whole file" would silently truncate a
            # legitimate secret instead of refusing it.
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, _SECRET_FILE_MAX_BYTES + 1 - total)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _SECRET_FILE_MAX_BYTES:
                    break  # backstop trip — st_size lied; stop reading now
            raw_bytes = b"".join(chunks)
        except OSError as exc:
            print(f"[secure_env] WARNING: {source} ({path}) could not be read "
                  f"({exc}) — falling through to the next credential source",
                  file=sys.stderr)
            return None
    finally:
        os.close(fd)

    # Backstop only: st.st_size already refused an over-cap file above for
    # every NORMAL regular file. This catches the rare case where st_size
    # does not reflect the true readable content (a procfs-style pseudo-file
    # reporting 0 while still yielding bytes).
    if len(raw_bytes) > _SECRET_FILE_MAX_BYTES:
        print(f"[secure_env] WARNING: {source} ({path}) exceeds "
              f"{_SECRET_FILE_MAX_BYTES} bytes (SECURE_ENV_SECRET_FILE_MAX_BYTES) "
              f"— refusing to read, treating as unset", file=sys.stderr)
        return None

    raw = raw_bytes.decode("utf-8", errors="replace")
    # Normalise the FILE-WRITE ARTEFACT (v0.9.63): every trailing CR and LF,
    # in any order and any number — `\n`, `\r\n`, `\n\n`, `\r\n\r\n`. Only
    # those two characters; a trailing SPACE or TAB is left exactly where it
    # is, because a space can be part of a literal secret (see the docstring)
    # and a tab is not a write artefact — it is corruption, refused below.
    raw = raw.rstrip("\r\n")
    if not raw.strip():
        print(f"[secure_env] WARNING: {source} ({path}) is empty — treating "
              f"as unset", file=sys.stderr)
        return None
    bad = _first_control_character(raw)
    if bad is not None:
        offset, ch = bad
        # No length: under the cap it is the secret's length. offset is a character index, not a byte position.
        print(f"[secure_env] WARNING: {source} ({path}) contains a control "
              f"character \\x{ord(ch):02x} at character offset {offset} "
              f"— refusing to use it, treating as "
              f"unset. A secret read from a file must not contain control "
              f"characters (they cannot appear in an HTTP header value). "
              f"Rewrite the file with: printf '%s' '<key>' > {path}",
              file=sys.stderr)
        return None
    return raw


# token_env is an arbitrary string. Without this, `../../../home/user/.ssh/id_rsa` would be read and sent as a bearer token.
_VALID_KEY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _credentials_directory_secret(key: str) -> "str | None":
    """Tier 2: `$CREDENTIALS_DIRECTORY/<key, lowercased>` — systemd
    `LoadCredential=`. Lowercase is the systemd norm (`LoadCredential=` names
    are conventionally lowercase, and $CREDENTIALS_DIRECTORY is systemd's own
    env var, always already present when the unit uses LoadCredential= — this
    module only ever reads it, never sets it). A deployer who names the
    credential in a different case gets a silent miss here by construction;
    the module docstring and ops/hive-mind-gateway.service's commented
    example both state the convention so that is a documentation problem,
    not a code one."""
    if not _VALID_KEY_NAME.match(key):
        print(f"[secure_env] WARNING: candidate key {key!r} fails the "
              f"safe-name check ({_VALID_KEY_NAME.pattern}) — refusing to "
              f"use it as a path component under $CREDENTIALS_DIRECTORY",
              file=sys.stderr)
        return None
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not cred_dir:
        return None
    path = Path(cred_dir) / key.lower()
    if not path.exists():
        return None
    return _read_secret_file(path, source="$CREDENTIALS_DIRECTORY entry")


def _file_indirection_secret(key: str, file_values: dict) -> "str | None":
    """Tier 3: `<KEY>_FILE` — Docker official-images convention. The pointer
    itself (the `_FILE` var's VALUE, i.e. the path) follows the same
    os.environ-first-then-.env-file precedence every other config lookup in
    this module already uses (matches load_split_env()'s own
    LLM_BACKENDS_JSON resolution) — only the SECRET the path points at is
    withheld from os.environ, never the path string, which is not itself
    sensitive."""
    if not _VALID_KEY_NAME.match(key):
        print(f"[secure_env] WARNING: candidate key {key!r} fails the "
              f"safe-name check ({_VALID_KEY_NAME.pattern}) — refusing to "
              f"resolve its _FILE pointer", file=sys.stderr)
        return None
    file_key = f"{key}_FILE"
    raw_path = (os.environ.get(file_key) or file_values.get(file_key, "")).strip()
    if not raw_path:
        return None
    return _read_secret_file(Path(raw_path), source=file_key)


def _warn_secrets_in_exec_environment(candidate_keys: set) -> None:
    """SEC-06 (ii): advisory only, never a refusal. A known-secret key
    already present in THIS process's own exec environment when
    load_split_env() runs arrived via EnvironmentFile=, an exported shell
    var, or similar — visible to /proc/<pid>/environ for this process and
    inherited by any child that copies os.environ wholesale (the exact
    exposure PR A1 closed everywhere in this codebase's own control). The
    value is still honoured (get_secret() checks os.environ first) — this
    only tells the deployer a safer alternative exists. Never logs a value,
    only the key NAME. De-duplicated per process via _advised_exec_env_names
    so a module reloaded many times (every test in this file) does not spam
    the same line repeatedly."""
    for key in sorted(candidate_keys):
        if key in os.environ and key not in _advised_exec_env_names:
            _advised_exec_env_names.add(key)
            print(
                f"[secure_env] ADVISORY: {key} is set directly in this "
                f"process's environment (EnvironmentFile= or an exported "
                f"shell var) — visible via /proc/<pid>/environ and inherited "
                f"by any child that copies the full environment. Prefer "
                f"{key}_FILE or $CREDENTIALS_DIRECTORY/{key.lower()} "
                f"(systemd LoadCredential=) instead. Advisory only — the "
                f"value is still honoured.",
                file=sys.stderr,
            )


def _derive_file_pointer_candidates(file_values: dict) -> set[str]:
    """R4 / QF-3 (fix round 1, Opus + Fable review, probe-confirmed): scan
    every `<K>_FILE` name present — in THIS process's own environment OR the
    parsed .env file — and derive `K` as a secret candidate when
    `is_secret_key(K)` accepts it.

    Without this, setting ONLY `<KEY>_FILE` for a key that is not on
    KNOWN_SECRET_NAMES, not already present as a plaintext line in .env, and
    not a discovered LLM_BACKENDS_JSON token_env name resolved to NOTHING —
    with NO warning at all, because the code never reached
    `_read_secret_file()` in the first place. Probe-confirmed on
    `AGENT_TOKEN_FILE` and `DEEPSEEK_API_KEY_FILE`, both of which resolved to
    `None` before this fix even with the secret file present, readable, and
    correctly formatted.

    NEW-1 (fix round 2, Opus review, probe-confirmed): CANDIDATE DERIVATION
    still scans BOTH sources — os.environ (an operator's own
    `export PG_PASSWORD_FILE=...` must still work) and the parsed .env file
    — but the "non-secret pointer ignored" WARNING below is now emitted
    ONLY for a name sourced from the PARSED .ENV FILE. A line in
    shared-memory/.env is addressed to this framework; an ambient env var
    ending in `_FILE` (`SSL_CERT_FILE`, `GIT_INDEX_FILE`, and any number of
    others a shell can already be carrying) is not this framework's
    business at all. Before this fix the warning fired for every such
    ambient name on EVERY `load_split_env()` call, un-deduplicated — probe-
    confirmed live on `SSL_CERT_FILE`/`GIT_INDEX_FILE`. De-duplicated per
    process via `_advised_ignored_file_pointer_names`, the same pattern
    `_advised_exec_env_names` already uses for the SEC-06 (ii) advisory.

    Fix round F12 (SEC1 LOW-9): the `_FILE` suffix check used to be exact-
    case (`name.endswith("_FILE")`), so a lowercase `agent_tokens_file=`
    line's actual suffix ("_file") never matched at all — the pointer was
    silently ignored with no candidate derived and no warning (the warning
    branch is also suffix-gated, so it never fired either). Now case-folds
    each candidate name via _normalize_key() (F11) before the suffix check,
    same as every other classification site in this module."""
    candidates: set[str] = set()
    file_keys_canonical = set(file_values)   # already canonical (F6 collapse)
    for name in set(os.environ) | file_keys_canonical:
        key_candidate = _normalize_key(name)
        if not key_candidate.endswith("_FILE"):
            continue
        key = key_candidate[: -len("_FILE")]
        if not key:
            continue
        if is_secret_key(key):
            candidates.add(key)
        elif (key_candidate in file_keys_canonical
              and key_candidate not in _advised_ignored_file_pointer_names):
            _advised_ignored_file_pointer_names.add(key_candidate)
            print(f"[secure_env] WARNING: {name} is set, but {key!r} is not "
                  f"classified as a secret — its _FILE pointer is ignored "
                  f"(only a secret-classified key can be delivered this way)",
                  file=sys.stderr)
    return candidates


def _derive_credentials_directory_candidates() -> set[str]:
    """R4 / QF-3 (fix round 1): list `$CREDENTIALS_DIRECTORY` (if set) and
    derive a candidate key from every entry's UPPERCASED filename, honoured
    only when `is_secret_key()` accepts it. Without this, `LoadCredential=`
    for a key outside the fixed set (e.g. `agent_token`, `deepseek_api_key`)
    silently delivered nothing either — same probe-confirmed gap as
    `_derive_file_pointer_candidates()` above, for the other tier."""
    candidates: set[str] = set()
    cred_dir = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not cred_dir:
        return candidates
    try:
        entries = os.listdir(cred_dir)
    except OSError:
        return candidates
    for entry in entries:
        key = entry.upper()
        if is_secret_key(key):
            candidates.add(key)
    return candidates


def _select_env_file() -> "Path | None":
    """Which .env file this process loads — the ONE decision point.

    ``SECURE_ENV_FILE`` overrides the candidate walk entirely: a path names
    the exact file to load; the EMPTY string means "load no env file at all"
    — the hermeticity contract the test suite pins in ``tests/conftest.py``,
    because this loader re-populates ``os.environ`` from the live deployer
    .env on every module reload, and a test can defeat that only by SETTING
    a key, never by deleting one (setdefault re-adds what delenv removed).
    A set-but-missing path is a deployer mistake and must be loud, not a
    silent fall-through to a DIFFERENT file than the one they named.

    Unset: the candidate walk unchanged from every prior release —
    shared-memory/.env first, the repo-root .env as the pre-0.6 fallback.
    """
    override = os.environ.get("SECURE_ENV_FILE")
    if override is not None:
        override = override.strip()
        if not override:
            return None
        p = Path(override)
        if p.exists():
            return p
        print(f"[secure_env] WARNING: SECURE_ENV_FILE={override!r} does not "
              f"exist — loading NO env file (refusing to fall through to a "
              f"file you did not name)", file=sys.stderr)
        return None
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / ".env", here.parent.parent.parent / ".env"]
    return next((p for p in candidates if p.exists()), None)


def load_split_env() -> None:
    """Parse the framework .env and split it between os.environ (config) and
    the in-process secrets store (everything is_secret_key() catches).

    Candidate order matches every other loader in this family (CLAUDE.md
    Group 4 / apply.py): shared-memory/.env first, the repo-root .env as the
    pre-0.6 fallback. Parsed by hand, one `key=val` per line — never
    `import dotenv` or any other parser library (an env loader must not
    depend on a library that might not be installed).

    Idempotent and additive: safe to call from more than one process/module
    in the same interpreter, never clears what a previous call (or an
    operator's own export) already established.

    PR A4 (SEC-06): every secret-classified value is now resolved from up to
    three tiers, in order — $CREDENTIALS_DIRECTORY/<key> (systemd
    LoadCredential=), then <KEY>_FILE (Docker convention), then the plaintext
    .env value — see the module docstring for the full precedence statement
    (an operator's direct os.environ export still wins over all three, via
    get_secret(), unchanged). The candidate key set is not limited to what
    the .env file happens to contain: KNOWN_SECRET_NAMES, any dynamically
    discovered token_env name, every `<K>_FILE` pointer actually present
    (fix round 1, `_derive_file_pointer_candidates()`), and every entry
    `$CREDENTIALS_DIRECTORY` actually contains (fix round 1,
    `_derive_credentials_directory_candidates()`) are all attempted, so a
    headless systemd deployment with NO plaintext shared-memory/.env at all
    can resolve ANY secret-classified credential purely from
    LoadCredential=/_FILE — not only the ones on the fixed list.

    Fix round F5/F6 (SEC1 HIGH-3/MED-5, HIGH-4): each raw line's key has an
    optional leading `export ` keyword stripped (case-insensitively, via
    _strip_export_prefix — F5) before it is stored anywhere, so
    "export AGENT_TOKENS=..."/"EXPORT AGENT_TOKENS=..." classify exactly
    like a plain "AGENT_TOKENS=..." line. `file_values` (F6) is now
    collapsed to CANONICAL keys ONCE, here, with the file's own last
    definition winning deterministically for a given canonical key — before
    this fix, two case-variant spellings of the same secret competed for
    the storage slot via SET ITERATION ORDER (`candidate_secret_keys` is a
    set), not file order, so which line won was unspecified and could even
    be a STALE line despite a later correction in the same file. This is
    ALSO the fix for F4 (SEC1 HIGH-2): `LLM_BACKENDS_JSON`'s own file-value
    read used to be an exact-case `file_values.get("LLM_BACKENDS_JSON")`,
    so a `llm_backends_json=`/BOM-prefixed spelling was invisible — read as
    ABSENT, not malformed, bypassing D.1's refusal entirely and leaking any
    token_env-named provider key the JSON declared. With `file_values`
    already canonical, the same exact-case read now sees every spelling.
    """
    env_path = _select_env_file()

    raw_pairs: list[tuple[str, str]] = []
    if env_path is not None:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = _strip_export_prefix(key).strip()
            if not key:
                continue
            val = val.strip()
            unquoted_val = _strip_balanced_quotes(val)
            if unquoted_val != val:
                print(
                    f"[secure_env] NOTE: {key}'s value in {env_path} was "
                    f"wrapped in a balanced surrounding quote pair — the "
                    f"pair is stripped; the value is used without it.",
                    file=sys.stderr,
                )
            raw_pairs.append((key, unquoted_val))

    # One spelling per key, last line wins. Otherwise two casings race a set's iteration order.
    file_values: dict[str, str] = {}
    for key, val in raw_pairs:
        file_values[_normalize_key(key)] = val

    # An exported value wins over the file, same as get_secret(). Still computed when there is no .env.
    llm_json = os.environ.get("LLM_BACKENDS_JSON") or file_values.get("LLM_BACKENDS_JSON", "")
    _dynamic_secret_names.update(_token_env_names(llm_json))

    # Config keeps the operator's spelling in os.environ. Secrets skip this path entirely.
    for key, val in raw_pairs:
        if not is_secret_key(key):
            os.environ.setdefault(key, val)

    # Includes names that exist only as a _FILE pointer or a credentials-directory entry. Without those, a present file resolved to None.
    candidate_secret_keys = (
        {k for k in file_values if is_secret_key(k)}
        | KNOWN_SECRET_NAMES
        | _dynamic_secret_names
        | _derive_file_pointer_candidates(file_values)
        | _derive_credentials_directory_candidates()
    )
    # Store and look up the canonical spelling. A lowercase agent_tokens= line otherwise misses get_secret("AGENT_TOKENS") and auth turns off. The first tier that resolves still wins.
    for key in candidate_secret_keys:
        canonical = _normalize_key(key)
        value = _credentials_directory_secret(canonical)
        if value is None:
            value = _file_indirection_secret(canonical, file_values)
        if value is None:
            value = file_values.get(canonical)
        if value is not None:
            _secrets.setdefault(canonical, value)

    _warn_secrets_in_exec_environment(candidate_secret_keys)


def get_secret(name: str, default: "str | None" = None) -> "str | None":
    """The one way a framework process should read a secret value.

    Review fix #1 (CRITICAL): os.environ is checked FIRST, then the
    in-process store. An operator-exported value must always win — that is
    load_split_env()'s own setdefault semantics for config keys, and this
    accessor's docstring always claimed it for secrets too, but the lookup
    order had it backwards: a value in the framework .env silently beat a
    value the operator (or a test, via monkeypatch.setenv/os.environ
    assignment) exported directly into the process environment. On any
    checkout that also has a real shared-memory/.env, that made
    os.environ-based configuration of a secret key unreachable — including
    coordinator.py's own AGENT_TOKENS test pattern.

    D.2 (ADV1-3) canonical fallback: an EXACT match on `name` (os.environ)
    is tried FIRST and unconditionally wins — this preserves every existing
    exact-case caller byte for byte, including a dynamic token_env lookup
    that legitimately uses a non-canonical spelling. Only when THAT misses
    does this fall back to the CANONICAL (stripped/BOM-stripped/upper-cased)
    form of `name` in os.environ, then the exact form in the in-process
    store, then the canonical form there — which is where load_split_env()
    now always stores a secret it resolved (see the storage-key
    canonicalisation note there). Without the exact-vs-canonical fallback,
    get_secret("AGENT_TOKENS") (used throughout this codebase as a fixed
    upper-case literal) would never see a value the .env file declared as
    `agent_tokens=`, even though is_secret_key() now correctly classifies it
    as secret and keeps it out of os.environ.

    Fix round F3 (SEC1 HIGH-1): the lookup order used to check the in-
    process store (`_secrets`, exact match) BEFORE the canonical form of
    os.environ — so a value the deployer's .env file resolved into
    `_secrets` won over a case-variant value the OPERATOR had exported
    directly into the process environment. Probed exactly as SEC1 measured
    it: `_secrets["AGENT_TOKENS"]="from-file"` +
    `os.environ["agent_tokens"]="from-operator"` must return
    "from-operator", never "from-file". The operator's own environment must
    always beat the file store, at BOTH the exact and the canonical step —
    this reorders the four checks so every os.environ tier (exact, then
    canonical) runs before either _secrets tier.

    The os.environ CANONICAL step is a case-insensitive SCAN of
    os.environ's actual key names, not a second exact lookup of
    `canonical(name)` — os.environ is a real, case-SENSITIVE mapping on
    POSIX (confirmed: setting `os.environ["agent_tokens"]` leaves
    `"AGENT_TOKENS" in os.environ` False), so a fixed-case second lookup
    can never find a genuinely lowercase operator export in the first
    place. A second exact-match probe would silently do nothing; the
    fallback this docstring promises requires actually looking."""
    if name in os.environ:
        return os.environ[name]
    canonical = _normalize_key(name)
    for env_key, env_val in os.environ.items():
        if _normalize_key(env_key) == canonical:
            return env_val
    if name in _secrets:
        return _secrets[name]
    if canonical in _secrets:
        return _secrets[canonical]
    return default


def read_daemon_token_from_fd(env_var: str = "AGENT_TOKEN_FD") -> "str | None":
    """Read this daemon's per-boot AGENT_TOKEN from the pipe fd the proxy
    handed it at spawn (SEC-10, Credential_Custody_Plan_2026-08-14 PR A2).

    Delivery shape: the fd NUMBER travels via `env_var`, a plain (non-secret)
    env var — a file descriptor number is meaningless off this process tree,
    so naming it costs nothing. The token VALUE itself crosses only through
    the pipe's kernel buffer: it appears in no `/proc/<pid>/environ`, no
    argv, and no file. See hive_mind_proxy._daemon_env_and_token_fd(), the
    write side of this same pipe.

    Returns None — never raises — when `env_var` is unset, not a valid
    integer, or the fd cannot be read (already closed, or this process was
    not actually spawned with one). That covers a standalone debug run of a
    daemon started directly (`python rem_loop.py`, no proxy in between): the
    caller's own fallback (`get_secret("AGENT_TOKEN")`, reading the
    framework .env or an operator's own export) is what makes that case work
    instead of a silent 401.

    Reads at most 4096 bytes in one call — token_urlsafe(32) is far under
    that, and the write side writes-then-closes before this ever runs, so a
    single read drains the whole buffered value.
    """
    raw_fd = os.environ.get(env_var, "").strip()
    if not raw_fd:
        return None
    try:
        fd = int(raw_fd)
    except ValueError:
        return None
    try:
        data = os.read(fd, 4096)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    token = data.decode("utf-8", errors="replace").strip()
    return token or None


def require_db_credentials(*, pg_password: str, pg_conn: str, neo4j_password: str,
                            daemon_name: str) -> None:
    """Review fix #4: fail LOUDLY, naming the cause, when a daemon has no way
    to authenticate to Postgres or Neo4j — never the bare
    `fe_sendauth: no password supplied` class of error the plan forbids.

    Call this from a daemon's `if __name__ == "__main__":` guard ONLY, never
    at bare module-import time: every test in this repo imports
    rem_loop/consolidation_loop without ever connecting for real (all SQL/
    Cypher is stubbed — see the repo's own testing discipline), so an
    unconditional exit here would kill test collection itself, not just a
    genuinely misconfigured daemon.

    `pg_conn` is the RAW value of an explicitly-set PG_CONN (empty string if
    unset) — not a constructed default DSN, which always looks non-empty
    even when it embeds an empty password and would defeat this check.
    """
    if not pg_password and not pg_conn:
        raise SystemExit(
            f"FATAL ({daemon_name}): no Postgres credential resolved — "
            "PG_PASSWORD and PG_CONN are both empty. Supply PG_PASSWORD (or "
            "a full PG_CONN) via shared-memory/.env, or the A4 file-based "
            "credential path once it ships."
        )
    if not neo4j_password:
        raise SystemExit(
            f"FATAL ({daemon_name}): no Neo4j credential resolved — "
            "NEO4J_PASSWORD is empty. Supply it via shared-memory/.env, or "
            "the A4 file-based credential path once it ships."
        )
