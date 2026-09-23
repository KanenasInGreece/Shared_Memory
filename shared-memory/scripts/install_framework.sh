#!/usr/bin/env bash
# install_framework.sh — first-time framework setup.
#
# Prompts for data paths and database passwords, writes shared-memory/.env from the template, and creates the directories compose mounts. It will not overwrite an existing .env without confirmation. Each agent's token lives in that agent's own skill .env.
#
# On a first install an empty password is generated inside this script, written at mode 600, and never displayed. On an overwrite an empty answer re-prompts, because the databases already use the old password. A password of 8 characters or fewer is refused, and a Neo4j password containing '/' is refused because NEO4J_AUTH cannot carry it.
set -euo pipefail

# --help prints this header and exits. Any other argument is refused, because this script used to ignore flags and run the install anyway.
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
# Whether an empty password may be generated is fixed here, before anything is written. ask_secret must not re-read the .env later.
SECRET_MODE="first-install"
if [ -f "$ENV_FILE" ]; then
  SECRET_MODE="overwrite"
  read -r -p "shared-memory/.env already exists. Overwrite? [y/N] " yn
  [[ "${yn:-}" =~ ^[Yy]$ ]] || { echo "Aborted — existing .env kept."; exit 0; }
fi

ask() {  # prompt default  → echoes answer (default if blank)
  local v; read -r -p "$1 [$2]: " v; printf '%s' "${v:-$2}"
}
# >>> ASK_SECRET
# Empty answer generates a password inside this process (never on argv); too-short or exhausted stdin fails loudly (fact:1499).
ask_secret() {  # prompt [mode] → echoes answer (input hidden), or exits 1
  # The mode argument is first-install or overwrite, captured before anything is written. Deriving it here would generate a new password over volumes that still need the old one.
  # This block is extracted and run standalone, so it must not read $ENV_FILE. Both call sites pass the mode.
  local v gen_rc mode trimmed
  mode="${2:-first-install}"
  case "$mode" in
    first-install|overwrite) ;;
    *)
      echo "✗ ERROR: ask_secret called with an unknown mode '$mode' (expected first-install or overwrite) — refusing to guess whether an empty answer may generate a password." >&2
      return 1
      ;;
  esac
  while :; do
    if ! IFS= read -r -s -p "$1: " v; then
      echo >&2
      echo "✗ $1: no more input on stdin — refusing to write a blank or unconfirmed password. Re-run this script from an interactive terminal (or a pipe that supplies a valid password) and answer the prompt." >&2
      return 1
    fi
    echo >&2
    if [ -z "$v" ]; then
      if [ "$mode" != "first-install" ]; then
        echo "  ✗ $1: this is an OVERWRITE of an existing shared-memory/.env, so an empty answer cannot generate a new password — the databases were initialised with the old one and would refuse it. Type the password those volumes already use (or delete the volumes and re-run as a first install)." >&2
        continue
      fi
      gen_rc=0
      # python3 -I so a sitecustomize cannot reach the generator. Validate after command substitution strips the newline, or a correct generator fails the check.
      v="$(python3 -I -c 'import secrets; print(secrets.token_hex(20))')" || gen_rc=$?
      if [ "$gen_rc" -ne 0 ] || [ -z "$v" ]; then
        echo "✗ ERROR: python3 failed while generating $1 (rc=$gen_rc) — refusing to continue. If python3 is missing or broken, run shared-memory/scripts/preflight.sh first; it diagnoses this directly and names the fix." >&2
        return 1
      fi
      # A python3 that prints a warning before the hex still exits 0, and that text was accepted as the password. A non-zero exit is not the only failure.
      if [[ ! "$v" =~ ^[0-9a-f]{40}$ ]]; then
        echo "✗ ERROR: python3 did not return a clean 40-character hex value while generating $1 — refusing to continue. Something on this host's python3 is writing to stdout before the value (a wrapper, a sitecustomize, a shell profile banner). Run shared-memory/scripts/preflight.sh; it diagnoses this directly and names the fix." >&2
        return 1
      fi
      echo "  (empty answer — generated a strong password internally; not displayed, not logged)" >&2
      printf '%s' "$v"
      return 0
    fi
    # Surrounding whitespace is refused, not stripped. compose and secure_env.py strip it, while read_env() keeps it, so a padded password would initialise one value and authenticate another.
    trimmed="${v#"${v%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    if [ "$trimmed" != "$v" ]; then
      echo "  ✗ $1 must not begin or end with spaces or tabs — parts of the install strip surrounding whitespace and parts keep it, so a padded password would initialise the databases under one value and be checked against another. Retype it without the padding." >&2
      continue
    fi
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

# The device prompts are a fixed-length sequence so a piped install stays in lockstep with this script. The default cpu writes nothing, and the template's pair-wise replicas still decide.
# No EMBEDDER_DEVICE or RERANKER_DEVICE is written. That was a persisted derived value (decision:1032), and the answer only sets the four replica variables.
echo
echo "  Measured on a 4 GB card: the embedder fits comfortably (671 MB VRAM);"
echo "  the reranker's 8192-token context window overflows a small card's"
echo "  device memory. Only matters if you use the bundled compose encoders."
EMBEDDER_DEVICE="$(ask 'Embedder device (cpu/gpu)' 'cpu')"
RERANKER_DEVICE="$(ask 'Reranker device (cpu/gpu) — not recommended on a small card' 'cpu')"
# Normalise case before the match so "GPU" and an unrecognised answer are judged on the same form.
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
# When any replica line is written, write all four. A gpu embedder and a cpu reranker must not leave the reranker implicit.
EMBEDDER_CPU_REPLICAS=1; EMBEDDER_GPU_REPLICAS=0
RERANKER_CPU_REPLICAS=1; RERANKER_GPU_REPLICAS=0
[ "$EMBEDDER_DEVICE" = "gpu" ] && { EMBEDDER_CPU_REPLICAS=0; EMBEDDER_GPU_REPLICAS=1; }
[ "$RERANKER_DEVICE" = "gpu" ] && { RERANKER_CPU_REPLICAS=0; RERANKER_GPU_REPLICAS=1; }

# GPU_RENDER_GID is always prompted, so the piped answer count does not depend on an earlier answer. The packaged default "video" is wrong on Debian, and the value is written only when a GPU encoder was chosen.
_gpu_render_gid_default="video"
if [ -e /dev/dri/renderD128 ]; then
  _detected_gid="$(stat -c '%g' /dev/dri/renderD128 2>/dev/null || echo '')"
  [ -n "$_detected_gid" ] && _gpu_render_gid_default="$_detected_gid"
fi
GPU_RENDER_GID="$(ask 'Render-node group id for the encoder GPU (only matters if either answer above is gpu)' "$_gpu_render_gid_default")"

# NEO4J_AUTH is neo4j/<password>, so a slash restart-loops the container. Refuse it here.
# The hint must not tell the operator to generate a password in their own shell (fact:1499). A first install generates on Enter; an overwrite re-prompts, because the databases already have a password.
if [ "$SECRET_MODE" = "first-install" ]; then
  _pw_hint="press Enter and a strong one is generated here — never displayed, never logged"
else
  _pw_hint="existing .env — type the password the databases were initialised with; Enter re-prompts"
fi
while :; do
  NEO4J_PASSWORD="$(ask_secret "Neo4j password (no \"/\"; $_pw_hint)" "$SECRET_MODE")"
  case "$NEO4J_PASSWORD" in
    */*) echo "  ✗ contains '/' — breaks the container's NEO4J_AUTH parsing; pick another" >&2 ;;
    *)   break ;;
  esac
done
PG_PASSWORD="$(ask_secret "Postgres password ($_pw_hint)" "$SECRET_MODE")"
# Half the host's threads plus one, counted as threads, so the budget matches the flag it feeds and still leaves room for the stores. Two encoders can run at once, and this script cannot know when to halve that again.
_ncpu="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null \
         || sysctl -n hw.ncpu 2>/dev/null || echo 8)"
LLAMA_CPU_THREADS="$(( _ncpu / 2 + 1 ))"
[ "$LLAMA_CPU_THREADS" -lt 1 ] && LLAMA_CPU_THREADS=1

# Derive encoder URL defaults before any byte of .env is written, so a failure leaves no half-written install and a bad value never reaches the file.
# A missing python3 is named here and points at preflight.sh. The value is refused unless it is a non-empty URL with no whitespace, the shape of a smuggled second KEY=VALUE line.
_check_encoder_default() {  # _check_encoder_default <VAR_NAME> <value>
    local name="$1" val="$2"
    if [[ -z "$val" ]]; then
        echo "✗ ERROR: could not derive a default for $name (python3/framework_defaults.py returned nothing) — refusing to write shared-memory/.env. If python3 is missing or broken, run shared-memory/scripts/preflight.sh first; it diagnoses this directly and names the fix." >&2
        exit 1
    fi
    if [[ "$val" =~ [[:space:][:cntrl:]] ]]; then
        echo "✗ ERROR: the derived default for $name contains whitespace or a control character — refusing to write shared-memory/.env (this is exactly the shape a value-injection attack, e.g. a smuggled second KEY=VALUE line, would take)." >&2
        exit 1
    fi
    if [[ ! "$val" =~ ^[A-Za-z][A-Za-z0-9+.-]*://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
        echo "✗ ERROR: the derived default for $name ('$val') is not URL-shaped (scheme://host[:port]) — refusing to write shared-memory/.env." >&2
        exit 1
    fi
}
_encoder_default_rc=0
_embedder_url_default="$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from framework_defaults import FRAMEWORK_DEFAULTS
print(FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"])
' "$SCRIPT_DIR")" || _encoder_default_rc=$?
if [ "$_encoder_default_rc" -ne 0 ]; then
    echo "✗ ERROR: python3 failed while deriving the default EMBEDDER_URL from framework_defaults.py (rc=$_encoder_default_rc) — refusing to write shared-memory/.env. If python3 is missing or broken, run shared-memory/scripts/preflight.sh first; it diagnoses this directly and names the fix." >&2
    exit 1
fi
_check_encoder_default "EMBEDDER_URL" "$_embedder_url_default"
_encoder_default_rc=0
_reranker_url_default="$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
from framework_defaults import FRAMEWORK_DEFAULTS
print(FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"])
' "$SCRIPT_DIR")" || _encoder_default_rc=$?
if [ "$_encoder_default_rc" -ne 0 ]; then
    echo "✗ ERROR: python3 failed while deriving the default RERANKER_URL from framework_defaults.py (rc=$_encoder_default_rc) — refusing to write shared-memory/.env. If python3 is missing or broken, run shared-memory/scripts/preflight.sh first; it diagnoses this directly and names the fix." >&2
    exit 1
fi
_check_encoder_default "RERANKER_URL" "$_reranker_url_default"

# Paths and the thread count may be exported. The database passwords are exported only inside the subshell that writes .env, so later install scripts cannot inherit them.
export NEO4J_HOST_DIR PG_DATA_DIR LLM_MODELS_DIR LLAMA_CPU_THREADS

# umask 077 creates .env at mode 600 from the first byte. create-then-chmod would leave a world-readable window.
(
  export NEO4J_PASSWORD PG_PASSWORD
  umask 077
  if [ "$SECRET_MODE" = "overwrite" ]; then
    # On overwrite, replace the six keys in the existing file. A fresh render would drop keys such as AGENT_TOKENS.
    tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    cp "$ENV_FILE" "$tmp"
    for k in NEO4J_HOST_DIR PG_DATA_DIR LLM_MODELS_DIR NEO4J_PASSWORD PG_PASSWORD LLAMA_CPU_THREADS; do
      inner="$(mktemp "${ENV_FILE}.XXXXXX")"
      grep -vE "^[[:space:]]*#?[[:space:]]*${k}=" "$tmp" > "$inner" || true
      printf '%s=%s\n' "$k" "${!k}" >> "$inner"
      mv "$inner" "$tmp"
    done
    mv "$tmp" "$ENV_FILE"
  else
    # Copy the template and replace only value lines. ENVIRON avoids escaping paths and passwords.
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
  fi
)
chmod 600 "$ENV_FILE"

# Those replica lines are commented out in the template, so the awk above cannot fill them. Append all four, plus GPU_RENDER_GID, only when an answer was gpu.
if [ "$EMBEDDER_DEVICE" = "gpu" ] || [ "$RERANKER_DEVICE" = "gpu" ]; then
  if grep -q '^[[:space:]]*EMBEDDER_CPU_REPLICAS=' "$ENV_FILE"; then
    for k in EMBEDDER_CPU_REPLICAS EMBEDDER_GPU_REPLICAS RERANKER_CPU_REPLICAS RERANKER_GPU_REPLICAS GPU_RENDER_GID; do
      inner="$(mktemp "${ENV_FILE}.XXXXXX")"
      grep -vE "^[[:space:]]*#?[[:space:]]*${k}=" "$ENV_FILE" > "$inner" || true
      printf '%s=%s\n' "$k" "${!k}" >> "$inner"
      mv "$inner" "$ENV_FILE"
    done
  else
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
  fi
  echo "  ✓ Encoder device split written: embedder=$EMBEDDER_DEVICE reranker=$RERANKER_DEVICE GPU_RENDER_GID=$GPU_RENDER_GID"
fi

# Write the shipped encoder endpoints explicitly (decision:1824). They are commented in the template, so the awk above cannot fill them, and they are written whether or not a GPU was chosen.
# The values were already checked above, before .env existed. An operator whose encoders live elsewhere edits the two lines.
if ! grep -q '^[[:space:]]*EMBEDDER_URL=' "$ENV_FILE"; then
  {
    echo ""
    echo "# ── Encoder endpoints (install_framework.sh writes the framework's default explicitly) ──"
    echo "# This installs the bundled compose's own default port for each encoder. If your"
    echo "# encoders run somewhere else (Q2's 'existing endpoint' answer), edit these two lines —"
    echo "# see AGENTS.md Phase 4."
    echo "EMBEDDER_URL=$_embedder_url_default"
    echo "RERANKER_URL=$_reranker_url_default"
  } >> "$ENV_FILE"
fi
echo "  ✓ Encoder endpoints written: EMBEDDER_URL=$_embedder_url_default RERANKER_URL=$_reranker_url_default (edit if yours differ)"

mkdir -p "$NEO4J_HOST_DIR"/{data,logs,import,plugins} "$PG_DATA_DIR"

# The image chowns data and logs, not import or plugins, so a fresh mkdir crash-loops those two. chown them now, or print the command.
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
# A terminal defaults this prompt to yes. A pipe that runs out of answers becomes an explicit no, so a finished scripted install does not die under set -e after .env is already written.
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
# Report a would-refuse config now, instead of at the gateway's first boot (decision:1832). Phase A of check_config.py is stdlib-only (fact:1585), so a missing dependency cannot crash a fresh host.
# Exit codes 0, 1, and 2 are the reporter's own results and must not fail this installer. Anything past that is a crash, not a config verdict, and is printed.
echo "Checking the configuration this install produced..."
rc=0; python3 "$SCRIPT_DIR/check_config.py" --phase-a-only || rc=$?
if [ "$rc" -gt 2 ]; then
  echo "⚠ check_config aborted (rc=$rc) — config report incomplete"
fi
