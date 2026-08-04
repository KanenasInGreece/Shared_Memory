#!/usr/bin/env bash
# sync_skills.sh — distribute the *thin client* skill to all agent install paths.
#
# Run this after changing the client (memory_bridge.py, SKILL.md, or any file
# in shared-memory-skill/shared-memory/):
#   bash shared-memory/scripts/sync_skills.sh           # sync the client
#   bash shared-memory/scripts/sync_skills.sh --prune    # sync + remove stale daemons
#
# TWO PHASES:
#  1. Refresh the repo's own tracked skill-copy directory
#     (shared-memory-skill/shared-memory/) from the framework source. This is
#     the actual distribution source everything else pulls from — whether a
#     remote client's update_skill.sh over https://, or phase 2 below over
#     file://. Direct cp here, since this IS the source of truth being built.
#  2. For every REAL agent install location, delegate to that location's own
#     update_skill.sh (RAW_BASE=file://<tracked skill copy>, FORCE=1) — the
#     exact same fetch/manifest/merge/atomic-replace logic a remote client
#     uses over the network, not a second, separately-debugged copy-loop.
#     FORCE=1 because local dev content can differ without VERSION having
#     bumped yet (the common case between releases).
#
# Symlinked installs are skipped entirely in phase 2 (already resolve
# straight to the repo's own files — nothing to sync, and update_skill.sh's
# mv-based atomic replace would otherwise silently convert the symlink into
# a static copy, which is exactly the state a symlinked dev install exists
# to avoid).
#
# WHAT SHIPS WITH THE SKILL (and what does NOT)
# ─────────────────────────────────────────────
# The skill is a thin HTTP client. The only Python an agent executes is
# memory_bridge.py, which talks to the gateway on :8888. The daemons
# (hive_mind_proxy, coordinator, rem_loop, consolidation_loop, gpu_load,
# ontology) are SERVER-SIDE — they run on the gateway host from the framework
# repo, never from a skill directory. A remote agent has no DB/GPU and cannot
# run them. Shipping them into skill dirs is dead weight and gives a false
# sense of version coupling (the file in a skill dir is not the running
# gateway process). So they are deliberately excluded here — the manifest
# that drives phase 2 (shared-memory-skill/shared-memory/MANIFEST.txt) never
# lists them.
#
# See shared-memory/Documentation/server-setup.md for the operations runbook.

set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/shared-memory"
SKILL_COPY="$REPO/shared-memory-skill/shared-memory"
PRUNE=0
[ "${1:-}" = "--prune" ] && PRUNE=1

# ── Phase 1: framework source → tracked skill copy (the distribution source) ─
#
# ⚠ DRIVEN BY MANIFEST.txt, THE SAME LIST PHASE 2 SHIPS. This used to be a
# hardcoded pair of filenames plus CLIENT_SCRIPTS=(memory_bridge.py
# update_skill.sh), while phase 2 and the parity test both read the manifest —
# so a file could be added to the manifest, ship to every agent, and be refreshed
# by nobody. That is exactly what happened to Documentation/schema.md: updated at
# source in two consecutive releases, never copied here, and shipped stale to
# every client for both of them while this script printed success.
#
# The manifest's own header promises "that's the whole maintenance surface".
# Now it is one for real, rather than one of two lists that must be kept in
# step by memory.
while IFS= read -r rel; do
  case "$rel" in ""|\#*) continue ;; esac
  # .env.example is NOT a copy — it is a DIFFERENT file that happens to share a
  # name. The client env holds only this agent's AGENT_TOKEN; the server env
  # holds PG_PASSWORD, NEO4J_PASSWORD and every agent's token, and vector-skill.py
  # refuses to load one that looks like the other. Copying it would ship exactly
  # the mistake that guard exists to prevent.
  [ "$rel" = ".env.example" ] && continue
  src="$SRC/$rel"
  dest="$SKILL_COPY/$rel"
  # Files that live ONLY in the skill tree (no source twin) are shipped by
  # phase 2 and have nothing to be refreshed from.
  [ -f "$src" ] || continue
  mkdir -p "$(dirname "$dest")"
  if cp "$src" "$dest" 2>/dev/null; then
    # A .sh script must land executable regardless of the source file's own
    # mode bit — a plain Write/edit doesn't preserve chmod +x, and that
    # silently ships a script nobody can run until someone notices.
    case "$rel" in *.sh) chmod +x "$dest" ;; esac
    echo "✓ $rel → shared-memory-skill (source of truth)"
  else
    echo "↔  same inode (repo-linked): $rel"
  fi
done < "$SKILL_COPY/MANIFEST.txt"
echo ""

