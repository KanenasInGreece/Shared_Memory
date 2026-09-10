#!/usr/bin/env bash
# install_framework.sh — first-time framework setup.
#
# Prompts for host data paths + DB passwords, writes the gitignored framework
# env (shared-memory/.env) from the committed template, and creates the data
# dirs docker-compose mounts. Idempotent-ish: refuses to clobber an existing
# .env without confirmation. The CLIENT token is configured separately in each
# agent's skill .env (shared-memory-skill/shared-memory/.env.example).
#
# Passwords: on a FIRST install (no shared-memory/.env yet) an empty answer to
# either password prompt generates a strong value inside this script — it is
# written to shared-memory/.env at mode 600 and never displayed or logged.
# When an existing .env is being OVERWRITTEN, an empty answer re-prompts
# instead: Postgres and Neo4j were already initialised with the old password
# and a freshly generated one would lock you out of both. A typed password of
# 8 characters or fewer is refused, and a Neo4j password containing '/' is
# refused because NEO4J_AUTH=neo4j/<password> cannot carry it.
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
# ⭐ WHICH PATH THIS RUN IS ON decides whether an empty password answer may
# GENERATE (see ask_secret below). Captured here, once, from the state of the
# file BEFORE anything is written — never re-derived later, when the .env may
# already have been replaced.
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
# Prompts for a DB password (hidden input) and never returns until it has a
# valid one — never a blank/short value silently written to .env (framework
# fact:1499 CRITICAL 1: pressing Enter used to write NEO4J_PASSWORD= /
# PG_PASSWORD= as literal empty strings, and the install still reported
# success).
#
# ⭐ W7 round 3 (fact:1499 class, on the PUBLISHED agent install path this
# time): an EMPTY answer no longer re-prompts — it means "generate a strong
# password INTERNALLY, right here, in this process" (python3's
# secrets.token_hex(20), 40 hex characters — hex never contains '/', so it
# also always clears the Neo4j no-slash check below for free). Before this,
# AGENTS.md's Phase 1 had THE AGENT run `openssl rand -hex 20` in its OWN
# shell and pipe the result in — the value then existed in the agent's shell
# and its transcript, the exact fact:1499 class, on a path this framework
# actively tells agents to drive. Generating it in here instead means no
# agent shell and no agent transcript ever holds the plaintext, at any point.
# This ALSO retires two smaller, related hazards for free: fact:1499
# CRITICAL 1 itself (Enter used to write an empty password) is now
# impossible by construction (empty means "generate", never "accept
# blank"), and the desync where an empty piped line used to be REJECTED and
# consumed the NEXT answer line off the pipe (silently shifting every answer
# after it by one) cannot happen either, because empty is now a terminal,
# valid answer rather than a rejected one. (Answers that ARE rejected — too
# short, a '/' in the Neo4j password, surrounding whitespace — still
# re-prompt and so still consume the next piped line; that is by design and
# is why the documented printf supplies genuinely empty lines, not blanks.)
#
# The generated value NEVER reaches any process's argv: python3's own argv
# here is the literal, fixed script text `import secrets; print(...)` —
# never the secret — and the value leaves python3 only via its STDOUT, which
# this function's own `$(...)` command substitution captures into a shell
# variable. It is never echoed, printed to a terminal, or logged — the ONLY
# place it is ever written out is the single `printf '%s' "$v"` at the
# bottom of this function, which is the SAME stdout-capture path a
# human-typed password already used (pinned by
# tests/test_install_framework_password_validation.py) — both call sites
# capture it the same way — via command substitution — so nothing here is new exposure;
# an UNCAPTURED call to this function would print the value to whatever
# stdout is connected to, exactly as an uncaptured call already would have
# for a human-typed one. The install may say THAT a password was generated
# and WHERE it ends up (shared-memory/.env, mode 600) — never WHAT it is.
#
# A NON-EMPTY answer keeps today's behaviour exactly: strictly more than 8
# characters is required, 8-or-fewer is refused and re-prompts.
#
# On EXHAUSTED input (stdin closed, or a pipe with no more lines left) `read`
# itself fails — bash's own signal that there is no one left to answer. That
# is exactly the measured failure mode this guards: piping stdin with nothing
# left ran the whole install silently on an empty/default password. Here it
# is instead a hard, loud, nonzero-exit failure that names the step, rather
# than a silent fall-through to the empty string.
ask_secret() {  # prompt [mode] → echoes answer (input hidden), or exits 1
  # ⭐ W7/F7 — GENERATION IS FIRST-INSTALL-ONLY, AND THE MODE IS AN EXPLICIT
  # PARAMETER, NEVER READ OFF THE DISK HERE. The second argument is
  # "first-install" (an empty answer generates) or "overwrite" (an empty
  # answer re-prompts, exactly as it did before this cycle). The caller
  # captures it from the state of shared-memory/.env BEFORE writing anything.
  #
  # WHY IT MUST NOT BE DERIVED INSIDE THIS FUNCTION: the installer offers
  # `Overwrite? [y/N]` on an existing .env, and an empty answer there would be
  # a *successful* rotation to a value the operator is told is "not displayed,
  # not logged" — while the already-initialised Postgres and Neo4j volumes
  # still require the OLD passwords. Both stores would then refuse auth, with
  # no way back to the value that would have worked. That footgun did not
  # exist before this build, and it is what the mode parameter closes.
  #
  # WHY A PARAMETER AND NOT A READ OF $ENV_FILE: this block ships between the
  # ASK_SECRET markers and is extracted and run STANDALONE by
  # tests/test_install_framework_password_validation.py. A read of the live
  # env path would break under `set -u` in that harness, and would make the
  # result depend on whether the developer's own checkout happens to have a
  # .env — turning the suite red on a machine that is merely already
  # installed. The `${2:-first-install}` default keeps that standalone
  # contract; both shipped call sites pass the mode explicitly.
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
      # -I (isolated): no sitecustomize, no user site-packages, no PYTHONPATH
      # — a generator that can be reached by anything on the box is not a
      # generator. Its output is validated below, AFTER `$(...)` has stripped
      # the trailing newline; validating before that strip would make a
      # CORRECT generator fail its own check.
      v="$(python3 -I -c 'import secrets; print(secrets.token_hex(20))')" || gen_rc=$?
      if [ "$gen_rc" -ne 0 ] || [ -z "$v" ]; then
        echo "✗ ERROR: python3 failed while generating $1 (rc=$gen_rc) — refusing to continue. If python3 is missing or broken, run shared-memory/scripts/preflight.sh first; it diagnoses this directly and names the fix." >&2
        return 1
      fi
      # ⭐ W7/F9 — MEASURED: a `python3` earlier on PATH that prints a warning
      # line before the hex returns rc 0 and hands back
      # "WARNING_ON_STDOUT\n<hex>", which was ACCEPTED as the password and
      # then broke the container's NEO4J_AUTH=neo4j/<password> parsing. A
      # non-zero exit is not the only way a generator fails.
      if [[ ! "$v" =~ ^[0-9a-f]{40}$ ]]; then
        echo "✗ ERROR: python3 did not return a clean 40-character hex value while generating $1 — refusing to continue. Something on this host's python3 is writing to stdout before the value (a wrapper, a sitecustomize, a shell profile banner). Run shared-memory/scripts/preflight.sh; it diagnoses this directly and names the fix." >&2
        return 1
      fi
      echo "  (empty answer — generated a strong password internally; not displayed, not logged)" >&2
      printf '%s' "$v"
      return 0
    fi
    # ⭐ W7 — SURROUNDING WHITESPACE IS REFUSED, NOT SILENTLY STRIPPED. The
    # `IFS=` above stops `read` from trimming the answer, which is what an
    # operator whose password genuinely ends in a space needs. But keeping the
    # padding would hand the rest of the install a value its readers disagree
    # about: `docker compose --env-file` and secure_env.py STRIP surrounding
    # whitespace, while read_env() in init_db.sh, preflight.sh, postflight.sh
    # and reconcile_stack.sh PRESERVE it. A padded password therefore
    # initialises the stores under one value and is authenticated with another,
    # and an all-whitespace answer of 9+ characters would pass the length rule
    # below while rendering POSTGRES_PASSWORD empty and NEO4J_AUTH as `neo4j/`
    # — with preflight still reporting the password "set" (measured). So this
    # refuses it here, one keystroke from the fix, exactly as the '/' rule does
    # for Neo4j. Empty never reaches this point: it is terminal above.
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
# one keystroke to fix, instead of there.
#
# ⭐ W7/F10 — THE PROMPT TELLS THE TRUTH, AND IT DIFFERS BY PATH. It no longer
# advertises a command for the operator to run in their own shell (that value
# would live in their history and, on the agent install path, in a transcript
# — fact:1499). On a FIRST install an empty answer generates internally; on an
# OVERWRITE of an existing .env it re-prompts, because the databases were
# already initialised with the old password. Saying "press Enter" on the
# overwrite path would be an instruction that does not work.
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

# Fix round F1 (SEC HIGH) + F2 (QA MED-2): derive the default encoder
# endpoints NOW, before the FIRST byte of shared-memory/.env is written
# below — a failure here must never leave a half-configured install (F2),
# and a poisoned/malformed value must never reach the file at all (F1).
#
# F2: guarded with the same `|| rc=$?` capture idiom this script's
# pre-existing python3 site already uses (its own comment there: "this
# script is set -euo pipefail, so an unguarded non-zero would abort with
# .env already written"). AGENTS.md runs this script in Phase 1 and
# preflight.sh (which diagnoses a missing python3 by name) in Phase 2 — an
# unguarded failure here would abort from the WRONG phase with no named
# remedy, so a missing/broken python3 is named explicitly and preflight.sh
# is pointed at as the diagnostic.
#
# F1: SANITIZE-AND-REFUSE, not shell-quoting — quoting the echo below would
# still let a crafted default land in .env as a value nothing then checks;
# the actual defense is refusing to write anything unless the extracted
# value is non-empty, carries no whitespace/control character (the shape a
# newline-smuggled second KEY=VALUE line would take when later echoed), and
# is URL-shaped (scheme://host[:port]).
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

# W5 (R-D, decision:1824 §3): write the shipped compose's default encoder
# ENDPOINTS explicitly too — same append-block precedent as the Q3b block
# just above (EMBEDDER_URL/RERANKER_URL are commented in the template, so
# the awk substitution above cannot fill them in). Written unconditionally,
# regardless of the device answers above: this documents what a bundled
# install is actually using, not a device choice. Reranker: default
# written, never elicited here — an operator whose encoders live elsewhere
# (Q2's "existing endpoint" answer) edits both lines directly; AGENTS.md's
# Phase 4 already covers that path, including the vLLM reranker shim case.
# Fix round F1/F2: the actual extraction + validation runs EARLY, above,
# before shared-memory/.env has been written at all — $_embedder_url_default/
# $_reranker_url_default are already-validated plain script-level vars by
# the time this block runs.
{
  echo ""
  echo "# ── Encoder endpoints (install_framework.sh writes the framework's default explicitly) ──"
  echo "# This installs the bundled compose's own default port for each encoder. If your"
  echo "# encoders run somewhere else (Q2's 'existing endpoint' answer), edit these two lines —"
  echo "# see AGENTS.md Phase 4."
  echo "EMBEDDER_URL=$_embedder_url_default"
  echo "RERANKER_URL=$_reranker_url_default"
} >> "$ENV_FILE"
echo "  ✓ Encoder endpoints written: EMBEDDER_URL=$_embedder_url_default RERANKER_URL=$_reranker_url_default (edit if yours differ)"

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
# dependency. $SCRIPT_DIR (not a bare CWD-relative path) — consistent with
# every other invocation in this script.
#
# Fix round Q5/Q6 (agy MED): report-not-gate is preserved — 0/1/2 are
# check_config.py's OWN contract codes (would boot / would refuse / report
# itself failed) and all three stay a silent pass here, this script's job is
# to REPORT the config, never to let the reporter's own verdict kill an
# installer that has nothing left to fail on past this point (this script is
# `set -euo pipefail`, so an unguarded non-zero would abort with .env already
# written and every prior step already done). But a bare `|| true` also
# swallowed a SIGNAL KILL or crash outside that contract silently, reading
# exactly like success — rc is captured and anything past the tool's own
# 0/1/2 vocabulary is surfaced.
echo "Checking the configuration this install produced..."
rc=0; python3 "$SCRIPT_DIR/check_config.py" --phase-a-only || rc=$?
if [ "$rc" -gt 2 ]; then
  echo "⚠ check_config aborted (rc=$rc) — config report incomplete"
fi
