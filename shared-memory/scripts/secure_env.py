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
"""
import json
import os
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
# this list — it IS a secret, delivered to a daemon's child env as the one
# named interim exception in hive_mind_proxy._daemon_env(); see review fix
# #7. Putting it here would be the misclassification in the other direction.)
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

    for key, val in raw_pairs:
        if is_secret_key(key):
            _secrets.setdefault(key, val)
        else:
            os.environ.setdefault(key, val)


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
