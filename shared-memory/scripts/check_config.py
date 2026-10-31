#!/usr/bin/env python3
"""Render effective framework config and whether the gateway would boot; never starts the gateway.

Phase A is stdlib-only (survives a coordinator import crash). Phase B needs daemon deps. Exit 0 = readable and would boot, 1 = readable but boot would refuse, 2 = could not render. Not wired into preflight.sh (different 0/1 contract; needs shared-memory/.env).
"""
import argparse
import importlib.util
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework_defaults import FRAMEWORK_DEFAULTS  # noqa: E402
import secure_env  # noqa: E402
from log_hygiene import scrub_url_credentials  # noqa: E402

# Only names with a real os.environ read. Each row's idiom lives in framework_defaults so this file does not keep a second table.
ENV_ROW_ORDER = (
    "EMBEDDER_URL",
    "RERANKER_URL",
    "PROXY_BIND",
    "EMBED_MAX_CONTEXT_TOKENS",
    "EMBED_CHARS_PER_TOKEN",
    "EMBED_SPECIAL_TOKEN_RESERVE",
    "EMBED_MAX_CHARS",
)

# ── Exception rendering (SEC-HIGH, fold round) — see the module docstring's
#    "Exception rendering" section for the full policy this implements. ────

_SAFE_TO_SHOW_MESSAGE = (ImportError, ModuleNotFoundError)
_PHASE_A_EXCEPTION_HINT = "inspect shared-memory/.env for a malformed or unreadable line"
# A bad encoder URL is the usual import crash, because the proxy imports the coordinator first. This hint must not repeat that validator's own wording.
_PHASE_B_EXCEPTION_HINT = ("inspect LLM_BACKENDS_JSON, or EMBEDDER_URL/RERANKER_URL (each must "
                           "be a valid http:// or https:// URL -- a malformed one fails "
                           "coordinator's own import-time validation), or run with the daemon's "
                           "dependencies (aiohttp/asyncpg/httpx/neo4j) installed")


def _render_exception(exc: BaseException, hint: str) -> str:
    """Renders `exc` for output WITHOUT ever risking a secret in its own
    str() reaching the terminal. type(exc).__name__ is ALWAYS shown. Only
    ImportError/ModuleNotFoundError (dependency-resolution messages, which
    carry no config payload) get their str() shown too — scrubbed through
    scrub_url_credentials regardless, belt-and-braces. Every other type
    renders its type name plus `hint` — NEVER its own message, which can
    embed a raw .env line or a raw LLM_BACKENDS_JSON fragment (i.e. a
    secret) depending on where it originated."""
    type_name = type(exc).__name__
    if isinstance(exc, _SAFE_TO_SHOW_MESSAGE):
        return f"{type_name}: {scrub_url_credentials(str(exc))}"
    return f"{type_name} — {hint}"


def _idiom_for(key: str) -> str:
    """The row's own 'idiom' field — every row ENV_ROW_ORDER names carries
    one (see framework_defaults.py's module docstring). No special-casing
    here any more: a KeyError on a row that lacks one is a genuine bug in
    ENV_ROW_ORDER (a key added there without an idiom in the table), not a
    condition this function should paper over."""
    return FRAMEWORK_DEFAULTS[key]["idiom"]


# Module-level float()/int() env reads of dream_telemetry.py, in module order. A parse failure
# in any of them stops the gateway at import, so _gateway_embed_values names the first to raise.
# A non-finite EMBED_CHARS_PER_TOKEN (nan, inf, 1e309) counts too: it is the only float fed to
# the derived int((ctx - reserve) * ratio) and int(reserve * ratio), which raise on it.
_DREAM_TELEMETRY_PARSES = (
    ("LLM_CEILING_FLOOR", float), ("LLM_MIN_TOK_S", float), ("EMBED_MIN_TOK_S", float),
    ("EMBED_MAX_CONTEXT_TOKENS", int), ("EMBED_CHARS_PER_TOKEN", float),
    ("EMBED_MAX_CHARS", int), ("EMBED_SAFETY_FACTOR", float),
    ("EMBED_TIMEOUT_FLOOR_S", float), ("RERANK_MIN_CHARS_S", float),
    ("RERANK_MAX_DOC_CHARS", int), ("RERANK_SAFETY_FACTOR", float),
    ("RERANK_TIMEOUT_FLOOR_S", float),
)
_EMBED_KEYS = ("EMBED_MAX_CONTEXT_TOKENS", "EMBED_CHARS_PER_TOKEN",
               "EMBED_SPECIAL_TOKEN_RESERVE", "EMBED_MAX_CHARS")
