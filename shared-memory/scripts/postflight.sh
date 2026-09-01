#!/usr/bin/env bash
#
# postflight.sh — verify an installed Shared Memory stack END TO END.
#
# Implements assertions A1–A8 of shared-memory/Documentation/postflight.md.
# THE SPEC IS THE CONTRACT: where this script and that document disagree, the
# document wins and this script is the defect.
#
#   A1  liveness & shape        anonymous vs authenticated /health (S-10 check)
#   A2  contract                client/gateway api_version; /health version vs checkout
#   A3  schema truth            delegates to the two shipped verifiers
#   A4  write path end to end   canary save → 1024-dim vector → outbox applied → :Fact
#   A5  read path, graded       search finds the canary; reranked OR declared degraded
#   A6  baseline emission       timings + backend_capability + capacity + hardware → JSON (never a gate)
#   A7  conduct constraints     by construction — see the spec; stated, not tested
#   A8  reasoning-backend       a REAL completion through the gateway proxy path; SKIPs
#       liveness, end to end    when no backend is reported HEALTHY (/health's
#                                llm_backends status map), never on a missing LLM
#
# Exit 0 iff A1–A5 and A8 all pass (A8 SKIPs, never gates, when no reasoning
# backend is reported healthy right now). Run after first install (AGENTS.md
# Phase 9) and after every upgrade:
#
#   export AGENT_TOKEN=...   # auth-on installs: any minted agent token,
#                            # from that agent's skill .env
#   bash shared-memory/scripts/postflight.sh

set -uo pipefail   # not -e: we run every assertion and summarise, never abort early

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the assertions ran anyway.
for _arg in "$@"; do
    case "$_arg" in
        -h|--help)
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            printf '\033[31m%s\033[0m\n' "✗ unknown argument: $_arg (this script takes none — see --help)" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Framework env lives at shared-memory/.env; the repo-root path is the pre-0.6
# fallback — same resolution order as the gateway (hive_mind_proxy.py).
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"
PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
BRIDGE="$REPO_ROOT/shared-memory/scripts/memory_bridge.py"
# Saves can take >60 s on small hosts (measured on a 2-core machine) — the
# outer client timeout stays generous. Slowness is A6's business, not a
# failure of A4/A5.
CLIENT_TIMEOUT="${CLIENT_TIMEOUT:-240}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }

declare -A afail
ok()   { grn "  ✓ $*"; }
warn() { ylw "  ! $*"; }
bad()  { local a="$1"; shift; red "  ✗ $a $*"; afail["$a"]=1; }

# Read one key from .env without sourcing it — values may contain spaces or
# other characters bash `source` would mis-parse (same idiom as preflight.sh).
read_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }

# JSON helpers — python3 one-liners, no new dependency (python3 is guaranteed:
# uv is an install prerequisite).
json_get() {  # json_get <key> [key...]  — reads JSON on stdin, prints the value
    python3 -c '
import json, sys
try:
    cur = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for k in sys.argv[1:]:
    if isinstance(cur, dict) and k in cur:
        cur = cur[k]
    else:
        sys.exit(1)
print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
' "$@"
}
json_keys() {  # sorted comma-joined top-level keys of the JSON on stdin
    python3 -c 'import json,sys; print(",".join(sorted(json.load(sys.stdin).keys())))' 2>/dev/null
}

# >>> SELECT_SUMMARY_PHRASE (tests/test_postflight_rebaseline.py extracts this
# block VERBATIM by its markers and runs it standalone via subprocess with
# fixture stdin — python3 stdlib only, keep it self-contained. WP-R3.)
select_summary_phrase() {  # reads one community_summaries row's content on
    # stdin, prints a deterministic distinctive phrase, or nothing + exit 1
    # when the content yields no words at all.
    python3 -c '
import re, sys

def select_phrase(content):
    """Pure function of content -> a short phrase for A5 re-baseline mode to
    search for. The same content always yields the same phrase (determinism).
    Strips a leading bracket-tag prefix from each line -- for a zero-
    inference thematic summary, content is literally the output of
    consolidation_loop.py fold_record_line(), joined line by line, e.g.
    "[FACT]" or
    "[DECISION kind=observation from=\"x\" recorded=... pg_id=123]" -- so a
    naive first-N-words grab would surface the machine tag, not the summary
    prose. Falls back to the raw content when every line is prefix-only (still
    returns something rather than nothing). str.split() splits on any Unicode
    whitespace, so this is unicode-safe; a short summary just yields fewer
    words, never a crash or an empty result unless the content truly has none."""
    lines = content.splitlines()
    cleaned = []
    for line in lines:
        line = re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
        if line:
            cleaned.append(line)
    text = " ".join(cleaned) if cleaned else content.strip()
    words = text.split()
    if not words:
        return None
    phrase = " ".join(words[:8])
    # SEC-01 (decision:1439, correcting fact:1437 CRITICAL to REQUIRED): strip
    # C0 (0x00-0x1F, ESC 0x1B included) and C1 (0x80-0x9F) control characters
    # from the FINAL phrase before it is ever printed or searched. The real
    # mechanism, stated correctly: postflight.sh printf %s does not interpret
    # escapes in its argument -- nothing here executes -- but raw ESC/control
    # bytes left in a phrase pulled from corpus content pass through verbatim
    # to whatever terminal or log viewer renders postflight output. That is
    # operator-visible output spoofing and log poisoning on a diagnostic
    # tool, by an actor who can already write corpus content -- not code
    # execution, but a real class, and one regex closes it.
    phrase = re.sub(r"[\x00-\x1f\x80-\x9f]", "", phrase)
    return phrase if phrase else None

phrase = select_phrase(sys.stdin.read())
if phrase:
    print(phrase)
else:
    sys.exit(1)
'
}
# <<< SELECT_SUMMARY_PHRASE

# >>> A8_BACKEND_INFO (tests/test_postflight_a8.py extracts this block
# VERBATIM and runs it standalone via subprocess with fixture stdin — same
# technique as SELECT_SUMMARY_PHRASE above. Pure function: given the
# /health payload (health_full) on stdin, prints
# "<healthy_count>|<comma-joined healthy urls, no credentials>|<comma-
# joined url=status for EVERY reported backend>".
#
# Fix round (operator ruling, post-build review): the first cut of this
# function keyed on `config.llm_backends` — the CONFIGURED list — which
# hive_mind_proxy.py's own _load_llm_backends() NEVER returns empty (unset
# LLM_BACKENDS/LLM_BACKENDS_JSON falls back to a single-entry list built
# from LLM_DEFAULT_TARGET, itself defaulting to "http://localhost:5000").
# That made the "no backend configured" SKIP branch effectively
# unreachable on any ordinary install, so a perfectly healthy LLM-less
# deployment (AGENTS.md Phase 7: "llm":"down" blocks dreaming only, never
# saves/search) would fire a doomed completion at the unconfigured
# default and FAIL postflight for having no LLM at all — exactly the
# outcome A8 exists to never cause.
#
# The correct signal lives in a DIFFERENT top-level /health key —
# `llm_backends` (not `config.llm_backends`) — the per-backend STATUS MAP
# the gateway's own liveness probe already populates every request cycle,
# e.g. {"http://localhost:5000": "ok", "https://api.deepseek.com":
# "timeout"}. Confirmed values (hive_mind_proxy.py, the loop that builds
# `backend_status` just above `checks["llm"] = ...`): "ok" (probe answered
# <400, or a credentialed backend's unauthenticated-probe 401/403 — see
# that code's own H-1/H-2 comment), "http_<code>" (any other status),
# "timeout", or "down" (connect/other exception) — "ok" is the ONLY
# healthy value; every OTHER value in the vocabulary, and no backend
# entry at all, means "not currently healthy". A3's own philosophy still
# applies: postflight never re-derives what the gateway already decided,
# it reads the gateway's verdict and trusts it — the fix is which VERDICT
# to read, not a return to re-parsing LLM_BACKENDS/LLM_BACKENDS_JSON/
# LLM_DEFAULT_TARGET in bash. healthy_count==0 is now A8's real,
# REACHABLE SKIP signal — genuinely no backend is answering right now,
# the documented non-fatal state, already surfaced by A1's own "llm"
# field. Only "url" (the map's key) is ever read for the healthy-urls
# list — has_credential/token_env never appear in this map at all, so
# nothing here can leak a key.
a8_backend_info() {  # reads /health JSON on stdin
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
statuses = d.get("llm_backends")
if not isinstance(statuses, dict):
    statuses = {}
healthy = [url for url, status in statuses.items() if status == "ok"]
summary = ",".join(f"{url}={status}" for url, status in statuses.items())
print(str(len(healthy)) + "|" + ",".join(healthy) + "|" + summary)
'
}
# <<< A8_BACKEND_INFO

