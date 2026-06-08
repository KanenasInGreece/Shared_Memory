#!/usr/bin/env bash
# sync_skills.sh — distribute the *thin client* skill to all agent install paths.
#
# Run this after changing the client (memory_bridge.py) or SKILL.md:
#   bash shared-memory/scripts/sync_skills.sh           # sync the client
#   bash shared-memory/scripts/sync_skills.sh --prune    # sync + remove stale daemons
#
# WHAT SHIPS WITH THE SKILL (and what does NOT)
# ─────────────────────────────────────────────
# The skill is a thin HTTP client. The only script an agent executes is
# memory_bridge.py, which talks to the gateway on :8888. The daemons
# (hive_mind_proxy, coordinator, rem_loop, consolidation_loop, gpu_load,
# ontology) are SERVER-SIDE — they run on the gateway host from the framework
# repo, never from a skill directory. A remote agent has no DB/GPU and cannot
# run them. Shipping them into skill dirs is dead weight and gives a false
# sense of version coupling (the file in a skill dir is not the running
# gateway process). So they are deliberately excluded here.
#
# See shared-memory/Documentation/server-setup.md for the operations runbook.
#
# Symlinked client files are detected and skipped (already repo-linked).

set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/shared-memory"
PRUNE=0
[ "${1:-}" = "--prune" ] && PRUNE=1

AGENTS=(
  "$HOME/.claude/skills/shared-memory"
  "$HOME/.codex/skills/shared-memory"
  "$HOME/.gemini/skills/shared-memory"
  "$HOME/.grok/skills/shared-memory"
  "$REPO/shared-memory-skill/shared-memory"
)

# Client surface — the only scripts that belong in a skill package.
CLIENT_SCRIPTS=(
  memory_bridge.py
)

# Operations surface — server-side daemons. Listed here ONLY so --prune can
# remove copies that older installs left behind. Never copied into a skill.
DAEMON_SCRIPTS=(
  hive_mind_proxy.py
  coordinator.py
  rem_loop.py
  consolidation_loop.py
  gpu_load.py
  ontology.py
)

for dir in "${AGENTS[@]}"; do
  if [ ! -d "$dir" ]; then
    echo "SKIP (not installed): $dir"
    continue
  fi

  # SKILL.md
  cp "$SRC/SKILL.md" "$dir/SKILL.md" \
    && echo "✓ SKILL.md → $(basename "$dir")"

  # Client scripts
  for script in "${CLIENT_SCRIPTS[@]}"; do
    src="$SRC/scripts/$script"
    dest="$dir/scripts/$script"
    [ -f "$src" ] || continue
    if [ -L "$dest" ]; then
      echo "↔  symlink (auto-updated): $dest"
    elif [ -d "$(dirname "$dest")" ]; then
      if cp "$src" "$dest" 2>/dev/null; then
        echo "✓ $script → $(basename "$dir")"
      else
        echo "↔  same inode (repo-linked): $script"
      fi
    fi
  done

  # Prune daemons that should not live in a skill dir (flat copies or symlinks).
  if [ "$PRUNE" -eq 1 ]; then
    # SAFETY: if scripts/ is itself a directory symlink to the canonical repo
    # scripts dir, then "$dir/scripts/<daemon>" resolves to the REAL repo file —
    # rm -f would delete the framework's own daemons. Skip the whole dir; an
    # install like this must be converted to a thin client by hand (replace the
    # scripts-dir symlink with a dir containing only a memory_bridge.py symlink).
    if [ -L "$dir/scripts" ]; then
      echo "⚠  prune SKIPPED — $dir/scripts is a directory symlink (repo-linked);"
      echo "    pruning through it would delete the canonical daemons. Convert this"
      echo "    install to a thin client manually (see server-setup.md)."
    else
      for script in "${DAEMON_SCRIPTS[@]}"; do
        dest="$dir/scripts/$script"
        # -L test removes a stale symlink safely (unlinks the link, not its target);
        # a real flat copy is removed directly. Parent is a real dir here, so neither
        # operation can reach into the repo.
        if [ -e "$dest" ] || [ -L "$dest" ]; then
          rm -f "$dest" && echo "✗ pruned daemon: $script ← $(basename "$dir")"
        fi
      done
    fi
  fi
done

echo ""
if [ "$PRUNE" -eq 1 ]; then
  echo "Sync + prune complete. Skill dirs now carry the thin client only."
else
  echo "Sync complete. Run with --prune to remove daemon scripts left by older installs."
fi
echo "Daemon/schema changes deploy on the GATEWAY host: git pull + migrations/apply.py + restart."
