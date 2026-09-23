#!/usr/bin/env bash
# sync_skills.sh — distribute the thin client to every registered install.
#
# Two install kinds, from AGENT_INSTALLS (`name:path` is kind skill; `name:kind:path` otherwise):
#   skill — a CLI agent's skill directory. Phases 1 and 2 below.
#   mcp   — an MCP connector directory. Receives the connector package, never the CLI package and never mcp.json.
#
# Sync delivers files. It does not splice a constitution, edit an MCP host config, or set a system prompt.
#
# Run this after changing the client (memory_bridge.py, SKILL.md, or any file in shared-memory-skill/shared-memory/):
#   bash shared-memory/scripts/sync_skills.sh           # sync the client
#   bash shared-memory/scripts/sync_skills.sh --prune    # sync + remove stale daemons
#
# Phase 1 refreshes shared-memory-skill/shared-memory/ from the framework source. Remote update_skill.sh and phase 2 both pull from that copy.
# Phase 2 calls each install's own update_skill.sh with RAW_BASE=file:// and FORCE=1, the same path a remote client uses. A symlinked install is skipped so that replace does not turn the link into a static copy.
#
# The skill is a thin HTTP client: memory_bridge.py talks to the gateway on :8888. Server daemons stay on the gateway host and are not listed in the skill manifest.
#
# See shared-memory/Documentation/server-setup.md for the operations runbook.

set -e
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO/shared-memory"
SKILL_COPY="$REPO/shared-memory-skill/shared-memory"
PRUNE=0
INSTALL_MISSING=0
for _arg in "$@"; do
  case "$_arg" in
    --prune)   PRUNE=1 ;;
    --install) INSTALL_MISSING=1 ;;
    -h|--help)
      awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"
      exit 0
      ;;
    *)
      echo "✗ unknown argument: $_arg (try --help)" >&2
      exit 1
      ;;
  esac
done

# shared-memory/.env first, then the pre-0.6 repo-root file. Parse it; sourcing would execute a malformed line, and the file holds every credential.
# SHARED_MEMORY_ENV_FILE overrides that search so a test can run this branch without this machine's real .env.
_registry_env=""
for _cand in "${SHARED_MEMORY_ENV_FILE:-}" "$SRC/.env" "$REPO/.env"; do
  [ -n "$_cand" ] && [ -f "$_cand" ] && { _registry_env="$_cand"; break; }
done

# Split the agent name on the first colon only. A two-field entry stays kind skill forever; skill: and mcp: name the package to deliver. The mint writes what this reads, so the two parsers must agree.
# The registry stores each install's .env. The directory synced is its parent.
registry_dirs=()
registry_kinds=()
if [ -n "$_registry_env" ]; then
  _raw="$(sed -n 's/^[[:space:]]*AGENT_INSTALLS=//p' "$_registry_env" | tail -n1)"
  _raw="${_raw%\"}"; _raw="${_raw#\"}"
  if [ -n "$_raw" ]; then
    _old_ifs="$IFS"; IFS=','
    for _pair in $_raw; do
      _pair="$(printf '%s' "$_pair" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
      [ -z "$_pair" ] && continue
      case "$_pair" in *:*) ;; *) continue ;; esac
      _rest="${_pair#*:}"
      [ -z "$_rest" ] && continue
      _kind="skill"
      _envpath="$_rest"
      case "$_rest" in
        skill:*) _kind="skill"; _envpath="${_rest#skill:}" ;;
        mcp:*)   _kind="mcp";   _envpath="${_rest#mcp:}" ;;
      esac
      [ -z "$_envpath" ] && continue
      registry_dirs+=("$(dirname "$_envpath")")
      registry_kinds+=("$_kind")
    done
    IFS="$_old_ifs"
  fi
fi

# The kind for a target directory defaults to skill when the registry does not name it.
_kind_for_dir() {
  local _want="$1" _i=0
  while [ "$_i" -lt "${#registry_dirs[@]}" ]; do
    if [ "${registry_dirs[$_i]}" = "$_want" ]; then
      printf '%s' "${registry_kinds[$_i]}"
      return 0
    fi
    _i=$((_i + 1))
  done
  printf '%s' "skill"
}