# >>> A8_GRADE_COMPLETION (tests/test_postflight_a8.py extracts this block
# VERBATIM and runs it standalone via subprocess with fixture argv/stdin —
# same technique as SELECT_SUMMARY_PHRASE above. Pure function: given the
# HTTP status code postflight's own curl call to POST $GATEWAY_URL/v1/chat/
# completions returned (argv 1), the response's X-SM-Fault-Origin header
# value (argv 2, W4/Ruling B(i)/E(alpha2) — empty string when absent) and
# that response's BODY on stdin, prints exactly one verdict token:
# OK | EMPTY | NO_RESPONSE | HTTP_<code> | SKIP_<declaration>. A 200
# with unparseable JSON, a missing/non-string "content", or blank content
# all grade EMPTY — a 200 is not by itself a usable completion (this is the
# D23 lesson generalised: liveness is not capability, and a shape check is
# not a content check) — UNLESS "reasoning_content" is itself a non-empty
# string (Item B, W5, measured 2026-08-30): a thinking model at A8's
# max_tokens: 16 returns 16 tokens of reasoning_content, EMPTY content,
# finish_reason: length — that is still proof a real completion crossed the
# gateway proxy join, which is the ONE thing A8 exists to prove, so it
# grades OK. The reasoning check mirrors the content guard EXACTLY —
# isinstance(str) and .strip() — a structured reasoning_content object
# ({"blocks": []}) must NOT pass. Accepted semantic shift, stated so it is
# never later read as a hole: a finish_reason: "content_filter" response
# carrying reasoning but no content now ALSO grades OK — correct for A8's
# question (did a completion cross the join?), not "did the model comply".
# SKIP_<declaration> is a NAMED NON-FATAL skip
# (documented post-0.9.81 state, never a FAIL) for a 422 whose body carries
# ALL FOUR conjuncts (V3): error == "no_eligible_backend", a `declaration`
# key present, constraint != "fit" (a genuine oversized-request fit failure
# stays FATAL), and the response actually originated at the GATEWAY (argv 2
# == "gateway") — a passed-through UPSTREAM 422 must never be misread as a
# gateway refusal, the same discipline rem_loop.py/consolidation_loop.py
# enforce on their own refusal parse. This function only GRADES; the
# human-readable message is composed at the A8 call site below — same
# split SELECT_SUMMARY_PHRASE has from A5.
#
# SEC L-3 (fix round) — accepted residue, joining §7's list: for this
# undeclared-fleet population, check_config.py's own exit code ALSO stays
# 0 now (the guard functions it calls no longer raise for M-5'/P-5'), so
# exit 1 there no longer distinguishes "undeclared" from "clean" either —
# a fourth green-but-dark leg alongside top-level /health `llm` liveness,
# postflight A1, and this SKIP_<declaration>. `/health`
# `dependencies.llm_pool` going `degraded` (with its remedy) and
# check_config's own per-entry ⚠ M-5'/P-5' lines are the surfaces that
# still tell the truth about this population.
a8_grade_completion() {  # a8_grade_completion <status_code> <fault_origin_header>  (body on stdin)
    local status="$1"
    local fault_origin="$2"
    if [[ -z "$status" || "$status" == "000" ]]; then
        echo "NO_RESPONSE"
        return
    fi
    if [[ "$status" == "422" ]]; then
        # Body read FIRST, before the generic HTTP_$status branch below —
        # the caller cannot discriminate afterward (it rm -f's the body
        # file before its own case statement).
        local body skip
        body="$(cat)"
        skip="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
if (d.get("error") == "no_eligible_backend" and d.get("declaration") is not None
        and d.get("constraint") != "fit"):
    print(d["declaration"])
')"
        if [[ -n "$skip" && "$fault_origin" == "gateway" ]]; then
            echo "SKIP_$skip"
            return
        fi
        echo "HTTP_422"
        return
    fi
    if [[ "$status" != "200" ]]; then
        echo "HTTP_$status"
        return
    fi
    python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("EMPTY"); sys.exit(0)
try:
    message = (d.get("choices") or [{}])[0].get("message", {})
except (AttributeError, IndexError, TypeError):
    message = {}
if not isinstance(message, dict):
    message = {}
content = message.get("content")
reasoning = message.get("reasoning_content")
ok = ((isinstance(content, str) and content.strip())
      or (isinstance(reasoning, str) and reasoning.strip()))
print("OK" if ok else "EMPTY")
'
}
# <<< A8_GRADE_COMPLETION

