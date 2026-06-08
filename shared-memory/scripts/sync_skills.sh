#!/usr/bin/env bash
# sync_skills.sh — copy canonical shared-memory skill files to all agent install paths.
#
# Run this after every code change:
#   bash shared-memory/scripts/sync_skills.sh
#
# Symlinked files (claude/codex/grok scripts) are detected and skipped automatically.
# Gemini CLI and shared-memory-skill receive full flat copies.

set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/shared-memory"

AGENTS=(
  "$HOME/.claude/skills/shared-memory"
  "$HOME/.codex/skills/shared-memory"
  "$HOME/.gemini/skills/shared-memory"
  "$HOME/.grok/skills/shared-memory"
  "$REPO/shared-memory-skill/shared-memory"
)

SCRIPTS=(
  memory_bridge.py
  rem_loop.py
  consolidation_loop.py
  gpu_load.py
  hive_mind_proxy.py
  ontology.py
  coordinator.py
)

for dir in "${AGENTS[@]}"; do
  if [ ! -d "$dir" ]; then
    echo "SKIP (not installed): $dir"
    continue
  fi

  # SKILL.md
  cp "$SRC/SKILL.md" "$dir/SKILL.md" \
    && echo "✓ SKILL.md → $(basename "$dir")"

  # Scripts
  for script in "${SCRIPTS[@]}"; do
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
done

echo ""
echo "Sync complete. Restart the gateway to pick up script changes."
