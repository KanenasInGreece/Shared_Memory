#!/usr/bin/env python3
"""check_config.py — render the framework's effective configuration and what
the running gateway will DO with it, without ever starting the gateway.

W1 (Local_Documentation plan, anchor v0.9.77). Standalone; not part of the
thin client, not wired into preflight.sh (see the note at the bottom of this
docstring for why). Fold round (PR #347, merger ruling on the security/QA
reviews) tightened the exception-rendering discipline below — see the SEC-
HIGH note under "Exception rendering" before touching either except clause.

Two phases, crash-proof by construction:

Phase A — the encoder + env half. STDLIB ONLY: imports only
framework_defaults, secure_env and log_hygiene.scrub_url_credentials, never
a daemon module (hive_mind_proxy.py / coordinator.py / rem_loop.py /
consolidation_loop.py). This is deliberate, not an oversight —
framework_defaults.py's own docstring explains why: importing coordinator.py
can RAISE at module level (a malformed EMBEDDER_URL/RERANKER_URL fails
_encoder_url's own validation, pinned by tests/test_coordinator_encoder_
urls.py:119/:127), and hive_mind_proxy.py:60 imports coordinator before
anything else in the gateway boots. Phase A must survive exactly that
misconfiguration and still print something useful, so it never touches
either module.

Loads shared-memory/.env through secure_env's own split loader (SEC-05/PR
A1), then renders a three-valued state per env-overridable setting:

  declared             key present in the environment, non-empty
  present-but-empty     key present but empty — what happens next depends
                        on the SITE'S OWN idiom (an "or" site falls back to
                        the default; a "get" site keeps the empty string —
                        see framework_defaults.py's LLM_DEFAULT_TARGET row
                        for the known latent this creates). Both the state
                        and the true effective value are always shown, so
                        this is never papered over as "falls back to
                        default" when it does not. Every row this script
                        renders a verdict for (EMBEDDER_URL, RERANKER_URL,
                        LLM_DEFAULT_TARGET, LLM_BACKENDS, PROXY_BIND) now
                        carries its idiom directly in framework_defaults.py
                        — fold-round item 4 removed a second, hand-written
                        idiom table that used to live only in this file.
  inherited default     key absent entirely — the table's default applies

Presence in os.environ means "resolved from the environment": secure_env's
loader setdefaults every NON-secret key into os.environ (secure_env.py
~:674-676), so an ABSENT shared-memory/.env is a legitimate headless state
— reported as environment-only, never as an error.

Every secret-classified key (PG_PASSWORD, NEO4J_PASSWORD, AGENT_TOKENS,
PG_CONN, AGENT_TOKEN, BACKUP_ADMIN_TOKEN, TAVILY_API_KEY, plus any
LLM_BACKENDS_JSON token_env name discovered) is answered ONLY as a boolean —
has_credential — via secure_env.get_secret(name) is not None. The value
itself is NEVER rendered, matching the same discipline hive_mind_proxy.
_config_snapshot() already applies on the authenticated /health payload.
Every URL is passed through log_hygiene.scrub_url_credentials before it is
ever printed.

Exception rendering (SEC-HIGH, fold round, PR #347) — the rule this entire
script's exception handling now follows, in BOTH phases: an exception's own
str() can embed content this script must never print. secure_env can raise
a ValueError quoting the OFFENDING .env LINE verbatim (a secret, if that
line set one); hive_mind_proxy's own module-level parsing can raise
AttributeError/ValueError referencing a raw LLM_BACKENDS_JSON fragment. So:

  - ALWAYS print the exception's TYPE NAME.
  - Print str(exc) — scrubbed through scrub_url_credentials, belt-and-
    braces — ONLY when the exception's type is on the small known-safe
    allowlist: ImportError / ModuleNotFoundError. Both are pure dependency-
    resolution messages ("No module named 'aiohttp'") that carry no config
    payload, so showing them is safe AND useful (it names the missing
    package).
  - Every OTHER exception type renders type-name-only, plus a fixed,
    per-phase actionable hint — never its message.

See _render_exception() below; both phases' except clauses funnel through
it, so this policy has exactly one implementation.

Phase B — the backend half. `import hive_mind_proxy` inside
`except Exception`, so a daemon-side misconfiguration (a bad encoder URL, a
non-numeric weight, LLM_BACKENDS_JSON shaped as an array of strings instead
of objects, or the daemon dependencies — aiohttp/asyncpg/httpx/neo4j —
simply not being installed under whatever python ran this) degrades to
"Phase A printed, Phase B unavailable, exit 2" rather than a raw traceback
(see the exception-rendering rule above — this branch renders through the
same _render_exception()).

If proxy.LLM_POOL_FALLBACK_REASON is set (QA Q2, fold round) —
LLM_BACKENDS_JSON was present but every entry got excluded (a parse error,
every entry malformed, ...) and hive_mind_proxy silently fell back to the
legacy LLM_BACKENDS/LLM_DEFAULT_TARGET pool — that is rendered as its own,
prominently flagged line BEFORE the roster. This does NOT change the exit
code: the gateway genuinely DOES boot on that fallback, so 0 stays 0 (exit
1 keeps its one meaning, "the gateway would refuse to start" — it must
never also mean "the gateway would boot on a configuration you probably
didn't intend").

W3 (Backend_Declaration_Spec_2026-08-30 §4 / decision:1846): if
proxy.LLM_POOL_CONFIG_EMPTY is set instead — nothing was declared at ALL
(no LLM_BACKENDS_JSON, no LLM_BACKENDS) and the gateway is serving the bare
LLM_DEFAULT_TARGET fallback — that gets its own flagged line too, mutually
exclusive with the FALLBACK_REASON line by construction (D1: one marks
EXCLUSION of a declared fleet, the other marks ABSENCE of any declaration
at all). Also exit 0 — the gateway boots either way — and this is the same
persistent state migrate_env.py's non-interactive report points an operator
back at.

On success, renders one line per configured LLM backend: url
(credential-scrubbed), weight, model, roles, n_ctx, has_credential (a bool,
never the token), private_ok (effective) and private_ok_explicit (the first
place LLM_BACKEND_PRIVATE_OK_EXPLICIT becomes visible outside the code), and
any collected role_config_errors (each element defensively re-scrubbed —
belt-and-braces, since _load_llm_backends() already scrubs every URL it
puts in that list at construction time). extra_body is deliberately NOT
rendered here — that display belongs to a later wave.

Three proxy-module symbols this script depends on (QA Q3, fold round) are
accessed via guarded getattr(), same defensive shape hive_mind_proxy.py
itself uses for its own private-member imports (hive_mind_proxy.py:39-56,
the _chmod_created_ancestors fallback): require_auth_when_provider_keys_
configured, require_valid_llm_routing_config, and the private
_LLM_BACKEND_ROLE_CONFIG_ERRORS list. A future rename of any of the three
degrades this script's report (an honest "UNKNOWN" line, or exit 2 when the
guard functions themselves are gone) instead of crashing it outright.

Exit codes — this script is a RENDERER, never an ENFORCER. It never
re-implements the gateway's own startup refusals; it calls the gateway's
own guard functions (hive_mind_proxy.require_auth_when_provider_keys_
configured() / require_valid_llm_routing_config() — both pure, module-level:
only log.warning and raise SystemExit, no I/O, no mutation) wrapped in
`except SystemExit as e: print(scrub_url_credentials(str(e)))` (belt-and-
braces — both functions already scrub every URL in their own message at
construction time). A copy of their predicates here would go stale the day
either function's own logic changes.

  0   config readable AND neither guard function would refuse to start.
      Also the outcome when LLM_BACKENDS_JSON silently fell back to the
      legacy pool (see LLM_POOL_FALLBACK_REASON above) — the gateway still
      boots, just not on the fleet you declared; look for the ⚠ line.
  1   readable, but the gateway WILL refuse to boot. Reachable ONLY once
      Phase B's import has already succeeded — both guard functions live in
      hive_mind_proxy.py, so this code is never reached without it.
  2   could not read/render at all: a Phase-B import crash, an unreadable-
      but-PRESENT .env file, OR any other exception while parsing it (a
      secure_env-internal error, not just a bare file-permission OSError —
      broadened in the fold round so nothing in Phase A's own try/except
      gap could print a raw secret via an unhandled traceback). An ABSENT
      .env is NOT exit 2 (see Phase A above). NOTE: a malformed
      LLM_BACKENDS_JSON that PARSES but produces zero usable entries
      (`{"not": "an array of objects"}`-shaped garbage, or valid JSON that
      is simply not a list) is NOT this case either — hive_mind_proxy's own
      loader catches that and falls back silently (see LLM_POOL_FALLBACK_
      REASON above); it surfaces as exit 0 with the ⚠ fallback line, never
      exit 2. Exit 2 is for a shape the loader's own try/except does NOT
      catch (e.g. a JSON array of bare strings instead of objects, which
      raises AttributeError deeper in the per-entry loop).

Usage:
    # Both phases (needs the daemon deps — aiohttp, asyncpg, httpx, neo4j —
    # e.g. the same `uv run --with ...` set the test suite uses):
    uv run --with aiohttp --with asyncpg --with httpx --with neo4j \\
        python3 shared-memory/scripts/check_config.py

    # Phase A only — plain python3, no third-party packages needed at all:
    python3 shared-memory/scripts/check_config.py --phase-a-only

See shared-memory/ops/README.md and AGENTS.md for the documented, worked
invocation of both forms.

⛔ NOT wired into preflight.sh. preflight.sh's exit contract is 0/1 (hard
requirements only) and it deliberately runs BEFORE a populated .env is
expected to exist; this script's 0/1/2 contract is different on purpose (a
config it cannot even read is not the same outcome as one it read and the
gateway will refuse), and it has nothing useful to say before Phase 1 has
written shared-memory/.env. Mentioned in the docs as a separate, later
diagnostic — never invoked automatically.
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
ENV_ROW_ORDER = ("EMBEDDER_URL", "RERANKER_URL", "LLM_DEFAULT_TARGET", "LLM_BACKENDS", "PROXY_BIND")

# ── Exception rendering (SEC-HIGH, fold round) — see the module docstring's
#    "Exception rendering" section for the full policy this implements. ────

_SAFE_TO_SHOW_MESSAGE = (ImportError, ModuleNotFoundError)
_PHASE_A_EXCEPTION_HINT = "inspect shared-memory/.env for a malformed or unreadable line"
_PHASE_B_EXCEPTION_HINT = ("inspect LLM_BACKENDS_JSON, or run with the daemon's dependencies "
                           "(aiohttp/asyncpg/httpx/neo4j) installed")


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
        lines.append(f"  {key:<20} {v['state']:<20} -> {effective!r}  [idiom={v['idiom']}]")

    lines.append("")
    lines.append("Credentials (boolean only — the value is never rendered):")
    for name in _secret_names():
        has_cred = secure_env.get_secret(name) is not None
        lines.append(f"  {name:<20} has_credential={has_cred}")

    return lines, True


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
        lines.append("⚠ NO BACKEND DECLARED — the gateway is falling back to "
                     + scrub_url_credentials(str(getattr(proxy, "DEFAULT_TARGET", "")))
                     + " (LLM_DEFAULT_TARGET/its own built-in default). Run "
                       "migrate_env.py to declare it explicitly.")
        lines.append("")

    if not proxy.LLM_BACKENDS:
        lines.append("(no backends configured)")
    for url in proxy.LLM_BACKENDS:
        lines.append(f"  {scrub_url_credentials(url)}")
        lines.append(f"    weight={proxy.LLM_WEIGHTS.get(url, 1.0)}")
        lines.append(f"    model={proxy.LLM_BACKEND_MODELS.get(url)}")
        roles = proxy.LLM_BACKEND_ROLES.get(url)
        lines.append(f"    roles={sorted(roles) if roles else None}")
        lines.append(f"    n_ctx={proxy.LLM_BACKEND_NCTX.get(url)}")
        lines.append(f"    has_credential={proxy.LLM_BACKEND_TOKENS.get(url) is not None}")
        lines.append(
            f"    private_ok={proxy.LLM_BACKEND_PRIVATE_OK.get(url, True)} "
            f"(explicit={proxy.LLM_BACKEND_PRIVATE_OK_EXPLICIT.get(url, False)})"
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
    lines.append("  none — the gateway would boot with this configuration.")
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