# GNU date assumed (%3N) — like the rest of the stack (Linux/docker hosts).
# Bash builtin, never `date`: uutils coreutils (default on Ubuntu ≥25.10)
# ignores the %3N width in `date +%s%3N` and returns nanoseconds — every timing
# silently inflated ×10⁶ (measured on a clean Ubuntu 26.04 install).
# $EPOCHREALTIME is "<seconds>.<microseconds>" from bash itself, everywhere.
now_ms() { local t=$EPOCHREALTIME; echo $(( ${t%.*} * 1000 + 10#${t#*.} / 1000 )); }

# Canary save through the gateway (A4/A6). new_project is idempotent-safe BY
# CONSTRUCTION: a registered project short-circuits ingress before the flag is
# even read (coordinator.py), so the flag stays on every save. The only
# new_project refusals are confusable/spelling-variant names — those need the
# operator, not a retry, and A4's failure message surfaces the gateway's reply.
# Exactly ONE save per call, so a timing window never contains two.
CANARY_META='{"project": "install-verification", "new_project": true}'
do_save() {  # do_save <content>  — prints the bridge's JSON reply
    timeout "$CLIENT_TIMEOUT" uv run --with httpx --with python-dotenv \
            python "$BRIDGE" save "$1" "$CANARY_META" 2>/dev/null
}

echo "Shared Memory — postflight verification (spec: shared-memory/Documentation/postflight.md)"
echo

# ── Mode selection (W-P, WP-R1) ────────────────────────────────────────────────
# Runtime-detected, no new flag: CANARY MODE (current behavior, unchanged) while
# the corpus holds ZERO live non-superseded community_summaries rows (either
# kind); RE-BASELINE MODE once at least one exists. Contract refinement of the
# v0.9.17 postflight (fact:1402/decision:1403 lineage). When the count itself is
# undeterminable (docker missing, or the store unreachable) this FALLS BACK to
# CANARY MODE — the safer default, since it preserves today's verification
# rather than silently skipping a check it could not confirm was safe to skip.
POSTFLIGHT_MODE="install"
live_summary_count=""
if command -v docker >/dev/null 2>&1; then
    live_summary_count="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
            "SELECT count(*) FROM community_summaries WHERE NOT superseded" 2>/dev/null | tr -d '[:space:]')"
fi
if [[ "$live_summary_count" =~ ^[0-9]+$ && "$live_summary_count" -ge 1 ]]; then
    POSTFLIGHT_MODE="re-baseline"
    echo "Mode: RE-BASELINE ($live_summary_count live non-superseded community summaries found) — A4 saves nothing (write-path proof stays anchored to the install canary); A5 proves the read path against a live Tier-3 summary; A6's save timings are null."
elif [[ "$live_summary_count" =~ ^[0-9]+$ ]]; then
    echo "Mode: CANARY (0 live non-superseded community summaries) — install-mode behavior, unchanged: A4/A6 save fresh canaries."
else
    echo "Mode: CANARY (community_summaries count undeterminable — docker missing or the store unreachable; defaulting to canary mode)."
fi
echo

# ── A1 — liveness & shape ─────────────────────────────────────────────────────
echo "A1 — liveness & shape:"

auth_on=0
[[ -n "$(read_env AGENT_TOKENS)" ]] && auth_on=1

gateway_down=0
token_missing=0
anon_health="$(curl -s --compressed --max-time 15 "$GATEWAY_URL/health" || true)"
health_full=""   # the full-shape payload (authenticated, or anonymous on auth-off) — A6 reads it

if [[ -z "$anon_health" ]]; then
    bad A1 "gateway did not answer at $GATEWAY_URL/health — is hive-mind-gateway.service running?"
    gateway_down=1
else
    ok "A1 gateway answers at $GATEWAY_URL/health (status: $(printf '%s' "$anon_health" | json_get status || echo '?'))"
    if [[ "$auth_on" == "1" ]]; then
        anon_keys="$(printf '%s' "$anon_health" | json_keys)"
        if [[ "$anon_keys" == "api_version,status,version" ]]; then
            ok "A1 anonymous payload slimmed to exactly {status, version, api_version} (S-10 holds)"
        else
            # Auth is STARTUP-FROZEN in the gateway (AUTH_CONFIGURED_AT_STARTUP),
            # while this script reads the CURRENT .env — the two can diverge, so
            # name both causes rather than misdiagnosing one as the other.
            bad A1 "anonymous payload keys are {$anon_keys} — either an S-10 regression, OR tokens were added to .env after the gateway started (auth is startup-frozen): restart the gateway and re-run before treating this as a regression"
        fi
        if [[ -n "${AGENT_TOKEN:-}" ]]; then
            # Token via curl config on stdin, not argv — argv is world-readable
            # in /proc/<pid>/cmdline for the request's lifetime (same reasoning
            # as init_db.sh's NEO4J_PASSWORD idiom).
            health_full="$(curl -s --compressed --max-time 15 -K - "$GATEWAY_URL/health" <<< "header = \"Authorization: Bearer $AGENT_TOKEN\"" || true)"
            missing="$(printf '%s' "$health_full" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("UNPARSEABLE"); sys.exit(0)
print(",".join(k for k in ("daemon", "backend_capability", "config") if k not in d))
' )"
            if [[ -z "$missing" ]]; then
                ok "A1 authenticated payload carries the full shape"
            elif [[ "$missing" == "UNPARSEABLE" ]]; then
                bad A1 "authenticated /health did not return JSON"
            else
                bad A1 "authenticated payload is missing expected keys: $missing — token not resolving, or health assembly broken"
            fi
        else
            # ONE clear message; A4/A5/A6/A8 are marked here and print a
            # single skip line each — never a cascade of confusing errors.
            #
            # Fix round R1 (decision:1435): in re-baseline mode A4 needs no
            # token at all (it performs no gateway call — see A4 below), so
            # it must never be pre-marked failed here, and this message must
            # not name it among what a missing token skips in that mode —
            # spec wins, A4 cannot contribute to the exit code in re-baseline
            # mode, INCLUDING indirectly via this earlier mark. A8 needs a
            # token in BOTH modes (it is a live gateway call regardless of
            # community_summaries state), so it is marked unconditionally,
            # the same way A5 already is.
            if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
                bad A1 "auth is configured but AGENT_TOKEN is not set — export AGENT_TOKEN=<any minted agent token, from that agent's skill .env> and re-run. A5, A6 and A8 are skipped for this same missing token (A4 needs no token in re-baseline mode)."
            else
                bad A1 "auth is configured but AGENT_TOKEN is not set — export AGENT_TOKEN=<any minted agent token, from that agent's skill .env> and re-run. A4, A5, A6 and A8 are skipped for this same missing token."
                afail[A4]=1
            fi
            token_missing=1
            afail[A5]=1
            afail[A8]=1
        fi
    else
        missing="$(printf '%s' "$anon_health" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("UNPARSEABLE"); sys.exit(0)
print(",".join(k for k in ("daemon", "backend_capability", "config") if k not in d))
' )"
        if [[ -z "$missing" ]]; then
            ok "A1 auth-off install: full /health payload served anonymously — the correct result for this mode"
            health_full="$anon_health"
        else
            bad A1 "auth-off per the current .env, but the anonymous payload is missing keys: $missing — either health assembly broke, OR tokens were removed from .env after the gateway started (auth is startup-frozen): restart and re-run"
        fi
    fi
    [[ "$auth_on" == "1" && -n "${AGENT_TOKEN:-}" && -z "$health_full" ]] && health_full="$anon_health"
fi

# ── A2 — contract ─────────────────────────────────────────────────────────────
echo
echo "A2 — contract:"

checkout_fw="$(grep -Em1 '^FRAMEWORK_VERSION' "$REPO_ROOT/shared-memory/scripts/coordinator.py" | cut -d'"' -f2)"
client_api="$(grep -Em1 '^API_VERSION' "$BRIDGE" | tr -dc '0-9')"

if [[ "$gateway_down" == "1" ]]; then
    bad A2 "gateway unreachable — version contract cannot be checked (checkout: FRAMEWORK_VERSION=$checkout_fw, client API_VERSION=$client_api)"
else
    gw_ver="$(printf '%s' "$anon_health" | json_get version || true)"
    gw_api="$(printf '%s' "$anon_health" | json_get api_version || true)"

    if [[ -n "$gw_api" && "$client_api" == "$gw_api" ]]; then
        ok "A2 api_version match: client v$client_api == gateway v$gw_api"
    else
        bad A2 "api_version skew: client speaks v${client_api:-?}, gateway speaks v${gw_api:-?} — upgrade the older side before trusting the pair"
    fi

    if [[ -n "$gw_ver" && "$gw_ver" == "$checkout_fw" ]]; then
        ok "A2 /health version $gw_ver equals this checkout's FRAMEWORK_VERSION"
    else
        older="$(printf '%s\n%s\n' "${gw_ver:-0}" "${checkout_fw:-0}" | sort -V | head -1)"
        if [[ "$older" == "${gw_ver:-0}" ]]; then
            bad A2 "gateway reports $gw_ver but this checkout is $checkout_fw — gateway is running an older build than this checkout — a restart/redeploy is owed"
        else
            bad A2 "gateway reports $gw_ver but this checkout is $checkout_fw — this checkout is older than the running gateway; git pull before trusting checkout-relative checks"
        fi
    fi
fi

# ── A3 — schema truth (delegated to the shipped verifiers) ────────────────────
echo
echo "A3 — schema truth:"

run_verifier() {  # run_verifier <label> <uv --with dep> <script path>
    local label="$1" dep="$2" script="$3" out rc
    out="$(uv run --with "$dep" python "$script" 2>&1)"; rc=$?
    if [[ "$rc" -eq 0 ]]; then
        ok "A3 $label passed ($(basename "$script"))"
    else
        bad A3 "$label failed ($(basename "$script"), exit $rc) — last lines:"
        printf '%s\n' "$out" | tail -8 | sed 's/^/      /'
    fi
}
run_verifier "Postgres fresh-install parity" psycopg2-binary "$REPO_ROOT/shared-memory/migrations/verify_schema_init.py"
run_verifier "Neo4j declared constraints"    neo4j           "$REPO_ROOT/shared-memory/migrations/verify_neo4j_init.py"

# ── A4 — write path end to end ────────────────────────────────────────────────
echo
echo "A4 — write path end to end:"

pg_id=""
short_ms=""
marker="$(date -u +%Y-%m-%dT%H:%M:%SZ) run-$$-$RANDOM"

if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
    # WP-R2: no saves of any kind; cannot fail in this mode — there is
    # nothing left here for A4 to assert. Unconditional: does not depend on
    # gateway/token state, since it performs no gateway call at all.
    ok "A4 re-baseline mode: no canary save performed — write-path proof stays anchored to the install canary (accepted trade: re-triggers no longer re-prove the write path once the corpus has matured; writes were measured as the resilient path throughout stress testing — fact:1402/decision:1403 lineage)"
elif [[ "$token_missing" == "1" ]]; then
    warn "A4 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    bad A4 "skipped — gateway unreachable (see A1)"
elif ! command -v docker >/dev/null 2>&1; then
    bad A4 "docker not found on PATH — store-side verification is impossible on this host"
else
    # Warm the uv environment UNTIMED first: on a fresh host the first
    # `uv run` resolves and downloads packages, which would otherwise dominate
    # A6's short-save number — the baseline must time the framework, not uv.
    uv run --with httpx --with python-dotenv python "$BRIDGE" --version >/dev/null 2>&1

    # Unique per run (timestamp embedded) so SHA-256 idempotency never
    # short-circuits — this save also provides A6's short-save timing.
    canary_content="Shared Memory install-verification canary ${marker} — postflight A4 write-path check; this record is the install's birth certificate and stays in the corpus."
    t0="$(now_ms)"
    save_out="$(do_save "$canary_content")"
    t1="$(now_ms)"
    save_status="$(printf '%s' "$save_out" | json_get status)"
    if [[ "$save_status" == "success" ]]; then
        pg_id="$(printf '%s' "$save_out" | json_get pg_id)"
        short_ms=$((t1 - t0))
        ok "A4 canary saved through the gateway (pg_id $pg_id, project install-verification)"
    else
        msg="$(printf '%s' "$save_out" | json_get message || printf '%s' "$save_out" | head -c 300)"
        bad A4 "canary save failed: ${msg:-no response (timeout after ${CLIENT_TIMEOUT}s?)}"
    fi

    if [[ "$save_status" == "success" && ! "$pg_id" =~ ^[0-9]+$ ]]; then
        # Never a silent skip: a success reply without a numeric pg_id means
        # the reply SHAPE broke — the store-side checks below cannot run, and
        # that is an A4 failure in its own right, not a quiet green.
        bad A4 "save replied success but returned no numeric pg_id ('${pg_id:-<none>}') — reply shape broke; store-side checks (a)–(c) not run"
    fi

    if [[ "$pg_id" =~ ^[0-9]+$ ]]; then
        # (a) The stored embedding dimension EQUALS 1024 — the VALUE is
        # asserted, never an equality between two expressions (fact:1309).
        dim="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                "SELECT vector_dims(embedding) FROM technical_docs WHERE id=$pg_id" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$dim" == "1024" ]]; then
            ok "A4 stored embedding dimension is 1024"
        else
            bad A4 "stored embedding dimension is '${dim:-<none>}', expected exactly 1024 — the record is invisible to semantic search"
        fi

        # (b) The outbox row reaches 'applied' — the worker drains within
        # seconds; poll briefly.
        obst=""
        for _ in $(seq 1 30); do
            obst="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                    "SELECT status FROM neo4j_outbox WHERE pg_id=$pg_id ORDER BY id DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]')"
            [[ "$obst" == "applied" || "$obst" == "failed" ]] && break
            sleep 1
        done
        if [[ "$obst" == "applied" ]]; then
            ok "A4 neo4j_outbox row for pg_id $pg_id reached status 'applied'"
        elif [[ "$obst" == "failed" ]]; then
            bad A4 "neo4j_outbox row is terminally 'failed' — Neo4j was unreachable past the retry window (recovery one-liner: AGENTS.md, Status/health runbook)"
        else
            # A healthy worker mid-backoff after a transient store blip also
            # re-queues rows as 'pending' — do not pronounce the worker dead.
            bad A4 "neo4j_outbox row for pg_id $pg_id is '${obst:-<none>}' after 30s — worker mid-backoff (transient store trouble re-queues with exponential backoff) or not running; check /health failed_age and the gateway journal before concluding"
        fi

        # (c) The :Fact node exists in Neo4j. Password read with grep/cut
        # (never source) and passed via the environment, never argv
        # (same idiom as init_db.sh).
        NEO4J_PASSWORD="$(read_env NEO4J_PASSWORD)"
        if [[ -z "$NEO4J_PASSWORD" ]]; then
            bad A4 "NEO4J_PASSWORD not found in $ENV_FILE — cannot verify the graph mirror"
        else
            export NEO4J_PASSWORD
            fact_count="$(docker exec -e NEO4J_PASSWORD "$NEO4J_CONTAINER" cypher-shell -u neo4j \
                    --format plain "MATCH (f:Fact {pg_id: $pg_id}) RETURN count(f);" 2>/dev/null | tail -n1 | tr -d '[:space:]')"
            if [[ "$fact_count" == "1" ]]; then
                ok "A4 :Fact node with pg_id $pg_id exists in Neo4j"
            else
                bad A4 ":Fact count for pg_id $pg_id is '${fact_count:-<none>}', expected 1 — if the outbox row reads 'applied', outbox atomicity is broken"
            fi
            unset NEO4J_PASSWORD   # scope the secret to the one exec that needed it
        fi
    fi