_GATEWAY_BIND_DEFAULT = "127.0.0.1"


def _gateway_embed_values() -> "tuple[dict | None, str | None]":
    """What the gateway's own dream_telemetry resolves for the four embed values, as
    (values, None); or (None, name) when importing it fails, name being the env variable
    whose parse raised (None if unknown). Loads a PRIVATE copy never put in sys.modules, so
    the shared module that Phase B, coordinator and encoder_window read is never reloaded
    or half-updated. Call after load_split_env(): the module reads os.environ at import."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dream_telemetry.py")
    try:
        spec = importlib.util.spec_from_file_location("_check_config_dream_telemetry", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return {k: getattr(module, k) for k in _EMBED_KEYS}, None
    except Exception:
        # Never surface the exception message (it can carry a raw .env line); name the variable.
        for name, conv in _DREAM_TELEMETRY_PARSES:
            raw = os.environ.get(name)
            if raw is None:
                continue
            try:
                parsed = conv(raw)
            except (ValueError, OverflowError):
                return None, name
            if name == "EMBED_CHARS_PER_TOKEN" and not math.isfinite(parsed):
                return None, name
        return None, None


def _encoder_base_problem(base: str) -> str:
    """Why coordinator._encoder_url would refuse the already-stripped `base` at import ('' if
    it would not): a query or fragment (normalize_encoder_base), or a scheme other than
    http/https."""
    try:
        parsed = urllib.parse.urlsplit(base)
    except ValueError:
        return "not an http(s) URL — the gateway would not start"
    if parsed.query or parsed.fragment:
        return "carries a query string or fragment — the gateway would not start"
    if parsed.scheme not in ("http", "https"):
        return "not an http(s) URL — the gateway would not start"
    return ""


def _verdict(key: str, gateway: "tuple[dict | None, str | None] | None" = None) -> dict:
    """The three-valued state for `key`, plus the value THIS process would
    actually resolve — mirroring the real consumer site's own idiom exactly
    (never normalised). An 'or' vs 'get' difference is the consumer's own
    idiom, not a bug to silently fix. Rows whose consumer does more than get/or (the embed
    values, PROXY_BIND, the encoder URLs) take the
    consumer's own result, or name why the gateway would not start, in `note`
    ('' when there is nothing to add). `gateway` is _gateway_embed_values()'s
    result; omitted, it is computed here."""
    default = FRAMEWORK_DEFAULTS[key]["default"]
    idiom = _idiom_for(key)
    raw = os.environ.get(key)
    if raw is None:
        state, effective = "inherited default", default
    elif raw.strip() == "":
        state = "present-but-empty"
        effective = (raw or default) if idiom == "or" else raw
    else:
        state, effective = "declared", raw
    note = ""
    if key in ("EMBEDDER_URL", "RERANKER_URL"):
        effective = (effective or "").strip().rstrip("/")
        # coordinator._encoder_url refuses a non-http(s) base at import.
        note = _encoder_base_problem(effective)
    elif key == "PROXY_BIND" and raw is not None:
        # hive_mind_proxy._resolve_proxy_bind_host: strip, empty falls back to loopback.
        effective = raw.strip() or _GATEWAY_BIND_DEFAULT
    elif key in _EMBED_KEYS:
        values, failed = gateway if gateway is not None else _gateway_embed_values()
        if values is None:
            if failed == key:
                note = "not a number — the gateway would not start"
        elif key == "EMBED_SPECIAL_TOKEN_RESERVE" and raw is not None:
            # dream_telemetry._parse_special_token_reserve: only an int in
            # [0, EMBED_MAX_CONTEXT_TOKENS) is used, anything else falls back.
            try:
                accepted = 0 <= int(raw) < values["EMBED_MAX_CONTEXT_TOKENS"]
            except ValueError:
                accepted = False
            if not accepted:
                effective = values[key]
                note = f"invalid — the gateway uses {effective}"
        elif key == "EMBED_MAX_CHARS" and raw is None:
            # dream_telemetry derives it from the context, reserve and chars-per-token.
            state, effective = "derived", values[key]
            note = ("from (EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) "
                    "* EMBED_CHARS_PER_TOKEN")
    return {"key": key, "state": state, "idiom": idiom, "effective": effective, "note": note}


def _redact_token_env_keys(text: str) -> str:
    """A token_env value that is not a variable name is omitted from the report."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if not isinstance(data, list):
        return text
    changed = False
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("token_env")
        if isinstance(name, str) and name and not secure_env.token_env_is_variable_name(name):
            entry["token_env"] = "<not a variable name>"
            changed = True
    if not changed:
        return text
    return json.dumps(data)


