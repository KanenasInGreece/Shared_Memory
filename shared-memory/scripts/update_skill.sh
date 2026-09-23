#!/usr/bin/env bash
# update_skill.sh — self-update this shared-memory skill install.
#
# Fetches MANIFEST.txt on each run, over https:// or file://, so a new skill file does not require this script to change. sync_skills.sh reuses this same path instead of a second copy loop.
#
# Never overwrites .env, which holds AGENT_TOKEN. New keys from .env.example are added; an existing key is left alone.
#
# Usage: run from anywhere — self-locates via its own path:
#   bash ~/.claude/skills/shared-memory/scripts/update_skill.sh
set -uo pipefail

# --help prints this header and exits. Any other argument is refused, because this script used to ignore flags and run the update anyway.
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
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
# Overridable so a test can point at a mock, and so sync_skills.sh can reuse this one fetch path (RAW_BASE=file://, FORCE=1). Paths are relative to the skill root, not the repo.
RAW_BASE="${SHARED_MEMORY_UPDATE_RAW_BASE:-https://raw.githubusercontent.com/KanenasInGreece/Shared_Memory/main/shared-memory-skill/shared-memory}"
# FORCE=1 rewrites every file even when the content matches. Version equality no longer skips the run; the knob stays so local sync can force a rewrite.
FORCE="${SHARED_MEMORY_UPDATE_FORCE:-0}"
ENV_FILE="$SKILL_DIR/.env"
TMP_TAG="update_skill.$$"

cleanup() { rm -f "/tmp/${TMP_TAG}".*; }
trap cleanup EXIT

fetch() {
    # One failed connection returns 1. The caller decides whether that aborts the run.
    local err="/tmp/${TMP_TAG}.curlerr"
    if ! curl -fsSL --connect-timeout 10 --max-time 30 "$1" -o "$2" 2>"$err"; then
        echo "  ✗ could not reach $1"
        echo "    $(tail -1 "$err" 2>/dev/null)"
        return 1
    fi
    return 0
}

echo "Checking shared-memory skill at: $SKILL_DIR"
echo ""

# A missing or placeholder token is reported on its own, so it is not mistaken for a version skew.
if [ ! -f "$ENV_FILE" ]; then
    echo "⚠  No .env found at $ENV_FILE"
    echo "   This client has no AGENT_TOKEN configured — saves/searches will fail"
    echo "   auth even after updating. See SKILL.md § Authentication."
elif ! grep -q '^AGENT_TOKEN=' "$ENV_FILE" 2>/dev/null; then
    echo "⚠  .env exists but has no AGENT_TOKEN= line — auth will fail."
elif grep -q '^AGENT_TOKEN=tok_change_me' "$ENV_FILE" 2>/dev/null; then
    echo "⚠  AGENT_TOKEN is still the placeholder value — mint and set a real"
    echo "   token (see SKILL.md § Authentication) before relying on saves."
else
    echo "✓ AGENT_TOKEN is set."
fi
echo ""

# The manifest is data. A new skill file does not require this script to change.
MANIFEST="/tmp/${TMP_TAG}.manifest"
if ! fetch "$RAW_BASE/MANIFEST.txt" "$MANIFEST"; then
    echo "Could not check for updates (network unreachable). Nothing was changed."
    exit 1
fi

# Versions are read for the message only. Equality used to skip the update, so a SKILL.md-only release never reached clients; step 6 decides from each file's content.
LOCAL_VERSION=""
[ -f "$SCRIPT_DIR/memory_bridge.py" ] && \
    LOCAL_VERSION="$(grep -m1 '^VERSION = ' "$SCRIPT_DIR/memory_bridge.py" 2>/dev/null | sed 's/VERSION = "\(.*\)"/\1/')"

MB_STAGE="/tmp/${TMP_TAG}.memory_bridge.py"
if ! fetch "$RAW_BASE/scripts/memory_bridge.py" "$MB_STAGE"; then
    echo "Could not check for updates (network unreachable). Nothing was changed."
    echo "This client is currently at version: ${LOCAL_VERSION:-unknown}"
    exit 1
fi
REMOTE_VERSION="$(grep -m1 '^VERSION = ' "$MB_STAGE" 2>/dev/null | sed 's/VERSION = "\(.*\)"/\1/')"