# Phase 1 copies the manifest, the same list phase 2 ships. A second hardcoded list is how schema.md shipped stale while this script printed success.
# SHARED_MEMORY_SYNC_SKIP_TRACKED=1 skips this phase so a delivery test does not repair the drift it is trying to detect.
if [ "${SHARED_MEMORY_SYNC_SKIP_TRACKED:-}" = "1" ]; then
  echo "(skipping phase 1 — SHARED_MEMORY_SYNC_SKIP_TRACKED=1)"
else
while IFS= read -r rel; do
  case "$rel" in ""|\#*) continue ;; esac
  # Do not copy .env.example. The client file holds one token; the server file holds the database passwords, and the connector refuses a file that looks like the other.
  [ "$rel" = ".env.example" ] && continue
  src="$SRC/$rel"
  dest="$SKILL_COPY/$rel"
  # A manifest path with no framework twin is shipped by phase 2 and has nothing to refresh from here.
  [ -f "$src" ] || continue
  mkdir -p "$(dirname "$dest")"
  if cp "$src" "$dest" 2>/dev/null; then
    # A shell script must land executable. A plain edit does not preserve the +x bit.
    case "$rel" in *.sh) chmod +x "$dest" ;; esac
    echo "✓ $rel → shared-memory-skill (source of truth)"
  else
    echo "↔  same inode (repo-linked): $rel"
  fi
done < "$SKILL_COPY/MANIFEST.txt"
fi
echo ""

# Phase 2 delegates to each install's update_skill.sh, not a second copy loop. SHARED_MEMORY_SYNC_AGENTS overrides the directory list so a test can point at a temporary tree.
# Prefer AGENT_INSTALLS over the four guessed paths. Those four remain only so an install that has not minted since the upgrade still updates.
_default_dirs=(
  "$HOME/.claude/skills/shared-memory"
  "$HOME/.codex/skills/shared-memory"
  "$HOME/.gemini/skills/shared-memory"
  "$HOME/.grok/skills/shared-memory"
)

if [ -n "${SHARED_MEMORY_SYNC_AGENTS:-}" ]; then
  IFS=':' read -r -a AGENTS <<< "$SHARED_MEMORY_SYNC_AGENTS"
elif [ "${#registry_dirs[@]}" -gt 0 ]; then
  # The registry starts by naming only the agent just added. Union it with historical defaults that already exist, or those installs stop updating with no SKIP line. Do not create a path that is neither registered nor installed.
  AGENTS=("${registry_dirs[@]}")
  for _d in "${_default_dirs[@]}"; do
    [ -d "$_d" ] || continue
    _dup=0
    for _a in "${AGENTS[@]}"; do [ "$_a" = "$_d" ] && _dup=1; done
    [ "$_dup" = "0" ] && AGENTS+=("$_d") && _carried=$((${_carried:-0} + 1))
  done
  echo "Targets: ${#registry_dirs[@]} from the AGENT_INSTALLS registry + ${_carried:-0} unregistered install(s) already on disk"
  for _a in "${AGENTS[@]}"; do echo "  [$(_kind_for_dir "$_a")] $_a"; done
  echo ""
else
  AGENTS=("${_default_dirs[@]}")
fi

# Warn once if uv is missing from a profile-free PATH; MCP configs must name uv by absolute path.
_any_install_exists=0
for _d in "${AGENTS[@]}"; do
  [ -d "$_d" ] && _any_install_exists=1 && break
done
if [ "$_any_install_exists" = "1" ] && command -v uv >/dev/null 2>&1; then
  _sys_path="$(getconf PATH 2>/dev/null)"
  if [ -n "$_sys_path" ] && ! env -i PATH="$_sys_path" sh -c 'command -v uv' >/dev/null 2>&1; then
    echo "⚠ uv resolves ONLY when your shell profile is loaded — it is NOT on the"
    echo "  system default PATH ($_sys_path). This is the normal outcome of the"
    echo "  install this project recommends (curl -LsSf"
    echo "  https://astral.sh/uv/install.sh | sh puts uv under \$HOME/.local/bin and"
    echo "  counts on your profile to expose it), not a misconfiguration. Any AGENT"
    echo "  or MCP HOST installed below spawns a non-interactive, non-login shell to"
    echo "  run this client, and will be UNABLE to find uv. For a CLI agent the"
    echo "  failure is SILENT — it answers some other way (or saves nothing) instead"
    echo "  of reporting a broken memory system; for an MCP host the server simply"
    echo "  never starts, reported as a dead MCP server rather than a PATH problem."
    echo "  Fix ANY of these (all keep the upstream installer): symlink uv onto a"
    echo "  directory already on the system default PATH, e.g."
    echo "  sudo ln -s \"\$(command -v uv)\" /usr/local/bin/uv — set PATH inside the"
    echo "  affected agent's own configuration — or, for an MCP host, name uv by"
    echo "  ABSOLUTE path in its config's command line instead of a bare \"uv\"."
    echo ""
  fi