def _secret_names() -> "list[str]":
    """Every secret-classified name Phase A can see BEFORE any daemon
    import: the fixed KNOWN_SECRET_NAMES list plus every LLM_BACKENDS_JSON
    token_env name secure_env.load_split_env() discovered while parsing
    the .env file — see secure_env.py's own precedence statement."""
    return sorted(secure_env.KNOWN_SECRET_NAMES | secure_env._dynamic_secret_names)


def phase_a_render() -> "tuple[list[str], bool]":
    """Returns (lines, ok). ok is False for ANY failure reading/resolving
    shared-memory/.env — SEC-HIGH fold-round fix: this used to catch only
    OSError (an unreadable file); broadened to Exception, because secure_
    env.load_split_env() can also raise a ValueError that quotes the
    OFFENDING .env LINE verbatim in its own message (i.e. a raw secret),
    and an unhandled exception of any other type would print that via a
    traceback. secure_env._select_env_file() itself now runs INSIDE this
    same try (QA Q3, fold round) — "crash-proof by construction" meant the
    whole of Phase A's env access, not just the read call afterward."""
    lines: list[str] = []
    lines.append("== Phase A — environment ==")
    saved = _push_gateway_picture()
    try:
        if _unit_file_error:
            lines.append(_unit_file_error)
            return lines, False
        return _phase_a_render_body(lines)
    finally:
        _pop_picture(saved)


def _phase_a_render_body(lines: list) -> "tuple[list[str], bool]":
    try:
        env_path = secure_env._select_env_file()
        if env_path is None:
            lines.append("shared-memory/.env: none found — environment-only "
                          "(this is a legitimate headless state, not an error)")
        else:
            lines.append(f"shared-memory/.env: {env_path}")
        secure_env.load_split_env()
    except Exception as exc:
        lines.append("ERROR: shared-memory/.env could not be read or parsed — "
                      + _render_exception(exc, _PHASE_A_EXCEPTION_HINT))
        return lines, False

    lines.append("")
    lines.append("Effective configuration:")
    gateway = _gateway_embed_values()
    for key in ENV_ROW_ORDER:
        v = _verdict(key, gateway)
        effective = scrub_url_credentials(str(v["effective"]))
        if key == "LLM_BACKENDS_JSON":
            effective = _redact_token_env_keys(effective)
        suffix = f"  ({v['note']})" if v["note"] else ""
        lines.append(f"  {key:<28} {v['state']:<20} -> {effective!r}  [idiom={v['idiom']}]{suffix}")
    if gateway[0] is None and gateway[1] is None:
        # The import failed but no variable could be named; say so without exception text.
        lines.append("  dream_telemetry: a value could not be used — the gateway would not start")
    elif gateway[0] is None and gateway[1] not in ENV_ROW_ORDER:
        lines.append(f"  {gateway[1]}: not a number — the gateway would not start")

    lines.append("")
    lines.append("Credentials (boolean only — the value is never rendered):")
    # Fix round QA finding 7: D.2 (SEC round) now stores every discovered
    # token_env name CANONICALLY (upper-cased) in _dynamic_secret_names —
    # correct for classification, but a name declared lowercase in the
    # operator's own .env/JSON (e.g. "token_env": "openrouter_cred") is
    # rendered here as OPENROUTER_CRED, a spelling that appears nowhere in
    # their config. Noted explicitly so the census stays honest about what
    # it shows, rather than silently implying an exact-spelling match.
    lines.append("  (names below are CANONICAL/upper-cased — a lowercase-"
                  "declared token_env spelling is normalised for display)")
    for name in _secret_names():
        # A token_env value that is not a variable name is a key. Printing it upper-cased would publish the key.
        if name not in secure_env.KNOWN_SECRET_NAMES and not secure_env.token_env_is_variable_name(name):
            continue
        has_cred = secure_env.get_secret(name) is not None
        lines.append(f"  {name:<20} has_credential={has_cred}")

    shape_lines = _phase_a_declaration_lines()
    if shape_lines:
        lines.append("")
        lines.extend(shape_lines)

    return lines, True


