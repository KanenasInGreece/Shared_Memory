#!/usr/bin/env bash
#
# bootstrap_tokens.sh — mint agent tokens and wire them into the gateway .env.
#
# Runs generate_tokens.py and writes the AGENT_TOKENS (digest form) +
# AGENT_ROLES + AGENT_INSTALLS lines into the gateway .env, IN PLACE — see
# replace_registry_line() below. generate_tokens.py's mint flow
# (Credential_Custody_Plan PR A2) writes each agent's token straight into
# its own skill .env (mode 600) for every REGISTERED install path whose
# directory already exists — nothing is printed to this terminal except
# names, digests, and destination paths (never a raw token, unless
# --reveal is used). A REMOTE agent (no registered install path) needs an
# explicit, human-run reveal — pass --reveal <name> to THIS script
# (repeatable) so it forwards to the SAME generate_tokens.py invocation
# that does the minting.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh
#   bash shared-memory/scripts/bootstrap_tokens.sh --reveal codex --reveal grok
#
# Fresh-host finding D19: a REGISTERED install path whose skill directory
# does not exist YET (the skill package hasn't been installed on this
# machine) is REFUSED per-agent, loudly, rather than silently minting a
# token nobody can ever receive — see the "REFUSED" lines in this script's
# own output. Install the skill package, then re-run (a bulk rotation
# above, or --add for just that one agent, below).
#
#   bash shared-memory/scripts/bootstrap_tokens.sh --add codex \
#       --install-path ~/.codex/skills/shared-memory/.env
#
# Additive mint: registers exactly ONE new agent (growing the roster)
# without rotating anyone else's existing token — every other agent's
# digest in AGENT_TOKENS is left byte-identical. Refuses loudly if the name
# is already registered; there is no single-agent rotation, only --force
# below (all-or-nothing). --install-path is optional — omit it for a
# remote agent and pass --reveal instead.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh --add opencode --mcp \
#       --install-path ~/.config/opencode/shared-memory-mcp/.env
#
# --mcp registers the install as an MCP CONNECTOR install rather than a CLI
# skill install (AGENT_INSTALLS entry `name:mcp:path`). The path is still an
# .env FILE — the walled connector directory's own — and it is what
# sync_skills.sh then uses to deliver the CONNECTOR package there
# (vector-skill.py, CONSTITUTION_SNIPPET_MCP.md, system-prompt.md) instead of
# the CLI skill package. An entry with no kind (`name:path`) is a CLI skill
# install, permanently; nothing rewrites an existing line. --mcp requires
# --install-path and only combines with --add / --remint.
#
# IMPORTANT: --reveal only shows a token from THIS invocation's mint. Running
# generate_tokens.py --reveal <name> separately, LATER, as a bulk mint, mints
# a FRESH set of tokens for every agent in the roster — a full rotation, not
# a free peek at the one you already registered. (--add never rotates
# anyone regardless of --reveal.)
#
# SAFETY: if AGENT_TOKENS is already set in .env, a BULK mint refuses to run
# — minting new tokens would invalidate every agent's existing token. Use
# --force only if you intend to rotate all tokens (agents with a registered,
# existing install directory are redistributed automatically by the
# write-through mint flow; remote agents still need a manual --reveal, which
# --force accepts alongside --reveal on the SAME invocation). --add is
# exempt from this guard entirely — growing the roster by one is the whole
# point of it, and it never touches an existing agent's digest.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Framework env lives at shared-memory/.env; the repo-root path is the pre-0.6
# fallback — same resolution order as the gateway (hive_mind_proxy.py). Tokens
# MUST land in the file the gateway actually reads, or auth stays silently off.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