fi

# An mcp install gets vector-skill.py, CONSTITUTION_SNIPPET_MCP.md, and system-prompt.md. Not mcp.json, which is a template with placeholder tokens, and not the CLI skill package.
MCP_FILES=(vector-skill.py CONSTITUTION_SNIPPET_MCP.md system-prompt.md)

sync_mcp_install() {
  local dir="$1" rel src changed=0

  for rel in "${MCP_FILES[@]}"; do
    src="$REPO/mcp/$rel"
    if [ ! -f "$src" ]; then
      echo "  ⚠ $rel missing from $REPO/mcp — not delivered"
      continue
    fi
    if [ ! -L "$dir/$rel" ] && cmp -s "$src" "$dir/$rel"; then
      echo "=  $rel already current: $dir"
    else
      was_link=""
      [ -L "$dir/$rel" ] && was_link=" (replaced a symlink into the repo)"
      rm -f "$dir/$rel"
      cp "$src" "$dir/$rel"
      echo "✓ $rel REFRESHED (was stale or absent)$was_link: $dir"
      changed=1
    fi
  done

  # Do not write the token .env. This script has no token to merge, so it only checks the mode.
  if [ -f "$dir/.env" ]; then
    mode="$(stat -c %a "$dir/.env" 2>/dev/null || stat -f %Lp "$dir/.env" 2>/dev/null || echo "")"
    if [ "$mode" != "600" ]; then
      chmod 600 "$dir/.env"
      echo "  ✓ .env mode tightened to 600 (was ${mode:-unknown}): $dir"
    fi
  else
    echo "  ⚠ no .env in $dir — this install has no token yet. Mint one with:"
    echo "      bash shared-memory/scripts/bootstrap_tokens.sh --add <name> --mcp \\"
    echo "          --install-path $dir/.env"
  fi

  # A CLI package left in an MCP install is reported, not deleted. Removing files next to a live token is not this script's call.
  _strays=""
  for rel in SKILL.md MANIFEST.txt CONSTITUTION_SNIPPET.md scripts/memory_bridge.py \
             scripts/update_skill.sh Documentation/schema.md mcp.json; do
    [ -e "$dir/$rel" ] && _strays="$_strays $rel"
  done
  if [ -n "$_strays" ]; then
    echo "  ⚠ CLI-skill / template files found in this MCP install:$_strays"
    echo "    A sync that predates AGENT_INSTALLS kinds delivered them here. They are"
    echo "    inert (no MCP host runs them) but they sit beside a live token. Remove"
    echo "    them yourself when you have looked at them — sync will not."
  fi

  # Byte-compile the connector instead of calling check_memory_health. That tool needs a running host and would have to read the token.
  if command -v python3 >/dev/null 2>&1; then
    if python3 -m py_compile "$dir/vector-skill.py" 2>/dev/null; then
      echo "  ✓ vector-skill.py byte-compiles"
    else
      echo "  ⚠ vector-skill.py FAILED to byte-compile — the copy is incomplete or corrupt:"
      python3 -m py_compile "$dir/vector-skill.py" || true
    fi
    # py_compile leaves a __pycache__ next to a mode-600 file. Remove it.
    rm -rf "$dir/__pycache__"
  else
    echo "  (python3 not on PATH — skipped the byte-compile check)"
  fi

  # Modes LAST, so anything created above is caught: dir 700, files 600.
  chmod 700 "$dir" 2>/dev/null || echo "  ⚠ could not chmod 700 $dir"
  for rel in "${MCP_FILES[@]}"; do
    [ -f "$dir/$rel" ] && { chmod 600 "$dir/$rel" 2>/dev/null || echo "  ⚠ could not chmod 600 $dir/$rel"; }
  done
  echo "  ✓ modes enforced: directory 700, delivered files 600"

  # Compare api_version from unauthenticated /health. doctor would have to read the token file, and a down gateway does not make the delivery wrong.
  _probe_url="${COORDINATOR_URL:-http://localhost:8888}"
  _client_api="$(sed -n 's/^API_VERSION = \([0-9][0-9]*\).*/\1/p' "$dir/vector-skill.py" | head -n1)"
  if ! command -v curl >/dev/null 2>&1; then
    echo "  (curl not on PATH — skipped the gateway compatibility probe)"
  elif _health="$(curl -fsS --connect-timeout 3 --max-time 8 "$_probe_url/health" 2>/dev/null)"; then
    _gw_api="$(printf '%s' "$_health" | sed -n 's/.*"api_version"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -n1)"
    if [ -z "$_gw_api" ] || [ -z "$_client_api" ]; then
      echo "  ⚠ compat UNKNOWN — reached $_probe_url but could not read both api_versions."
    elif [ "$_gw_api" = "$_client_api" ]; then
      echo "  ✓ compat ok — connector and gateway both speak api_version $_client_api"
    else
      echo "  ⚠ INCOMPATIBLE — connector speaks api_version $_client_api, gateway $_gw_api."
      echo "    Upgrade whichever is behind; until then treat saves as unsafe."
    fi
  else
    echo "  ⚠ gateway not reachable at $_probe_url — compat UNKNOWN. This says nothing"
    echo "    about its version: no answer was received. The files delivered fine."
  fi

  # Delivery is not configuration. Say what the operator still has to apply, or "sync said done" is read as "the host is wired up".
  echo "  → This install is DELIVERED, not CONFIGURED. Still owed, by hand:"
  echo "     • AGENT host (its own constitution file): propose splicing the"
  echo "       marker-delimited block from CONSTITUTION_SNIPPET_MCP.md — ask first"
  echo "       (AGENTS.md Phase 8b), never write it silently. For OpenCode do not"
  echo "       guess ~/.config/opencode/AGENTS.md — a first install from \$HOME"
  echo "       often uses ~/AGENTS.md. Splice the file that session loaded."
  echo "     • LLM SERVER (a system-prompt field, e.g. LM Studio): paste"
  echo "       system-prompt.md into the model's system prompt."
  echo "     • Point the host's MCP config at $dir/vector-skill.py — an ABSOLUTE"
  echo "       uv path, since an MCP host spawns a non-login shell. For OpenCode"
  echo "       1.18.x that is mcp.shared-memory (type local, command as one array,"
  echo "       environment not env, timeout >= search cost). Do not copy mcp.json"
  echo "       (command/args/env) and do not nest under mcp.servers — 1.18 then"
  echo "       reports no MCP servers. Verify with: opencode mcp list"
  echo "     • ⚠ SPAWN LINE CHANGED: if your MCP host still holds the old"
  echo "       '--with fastmcp --with httpx' args, re-point it at"
  echo "       'uv run --no-project $dir/vector-skill.py' — the connector now"
  echo "       declares its own pinned dependencies inline, and sync delivers"
  echo "       files, it never edits a host config."
  echo "     • Restart BOTH the MCP host (it reads its env once, at spawn) and the"
  echo "       gateway if a token was minted since it started (auth is startup-frozen)."
  return 0
}

