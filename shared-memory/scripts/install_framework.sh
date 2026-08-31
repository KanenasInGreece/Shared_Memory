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
# >>> ASK_SECRET
# Prompts for a DB password (hidden input) and never returns until it has a
# valid one — never a blank/short value silently written to .env (framework
# fact:1499 CRITICAL 1: pressing Enter used to write NEO4J_PASSWORD= /
# PG_PASSWORD= as literal empty strings, and the install still reported
# success). "Valid" means strictly more than 8 characters; 8-or-fewer is
# refused, including the empty string.
#
# On a REAL answer being available — an interactive terminal, or a script
# feeding scripted lines on a pipe — `read` succeeds and returns whatever it
# got, so an invalid entry (empty or too short) loops back for another try:
# this is the RE-PROMPT case, and covers both a human pressing Enter and an
# automated caller feeding a too-short placeholder.
#
# On EXHAUSTED input (stdin closed, or a pipe with no more lines left) `read`
# itself fails — bash's own signal that there is no one left to answer. That
# is exactly the measured failure mode this guards: piping stdin with nothing
# left ran the whole install silently on an empty/default password. Here it
# is instead a hard, loud, nonzero-exit failure that names the step, rather
# than a silent fall-through to the empty string.
ask_secret() {  # prompt → echoes answer (input hidden), or exits 1
  local v
  while :; do
    if ! read -r -s -p "$1: " v; then
      echo >&2
      echo "✗ $1: no more input on stdin — refusing to write a blank or unconfirmed password. Re-run this script from an interactive terminal (or a pipe that supplies a valid password) and answer the prompt." >&2
      return 1
    fi
    echo >&2
    if [ "${#v}" -gt 8 ]; then
      printf '%s' "$v"
      return 0
    fi
    echo "  ✗ $1 must be more than 8 characters (got ${#v}) — try again." >&2
  done
}
# <<< ASK_SECRET

NEO4J_HOST_DIR="$(ask 'Neo4j host data dir'        "$HOME/databases/neo4j")"
PG_DATA_DIR="$(ask 'Postgres data dir'             "$HOME/databases/postgres")"
LLM_MODELS_DIR="$(ask 'GGUF models dir (blank if using LM Studio)' '')"

# ── Q3b (AGENTS.md): per-service encoder device split ──────────────────────
# Three plain VALUE prompts (same shape as the three dirs above, via ask())
# so this stays a FIXED, unconditional-length sequence — never branching on
# an earlier answer — which is what lets AGENTS.md's Phase 1 drive the whole
# script with one fixed printf of piped answers
# (tests/test_change_group_contracts.py enforces the two stay in sync).
# Defaulting to "cpu" for both device answers and writing NOTHING to .env
# unless one is answered "gpu" means accepting the default (Enter, Enter,
# Enter) reproduces TODAY's behaviour exactly: the pair-wise
# CPU_ENCODER_REPLICAS/GPU_ENCODER_REPLICAS already in the template decide,
# same as before this question existed. Asked even when LLM_MODELS_DIR is
# blank (encoders hosted elsewhere) — harmless there since the default
# writes nothing.
#
# M4 ruling (PR #308 review, operator-adjudicated): NO separate
# EMBEDDER_DEVICE/RERANKER_DEVICE var is written — that would be a
# PERSISTED DERIVED VALUE (decision:1032) whose only consumer was a
# drift-checker for the divergence its own existence created. The answer to
# "cpu"/"gpu" here decides ONLY the four replica vars below; nothing else
# reads or writes a device string.
echo
echo "  Measured on a 4 GB card: the embedder fits comfortably (671 MB VRAM);"
echo "  the reranker's 8192-token context window overflows a small card's"
echo "  device memory. Only matters if you use the bundled compose encoders."
EMBEDDER_DEVICE="$(ask 'Embedder device (cpu/gpu)' 'cpu')"
RERANKER_DEVICE="$(ask 'Reranker device (cpu/gpu) — not recommended on a small card' 'cpu')"
# L3: case-insensitive ("GPU"/"Gpu" must mean the same as "gpu") — normalise
# before the case match, not after, so an unrecognised answer is judged on
# its normalised form too.
EMBEDDER_DEVICE="$(printf '%s' "$EMBEDDER_DEVICE" | tr '[:upper:]' '[:lower:]')"
RERANKER_DEVICE="$(printf '%s' "$RERANKER_DEVICE" | tr '[:upper:]' '[:lower:]')"
case "$EMBEDDER_DEVICE" in
  gpu|cpu) ;;
  *) echo "  ⚠ unrecognised embedder device '$EMBEDDER_DEVICE' — treating as cpu" >&2
     EMBEDDER_DEVICE="cpu" ;;
esac
case "$RERANKER_DEVICE" in
  gpu|cpu) ;;
  *) echo "  ⚠ unrecognised reranker device '$RERANKER_DEVICE' — treating as cpu" >&2
     RERANKER_DEVICE="cpu" ;;
esac
# M3: write ALL FOUR per-service replica vars whenever the block below is
# written at all — never only the ones for the encoder that moved. An
# install that answers embedder=gpu, reranker=cpu must not rely on a
# pair-wise fallback line for the reranker's replicas; the rendered compose
# must match the two answers with nothing left implicit.
EMBEDDER_CPU_REPLICAS=1; EMBEDDER_GPU_REPLICAS=0
RERANKER_CPU_REPLICAS=1; RERANKER_GPU_REPLICAS=0
[ "$EMBEDDER_DEVICE" = "gpu" ] && { EMBEDDER_CPU_REPLICAS=0; EMBEDDER_GPU_REPLICAS=1; }
[ "$RERANKER_DEVICE" = "gpu" ] && { RERANKER_CPU_REPLICAS=0; RERANKER_GPU_REPLICAS=1; }