if [ -n "$LOCAL_VERSION" ] && [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
    echo "Client and remote are both version $REMOTE_VERSION — comparing file"
    echo "contents anyway, because a release can change SKILL.md alone."
else
    echo "Update available: ${LOCAL_VERSION:-none installed} → $REMOTE_VERSION"
fi
echo ""

# Stage beside the destination, then rename. curl -o would truncate a file this script may still be reading. A failed fetch aborts before any real file is replaced; .env.example is merged, and this script is refreshed last.
declare -a STAGED_SRC STAGED_DEST
ENV_EXAMPLE_STAGE=""

while IFS= read -r rel || [ -n "$rel" ]; do
    case "$rel" in
        ""|\#*) continue ;;
    esac
    dest="$SKILL_DIR/$rel"

    if [ "$rel" = "scripts/memory_bridge.py" ]; then
        STAGED_SRC+=("$MB_STAGE"); STAGED_DEST+=("$dest")
        echo "   fetched memory_bridge.py"
        continue
    fi
    if [ "$rel" = ".env.example" ]; then
        ENV_EXAMPLE_STAGE="/tmp/${TMP_TAG}.env.example"
        if fetch "$RAW_BASE/$rel" "$ENV_EXAMPLE_STAGE"; then
            echo "✓ .env.example (used for merge below, not copied directly)"
        else
            echo "✗ .env.example fetch failed — .env merge will be skipped this run"
            ENV_EXAMPLE_STAGE=""
        fi
        continue
    fi
    if [ "$rel" = "scripts/update_skill.sh" ]; then
        continue   # this running script is refreshed after the other files land
    fi

    # A symlinked destination directory would write into the source checkout, so it is replaced with a real directory first.
    dest_dir="$(dirname "$dest")"
    if [ -L "$dest_dir" ]; then
        echo "   $(basename "$dest_dir")/ was a symlink — replacing with a real directory"
        rm -f "$dest_dir"
    fi
    mkdir -p "$dest_dir"
    stage="/tmp/${TMP_TAG}.$(echo "$rel" | tr '/' '_')"
    if fetch "$RAW_BASE/$rel" "$stage"; then
        STAGED_SRC+=("$stage"); STAGED_DEST+=("$dest")
        echo "   fetched $rel"
    else
        echo "✗ $rel fetch failed — aborting, nothing was changed"
        exit 1
    fi
done < "$MANIFEST"

# Add new .env keys from the example. Never change a key that already exists, commented or not.
if [ -n "$ENV_EXAMPLE_STAGE" ] && [ -f "$ENV_FILE" ]; then
    added=0
    while IFS= read -r line || [ -n "$line" ]; do
        key="$(echo "$line" | sed -n 's/^#\{0,1\}[[:space:]]*\([A-Z_][A-Z0-9_]*\)=.*/\1/p')"
        [ -z "$key" ] && continue
        if ! grep -q "^#\{0,1\}[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
            printf '\n%s\n' "$line" >> "$ENV_FILE"
            echo "  + added new .env key: $key (see .env for its default/comment)"
            added=$((added + 1))
        fi
    done < "$ENV_EXAMPLE_STAGE"
    [ "$added" -eq 0 ] && echo "✓ .env already has every known key — nothing added."
elif [ -n "$ENV_EXAMPLE_STAGE" ] && [ ! -f "$ENV_FILE" ]; then
    echo "  (skipping .env merge — no .env exists yet, see token warning above)"
fi

# The client .env holds the bearer token, so it is mode 600 even when this run did not otherwise touch it.
if [ -f "$ENV_FILE" ]; then
    current_mode="$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE" 2>/dev/null || echo "")"
    if [ "$current_mode" != "600" ]; then
        chmod 600 "$ENV_FILE"
        echo "✓ .env mode tightened to 600 (was ${current_mode:-unknown})"
    fi
fi