# ── Phase 2: tracked skill copy → every REAL agent install, via THIS
#    project's own update_skill.sh — see header for why not a parallel loop. ─
AGENTS=(
  "$HOME/.claude/skills/shared-memory"
  "$HOME/.codex/skills/shared-memory"
  "$HOME/.gemini/skills/shared-memory"
  "$HOME/.grok/skills/shared-memory"
)

for dir in "${AGENTS[@]}"; do
  if [ ! -d "$dir" ]; then
    echo "SKIP (not installed): $dir"
    continue
  fi
  # SKILL.md is COPIED into every install — only memory_bridge.py is symlinked —
  # so it must be refreshed BEFORE the symlink short-circuit below, never after.
  # This copy used to sit after it, so an install whose script was a symlink was
  # declared "already current" as a whole and its SKILL.md was never touched
  # again. The symlink makes the SCRIPT auto-current and says nothing about the
  # capture surface: measured on this machine, three of four agents were serving
  # a SKILL.md many versions behind while sync reported them current every run.
  # That is the worst shape for this file to rot in, because SKILL.md IS the
  # elicitation surface — a stale copy asks the operator for the wrong fields and
  # nothing anywhere reports a problem.
  if [ ! -L "$dir/SKILL.md" ] && [ -f "$SKILL_COPY/SKILL.md" ]; then
    if cmp -s "$SKILL_COPY/SKILL.md" "$dir/SKILL.md"; then
      echo "=  SKILL.md already current: $dir"
    else
      cp "$SKILL_COPY/SKILL.md" "$dir/SKILL.md"
      echo "✓ SKILL.md REFRESHED (was stale): $dir"
    fi
  fi

  if [ -L "$dir/scripts/memory_bridge.py" ] || [ -L "$dir/scripts" ]; then
    echo "↔  scripts symlinked (repo-linked, auto-current): $dir"
    continue
  fi

  # Always refresh update_skill.sh from the tracked copy BEFORE invoking it —
  # never conditionally, even if one already exists. A stale copy (e.g. from
  # before a RAW_BASE convention change) would run with outdated path
  # assumptions and fail confusingly; this is the one file that must be
  # current for the delegation call below to even be correct.
  mkdir -p "$dir/scripts"
  cp "$SKILL_COPY/scripts/update_skill.sh" "$dir/scripts/update_skill.sh"
  chmod +x "$dir/scripts/update_skill.sh"

  echo "── $(basename "$(dirname "$(dirname "$dir")")")/$(basename "$dir") ──"
  if SHARED_MEMORY_UPDATE_RAW_BASE="file://$SKILL_COPY" \
     SHARED_MEMORY_UPDATE_FORCE=1 \
     bash "$dir/scripts/update_skill.sh"; then
    :
  else
    echo "  ⚠ update_skill.sh reported a problem for $dir — see output above."
  fi
  echo ""
done

# Operations surface — server-side daemons. Listed here ONLY so --prune can
# remove copies that older installs left behind. Never shipped to a skill.
DAEMON_SCRIPTS=(
  hive_mind_proxy.py
  coordinator.py
  rem_loop.py
  consolidation_loop.py
  gpu_load.py
  ontology.py
)

if [ "$PRUNE" -eq 1 ]; then
  for dir in "${AGENTS[@]}"; do
    [ -d "$dir" ] || continue
    # SAFETY: if scripts/ is itself a directory symlink to the canonical repo
    # scripts dir, then "$dir/scripts/<daemon>" resolves to the REAL repo file —
    # rm -f would delete the framework's own daemons. Skip the whole dir; an
    # install like this must be converted to a thin client by hand (replace the
    # scripts-dir symlink with a dir containing only a memory_bridge.py symlink).
    if [ -L "$dir/scripts" ]; then
      echo "⚠  prune SKIPPED — $dir/scripts is a directory symlink (repo-linked);"
      echo "    pruning through it would delete the canonical daemons. Convert this"
      echo "    install to a thin client manually (see server-setup.md)."
      continue
    fi
    for script in "${DAEMON_SCRIPTS[@]}"; do
      dest="$dir/scripts/$script"
      # -L test removes a stale symlink safely (unlinks the link, not its target);
      # a real flat copy is removed directly. Parent is a real dir here, so neither
      # operation can reach into the repo.
      if [ -e "$dest" ] || [ -L "$dest" ]; then
        rm -f "$dest" && echo "✗ pruned daemon: $script ← $(basename "$dir")"
      fi
    done
  done
fi

echo ""
if [ "$PRUNE" -eq 1 ]; then
  echo "Sync + prune complete. Skill dirs now carry the thin client only."
else
  echo "Sync complete. Run with --prune to remove daemon scripts left by older installs."
fi
echo "Daemon/schema changes deploy on the GATEWAY host: git pull + migrations/apply.py + restart."