# M2: GPU_RENDER_GID — the packaged compose default ("video") is WRONG on
# Debian (render node group is "render", gid 992, measured on a fresh
# Debian 13 install — AGENTS.md's post-install prose already carried this
# warning; it was never wired into the interactive install). A THIRD
# unconditional value prompt (same fixed-shape reasoning as the two device
# prompts above) rather than a conditional one gated on "gpu was chosen" —
# a prompt whose very presence depended on an earlier answer would break
# AGENTS.md's fixed-length piped-answer sequence exactly the way a nested
# y/n gate did in an earlier draft of Q3b. Pre-filled with the REAL value
# when the render node is visible (`stat -c '%g' /dev/dri/renderD128`, the
# documented method) so accepting the default on a host that actually has
# one just works; the prompt itself IS the ".env.example guidance" fallback
# on a host where the device is not visible (no card, wrong permissions,
# containerised dev environment). Only written to .env when a GPU was
# actually chosen for at least one encoder — irrelevant otherwise.
_gpu_render_gid_default="video"
if [ -e /dev/dri/renderD128 ]; then
  _detected_gid="$(stat -c '%g' /dev/dri/renderD128 2>/dev/null || echo '')"
  [ -n "$_detected_gid" ] && _gpu_render_gid_default="$_detected_gid"
fi
GPU_RENDER_GID="$(ask 'Render-node group id for the encoder GPU (only matters if either answer above is gpu)' "$_gpu_render_gid_default")"

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

# The per-service replica vars (and GPU_RENDER_GID/ENCODER_GPU_INDEX) are
# COMMENTED OUT in the template (like CPU_ENCODER_REPLICAS/
# GPU_ENCODER_REPLICAS above them), so the awk substitution above — which
# only rewrites lines already live in the template — cannot fill them in.
# Append instead, and only when Q3b actually moved something off "cpu" —
# both at the default means nothing to add: the pair-wise defaults already
# in the template govern, exactly as before this question existed. M3: once
# writing at all, write ALL FOUR replica vars (never only the moved
# encoder's), so the rendered compose matches the two answers on its own.
if [ "$EMBEDDER_DEVICE" = "gpu" ] || [ "$RERANKER_DEVICE" = "gpu" ]; then
  {
    echo ""
    echo "# ── Per-service encoder device split (Q3b, install_framework.sh) ──"
    echo "EMBEDDER_CPU_REPLICAS=$EMBEDDER_CPU_REPLICAS"
    echo "EMBEDDER_GPU_REPLICAS=$EMBEDDER_GPU_REPLICAS"
    echo "RERANKER_CPU_REPLICAS=$RERANKER_CPU_REPLICAS"
    echo "RERANKER_GPU_REPLICAS=$RERANKER_GPU_REPLICAS"
    echo "GPU_RENDER_GID=$GPU_RENDER_GID"
    echo "ENCODER_GPU_INDEX=0"
  } >> "$ENV_FILE"
  echo "  ✓ Encoder device split written: embedder=$EMBEDDER_DEVICE reranker=$RERANKER_DEVICE GPU_RENDER_GID=$GPU_RENDER_GID"
fi

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
# W0 item ③: interactive default is Y (an operator hitting Enter here almost
# always wants to configure a backend); a non-interactive run (piped stdin)
# keeps the historical N default. `[ -t 0 ]` picks the default a blank
# answer resolves to; the guarded read (ask_secret's precedent, above) turns
# an EXHAUSTED pipe into an explicit "n" rather than a `set -e` death here —
# .env is already written by this point, so that death used to exit
# non-zero on a fully scripted install with nothing left to answer. Now it
# takes the N branch and the installer exits 0 (deliberate, ruled behaviour
# change — the AGENTS.md piped "n\nn" install still answers explicitly and
# is unaffected either way).
_llm_yn_default=N
[ -t 0 ] && _llm_yn_default=Y
if ! read -r -p "Configure reasoning-LLM backend(s) now (local, remote, or a paid cloud API)? [Y/n] " llm_yn; then
  llm_yn=n
fi
if [[ "${llm_yn:-$_llm_yn_default}" =~ ^[Yy]$ ]]; then
  bash "$FRAMEWORK_DIR/ops/install_llm_backends.sh"
else
  echo "  Skipped. Until you configure backends, the gateway falls back to"
  echo "  http://localhost:5000 (LLM_DEFAULT_TARGET). This implicit fallback is"
  echo "  being retired — configure explicitly with:"
  echo "    bash shared-memory/ops/install_llm_backends.sh"
fi

echo
# D5 (decision:1832): a would-refuse config now REPORTS at install time
# instead of only being discovered at the gateway's own first boot. Phase A
# of check_config.py is stdlib-only by design (fact:1585-adjacent — see its
# own module docstring), so this cannot crash a fresh host on a missing
# dependency; `|| true` because this script's job is to REPORT the config,
# never to let the reporter's own exit code kill an installer that has
# nothing left to fail on past this point (this script is `set -euo
# pipefail`, so an unguarded non-zero here would abort with .env already
# written and every prior step already done).
echo "Checking the configuration this install produced..."
python3 shared-memory/scripts/check_config.py --phase-a-only || true
