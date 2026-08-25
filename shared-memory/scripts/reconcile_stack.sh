#!/usr/bin/env bash
#
# reconcile_stack.sh — show, and on your say-so close, the gap between the
# image pins in postgres_neo4j_limits.yaml and the containers actually
# running on this host.
#
#   bash shared-memory/scripts/reconcile_stack.sh              # table, then prompt if there is drift
#   bash shared-memory/scripts/reconcile_stack.sh --dry-run    # table only, never prompts, changes nothing
#   bash shared-memory/scripts/reconcile_stack.sh --yes        # table, then reconcile without prompting
#
# Env overrides: GATEWAY_URL, COMPOSE_FILE.
#
# WHY THIS SCRIPT EXISTS. update_framework.sh moves code, schema and skills
# forward, but never the containers: a compose image pin moving (v0.9.55
# repinned pgvector and neo4j) leaves a host running an updated gateway
# against OLD store images until someone runs `docker compose ... pull &&
# up -d` by hand. The operator ruled this stays a STANDALONE script the
# operator runs when they choose, never a step the update path takes on its
# own — a host may have other legacy problems to work through first, and
# recreating a database container is not a step to take silently.
#
# WHAT THIS NEVER DOES: edit shared-memory/.env, run a migration, or restart
# the gateway. The gateway is a separate process that reconnects to
# Postgres/Neo4j on its own once they come back — this script only prints
# the /health line to check that happened.
#
# Exit 0 when there is no drift (or after a reconcile that removed it all).
# --dry-run exits 2 when drift is present, 0 when it is not, and runs
# nothing else. A real (non-dry-run) run exits 1 if drift remains after
# reconciling. A "floating" row (a pinned tag with no version in it — the
# llama.cpp images today) never counts as drift: there is no pin to
# reconcile it to.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Same candidate order as every other loader in this project (apply.py's
# _load_env(), uninstall_framework.sh, sync_skills.sh): shared-memory/.env,
# falling back to the pre-0.6 repo-root path. Never a single hardcoded path,
# never an imported parser.
_ENV_CANDIDATES=("$REPO_ROOT/shared-memory/.env" "$REPO_ROOT/.env")
ENV_FILE="${_ENV_CANDIDATES[0]}"
[[ -f "$ENV_FILE" ]] || ENV_FILE="${_ENV_CANDIDATES[1]}"

COMPOSE_FILE="${COMPOSE_FILE:-$REPO_ROOT/shared-memory/ops/postgres_neo4j_limits.yaml}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8888}"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "✗ $*"; exit 1; }

DRY_RUN=0
ASSUME_YES=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=1; shift ;;
        --yes|-y)    ASSUME_YES=1; shift ;;
        -h|--help)   awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
        *)           die "unknown argument: $1 (see --help)" ;;
    esac
done

# Read one key from .env without sourcing it (same idiom as postflight.sh /
# init_db.sh — values may contain characters `source` would mis-parse).
read_env() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2-; }

[[ -f "$COMPOSE_FILE" ]] || die "compose file not found: $COMPOSE_FILE"

# ── Read this host's own reality out of the SHIPPED yaml, never a copy ───────
#
# container_name: and image: are read per service rather than hardcoded here,
# so a fork that renames a container still gets a correct table. Each helper
# scopes its awk scan to one service's block (the "  <service>:" line until
# the next line at that same 2-space indent), the same block-scoped grep
# idiom uninstall_framework.sh's compose_down_and_verify() already uses for
# container_name: — never a generic YAML parser.
_yaml_service_field() {
    local svc="$1" field="$2"
    awk -v svc="$svc" -v field="$field" '
        $0 ~ "^  "svc":[[:space:]]*$" { grab=1; next }
        grab && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { grab=0 }
        grab && $0 ~ "^    "field":" {
            v=$0
            sub("^    "field":[[:space:]]*", "", v)
            print v
            exit
        }
    ' "$COMPOSE_FILE"
}

_yaml_service_replicas() {
    local svc="$1"
    awk -v svc="$svc" '
        $0 ~ "^  "svc":[[:space:]]*$" { grab=1; next }
        grab && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { grab=0 }
        grab && /^      replicas:/ {
            v=$0
            sub("^      replicas:[[:space:]]*", "", v)
            print v
            exit
        }
    ' "$COMPOSE_FILE"
}

_yaml_postgres_db() {
    awk '
        /^  postgres:[[:space:]]*$/ { grab=1; next }
        grab && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { grab=0 }
        grab && /^      - POSTGRES_DB=/ {
            v=$0
            sub("^      - POSTGRES_DB=", "", v)
            print v
            exit
        }
    ' "$COMPOSE_FILE"
}

