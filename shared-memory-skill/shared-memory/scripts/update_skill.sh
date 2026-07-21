#!/usr/bin/env bash
# update_skill.sh — self-update this shared-memory skill install.
#
# Ships INSIDE each skill install (alongside SKILL.md and scripts/memory_bridge.py)
# so a stale or incompatible client can update itself without depending on
# anything else that might also be stale — fetches fresh files from a
# MANIFEST (MANIFEST.txt, fetched fresh each run), never a hardcoded file
# list, so a future file added to the skill package doesn't need this script
# to change too. Works identically over https:// (a real remote update) or
# file:// (used by sync_skills.sh for local development sync against
# uncommitted local changes — the SAME tested logic, not a second copy of it).
#
# NEVER overwrites .env — that file holds this agent's AGENT_TOKEN. New
# optional keys introduced by a framework upgrade are ADDED (never overwriting
# an existing key), so an env-upgrade can't silently strand a client on a
# stale .env while also never touching what's already configured. .env itself
# is never in the manifest — only .env.example is, which drives that merge.
#
# Usage: run from anywhere — self-locates via its own path:
#   bash ~/.claude/skills/shared-memory/scripts/update_skill.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(dirname "$SCRIPT_DIR")"
# Overridable so this script is actually testable against a local mock server
# — and, deliberately, so it is the ONE fetch/merge/atomic-replace
# implementation reused by sync_skills.sh (RAW_BASE=file://..., FORCE=1)
# rather than a second, separately-debugged copy of the same logic. Points at
# the SKILL root (the tracked shared-memory-skill/shared-memory directory),
# not the repo root — every manifest path is relative to this.
RAW_BASE="${SHARED_MEMORY_UPDATE_RAW_BASE:-https://raw.githubusercontent.com/KanenasInGreece/Shared_Memory/main/shared-memory-skill/shared-memory}"
# FORCE=1 skips the version-equality skip below — needed for local dev sync,
# where content can differ without VERSION having bumped yet (the common case
# between releases). A real remote update always wants the skip, so this stays
# unset there.
FORCE="${SHARED_MEMORY_UPDATE_FORCE:-0}"
ENV_FILE="$SKILL_DIR/.env"
TMP_TAG="update_skill.$$"

cleanup() { rm -f "/tmp/${TMP_TAG}".*; }
trap cleanup EXIT

fetch() {
    # fetch <url> <dest> — graceful on any network failure, never aborts the
    # whole script on one bad connection; caller checks the return code.
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

# ── 1. Token check — distinct from compat, so a missing/placeholder token
#    doesn't get misread as a version-skew problem. ─────────────────────────
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

# ── 2. Fetch the manifest — the list of files to sync is data, not code, so
#    a new file shipping with the skill never requires this script to change. ─
MANIFEST="/tmp/${TMP_TAG}.manifest"
if ! fetch "$RAW_BASE/MANIFEST.txt" "$MANIFEST"; then
    echo "Could not check for updates (network unreachable). Nothing was changed."
    exit 1
fi

# ── 3. Version check FIRST — don't touch anything if nothing changed
#    (unless FORCE=1). memory_bridge.py is the version anchor; fetched here
#    once and reused in the copy loop below rather than fetched twice. ─────
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

if [ "$FORCE" != "1" ] && [ -n "$LOCAL_VERSION" ] && [ "$LOCAL_VERSION" = "$REMOTE_VERSION" ]; then
    echo "✓ Already up to date (version $LOCAL_VERSION). Nothing to do."
    exit 0
fi

echo "Update available: ${LOCAL_VERSION:-none installed} → $REMOTE_VERSION"
echo ""

# ── 4. Fetch every manifest file to <dest>.new (temp-then-atomic-rename —
#    curl -o would truncate-in-place into a file bash may still be reading
#    mid-execution for this script's own case, handled specially below).
#    .env.example is fetched but applied via merge, not a direct copy.
#    scripts/update_skill.sh is this running script — refreshed last.
#    Any fetch failure aborts before ANYTHING is applied (step 6). ──────────
declare -a STAGED_SRC STAGED_DEST
ENV_EXAMPLE_STAGE=""

while IFS= read -r rel || [ -n "$rel" ]; do
    case "$rel" in
        ""|\#*) continue ;;
    esac
    dest="$SKILL_DIR/$rel"

    if [ "$rel" = "scripts/memory_bridge.py" ]; then
        STAGED_SRC+=("$MB_STAGE"); STAGED_DEST+=("$dest")
        echo "✓ memory_bridge.py"
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
        continue   # handled last, after everything else has applied cleanly
    fi

    mkdir -p "$(dirname "$dest")"
    stage="/tmp/${TMP_TAG}.$(echo "$rel" | tr '/' '_')"
    if fetch "$RAW_BASE/$rel" "$stage"; then
        STAGED_SRC+=("$stage"); STAGED_DEST+=("$dest")
        echo "✓ $rel"
    else
        echo "✗ $rel fetch failed — aborting, nothing was changed"
        exit 1
    fi
done < "$MANIFEST"

# ── 5. .env additive merge — copy in any NEW keys an upgrade introduced,
#    never touching a key that already exists (by name, commented or not). ─
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

# ── 6. Now actually apply every staged file — everything fetched cleanly,
#    so this is the only step that touches a real destination. ─────────────
for i in "${!STAGED_SRC[@]}"; do
    mv "${STAGED_SRC[$i]}" "${STAGED_DEST[$i]}"
done

# Refresh this script itself last. temp-then-rename is safe to do to a script
# bash is still executing (rename() doesn't affect an already-open file
# descriptor), but there is no reason to risk it before everything else that
# matters has already landed.
UPDATE_SELF_STAGE="/tmp/${TMP_TAG}.update_skill.sh"
if fetch "$RAW_BASE/scripts/update_skill.sh" "$UPDATE_SELF_STAGE"; then
    chmod +x "$UPDATE_SELF_STAGE"
    mv "$UPDATE_SELF_STAGE" "$SCRIPT_DIR/update_skill.sh"
    echo "✓ update_skill.sh (this script, refreshed for next time)"
else
    echo "  (this script itself wasn't refreshed — everything else updated fine)"
fi

echo ""
echo "Verifying compatibility..."
if python3 "$SCRIPT_DIR/memory_bridge.py" doctor; then
    echo "Update complete — now at $REMOTE_VERSION, compat: ok."
else
    status=$?
    echo ""
    echo "⚠ Updated to $REMOTE_VERSION but still incompatible. The GATEWAY itself"
    echo "  needs upgrading — that happens on its own host (git pull + restart),"
    echo "  not here. See Documentation/server-setup.md. Until then, treat save/"
    echo "  save_decision/save_retrospective as unsafe; search remains fine"
    echo "  (read-only)."
    exit "$status"
fi
