#!/usr/bin/env bash
# install_framework.sh — first-time framework setup.
#
# Prompts for host data paths + DB passwords, writes the gitignored framework
# env (shared-memory/.env) from the committed template, and creates the data
# dirs docker-compose mounts. Idempotent-ish: refuses to clobber an existing
# .env without confirmation. The CLIENT token is configured separately in each
# agent's skill .env (shared-memory-skill/shared-memory/.env.example).
set -euo pipefail

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the interactive install
# ran anyway.
for _arg in "$@"; do
    case "$_arg" in
        -h|--help)
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "✗ unknown argument: $_arg (this script takes none — see --help)" >&2
            exit 1
            ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRAMEWORK_DIR="$(dirname "$SCRIPT_DIR")"          # …/shared-memory
REPO_DIR="$(dirname "$FRAMEWORK_DIR")"            # repo root
EXAMPLE="$FRAMEWORK_DIR/.env.example"
ENV_FILE="$FRAMEWORK_DIR/.env"

[ -f "$EXAMPLE" ] || { echo "ERROR: missing $EXAMPLE" >&2; exit 1; }

echo "── Shared Memory — framework first-install ──"
if [ -f "$ENV_FILE" ]; then
  read -r -p "shared-memory/.env already exists. Overwrite? [y/N] " yn
  [[ "${yn:-}" =~ ^[Yy]$ ]] || { echo "Aborted — existing .env kept."; exit 0; }
fi

ask() {  # prompt default  → echoes answer (default if blank)
  local v; read -r -p "$1 [$2]: " v; printf '%s' "${v:-$2}"
}
ask_secret() {  # prompt → echoes answer (input hidden)
  local v; read -r -s -p "$1: " v; echo >&2; printf '%s' "$v"
}

