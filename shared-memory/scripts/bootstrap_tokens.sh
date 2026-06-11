#!/usr/bin/env bash
#
# bootstrap_tokens.sh — mint agent tokens and wire them into the gateway .env.
#
# Runs generate_tokens.py, appends the AGENT_TOKENS (and AGENT_ROLES) line to
# the gateway .env, and prints the per-agent token table so you can paste each
# agent's own AGENT_TOKEN into its skill .env.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh
#
# SAFETY: if AGENT_TOKENS is already set in .env this refuses to run — minting
# new tokens would invalidate every agent's existing token. Use --force only if
# you intend to rotate all tokens (and will redistribute them).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

force=0
[[ "${1:-}" == "--force" ]] && force=1

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run: cp .env.example .env"; exit 1; }

# Guard: never silently overwrite a live token registry.
if grep -qE '^[[:space:]]*AGENT_TOKENS=.+' "$ENV_FILE" && [[ "$force" -eq 0 ]]; then
    ylw "AGENT_TOKENS is already set in $ENV_FILE — refusing to regenerate."
    ylw "Minting new tokens would break every agent that holds a current token."
    ylw "To rotate all tokens deliberately: bootstrap_tokens.sh --force"
    exit 0
fi

echo "Generating agent tokens ..."
out="$(cd "$REPO_ROOT" && uv run python shared-memory/scripts/generate_tokens.py)"

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

grn "✓ AGENT_TOKENS appended to $ENV_FILE"
[[ -n "$roles_line" ]] && grn "✓ AGENT_ROLES (read-only monitor) appended"

echo
echo "Distribute each agent's own AGENT_TOKEN into its skill .env"
echo "(e.g. ~/.claude/skills/shared-memory/.env). Never share a token across agents:"
echo
grep -E 'AGENT_TOKEN=' <<<"$out" | grep -vE '^AGENT_TOKENS='
echo
ylw "Restart the gateway to load the new AGENT_TOKENS."
