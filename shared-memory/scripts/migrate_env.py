#!/usr/bin/env python3
"""migrate_env.py — one-time (per install) migration of the framework's LLM
routing config from implicit/legacy shapes to the explicit
``LLM_BACKENDS_JSON`` form, per the W3 build spec
(``Local_Documentation/ColdBriefs/W3_Env_Migration_Brief.md``, ``decision:1846``
on ``fact:1845``). Spec: ``Backend_Declaration_Spec_2026-08-30.md`` §4 — an
upgrade must never change a running install's behaviour, mechanically, not by
operator memory.

Standalone: no other module in this repo imports this one. Two top-level
modes:

    migrate_env.py --capture-preimage <out.json>      # capture only
    migrate_env.py [--preimage <in.json>] [--apply]    # evaluate / apply

Without ``--apply`` the second form is a DRY-RUN PREVIEW: it prints exactly
what an ``--apply`` run would do and writes nothing (the
``reconcile_project_identity.py`` shape). With ``--apply`` and no
``--preimage``, it SELF-CAPTURES a pre-image using the CURRENT loader before
making any change (N-9 — valid only while loader semantics are unchanged
from the running version; true at W3).

THE PROPERTY THIS TOOL EXISTS TO HOLD: ``old_code(pre_env) == new_code(post_env)``
— a successful migration only ever changes DECLARATION-STATUS fields
(``private_ok_explicit``, ``LLM_POOL_CONFIG_EMPTY``) on the entries it
reports touching; every BEHAVIOURAL field (urls, weights, whether a token is
present, models, roles, n_ctx, max_inflight, extra_body, effective
private_ok, the fallback-reason class, both startup-guard verdicts) must
come out identical. See ``two_layer_compare()`` below.

Exit codes (M-D12 — the bash caller dies on rc != 0, so this is
load-bearing):
    0   success, no-op, or any DECLINE-AND-REPORT outcome (unit-owned keys,
        credentialed-neither-key, roles-carrying entries, non-interactive
        case-4, an explicit "N" answer, present-but-empty keys). The env is
        untouched in every one of these; today's behaviour is unchanged.
    1   an outcome that must STOP the caller: a duplicate managed key, an
        unwritable/unresolvable write target, a capture failure or
        capture-schema mismatch, or a post-image divergence (after restore).

Never reads a raw secret value into anything this script itself prints or
persists: every URL rendered anywhere routes through
``log_hygiene.scrub_url_credentials``; every token is represented only as
``has_token: bool``.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import select
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import secure_env  # noqa: E402
from log_hygiene import scrub_url_credentials, _chmod_created_ancestors  # noqa: E402

CAPTURE_SCHEMA = 1
# Ruling A(a) 2026-08-31 (§6.4): a LOADER-SEMANTICS marker, distinct from
# CAPTURE_SCHEMA — CAPTURE_SCHEMA versions the pre-image FORMAT (this
# script's own JSON shape); LOADER_SEMANTICS versions what the GATEWAY's
# loader computes for a given raw env (private_ok's default direction,
# here). Bump this whenever such a default changes; never fold it into
# CAPTURE_SCHEMA. An image with no "loader_semantics" key (an old-code
# capture, or a hand-built test fixture) reads as 1 — trust-by-default —
# by construction wherever this is read (`.get("loader_semantics", 1)`).
LOADER_SEMANTICS = 2
DEFAULT_GATEWAY_UNIT = "hive-mind-gateway.service"
MANAGED_KEYS = ("EMBEDDER_URL", "RERANKER_URL", "LLM_DEFAULT_TARGET",
                "LLM_BACKENDS", "LLM_BACKENDS_JSON")
# The three env-default rows this tool may WRITE when absent (R-A: never
# LLM_DEFAULT_TARGET). Values come from framework_defaults.py — "read,
# never retyped" (decision:1032).
from framework_defaults import FRAMEWORK_DEFAULTS  # noqa: E402

EXIT_OK = 0
EXIT_STOP = 1


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _scrub(text: str) -> str:
    return scrub_url_credentials(str(text))


# ─────────────────────────────────────────────────────────────────────────
# Unit ownership (R-C) — ask the gateway unit what it owns before writing
# anything. Two distinct UNKNOWNs (NEW-B): systemctl ABSENT -> proceed
# normally (no unit, nothing can shadow); systemctl PRESENT but the query
# fails, or a named EnvironmentFile is unreadable -> a unit MAY exist ->
# every write declines.
# ─────────────────────────────────────────────────────────────────────────

class UnitQuery:
    """status is one of: 'no_systemctl' (proceed normally), 'ok' (owned_keys
    is authoritative), 'query_failed' (ALL writes must decline)."""

    def __init__(self, status: str, owned_keys: "set[str]" = frozenset(),
                 environment: "dict[str, str]" = None, error: str = None):
        self.status = status
        self.owned_keys = owned_keys
        self.environment = environment or {}
        self.error = error


class _EnvironmentParseError(Exception):
    """Raised when an `Environment=` line does not parse into clean
    KEY=VALUE tokens (SEC H-1) — the caller treats this as query_failed
    (decline every write) rather than silently missing an owned key."""


def _parse_systemctl_environment(raw: str) -> "dict[str, str]":
    """`Environment=KEY=VALUE KEY2=VALUE2 ...` (one line). systemd applies
    SHELL-STYLE quoting to each element when it contains whitespace, a
    quote, `$`, `\\`, or a glob character (systemd.syntax(7)) — e.g. a
    value carrying JSON with embedded double quotes (LLM_BACKENDS_JSON,
    always) is emitted as a single double-quoted, backslash-escaped token:

        Environment="LLM_BACKENDS_JSON=[{\\"url\\": \\"http://a:5000\\"}]"

    A naive `str.split()` on whitespace (the original cut) tokenizes this
    into garbage — `["LLM_BACKENDS_JSON` with a leading quote, which
    matches no MANAGED_KEYS name, so the R-C unit-ownership gate silently
    never fires for the one key it matters most for. `shlex.split()`
    (POSIX mode) tokenizes systemd's quoting correctly — verified against
    real `systemctl show -p Environment` output for both a JSON value and
    a plain space-containing one (SEC H-1 regression fixture).

    FAILS CLOSED: a line shlex cannot tokenize (unbalanced quotes) raises
    _EnvironmentParseError rather than silently returning a partial/wrong
    dict — the caller turns this into query_failed (every write declines),
    per the fix's own stated fallback ("failing that, treat 'does not
    parse into clean KEY=VALUE tokens' as query_failed")."""
    out: "dict[str, str]" = {}
    prefix = "Environment="
    for line in raw.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):].strip()
        if not rest:
            continue
        try:
            tokens = shlex.split(rest, posix=True)
        except ValueError as exc:
            raise _EnvironmentParseError(
                f"could not tokenize Environment= line ({type(exc).__name__})") from None
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k] = v
    return out


def _parse_systemctl_environment_files(raw: str) -> "list[str]":
    """`EnvironmentFiles=/path/to/file (ignore_errors=no)` — one or more
    lines. Extracts the path only."""
    paths: "list[str]" = []
    prefix = "EnvironmentFiles="
    for line in raw.splitlines():
        if not line.startswith(prefix):
            continue
        rest = line[len(prefix):].strip()
        if not rest:
            continue
        m = re.match(r"^(\S+)\s*\(", rest)
        paths.append(m.group(1) if m else rest.split()[0])
    return paths


def _read_env_file_key_names(path: str) -> "set[str]":
    """The KEY names present in an EnvironmentFile= target — never values
    (R-C: 'ONLY managed-key names may ever be printed from it'). Raises
    OSError if the file cannot be read; the caller treats that as a failed
    query (NEW-B)."""
    names: "set[str]" = set()
    text = Path(path).read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, _ = line.partition("=")
        names.add(key.strip())
    return names