# ── Effective replicas — the SAME nested-default chain preflight.sh already
# computes ("EFFECTIVE replicas" section there), so this script's picture of
# what is actually deployed matches what `docker compose up` actually does,
# per-service override included. Duplicated rather than sourced: preflight.sh
# is not a library, and the six variable names are fixed by the shipped yaml,
# not something a generic parser would do more honestly.
_cpu_reps="$(read_env CPU_ENCODER_REPLICAS)"; _cpu_reps="${_cpu_reps:-1}"
_gpu_reps="$(read_env GPU_ENCODER_REPLICAS)"; _gpu_reps="${_gpu_reps:-0}"
_emb_cpu="$(read_env EMBEDDER_CPU_REPLICAS)"; _emb_cpu="${_emb_cpu:-$_cpu_reps}"
_emb_gpu="$(read_env EMBEDDER_GPU_REPLICAS)"; _emb_gpu="${_emb_gpu:-$_gpu_reps}"
_rer_cpu="$(read_env RERANKER_CPU_REPLICAS)"; _rer_cpu="${_rer_cpu:-$_cpu_reps}"
_rer_gpu="$(read_env RERANKER_GPU_REPLICAS)"; _rer_gpu="${_rer_gpu:-$_gpu_reps}"

_is_int() { [[ "$1" =~ ^[0-9]+$ ]]; }

# Whether SERVICE is active on this host, given the .env's *_REPLICAS vars.
# neo4j and postgres carry no `replicas:` key at all (deploy: is limits-only
# for them) — no replicas key means always-on. A value that fails to
# normalise to a plain integer is reported active with a warning rather than
# silently skipped: compose itself will fail loudly on it at `up` time
# (preflight.sh already checks this ahead of time), and this table must never
# go quiet about a service it could not evaluate.
_service_active() {
    local svc="$1" reps
    case "$svc" in
        neo4j|postgres)        echo 1; return ;;
        retriever-api)         reps="$_emb_cpu" ;;
        reranker-api)          reps="$_rer_cpu" ;;
        retriever-api-gpu)     reps="$_emb_gpu" ;;
        reranker-api-gpu)      reps="$_rer_gpu" ;;
        *)                     echo 1; return ;;
    esac
    if ! _is_int "$reps"; then
        ylw "  ! $svc: replicas value '$reps' is not a plain integer — treating as active; compose will fail loudly on this value at 'up' time" >&2
        echo 1
        return
    fi
    [[ "$reps" != "0" ]] && echo 1 || echo 0
}

# Tag off the last path component; "no digit anywhere in the tag" is what
# distinguishes an exact pin (0.8.6-pg17, 5.26.30-community) from a floating
# tag (server, server-vulkan — the llama.cpp images today, verified against
# the shipped yaml). A floating tag can never be "in sync" — there is no
# specific version to compare against — and can never be reconciled TO a pin
# by this script, because there isn't one.
_image_tag() {
    local img="$1" path
    path="${img##*/}"
    [[ "$path" == *:* ]] && printf '%s' "${path##*:}" || printf '%s' "latest"
}
_is_versioned_tag() { [[ "$1" =~ [0-9] ]]; }

SERVICES=(neo4j postgres retriever-api reranker-api retriever-api-gpu reranker-api-gpu)

# Populated by print_table(); read by the caller after each call.
DRIFT_ROWS=()
_NEO4J_CNAME=""
_PG_CNAME=""
_PG_ACTIVE=0
_PG_PIN_IMAGE=""

print_table() {
    DRIFT_ROWS=()
    printf '%-22s %-11s %-38s %-38s\n' "SERVICE" "STATUS" "PINNED" "RUNNING"
    local svc image cname active tag running rc status
    for svc in "${SERVICES[@]}"; do
        image="$(_yaml_service_field "$svc" image)"
        cname="$(_yaml_service_field "$svc" container_name)"
        active="$(_service_active "$svc")"
        [[ "$svc" == "neo4j" ]]    && _NEO4J_CNAME="$cname"
        [[ "$svc" == "postgres" ]] && { _PG_CNAME="$cname"; _PG_ACTIVE="$active"; _PG_PIN_IMAGE="$image"; }

        if [[ "$active" != "1" ]]; then
            printf '%-22s %-11s %-38s %-38s\n' "$svc" "not deployed here" "$image" "-"
            continue
        fi

        running="$(docker inspect "$cname" --format '{{.Config.Image}}' 2>/dev/null)"
        rc=$?
        [[ "$rc" -eq 0 && -n "$running" ]] || running="absent"

        tag="$(_image_tag "$image")"
        if ! _is_versioned_tag "$tag"; then
            status="floating"
        elif [[ "$running" == "absent" || "$running" != "$image" ]]; then
            status="DRIFT"
            DRIFT_ROWS+=("$svc: pinned $image, running $running")
        else
            status="in sync"
        fi
        printf '%-22s %-11s %-38s %-38s\n' "$svc" "$status" "$image" "$running"
    done

    # ── pgvector extension row — only meaningful once the postgres container
    # actually exists to ask.
    if [[ "$_PG_ACTIVE" == "1" ]]; then
        local pg_running_rc pg_ext_sql pg_ext_file pg_ext_img status
        docker inspect "$_PG_CNAME" >/dev/null 2>&1
        pg_running_rc=$?
        if [[ "$pg_running_rc" -ne 0 ]]; then
            printf '%-22s %-11s %-38s %-38s\n' "pgvector-extension" "absent" "-" "-"
        else
            pg_ext_sql="$(docker exec "$_PG_CNAME" psql -U postgres -d "$PG_DB_RESOLVED" -tAc \
                "SELECT extversion FROM pg_extension WHERE extname='vector'" 2>/dev/null | tr -d '[:space:]')"
            pg_ext_file="$(docker exec "$_PG_CNAME" sh -c \
                'ls /usr/share/postgresql/*/extension/vector--*.sql 2>/dev/null | sort -V | tail -1' 2>/dev/null)"
            pg_ext_img="$(printf '%s' "$pg_ext_file" | sed -E 's#.*/vector--##; s#\.sql$##')"
            if [[ -z "$pg_ext_sql" || -z "$pg_ext_img" ]]; then
                status="unknown"
            elif [[ "$pg_ext_sql" != "$pg_ext_img" ]]; then
                status="DRIFT"
                DRIFT_ROWS+=("pgvector-extension: SQL reports $pg_ext_sql, image carries $pg_ext_img")
            else
                status="in sync"
            fi
            printf '%-22s %-11s %-38s %-38s\n' "pgvector-extension" "$status" "${pg_ext_img:-?}" "${pg_ext_sql:-?}"
        fi
    fi
}