# Rewrites ENV_FILE in place, replacing the first LIVE *or* commented-out
# "$1=" assignment with "$2" (verbatim), appending it if no such line exists
# at all. D20 (fresh-host finding): the old code only ever APPENDED, which
# left TWO live AGENT_TOKENS= lines on a .env copied from .env.example (that
# file used to ship a live, empty AGENT_TOKENS= placeholder) — it worked only
# because the parser happens to take the last one, a parser-dependent
# arrangement, not a stable one, and the SAME file is passed to
# `docker compose --env-file`. Stripping any prior line (commented or live)
# before writing the fresh one means re-running this script against an
# OLDER .env that still carries that stale placeholder converges to exactly
# one live assignment, same as a fresh install would.
# ⛔ WRITE EVERY REGISTRY LINE IN ONE PASS. These used to be three sequential
# read-modify-write calls (AGENT_TOKENS, then AGENT_INSTALLS, then AGENT_ROLES),
# which had two failure modes, both found by review:
#
#   * Interruption between them leaves the file half-updated. The worst ordering
#     is the one that existed: a new agent gets its TOKEN written and its ROLE
#     not — and absence from AGENT_ROLES means FULL read/write, so a crash mid-run
#     hands out an unconfined credential.
#   * Two concurrent runs each read the same baseline and each rename their own
#     temp file over the result, so the later one silently discards the earlier
#     agent entirely.
#
# One temp file, all keys applied, one rename — plus the lock below, which is
# what makes "read the baseline" and "replace it" a single operation rather than
# two an interleaving run can slip between.
replace_registry_lines() {
    # Args: key1 line1 [key2 line2 ...]. A key whose line is empty is skipped,
    # so a caller need not know which optional registries were produced.
    local tmp; tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    cp "$ENV_FILE" "$tmp"
    while [[ $# -gt 0 ]]; do
        local key="$1" value_line="$2"; shift 2
        [[ -z "$value_line" ]] && continue
        local inner; inner="$(mktemp "${ENV_FILE}.XXXXXX")"
        grep -vE "^[[:space:]]*#?[[:space:]]*${key}=" "$tmp" > "$inner" || true
        printf '%s\n' "$value_line" >> "$inner"
        mv "$inner" "$tmp"
    done
    chmod --reference="$ENV_FILE" "$tmp" 2>/dev/null || true
    mv "$tmp" "$ENV_FILE"
}


force=0
add_name=""
install_path=""
reveal_args=()
install_kind_flag=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)         force=1; shift ;;
        --add)           add_name="${2:?--add needs an agent name}"; shift 2 ;;
        --remint)        remint_name="${2:?--remint needs an agent name}"; shift 2 ;;
        --role)          add_role="${2:?--role needs a role name}"; shift 2 ;;
        --install-path)  install_path="${2:?--install-path needs a path}"; shift 2 ;;
        --mcp)           install_kind_flag=(--mcp); shift ;;
        --reveal)        reveal_args+=(--reveal "${2:?--reveal needs an agent name}"); shift 2 ;;
        -h|--help)       awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)               red "✗ unknown argument: $1"; exit 1 ;;
    esac
done

if [[ -n "$install_path" && -z "$add_name" && -z "${remint_name:-}" ]]; then
    red "✗ --install-path only makes sense together with --add or --remint"
    exit 1
fi
if [[ "${#install_kind_flag[@]}" -gt 0 && -z "$add_name" && -z "${remint_name:-}" ]]; then
    # An install kind describes ONE registration. A bulk mint re-emits the whole
    # registry, each entry already carrying its own kind — accepting --mcp there
    # would read as "convert them all".
    red "✗ --mcp only makes sense together with --add or --remint"
    exit 1
fi
if [[ "${#install_kind_flag[@]}" -gt 0 && -z "$install_path" ]]; then
    # Refused HERE as well as in generate_tokens.py, deliberately: this is the
    # documented front door, and an operator who omits the path should be told
    # before a mint is even attempted rather than by a Python refusal two layers
    # down that looks like a script error.
    red "✗ --mcp needs --install-path <walled-dir>/.env — an install kind says"
    red "  what to deliver WHERE, and without a registered path there is nowhere."
    exit 1
fi
if [[ -n "${add_role:-}" && -z "$add_name" && -z "${remint_name:-}" ]]; then
    # A bulk mint derives every role from READ_ONLY_AGENTS. Accepting --role
    # there would look like it applied to all of them.
    red "✗ --role only makes sense together with --add or --remint"
    exit 1
fi
if [[ ( -n "$add_name" || -n "${remint_name:-}" ) && "$force" -eq 1 ]]; then
    red "✗ --add and --force are mutually exclusive — --add never rotates anyone,"
    red "  --force always rotates everyone. Run them separately."
    exit 1
fi

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run: bash shared-memory/scripts/install_framework.sh"; exit 1; }