fi

# ── A5 — read path, honestly graded ───────────────────────────────────────────
echo
echo "A5 — read path:"

search_ms=""
search_rebaseline_ms=""   # R2 (decision:1435): its OWN timing key — never
                          # shares "search" with the canary-mode timing,
                          # since the two time different workloads (a
                          # project-filtered marker search vs an unfiltered
                          # whole-corpus phrase search).
if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
    # WP-R3: prove the read path against a LIVE Tier-3 summary, selected at
    # run time (never a pinned id — supersession would orphan the check).
    if [[ "$token_missing" == "1" ]]; then
        warn "A5 skipped — AGENT_TOKEN missing (see A1)"
    elif [[ "$gateway_down" == "1" ]]; then
        bad A5 "skipped — gateway unreachable (see A1)"
    elif ! command -v docker >/dev/null 2>&1; then
        bad A5 "docker not found on PATH — cannot select a live Tier-3 summary for re-baseline verification"
    else
        # QA-01 (decision:1439): BOUNDED MULTI-CANDIDATE PROBE, not a
        # single-summary gate. The 3 most-recently-updated live rows
        # (either kind, in order); fewer than 3 live rows: use what
        # exists. json_agg(... ORDER BY updated_at DESC) keeps embedded
        # newlines/unicode JSON-escaped and the ordering explicit; COALESCE
        # covers the zero-row case (json_agg returns NULL, not '[]', on an
        # empty input set).
        #
        # Why 3, measured not chosen (fact:1438 sweep, all 21 live rows on
        # the reference install, same selector/limit-20 search this script
        # runs): exactly 1/21 rows fails individually -- both its Tier-3
        # candidate slots lost the rerank cut against 20 Tier-1 facts. At
        # that rate no set of 3 DISTINCT rows on this corpus can consist
        # entirely of failures, while a wholesale Tier-3 retrieval break
        # still fails all 3 loudly. This is a property of this corpus at
        # this moment, not a constant -- the fresh-install VM test
        # re-measures it on a young corpus.
        candidates_json="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                "SELECT COALESCE(json_agg(row_json ORDER BY updated_at DESC), '[]') FROM (SELECT json_build_object('id', id, 'content', content, 'kind', COALESCE(metadata->>'kind','thematic')) AS row_json, updated_at FROM community_summaries WHERE NOT superseded ORDER BY updated_at DESC LIMIT 3) sub" 2>/dev/null)"
        candidate_count="$(printf '%s' "$candidates_json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = None