NEO4J_HOST_DIR="$(ask 'Neo4j host data dir'        "$HOME/databases/neo4j")"
PG_DATA_DIR="$(ask 'Postgres data dir'             "$HOME/databases/postgres")"
LLM_MODELS_DIR="$(ask 'GGUF models dir (blank if using LM Studio)' '')"
# The compose file passes the Neo4j password as NEO4J_AUTH=neo4j/<password>,
# a '/'-delimited string — a password containing '/' silently breaks parsing
# and the container restart-loops on "… is invalid" (measured on a fresh
# install; base64 output is the classic source). Refuse it here, where it is
# one keystroke to fix, instead of there. Hex never has this problem:
#   openssl rand -hex 20
while :; do
  NEO4J_PASSWORD="$(ask_secret 'Neo4j password (no "/" — hex is safest, e.g. openssl rand -hex 20)')"
  case "$NEO4J_PASSWORD" in
    */*) echo "  ✗ contains '/' — breaks the container's NEO4J_AUTH parsing; pick another" >&2 ;;
    *)   break ;;
  esac
done
PG_PASSWORD="$(ask_secret 'Postgres password')"
# CPU thread budget for the two encoder containers, DERIVED from this host
# rather than assumed: about half its threads plus one, so reranking cannot
# starve Postgres, Neo4j, the gateway and the desktop. Portable across the
# three ways a machine reports its CPU count; falls back to the compose default.
# `--threads` counts THREADS, so this counts threads — matching the unit of the
# flag it feeds. (Deriving it from physical cores and passing it to a thread
# flag mixes two units and silently halves the budget.) Half the machine plus
# one leaves room for Postgres, Neo4j, the gateway and the desktop.
#
# ⚠ This is a PER-CONTAINER default and there are two encoders. They can run at
# once (a search reranks while a save embeds), so on a machine where that
# overlap is sustained, halve it again or pin each to its own cores — the
# framework cannot know which, so it ships the simple derivation and leaves the
# tuning to the operator.
_ncpu="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null \
         || sysctl -n hw.ncpu 2>/dev/null || echo 8)"
LLAMA_CPU_THREADS="$(( _ncpu / 2 + 1 ))"
[ "$LLAMA_CPU_THREADS" -lt 1 ] && LLAMA_CPU_THREADS=1
# S-07: NEO4J_HOST_DIR/PG_DATA_DIR/LLM_MODELS_DIR/LLAMA_CPU_THREADS are plain
# config — safe to export at the top level, and install_service.sh /
# install_llm_backends.sh (spawned below, neither of which needs a DB
# password) inheriting them costs nothing. NEO4J_PASSWORD/PG_PASSWORD are
# exported ONLY inside this subshell, scoped to the one awk invocation that
# needs them via ENVIRON — they never reach the outer script's environment,
# so a later `bash "$FRAMEWORK_DIR/ops/install_service.sh"` or
# `install_llm_backends.sh` cannot inherit a DB password neither one needs.
export NEO4J_HOST_DIR PG_DATA_DIR LLM_MODELS_DIR LLAMA_CPU_THREADS

# S-07: umask 077 so $ENV_FILE is created 600 from the FIRST byte — never
# create-then-chmod, which leaves a window at the process umask (often 0644,
# world-readable) between the file's creation and the chmod below. The
# trailing chmod stays as a belt-and-suspenders no-op for an inherited umask
# that already happened to be tighter.
(
  export NEO4J_PASSWORD PG_PASSWORD
  umask 077
  # Render: copy the template, replacing only the value lines (ENVIRON avoids
  # any escaping pitfalls with slashes/special chars in paths or passwords).
  awk '
    function put(k) { print k "=" ENVIRON[k]; }
    /^NEO4J_HOST_DIR=/ { put("NEO4J_HOST_DIR"); next }
    /^PG_DATA_DIR=/    { put("PG_DATA_DIR");    next }
    /^LLM_MODELS_DIR=/ { put("LLM_MODELS_DIR"); next }
    /^NEO4J_PASSWORD=/ { put("NEO4J_PASSWORD"); next }
    /^PG_PASSWORD=/    { put("PG_PASSWORD");    next }
    /^LLAMA_CPU_THREADS=/ { put("LLAMA_CPU_THREADS"); next }
    { print }
  ' "$EXAMPLE" > "$ENV_FILE"
)
chmod 600 "$ENV_FILE"

mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"

# The neo4j container drops to uid 7474 and demands WRITE access to its
# mounted dirs. Its entrypoint chowns /data and /logs itself, but NOT /import
# and /plugins — freshly mkdir'ed user-owned 0755 dirs crash-loop the
# container on "/import is not accessible" (measured on a fresh Fedora
# install). Chown them now; plain chown needs root, so fall back to a docker
# one-liner (root inside the container), and to printing the command when
# neither is possible. preflight.sh verifies this either way.
if ! chown -R 7474:7474 "$NEO4J_HOST_DIR"/{import,plugins} 2>/dev/null; then
  if docker info >/dev/null 2>&1 && \
     docker run --rm -v "$NEO4J_HOST_DIR":/t:z alpine chown -R 7474:7474 /t/import /t/plugins 2>/dev/null; then
    echo "  ✓ Neo4j import/plugins dirs chowned to the container user (via docker)"
  else
    echo "  ⚠ Could not chown Neo4j dirs to the container user. Run:"
    echo "      sudo chown -R 7474:7474 \"$NEO4J_HOST_DIR\"/{import,plugins}"
    echo "    (preflight.sh will re-check this)"
  fi
else
  echo "  ✓ Neo4j import/plugins dirs chowned to the container user"
fi

echo
echo "✓ Wrote $ENV_FILE (chmod 600) and created data dirs."
echo "  Encoder CPU budget:         LLAMA_CPU_THREADS=$LLAMA_CPU_THREADS (of $_ncpu host threads)"
echo "  Confirm it is gitignored:   git -C \"$REPO_DIR\" check-ignore shared-memory/.env"
echo "  Bring up the stack:         docker compose -f \"$REPO_DIR/shared-memory/ops/postgres_neo4j_limits.yaml\" --env-file \"$ENV_FILE\" up -d"
echo "  Initialise both schemas:    bash shared-memory/scripts/init_db.sh"
echo "  Then mint client tokens:    bash shared-memory/scripts/bootstrap_tokens.sh"

echo
if command -v systemctl >/dev/null 2>&1; then
  read -r -p "Install the gateway as a systemd --user service now (auto-start on boot, clean shutdown, no manual restart step)? [Y/n] " svc_yn
  if [[ ! "${svc_yn:-Y}" =~ ^[Nn]$ ]]; then
    bash "$FRAMEWORK_DIR/ops/install_service.sh"
  else
    echo "  Skipped. Install later:      bash shared-memory/ops/install_service.sh"
  fi
else
  echo "  systemd not found — skipping the service-install prompt. The gateway still"
  echo "  runs fine started by hand; it just won't survive logout/reboot without one."
fi

echo
read -r -p "Configure reasoning-LLM backend(s) now (local, remote, or a paid cloud API)? [y/N] " llm_yn
if [[ "${llm_yn:-N}" =~ ^[Yy]$ ]]; then
  bash "$FRAMEWORK_DIR/ops/install_llm_backends.sh"
else
  echo "  Skipped. A single default backend at LLM_DEFAULT_TARGET (http://localhost:5000)"
  echo "  is used until you configure one:  bash shared-memory/ops/install_llm_backends.sh"
fi
