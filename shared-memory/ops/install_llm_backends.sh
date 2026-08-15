#!/usr/bin/env bash
#
# install_llm_backends.sh — interactively configure one or more reasoning-LLM
# backends (local-supervised, remote/already-running, or a paid cloud API) and
# write them into shared-memory/.env as LLM_BACKENDS_JSON.
#
# NEVER asks for a literal API key — only the NAME of an env var you export it
# under yourself. See shared-memory/ops/README.md, "Reasoning-LLM backends",
# for why (and how to get that variable into the gateway's systemd service).
#
#   bash shared-memory/ops/install_llm_backends.sh
#
# Safe to re-run: each run REPLACES the LLM_BACKENDS_JSON line with what you
# enter this run — it does not merge with an earlier run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # …/shared-memory/ops
FRAMEWORK_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"                 # …/shared-memory
ENV_FILE="$FRAMEWORK_DIR/.env"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

command -v jq >/dev/null 2>&1 || {
    red "ERROR: jq not found — install it first (this script builds JSON with jq"
    echo "  rather than hand-rolled string escaping, which is exactly the kind of"
    echo "  bug that could put a broken or unintended value in your credential config)."
    exit 1
}
[[ -f "$ENV_FILE" ]] || { red "ERROR: $ENV_FILE not found — run install_framework.sh first."; exit 1; }
command -v systemctl >/dev/null 2>&1 || ylw "Note: systemctl not found — local-supervised backends will be skipped as an option."

ask()          { local v; read -r -p "$1 [$2]: " v; printf '%s' "${v:-$2}"; }
ask_required() { local v; while true; do read -r -p "$1: " v; [[ -n "$v" ]] && { printf '%s' "$v"; return; }; echo "  (required)"; done; }
yesno()        { local v; read -r -p "$1 [y/N]: " v; [[ "$v" =~ ^[Yy]$ ]]; }

echo "── Shared Memory — configure reasoning-LLM backends ──"
echo "Each backend is a URL the gateway load-balances across. Add as many as you like:"
echo "local hardware, a remote host you already run, and/or a paid cloud API."

entries=()
while true; do
    echo
    echo "── Backend $((${#entries[@]} + 1)) ──"
    url="$(ask_required "  Base URL (OpenAI-compatible, e.g. http://localhost:5000 or https://api.deepseek.com/v1)")"
    url="${url%/}"

    weight="$(ask "  Capacity weight (a faster/larger backend can take more load)" "1")"
    [[ "$weight" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { ylw "  Not a number — using 1."; weight="1"; }

    model=""
    if yesno "  Does this backend need a specific model id (a hosted/routing endpoint that validates it)?"; then
        model="$(ask_required "  Model id (e.g. deepseek-chat)")"
    fi

    token_env=""
    if yesno "  Does this backend need an API credential (a paid/cloud endpoint)?"; then
        echo "  Enter ONLY the NAME of an environment variable you will export the key"
        echo "  under yourself (e.g. DEEPSEEK_API_KEY) — NEVER the key itself. This"
        echo "  script and this framework never accept or store the literal key."
        while true; do
            token_env="$(ask_required "  Env var NAME")"
            if [[ "$token_env" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && ${#token_env} -le 64 ]]; then
                break
            fi
            ylw "  That doesn't look like an env var name (expected e.g. DEEPSEEK_API_KEY)."
            ylw "  If you just pasted a real key by mistake, enter its variable name instead."
        done
        echo "  Reminder: this framework never stores the literal key. Get it to the"
        echo "  gateway via (preferred, highest to lowest — SEC-06, PR A4):"
        echo "    1. systemd LoadCredential=  (see hive-mind-gateway.service's commented example)"
        echo "    2. ${token_env}_FILE=/path/to/secret  in shared-memory/.env"
        echo "    3. export $token_env=\$(...) + systemctl --user import-environment (DEPRECATED)"
        echo "  Full convention: shared-memory/ops/README.md, \"Reasoning-LLM backends\"."
    fi

    if command -v systemctl >/dev/null 2>&1 && yesno "  Does THIS machine run this backend, and should it be supervised as a systemd service?"; then
        label="$(ask_required "  Short label for the service (e.g. qwen3-a770 — used in the unit name)")"
        echo "  Paste the exact command that starts this backend (your own llama-server /"
        echo "  LM Studio CLI / etc. invocation — this script does not construct one for"
        echo "  you, hardware and model choice vary too much)."
        launch_cmd="$(ask_required "  Launch command")"
        unit_path="$HOME/.config/systemd/user/llm-backend-${label}.service"
        cat > "$unit_path" <<EOF
[Unit]
Description=Reasoning-LLM backend: $label ($url)
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
ExecStart=$launch_cmd
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
        systemctl --user daemon-reload
        systemctl --user enable --now "llm-backend-${label}.service"
        grn "  ✓ Installed + started llm-backend-${label}.service"
    fi

    entry=$(jq -n --arg url "$url" --arg weight "$weight" --arg model "$model" --arg token_env "$token_env" '
        {url: $url, weight: ($weight | tonumber)}
        + (if $model != "" then {model: $model} else {} end)
        + (if $token_env != "" then {token_env: $token_env} else {} end)
    ')
    entries+=("$entry")
    grn "  Added: $url"

    yesno "Add another backend?" || break
done

if [[ ${#entries[@]} -eq 0 ]]; then
    ylw "No backends entered — nothing written."
    exit 0
fi

json_array=$(printf '%s\n' "${entries[@]}" | jq -s -c '.')

# awk (not sed) for the same reason install_framework.sh uses it: the JSON value
# contains slashes and quotes that would need fragile escaping as a sed replacement.
#
# S-06: a fresh $ENV_FILE.tmp is created with the process umask (often 0644),
# which would widen $ENV_FILE's mode for the window between the awk write and
# the mv below. chmod --reference (the same pattern bootstrap_tokens.sh's
# --force rewrite already uses) copies $ENV_FILE's own mode onto the temp
# file BEFORE the mv, so there is no window where the secrets-bearing file
# sits at default permissions.
if grep -q '^LLM_BACKENDS_JSON=' "$ENV_FILE"; then
    awk -v new="LLM_BACKENDS_JSON=$json_array" '
        /^LLM_BACKENDS_JSON=/ { print new; next }
        { print }
    ' "$ENV_FILE" > "$ENV_FILE.tmp"
    chmod --reference="$ENV_FILE" "$ENV_FILE.tmp" 2>/dev/null || true
    mv "$ENV_FILE.tmp" "$ENV_FILE"
else
    {
        echo ""
        echo "# Added by install_llm_backends.sh"
        echo "LLM_BACKENDS_JSON=$json_array"
    } >> "$ENV_FILE"
fi

echo
grn "✓ Wrote LLM_BACKENDS_JSON to $ENV_FILE ($(echo "$json_array" | jq 'length') backend(s))"
echo "  No literal key was ever written to this file — only env var NAMES, per backend."
echo "  Restart the gateway to pick this up:"
echo "    systemctl --user restart hive-mind-gateway.service"
echo "  (or: bash shared-memory/ops/install_service.sh, if it isn't installed as a service yet)"