print(len(d) if isinstance(d, list) else 0)
' 2>/dev/null)"
        if [[ ! "$candidate_count" =~ ^[0-9]+$ || "$candidate_count" -lt 1 ]]; then
            bad A5 "re-baseline mode selected on a nonzero live-summary count, but no live non-superseded community_summaries rows could be read just now for the multi-candidate probe — the count and this read disagree; the corpus may have changed between the two, or the store is unreachable"
        else
            probe_pass_message=""
            probe_hardfail_message=""
            probe_last_message=""
            probe_last_was_catchall=0
            t0="$(now_ms)"
            for cand_idx in $(seq 1 "$candidate_count"); do
                cand_row="$(printf '%s' "$candidates_json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(json.dumps(d[$cand_idx - 1]))
")"
                cand_id="$(printf '%s' "$cand_row" | json_get id)"
                cand_kind="$(printf '%s' "$cand_row" | json_get kind)"
                cand_content="$(printf '%s' "$cand_row" | json_get content)"
                cand_ref_type="summary"
                [[ "$cand_kind" == "insight" ]] && cand_ref_type="insight"
                cand_ref="${cand_ref_type}:${cand_id}"
                cand_phrase="$(printf '%s' "$cand_content" | select_summary_phrase)"
                if [[ -z "$cand_phrase" ]]; then
                    probe_last_message="candidate $cand_idx of $candidate_count, $cand_ref has no extractable phrase (content yields no words after cleaning)"
                    probe_last_was_catchall=0
                    continue
                fi
                cand_search_out="$(timeout "$CLIENT_TIMEOUT" uv run --with httpx --with python-dotenv \
                        python "$BRIDGE" search "$cand_phrase" 20 2>/dev/null)"
                # C2 (decision:1435): the coordinator keyword-fallback shape
                # (served when the embedder is unreachable) omits the
                # "ranked" key entirely -- a DIFFERENT signal from an honest
                # ranked:false degraded result, never graded as one.
                # QA-02 (decision:1439): report the ACTUAL returned count
                # (n), never a hardcoded 20 -- the set is at MOST 20 and
                # smaller on a young or filtered corpus.
                cand_verdict="$(printf '%s' "$cand_search_out" | python3 -c '
import json, sys
ref = sys.argv[1]
try:
    d = json.load(sys.stdin)
except Exception:
    print("BADJSON"); sys.exit(0)
if isinstance(d, dict):
    print("ERROR:" + str(d.get("message") or d.get("error") or "unexpected reply")[:200]); sys.exit(0)
if not d:
    print("EMPTY"); sys.exit(0)
if "ranked" not in d[0]:
    print("KEYWORD_FALLBACK"); sys.exit(0)
ranked = bool(d[0].get("ranked"))
n = len(d)
if ranked:
    idx = next((i for i, r in enumerate(d) if r.get("ref") == ref), None)
    print(("PRESENT:%d:%d" % (idx + 1, n)) if idx is not None else ("ABSENT:%d" % n))
else:
    print("DEGRADED")
' "$cand_ref")"
                case "$cand_verdict" in
                    PRESENT:*)
                        rest="${cand_verdict#PRESENT:}"
                        cand_rank="${rest%%:*}"
                        cand_total="${rest#*:}"
                        probe_pass_message="A5 re-baseline: candidate $cand_idx of $candidate_count, $cand_ref present at rank $cand_rank of $cand_total for phrase \"$cand_phrase\" (presence, not rank, asserted — v0.8.54 gives no rank guarantee)"
                        break
                        ;;
                    DEGRADED)
                        probe_pass_message="A5 re-baseline: candidate $cand_idx of $candidate_count, $cand_ref — results returned, DEGRADED mode declared honestly — Tier-3 narratives are omitted in degraded mode by design (measured in the 2026-08-21 stress test; v0.8.54 ruling \"ranked, not guaranteed\"); the presence assertion is WAIVED for this candidate, not silently passed"
                        break
                        ;;
                    KEYWORD_FALLBACK)
                        # Immediate hard failure -- do NOT keep trying
                        # candidates. The embedder being gone is not a
                        # per-row problem a different candidate could route
                        # around; re-baseline A4 performs no save, so this
                        # would otherwise pass undetected.
                        probe_hardfail_message="re-baseline: candidate $cand_idx of $candidate_count, $cand_ref — results carry no \"ranked\" key at all, semantic search is not serving, keyword-fallback shape detected (the embedder is unreachable); the probe stops here rather than trying more candidates"
                        break
                        ;;
                    ABSENT:*)
                        cand_total="${cand_verdict#ABSENT:}"
                        probe_last_message="candidate $cand_idx of $candidate_count, $cand_ref absent from the $cand_total returned rows"
                        probe_last_was_catchall=0
                        ;;
                    EMPTY)
                        probe_last_message="candidate $cand_idx of $candidate_count, $cand_ref — search returned zero results"
                        probe_last_was_catchall=0
                        ;;
                    ERROR:*)
                        probe_last_message="candidate $cand_idx of $candidate_count, $cand_ref — search failed: ${cand_verdict#ERROR:}"
                        probe_last_was_catchall=0
                        ;;
                    *)
                        probe_last_message="candidate $cand_idx of $candidate_count, $cand_ref — no parseable JSON (timeout after ${CLIENT_TIMEOUT}s?)"
                        probe_last_was_catchall=1
                        ;;
                esac
            done
            t1="$(now_ms)"
            search_rebaseline_ms=$((t1 - t0))
            if [[ -n "$probe_pass_message" ]]; then
                ok "$probe_pass_message"
            elif [[ -n "$probe_hardfail_message" ]]; then
                bad A5 "$probe_hardfail_message"
            else
                # QA-03 (decision:1439): name the two preconditions so a
                # reader can tell a rerank cut from a broken read path.
                bad A5 "none of the $candidate_count attempted candidate(s) came back — last: ${probe_last_message:-no candidate could be evaluated}. A candidate must (a) win its kind's single Tier-3 slot by vector nearest-neighbour, then (b) survive the rerank cut against the Tier-1 candidates in the pool — a genuine break here, across $candidate_count independent candidates, is a real read-path failure, not a rank complaint on any single row"
                if [[ "$probe_last_was_catchall" == "1" ]]; then
                    search_rebaseline_ms=""   # the decisive attempt was unparseable/timed out — not a measurement
                fi
            fi
        fi
    fi
