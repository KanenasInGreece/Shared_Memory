#!/usr/bin/env bash
# sync_skills.sh — distribute the *thin client* to every registered install.
#
# TWO INSTALL KINDS, from the AGENT_INSTALLS registry (`name:path` = kind
# `skill`, permanently; `name:kind:path` for anything else):
#   skill — a CLI agent's skill directory. Phases 1 and 2 below, unchanged.
#   mcp   — an MCP connector's walled directory. Receives the CONNECTOR package
#           (vector-skill.py, CONSTITUTION_SNIPPET_MCP.md, system-prompt.md),
#           never the CLI package and never mcp.json. See sync_mcp_install().
#
# ⛔ SYNC DELIVERS; IT NEVER CONFIGURES A HOST. No constitution file is spliced,
# no MCP host config is edited, no system prompt is set. It says which
# deliverable the host's kind calls for and leaves the applying to Phase 8/8b.
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

# ── The install-path REGISTRY (AGENT_INSTALLS), read from the gateway .env ────
#
# Standing candidate order for every loader in this family (apply.py,
# secure_env.py, bootstrap_tokens.sh): shared-memory/.env first, the repo-root
# .env as the pre-0.6 fallback. Parsed here rather than sourced — this file
# holds every credential the install has, and `source`ing it would execute
# whatever a malformed line happens to look like.
# SHARED_MEMORY_ENV_FILE overrides the candidate search — same reason
# SHARED_MEMORY_SYNC_AGENTS exists: without it the registry branch could only
# ever be exercised against this machine's real .env, so a test would have to
# read this script's source and believe it rather than run it.
_registry_env=""
for _cand in "${SHARED_MEMORY_ENV_FILE:-}" "$SRC/.env" "$REPO/.env"; do
  [ -n "$_cand" ] && [ -f "$_cand" ] && { _registry_env="$_cand"; break; }
done

# AGENT_INSTALLS is `name:path` or `name:kind:path`, comma-separated. The agent
# NAME is split off on the FIRST colon only (a legacy path may legitimately
# contain one); what remains is a KNOWN kind plus a path, or — the two-field
# form, which is permanent and never rewritten — a bare path meaning kind
# `skill`. This mirrors generate_tokens.py's _split_install_entry() exactly;
# the two parsers must agree, because the mint writes what this reads.
#
# The kind decides WHAT IS DELIVERED, and getting it wrong is not cosmetic:
#   skill — the CLI thin-client package, driven by MANIFEST.txt.
#   mcp   — the CONNECTOR package into a walled directory. Before this kind
#           existed, an MCP install registered its `.env` here like any other
#           agent and sync dumped SKILL.md + memory_bridge.py into the walled
#           directory: a CLI skill nothing there can run, next to a live token.
#
# The registry records each install's .env; the directory synced is its parent.
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

# The kind for a target directory, defaulting to `skill` for anything the
# registry does not name (the historical hardcoded four, and every
# SHARED_MEMORY_SYNC_AGENTS entry — both predate kinds and are CLI installs).
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
# Target selection, most specific first. The REGISTRY is preferred over the
# built-in list because it is the only source that knows where agents on THIS
# machine actually live: v0.9.27 replaced the guessed-from-name layout with
# AGENT_INSTALLS precisely because an install path is owned information about a
# host, not something a naming convention can be trusted to reproduce. The
# hardcoded four remain as the pre-registry fallback so an install that has not
# minted since the upgrade keeps working unchanged.
_default_dirs=(
  "$HOME/.claude/skills/shared-memory"
  "$HOME/.codex/skills/shared-memory"
  "$HOME/.gemini/skills/shared-memory"
  "$HOME/.grok/skills/shared-memory"
)

if [ -n "${SHARED_MEMORY_SYNC_AGENTS:-}" ]; then
  IFS=':' read -r -a AGENTS <<< "$SHARED_MEMORY_SYNC_AGENTS"
