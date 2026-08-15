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
import stat
import sys
from pathlib import Path

# The explicit half of SEC-09's classification.
#
# Review fix #3: PG_CONN joins this list. A full DSN
# (postgresql://postgres:<pw>@host/db) embeds the Postgres password verbatim
# — treating PG_CONN as "config" would have exported that password to
# os.environ and every daemon's child env exactly as if PG_PASSWORD itself
# had leaked.
KNOWN_SECRET_NAMES = {
    "PG_PASSWORD",
    "NEO4J_PASSWORD",
    "TAVILY_API_KEY",
    "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN",
    "PG_CONN",
}

# Review fix #5: names that WOULD match the suffix pattern below but are
# genuine operator config, not secrets — checked BEFORE the pattern so they
# are never misclassified. Found by grepping .env.example and every script
# in this process family for a name ending in any of the (widened, fix #6)
# suffixes:
#   EMBED_CHARS_PER_TOKEN            — dream_telemetry.py's chars-per-token
#                                       ratio (a float knob, not a credential)
#   BACKUP_ADVISORY_LOCK_KEY         — a Postgres advisory-lock integer id
#   NREM_PRIORITY_ADVISORY_LOCK_KEY  — same, NREM's priority-wait lock id
# (AGENT_TOKEN, singular, also matches "_TOKEN" but is deliberately NOT on
# this list — it IS a secret. PR A1 delivered it to a daemon's child env as
# one named interim exception in hive_mind_proxy._daemon_env(); PR A2
# (SEC-10) closes that: a freshly-minted, per-boot daemon token now crosses
# only through an inherited pipe fd (see read_daemon_token_from_fd() below),
# never through the child environment at all. Putting AGENT_TOKEN on this
# list would still be the misclassification in the other direction — it
# stays classified secret so it can never be exported to os.environ either.)
KNOWN_CONFIG_NAMES = {
    "EMBED_CHARS_PER_TOKEN",
    "BACKUP_ADVISORY_LOCK_KEY",
    "NREM_PRIORITY_ADVISORY_LOCK_KEY",
}

# The pattern half — catches provider keys (DEEPSEEK_API_KEY, XAI_API_KEY, ...)
# and any future secret-shaped var nobody added to KNOWN_SECRET_NAMES above.
# Review fix #6: widened past the original three (_PASSWORD/_TOKEN/_API_KEY)
# to also catch _SECRET/_KEY/_CREDENTIAL(S), matched case-insensitively —
# is_secret_key() upper-cases the candidate before comparing. _KEY alone is
# broad enough to catch real config (the *_ADVISORY_LOCK_KEY pair above),
# which is exactly why KNOWN_CONFIG_NAMES exists and is checked first.
_SECRET_SUFFIXES = (
    "_PASSWORD", "_TOKEN", "_API_KEY", "_SECRET", "_KEY",
    "_CREDENTIAL", "_CREDENTIALS",
)

# Review fix #2: token_env names discovered at runtime from LLM_BACKENDS_JSON.
# A backend can name an arbitrary env var (e.g. "OPENROUTER_CREDENTIAL") that
# matches none of the suffixes above, so the suffix pattern alone cannot
# catch it — SEC-09's "every token_env name from backend config" clause is
# what does. Populated by load_split_env(), consulted by is_secret_key() (so
# _daemon_env()'s filter excludes it too). Module-level and additive: once a
# name is seen it stays classified secret for the life of the process — this
# is deliberately NOT reset by load_split_env() re-runs, only by a test
# harness that owns the module's lifetime (see the test file's fixture).
_dynamic_secret_names: set[str] = set()

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


def is_secret_key(name: str) -> bool:
    """True if `name` must never be exported to os.environ or forwarded into
    a child process environment — the known-config allowlist (checked first,
    so it always wins), then the known-name list / dynamically-discovered
    token_env names, then the suffix pattern (case-insensitive, fix #6)."""
    if name in KNOWN_CONFIG_NAMES:
        return False
    if name in KNOWN_SECRET_NAMES or name in _dynamic_secret_names:
        return True
    return name.upper().endswith(_SECRET_SUFFIXES)


def _token_env_names(raw_json: str) -> set[str]:
    """Every `token_env` name LLM_BACKENDS_JSON references — SEC-09's 'every
    token_env name from backend config' clause, for the case such a name
    doesn't happen to match the suffix pattern. Malformed/absent JSON yields
    an empty set; hive_mind_proxy._load_llm_backends() does the real
    (stricter) validation later — this is classification only, and must not
    raise on input it will reject anyway."""
    names: set[str] = set()
    if not raw_json:
        return names
    try:
        entries = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        return names
    if not isinstance(entries, list):
        return names
    for entry in entries:
        if isinstance(entry, dict):
            token_env = entry.get("token_env")
            if isinstance(token_env, str) and token_env:
                names.add(token_env)
    return names