# A loaded unit does not inherit these from the operator shell. --apply never reads them from the shell either.
_HIDDEN_WHEN_UNIT_LOADED = ("LLM_BACKENDS_JSON", "LLM_BACKENDS", "LLM_DEFAULT_TARGET")


# Set when a unit EnvironmentFile= is not UTF-8. phase_a_render prints it and does not exit 0.
_unit_file_error: "str | None" = None


def _picture_llm_json(picture: dict) -> str:
    for key, value in picture.items():
        if secure_env._normalize_key(str(key)) == "LLM_BACKENDS_JSON" and value:
            return str(value)
    return ""


def _remember_picture_secrets(*pictures: dict) -> None:
    """A token_env name and a *_FILE stem are secret before the picture is copied.

    The copy runs before load_split_env, so a name with no secret suffix is still ordinary config. systemctl and busctl inherit this process.
    """
    flag = secure_env._llm_backends_json_parse_failed
    found: set[str] = set()
    try:
        for picture in pictures:
            raw = _picture_llm_json(picture)
            if raw:
                found.update(secure_env._token_env_names(raw))
            for key in picture:
                norm = secure_env._normalize_key(str(key))
                if not norm.endswith("_FILE"):
                    continue
                stem = norm[: -len("_FILE")]
                if stem:
                    found.add(stem)
    finally:
        secure_env._llm_backends_json_parse_failed = flag
    if found:
        secure_env._dynamic_secret_names.update(found)


def _unit_loaded_picture() -> "tuple[dict, bool] | None":
    """(picture, hide_shell_only). None when the query raised: the shell picture stands, and nothing is printed."""
    global _unit_file_error
    import migrate_env
    _unit_file_error = None
    unit = os.environ.get("GATEWAY_UNIT", migrate_env.DEFAULT_GATEWAY_UNIT)
    try:
        query = migrate_env.query_gateway_unit(unit)
    except Exception:
        # A unit query that raises must not become a traceback, and its text can carry a secret. The shell picture remains.
        return None
    if query.status == "query_failed" and query.error and "is not UTF-8" in query.error:
        # Checked before _gateway_env so a later read cannot hide this refusal.
        _unit_file_error = query.error
        return None
    try:
        overlay = migrate_env._gateway_env(query)
    except Exception:
        return None
    if query.status == "ok" and query.unit_id:
        # Same rule as --apply: the operator shell is not what that unit will read.
        return overlay, True
    merged = dict(os.environ)
    for key, value in overlay.items():
        if key in ("PATH", "HOME"):
            continue
        merged[key] = value
    return merged, False


def _gateway_picture() -> dict:
    """What migrate_env --apply validates.

    A loaded unit (systemctl show returned an Id) is that unit's Environment= and EnvironmentFile=, then the secure env file. The operator shell is not part of it. A check with no loaded unit is this process, so the shell remains visible underneath. PATH and HOME stay the process's.
    """
    loaded = _unit_loaded_picture()
    if loaded is None:
        return dict(os.environ)
    return loaded[0]


_PICTURE_ABSENT = object()


