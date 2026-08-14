#!/usr/bin/env bash
#
# bootstrap_tokens.sh — mint agent tokens and wire them into the gateway .env.
#
# Runs generate_tokens.py and appends the AGENT_TOKENS (digest form) + AGENT_ROLES
# line to the gateway .env. generate_tokens.py's mint flow (Credential_Custody_Plan
# PR A2) writes each LOCAL agent's token straight into that agent's own skill .env
# (mode 600) — nothing is printed to this terminal. A REMOTE agent (no local skill
# install found on this machine) needs an explicit, human-run reveal — pass
# --reveal <name> to THIS script (repeatable) so it forwards to the SAME
# generate_tokens.py invocation that does the minting.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh
#   bash shared-memory/scripts/bootstrap_tokens.sh --reveal codex --reveal grok
#
# IMPORTANT: --reveal only shows a token from THIS invocation's mint. Running
# generate_tokens.py --reveal <name> separately, LATER, mints a FRESH set of
# tokens for every agent — a full rotation, not a free peek at the one you
# already registered.
#
# SAFETY: if AGENT_TOKENS is already set in .env this refuses to run — minting
# new tokens would invalidate every agent's existing token. Use --force only if
# you intend to rotate all tokens (local agents are redistributed automatically
# by the write-through mint flow; remote agents still need a manual --reveal,
# which --force accepts alongside --reveal on the SAME invocation).

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

force=0
reveal_args=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)  force=1; shift ;;
        --reveal) reveal_args+=(--reveal "${2:?--reveal needs an agent name}"); shift 2 ;;
        *)        red "✗ unknown argument: $1"; exit 1 ;;
    esac
done

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run: bash shared-memory/scripts/install_framework.sh"; exit 1; }

# Presence check before first use (defensive-bash rule from the sister
# project's install review) — a curated message beats bash's bare
# "uv: command not found" halfway through the run.
command -v uv >/dev/null 2>&1 || { red "✗ uv not found on PATH — install uv first (preflight.sh checks this)."; exit 1; }

# Guard: never silently overwrite a live token registry.
if grep -qE '^[[:space:]]*AGENT_TOKENS=.+' "$ENV_FILE" && [[ "$force" -eq 0 ]]; then
    ylw "AGENT_TOKENS is already set in $ENV_FILE — refusing to regenerate."
    ylw "Minting new tokens would break every agent that holds a current token."
    ylw "To rotate all tokens deliberately: bootstrap_tokens.sh --force"
    if [[ "${#reveal_args[@]}" -gt 0 ]]; then
        ylw "--reveal was requested, but there is nothing to reveal without minting —"
        ylw "reveal only ever shows a token from the SAME mint. Re-run with --force"
        ylw "if you mean to rotate every agent's token to get at this one."
    fi
    exit 0
fi

echo "Generating agent tokens ..."
out="$(cd "$REPO_ROOT" && uv run python shared-memory/scripts/generate_tokens.py "${reveal_args[@]}")"

tokens_line="$(grep -E '^AGENT_TOKENS=' <<<"$out" || true)"
roles_line="$(grep -E '^AGENT_ROLES='  <<<"$out" || true)"
[[ -n "$tokens_line" ]] || { red "✗ generate_tokens.py produced no AGENT_TOKENS line"; exit 1; }

# If forcing, strip any existing token/role lines before appending the new ones.
# Preserve the original file mode — a fresh temp file would otherwise be created
# with the default umask (often 0644), widening read access on a secrets file.
if [[ "$force" -eq 1 ]]; then
    grep -vE '^[[:space:]]*AGENT_(TOKENS|ROLES)=' "$ENV_FILE" > "$ENV_FILE.tmp"
    chmod --reference="$ENV_FILE" "$ENV_FILE.tmp" 2>/dev/null || true
    mv "$ENV_FILE.tmp" "$ENV_FILE"
fi

{
    echo ""
    echo "# Agent token registry (bootstrap_tokens.sh, $(date -u +%Y-%m-%d))"
    echo "$tokens_line"
    [[ -n "$roles_line" ]] && echo "$roles_line"
} >> "$ENV_FILE"

grn "✓ AGENT_TOKENS appended to $ENV_FILE (digest form)"
[[ -n "$roles_line" ]] && grn "✓ AGENT_ROLES (read-only monitor) appended"

echo
echo "Per-agent tokens were written straight into each LOCAL agent's skill .env"
echo "(S-01: mode 600, enforced from the first byte) by generate_tokens.py's"
echo "mint flow — nothing was printed here. If you ever paste a token into a"
echo "skill .env by hand instead, chmod 600 it yourself afterward."

if [[ "${#reveal_args[@]}" -gt 0 ]]; then
    echo
    ylw "⚠ REVEALING raw token value(s) below — you must be a human running this"
    ylw "  command directly, never through an agent. Agent transcripts are durable:"
    ylw "  piping this output through an agent turns \"shown once\" into \"stored forever\"."
    grep -E '^  [a-z_]+: AGENT_TOKEN=' <<<"$out" || true
else
    echo "For a REMOTE agent (no local install found on this machine), reveal its"
    echo "token on THE SAME mint invocation next time:"
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