def query_gateway_unit(gateway_unit: str) -> UnitQuery:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return UnitQuery("no_systemctl")
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", gateway_unit, "-p", "Environment",
             "-p", "EnvironmentFiles"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as exc:
        return UnitQuery("query_failed", error=f"{type(exc).__name__} running systemctl")
    if proc.returncode != 0:
        return UnitQuery("query_failed",
                          error=f"systemctl show exited {proc.returncode}")
    try:
        environment = _parse_systemctl_environment(proc.stdout)
    except _EnvironmentParseError as exc:
        return UnitQuery("query_failed", error=str(exc))
    owned = set(environment) & set(MANAGED_KEYS)
    for path in _parse_systemctl_environment_files(proc.stdout):
        try:
            owned |= _read_env_file_key_names(path) & set(MANAGED_KEYS)
        except OSError as exc:
            return UnitQuery(
                "query_failed",
                error=f"EnvironmentFile {path!r} unreadable ({type(exc).__name__})")
    return UnitQuery("ok", owned_keys=owned, environment=environment)


# ─────────────────────────────────────────────────────────────────────────
# Faithful environment — PATH/HOME plus whatever the unit itself declares.
# Operator shell exports are EXCLUDED from everything else, so the image
# this tool computes matches what systemd would actually hand the gateway,
# not what happens to be exported in the migrating agent's own shell.
#
# KNOWN FIDELITY GAPS (documented, not fixed here — MED-7 / L-4, fix round):
#   * $CREDENTIALS_DIRECTORY / <KEY>_FILE delivery is INVISIBLE to this
#     reconstruction: systemd injects CREDENTIALS_DIRECTORY at runtime, and
#     it never appears in `systemctl show -p Environment`. A LoadCredential=
#     host's credentialed entry is therefore excluded here and classifies
#     as case 2 (unusable) even though the running gateway resolves it
#     fine. SAFE (case 2 declines and writes nothing) but the report is
#     pessimistic for that population — recorded for W4 rather than
#     discovered on a customer install.
#   * Secret-classified keys delivered via a literal `Environment=` line in
#     the unit (see below) are deliberately EXCLUDED from the loader
#     subprocess's environment too, for the same reason SEC-06(ii) exists
#     everywhere else in this codebase: a value copied wholesale into a
#     child process's environ is visible to that child's own
#     /proc/<pid>/environ. This means `has_token` can read False for a
#     backend whose ONLY credential source is a unit `Environment=` line
#     (never `EnvironmentFile=` or `LoadCredential=`, both unaffected) —
#     an accepted fidelity/security tradeoff (SEC L-4), not a bug.
# ─────────────────────────────────────────────────────────────────────────

def build_faithful_env(unit_query: UnitQuery) -> "dict[str, str]":
    faithful: "dict[str, str]" = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    # SEC L-4 (fix round): config keys only — a secret-classified name in
    # the unit's OWN Environment= line (e.g. a provider key delivered as
    # `Environment=DEEPSEEK_API_KEY=...`) must never reach this (or any)
    # child process's environ, exactly like secure_env's own split-env
    # design (PR A1) prevents everywhere else in this codebase.
    if unit_query.status == "ok":
        for key, value in unit_query.environment.items():
            if not secure_env.is_secret_key(key):
                faithful[key] = value
    # SECURE_ENV_FILE is a mechanism variable, not operator config — honour
    # the unit's own declaration of it first (this is what a real gateway
    # process would see); fall back to the CURRENT process's own value so a
    # standalone/headless/test invocation (no unit at all) still resolves
    # the same env file the operator's shell would.
    if "SECURE_ENV_FILE" not in faithful and "SECURE_ENV_FILE" in os.environ:
        faithful["SECURE_ENV_FILE"] = os.environ["SECURE_ENV_FILE"]
    return faithful


# ─────────────────────────────────────────────────────────────────────────
# The loader subprocess — NEVER in-process (hive_mind_proxy's own loader
# runs once at import and writes module globals). Fresh subprocess per
# image, under the faithful environment above.
# ─────────────────────────────────────────────────────────────────────────

_LOADER_SNIPPET = """
import json, sys
sys.path.insert(0, {here!r})
try:
    import secure_env
    secure_env.load_split_env()
    import hive_mind_proxy as proxy
except Exception as exc:
    print(json.dumps({{"loader_error": type(exc).__name__}}))
    raise SystemExit(0)

def _guard(fn):
    try:
        fn()
        return {{"raised": False, "message": None}}
    except SystemExit as e:
        return {{"raised": True, "message": str(e)}}

env_path = secure_env._select_env_file()
image = {{
    "urls": list(proxy.LLM_BACKENDS),
    "weights": {{u: proxy.LLM_WEIGHTS.get(u) for u in proxy.LLM_BACKENDS}},
    "has_token": {{u: proxy.LLM_BACKEND_TOKENS.get(u) is not None for u in proxy.LLM_BACKENDS}},
    "models": {{u: proxy.LLM_BACKEND_MODELS.get(u) for u in proxy.LLM_BACKENDS}},
    "roles": {{u: (sorted(proxy.LLM_BACKEND_ROLES.get(u)) if proxy.LLM_BACKEND_ROLES.get(u) is not None else None) for u in proxy.LLM_BACKENDS}},
    "n_ctx": {{u: proxy.LLM_BACKEND_NCTX.get(u) for u in proxy.LLM_BACKENDS}},
    "max_inflight": {{u: proxy.LLM_BACKEND_MAX_INFLIGHT.get(u) for u in proxy.LLM_BACKENDS}},
    "extra_body": {{u: proxy.LLM_BACKEND_EXTRAS.get(u) for u in proxy.LLM_BACKENDS}},
    "private_ok": {{u: proxy.LLM_BACKEND_PRIVATE_OK.get(u, False) for u in proxy.LLM_BACKENDS}},
    "private_ok_explicit": {{u: proxy.LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(u, False) for u in proxy.LLM_BACKENDS}},
    "fallback_reason": proxy.LLM_POOL_FALLBACK_REASON,
    "config_empty": proxy.LLM_POOL_CONFIG_EMPTY,
    "default_target": proxy.DEFAULT_TARGET,
    "env_file": (str(env_path) if env_path else None),
    "guard_routing": _guard(proxy.require_valid_llm_routing_config),
    "guard_auth": _guard(proxy.require_auth_when_provider_keys_configured),
    "loader_semantics": {loader_semantics},
}}
print(json.dumps(image))
"""


class LoaderError(Exception):
    pass


def run_loader(faithful_env: "dict[str, str]") -> dict:
    """Runs the gateway's own `_load_llm_backends()` + startup guards in a
    fresh subprocess under `faithful_env`, returns the resulting image dict.
    Raises LoaderError (never a raw traceback to the caller) on any failure
    — an unparseable LLM_BACKENDS_JSON shape (array of strings, which makes
    `import hive_mind_proxy` itself raise), a missing dependency, or a crash
    of any other kind."""
    snippet = _LOADER_SNIPPET.format(here=str(HERE), loader_semantics=LOADER_SEMANTICS)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            env=faithful_env, capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        raise LoaderError(f"could not run the loader subprocess: {type(exc).__name__}") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        raise LoaderError(
            f"loader subprocess exited {proc.returncode} — "
            + _scrub(proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "no output"))
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise LoaderError(f"loader subprocess produced unparseable output: {type(exc).__name__}") from None
    if "loader_error" in data:
        raise LoaderError(f"loader subprocess could not import hive_mind_proxy: {data['loader_error']}")
    return data