for dir in "${AGENTS[@]}"; do
  _dir_kind="$(_kind_for_dir "$dir")"
  if [ ! -d "$dir" ]; then
    # --install creates a directory only when the registry names it. The default is to update an existing install, not to guess a path.
    if [ "$INSTALL_MISSING" = "1" ] && [ "${#registry_dirs[@]}" -gt 0 ]; then
      _registered=0
      for _rd in "${registry_dirs[@]}"; do [ "$_rd" = "$dir" ] && _registered=1; done
      if [ "$_registered" = "1" ]; then
        mkdir -p "$dir"
        echo "CREATED (registered, --install): $dir"
      else
        echo "SKIP (not installed, not in registry): $dir"
        continue
      fi
    else
      echo "SKIP (not installed): $dir"
      continue
    fi
  fi
  # Refuse an install directory that is itself a symlink. Copying into it would write through the link into the tracked source.
  if [ -L "$dir" ]; then
    echo "⛔ REFUSING $dir — it is a symlink to $(readlink "$dir")."
    echo "   Copying into it would write into the source tree. Replace it with a"
    echo "   real directory:  rm '$dir' && mkdir -p '$dir'  then re-run."
    continue
  fi

  # A two-field registry entry stays kind skill, so a directory that already holds vector-skill.py would get the CLI package copied over the connector and the connector would never update (fact:1595).
  if [ "$_dir_kind" != "mcp" ] && [ -f "$dir/vector-skill.py" ]; then
    echo "⛔ REFUSING $dir — it holds vector-skill.py, so it is an MCP connector's"
    echo "   walled directory, but AGENT_INSTALLS registers it as kind 'skill'"
    echo "   (the two-field name:path form). The CLI package would be copied on"
    echo "   top of the connector and the connector itself would never update."
    echo "   Fix the entry in shared-memory/.env to  <name>:mcp:$dir/.env  and re-run."
    continue
  fi

  # ── Kind fork. Everything below this point is the CLI skill package path,
  # byte-for-byte the behaviour it has always had; an `mcp` target takes the
  # connector path instead and never sees a line of it. ───────────────────────
  if [ "$_dir_kind" = "mcp" ]; then
    echo "── $(basename "$dir") (MCP connector install) ──"
    sync_mcp_install "$dir"
    echo ""
    continue
  fi

  # Replace a symlinked scripts/ or Documentation/ before writing inside it. Otherwise rm of a file there deletes the repo's copy, not the install's.
  for sub in scripts Documentation; do
    if [ -L "$dir/$sub" ]; then
      echo "✓ $sub/ was a symlink into the repo — replacing it with a real directory: $dir"
      rm -f "$dir/$sub"
      mkdir -p "$dir/$sub"
    fi
  done

  # Refresh every manifest path, not a filename written here. A per-file exception is how SKILL.md and then schema.md stayed stale while sync printed success.
  # Replace a symlink with a real copy. cp follows a link and would write into the source tree, and a link into this checkout breaks every agent when the checkout moves.
  while IFS= read -r rel; do
    case "$rel" in ""|\#*) continue ;; esac
    # .env.example is merged by update_skill.sh. Copying it would overwrite AGENT_TOKEN.
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

  # Refresh update_skill.sh before invoking it, even if a copy exists. A stale copy is the program the delegation below would run.
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

  # Tighten .env to mode 600 again after update_skill.sh returns. That script may have exited before its own chmod.
  if [ -f "$dir/.env" ]; then
    mode="$(stat -c %a "$dir/.env" 2>/dev/null || stat -f %Lp "$dir/.env" 2>/dev/null || echo "")"
    if [ "$mode" != "600" ]; then
      chmod 600 "$dir/.env"
      echo "  ✓ .env mode tightened to 600 (was ${mode:-unknown}): $dir"
    fi
  fi
  echo ""
done

# These daemons are listed only so --prune can remove copies older installs left behind. They are never shipped to a skill.
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
    # --prune removes daemon copies, not files beside an MCP token. Those leftovers are reported above and left for a person.
    [ "$(_kind_for_dir "$dir")" = "mcp" ] && continue
    # If scripts/ is a symlink into the repo, rm would delete the framework's own daemons. Skip that install.
    if [ -L "$dir/scripts" ]; then
      echo "⚠  prune SKIPPED — $dir/scripts is a directory symlink (repo-linked);"
      echo "    pruning through it would delete the canonical daemons. Convert this"
      echo "    install to a thin client manually (see server-setup.md)."
      continue
    fi
    for script in "${DAEMON_SCRIPTS[@]}"; do
      dest="$dir/scripts/$script"
      # -e or -L removes a real copy or a stale symlink. The parent was checked not to be a repo symlink.
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
  echo "Sync complete. --prune removes daemon scripts left by older installs; --install creates a registered target directory that does not exist yet."
fi
echo "Daemon/schema changes deploy on the GATEWAY host: git pull + migrations/apply.py + restart."
