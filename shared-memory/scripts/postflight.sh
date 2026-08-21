#!/usr/bin/env bash
#
# postflight.sh — verify an installed Shared Memory stack END TO END.
#
# Implements assertions A1–A7 of shared-memory/Documentation/postflight.md.
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
#
# Exit 0 iff A1–A5 all pass. Run after first install (AGENTS.md Phase 9) and
# after every upgrade:
#
#   export AGENT_TOKEN=...   # auth-on installs: any minted agent token,
#                            # from that agent's skill .env
#   bash shared-memory/scripts/postflight.sh

set -uo pipefail   # not -e: we run every assertion and summarise, never abort early

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
    return " ".join(words[:8])

phrase = select_phrase(sys.stdin.read())
if phrase:
    print(phrase)
else:
    sys.exit(1)
'
}
# <<< SELECT_SUMMARY_PHRASE

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
anon_health="$(curl -s --max-time 15 "$GATEWAY_URL/health" || true)"
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
            health_full="$(curl -s --max-time 15 -K - "$GATEWAY_URL/health" <<< "header = \"Authorization: Bearer $AGENT_TOKEN\"" || true)"
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
            # ONE clear message; A4/A5/A6 are marked here and print a single
            # skip line each — never a cascade of confusing errors.
            bad A1 "auth is configured but AGENT_TOKEN is not set — export AGENT_TOKEN=<any minted agent token, from that agent's skill .env> and re-run. A4, A5 and A6 are skipped for this same missing token."
            token_missing=1
            afail[A4]=1; afail[A5]=1
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
        # Most-recently-updated live row, either kind — one query, one row;
        # json_build_object keeps embedded newlines/unicode JSON-escaped so
        # this stays a single -tAc line (same reasoning as A4's psql calls).
        summary_row="$(docker exec "$PG_CONTAINER" psql -U postgres -d "$PG_DB" -tAc \
                "SELECT json_build_object('id', id, 'content', content, 'kind', COALESCE(metadata->>'kind','thematic')) FROM community_summaries WHERE NOT superseded ORDER BY updated_at DESC LIMIT 1" 2>/dev/null)"
        summary_id="$(printf '%s' "$summary_row" | json_get id)"
        summary_kind="$(printf '%s' "$summary_row" | json_get kind)"
        summary_content="$(printf '%s' "$summary_row" | json_get content)"
        if [[ ! "$summary_id" =~ ^[0-9]+$ ]]; then
            bad A5 "re-baseline mode selected on a nonzero live-summary count, but no live non-superseded community_summaries row could be read just now — the count and this read disagree; the corpus may have changed between the two, or the store is unreachable"
        else
            summary_ref_type="summary"
            [[ "$summary_kind" == "insight" ]] && summary_ref_type="insight"
            summary_ref="${summary_ref_type}:${summary_id}"
            phrase="$(printf '%s' "$summary_content" | select_summary_phrase)"
            if [[ -z "$phrase" ]]; then
                bad A5 "selected $summary_ref has no extractable phrase (content yields no words after cleaning) — cannot search for it"
            else
                t0="$(now_ms)"
                search_out="$(timeout "$CLIENT_TIMEOUT" uv run --with httpx --with python-dotenv \
                        python "$BRIDGE" search "$phrase" 5 2>/dev/null)"
                t1="$(now_ms)"
                search_ms=$((t1 - t0))
                verdict="$(printf '%s' "$search_out" | python3 -c '
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
ranked = bool(d[0].get("ranked"))
hit = next((r for r in d if r.get("ref") == ref), None)
if ranked:
    print("RANKED_HIT" if hit is not None else "RANKED_MISS")
else:
    print("DEGRADED")
' "$summary_ref")"
                case "$verdict" in
                    RANKED_HIT)
                        ok "A5 re-baseline: $summary_ref found in ranked results for phrase \"$phrase\"" ;;
                    RANKED_MISS)
                        bad A5 "re-baseline: $summary_ref not in the ranked results for phrase \"$phrase\" — the read path is broken for real Tier-3 content (reranker is live, so this is not the degraded-mode waiver)" ;;
                    DEGRADED)
                        ok "A5 re-baseline: results returned, DEGRADED mode declared honestly — Tier-3 narratives are omitted in degraded mode by design (measured in the 2026-08-21 stress test; v0.8.54 ruling \"ranked, not guaranteed\"); the $summary_ref presence assertion is WAIVED, not silently passed" ;;
                    EMPTY)
                        bad A5 "re-baseline: search for phrase \"$phrase\" returned zero results — even degraded mode requires results returned" ;;
                    ERROR:*)
                        bad A5 "re-baseline search failed: ${verdict#ERROR:}" ;;
                    *)
                        bad A5 "re-baseline search returned no parseable JSON (timeout after ${CLIENT_TIMEOUT}s?)"
                        search_ms=""   # a timeout is not a measurement — record null, not the ceiling
                        ;;
                esac
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
        ok "A6 re-baseline mode: no realistic-payload save performed — save_short/save_realistic are recorded null (write-path timing stays anchored to the install canary, same accepted trade as A4); search ${search_ms:-?} ms"
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

    base_file="$HOME/.shared-memory/postflight/baseline-$(date -u +%Y%m%dT%H%M%SZ).json"
    written="$(printf '%s' "${health_full:-$anon_health}" | python3 -c '
import datetime, json, os, shutil, subprocess, sys
path, short_ms, big_ms, search_ms, fw, mode = sys.argv[1:7]
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
    note += (". re-baseline mode: save_short/save_realistic are null by "
             "design (no saves in this mode, W-P/fact:1402 lineage) -- "
             "write-path timing stays anchored to the original install "
             "canary; search is the summary-search timing (A5), not a "
             "canary search")

doc = {
    "mode": mode,
    "framework_version": fw or h.get("version"),
    "date": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "timings_s": {
        "save_short": secs(short_ms),
        "save_realistic": secs(big_ms),
        "search": secs(search_ms),
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
}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w") as f:
    json.dump(doc, f, indent=2)
print(path)
' "$base_file" "${short_ms:-}" "${big_ms:-}" "${search_ms:-}" "${checkout_fw:-}" "$POSTFLIGHT_MODE")"
    if [[ -n "$written" ]]; then
        ok "A6 baseline written: $written"
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

# ── Summary ───────────────────────────────────────────────────────────────────
echo
fail=0
for a in A1 A2 A3 A4 A5; do
    [[ "${afail[$a]:-0}" == "1" ]] && fail=1
done
if [[ "$fail" -eq 0 ]]; then
    grn "Postflight passed (A1–A5). The install works end to end; A6's baseline is your performance reference."
else
    failed=""
    for a in A1 A2 A3 A4 A5; do
        [[ "${afail[$a]:-0}" == "1" ]] && failed="$failed $a"
    done
    red "Postflight failed —$failed did not pass. Resolve the ✗ items above, then re-run."
fi
exit "$fail"