PG_DB_RESOLVED="$(_yaml_postgres_db)"
[[ -n "$PG_DB_RESOLVED" ]] || PG_DB_RESOLVED="$(read_env PG_DB)"
[[ -n "$PG_DB_RESOLVED" ]] || PG_DB_RESOLVED="agent_data"

echo "Shared Memory — stack reconcile"
echo "  compose : $COMPOSE_FILE"
echo "  env     : $ENV_FILE"
echo

print_table
drift_count=${#DRIFT_ROWS[@]}
echo

if [[ "$DRY_RUN" == "1" ]]; then
    if [[ "$drift_count" -eq 0 ]]; then
        grn "No drift — every deployed pin matches its running container."
        exit 0
    fi
    red "DRIFT in $drift_count row(s):"
    for row in "${DRIFT_ROWS[@]}"; do red "  - $row"; done
    exit 2
fi

if [[ "$drift_count" -eq 0 ]]; then
    grn "No drift — nothing to reconcile."
    exit 0
fi

red "DRIFT in $drift_count row(s):"
for row in "${DRIFT_ROWS[@]}"; do red "  - $row"; done
echo
ylw "Reconciling pulls the pinned images, recreates the containers above and"
ylw "runs ALTER EXTENSION vector UPDATE. It does NOT edit .env, run a"
ylw "migration, or restart the gateway. Note: 'pull' also refreshes any"
ylw "FLOATING image (rows marked floating) to whatever its tag points at now."

if [[ "$ASSUME_YES" != "1" ]]; then
    echo
    read -r -p "Type 'reconcile' to confirm: " _answer
    [[ "$_answer" == "reconcile" ]] || die "not confirmed — nothing was changed."
fi

echo
echo "Pulling pinned images ..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull \
    || die "docker compose pull failed — nothing was recreated."

echo "Recreating containers ..."
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d \
    || die "docker compose up -d failed — the images were pulled but containers may be inconsistent. Check: docker compose -f $COMPOSE_FILE --env-file $ENV_FILE ps"

if [[ "$_PG_ACTIVE" == "1" ]]; then
    echo "Waiting for Postgres ($_PG_CNAME) to accept connections ..."
    _pg_ready=0
    for _ in $(seq 1 60); do
        if docker exec "$_PG_CNAME" pg_isready -U postgres -d "$PG_DB_RESOLVED" >/dev/null 2>&1; then
            _pg_ready=1
            break
        fi
        sleep 1
    done
    if [[ "$_pg_ready" != "1" ]]; then
        die "Postgres did not become ready within 60s after recreation — check: docker logs $_PG_CNAME"
    fi
    echo "Running ALTER EXTENSION vector UPDATE (idempotent) ..."
    docker exec "$_PG_CNAME" psql -U postgres -d "$PG_DB_RESOLVED" -c "ALTER EXTENSION vector UPDATE" \
        || die "ALTER EXTENSION vector UPDATE failed — containers were recreated but the extension was not updated. Re-run this script, or by hand: docker exec $_PG_CNAME psql -U postgres -d $PG_DB_RESOLVED -c \"ALTER EXTENSION vector UPDATE\""
fi

echo
echo "The gateway was NOT restarted — it reconnects to Postgres/Neo4j on its"
echo "own. Check it came back: curl -s $GATEWAY_URL/health"
echo

print_table
drift_count=${#DRIFT_ROWS[@]}
echo

if [[ "$drift_count" -eq 0 ]]; then
    grn "Reconciled — no DRIFT rows remain (floating rows, if any, do not count)."
    exit 0
fi

red "Reconcile finished, but $drift_count DRIFT row(s) remain:"
for row in "${DRIFT_ROWS[@]}"; do red "  - $row"; done
exit 1