elif [ "${#registry_dirs[@]}" -gt 0 ]; then
  # ⛔ THE REGISTRY IS A UNION WITH WHAT IS ALREADY INSTALLED, NEVER A REPLACEMENT
  # FOR IT. The registry only starts existing when someone adds an agent, and it
  # then names ONLY that agent — every install that predates it is registered
  # nowhere. Treating it as the whole target list therefore drops the existing
  # installs the moment a new agent is added, silently and with no SKIP line,
  # leaving four agents pinned to whatever version they last received. Stale
  # copies fail silently, which is exactly why this project ships copies and
  # reports every refresh. So: everything registered, PLUS any historical
  # default that actually exists on disk. An unregistered path that is NOT
  # installed is still not conjured into existence.
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

# ── uv PATH reachability — the same silent failure preflight.sh now checks,
# printed from HERE instead where it actually matters for delivery. ─────────
#
# preflight.sh can only ask "is uv installed somewhere on this host" — it has
# no idea whether an agent skill is actually deployed. This script is the one
# place that DOES know: it is about to write (or has already written) real
# skill installs into real directories. So this is where the warning belongs
# for an operator who never ran (or re-ran) preflight after installing an
# agent — sync runs on every release, preflight does not.
#
# Mirrors preflight.sh's check exactly (see the long comment there for the
# full rationale): env -i clears the whole environment so no inherited PATH
# edit survives, and getconf PATH is the platform's own compiled-in default —
# the closest thing to "what a profile-free shell starts with" any POSIX host
# can answer, and it depends on neither uv nor python (this project's OWN
# instrument obligation, fact:1338/1321 — the check must not depend on the
# thing it is checking for).
#
# ONE warning for the whole run, not one per directory: the cause is a
# property of THIS HOST's PATH, not of any individual agent's install, and a
# warning repeated once per target would just restate the same fact four
# times. Gated on at least one install actually existing on disk — an
# operator syncing to nothing but --install targets that do not exist yet has
# nothing here to warn about (yet).
#
# ⚠ THIS APPLIES TO MCP INSTALLS TOO, AND MORE SHARPLY. This comment used to
# say an MCP-only host "is not broken by this at all" — measured wrong on a live
# conversion: an MCP host spawns its stdio server from a non-interactive,
# non-login shell, exactly like a CLI agent spawns the skill, and the shipped
# config template invokes a bare `uv`. On a host where uv sits in
# $HOME/.local/bin (the outcome of the recommended installer) that server never
# starts, and the host reports a dead MCP server rather than a PATH problem. The
# fix in an MCP config is to name uv by ABSOLUTE path.
#
# Non-fatal by design: sync's job is delivery, and a host that reaches uv some
# other way is not broken by this at all.
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