elif [[ "$token_missing" == "1" ]]; then
    warn "A5 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    bad A5 "skipped — gateway unreachable (see A1)"
elif [[ ! "$pg_id" =~ ^[0-9]+$ ]]; then
    bad A5 "skipped — no canary to search for (A4 save failed)"
else
    t0="$(now_ms)"
    search_out="$(timeout "$CLIENT_TIMEOUT" uv run --with httpx --with python-dotenv \
            python "$BRIDGE" search "Shared Memory install-verification canary ${marker}" 5 \
            --project install-verification 2>/dev/null)"
    t1="$(now_ms)"
    search_ms=$((t1 - t0))
    verdict="$(printf '%s' "$search_out" | python3 -c '
import json, sys
pg = int(sys.argv[1])
try:
    d = json.load(sys.stdin)
except Exception:
    print("BADJSON"); sys.exit(0)
if isinstance(d, dict):
    print("ERROR:" + str(d.get("message") or d.get("error") or "unexpected reply")[:200]); sys.exit(0)
hit = next((r for r in d if r.get("pg_id") == pg and r.get("tier") == "fact"), None)
if hit is None:
    print("MISSING"); sys.exit(0)
sc = hit.get("score")
if hit.get("ranked") and isinstance(sc, (int, float)):
    print("RANKED:%s" % sc)
else:
    print("DEGRADED")
' "$pg_id")"
    case "$verdict" in
        RANKED:*)
            ok "A5 canary found — reranker mode: real numeric score ${verdict#RANKED:}" ;;
        DEGRADED)
            ok "A5 canary found — DEGRADED mode declared honestly (null scores = vector order served); this passes, but the reranker is not ranking on this install" ;;
        MISSING)
            bad A5 "canary (pg_id $pg_id) not in the search results — retrieval is broken end to end" ;;
        ERROR:*)
            bad A5 "search failed: ${verdict#ERROR:}" ;;
        *)
            bad A5 "search returned no parseable JSON (timeout after ${CLIENT_TIMEOUT}s?)"
            search_ms=""   # a timeout is not a measurement — record null, not the ceiling
            ;;
    esac
fi

# ── A6 — baseline emission (measurement, never a gate) ────────────────────────
echo
echo "A6 — baseline emission (measurement, never a gate):"

if [[ "$token_missing" == "1" ]]; then
    warn "A6 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    warn "A6 skipped — gateway unreachable (see A1)"
else
    big_ms=""
    if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
        # WP-R2/WP-R4: zero saves in this mode, mirroring A4 — the realistic
        # canary is a save too. save_short/save_realistic stay null; the
        # baseline JSON's note explains why.
        ok "A6 re-baseline mode: no realistic-payload save performed — save_short/save_realistic are recorded null (write-path timing stays anchored to the install canary, same accepted trade as A4); canary-mode search is null in this mode; summary-search (search_rebaseline) ${search_rebaseline_ms:-?} ms"
    else
        # Realistic save ~3.5 KB, unique per run (timestamp embedded in the marker).
        big_content="$(python3 -c '
import sys
marker = sys.argv[1]
para = ("This is the postflight realistic-payload canary for the Shared Memory "
        "installation. It exists to time a representative save through the gateway: "
        "embedding a few kilobytes of text, writing the Tier 1 record, enqueueing the "
        "outbox row and mirroring the record into the graph. The content is filler by "
        "design and unique per run, so idempotency never short-circuits the timing. ")
print(("Shared Memory install-verification realistic canary " + marker + " — " + para * 12)[:3500])
' "$marker")"
        t0="$(now_ms)"
        big_out="$(do_save "$big_content")"
        t1="$(now_ms)"
        if [[ "$(printf '%s' "$big_out" | json_get status)" == "success" ]]; then
            big_ms=$((t1 - t0))
            ok "A6 realistic save timed (${big_ms} ms; short save ${short_ms:-?} ms; search ${search_ms:-?} ms)"
        else
            warn "A6 realistic save did not succeed — its timing is recorded as null"
        fi
    fi

    # D22: the timings above are a FLOOR, not a steady-state search time — on
    # a fresh install the reranker scored whatever tiny candidate pool
    # actually existed (often exactly the one canary A4 just saved), never a
    # real corpus. State the pool size the baseline was measured against so
    # a floor can never again be silently read as a steady-state reference.
    # Query scope MIRRORS what A5 actually searched: canary mode's search is
    # project-filtered (--project install-verification), so that project's
    # row count IS the candidate pool; re-baseline mode's search is
    # unfiltered whole-corpus, so the global row count is the honest scope
    # instead. Docker-optional, like the rest of A6 — a measurement that
    # cannot be taken is recorded null, never treated as a gate.
    corpus_scope=""
    corpus_technical_docs=""
    if command -v docker >/dev/null 2>&1; then
        if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
            corpus_scope="global"
            corpus_technical_docs="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                    "SELECT count(*) FROM technical_docs" 2>/dev/null | tr -d '[:space:]')"
        else
            corpus_scope="project:install-verification"
            corpus_technical_docs="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                    "SELECT count(*) FROM technical_docs WHERE metadata->>'project' = 'install-verification'" 2>/dev/null | tr -d '[:space:]')"
        fi
    fi

    base_file="$HOME/.shared-memory/postflight/baseline-$(date -u +%Y%m%dT%H%M%SZ).json"
    # >>> A6_BASELINE_WRITER (tests/test_postflight_a8.py extracts this block
    # VERBATIM and runs it standalone via subprocess with fixture argv/
    # stdin, feeding a scratch path for `path` -- same technique as
    # SELECT_SUMMARY_PHRASE/A8_BACKEND_INFO/A8_GRADE_COMPLETION above. This
    # lets D22's corpus_size field be verified without a live gateway or
    # Postgres/Neo4j, unlike the rest of A6's own timings (see this file's
    # own module docstring for what stays reference-install-only).
    written="$(printf '%s' "${health_full:-$anon_health}" | python3 -c '
import datetime, json, os, shutil, subprocess, sys
(path, short_ms, big_ms, search_ms, search_rebaseline_ms, fw, mode,
 corpus_scope, corpus_technical_docs, corpus_summaries) = sys.argv[1:11]
try:
    h = json.load(sys.stdin)
except Exception:
    h = {}

def secs(ms):
    try:
        return round(int(ms) / 1000.0, 3)
    except (TypeError, ValueError):
        return None

hw = {"threads": None, "mem_total_kb": None, "gpu": None}
try:
    hw["threads"] = int(subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip())
except Exception:
    pass
try:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal"):
                hw["mem_total_kb"] = int(line.split()[1])
                break
except Exception:
    pass
# Always record the actual VGA device — nvtop presence is a separate boolean
# (recording only "nvtop present" would yield LESS fingerprint when the tool
# exists than when it does not).
hw["nvtop"] = bool(shutil.which("nvtop"))
try:
    out = subprocess.run(["lspci"], capture_output=True, text=True).stdout
    hw["gpu"] = next((l.strip() for l in out.splitlines() if "vga" in l.lower()), None)
except Exception:
    pass

note = ("wall-clock through the client bridge; uv environment pre-warmed "
        "untimed; exactly one save per timing window; a timed-out "
        "operation records null, never the timeout ceiling")