# ─────────────────────────────────────────────────────────────────────────
# Raw env-file parsing — the migration's OWN view of the file's lines,
# independent of (and prior to) any loader subprocess. Mirrors secure_env's
# own key=value split exactly: first-wins per key, no quote/comment
# stripping.
# ─────────────────────────────────────────────────────────────────────────

def resolve_env_file(faithful_env: "dict[str, str]") -> "Path | None":
    """Mirrors secure_env._select_env_file() exactly, but against the
    FAITHFUL environment dict rather than this process's own os.environ —
    so a unit-declared SECURE_ENV_FILE is honoured without ever mutating
    this process's real environment."""
    override = faithful_env.get("SECURE_ENV_FILE")
    if override is not None:
        override = override.strip()
        if not override:
            return None
        p = Path(override)
        return p.resolve() if p.exists() else None
    candidates = [HERE.parent / ".env", HERE.parent.parent / ".env"]
    found = next((p for p in candidates if p.exists()), None)
    return found.resolve() if found is not None else None


def read_raw_lines(path: Path) -> "list[str]":
    return path.read_text().splitlines()


def parse_managed_key_lines(lines: "list[str]") -> "dict[str, list[int]]":
    """key -> [line indices] it appears on, in file order. Blank/comment
    lines and anything without '=' are skipped, matching secure_env's own
    parser exactly."""
    out: "dict[str, list[int]]" = {k: [] for k in MANAGED_KEYS}
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in out:
            out[key].append(i)
    return out


def find_duplicate_managed_keys(occurrences: "dict[str, list[int]]") -> "list[str]":
    return sorted(k for k, idxs in occurrences.items() if len(idxs) > 1)


def verify_only_planned_keys_changed(
        pre_lines: "list[str]", post_lines: "list[str]", planned_keys: "set[str]") -> "tuple[bool, str]":
    """SEC M-4 / QA MED-9 (fix round): a FILE-LEVEL verification, re-reading
    the WRITTEN file and diffing every MANAGED_KEYS value against the
    pre-write snapshot — independent of (and in addition to) the semantic
    two_layer_compare(), which only covers the LLM pool. Without this, a
    wrong EMBEDDER_URL/RERANKER_URL write (or any other managed-key value
    this tool did not intend to touch) would pass the behavioural check
    green, because the pool image never carries either field. Returns
    (ok, message); any key outside `planned_keys` whose value moved is a
    failure — this is what "assert exactly the planned keys changed"
    means in code."""
    pre_occ = parse_managed_key_lines(pre_lines)
    post_occ = parse_managed_key_lines(post_lines)
    for key in MANAGED_KEYS:
        if key in planned_keys:
            continue
        pre_val = _line_value(pre_lines, pre_occ[key][0]) if pre_occ.get(key) else None
        post_val = _line_value(post_lines, post_occ[key][0]) if post_occ.get(key) else None
        if pre_val != post_val:
            return False, (f"unplanned change to {key} (not in this run's planned "
                            f"moves): {pre_val!r} -> {post_val!r}")
    return True, "ok"


def _line_value(lines: "list[str]", idx: int) -> str:
    _, _, val = lines[idx].strip().partition("=")
    return val.strip()


# ─────────────────────────────────────────────────────────────────────────
# The decision ladder (strict order, FIRST MATCH ONLY, evaluated on the
# PRE-IMAGE + the raw file's own managed-key occurrences).
# ─────────────────────────────────────────────────────────────────────────

CASE_JSON_PRESENT_EMPTY = 0
CASE_JSON_USABLE = 1
CASE_JSON_UNUSABLE = 2
CASE_CSV_LIVE = 3
CASE_FALLBACK = 4


def classify(image: dict, occurrences: "dict[str, list[int]]", lines: "list[str]") -> int:
    json_idxs = occurrences.get("LLM_BACKENDS_JSON") or []
    if json_idxs and not _line_value(lines, json_idxs[0]):
        return CASE_JSON_PRESENT_EMPTY
    # QA H2 (fix round): case 2 requires the KEY to actually be PRESENT in
    # the file, not merely `image["fallback_reason"]` being truthy — a real
    # loader run can only ever set fallback_reason from inside its own
    # `if raw_json:` branch (hive_mind_proxy.py), so this is defence in
    # depth for any caller (a test, a future refactor) that hands classify()
    # an inconsistent image. It is also what makes an end-to-end 'delete the
    # JSON line from the fixture' mutation actually change the outcome —
    # without `json_idxs and` here, a stale/mocked fallback_reason with no
    # JSON key present would still misreport case 2.
    if json_idxs and image.get("fallback_reason"):
        return CASE_JSON_UNUSABLE
    if json_idxs and image.get("urls"):
        return CASE_JSON_USABLE
    if occurrences.get("LLM_BACKENDS") and _line_value(lines, occurrences["LLM_BACKENDS"][0]):
        return CASE_CSV_LIVE
    return CASE_FALLBACK