def _read_secret_file(path: Path, *, source: str) -> "str | None":
    """Read one secret value from `path` (SEC-06 i, PR A4). Never raises: an
    unreadable, missing, or empty file WARNS to stderr and returns None so
    the caller falls through to the next precedence tier — a mount that came
    and went (or a deployer who has not wired this tier yet) must not crash
    a daemon's startup.

    Loose permissions (group/world read or write) WARN but do NOT refuse to
    read: the Docker official-images `_FILE` convention itself commonly
    mounts secrets 0444 (world-readable inside the container, by design), so
    a hard refusal here would break the very convention this function exists
    to support. There is no existing hard-refuse posture anywhere else in
    this codebase for a file this framework did not itself create (only a
    tighten-or-warn posture, e.g. log_hygiene.append_secure) — this mirrors
    that, staying consistent rather than inventing a stricter rule for one
    ingestion path.

    Strips EXACTLY ONE trailing newline (the standard secret-file
    convention — e.g. Docker's own `printf` recipe) — never .strip() /
    .rstrip(), which would also eat leading/trailing spaces that could be
    part of the literal secret.
    """
    try:
        st = path.stat()
    except OSError as exc:
        print(f"[secure_env] WARNING: {source} ({path}) not readable ({exc}) "
              f"— falling through to the next credential source",
              file=sys.stderr)
        return None
    loose = stat.S_IMODE(st.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
    if loose:
        print(f"[secure_env] WARNING: {source} ({path}) is group/world-accessible "
              f"(mode {oct(stat.S_IMODE(st.st_mode))}) — reading it anyway (a "
              f"Docker secrets mount is commonly 0444 by design); tighten it "
              f"if this is not a container mount", file=sys.stderr)
    try:
        raw = path.read_text()
    except OSError as exc:
        print(f"[secure_env] WARNING: {source} ({path}) could not be read "
              f"({exc}) — falling through to the next credential source",
              file=sys.stderr)
        return None
    if raw.endswith("\n"):
        raw = raw[:-1]
    if not raw.strip():
        print(f"[secure_env] WARNING: {source} ({path}) is empty — treating "
              f"as unset", file=sys.stderr)
        return None
    return raw


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
    the .env file happens to contain: KNOWN_SECRET_NAMES and any dynamically
    discovered token_env name are always attempted too, so a headless
    systemd deployment with NO plaintext shared-memory/.env at all can still
    resolve every credential purely from LoadCredential=/_FILE.
    """
    here = Path(__file__).resolve()
    candidates = [here.parent.parent / ".env", here.parent.parent.parent / ".env"]
    env_path = next((p for p in candidates if p.exists()), None)

    raw_pairs: list[tuple[str, str]] = []
    if env_path is not None:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            raw_pairs.append((key.strip(), val.strip()))

    # Review fix #2: the EFFECTIVE LLM_BACKENDS_JSON — os.environ first (an
    # operator/systemd-exported value, the documented provider-key delivery
    # path — AGENTS.md / ops/README.md), the file second. Same precedence as
    # get_secret() (fix #1): an exported value always wins. Computed even
    # when no .env file exists at all, since the mainline case is an
    # exec-env-only deployment.
    file_values = dict(raw_pairs)
    llm_json = os.environ.get("LLM_BACKENDS_JSON") or file_values.get("LLM_BACKENDS_JSON", "")
    _dynamic_secret_names.update(_token_env_names(llm_json))

    # Config keys: unchanged from every prior release — setdefault into
    # os.environ. Secret-classified keys are skipped here entirely; they are
    # resolved below, through the three-tier secret path instead (SEC-06 i:
    # they must never touch os.environ by any route, including this one).
    for key, val in raw_pairs:
        if not is_secret_key(key):
            os.environ.setdefault(key, val)

    # Secret keys: every name we can actually see as secret-shaped —
    # present in the .env file, on the fixed KNOWN_SECRET_NAMES list (so
    # LoadCredential=/_FILE alone can resolve a credential with no .env file
    # present at all), or a dynamically-discovered token_env name.
    candidate_secret_keys = (
        {k for k, _ in raw_pairs if is_secret_key(k)}
        | KNOWN_SECRET_NAMES
        | _dynamic_secret_names
    )
    for key in candidate_secret_keys:
        value = _credentials_directory_secret(key)
        if value is None:
            value = _file_indirection_secret(key, file_values)
        if value is None:
            value = file_values.get(key)
        if value is not None:
            _secrets.setdefault(key, value)

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
    """
    if name in os.environ:
        return os.environ[name]
    if name in _secrets:
        return _secrets[name]
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