if mode == "re-baseline":
    # R2 (decision:1435): search stays canary-search-only and is null here
    # -- the two workloads (project-filtered marker search vs unfiltered
    # whole-corpus phrase search) do not share a timing field even though
    # the earlier build made that mistake; search_rebaseline is the key A5
    # populates instead, only in this mode. A metric whose meaning
    # silently changes while its name stays constant is the known
    # monitor-class defect this avoids.
    note += (". re-baseline mode: save_short/save_realistic are null by "
             "design (no saves in this mode, W-P/fact:1402 lineage) -- "
             "write-path timing stays anchored to the original install "
             "canary; search is null in this mode (canary-search-only "
             "field); the A5 summary-search timing lands under its own "
             "key, search_rebaseline")

doc = {
    "mode": mode,
    "framework_version": fw or h.get("version"),
    "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "timings_s": {
        "save_short": secs(short_ms),
        "save_realistic": secs(big_ms),
        "search": secs(search_ms),
        "search_rebaseline": secs(search_rebaseline_ms),
        "note": note,
    },
    "backend_capability": h.get("backend_capability"),
    # R0-I (decision:1424), trigger "manual": the current CAPACITY record the
    # gateway already derived and stored, fetched verbatim off the
    # authenticated /health payload -- no re-derivation happens in bash. None
    # when the gateway has not derived one yet (fresh install, first probe
    # still in flight) or when only the anonymous payload was available.
    # Rendered identically in both modes -- the capacity verdict section
    # below is untouched by mode (WP-R4).
    "capacity": h.get("capacity"),
    "hardware": hw,
    # D22: the pool the timings above were measured against -- see the bash
    # comment just above this python block for the scope reasoning. A small
    # int here (frequently 1, on the first run of a fresh install) means the
    # timings are a FLOOR: how fast a search can possibly be, not how fast
    # it stays once the corpus is real. Compare timings across baselines
    # only when their corpus_size is comparable.
    "corpus_size": {
        "scope": corpus_scope or None,
        "technical_docs": int(corpus_technical_docs) if corpus_technical_docs.isdigit() else None,
        "community_summaries_live": int(corpus_summaries) if corpus_summaries.isdigit() else None,
        "note": ("the candidate pool timings_s was measured against, in the "
                 "scope corpus_scope names -- NOT a steady-state search "
                 "time: a fresh install project-scoped pool is often "
                 "exactly the canaries prior postflight runs saved "
                 "(frequently just 1), which is a FLOOR on search latency, "
                 "not a reference for a mature corpus. community_summaries_"
                 "live is populated only in re-baseline mode, mirroring the "
                 "mode-selection count printed at the top of this run."),
    },
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(doc, f, indent=2)
print(path)
' "$base_file" "${short_ms:-}" "${big_ms:-}" "${search_ms:-}" "${search_rebaseline_ms:-}" "${checkout_fw:-}" "$POSTFLIGHT_MODE" \
        "$corpus_scope" "${corpus_technical_docs:-}" "${live_summary_count:-}")"
    # <<< A6_BASELINE_WRITER
    if [[ -n "$written" ]]; then
        ok "A6 baseline written: $written"
        ok "A6 corpus size at this baseline: ${corpus_scope:-unknown scope} = ${corpus_technical_docs:-?} technical_docs row(s) — the candidate pool the timings above were measured against; a floor on a fresh install, not a steady-state reference"
    else
        warn "A6 baseline JSON could not be written (measurement lost, never a gate)"
    fi

    # Plain-language capacity verdict (fact:1425 A1 / decision:1424) — rendered
    # strictly from the record the gateway already published on /health; no
    # measurement or derivation happens here. Informational only: nothing in
    # this section may affect the exit code.
    cap_fields="$(printf '%s' "${health_full:-}" | python3 -c '
import json, sys
try:
    d = ((json.load(sys.stdin) or {}).get("capacity") or {}).get("derived") or {}
except Exception:
    d = {}
s, n = d.get("s_mean_s"), d.get("queue_bound")
exceeds, tolerable = d.get("single_search_exceeds_wait"), d.get("tolerable_wait_s")
print("UNDERIVABLE" if s is None or n is None else f"{s}|{n}|{exceeds}|{tolerable}")
' 2>/dev/null)"
    if [[ -z "$cap_fields" || "$cap_fields" == "UNDERIVABLE" ]]; then
        warn "Capacity verdict not derivable — the gateway has not published a capacity record yet (fresh install, first probe still in flight, or anonymous-only health); informational, never a gate"
    else
        # M6 (fix round): this is the RERANK-STAGE worst-case projection
        # (the probe's fixed 20-doc model, see hive_mind_proxy.py's own
        # docstring on _build_capacity_record) -- not a claim about "a
        # fully-ranked search" in general, and the baseline above is not
        # necessarily ranked on every install, so the old "(unranked:
        # measured in the baseline above)" parenthetical was false on a
        # healthy install where the baseline search WAS ranked.
        #
        # N2 (fix round 2): a bare "queue depth 0" is ambiguous -- it can
        # mean "a single search already exceeds the tolerable wait" or read
        # as "no data". single_search_exceeds_wait disambiguates which one
        # this is, and tolerable_wait_s names what the depth was actually
        # measured against (CAPACITY_TOLERABLE_WAIT_S) rather than leaving
        # the reader to guess or assume the shipped default.
        IFS='|' read -r cap_s cap_n cap_exceeds cap_tolerable <<< "$cap_fields"
        if [[ "$cap_exceeds" == "True" ]]; then
            ok "Capacity on this hardware: a single rerank-stage projection (~${cap_s}s) already exceeds the tolerable wait (${cap_tolerable}s) — queue depth 0 means exactly that"
        else
            ok "Capacity on this hardware: the rerank-stage worst-case projection is ~${cap_s}s; sustainable queue depth within the ${cap_tolerable}s tolerable wait: ${cap_n}"
        fi
        echo "     If that projection is too slow for your use, README §17's payload cap and GPU-encoder options are the dials."
    fi
fi

# ── A7 — conduct constraints (by construction) ────────────────────────────────
echo
echo "A7 — conduct constraints (by construction; see the spec):"
ok "A7 gateway-only traffic: every memory operation went through $GATEWAY_URL — never :8070/:8071 directly"
ok "A7 postflight's own store access was docker exec, read-only queries only — EXCEPT A3's incorporated verifier, which builds and drops a prefix-guarded throwaway database over TCP (its own documented contract, not this script's)"
ok "A7 writes outside the gateway path: A6's baseline JSON, plus A3's throwaway verification database (created and dropped by verify_schema_init.py)"
ok "A7 canaries live under the reserved project 'install-verification' and STAY in the corpus — the install's birth certificate"

# ── A8 — reasoning-backend liveness, end to end ────────────────────────────────
echo
echo "A8 — reasoning-backend liveness, end to end:"
# D23 (v0.9.24): hive_mind_proxy.py joined a configured backend base and the
# incoming request path with a naive concat — f"{target_base}{request.
# rel_url}" — so a base ending in /v1 (every cloud provider's documented
# shape, and our own shipped .env.example before this fix) doubled into
# /v1/v1/chat/completions, which providers answer with 404. It failed
# COMPLETELY SILENTLY: a 404 is never billed, so neither the token counters
# nor the provider dashboard showed anything; /health reported the backend
# "ok" (that check is a bare /v1/models LIVENESS probe, a different code
# path — see _v1_models_probe_url — that happened not to share the bug);
# both daemons said "running"; and every assertion that existed at the time
# (there was no A8 yet) passed green while REM retried the same dead
# completion every 30 s for 45 minutes, achieving nothing. A8 exists
# specifically to close that hole: it is the ONE assertion that drives a
# REAL completion through the exact proxy join D23 broke (_upstream_url) —
# never a /health field, a /v1/models probe, or a bare TCP connect, none of
# which would have caught D23 (all three stayed green throughout the live
# incident).
reasoning_ms=""
# ND6: carried into the Summary block below by the SKIP_* case arm — never
# re-derived there, and never keyed off afail[] (a named skip deliberately
# leaves afail[A8] untouched, so afail[] alone cannot distinguish "passed
# clean" from "passed with A8 skipped").
a8_skip_declaration=""
if [[ "$token_missing" == "1" ]]; then
    warn "A8 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    bad A8 "skipped — gateway unreachable (see A1)"