def _push_picture(picture: dict, hide_absent: "tuple[str, ...]" = ()) -> dict:
    """Copy non-secret picture keys into os.environ so a gateway import sees the same file the declaration check read.

    hide_absent names shell keys a loaded unit did not set. Those are removed for the duration of the check and restored by _pop_picture.
    """
    saved = {}
    for key, value in picture.items():
        if key in ("PATH", "HOME") or secure_env.is_secret_key(key):
            continue
        text = "" if value is None else str(value)
        current = os.environ.get(key, _PICTURE_ABSENT)
        if current == text:
            continue
        saved[key] = current
        os.environ[key] = text
    for key in hide_absent:
        if key in picture or key in ("PATH", "HOME") or secure_env.is_secret_key(key):
            continue
        current = os.environ.get(key, _PICTURE_ABSENT)
        if current is _PICTURE_ABSENT:
            continue
        saved[key] = current
        os.environ.pop(key, None)
    return saved


def _push_gateway_picture() -> dict:
    loaded = _unit_loaded_picture()
    if _unit_file_error:
        return {}
    if loaded is None:
        picture: dict = dict(os.environ)
        hide: "tuple[str, ...]" = ()
    else:
        picture, hide_shell = loaded
        hide = _HIDDEN_WHEN_UNIT_LOADED if hide_shell else ()
    # Names from every picture, including one this copy will not apply. The value may sit in a different picture than the JSON.
    _remember_picture_secrets(os.environ, picture)
    return _push_picture(picture, hide)


def _pop_picture(saved: dict) -> None:
    for key, old in saved.items():
        if old is _PICTURE_ABSENT:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def _phase_a_declaration_lines() -> "list[str]":
    """The same FATAL classes the gateway refuses. Names the entry and the field, never a secret."""
    errors = secure_env.llm_declaration_errors(_gateway_picture())
    if not errors:
        return []
    out = ["LLM_BACKENDS_JSON would stop the gateway (exit 78):"]
    for entry, field in errors:
        out.append(f"  {entry} field {field}")
    out.append(
        "  Each entry needs url, n_ctx and max_output_tokens, positive integers. "
        "Query the URL; if it does not say, ask the operator. Never guess."
    )
    return out


def _w4_census_lines(proxy) -> "list[str]":
    """§6.2 (M11 + case-0 census, W4): three specific present-but-empty /
    composed latents, counted and rendered LOUDLY here — REPORT ONLY, no
    behaviour change (0 on our fleet is the expected, verified reading).
    Phase B (not Phase A) because the third item needs the gateway's own
    COMPUTED LLM_BACKENDS to observe the composition, and grouping all
    three here makes it one scannable block instead of three facts
    scattered across two phases."""
    lines: "list[str]" = ["", "W4 census (present-but-empty / composed latents — report only, "
                               "no behaviour change):"]
    count = 0
    for key in ("EMBEDDER_URL", "RERANKER_URL"):
        raw = os.environ.get(key)
        if raw is not None and raw.strip() == "":
            lines.append(f"  {key} is present but EMPTY (distinct from absent) — the "
                         f"'or'-idiom falls back to the framework default anyway.")
            count += 1
    raw_json = os.environ.get("LLM_BACKENDS_JSON")
    if raw_json is not None and raw_json.strip() == "":
        lines.append("  LLM_BACKENDS_JSON is present but EMPTY — no reasoning backend is declared.")
        count += 1
    lines.append(f"  {count} latent case(s) present on this install.")
    return lines