# ── One minter at a time ─────────────────────────────────────────────────────
#
# Minting is read-modify-write on a shared file: generate_tokens.py reads the
# current AGENT_TOKENS, merges one entry, and prints a full replacement line
# this script then writes back. Two concurrent runs both read the same baseline
# and the later write silently DISCARDS the earlier agent — it is registered
# nowhere, while its token has already been written into that agent's skill .env
# (mode 600). The result is a credential that exists on disk and authenticates
# against nothing, which reads as a broken agent rather than a lost write.
#
# The lock spans read AND write, because locking only the write would still let
# two runs read the same baseline. Non-blocking with a clear message: a second
# operator should be told to wait, not left watching a silent hang.
_LOCKFILE="${ENV_FILE}.mintlock"
exec 8>"$_LOCKFILE" 2>/dev/null || true
if command -v flock >/dev/null 2>&1; then
    flock -n 8 || {
        red "✗ another bootstrap_tokens.sh is minting against $ENV_FILE right now."
        red "  Wait for it to finish and re-run — concurrent mints drop one"
        red "  agent's registration while still writing its token to disk."
        exit 1
    }
fi

# Presence check before first use (defensive-bash rule from the sister
# project's install review) — a curated message beats bash's bare
# "uv: command not found" halfway through the run.
command -v uv >/dev/null 2>&1 || { red "✗ uv not found on PATH — install uv first (preflight.sh checks this)."; exit 1; }

# ── Additive mint: grow the roster by one, never rotate ─────────────────────
if [[ -n "$add_name" || -n "${remint_name:-}" ]]; then
    if [[ -n "$add_name" && -n "${remint_name:-}" ]]; then
        red "✗ --add and --remint are mutually exclusive: one registers a NEW"
        red "  agent, the other re-issues an existing one."
        exit 1
    fi
    if [[ -n "$add_name" ]]; then
        echo "Adding agent '$add_name' ..."
        add_flags=(--add "$add_name")
    else
        add_name="$remint_name"          # shared reporting below
        echo "Re-issuing token for existing agent '$remint_name' ..."
        echo "  ⚠ this INVALIDATES its current token — the agent must receive the new one."
        add_flags=(--remint "$remint_name")
    fi
    [[ -n "$install_path" ]] && add_flags+=(--install-path "$install_path")
    [[ -n "${add_role:-}" ]] && add_flags+=(--role "$add_role")
    [[ "${#install_kind_flag[@]}" -gt 0 ]] && add_flags+=("${install_kind_flag[@]}")

    rc=0
    out="$(cd "$REPO_ROOT" && uv run python shared-memory/scripts/generate_tokens.py \
        "${add_flags[@]}" "${reveal_args[@]}" 2>&1)" || rc=$?
    echo "$out"

    if [[ "$rc" -ne 0 ]]; then
        red "✗ refused — nothing was minted, written, or registered (see above)."
        exit "$rc"
    fi

    tokens_line="$(grep -E '^AGENT_TOKENS=' <<<"$out" || true)"
    installs_line="$(grep -E '^AGENT_INSTALLS=' <<<"$out" || true)"
    # AGENT_ROLES is emitted only when the new agent actually needs a role — a
    # read-only identity (READ_ONLY_AGENTS), or an explicit --role. It carries
    # LEAST PRIVILEGE, and absence from it means FULL read/write in the gateway,
    # so failing to write it hands a dashboard a write-capable token. That is
    # exactly what this path used to do: the bulk mint printed the line and the
    # additive mint did not, so nobody noticed the roster was only half honoured.
    roles_line="$(grep -E '^AGENT_ROLES=' <<<"$out" || true)"
    [[ -n "$tokens_line" ]] || { red "✗ generate_tokens.py produced no AGENT_TOKENS line"; exit 1; }

    replace_registry_lines \
        "AGENT_TOKENS"   "$tokens_line" \
        "AGENT_INSTALLS" "$installs_line" \
        "AGENT_ROLES"    "$roles_line"

    echo
    grn "✓ AGENT_TOKENS updated in $ENV_FILE — '$add_name' added, every other"
    grn "  agent's digest is unchanged."
    [[ -n "$installs_line" ]] && grn "✓ AGENT_INSTALLS updated in $ENV_FILE"
    [[ -n "$roles_line" ]] && grn "✓ AGENT_ROLES updated in $ENV_FILE — '$add_name' is role-confined"
    echo
    ylw "Restart the gateway to load the new AGENT_TOKENS."
    exit 0
fi

# ── Bulk mint: the whole roster, first bootstrap or a deliberate rotation ──

