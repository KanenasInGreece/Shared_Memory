#!/usr/bin/env python3
"""Render effective framework config and whether the gateway would boot; never starts the gateway.

Phase A is stdlib-only (survives a coordinator import crash). Phase B needs daemon deps. Exit 0 = readable and would boot, 1 = readable but boot would refuse, 2 = could not render. Not wired into preflight.sh (different 0/1 contract; needs shared-memory/.env).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from framework_defaults import FRAMEWORK_DEFAULTS  # noqa: E402
import secure_env  # noqa: E402
from log_hygiene import scrub_url_credentials  # noqa: E402

# Env-overridable settings Phase A can meaningfully render a
# declared/present-but-empty/inherited verdict for — i.e. an actual
# os.environ-reading site exists for each. Every one of these rows carries
# its OWN "idiom" field in framework_defaults.py now (fold-round item 4;
# PROXY_BIND's idiom lives there too, even though its "kind" stays
# documented-only — no W1 code change at that site — precisely so this
# script never needs a second, hand-written idiom table of its own).
ENV_ROW_ORDER = (
    "EMBEDDER_URL",
    "RERANKER_URL",
    "LLM_DEFAULT_TARGET",
    "LLM_BACKENDS",
    "PROXY_BIND",
    "EMBED_MAX_CONTEXT_TOKENS",
    "EMBED_CHARS_PER_TOKEN",
    "EMBED_SPECIAL_TOKEN_RESERVE",
    "EMBED_MAX_CHARS",
    "OVERFLOW_TOKEN_SLACK",
)

# ── Exception rendering (SEC-HIGH, fold round) — see the module docstring's
#    "Exception rendering" section for the full policy this implements. ────

_SAFE_TO_SHOW_MESSAGE = (ImportError, ModuleNotFoundError)
_PHASE_A_EXCEPTION_HINT = "inspect shared-memory/.env for a malformed or unreadable line"
# H3 (HYG round S2): names the measured-common cause alongside the two that
# were already here -- a malformed EMBEDDER_URL/RERANKER_URL (not a valid
# http:// or https:// URL) fails coordinator._encoder_url()'s own
# import-time validation, and hive_mind_proxy.py imports coordinator before
# anything else, so THAT failure is what most often lands here. Worded to
# avoid two exact substrings other tests pin as ABSENT from this hint:
# "has no attribute" (AttributeError's own message) and "must be an http(s)
# URL" (_encoder_url's own message) -- see tests/test_check_config.py near
# :208 and :227.
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


def _verdict(key: str) -> dict:
    """The three-valued state for `key`, plus the value THIS process would
    actually resolve — mirroring the real consumer site's own idiom exactly
    (never normalised; see framework_defaults.py's LLM_DEFAULT_TARGET row
    for why an 'or' vs 'get' difference here is documented, not a bug to
    silently fix)."""
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
    if key in ("EMBEDDER_URL", "RERANKER_URL"):
        effective = (effective or "").strip().rstrip("/")
    return {"key": key, "state": state, "idiom": idiom, "effective": effective}


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
    for key in ENV_ROW_ORDER:
        v = _verdict(key)
        effective = scrub_url_credentials(str(v["effective"]))
        lines.append(f"  {key:<28} {v['state']:<20} -> {effective!r}  [idiom={v['idiom']}]")

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
        has_cred = secure_env.get_secret(name) is not None
        lines.append(f"  {name:<20} has_credential={has_cred}")

    return lines, True


def _render_extra_body_suppression(extra_body) -> str:
    """W5 E-reduced (declared half only): renders a backend's declared
    thinking-suppression state as a RECOGNISED-SHAPE match, never the raw
    `extra_body` dict — it is operator-supplied and could contain anything,
    including a key (N2, mutation-checked: render the dict here instead and
    the unrecognised-shape test dies).

    PARKED (ruling 1, `fact:1338`): probe-time `emits_reasoning` detection
    and any budget factor derived from it. This function renders only what
    was DECLARED, never anything measured or probed — see the brief's §2
    for the full set of refutations (H1/H2/H3/H5/M9/ND5) a future designer
    should read before reopening that half. Reopen trigger: a measured
    reasoning-caused REM/NREM dead-letter on a live fleet.
    """
    if not extra_body:
        return "none declared"
    if not isinstance(extra_body, dict):
        return "present, unrecognised shape"
    # DeepSeek shape: {"thinking": {"type": "disabled"}}.
    thinking = extra_body.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") == "disabled":
        return "thinking suppression declared (DeepSeek shape: thinking.type=disabled)"
    # Qwen/DashScope shape: {"enable_thinking": false}.
    if extra_body.get("enable_thinking") is False:
        return "thinking suppression declared (enable_thinking=false)"
    return "present, unrecognised shape"


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
        lines.append("  LLM_BACKENDS_JSON is present but EMPTY — never used; the legacy "
                     "LLM_BACKENDS/LLM_DEFAULT_TARGET pool is what actually serves "
                     "(migrate_env.py's own case 0, CASE_JSON_PRESENT_EMPTY).")
        count += 1
    if list(getattr(proxy, "LLM_BACKENDS", [])) == [""]:
        lines.append("  LLM_BACKENDS composed to [''] — LLM_DEFAULT_TARGET is present but "
                     "EMPTY and neither LLM_BACKENDS nor LLM_BACKENDS_JSON is declared; "
                     "the gateway will attempt to route to an empty URL.")
        count += 1
    lines.append(f"  {count} latent case(s) present on this install.")
    return lines


def phase_b_render() -> "tuple[list[str], int]":
    """Returns (lines, exit_code). Never raises: a daemon-side import
    failure (bad encoder URL, a malformed LLM_BACKENDS_JSON entry shape, or
    the daemon dependencies simply not being installed) is caught here and
    reported as one line via _render_exception(), never a traceback and
    never the raw exception message unless its type is on the safe
    allowlist."""
    lines: list[str] = []
    try:
        import hive_mind_proxy as proxy  # noqa: PLC0415
    except Exception as exc:
        lines.append("")
        lines.append("== Phase B — backends ==")
        lines.append("UNAVAILABLE — import failed: " + _render_exception(exc, _PHASE_B_EXCEPTION_HINT))
        return lines, 2

    lines.append("")
    lines.append("== Phase B — backends ==")

    # QA Q2 (fold round, the substantive finding): a PARSE-ERROR
    # LLM_BACKENDS_JSON ('{not json', or valid JSON that is simply not a
    # list) is caught INSIDE hive_mind_proxy._load_llm_backends() and
    # silently replaced by the legacy LLM_BACKENDS/LLM_DEFAULT_TARGET
    # fallback — import succeeds, the guard functions below pass, and
    # without this line the report would look like a clean, intended
    # single-backend roster. Ruled: exit code stays 0 (the gateway DOES
    # boot) — this is a prominent WARNING line, never a second meaning for
    # exit 1.
    fallback_reason = getattr(proxy, "LLM_POOL_FALLBACK_REASON", None)
    if fallback_reason:
        lines.append("⚠ DECLARED FLEET NOT USABLE — the gateway would boot on the "
                     "legacy fallback: " + scrub_url_credentials(str(fallback_reason)))
        lines.append("")

    # W3 build item (Backend_Declaration_Spec_2026-08-30 §4 / R-A): the OTHER
    # half of D1's pair, rendered as its own flagged line so the instrument
    # migrate_env.py itself leans on (the non-interactive report line's
    # "see GET /health" pointer) shows the same state here too. Mutually
    # exclusive with LLM_POOL_FALLBACK_REASON by construction (D1) — nothing
    # was declared at all here, vs. a declared fleet that got excluded above
    # — so this never doubles up with the warning block just printed.
    config_empty = getattr(proxy, "LLM_POOL_CONFIG_EMPTY", False)
    if config_empty:
        # Remedy honesty (§6.5): migrate_env.py's same-generation gate means
        # it correctly plans NOTHING for an install already on the current
        # loader semantics — naming it here as the fix would be a false
        # remedy. Declare LLM_BACKENDS_JSON directly instead.
        lines.append("⚠ NO BACKEND DECLARED — the gateway is falling back to "
                     + scrub_url_credentials(str(getattr(proxy, "DEFAULT_TARGET", "")))
                     + " (LLM_DEFAULT_TARGET/its own built-in default), and it is now "
                       "INELIGIBLE for role-less traffic (W4 default-deny). Declare "
                       "LLM_BACKENDS_JSON yourself.")
        lines.append("")

    if not proxy.LLM_BACKENDS:
        lines.append("(no backends configured)")
    # QA HIGH-1 (fix round): M-5'/P-5' are DEGRADED WARNINGS now (never a
    # SystemExit) — set True the moment either fires anywhere in the fleet,
    # so the "Gateway startup refusals" closing line below can stop reading
    # as an unqualified all-clear (neither guard function raises for
    # either shape, so "none" alone would say "clean" for a degraded pool).
    any_degraded_warnings = False
    for url in proxy.LLM_BACKENDS:
        lines.append(f"  {scrub_url_credentials(url)}")
        lines.append(f"    weight={proxy.LLM_WEIGHTS.get(url, 1.0)}")
        lines.append(f"    model={proxy.LLM_BACKEND_MODELS.get(url)}")
        roles = proxy.LLM_BACKEND_ROLES.get(url)
        lines.append(f"    roles={sorted(roles) if roles else None}")
        lines.append(f"    n_ctx={proxy.LLM_BACKEND_NCTX.get(url)}")
        has_credential = proxy.LLM_BACKEND_TOKENS.get(url) is not None
        lines.append(f"    has_credential={has_credential}")
        explicit = proxy.LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(url, False)
        private_ok = proxy.LLM_BACKEND_PRIVATE_OK.get(url, False)
        lines.append(f"    private_ok={private_ok} (explicit={explicit})")
        # W5 E-reduced (declared half only): recognised-shape match, never
        # the raw extra_body dict — see _render_extra_body_suppression's
        # own docstring for the parked probe/budget half.
        extra_body = proxy.LLM_BACKEND_EXTRAS.get(url)
        lines.append(f"    extra_body: {_render_extra_body_suppression(extra_body)}")
        # R-B announce (§6.3, W4/decision:1824): a roles-carrying entry with
        # no explicit private_ok used to also serve role-less traffic (the
        # plain default was True); it no longer does. Source-pinned
        # alongside the CHANGELOG opt-back line this release drafts.
        if roles and not explicit:
            lines.append(
                "    ⚠ R-B (W4): role-less traffic no longer reaches this backend — "
                "add \"private_ok\": true to opt back in."
            )
        # M-5' announce (QA HIGH-1, fix round): mirrors
        # require_valid_llm_routing_config()'s own predicate exactly — a
        # credentialed backend with NEITHER `roles` NOR an explicit
        # `private_ok` is configured but will NEVER be selected under
        # default-deny. The gateway boots (a WARNING, never a refusal) —
        # this is the per-entry rendering the brief named as the SECOND
        # of the two instruments for this check, alongside the startup
        # log line. SEC M-1: the credential is still probed on every
        # /health cycle even though the backend can serve nothing.
        if has_credential and roles is None and not explicit:
            any_degraded_warnings = True
            lines.append(
                "    ⚠ M-5' (W4): configured but will NEVER be selected — declare "
                "\"roles\" or \"private_ok\" explicitly. Its credential is still "
                "sent on every /health probe cycle even though it can serve "
                "nothing (SEC M-1) — remove the entry if you did not mean to "
                "attach the key."
            )
        # P-5' announce (QA HIGH-1 / SEC H-1, fix round): mirrors
        # require_valid_llm_routing_config()'s own narrowed predicate — auth
        # is OFF (AGENT_TOKENS unset) and this entry's private_ok was
        # EXPLICITLY set false. SEC H-1: "safe by construction" is true only
        # for the roles-ABSENT subset (no roles + private_ok=false really
        # does serve nothing); a `roles`-carrying entry in this state still
        # serves every caller those roles — auth off means the gateway
        # cannot tell callers apart — so this is said honestly, not
        # papered over as a blanket safety claim.
        if not proxy.AUTH_CONFIGURED_AT_STARTUP and explicit and not private_ok:
            any_degraded_warnings = True
            if roles:
                lines.append(
                    "    ⚠ P-5' (W4): AGENT_TOKENS is unset (auth off) and private_ok "
                    "is explicitly false, but this entry still declares "
                    f"roles={sorted(roles)} — NOT safe by construction: those roles "
                    "still serve EVERY caller, and with auth off the gateway cannot "
                    "tell callers apart. Set AGENT_TOKENS if that scoping was meant "
                    "to be enforceable."
                )
            else:
                lines.append(
                    "    ⚠ P-5' (W4): AGENT_TOKENS is unset (auth off) and private_ok "
                    "is explicitly false — safe by construction (no roles, so this "
                    "backend already serves nothing)."
                )

    # QA Q3 (fold round, LOW): guarded getattr on the one PRIVATE proxy
    # symbol this script reads, same defensive shape hive_mind_proxy.py
    # itself uses for its own private-member imports (hive_mind_proxy.py
    # :39-56) — a future rename degrades this report (an honest line),
    # never crashes it.
    _MISSING = object()
    role_errors = getattr(proxy, "_LLM_BACKEND_ROLE_CONFIG_ERRORS", _MISSING)
    if role_errors is _MISSING:
        lines.append("  role config errors: UNKNOWN — hive_mind_proxy no longer exposes "
                     "_LLM_BACKEND_ROLE_CONFIG_ERRORS; update check_config.py to match "
                     "its current internals")
    elif role_errors:
        lines.append("  role config errors:")
        for e in role_errors:
            # Belt-and-braces (SEC-MED, fold round): _load_llm_backends()
            # already scrubs every URL it puts into this list at
            # construction time — this re-wrap costs nothing and guards
            # against a future change to that construction site silently
            # dropping the scrub.
            lines.append(f"    {scrub_url_credentials(str(e))}")

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
    except SystemExit as exc:
        # SEC-MED (fold round): belt-and-braces — both guard functions
        # already scrub every URL in their own message at construction
        # time; this re-wrap guards against a future change there.
        lines.append("  WOULD REFUSE TO START: " + scrub_url_credentials(str(exc)))
        return lines, 1
    # QA HIGH-1 (fix round): "none" here used to be read as an unqualified
    # all-clear — but neither guard function raises for M-5'/P-5' any more,
    # so a fleet with one or more ⚠ lines above would print this exact
    # sentence too. Qualify it rather than let "none" carry a meaning it no
    # longer has; the base sentence text is left intact (tests key on it).
    closing = "  none — the gateway would boot with this configuration."
    if any_degraded_warnings:
        closing += (" ⚠ Degraded, not clean — see the M-5'/P-5' warning(s) above: "
                    "the gateway boots, but not every backend you configured serves "
                    "what you may have intended.")
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
        return 0

    b_lines, code = phase_b_render()
    for line in b_lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