def _restart_prevent_line() -> "str | None":
    """Warn when the loaded unit would restart a config refusal.

    A missing systemctl, or a unit that is not LoadState=loaded, is not a warning: show succeeds for a name that does not exist. 78 must be its own status. 178 contains those digits and is a different code.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None
    unit = os.environ.get("GATEWAY_UNIT") or "hive-mind-gateway.service"
    try:
        proc = subprocess.run(
            [systemctl, "--user", "show", unit, "-p", "LoadState", "-p", "RestartPreventExitStatus"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    if fields.get("LoadState") != "loaded":
        return None
    statuses = fields.get("RestartPreventExitStatus", "").split()
    if "78" in statuses:
        return None
    return ("  ⚠ the gateway unit has no RestartPreventExitStatus=78. "
            "A refused config exits 78 and systemd would restart it.")


class _DropNoBackendError(logging.Filter):
    # Only the gateway's own "no usable backend" ERROR is dropped; the reason
    # is printed in check_config's own line. Keep per-backend token_env warnings.
    def filter(self, record):
        return not record.getMessage().startswith("LLM_BACKENDS_JSON produced no usable backend")


def phase_b_render() -> "tuple[list[str], int]":
    """Returns (lines, exit_code). Never raises: a daemon-side import
    failure (bad encoder URL, a malformed LLM_BACKENDS_JSON entry shape, or
    the daemon dependencies simply not being installed) is caught here and
    reported as one line via _render_exception(), never a traceback and
    never the raw exception message unless its type is on the safe
    allowlist."""
    lines: list[str] = []
    proxy_filter = _DropNoBackendError()
    proxy_logger = logging.getLogger("hive-proxy")
    proxy_logger.addFilter(proxy_filter)
    # The picture stays until this function returns. require_auth, the census, and the hidden-credential note read os.environ after the import.
    saved = _push_gateway_picture()
    try:
        if _unit_file_error:
            lines.append(_unit_file_error)
            return lines, 2
        return _phase_b_body(lines, proxy_logger, proxy_filter)
    finally:
        _pop_picture(saved)


def _phase_b_body(lines: list, proxy_logger, proxy_filter) -> "tuple[list[str], int]":
    try:
        try:
            import importlib
            import sys
            import types
            was_loaded = "hive_mind_proxy" in sys.modules
            import hive_mind_proxy as proxy  # noqa: PLC0415
            # Reload only the real module. A test's stand-in in sys.modules is not a module file, and reloading it is not this check.
            if (was_loaded and isinstance(proxy, types.ModuleType)
                    and getattr(proxy, "__file__", None)):
                proxy = importlib.reload(proxy)
        except Exception as exc:
            lines.append("")
            lines.append("== Phase B — backends ==")
            lines.append("UNAVAILABLE — import failed: " + _render_exception(exc, _PHASE_B_EXCEPTION_HINT))
            return lines, 2
    finally:
        proxy_logger.removeFilter(proxy_filter)

    lines.append("")
    lines.append("== Phase B — backends ==")

    excluded = list(getattr(proxy, "LLM_BACKENDS_EXCLUDED", []))
    any_degraded_warnings = bool(excluded) or not proxy.LLM_BACKENDS
    if not proxy.LLM_BACKENDS and not excluded:
        lines.append("⚠ NO BACKEND DECLARED — the gateway runs with no reasoning. "
                     "Saves and search keep working. Declare LLM_BACKENDS_JSON to add a backend.")
        lines.append("")
    for item in excluded:
        lines.append(f"  excluded {scrub_url_credentials(str(item.get('url', '')))} "
                     f"reason={item.get('reason')}")
    if any(item.get("reason") == "token_missing" for item in excluded):
        try:
            import migrate_env  # noqa: PLC0415
            token_env_names = migrate_env.extract_token_env_names(os.environ.get("LLM_BACKENDS_JSON"))
            unseen = [
                name for name in token_env_names
                if secure_env.token_env_is_variable_name(name) and not secure_env.get_secret(name)
            ]
            unit_name = os.environ.get("GATEWAY_UNIT") or migrate_env.DEFAULT_GATEWAY_UNIT
            unit_q = migrate_env.query_gateway_unit(unit_name)
            hidden = migrate_env.hidden_credentials(unit_q, unseen)
            if hidden:
                lines.append("  " + migrate_env.format_hidden_credential_note(hidden))
        except Exception:
            pass

    if not proxy.LLM_BACKENDS:
        lines.append("(no backends configured)")
    for url in proxy.LLM_BACKENDS:
        lines.append(f"  {scrub_url_credentials(url)}")
        lines.append(f"    weight={proxy.LLM_WEIGHTS.get(url, 1.0)}")
        lines.append(f"    model={proxy.LLM_BACKEND_MODELS.get(url)}")
        lines.append(f"    n_ctx={proxy.LLM_BACKEND_NCTX.get(url)}")
        lines.append(f"    max_output_tokens={proxy.LLM_BACKEND_MAX_OUTPUT.get(url)}")
        lines.append(f"    priority={proxy.LLM_BACKEND_PRIORITY.get(url, 1)}")
        lines.append(f"    has_credential={proxy.LLM_BACKEND_TOKENS.get(url) is not None}")

    _MISSING = object()
    prevent = _restart_prevent_line()
    if prevent:
        # This warning is the unit, not an excluded backend or an empty roster. Those already set any_degraded_warnings above.
        lines.append(prevent)

    lines.extend(_w4_census_lines(proxy))

    # Encoder window (decision:2540): probed window vs required is a note,
    # never a gateway startup refusal.
    lines.append("")
    lines.append("Encoder context window (probed window vs required is a note, never a startup refusal):")
    try:
        import dream_telemetry as dt
        req = dt.EMBED_MAX_CONTEXT_TOKENS
    except Exception:
        req = 8192
    lines.append(f"  required: {req} tokens (EMBED_MAX_CONTEXT_TOKENS)")

    try:
        import encoder_window
        snapshot = encoder_window.get_encoder_window_snapshot()
        embed_adv = snapshot.get("embedder", {}).get("advertised_tokens")
        rerank_adv = snapshot.get("reranker", {}).get("advertised_tokens")
        if embed_adv is not None or rerank_adv is not None:
            lines.append(f"  embedder advertised: {embed_adv} (source={snapshot.get('embedder', {}).get('source')})")
            lines.append(f"  reranker advertised: {rerank_adv} (source={snapshot.get('reranker', {}).get('source')})")
            if (isinstance(embed_adv, int) and embed_adv < req) or (isinstance(rerank_adv, int) and rerank_adv < req):
                lines.append(f"  note: advertised window is short of required {req} tokens — postflight A9 will gate this")
            else:
                lines.append("  note: advertised context window meets requirement")
        else:
            lines.append(f"  note: encoder window unprobed (endpoint unreached or not exposing context length; required: {req} tokens)")
    except Exception:
        lines.append(f"  note: encoder window unprobed (required: {req} tokens)")

    lines.append("")
    lines.append("Gateway startup refusals (calling the gateway's own guard functions):")

    # QA Q3 (fold round, LOW): same guarded-getattr shape for the two guard
    # FUNCTIONS this script calls but never re-implements — a rename of
    # either is a genuine "could not render whether the gateway would boot"
    # condition, so it is reported and this returns exit 2, not a crash.
    require_auth = getattr(proxy, "require_auth_when_provider_keys_configured", _MISSING)
    require_routing = getattr(proxy, "require_valid_llm_routing_config", _MISSING)
    missing_guards = [name for name, fn in (
        ("require_auth_when_provider_keys_configured", require_auth),
        ("require_valid_llm_routing_config", require_routing),
    ) if fn is _MISSING]
    if missing_guards:
        lines.append("  UNKNOWN — hive_mind_proxy no longer exposes " + ", ".join(missing_guards)
                     + "; update check_config.py to match its current guard functions")
        return lines, 2

    try:
        require_auth()
        require_routing()
        from coordinator import require_reserved_daemon_names
        require_reserved_daemon_names()
    except SystemExit as exc:
        # SEC-MED (fold round): belt-and-braces — both guard functions
        # already scrub every URL in their own message at construction
        # time; this re-wrap guards against a future change there.
        detail = getattr(exc, "message", "") or ""
        lines.append("  WOULD REFUSE TO START: " + scrub_url_credentials(str(detail)))
        return lines, 1
    closing = "  none — the gateway would boot with this configuration."
    if any_degraded_warnings:
        closing += (" ⚠ Degraded, not clean — an excluded backend or an empty roster "
                    "is listed above. The gateway still boots.")
    lines.append(closing)
    return lines, 0


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase-a-only", action="store_true",
                     help="render only the environment half (stdlib-only; "
                          "no daemon dependencies needed)")
    args = ap.parse_args(argv)

    a_lines, a_ok = phase_a_render()
    for line in a_lines:
        print(line)
    if not a_ok:
        return 2
    if args.phase_a_only:
        # Declaration fatals are exit 1. An unreadable env already returned 2.
        return 1 if secure_env.llm_declaration_errors(_gateway_picture()) else 0

    b_lines, code = phase_b_render()
    for line in b_lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