def _load_raw_backend_entries(raw_json_text: str) -> "list[dict] | None":
    """The migration's OWN parse of LLM_BACKENDS_JSON for the case-1
    mutation. Returns None (never raises) on anything that is not a JSON
    array of objects — an array of bare strings included (Loader-shape
    tolerance) — so the caller can fall back to 'touch nothing, report'."""
    try:
        entries = json.loads(raw_json_text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(entries, list):
        return None
    if not all(isinstance(e, dict) for e in entries):
        return None
    return entries


def plan_case_json_usable(image: dict, raw_json_text: str) -> "tuple[list[dict] | None, list[str]]":
    """Returns (new_entries_or_None, touched_urls). new_entries is None when
    the raw shape cannot be safely mutated (graceful refusal) — the caller
    then treats this exactly like case 2 (touch nothing, report why)."""
    entries = _load_raw_backend_entries(raw_json_text)
    if entries is None:
        return None, []
    touched: "list[str]" = []
    new_entries = []
    for entry in entries:
        url = str(entry.get("url", "")).rstrip("/")
        eligible = (
            url
            and "token_env" not in entry
            and "roles" not in entry
            and "private_ok" not in entry
            and image.get("private_ok_explicit", {}).get(url) is False
            and not image.get("has_token", {}).get(url)
        )
        if eligible:
            entry = dict(entry)
            entry["private_ok"] = True
            touched.append(url)
        new_entries.append(entry)
    return new_entries, touched


def plan_case_csv_live(image: dict, csv_value: str) -> "list[dict]":
    """Converts a live comma-form LLM_BACKENDS line into the JSON shape —
    the MEASURED effective value (hive_mind_proxy.py's own CSV parse:
    url/weight only, never roles/token_env, which _parse_backend() cannot
    produce)."""
    out = []
    for entry in csv_value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        url, _, w = entry.partition("@")
        url = url.strip().rstrip("/")
        try:
            weight = float(w) if w else 1.0
        except ValueError:
            weight = 1.0
        out.append({"url": url, "weight": max(weight, 0.1), "private_ok": True})
    return out


# ─────────────────────────────────────────────────────────────────────────
# Interactive confirm (case 4, R-A) — probe is advisory only; a human
# confirms every materialisation.
# ─────────────────────────────────────────────────────────────────────────

def _v1_models_probe_url(base: str) -> str:
    base = base.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disables following a redirect (SEC M-5): the probe's only job is a
    liveness/shape check on the EXACT configured URL — following a 3xx
    could land it (silently) on a different host than the one the operator
    is about to confirm. Returning None from redirect_request() makes the
    opener treat the redirect response itself as the final response rather
    than chasing it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_backend(url: str, timeout: float = 3.0) -> dict:
    """{'answered': bool, 'status': int|None, 'parsed_model_list': bool,
    'n_models': int|None}. Unauthenticated GET, proxies disabled, redirects
    NOT followed, response body size-capped at 65536 bytes — only
    `status`/`n_models` (both ints) ever reach the interactive question,
    never raw response bytes. Never raises.

    ONE WALL-CLOCK DEADLINE covering DNS + connect + response (fix round,
    QA H3 / SEC M-5 — the ORIGINAL cut passed `timeout` straight to
    `opener.open()`, which is a per-SOCKET-OPERATION bound: it does not
    cover `socket.getaddrinfo()` at all (a black-holed resolver can hold
    the call for its own OS-level DNS timeout, commonly far longer than
    `timeout`), and it re-arms on every individual `recv()` inside
    `resp.read()` — a server dribbling one byte every `timeout - epsilon`
    seconds could hold the read open indefinitely without ever tripping
    it. Both gaps are closed the same way: the ENTIRE network operation
    (resolve, connect, read) runs in a daemon thread, and this function
    returns based on `Thread.join(timeout)` — a probe that has not
    finished within `timeout` wall-clock seconds is reported exactly as
    'did not answer', full stop, regardless of what phase it was in. The
    thread past its deadline is ABANDONED, not CANCELLED — stdlib has no
    clean way to interrupt a blocking socket call, so it keeps running and
    keeps HOLDING ITS SOCKET open until whatever it is doing (DNS, connect,
    or read) eventually finishes or errors on its own; it is daemonized
    only so it can never block this PROCESS's own exit. An accepted,
    bounded cost (at most one lingering thread — and one lingering socket
    — per confirm), not a resource leak across runs."""
    result = {"answered": False, "status": None, "parsed_model_list": False, "n_models": None}

    def _do_probe() -> None:
        probe_url = _v1_models_probe_url(url)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
        req = urllib.request.Request(probe_url, method="GET")
        try:
            # Belt-and-braces: a per-socket-operation timeout too, so the
            # ordinary "nothing answers at all" case still tends to end
            # this thread promptly — the join() deadline above is the
            # real, load-bearing bound either way.
            with opener.open(req, timeout=timeout) as resp:
                status = resp.getcode()
                body = resp.read(65536)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = exc.read(65536)
            except Exception:
                body = b""
        except Exception:
            return
        parsed, n_models = False, None
        try:
            obj = json.loads(body.decode("utf-8", errors="replace"))
            if isinstance(obj, dict) and isinstance(obj.get("data"), list):
                parsed = True
                n_models = len(obj["data"])
        except Exception:
            pass
        result.update(answered=True, status=status, parsed_model_list=parsed, n_models=n_models)

    t = threading.Thread(target=_do_probe, daemon=True)
    t.start()
    t.join(timeout)
    # A snapshot, taken NOW: if the thread is still running past the
    # deadline it may still mutate `result` afterward — returning a copy
    # rather than the live dict means that later mutation can never reach
    # a caller that already moved on.
    return dict(result)


def _effective_url_probeable(url: str) -> bool:
    if not url or not url.strip():
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except Exception:
        return False
    return bool(parts.scheme and parts.hostname)


def build_confirm_question(url: str, probe: "dict | None") -> str:
    scrubbed = _scrub(url)
    # QA MED-4 (fix round): `probe is None` and `probe["answered"] is False`
    # are DIFFERENT states — the first means this call never ran a probe at
    # all (a non-interactive dry-run preview, which does not have a live
    # operator to ask), the second means a probe genuinely ran and got no
    # response. Conflating them as the same "did not answer" wording stated
    # an observation that was never made.
    if probe is None:
        observation = "not probed — this preview is non-interactive"
    elif not probe["answered"]:
        observation = "did not answer"
    elif probe["parsed_model_list"]:
        observation = f"HTTP {probe['status']}, response looks like an OpenAI model list ({probe['n_models']} models)"
    elif probe["status"] == 401:
        observation = f"HTTP {probe['status']} — this server wants a credential the gateway does not have"
    else:
        observation = f"HTTP {probe['status']}, response body does not look like a model list"
    return (
        f"No LLM backend is declared — the gateway is currently falling back to "
        f"{scrubbed}. A liveness probe answered: {observation}.\n"
        f"Declare this as the explicit backend (LLM_BACKENDS_JSON=[{{\"url\": {scrubbed!r}, "
        f"\"weight\": 1, \"private_ok\": true}}])? [y/N] "
    )


def read_confirm(deadline: float = 60.0, reader=None) -> bool:
    """True only on an explicit 'y' (case-insensitive) read before the
    deadline. Deadline, EOF, or anything else = No (N-6 — an unanswered
    access question never widens; here, never writes). `reader` is an
    injection seam for tests (a callable taking no args, returning a line
    or '' for EOF) — production passes None and this selects on real stdin."""
    if reader is not None:
        try:
            line = reader()
        except Exception:
            return False
        return line.strip().lower() == "y"
    try:
        ready, _, _ = select.select([sys.stdin], [], [], deadline)
    except Exception:
        return False
    if not ready:
        return False
    line = sys.stdin.readline()
    return line.strip().lower() == "y"


# ─────────────────────────────────────────────────────────────────────────
# File mechanics
# ─────────────────────────────────────────────────────────────────────────

def _detect_crlf(path: Path) -> bool:
    """Reads RAW BYTES — Path.read_text()/open(..., 'r') perform universal-
    newline translation (\\r\\n silently becomes \\n on read), which would
    make this always report False regardless of the file's real line
    endings."""
    return b"\r\n" in path.read_bytes()


def backup_env_file(path: Path) -> Path:
    backup_dir = Path(os.path.expanduser("~/.shared-memory/env-backups"))
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    backup_path = backup_dir / f".env.pre-migration-{_utc_stamp()}"
    data = path.read_bytes()
    fd = os.open(backup_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(backup_path, 0o600)
    return backup_path


def atomic_write(target: Path, new_text: str) -> None:
    """Complete new file in a temp file BESIDE target (same filesystem —
    rename cannot cross one), one atomic rename. Temp removed on every exit
    path.

    SEC L-5 (fix round) — SEQUENCING IS THE FIX, already correct as shipped,
    documented explicitly so a future edit does not reorder it by accident:
    `tempfile.mkstemp()` creates its file at 0600 BY DEFAULT (no `mode=`
    override here), so the temp file is 0600 for the ENTIRE time it holds
    plaintext content (which may include raw credentialed URLs, per ruled
    design). Only AFTER the content is fully written does `chmod --reference`
    semantics widen it to the original file's mode — immediately before the
    rename that makes it live. There is no window where a wider-than-0600
    file holds the new content under its temporary name."""
    orig_mode = target.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(prefix=".env.migrate_env.", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(new_text)
        os.chmod(tmp_path, stat.S_IMODE(orig_mode))  # widen, THEN...
        os.replace(tmp_path, target)                 # ...rename, immediately after
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


class RestoreError(Exception):
    """The loudest failure this tool has: a post-migration restore could
    not be verified byte-for-byte. Distinct from LoaderError (SEC M-2 /
    QA MED-5 — reusing LoaderError for a restore fault was a misnomer, and
    both call sites now wrap this so it never surfaces as a raw
    traceback)."""


def restore_from_backup(target: Path, backup_path: Path) -> None:
    """`cat backup > target` semantics (truncate-in-place, preserving
    inode/symlink), THEN re-read and byte-compare — an ENOSPC/EIO during
    recovery must be detected, never assumed away (M-D9). Raises
    RestoreError on a failed verify; an OSError from the read/write
    themselves propagates as-is — both are caught by the caller's own
    wrapper (`_restore_or_die()`), never left to reach main() as a raw
    traceback."""
    data = backup_path.read_bytes()
    with open(target, "wb") as fh:
        fh.write(data)
    if target.read_bytes() != data:
        raise RestoreError(
            f"RESTORE VERIFICATION FAILED after post-image divergence — "
            f"{target} does not match its own backup at {backup_path} after "
            f"the restore write. This is the loudest failure this tool has: "
            f"do not trust {target}; recover it by hand from {backup_path}.")


def _restore_or_die(lines: "list[str]", env_path: Path, backup_path: Path, reason_msg: str) -> int:
    """SEC M-2 / QA MED-5 (fix round): the ONE place that calls
    restore_from_backup(), so the failure handling exists exactly once.
    Prints the PENDING `lines` (which carry the divergence diagnosis
    computed so far) FIRST regardless of whether the restore itself
    succeeds or fails — losing that diagnosis at the exact moment the
    operator needs it was the defect. A restore failure (RestoreError, or
    a bare OSError from the read/write) is caught here and rendered as a
    clean, named message — never a Python traceback — always returns
    EXIT_STOP."""
    _report(lines, reason_msg)
    try:
        restore_from_backup(env_path, backup_path)
    except Exception as exc:
        print("\n".join(lines))
        print(f"RESTORE FAILED ({type(exc).__name__}): {_scrub(str(exc))}")
        print(f"do NOT trust {env_path} — it may be PARTIALLY WRITTEN. Recover it "
              f"by hand from {backup_path}, then investigate before re-running.")
        return EXIT_STOP
    print("\n".join(lines))
    return EXIT_STOP


# ─────────────────────────────────────────────────────────────────────────
# Two-layer comparison (the property test)
# ─────────────────────────────────────────────────────────────────────────

_LAYER1_PER_URL_FIELDS = ("weights", "has_token", "models", "roles", "n_ctx",
                           "max_inflight", "extra_body")
# Ruling A(a) (§6.4): `private_ok` moved OUT of Layer 1 (must-be-EQUAL) and
# into the Layer-2 planned-direction set below, alongside
# `private_ok_explicit` — a loader-semantics generation bump (W4
# default-deny) can legitimately move it True->False on an entry this run
# left untouched, which plain equality would misreport as a regression.
# Still url-keyed for normalisation purposes (SEC H-4), so both stay listed
# here even though only the first tuple is asserted EQUAL below.
_URL_KEYED_FIELDS = _LAYER1_PER_URL_FIELDS + ("private_ok", "private_ok_explicit")


def _normalize_url_key(url: str) -> str:
    return url.rstrip("/")


def _normalize_image(image: dict) -> dict:
    """SEC H-4 (fix round): rewrites every url-keyed field to a trailing-
    slash-insensitive key. hive_mind_proxy's own loader is ASYMMETRIC
    (out of this tool's scope to change): the case-4 fallback substitution
    (`_raw_backends = [(DEFAULT_TARGET, 1.0)]`) never strips a trailing
    slash, while its LLM_BACKENDS_JSON parsing branch always does
    (`str(entry.get("url","")).rstrip("/")`). Without normalising here, a
    bare `LLM_DEFAULT_TARGET` carrying a trailing slash makes a correctly
    CONFIRMED case-4 write look like a backend-URL-set change — the
    'y' the operator just typed then aborts the whole upgrade on nothing
    but a slash (reproduced live; this repo shipped v0.9.76 specifically
    over a trailing-slash hazard). Applied to BOTH images before any
    comparison, so no ordering of the fix matters."""
    out = dict(image)
    out["urls"] = [_normalize_url_key(u) for u in image.get("urls", [])]
    for field in _URL_KEYED_FIELDS:
        d = image.get(field)
        if isinstance(d, dict):
            out[field] = {_normalize_url_key(k): v for k, v in d.items()}
    return out


def two_layer_compare(pre: dict, post: dict, reported_writes: "set[str]",
                       report_lines: "list[str] | None" = None) -> "tuple[bool, str]":
    """Layer 1 (behavioural invariants) must be EQUAL. Layer 2
    (declaration-status fields) may move ONLY in the planned direction on
    ONLY the reported entries. `report_lines`, when given, receives one
    informational line per ALLOWED boundary-crossing private_ok movement
    (Ruling A(a): "reported per-entry by name — never silent") — it never
    affects the (ok, message) verdict. Returns (ok, message)."""
    pre = _normalize_image(pre)
    post = _normalize_image(post)
    reported_writes = {_normalize_url_key(w) for w in reported_writes}
    boundary_crossed = pre.get("loader_semantics", 1) < post.get("loader_semantics", 1)
    if sorted(pre.get("urls", [])) != sorted(post.get("urls", [])):
        return False, f"backend URL set changed: {sorted(pre.get('urls', []))} -> {sorted(post.get('urls', []))}"
    for field in _LAYER1_PER_URL_FIELDS:
        pre_field, post_field = pre.get(field, {}), post.get(field, {})
        for url in pre.get("urls", []):
            if pre_field.get(url) != post_field.get(url):
                return False, (f"behavioural field {field!r} changed for "
                                f"{_scrub(url)}: {pre_field.get(url)!r} -> {post_field.get(url)!r}")
    if bool(pre.get("fallback_reason")) != bool(post.get("fallback_reason")):
        return False, "LLM_POOL_FALLBACK_REASON class changed"
    for guard in ("guard_routing", "guard_auth"):
        if pre.get(guard, {}).get("raised") != post.get(guard, {}).get("raised"):
            return False, f"{guard} verdict changed"
        if (pre.get(guard, {}).get("raised") and post.get(guard, {}).get("raised")
                and pre[guard]["message"] != post[guard]["message"]):
            return False, f"{guard} refusal message changed"

    pre_explicit = pre.get("private_ok_explicit", {})
    post_explicit = post.get("private_ok_explicit", {})
    for url in pre.get("urls", []):
        moved = pre_explicit.get(url) != post_explicit.get(url)
        if moved and not (pre_explicit.get(url) is False and post_explicit.get(url) is True
                           and url in reported_writes):
            return False, (f"private_ok_explicit moved for {_scrub(url)} "
                            f"({pre_explicit.get(url)} -> {post_explicit.get(url)}) "
                            f"but that entry was not among the reported writes")

    # Ruling A(a) (§6.4): private_ok direction, Layer 2. Two, and only two,
    # movements are legitimate:
    #   rule 1 — False->True on an entry THIS RUN reported writing (every
    #            write materialises true: :504/:526/:1144 — presence changes
    #            are caught by the url-set equality above; a written entry
    #            never goes True->False, so that direction is unreachable).
    #   rule 2 — True->False on an entry the run LEFT UNTOUCHED, but ONLY
    #            when the comparison crosses the loader-semantics boundary
    #            (an old-code pre-image recomputed under new-code semantics
    #            — exactly what a version-jump self-capture-free --preimage
    #            run measures). Reported per-entry by name, never silent.
    # Anything else (None<->bool, True->False within the same generation,
    # False->True on an unreported entry) is a REAL regression — refuse.
    pre_private = pre.get("private_ok", {})
    post_private = post.get("private_ok", {})
    for url in pre.get("urls", []):
        pv, qv = pre_private.get(url), post_private.get(url)
        if pv == qv:
            continue
        if pv is False and qv is True and url in reported_writes:
            continue
        if pv is True and qv is False and boundary_crossed and url not in reported_writes:
            if report_lines is not None:
                report_lines.append(
                    f"private_ok for {_scrub(url)} moved True -> False across the "
                    f"loader-semantics boundary (untouched this run) — the "
                    f"pre-W4 effective value is no longer the default; declare "
                    f"it explicitly if it must be preserved.")
            continue
        return False, (f"private_ok moved for {_scrub(url)} ({pv!r} -> {qv!r}) "
                        f"outside the two ruled directions (A(a)) — refusing "
                        f"rather than risk a silent eligibility regression")

    pre_empty, post_empty = bool(pre.get("config_empty")), bool(post.get("config_empty"))
    if pre_empty != post_empty:
        if not (pre_empty and not post_empty and "__fallback_materialised__" in reported_writes):
            return False, "LLM_POOL_CONFIG_EMPTY moved without a confirmed case-4 write"

    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────
# Report lines — plain text, always scrubbed.
# ─────────────────────────────────────────────────────────────────────────

def _report(lines: "list[str]", text: str) -> None:
    """Appends `text` as-is. Deliberately NOT a second scrub pass: every
    call site below scrubs its OWN raw URL value(s) before embedding them
    (the check_config.py discipline). Re-scrubbing an already-composed,
    punctuation-adjacent message is actively harmful — the scrub regex is
    greedy on non-whitespace, so a URL immediately followed by a quote,
    comma or period (as in a rendered JSON example) gets swept into the
    same match and can trip scrub_url_credentials' own exception fallback
    (a trailing '.' turns 'PORT.' into an unparsable port, which raises
    inside its .port property access and redacts the whole match) —
    measured while building this script."""
    lines.append(text)


# ─────────────────────────────────────────────────────────────────────────
# Top-level: capture
# ─────────────────────────────────────────────────────────────────────────

def _open_fresh_secure_file(path: Path, mode: int = 0o600) -> int:
    """Opens `path` for writing, returning a fd this call is GUARANTEED to
    have freshly created (SEC M-1, fix round). `O_EXCL` closes the
    create-time TOCTOU (a pre-planted file at this exact path is never
    reused — the open FAILS instead); `O_NOFOLLOW` refuses a symlink at
    the final path component outright rather than following it. A
    pre-existing entry at `path` (the ordinary standalone re-run-to-the-
    same-path case) is unlinked first — `unlink()` removes the directory
    LINK, never anything a symlink might point at, so this is safe even if
    that pre-existing entry were itself a symlink planted by another
    actor. Permissions are set via `os.fchmod` on the open descriptor
    (TOCTOU-safe — no window between "created" and "permissions applied"),
    matching `log_hygiene.secure_path()`'s own approach elsewhere in this
    codebase."""
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except OSError:
        pass
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    os.fchmod(fd, mode)
    return fd


def do_capture(out_path: str, gateway_unit: str) -> int:
    lines: "list[str]" = []
    unit_query = query_gateway_unit(gateway_unit)
    if unit_query.status == "query_failed":
        _report(lines, f"CAPTURE FAILED — could not determine what {gateway_unit} "
                        f"owns ({unit_query.error}); a wrong baseline would silently "
                        f"invalidate the pre/post equality check. Re-run standalone "
                        f"once systemctl is reachable, or pass --skip-env-migration.")
        print("\n".join(lines))
        return EXIT_STOP
    faithful = build_faithful_env(unit_query)
    try:
        image = run_loader(faithful)
    except LoaderError as exc:
        _report(lines, f"CAPTURE FAILED — {exc}")
        print("\n".join(lines))
        return EXIT_STOP
    captured_by = os.environ.get("SM_PRE_UPDATE_VERSION") or "self-capture (standalone)"
    payload = {"capture_schema": CAPTURE_SCHEMA, "captured_by": captured_by, "image": image}
    out = Path(out_path)
    # SEC M-1 (fix round): only the ancestors THIS call actually creates go
    # to 0700 (log_hygiene's own helper, reused rather than duplicated) —
    # the update_framework.sh path already gets a 0700 mktemp -d parent,
    # this matters for the documented standalone/operator-chosen-path mode.
    _chmod_created_ancestors(out.parent)
    fd = _open_fresh_secure_file(out, 0o600)
    try:
        os.write(fd, json.dumps(payload).encode("utf-8"))
    finally:
        os.close(fd)
    _report(lines, f"captured pre-image: {len(image.get('urls', []))} backend(s), "
                    f"config_empty={image.get('config_empty')}, "
                    f"fallback={'yes' if image.get('fallback_reason') else 'no'} -> {out_path}")
    print("\n".join(lines))
    return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────
# Top-level: apply / dry-run
# ─────────────────────────────────────────────────────────────────────────

def do_apply_or_dryrun(preimage_path: "str | None", apply: bool, gateway_unit: str,
                        confirm_reader=None) -> int:
    lines: "list[str]" = []
    mode = "APPLY" if apply else "DRY RUN"

    unit_query = query_gateway_unit(gateway_unit)
    faithful = build_faithful_env(unit_query)

    # Pre-image: from --preimage, or self-captured now (N-9).
    if preimage_path:
        try:
            payload = json.loads(Path(preimage_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _report(lines, f"could not read --preimage {preimage_path}: {type(exc).__name__}")
            print("\n".join(lines))
            return EXIT_STOP
        schema = payload.get("capture_schema")
        if not isinstance(schema, int) or schema > CAPTURE_SCHEMA:
            _report(lines, f"--preimage capture_schema={schema!r} is NEWER than this "
                            f"script understands ({CAPTURE_SCHEMA}) — re-run "
                            f"'migrate_env.py --apply' with no --preimage to self-capture "
                            f"with the CURRENT loader instead.")
            print("\n".join(lines))
            return EXIT_STOP
        pre_image = payload.get("image", {})
    else:
        if unit_query.status == "query_failed":
            _report(lines, f"could not self-capture — {unit_query.error}; declining every "
                            f"write this run (see below).")
            pre_image = None
        else:
            try:
                pre_image = run_loader(faithful)
            except LoaderError as exc:
                _report(lines, f"self-capture failed — {exc}")
                print("\n".join(lines))
                return EXIT_STOP

    if unit_query.status == "query_failed":
        _report(lines, f"[{mode}] a systemd unit ({gateway_unit}) MAY own one or more "
                        f"managed keys and its own configuration could not be read "
                        f"({unit_query.error}) — every write below DECLINES rather than "
                        f"risk writing a key the unit shadows. Fix systemctl reachability "
                        f"or the named EnvironmentFile and re-run.")
        print("\n".join(lines))
        return EXIT_OK

    env_path = resolve_env_file(faithful)
    if env_path is None:
        _report(lines, "no shared-memory/.env found — environment-only deployment; "
                        "nothing to migrate.")
        print("\n".join(lines))
        return EXIT_OK

    try:
        raw_lines = read_raw_lines(env_path)
    except UnicodeDecodeError as exc:
        _report(lines, f"REFUSING — {env_path} could not be decoded as text "
                        f"({type(exc).__name__}) — this tool only ever edits a plain-text "
                        f".env; fix the file's encoding and re-run.")
        print("\n".join(lines))
        return EXIT_STOP
    original_raw_lines = list(raw_lines)  # SEC M-4 / QA MED-9: the pre-write snapshot,
    # taken BEFORE any case mutates `raw_lines` in place — the file-level
    # verification below diffs the WRITTEN file against exactly this.
    occurrences = parse_managed_key_lines(raw_lines)
    dupes = find_duplicate_managed_keys(occurrences)
    if dupes:
        _report(lines, f"REFUSING — duplicate managed key(s) in {env_path}: "
                        f"{', '.join(dupes)}. Config parse is first-wins for managed "
                        f"keys, so which copy you meant is not guessable. Remove the "
                        f"extra line(s) and re-run.")
        print("\n".join(lines))
        return EXIT_STOP

    for key in MANAGED_KEYS:
        if key in unit_query.owned_keys:
            _report(lines, f"'{key}' is configured in the systemd unit ({gateway_unit}) — "
                            f"migrate it by hand there; never written here.")

    reported_writes: "set[str]" = set()       # URL-keyed, feeds two_layer_compare
    planned_key_names: "set[str]" = set()     # MANAGED_KEYS names, feeds the file-level check
    planned_new_lines: "list[str] | None" = None
    appended: "list[str]" = []

    # ── Endpoint keys: absent -> write the framework default; present
    # (even empty) -> untouched, counted, reported. ─────────────────────
    for key in ("EMBEDDER_URL", "RERANKER_URL"):
        if key in unit_query.owned_keys:
            continue
        if not occurrences.get(key):
            default = FRAMEWORK_DEFAULTS[key]["default"]
            appended.append(f"{key}={default}")
            planned_key_names.add(key)
            _report(lines, f"{key} absent — will default to {default} (framework default; "
                            f"decision:1032).")
        else:
            val = _line_value(raw_lines, occurrences[key][0])
            if not val:
                _report(lines, f"{key} present but EMPTY — left untouched (W4 census item).")

    backend_ok_to_touch = "LLM_BACKENDS_JSON" not in unit_query.owned_keys

    if not backend_ok_to_touch:
        _report(lines, "LLM_BACKENDS_JSON is unit-owned — the entire backend half "
                        "declines (endpoint-key changes above still apply).")
        case = None
    else:
        # pre_image is never None here: the only path that sets it None
        # (unit_query.status == "query_failed") already returned above.
        case = classify(pre_image, occurrences, raw_lines)

    # V2 (§6.4, obligation 4): the same-generation planning gate. Cases 1, 3
    # and 4's eligibility predicates are semantics-blind — every condition
    # they check still holds post-W4 for a deliberately-undeclared entry, so
    # an ungated run would PLAN a write on an install that was never
    # upgrading from anything (self-capture, no --preimage: pre and post are
    # both computed by the CURRENT loader). Without a semantics boundary to
    # cross there is no prior effective behaviour to preserve, so any such
    # write would be an OPINION ("turn this on"), not a materialisation —
    # outside this tool's job. Only a genuine version-jump (a captured
    # pre-image from OLD code, carrying no "loader_semantics" key or an
    # older one) opens the gate.
    boundary_open = bool(pre_image) and pre_image.get("loader_semantics", 1) < LOADER_SEMANTICS

    if case == CASE_JSON_PRESENT_EMPTY:
        _report(lines, "LLM_BACKENDS_JSON is present but EMPTY in the file — never "
                        "written to, reported by name, counted for W4. This install "
                        "faces the same 'no backend declared' outcome W4 will apply — "
                        "see GET /health.")
    elif case == CASE_JSON_UNUSABLE:
        _report(lines, f"LLM_BACKENDS_JSON is present but unusable — "
                        f"{_scrub(pre_image.get('fallback_reason') or '')}. Touching "
                        f"nothing (JSON key, CSV key, and the pool half all left as-is).")
    elif case == CASE_JSON_USABLE:
        if not boundary_open:
            _report(lines, "LLM_BACKENDS_JSON already reflects the CURRENT loader "
                            "generation — no boundary to cross, so adding "
                            "\"private_ok\": true now would be an OPINION, not a "
                            "preserved behaviour. Declare private_ok/roles yourself "
                            "for any entry you want role-less-eligible.")
        else:
            json_idx = occurrences["LLM_BACKENDS_JSON"][0]
            raw_json_text = _line_value(raw_lines, json_idx)
            new_entries, touched = plan_case_json_usable(pre_image, raw_json_text)
            if new_entries is None:
                # QA MED-3 (fix round): an unparseable shape means "cannot
                # safely mutate — touch NOTHING", full stop. The report and the
                # action must agree — falling through to the CSV cleanup below
                # would write while claiming not to.
                _report(lines, "LLM_BACKENDS_JSON does not parse as a JSON array of objects — "
                                "cannot safely add private_ok — touching nothing "
                                "(the live CSV line, if any, is also left untouched).")
            else:
                if not touched:
                    _report(lines, "LLM_BACKENDS_JSON already fully explicit — nothing to do.")
                else:
                    new_json = json.dumps(new_entries)
                    raw_lines[json_idx] = f"LLM_BACKENDS_JSON={new_json}"
                    reported_writes |= set(touched)
                    planned_key_names.add("LLM_BACKENDS_JSON")
                    planned_new_lines = raw_lines
                    _report(lines, f"LLM_BACKENDS_JSON: adding \"private_ok\": true to "
                                    f"{len(touched)} entr{'y' if len(touched) == 1 else 'ies'}: "
                                    f"{', '.join(_scrub(u) for u in touched)}.")
                csv_idxs = occurrences.get("LLM_BACKENDS") or []
                if csv_idxs and _line_value(raw_lines, csv_idxs[0]):
                    orig = raw_lines[csv_idxs[0]]
                    raw_lines[csv_idxs[0]] = (
                        f"# migrated to LLM_BACKENDS_JSON by migrate_env.py {_utc_stamp()} "
                        f"— original: {orig}")
                    planned_key_names.add("LLM_BACKENDS")
                    planned_new_lines = raw_lines
                    _report(lines, "a live LLM_BACKENDS (CSV) line is provably dead under "
                                    "LLM_BACKENDS_JSON — commented out with provenance, never deleted.")
    elif case == CASE_CSV_LIVE:
        if not boundary_open:
            _report(lines, "LLM_BACKENDS (CSV) is live but already reflects the CURRENT "
                            "loader generation — its role-less traffic is undeclared and "
                            "correctly serves nothing under default-deny; converting it to "
                            "JSON now would OPINE it into private_ok=true rather than "
                            "preserve prior behaviour. Declare LLM_BACKENDS_JSON yourself "
                            "if you want this fleet eligible.")
        else:
            csv_idx = occurrences["LLM_BACKENDS"][0]
            csv_value = _line_value(raw_lines, csv_idx)
            new_entries = plan_case_csv_live(pre_image, csv_value)
            new_json = json.dumps(new_entries)
            orig = raw_lines[csv_idx]
            raw_lines[csv_idx] = (
                f"# migrated to LLM_BACKENDS_JSON by migrate_env.py {_utc_stamp()} "
                f"— original: {orig}")
            appended.append(f"LLM_BACKENDS_JSON={new_json}")
            reported_writes |= {e["url"] for e in new_entries}
            planned_key_names |= {"LLM_BACKENDS", "LLM_BACKENDS_JSON"}
            planned_new_lines = raw_lines
            _report(lines, f"LLM_BACKENDS (CSV) is live and the only declared pool — "
                            f"converting {len(new_entries)} entr{'y' if len(new_entries) == 1 else 'ies'} "
                            f"to LLM_BACKENDS_JSON (effective private_ok=true), CSV commented out "
                            f"with provenance.")
    elif case == CASE_FALLBACK:
        default_target = pre_image.get("default_target", "")
        if not boundary_open:
            shown = _scrub(default_target) if default_target else "(empty)"
            _report(lines, f"no backend declared — the effective fallback target "
                            f"({shown}) is already the CURRENT loader generation, "
                            f"carrying no pre-W4 grandfathering to preserve — "
                            f"migrate_env only materialises PRE-W4 EFFECTIVE state, "
                            f"never a fresh opinion. Declare LLM_BACKENDS_JSON yourself "
                            f"(see check_config.py / GET /health).")
        elif not _effective_url_probeable(default_target):
            shown = _scrub(default_target) if default_target else "(empty)"
            _report(lines, f"no backend declared, and the effective fallback target "
                            f"({shown}) is empty or unparseable — never probed, "
                            f"never materialised. Set LLM_BACKENDS_JSON yourself, or fix "
                            f"LLM_DEFAULT_TARGET, then re-run.")
        elif not apply:
            probe = probe_backend(default_target) if sys.stdin.isatty() or confirm_reader is not None else None
            question = build_confirm_question(default_target, probe)
            _report(lines, f"no backend declared — falling back to {_scrub(default_target)}. "
                            f"A real run would ask:\n  {question.strip()}\n"
                            f"DRY RUN — no write happens without an interactive 'y'.")
        elif sys.stdin.isatty() or confirm_reader is not None:
            probe = probe_backend(default_target)
            question = build_confirm_question(default_target, probe)
            sys.stdout.write("\n".join(lines) + ("\n" if lines else "") + question)
            sys.stdout.flush()
            answered_yes = read_confirm(reader=confirm_reader)
            lines = []
            if answered_yes:
                new_entries = [{"url": default_target.rstrip("/"), "weight": 1, "private_ok": True}]
                appended.append(f"LLM_BACKENDS_JSON={json.dumps(new_entries)}")
                reported_writes.add("__fallback_materialised__")
                reported_writes.add(default_target)
                planned_key_names.add("LLM_BACKENDS_JSON")
                planned_new_lines = raw_lines
                _report(lines, f"confirmed — LLM_BACKENDS_JSON will declare {_scrub(default_target)} "
                                f"explicitly. LLM_DEFAULT_TARGET is now frozen: changing it no "
                                f"longer moves the pool.")
            else:
                _report(lines, "not confirmed — nothing written. You will be asked again at "
                                "each upgrade until you declare a backend (or set "
                                "LLM_BACKENDS_JSON yourself) — the repetition is designed, not "
                                "forgetfulness.")
        else:
            # Ruling D(a) V1 (§6.4): boundary crossing + apply + no human
            # present + a real materialisation is NEEDED to preserve this
            # pre-W4 install's effective behaviour (its fallback WAS serving
            # role-less traffic) — STOP rather than exit 0 into a gateway
            # that now silently serves nothing. R-A is preserved exactly:
            # still no write without a human.
            _report(lines, f"no backend declared — falling back to {_scrub(default_target)}, "
                            f"and this is an upgrade from a pre-default-deny install where "
                            f"that fallback WAS serving role-less traffic implicitly. A "
                            f"materialisation is needed to preserve it and no human is "
                            f"present to confirm one (non-interactive). Refusing rather than "
                            f"silently leaving it dark — re-run interactively, declare "
                            f"LLM_BACKENDS_JSON yourself, or pass --skip-env-migration to "
                            f"proceed without migrating.")
            print("\n".join(lines))
            return EXIT_STOP

    has_writes = bool(appended) or (planned_new_lines is not None)

    if not apply:
        if has_writes:
            _report(lines, "DRY RUN — the above would be written; re-run with --apply.")
        print("\n".join(lines))
        return EXIT_OK

    if not has_writes:
        _report(lines, "no planned writes — nothing written, .env untouched (mtime unchanged).")
        print("\n".join(lines))
        return EXIT_OK

    # SEC M-3 (fix round): the operation is mkstemp(dir=parent) + os.replace()
    # — a RENAME — which needs write+execute permission on the PARENT
    # DIRECTORY, not on the target file's own inode (a group-writable .env
    # in a root-owned directory would pass a file-level check and then fail
    # inside atomic_write, reported as a raw WRITE FAILED instead of this
    # named refusal).
    if not os.access(env_path.parent, os.W_OK):
        _report(lines, f"REFUSING — {env_path.parent} (the .env's directory) is not "
                        f"writable and this run has planned write(s) — the write is a "
                        f"rename, which needs directory permission, not file permission. "
                        f"Fix permissions, or re-run with --skip-env-migration on the caller.")
        print("\n".join(lines))
        return EXIT_STOP

    final_lines = planned_new_lines if planned_new_lines is not None else raw_lines
    crlf = _detect_crlf(env_path)
    new_text = "\n".join(final_lines + appended) + "\n"
    if crlf:
        new_text = new_text.replace("\n", "\r\n")

    backup_path = backup_env_file(env_path)
    try:
        atomic_write(env_path, new_text)
    except Exception as exc:
        _report(lines, f"WRITE FAILED ({type(exc).__name__}) — backup preserved at "
                        f"{backup_path}. SM_PRE_UPDATE_VERSION="
                        f"{os.environ.get('SM_PRE_UPDATE_VERSION', '(unset)')}. Re-run "
                        f"standalone: migrate_env.py --apply --preimage <captured JSON>.")
        print("\n".join(lines))
        return EXIT_STOP

    try:
        post_image = run_loader(faithful)
    except LoaderError as exc:
        return _restore_or_die(
            lines, env_path, backup_path,
            f"POST-IMAGE COMPUTE FAILED ({exc}) — restoring from {backup_path}. "
            f"SM_PRE_UPDATE_VERSION={os.environ.get('SM_PRE_UPDATE_VERSION', '(unset)')}. "
            f"Re-run standalone once fixed: migrate_env.py --apply --preimage <captured JSON>.")

    ok, msg = two_layer_compare(pre_image, post_image, reported_writes, report_lines=lines)
    if not ok:
        return _restore_or_die(
            lines, env_path, backup_path,
            f"POST-IMAGE DIVERGED ({msg}) — restoring from {backup_path}. "
            f"SM_PRE_UPDATE_VERSION={os.environ.get('SM_PRE_UPDATE_VERSION', '(unset)')}. "
            f"Re-run standalone once investigated: migrate_env.py --apply --preimage "
            f"<captured JSON>.")

    # SEC M-4 / QA MED-9 (fix round): a SECOND, independent verification —
    # re-read the WRITTEN file and assert EXACTLY the planned managed keys
    # changed. two_layer_compare() above only covers the LLM pool; a wrong
    # EMBEDDER_URL/RERANKER_URL write (or any managed-key side effect) would
    # pass that check green with nothing to catch it.
    try:
        post_raw_lines = read_raw_lines(env_path)
    except UnicodeDecodeError as exc:
        return _restore_or_die(
            lines, env_path, backup_path,
            f"POST-WRITE FILE COULD NOT BE RE-READ ({type(exc).__name__}) — restoring "
            f"from {backup_path}. SM_PRE_UPDATE_VERSION="
            f"{os.environ.get('SM_PRE_UPDATE_VERSION', '(unset)')}.")
    file_ok, file_msg = verify_only_planned_keys_changed(
        original_raw_lines, post_raw_lines, planned_key_names)
    if not file_ok:
        return _restore_or_die(
            lines, env_path, backup_path,
            f"POST-WRITE FILE DIVERGED ({file_msg}) — restoring from {backup_path}. "
            f"SM_PRE_UPDATE_VERSION={os.environ.get('SM_PRE_UPDATE_VERSION', '(unset)')}. "
            f"Re-run standalone once investigated: migrate_env.py --apply --preimage "
            f"<captured JSON>.")

    _report(lines, f"applied — {env_path} rewritten (backup: {backup_path}). "
                    f"Post-image verified behaviourally identical to the pre-image; "
                    f"only the planned key(s) changed on disk.")
    print("\n".join(lines))
    return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────

def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-preimage", metavar="OUT_JSON",
                     help="capture the current effective config to OUT_JSON and exit. "
                          "A standalone run (not update_framework.sh) has no EXIT trap "
                          "on OUT_JSON — the caller owns that file's lifetime.")
    ap.add_argument("--apply", action="store_true",
                     help="write changes; without this, preview only (dry run)")
    ap.add_argument("--preimage", metavar="IN_JSON",
                     help="evaluate against a previously captured pre-image "
                          "(--capture-preimage); omit to self-capture with the "
                          "CURRENT loader (valid only while loader semantics are "
                          "unchanged from the running version)")
    ap.add_argument("--gateway-unit", default=os.environ.get("GATEWAY_UNIT", DEFAULT_GATEWAY_UNIT),
                     help=f"systemd --user unit to query for owned keys "
                          f"(default: {DEFAULT_GATEWAY_UNIT}, env GATEWAY_UNIT).")
    args = ap.parse_args(argv)

    if args.capture_preimage:
        return do_capture(args.capture_preimage, args.gateway_unit)
    return do_apply_or_dryrun(args.preimage, args.apply, args.gateway_unit)


if __name__ == "__main__":
    raise SystemExit(main())
