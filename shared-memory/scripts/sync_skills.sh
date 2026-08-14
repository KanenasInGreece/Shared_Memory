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
#
# SHARED_MEMORY_SYNC_SKIP_TRACKED=1 skips this phase. It exists for the delivery
# TESTS: they execute this script for real, and phase 1 writes into the repo's
# own tracked copy — so a test run would silently REPAIR a genuine drift and make
# the parity guard pass vacuously, depending on nothing more than test order. A
# harness that repairs the thing it is meant to detect is worse than no harness.
if [ "${SHARED_MEMORY_SYNC_SKIP_TRACKED:-}" = "1" ]; then
  echo "(skipping phase 1 — SHARED_MEMORY_SYNC_SKIP_TRACKED=1)"
else
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
fi
echo ""

# ── Phase 2: tracked skill copy → every REAL agent install, via THIS
#    project's own update_skill.sh — see header for why not a parallel loop. ─
# Env-overridable (colon-separated), never a fixed layout: these four are OUR
# agent set, not THE agent set, and a deployment with different tools installed
# elsewhere must not need this file edited. It is also what makes phase 2's
# delivery testable at all — a test points it at a temporary tree and asserts
# what actually lands there, rather than reading this script's source and
# believing it.
if [ -n "${SHARED_MEMORY_SYNC_AGENTS:-}" ]; then
  IFS=':' read -r -a AGENTS <<< "$SHARED_MEMORY_SYNC_AGENTS"
else
  AGENTS=(
    "$HOME/.claude/skills/shared-memory"
    "$HOME/.codex/skills/shared-memory"
    "$HOME/.gemini/skills/shared-memory"
    "$HOME/.grok/skills/shared-memory"
  )
fi

for dir in "${AGENTS[@]}"; do
  if [ ! -d "$dir" ]; then
    echo "SKIP (not installed): $dir"
    continue
  fi
  # ⛔ AN INSTALL DIRECTORY THAT IS ITSELF A SYMLINK IS REFUSED. Copying into it
  # would write THROUGH the link into the repo's own tracked copy — the source
  # would silently become the destination. README used to offer exactly this
  # arrangement for one agent; it no longer does (see the copy-only rule below).
  if [ -L "$dir" ]; then
    echo "⛔ REFUSING $dir — it is a symlink to $(readlink "$dir")."
    echo "   Copying into it would write into the source tree. Replace it with a"
    echo "   real directory:  rm '$dir' && mkdir -p '$dir'  then re-run."
    continue
  fi

  # ⛔ A SYMLINKED SUBDIRECTORY IS EVEN MORE DANGEROUS THAN A SYMLINKED FILE, and
  # it must be dissolved BEFORE anything writes inside it. If `scripts/` is a
  # link into the repo, then `rm -f "$dir/scripts/memory_bridge.py"` below would
  # delete the REPO's copy, not the install's. Replace the link with a real
  # directory holding the same contents; nothing is lost, because the source of
  # those contents is the tracked skill tree we are about to copy from anyway.
  for sub in scripts Documentation; do
    if [ -L "$dir/$sub" ]; then
      echo "✓ $sub/ was a symlink into the repo — replacing it with a real directory: $dir"
      rm -f "$dir/$sub"
      mkdir -p "$dir/$sub"
    fi
  done

  # ⚠ EVERY MANIFEST FILE IS REFRESHED, AND THE LIST IS THE MANIFEST — NOT A
  # FILENAME WRITTEN HERE.
  #
  # This has now failed twice, the same way, because the fix was per-file both
  # times. First SKILL.md: an install whose script was a symlink was declared
  # "already current" as a whole, and three of four agents served a SKILL.md many
  # versions behind while sync reported them current every run. SKILL.md was
  # hoisted above the short-circuit — and then Documentation/schema.md was added
  # to the manifest and fell into exactly the same hole, missing entirely from
  # .codex and .grok while this script printed success.
  #
  # ⛔ EVERY INSTALLED FILE IS A REAL COPY — NEVER A SYMLINK INTO THIS REPO.
  # (Xenofon, 2026-08-04.) Repo-linking made a file auto-current at the cost of
  # binding every agent on the machine to this checkout's PATH: move, rename or
  # archive the project and all four installs break at once, silently, and the
  # only symptom is a skill that no longer runs. Staleness is the lesser risk
  # BECAUSE IT IS DETECTABLE — every file is content-compared on each sync, and
  # `doctor` reports version skew — whereas a dangling link is discovered by an
  # agent failing mid-task. It also matches how everyone ELSE gets this package:
  # update_skill.sh fetches it from GitHub and writes real files, so the local
  # dev path now produces the same result as the shipped one instead of a shape
  # only this machine has.
  #
  # An existing symlink is therefore REPLACED, not preserved and not written
  # through: `rm -f` first, because `cp` onto a symlink follows it and would
  # write into the source tree.
  while IFS= read -r rel; do
    case "$rel" in ""|\#*) continue ;; esac
    # .env.example is MERGED into a live .env by update_skill.sh, never copied
    # over it — a copy would overwrite this agent's AGENT_TOKEN.
    [ "$rel" = ".env.example" ] && continue
    [ -f "$SKILL_COPY/$rel" ] || continue
    if [ ! -L "$dir/$rel" ] && cmp -s "$SKILL_COPY/$rel" "$dir/$rel"; then
      echo "=  $rel already current: $dir"
    else
      was_link=""
      [ -L "$dir/$rel" ] && was_link=" (replaced a symlink into the repo)"
      mkdir -p "$(dirname "$dir/$rel")"
      rm -f "$dir/$rel"
      cp "$SKILL_COPY/$rel" "$dir/$rel"
      case "$rel" in *.sh) chmod +x "$dir/$rel" ;; esac
      echo "✓ $rel REFRESHED (was stale or absent)$was_link: $dir"
    fi
  done < "$SKILL_COPY/MANIFEST.txt"

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

  # S-01 (Credential_Custody_Plan_2026-08-14, PR A2), belt-and-braces: the
  # invoked update_skill.sh above already tightens $dir/.env to 600 as part
  # of its own merge step. This is a second, cheap check after it returns —
  # catches a .env this run's update_skill.sh never touched (e.g. it exited
  # early on a network hiccup) without depending on that script's internals.
  if [ -f "$dir/.env" ]; then
    mode="$(stat -c %a "$dir/.env" 2>/dev/null || stat -f %Lp "$dir/.env" 2>/dev/null || echo "")"
    if [ "$mode" != "600" ]; then
      chmod 600 "$dir/.env"
      echo "  ✓ .env mode tightened to 600 (was ${mode:-unknown}): $dir"
    fi
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