# Guard: never silently overwrite a live token registry.
if grep -qE '^[[:space:]]*AGENT_TOKENS=.+' "$ENV_FILE" && [[ "$force" -eq 0 ]]; then
    ylw "AGENT_TOKENS is already set in $ENV_FILE — refusing to regenerate."
    ylw "Minting new tokens would break every agent that holds a current token."
    ylw "To add ONE new agent without touching anyone else: bootstrap_tokens.sh --add <name>"
    ylw "To rotate ALL tokens deliberately: bootstrap_tokens.sh --force"
    if [[ "${#reveal_args[@]}" -gt 0 ]]; then
        ylw "--reveal was requested, but there is nothing to reveal without minting —"
        ylw "reveal only ever shows a token from the SAME mint. Re-run with --force"
        ylw "if you mean to rotate every agent's token to get at this one."
    fi
    exit 0
fi

echo "Generating agent tokens ..."
out="$(cd "$REPO_ROOT" && uv run python shared-memory/scripts/generate_tokens.py "${reveal_args[@]}")"
echo "$out"

tokens_line="$(grep -E '^AGENT_TOKENS=' <<<"$out" || true)"
roles_line="$(grep -E '^AGENT_ROLES='  <<<"$out" || true)"
installs_line="$(grep -E '^AGENT_INSTALLS=' <<<"$out" || true)"
[[ -n "$tokens_line" ]] || { red "✗ generate_tokens.py produced no AGENT_TOKENS line"; exit 1; }

replace_registry_lines \
    "AGENT_TOKENS"   "$tokens_line" \
    "AGENT_INSTALLS" "$installs_line" \
    "AGENT_ROLES"    "$roles_line"

echo
grn "✓ AGENT_TOKENS written to $ENV_FILE (digest form)"
[[ -n "$roles_line" ]] && grn "✓ AGENT_ROLES (read-only roster + your declarations) written"
[[ -n "$installs_line" ]] && grn "✓ AGENT_INSTALLS (install-path registry) written"

echo
echo "Per-agent tokens were written straight into each registered LOCAL agent's"
echo "skill .env (S-01: mode 600, enforced from the first byte) by"
echo "generate_tokens.py's mint flow — see the per-agent report above. Any agent"
echo "REFUSED there (a registered path whose directory doesn't exist yet) got NO"
echo "token minted at all; install its skill package and re-run (bulk, or --add)."
echo "If you ever paste a token into a skill .env by hand instead, chmod 600 it"
echo "yourself afterward."

if [[ "${#reveal_args[@]}" -eq 0 ]]; then
    echo
    echo "For a REMOTE agent (no registered install path on this machine), reveal"
    echo "its token on THE SAME mint invocation next time:"
    echo
    echo "  bash shared-memory/scripts/bootstrap_tokens.sh --reveal <name>"
    echo
    echo "AGENT_TOKENS is now set in $ENV_FILE, so a LATER, separate reveal needs"
    echo "--force too — it mints a FRESH set of tokens for every agent (a full"
    echo "rotation), never a free peek at the one just registered:"
    echo
    echo "  bash shared-memory/scripts/bootstrap_tokens.sh --force --reveal <name>"
fi

echo
ylw "Restart the gateway to load the new AGENT_TOKENS."

# Security-review finding F4 / I-A10: generate_tokens.py's bulk mint()
# returns exit 0 even when one or more agents FAILED this round (a genuine
# write error, a missing directory, or a symlink refusal) -- deliberately,
# because the AGENT_TOKENS line already applied above is SAFE regardless
# (a failed agent's existing entry is carried forward unchanged, never
# dropped; see mint()'s own docstring). Returning nonzero from THAT script
# would have made the `out="$(...)"` capture above abort under `set -e`
# BEFORE this script ever echoed the report or applied the safe merge --
# exactly backwards. Instead, check for the marker HERE, after the safe
# write already happened, so automation still gets a distinguishable exit
# code without the report ever being suppressed.
if grep -q "PARTIAL FAILURE" <<<"$out"; then
    echo
    red "⚠ PARTIAL FAILURE during this mint — see the report above for which"
    red "  agent(s) are affected and how to recover. The registry written above"
    red "  IS safe as applied (no working credential was revoked) — but go fix"
    red "  the underlying issue for the affected agent(s) and re-run (bulk, or"
    red "  --add for just that one) once ready."
    exit 2
fi
