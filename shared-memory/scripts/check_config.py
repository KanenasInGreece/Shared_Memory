#!/usr/bin/env python3
"""check_config.py — render the framework's effective configuration and what
the running gateway will DO with it, without ever starting the gateway.

W1 (Local_Documentation plan, anchor v0.9.77). Standalone; not part of the
thin client, not wired into preflight.sh (see the note at the bottom of this
docstring for why).

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
                        default" when it does not.
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

Phase B — the backend half. `import hive_mind_proxy` inside
`except Exception`, so a daemon-side misconfiguration (a bad encoder URL, a
non-numeric weight, LLM_BACKENDS_JSON shaped as an array of strings instead
of objects, or the daemon dependencies — aiohttp/asyncpg/httpx/neo4j —
simply not being installed under whatever python ran this) degrades to
"Phase A printed, Phase B unavailable, exit 2" rather than a raw traceback.
On success, renders one line per configured LLM backend: url
(credential-scrubbed), weight, model, roles, n_ctx, has_credential (a bool,
never the token), private_ok (effective) and private_ok_explicit (the first
place LLM_BACKEND_PRIVATE_OK_EXPLICIT becomes visible outside the code), and
any collected role_config_errors. extra_body is deliberately NOT rendered
here — that display belongs to a later wave.

Exit codes — this script is a RENDERER, never an ENFORCER. It never
re-implements the gateway's own startup refusals; it calls the gateway's
own guard functions (hive_mind_proxy.require_auth_when_provider_keys_
configured() / require_valid_llm_routing_config() — both pure, module-level:
only log.warning and raise SystemExit, no I/O, no mutation) wrapped in
`except SystemExit as e: print(str(e))`. A copy of their predicates here
would go stale the day either function's own logic changes.

  0   config readable AND neither guard function would refuse to start.
  1   readable, but the gateway WILL refuse to boot. Reachable ONLY once
      Phase B's import has already succeeded — both guard functions live in
      hive_mind_proxy.py, so this code is never reached without it.
  2   could not read/render at all: a Phase-B import crash, OR an
      unreadable-but-PRESENT .env file. An ABSENT .env is NOT exit 2 (see
      Phase A above).

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
# os.environ-reading site exists for each. The first four are
# framework_defaults.py "env-default" rows and carry their own "idiom"
# field; PROXY_BIND (hive_mind_proxy.py:5253) is a genuine env-overridable
# site too but a "documented-only" row in that table (no code change in
# W1), so its idiom is recorded here rather than widening D1's table.
ENV_ROW_ORDER = ("EMBEDDER_URL", "RERANKER_URL", "LLM_DEFAULT_TARGET", "LLM_BACKENDS", "PROXY_BIND")
_PROXY_BIND_IDIOM = "get"


def _idiom_for(key: str) -> str:
    row = FRAMEWORK_DEFAULTS[key]
    if "idiom" in row:
        return row["idiom"]
    if key == "PROXY_BIND":
        return _PROXY_BIND_IDIOM
    raise ValueError(f"no known idiom for {key!r}")  # pragma: no cover -- defensive, unreachable via ENV_ROW_ORDER


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
    """Returns (lines, ok). ok is False only for the one failure Phase A can
    itself hit: shared-memory/.env EXISTS but could not be read (permission
    denied, a directory in its place, ...) — an ABSENT file is not this."""
    lines: list[str] = []
    lines.append("== Phase A — environment ==")
    env_path = secure_env._select_env_file()
    if env_path is None:
        lines.append("shared-memory/.env: none found — environment-only "
                      "(this is a legitimate headless state, not an error)")
    else:
        lines.append(f"shared-memory/.env: {env_path}")

    try:
        secure_env.load_split_env()
    except OSError as exc:
        lines.append(f"ERROR: {env_path} exists but could not be read ({exc})")
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
    reported as one line, never a traceback."""
    lines: list[str] = []
    try:
        import hive_mind_proxy as proxy  # noqa: PLC0415
    except Exception as exc:
        lines.append("")
        lines.append("== Phase B — backends ==")
        lines.append(f"UNAVAILABLE — import failed ({type(exc).__name__}: {exc})")
        return lines, 2

    lines.append("")
    lines.append("== Phase B — backends ==")
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
    if proxy._LLM_BACKEND_ROLE_CONFIG_ERRORS:
        lines.append("  role config errors:")
        for e in proxy._LLM_BACKEND_ROLE_CONFIG_ERRORS:
            lines.append(f"    {e}")

    lines.append("")
    lines.append("Gateway startup refusals (calling the gateway's own guard functions):")
    try:
        proxy.require_auth_when_provider_keys_configured()
        proxy.require_valid_llm_routing_config()
    except SystemExit as exc:
        lines.append(f"  WOULD REFUSE TO START: {exc}")
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