# ── MCP connector delivery (AGENT_INSTALLS kind `mcp`) ──────────────────────
#
# ⛔ THREE FILES, AND DELIBERATELY NOT A FOURTH.
#   vector-skill.py            the connector itself
#   CONSTITUTION_SNIPPET_MCP.md  the standing rules, for an AGENT host's own
#                              constitution file (Phase 8b splices it — SYNC
#                              NEVER DOES; see the note printed below)
#   system-prompt.md           the same rules wrapped for an LLM SERVER's
#                              system-prompt field (the LM Studio case)
#
# ⛔ NEVER mcp.json. It is a TEMPLATE: `YOUR_*` placeholders and a
# `/path/to/your/...` repo path. Copied into a live install it looks like
# configuration and is not — and a host that read it would try to authenticate
# with the literal string YOUR_LM_STUDIO_AGENT_TOKEN.
# ⛔ NEVER the CLI skill package. That is the whole reason the kind exists.
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

  # ⚠ The token .env is NEVER written here — same posture as the CLI path,
  # where update_skill.sh MERGES .env.example's defaults into a live .env
  # rather than copying over it. An MCP install's .env holds exactly one thing
  # that matters, the AGENT_TOKEN the mint wrote through, and this script has
  # no version of it to merge in. So: not copied, not templated, not touched —
  # only its mode is checked below.
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

  # ⚠ A CLI package sitting in an MCP install is REPORTED, never deleted. It is
  # what a pre-kind sync left behind, and it is dead weight next to a live
  # token — but removing files nobody asked to remove is not sync's call.
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

  # Sanity WITHOUT reading the token: byte-compile the delivered connector.
  # The documented verify step is check_memory_health, which is an MCP tool
  # call — it needs a running host and spends the credential, so it can neither
  # run here nor be run by an installing agent. py_compile answers the narrower
  # question this step actually owns: did a complete, parseable file land.
  if command -v python3 >/dev/null 2>&1; then
    if python3 -m py_compile "$dir/vector-skill.py" 2>/dev/null; then
      echo "  ✓ vector-skill.py byte-compiles"
    else
      echo "  ⚠ vector-skill.py FAILED to byte-compile — the copy is incomplete or corrupt:"
      python3 -m py_compile "$dir/vector-skill.py" || true
    fi
    # py_compile drops a 775 __pycache__ next to a 600 file in a 700 dir.
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

  # Gateway compatibility. The CLI path runs `memory_bridge.py doctor`, which
  # authenticates; there is no equivalent CLI mode on the connector, and a
  # probe that authenticated would have to READ THE TOKEN FILE — which this
  # script must never do. So the same comparison is made from the unauthenticated
  # /health payload: gateway api_version against the api_version of the file
  # just delivered. Non-fatal in every branch; a delivery is not wrong because a
  # gateway is down.
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

  # ⛔ SYNC NEVER CONFIGURES THE HOST. It delivers; a human or an agent following
  # Phase 8/8b applies. Say which deliverable belongs to which kind of host, so
  # "sync said done" is never mistaken for "the host is wired up".
  echo "  → This install is DELIVERED, not CONFIGURED. Still owed, by hand:"
  echo "     • AGENT host (its own constitution file): propose splicing the"
  echo "       marker-delimited block from CONSTITUTION_SNIPPET_MCP.md — ask first"
  echo "       (AGENTS.md Phase 8b), never write it silently."
  echo "     • LLM SERVER (a system-prompt field, e.g. LM Studio): paste"
  echo "       system-prompt.md into the model's system prompt."
  echo "     • Point the host's MCP config at $dir/vector-skill.py — an ABSOLUTE"
  echo "       uv path, since an MCP host spawns a non-login shell."
  echo "     • Restart BOTH the MCP host (it reads its env once, at spawn) and the"
  echo "       gateway if a token was minted since it started (auth is startup-frozen)."
  return 0
}

for dir in "${AGENTS[@]}"; do
  _dir_kind="$(_kind_for_dir "$dir")"
  if [ ! -d "$dir" ]; then
    # ⛔ --install CREATES A DIRECTORY ONLY FOR AN AGENT THE REGISTRY NAMES.
    # Without it this script only ever UPDATES an existing install, which is
    # correct by default — but on a genuinely fresh host it meant neither the
    # mint nor the sync would create the directory the other one needed, and
    # the operator was left to do it by hand or discover the gap the hard way.
    # A registered path is an explicit operator statement of where an agent
    # lives, so creating it is honouring the registry rather than guessing.
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

  # ── Kind fork. Everything below this point is the CLI skill package path,
  # byte-for-byte the behaviour it has always had; an `mcp` target takes the
  # connector path instead and never sees a line of it. ───────────────────────
  if [ "$_dir_kind" = "mcp" ]; then
    echo "── $(basename "$dir") (MCP connector install) ──"
    sync_mcp_install "$dir"
    echo ""
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
    # An MCP install never had a scripts/ directory to leave daemons in. Its own
    # leftovers (a CLI package a pre-kind sync delivered there) are REPORTED by
    # sync_mcp_install and removed by a human — --prune's contract is "daemon
    # copies older installs left behind", and silently widening it to delete
    # files next to a live token is not that.
    [ "$(_kind_for_dir "$dir")" = "mcp" ] && continue
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
  echo "Sync complete. --prune removes daemon scripts left by older installs; --install creates a registered target directory that does not exist yet."
fi
echo "Daemon/schema changes deploy on the GATEWAY host: git pull + migrations/apply.py + restart."
