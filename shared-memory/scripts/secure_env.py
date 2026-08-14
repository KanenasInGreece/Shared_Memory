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
"""
import json
import os
import re
from pathlib import Path

# The explicit half of SEC-09's classification.
KNOWN_SECRET_NAMES = {
    "PG_PASSWORD",
    "NEO4J_PASSWORD",
    "TAVILY_API_KEY",
    "AGENT_TOKENS",
    "BACKUP_ADMIN_TOKEN",
}
# The pattern half — catches provider keys (DEEPSEEK_API_KEY, XAI_API_KEY, ...)
# and any future *_PASSWORD/*_TOKEN/*_API_KEY var nobody added to the list above.
_SECRET_SUFFIXES = ("_PASSWORD", "_TOKEN", "_API_KEY")

# In-process only. Populated by load_split_env(); read by get_secret(). Never
# written to os.environ and never handed to a subprocess env dict wholesale —
# see hive_mind_proxy._daemon_env(), which filters by is_secret_key() instead
# of passing this dict (or os.environ) through.
_secrets: dict[str, str] = {}


def is_secret_key(name: str) -> bool:
    """True if `name` must never be exported to os.environ or forwarded into
    a child process environment — the known-name list OR the suffix pattern."""
    if name in KNOWN_SECRET_NAMES:
        return True
    return name.endswith(_SECRET_SUFFIXES)


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
    if env_path is None:
        return

    raw_pairs: list[tuple[str, str]] = []
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        raw_pairs.append((key.strip(), val.strip()))

    # Extend the secret-name set with any token_env names THIS FILE declares,
    # so a backend credential classifies as secret even when its name doesn't
    # match the suffix pattern. (A token_env supplied only via the process's
    # own exec-time environment, not this file, is covered separately —
    # get_secret() falls back to os.environ for exactly that case.)
    file_values = dict(raw_pairs)
    llm_json = file_values.get("LLM_BACKENDS_JSON") or os.environ.get("LLM_BACKENDS_JSON", "")
    dynamic_secret_names = _token_env_names(llm_json)

    for key, val in raw_pairs:
        if key in dynamic_secret_names or is_secret_key(key):
            _secrets.setdefault(key, val)
        else:
            os.environ.setdefault(key, val)


def get_secret(name: str, default: "str | None" = None) -> "str | None":
    """The one way a framework process should read a secret value.

    Checks the in-process store first (populated by load_split_env() from the
    framework .env). Falls back to os.environ so a value the deployer or a
    test supplies through the process's own exec-time environment (systemd
    EnvironmentFile=, monkeypatch.setenv, an operator's `export`) is still
    honoured — that delivery path re-exposes the value via /proc/<pid>/environ
    on its own account, which is a separate, already-named anti-pattern
    (SEC-06, addressed for the file-based delivery path in PR A4) rather than
    something this accessor should silently break.
    """
    if name in _secrets:
        return _secrets[name]
    return os.environ.get(name, default)