else
    # IFS='|' read, matching the idiom already used for cap_fields below —
    # a8_backend_info's third field (the full status summary) can be empty
    # (no backends reported at all), which a bash `#*|`/`%%|*` split alone
    # handles awkwardly once there are two delimiters.
    backend_info="$(printf '%s' "${health_full:-}" | a8_backend_info)"
    IFS='|' read -r backend_count backend_urls backend_status_summary <<< "$backend_info"
    if [[ ! "$backend_count" =~ ^[0-9]+$ || "$backend_count" -lt 1 ]]; then
        # NEVER a gate: no backend is reported HEALTHY right now (per
        # /health's own llm_backends status map — see a8_backend_info's own
        # comment for the full fix-round reasoning and status vocabulary).
        # This is the documented non-fatal state (AGENTS.md Phase 7:
        # "llm":"down" blocks dreaming only, never saves/search), already
        # surfaced by A1's own "llm" field — this branch must never call
        # bad(), only warn().
        warn "A8 skipped — no reasoning backend reported healthy on this gateway right now (per /health's llm_backends status map${backend_status_summary:+: $backend_status_summary}) — this is the documented non-fatal no-working-LLM state; A8 can never fail an install for it"
    else
        # A minimal REAL completion, not a probe: small deterministic
        # prompt, small max_tokens, same route (POST $GATEWAY_URL/v1/chat/
        # completions) and body shape every daemon actually sends
        # (rem_loop.py, consolidation_loop.py) — LLM_MODEL, defaulting to
        # "local-model" exactly like them, so the model id sent here is
        # never a postflight-only guess that could mask a real routing
        # difference.
        a8_model="$(read_env LLM_MODEL)"
        [[ -z "$a8_model" ]] && a8_model="local-model"
        a8_body_file="$(mktemp)"
        a8_resp_file="$(mktemp)"
        a8_header_file="$(mktemp)"
        python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": "Reply with exactly one word: ok"}],
    "max_tokens": 16,
    "temperature": 0,
}))
' "$a8_model" > "$a8_body_file"

        t0="$(now_ms)"
        if [[ "$auth_on" == "1" && -n "${AGENT_TOKEN:-}" ]]; then
            # Token via curl config on stdin, never argv — same idiom as A1
            # (argv is world-readable in /proc/<pid>/cmdline for the
            # request's lifetime).
            a8_status="$(curl -s --compressed --max-time "$CLIENT_TIMEOUT" -K - \
                    -H "Content-Type: application/json" \
                    --data-binary @"$a8_body_file" \
                    -D "$a8_header_file" -o "$a8_resp_file" -w '%{http_code}' \
                    "$GATEWAY_URL/v1/chat/completions" \
                    <<< "header = \"Authorization: Bearer $AGENT_TOKEN\"" 2>/dev/null)"
        else
            a8_status="$(curl -s --compressed --max-time "$CLIENT_TIMEOUT" \
                    -H "Content-Type: application/json" \
                    --data-binary @"$a8_body_file" \
                    -D "$a8_header_file" -o "$a8_resp_file" -w '%{http_code}' \
                    "$GATEWAY_URL/v1/chat/completions" 2>/dev/null)"
        fi
        t1="$(now_ms)"
        rm -f "$a8_body_file"

        # W4 (§6.7): the second edit site, OUTSIDE the verbatim-extracted
        # A8_GRADE_COMPLETION block — a passed-through UPSTREAM 422 must
        # never be misread as a gateway refusal, so the discriminator needs
        # the header the gateway itself stamps (hive_mind_proxy.py:1696).
        # `tail -1` takes the LAST occurrence in case of a redirect chain.
        a8_fault_origin="$(grep -i '^X-SM-Fault-Origin:' "$a8_header_file" 2>/dev/null \
                | tail -1 | cut -d: -f2- | tr -d ' \t\r\n')"
        rm -f "$a8_header_file"

        a8_verdict="$(a8_grade_completion "$a8_status" "$a8_fault_origin" < "$a8_resp_file")"
        rm -f "$a8_resp_file"

        case "$a8_verdict" in
            OK)
                reasoning_ms=$((t1 - t0))
                ok "A8 real completion returned through the gateway proxy path (model $a8_model, ${reasoning_ms} ms)"
                ;;
            EMPTY)
                bad A8 "gateway returned HTTP 200 but no usable completion content or reasoning_content — a 200 with both fields empty/absent is a failure, not a pass. Healthy backend(s) at request time: ${backend_urls:-<none>}"
                ;;
            HTTP_404)
                bad A8 "gateway returned 404 from the reasoning-backend proxy path — the known cause (D23) is a doubled /v1 path segment when a configured base already ends in /v1. Healthy backend(s) at request time: ${backend_urls:-<none>}"
                ;;
            SKIP_*)
                # Ruling B(i) (§6.7): a NAMED non-fatal skip, never a FAIL —
                # postflight exits 0 with this note. Fit and every other
                # routing/join defect (the D23 class) stay FATAL below.
                # ND6: this is the ONE place a8_skip_declaration is set — the
                # Summary block reads it verbatim, never re-deriving the
                # verdict or keying off afail[] (untouched by a skip).
                a8_skip_declaration="${a8_verdict#SKIP_}"
                warn "A8 skipped: ${a8_skip_declaration} — documented post-0.9.81 state; run check_config.py to see per-backend declaration status. Healthy backend(s) at request time: ${backend_urls:-<none>}"
                ;;
            HTTP_*)
                bad A8 "gateway returned HTTP ${a8_verdict#HTTP_} from the reasoning-backend proxy path. Healthy backend(s) at request time: ${backend_urls:-<none>}"
                ;;
            *)
                bad A8 "no response from $GATEWAY_URL/v1/chat/completions (timeout after ${CLIENT_TIMEOUT}s, or connection failed). Healthy backend(s) at request time: ${backend_urls:-<none>}"
                ;;
        esac
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
fail=0
for a in A1 A2 A3 A4 A5 A8; do
    [[ "${afail[$a]:-0}" == "1" ]] && fail=1
done
if [[ "$fail" -eq 0 ]]; then
    if [[ -n "${a8_skip_declaration:-}" ]]; then
        # ND6: the skip is loud at the A8 check itself, but must survive a
        # scrolled-past terminal — a passing run with A8 named-skipped reads
        # identically to a full pass unless the summary says otherwise.
        grn "Postflight passed (A1–A5, A8 skipped: ${a8_skip_declaration}). The install works end to end for what is declared; A6's baseline is your performance reference."
    else
        grn "Postflight passed (A1–A5, A8). The install works end to end; A6's baseline is your performance reference."
    fi
else
    failed=""
    for a in A1 A2 A3 A4 A5 A8; do
        [[ "${afail[$a]:-0}" == "1" ]] && failed="$failed $a"
    done
    red "Postflight failed —$failed did not pass. Resolve the ✗ items above, then re-run."
fi
exit "$fail"
