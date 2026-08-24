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

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the self-update ran anyway.
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
# Overridable so this script is actually testable against a local mock server
# — and, deliberately, so it is the ONE fetch/merge/atomic-replace
# implementation reused by sync_skills.sh (RAW_BASE=file://..., FORCE=1)
# rather than a second, separately-debugged copy of the same logic. Points at
# the SKILL root (the tracked shared-memory-skill/shared-memory directory),
# not the repo root — every manifest path is relative to this.
RAW_BASE="${SHARED_MEMORY_UPDATE_RAW_BASE:-https://raw.githubusercontent.com/KanenasInGreece/Shared_Memory/main/shared-memory-skill/shared-memory}"
# FORCE=1 re-applies every fetched file even when its content already matches.
# It used to mean "skip the version-equality early exit", which is no longer a
# thing that exists — the apply step compares CONTENT per file, so an unforced
# run already picks up a change that did not bump VERSION. Kept because the local
# dev sync wants unconditional re-application, and because a caller that has to
# defeat a staleness check is a smell worth keeping visible in one named knob.
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

# ── 3. Read both versions — for the MESSAGE, not as a skip condition.
#
#    Version equality used to exit 0 here with "Already up to date. Nothing to
#    do." That was wrong, and silently: the version compared is
#    memory_bridge.py's, so a release that changed only SKILL.md — the
#    ELICITATION surface, which decides what an operator is asked for before a
#    save — left every remote client reporting itself current while serving
#    stale prompts indefinitely. Nothing enforces "if SKILL.md changed, VERSION
#    must bump"; it was a human habit, and one docs-only merge would have broken
#    it. Same class as the sync_skills.sh symlink short-circuit fixed in 0.8.19.
#
#    So the decision is made on CONTENT, per file, at apply time (step 6): every
#    manifest file is fetched (a handful of small files — cheaper than being
#    wrong) and only files that actually differ are replaced. memory_bridge.py
#    is still fetched here once and reused below rather than fetched twice. ───
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
        continue   # handled last, after everything else has applied cleanly
    fi

    # A symlinked SUBDIRECTORY is dissolved before anything is written inside
    # it: applying into `scripts/` when that is a link into a source checkout
    # would write into the checkout, not the install. Same copy-only rule as the
    # files themselves.
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

# S-01 (Credential_Custody_Plan_2026-08-14, PR A2): the client .env holds
# this agent's bearer token — mode 600, always. Enforced here regardless of
# whether the merge above actually touched the file, so a pre-existing 644
# install (measured live: all four were 644 before this PR) self-heals on
# its next routine update, not only on first creation.
if [ -f "$ENV_FILE" ]; then
    current_mode="$(stat -c %a "$ENV_FILE" 2>/dev/null || stat -f %Lp "$ENV_FILE" 2>/dev/null || echo "")"
    if [ "$current_mode" != "600" ]; then
        chmod 600 "$ENV_FILE"
        echo "✓ .env mode tightened to 600 (was ${current_mode:-unknown})"
    fi
fi

# ── 6. Apply every staged file whose CONTENT differs — everything fetched
#    cleanly, so this is the only step that touches a real destination.
#
#    Per-file comparison is what makes this correct: no file can be skipped
#    because some OTHER file's version looked current. And each outcome prints
#    distinctly — "REFRESHED" must never read the same as "already current", or
#    the next silent drift is as undetectable as this one was. FORCE=1 re-applies
#    regardless, which is what the local dev sync wants. ────────────────────
refreshed=0
unchanged=0
for i in "${!STAGED_SRC[@]}"; do
    src="${STAGED_SRC[$i]}"
    dst="${STAGED_DEST[$i]}"
    rel="${dst#"$SKILL_DIR"/}"
    # ⛔ AN INSTALLED FILE IS ALWAYS A REAL COPY, NEVER A SYMLINK (Xenofon,
    # 2026-08-04). A link into a checkout breaks the moment that checkout is
    # moved, renamed or archived — silently, and for every agent at once. So a
    # symlink found here is REPLACED. `mv` already replaces the link rather than
    # writing through it, but the `rm -f` is explicit: `cmp` below FOLLOWS the
    # link, so a link pointing at identical content would otherwise be reported
    # "already current" and survive forever.
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

# Refresh this script itself last. temp-then-rename is safe to do to a script
# bash is still executing (rename() doesn't affect an already-open file
# descriptor), but there is no reason to risk it before everything else that
# matters has already landed.
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
# ⛔ RUN IT THE DOCUMENTED WAY. The published invocation is
#   uv run --with httpx python .../memory_bridge.py doctor
# and the invocation line IS the contract. This step used to call a bare
# `python3`, which only works where httpx happens to be importable globally —
# true on a developer box, false on a clean install. So the check passed
# wherever it was written and failed wherever it mattered.
if command -v uv >/dev/null 2>&1; then
    doctor_out="$(uv run --with httpx python "$SCRIPT_DIR/memory_bridge.py" doctor 2>&1)"
    status=$?
else
    # No uv: try anyway, so a host that has httpx installed system-wide still
    # gets a real verdict rather than a refusal.
    doctor_out="$(python3 "$SCRIPT_DIR/memory_bridge.py" doctor 2>&1)"
    status=$?
fi
echo "$doctor_out"

if [ "$status" -eq 0 ]; then
    echo "Update complete — now at $REMOTE_VERSION, compat: ok."
elif printf '%s' "$doctor_out" | grep -q '"reachable"[[:space:]]*:[[:space:]]*false'; then
    # ⛔ THE THIRD VARIANT OF THE SAME MISTAKE. The client ran and printed a
    # verdict, but the verdict is "I could not reach the gateway" — compat comes
    # back "unknown", not a version comparison. Reporting that as "the GATEWAY
    # needs upgrading" accuses a healthy gateway of being old when it is simply
    # not running, or is at a different address. Same failure as blaming it for
    # a missing local dependency: naming a component the check never actually
    # compared against.
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
    # The client RAN, REACHED the gateway, and returned a version verdict — so
    # the comparison is real and the gateway is the right thing to name.
    # ⚠ Matched as a JSON KEY (`"compat":`), not the bare word. A traceback that
    # merely MENTIONS compat — a source line like diag["compat"], a KeyError, a
    # path containing it — would otherwise be read as a verdict and bring the
    # false gateway accusation straight back. The key form cannot appear in a
    # traceback without the client having actually printed the object.
    echo ""
    echo "⚠ Updated to $REMOTE_VERSION but still incompatible. The GATEWAY itself"
    echo "  needs upgrading — that happens on its own host (git pull + restart),"
    echo "  not here. See Documentation/server-setup.md. Until then, treat save/"
    echo "  save_decision/save_retrospective as unsafe; search remains fine"
    echo "  (read-only)."
    exit "$status"
else
    # The client never got far enough to have an opinion about the gateway.
    # Saying "the gateway needs upgrading" here is a false accusation against a
    # component that was never contacted — and it is the message an operator
    # acts on. `doctor` exits 1 for a version verdict AND for an interpreter
    # crash, so the two are told apart by whether a verdict was actually
    # printed, not by the exit code.
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
