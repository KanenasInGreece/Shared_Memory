#!/usr/bin/env bash
#
# postflight.sh — verify an installed Shared Memory stack end to end.
#
# Implements A1–A9 of shared-memory/Documentation/postflight.md. Where this script and that document disagree, the document wins.
#
# A1 liveness and shape. A2 contract. A3 schema truth. A4 write path. A5 read path, graded. A6 baseline emission, never a gate. A7 conduct, stated not tested. A8 a real completion through the gateway, skipped when no backend is healthy. A9 encoder window: advertised context and a full payload (decision:2540).
#
# Exit 0 iff A1–A5, A8 and A9 all pass. A8 skips, and does not gate, when no reasoning backend is healthy. Run after first install and after every upgrade.
#
# Read AGENT_TOKEN from a minted agent's skill .env. Never `. file` (that executes it) and never cat or grep it (fact:1499):
#   AGENT_ENV=${AGENT_ENV:-$HOME/.claude/skills/shared-memory/.env}
#   AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' "$AGENT_ENV" | head -1); export AGENT_TOKEN
#   bash shared-memory/scripts/postflight.sh

set -uo pipefail   # not -e: we run every assertion and summarise, never abort early

# --help prints this header and exits. Any other argument is refused, because this script used to ignore flags and run the assertions anyway.
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
# shared-memory/.env first, then the pre-0.6 repo-root file, matching the gateway.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"
PG_CONTAINER="${PG_CONTAINER:-postgres-vector}"
NEO4J_CONTAINER="${NEO4J_CONTAINER:-neo4j-memory}"
PG_DB="${PG_DB:-agent_data}"
BRIDGE="$REPO_ROOT/shared-memory/scripts/memory_bridge.py"
# Saves can take more than 60s on a small host. That slowness is A6's measurement, not an A4 or A5 failure, so the client timeout stays generous.
CLIENT_TIMEOUT="${CLIENT_TIMEOUT:-240}"

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
grn()   { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()   { printf '\033[33m%s\033[0m\n' "$*"; }

declare -A afail
ok()   { grn "  ✓ $*"; }
warn() { ylw "  ! $*"; }
bad()  { local a="$1"; shift; red "  ✗ $a $*"; afail["$a"]=1; }

# Read one key without sourcing .env. The shared parser does not do bash quote-matching.
read_env() { python3 "$SCRIPT_DIR/read_env_key.py" "$ENV_FILE" "$1"; }

# JSON helpers are python3 one-liners. python3 is already an install prerequisite, so this adds no dependency.
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
# Read llm_backends, the status map, not config.llm_backends. The configured list is never empty, so keying on it made an LLM-less install fail A8. Only status "ok" is healthy, and the map's keys are URLs, not credentials.
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
# A 200 with empty content is not a completion, unless reasoning_content is a non-empty string: a thinking model can spend the whole token budget there and still prove the proxy join. A structured reasoning object must not pass.
# SKIP_ is a named non-fatal 422 only when the body says no_eligible_backend, names a declaration, is not a fit failure, and X-SM-Fault-Origin is gateway. An upstream 422 is not that refusal.
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

# >>> A9_GRADE_WINDOW (tests/test_postflight_a9.py extracts this block
# Prints VERDICT|DETAIL|WARNING from encoder_window. Verdicts: OK, STILL_NULL, FAIL_WINDOW_SHORT, FAIL_EMBED_OVERRUN, FAIL_MISSING_BLOCK, UNPARSEABLE, EMPTY.
a9_grade_window() {
    python3 -c '
import json, sys

raw = sys.stdin.read().strip()
if not raw:
    print("EMPTY||")
    sys.exit(0)

try:
    data = json.loads(raw)
except Exception:
    print("UNPARSEABLE||")
    sys.exit(0)

checks = data if isinstance(data, dict) else {}
win = checks.get("encoder_window")
if not isinstance(win, dict):
    print("FAIL_MISSING_BLOCK|encoder_window block missing from /health|")
    sys.exit(0)

required = win.get("required_tokens", 8192)
embed_max_chars = win.get("embed_max_chars", 24570)
embed = win.get("embedder") or {}
rerank = win.get("reranker") or {}

embed_adv = embed.get("advertised_tokens")
embed_ok = embed.get("full_payload_ok")

rerank_adv = rerank.get("advertised_tokens")
rerank_ok = rerank.get("full_payload_ok")

# 1. Check if embedder probe is still in-flight
if embed_ok is None:
    print(f"STILL_NULL_EMBED|embedder full_payload_ok is null (required: {required})|")
    sys.exit(0)

# 2. Check embedder advertised window short
if isinstance(embed_adv, int) and embed_adv < required:
    print(f"FAIL_WINDOW_SHORT|embedder advertised window {embed_adv} < required {required} tokens (--max-model-len or -c required: {required}, EMBED_MAX_CONTEXT_TOKENS={required}, check for leftover EMBED_MAX_CHARS)|")
    sys.exit(0)

# 3. Check embedder empirical full payload failure
if embed_ok is False:
    print(f"FAIL_EMBED_OVERRUN|embedder full payload test failed (full_payload_ok is false). Upstream encoder must support --max-model-len {required} or -c {required} (EMBED_MAX_CONTEXT_TOKENS={required}); check for leftover EMBED_MAX_CHARS={embed_max_chars}|")
    sys.exit(0)

# 1b. Check if reranker probe is still in-flight
# Wait until rerank advertised is non-null or rerank empirical is non-null (skip-null rule)
if rerank_adv is None and rerank_ok is None:
    print(f"STILL_NULL_RERANK|reranker probe in-flight (advertised and full_payload_ok are null)|")
    sys.exit(0)

# 4. Check reranker advertised window short
if isinstance(rerank_adv, int) and rerank_adv < required:
    print(f"FAIL_WINDOW_SHORT|reranker advertised window {rerank_adv} < required {required} tokens (--max-model-len or -c required: {required}, EMBED_MAX_CONTEXT_TOKENS={required}, check for leftover EMBED_MAX_CHARS)|")
    sys.exit(0)

# 5. Reranker empirical false is warn-only
warn_msg = ""
if rerank_ok is False:
    warn_msg = "reranker full payload probe failed (full_payload_ok: false); search will fall back or truncate on large candidate sets"

adv_str = f"advertised: {embed_adv}" if embed_adv is not None else "advertised: unprobed"
print(f"OK|embed full_payload_ok: true, {adv_str}, required: {required}|{warn_msg}")
'
}
# <<< A9_GRADE_WINDOW

# Use bash $EPOCHREALTIME, not `date`. uutils ignores the %3N width and returns nanoseconds, which inflates every timing by a million.
now_ms() { local t=$EPOCHREALTIME; echo $(( ${t%.*} * 1000 + 10#${t#*.} / 1000 )); }

# One canary save per call, so a timing window never contains two. new_project stays on because a registered project ignores it; a spelling clash is an operator problem, not a retry.
CANARY_META='{"project": "install-verification", "new_project": true}'
do_save() {  # do_save <content>  — prints the bridge's JSON reply
    timeout "$CLIENT_TIMEOUT" uv run --with httpx \
            python "$BRIDGE" save "$1" "$CANARY_META" 2>/dev/null
}

echo "Shared Memory — postflight verification (spec: shared-memory/Documentation/postflight.md)"
echo

# Canary mode while no live community summary exists; re-baseline once one does (fact:1402/decision:1403). If the count cannot be read, stay in canary mode rather than skip a check that was not shown to be safe to skip.
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
telemetry_full=""   # the numbers endpoint — A6's baseline and the capacity verdict read it

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
print(",".join(k for k in ("nrem_daemon_process", "backend_capability", "dependencies") if k not in d))
' )"
            if [[ -z "$missing" ]]; then
                ok "A1 authenticated payload carries the full shape"
            elif [[ "$missing" == "UNPARSEABLE" ]]; then
                bad A1 "authenticated /health did not return JSON"
            else
                bad A1 "authenticated payload is missing expected keys: $missing — token not resolving, or health assembly broken"
            fi
        else
            # One message, then one skip line each. In re-baseline mode A4 makes no gateway call, so a missing token must not pre-fail it (decision:1435). A8 needs a token in both modes.
            # Point at the sed read in postflight.md. An export line would put the bearer on the command line and in shell history.
            if [[ "$POSTFLIGHT_MODE" == "re-baseline" ]]; then
                bad A1 "auth is configured but AGENT_TOKEN is not set — read it from a minted agent's skill .env per postflight.md's Quick Start (AGENT_ENV + sed, never a pasted export) and re-run. A5, A6 and A8 are skipped for this same missing token (A4 needs no token in re-baseline mode)."
            else
                bad A1 "auth is configured but AGENT_TOKEN is not set — read it from a minted agent's skill .env per postflight.md's Quick Start (AGENT_ENV + sed, never a pasted export) and re-run. A4, A5, A6 and A8 are skipped for this same missing token."
                afail[A4]=1
            fi
            token_missing=1
            afail[A5]=1
            afail[A8]=1
            afail[A9]=1
        fi
    else
        missing="$(printf '%s' "$anon_health" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("UNPARSEABLE"); sys.exit(0)
print(",".join(k for k in ("nrem_daemon_process", "backend_capability", "dependencies") if k not in d))
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
    uv run --with httpx python "$BRIDGE" --version >/dev/null 2>&1

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
        # Probe the three newest live rows, not one summary (decision:1439). Three is measured: one of 21 rows failed alone, so three distinct rows still catch a real retrieval break (fact:1438). json_agg of no rows is NULL, hence the COALESCE.
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
                cand_search_out="$(timeout "$CLIENT_TIMEOUT" uv run --with httpx \
                        python "$BRIDGE" search "$cand_phrase" 20 2>/dev/null)"
                # A missing "ranked" key is the embedder-down fallback, not an honest ranked:false (decision:1435). Report the count actually returned, not a hardcoded 20 (decision:1439).
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
                        # Stop on the first candidate. A dead embedder is not a per-row miss, and re-baseline A4 saves nothing that would have caught it.
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
    search_out="$(timeout "$CLIENT_TIMEOUT" uv run --with httpx \
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

    # Record the pool A5 actually searched, because a fresh-install timing is a floor, not a steady-state search. A count that cannot be taken is null, not a gate.
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

    # The full capacity record is on /memory/telemetry, not /health. Fetch it once, with the token on stdin, because argv is world-readable in /proc.
    if [[ -n "${AGENT_TOKEN:-}" ]]; then
        telemetry_full="$(curl -s --compressed --max-time 15 -K - "$GATEWAY_URL/memory/telemetry" <<< "header = \"Authorization: Bearer $AGENT_TOKEN\"" || true)"
    elif [[ "$auth_on" != "1" ]]; then
        # Auth-off install: the numbers endpoint is served anonymously, exactly
        # as /health is above — so the verdict renders in both modes.
        telemetry_full="$(curl -s --compressed --max-time 15 "$GATEWAY_URL/memory/telemetry" || true)"
    fi

    base_file="$HOME/.shared-memory/postflight/baseline-$(date -u +%Y%m%dT%H%M%SZ).json"
    # >>> A6_BASELINE_WRITER (tests/test_postflight_a8.py extracts this block
    # The test runs this block alone, so corpus_size can be checked without a live gateway.
    written="$(printf '%s' "${health_full:-$anon_health}" | python3 -c '
import datetime, json, os, shutil, subprocess, sys
(path, short_ms, big_ms, search_ms, search_rebaseline_ms, fw, mode,
 corpus_scope, corpus_technical_docs, corpus_summaries, telemetry_raw) = sys.argv[1:12]
try:
    h = json.load(sys.stdin)
except Exception:
    h = {}
try:
    t = (json.loads(telemetry_raw) or {}).get("telemetry") or {}
except Exception:
    t = {}

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
    # gateway already derived and stored, fetched verbatim -- no re-derivation
    # happens in bash. /health carries the sizing a client needs to set its own
    # timeouts; /memory/telemetry carries the whole record, fingerprint and
    # measured probe included, which is what makes a baseline comparable across
    # hardware. None when the gateway has not derived one yet (fresh install,
    # first probe still in flight) or when the payload was not available --
    # anonymous for capacity, no token for capacity_telemetry. Rendered
    # identically in both modes: the capacity verdict below is untouched by
    # mode (WP-R4).
    "capacity": h.get("capacity"),
    "capacity_telemetry": t.get("capacity"),
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
        "$corpus_scope" "${corpus_technical_docs:-}" "${live_summary_count:-}" "${telemetry_full:-}")"
    # <<< A6_BASELINE_WRITER
    if [[ -n "$written" ]]; then
        ok "A6 baseline written: $written"
        ok "A6 corpus size at this baseline: ${corpus_scope:-unknown scope} = ${corpus_technical_docs:-?} technical_docs row(s) — the candidate pool the timings above were measured against; a floor on a fresh install, not a steady-state reference"
    else
        warn "A6 baseline JSON could not be written (measurement lost, never a gate)"
    fi

    # Plain-language capacity verdict (fact:1425 A1 / decision:1424) — rendered
    # strictly from the record the gateway already published on
    # /memory/telemetry; no measurement or derivation happens here.
    # Informational only: nothing in this section may affect the exit code.
    cap_fields="$(printf '%s' "${telemetry_full:-}" | python3 -c '
import json, sys
try:
    t = (json.load(sys.stdin) or {}).get("telemetry") or {}
    d = (t.get("capacity") or {}).get("derived") or {}
except Exception:
    d = {}
s, n = d.get("s_mean_s"), d.get("queue_bound")
exceeds, tolerable = d.get("single_search_exceeds_wait"), d.get("tolerable_wait_s")
print("UNDERIVABLE" if s is None or n is None else f"{s}|{n}|{exceeds}|{tolerable}")
' 2>/dev/null)"
    if [[ -z "$cap_fields" || "$cap_fields" == "UNDERIVABLE" ]]; then
        warn "Capacity verdict not derivable — the gateway has not published a capacity record yet (fresh install, first probe still in flight), or auth is on and no AGENT_TOKEN was set to read /memory/telemetry with; informational, never a gate"
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
# A8 posts a real completion through the proxy join. A doubled /v1 path 404'd silently while /health, a models probe, and every older assertion stayed green.
reasoning_ms=""
# The summary reads this skip name. A named skip leaves afail[A8] unset, so that array cannot tell a clean pass from a skipped A8.
a8_skip_declaration=""
if [[ "$token_missing" == "1" ]]; then
    warn "A8 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    bad A8 "skipped — gateway unreachable (see A1)"
else
    # Read on '|' because the status field can be empty, and a prefix/suffix trim mishandles two delimiters.
    backend_info="$(printf '%s' "${health_full:-}" | a8_backend_info)"
    IFS='|' read -r backend_count backend_urls backend_status_summary <<< "$backend_info"
    if [[ ! "$backend_count" =~ ^[0-9]+$ || "$backend_count" -lt 1 ]]; then
        # No healthy backend is not a failure. A1 already reports llm down, and this branch must warn, never call bad().
        warn "A8 skipped — no reasoning backend reported healthy on this gateway right now (per /health's llm_backends status map${backend_status_summary:+: $backend_status_summary}) — this is the documented non-fatal no-working-LLM state; A8 can never fail an install for it"
    else
        # Same route and body the daemons send, including the default model id. A postflight-only model would hide a routing difference.
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

        # Read the fault-origin header here, outside the graded function. An upstream 422 is not a gateway refusal, and tail -1 keeps the last hop of a redirect.
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
                # A named skip exits 0. Fit and other join defects stay fatal below. This is the only place the skip name is set; the summary reads it and does not look at afail.
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

# ── A9 — encoder window contract ───────────────────────────────────────────────
echo "A9 — encoder window contract:"

if [[ "$token_missing" == "1" ]]; then
    warn "A9 skipped — AGENT_TOKEN missing (see A1)"
elif [[ "$gateway_down" == "1" ]]; then
    bad A9 "skipped — gateway unreachable (see A1)"
else
    embedder_down="$(printf '%s' "${health_full:-}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    deps = d.get("dependencies", {})
    state = deps.get("embedder", {}).get("state")
    probe = d.get("embedder")
    if state == "down" or probe == "down" or (probe and probe != "ok" and not str(probe).startswith("http_")):
        print("1")
    else:
        print("0")
except Exception:
    print("0")
' 2>/dev/null || echo "0")"

    if [[ "$embedder_down" == "1" ]]; then
        warn "A9 skipped — embedder backend is down (see A1)"
    else
        ceiling_s="$(python3 -c '
import sys, math
sys.path.insert(0, "'"$SCRIPT_DIR"'")
try:
    import dream_telemetry as dt
    e_ceil = dt.embed_ceiling(dt.EMBED_MAX_CHARS)
    r_ceil = dt.rerank_ceiling(["x" * int(dt.RERANK_MAX_DOC_CHARS)])
    print(int(math.ceil(e_ceil + r_ceil)) + 5)
except Exception:
    print(60)
' 2>/dev/null || echo 60)"

        start_s=$SECONDS
        grade_res="$(printf '%s' "${health_full:-}" | a9_grade_window)"
        IFS='|' read -r verdict detail warn_part <<< "$grade_res"

        while [[ ( "$verdict" == "STILL_NULL_EMBED" || "$verdict" == "STILL_NULL_RERANK" || "$verdict" == "STILL_NULL" ) && $(( SECONDS - start_s )) -le "$ceiling_s" ]]; do
            sleep 2
            if [[ "$auth_on" == "1" && -n "${AGENT_TOKEN:-}" ]]; then
                health_full="$(curl -s --compressed --max-time 15 -K - "$GATEWAY_URL/health" <<< "header = \"Authorization: Bearer $AGENT_TOKEN\"" || true)"
            else
                health_full="$(curl -s --compressed --max-time 15 "$GATEWAY_URL/health" || true)"
            fi
            grade_res="$(printf '%s' "${health_full:-}" | a9_grade_window)"
            IFS='|' read -r verdict detail warn_part <<< "$grade_res"
        done

        case "$verdict" in
            OK)
                if [[ -n "$warn_part" ]]; then
                    warn "A9 $warn_part"
                fi
                ok "A9 encoder window contract verified ($detail)"
                ;;
            STILL_NULL_RERANK)
                warn "A9 reranker probe in-flight timed out after ${ceiling_s}s (skip-null); search will fall back or truncate on large candidate sets"
                ok "A9 encoder window contract verified ($detail)"
                ;;
            STILL_NULL|STILL_NULL_EMBED)
                bad A9 "encoder window probe timed out after ${ceiling_s}s (still null). Verify upstream encoder is started with --max-model-len or -c matching EMBED_MAX_CONTEXT_TOKENS, and check for leftover EMBED_MAX_CHARS"
                ;;
            FAIL_WINDOW_SHORT|FAIL_EMBED_OVERRUN)
                bad A9 "$detail"
                ;;
            *)
                bad A9 "encoder window verification failed: $detail (verdict: $verdict)"
                ;;
        esac
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
fail=0
for a in A1 A2 A3 A4 A5 A8 A9; do
    [[ "${afail[$a]:-0}" == "1" ]] && fail=1
done
if [[ "$fail" -eq 0 ]]; then
    if [[ -n "${a8_skip_declaration:-}" ]]; then
        # ND6: the skip is loud at the A8 check itself, but must survive a
        # scrolled-past terminal — a passing run with A8 named-skipped reads
        # identically to a full pass unless the summary says otherwise.
        grn "Postflight passed (A1–A5, A8 skipped: ${a8_skip_declaration}, A9). The install works end to end for what is declared; A6's baseline is your performance reference."
    else
        grn "Postflight passed (A1–A5, A8 and A9). The install works end to end; A6's baseline is your performance reference."
    fi
else
    failed=""
    for a in A1 A2 A3 A4 A5 A8 A9; do
        [[ "${afail[$a]:-0}" == "1" ]] && failed="$failed $a"
    done
    red "Postflight failed —$failed did not pass. Resolve the ✗ items above, then re-run."
fi
exit "$fail"