# Replace a staged file only when its own content differs, so one current file cannot skip another. REFRESHED and "already current" stay distinct; FORCE=1 rewrites anyway for local sync.
refreshed=0
unchanged=0
for i in "${!STAGED_SRC[@]}"; do
    src="${STAGED_SRC[$i]}"
    dst="${STAGED_DEST[$i]}"
    rel="${dst#"$SKILL_DIR"/}"
    # An installed file must be a real copy. cmp follows a symlink, so a link at identical content would stay "already current" forever.
    if [ -L "$dst" ]; then
        rm -f "$dst"
        mv "$src" "$dst"
        echo "✓  $rel REFRESHED (replaced a symlink with a real copy)"
        refreshed=$((refreshed + 1))
    elif [ "$FORCE" != "1" ] && cmp -s "$src" "$dst"; then
        echo "=  $rel already current"
        rm -f "$src"
        unchanged=$((unchanged + 1))
    else
        mv "$src" "$dst"
        echo "✓  $rel REFRESHED"
        refreshed=$((refreshed + 1))
    fi
done
echo ""
echo "Applied: $refreshed refreshed, $unchanged already current."

# Refresh this script last. rename() is safe on a running script, but only after every other file has landed.
UPDATE_SELF_STAGE="/tmp/${TMP_TAG}.update_skill.sh"
if fetch "$RAW_BASE/scripts/update_skill.sh" "$UPDATE_SELF_STAGE"; then
    if [ "$FORCE" != "1" ] && cmp -s "$UPDATE_SELF_STAGE" "$SCRIPT_DIR/update_skill.sh"; then
        echo "=  update_skill.sh (this script) already current"
    else
        chmod +x "$UPDATE_SELF_STAGE"
        mv "$UPDATE_SELF_STAGE" "$SCRIPT_DIR/update_skill.sh"
        echo "✓  update_skill.sh REFRESHED (this script, for next time)"
    fi
else
    echo "  (this script itself wasn't refreshed — everything else updated fine)"
fi

echo ""
echo "Verifying compatibility..."
# Run doctor as `uv run --with httpx`. A bare python3 passes only where httpx is already global, which is not a clean install.
if command -v uv >/dev/null 2>&1; then
    doctor_out="$(uv run --with httpx python "$SCRIPT_DIR/memory_bridge.py" doctor 2>&1)"
    status=$?
else
    # Without uv, still try python3 so a host that already has httpx gets a real verdict.
    doctor_out="$(python3 "$SCRIPT_DIR/memory_bridge.py" doctor 2>&1)"
    status=$?
fi
echo "$doctor_out"

if [ "$status" -eq 0 ]; then
    echo "Update complete — now at $REMOTE_VERSION, compat: ok."
elif printf '%s' "$doctor_out" | grep -q '"reachable"[[:space:]]*:[[:space:]]*false'; then
    # "reachable": false is not a version verdict. Naming a gateway upgrade here accuses a gateway this check never compared.
    echo ""
    echo "⚠ Updated to $REMOTE_VERSION, but the gateway could not be REACHED."
    echo "  This says nothing about its version — the client never got an answer."
    echo "  Check that it is running and that this client points at the right"
    echo "  address (COORDINATOR_URL in this skill's .env):"
    echo "    systemctl --user status hive-mind-gateway.service"
    echo "    curl -s http://localhost:8888/health"
    echo "  The skill files updated fine; only the check could not complete."
    exit "$status"
elif printf '%s' "$doctor_out" | grep -q '"compat"[[:space:]]*:'; then
    # A real version verdict has the JSON key "compat":. The bare word can appear in a traceback and must not be read as that verdict.
    echo ""
    echo "⚠ Updated to $REMOTE_VERSION but still incompatible. The GATEWAY itself"
    echo "  needs upgrading — that happens on its own host (git pull + restart),"
    echo "  not here. See Documentation/server-setup.md. Until then, treat save/"
    echo "  save_decision/save_retrospective as unsafe; search remains fine"
    echo "  (read-only)."
    exit "$status"
else
    # doctor exits 1 for a version mismatch and for a crash. Only a printed verdict may name the gateway; otherwise the client never reached it.
    echo ""
    echo "⚠ Updated to $REMOTE_VERSION, but the compatibility check could not RUN."
    echo "  This says nothing about the gateway — the client failed before"
    echo "  reaching it. The usual cause is a missing dependency: memory_bridge.py"
    echo "  needs httpx, which the documented invocation supplies via"
    echo "  'uv run --with httpx'. Install uv, or make httpx importable, then:"
    echo "    uv run --with httpx python $SCRIPT_DIR/memory_bridge.py doctor"
    echo "  The skill files themselves updated fine; only the check was skipped."
    exit "$status"
fi
