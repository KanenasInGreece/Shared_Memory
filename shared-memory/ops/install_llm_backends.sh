#!/usr/bin/env bash
#
# install_llm_backends.sh — write LLM_BACKENDS_JSON. It asks for the env-var name of a key, never the key. Re-running replaces the line; it does not merge.
# A credentialed entry over plaintext http to a public host is excluded by the gateway. This script does not write plaintext_ok.
#
#   bash shared-memory/ops/install_llm_backends.sh

set -euo pipefail

# --help used to fall through into the prompts. Unknown arguments must refuse.
for _arg in "$@"; do
    case "$_arg" in
        -h|--help)
            awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            printf '\033[31m%s\033[0m\n' "✗ unknown argument: $_arg (this script takes none — see --help)" >&2
            exit 1
            ;;
    esac
done

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

# Only S-05 still refuses to start: a credentialed backend while AGENT_TOKENS is unset. M-5 and P-5 are warnings since W4 (decision:1824); this block asks the access question instead of leaving it to the startup log.
#
# Context is read from the URL. A missing answer is a refusal, not a default.
#
# >>> BACKEND_ACCESS
# Stays inside the marker so the extracted test block can call build_backend_entry on its own.
# A defined backend with no n_ctx stops the gateway. Query the URL; if it does not say, ask the operator. Never guess.
# A provider key goes to curl on stdin (-K -), never on the argument list.
_probe_url() {
    local target="$1" token_env="$2"
    if [[ -n "$token_env" && -n "${!token_env:-}" ]]; then
        curl -sf -K - "$target" <<< "header = \"Authorization: Bearer ${!token_env}\"" 2>/dev/null || true
    else
        curl -sf "$target" 2>/dev/null || true
    fi
}

_query_advertised_n_ctx() {
    local url="$1" token_env="$2" body base
    base="${url%/}"
    base="${base%/v1}"
    body="$(_probe_url "${base}/props" "$token_env")"
    if [[ -n "$body" ]] && command -v jq >/dev/null 2>&1; then
        local n
        n="$(printf '%s' "$body" | jq -r '.default_generation_settings.n_ctx // empty' 2>/dev/null || true)"
        if [[ "$n" =~ ^[1-9][0-9]*$ ]]; then
            printf '%s' "$n"
            return 0
        fi
    fi
    body="$(_probe_url "${base}/v1/models" "$token_env")"
    if [[ -n "$body" ]] && command -v jq >/dev/null 2>&1; then
        local n
        n="$(printf '%s' "$body" | jq -r '[.. | objects | .max_context_length? // empty] | map(select(type=="number")) | first // empty' 2>/dev/null || true)"
        if [[ "$n" =~ ^[1-9][0-9]*$ ]]; then
            printf '%s' "$n"
            return 0
        fi
    fi
    return 1
}

_ask_positive_int() {
    local prompt="$1" raw
    while true; do
        if ! read -r -p "$prompt" raw; then
            echo "  No more input on stdin — refusing to guess." >&2
            return 1
        fi
        if [[ "$raw" =~ ^[1-9][0-9]*$ ]]; then
            printf '%s' "$raw"
            return 0
        fi
        echo "  Enter a positive integer. There is no default." >&2
    done
}

build_backend_entry() {
    local url="$1" weight="$2" model="$3" token_env="$4" env_file="$5"
    local n_ctx="" max_out="" advertised="" answer=""
    if [[ -n "$token_env" ]] && ! grep -qE '^[[:space:]]*AGENT_TOKENS=[^[:space:]]' "$env_file" 2>/dev/null \
        && ! grep -qE '^[[:space:]]*ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=(1|true|yes|on)' "$env_file" 2>/dev/null; then
        echo "  ⚠ AGENT_TOKENS is not set in $env_file. A provider key with auth off refuses" >&2
        echo "    startup unless ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1 is set. The chat route" >&2
        echo "    still admits only a spawned daemon." >&2
    fi
    if advertised="$(_query_advertised_n_ctx "$url" "$token_env")"; then
        echo "  The URL advertised n_ctx=${advertised}." >&2
        if ! read -r -p "  Use ${advertised}? [Y/n]: " answer; then
            echo "  No more input on stdin — refusing to guess n_ctx." >&2
            return 1
        fi
        if [[ "$answer" =~ ^[Nn]$ ]]; then
            n_ctx="$(_ask_positive_int "  n_ctx (positive integer, no default): ")" || return 1
        else
            n_ctx="$advertised"
        fi
    else
        echo "  COULD NOT READ THE CONTEXT from ${url}." >&2
        echo "  /props had no default_generation_settings.n_ctx and /v1/models had no max_context_length." >&2
        echo "  A defined backend with no n_ctx stops the gateway. Ask the operator. Never guess." >&2
        n_ctx="$(_ask_positive_int "  n_ctx (positive integer, no default): ")" || return 1
    fi
    max_out="$(_ask_positive_int "  max_output_tokens (positive integer, no default): ")" || return 1
    jq -n --arg url "$url" --arg weight "$weight" --arg model "$model" --arg token_env "$token_env" \
        --argjson n_ctx "$n_ctx" --argjson max_out "$max_out" '
        {url: $url, weight: ($weight | tonumber), n_ctx: $n_ctx, max_output_tokens: $max_out}
        + (if $model != "" then {model: $model} else {} end)
        + (if $token_env != "" then {token_env: $token_env} else {} end)
    '
}

# <<< BACKEND_ACCESS

echo "── Shared Memory — configure reasoning-LLM backends ──"
echo "Each backend is a URL the gateway load-balances across. Add as many as you like:"
echo "local hardware, a remote host you already run, and/or a paid cloud API."

entries=()
while true; do
    echo
    echo "── Backend $((${#entries[@]} + 1)) ──"
    url="$(ask_required "  URL (JSON field is url, not OpenAI SDK's base_url; e.g. http://localhost:5000 or https://api.deepseek.com/v1)")"
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

    # M6 (fix round): tracked so a LATER failure in build_backend_entry
    # (below) can tell the operator a systemd unit was already created and
    # started for this backend even though NO config line will be written
    # for it -- rather than silently exiting with a running, unconfigured
    # service and no explanation.
    systemd_unit_created=""
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
        systemd_unit_created="llm-backend-${label}.service"
        grn "  ✓ Installed + started llm-backend-${label}.service"
    fi

    if ! entry="$(build_backend_entry "$url" "$weight" "$model" "$token_env" "$ENV_FILE")"; then
        red "✗ No LLM_BACKENDS_JSON entry was written for this backend — the access"
        red "  question above was left unanswered."
        if [[ -n "$systemd_unit_created" ]]; then
            red "  NOTE: the systemd unit $systemd_unit_created WAS already created and"
            red "  started for this backend, with NO config line written for it. Disable"
            red "  it if you don't want that running unconfigured:"
            red "    systemctl --user disable --now $systemd_unit_created"
        fi
        exit 1
    fi
    entries+=("$entry")
    grn "  Added: $url"

    yesno "Add another backend?" || break
done

if [[ ${#entries[@]} -eq 0 ]]; then
    ylw "No backends entered — nothing written."
    exit 0
fi

json_array=$(printf '%s\n' "${entries[@]}" | jq -s -c '.')

# awk, not sed: the JSON contains slashes and quotes. umask 077 makes the temp file 600 from the first byte; chmod after the write used to leave every secret at 0644, and a failed chmod must abort before mv.
if grep -q '^LLM_BACKENDS_JSON=' "$ENV_FILE"; then
    (
      umask 077
      awk -v new="LLM_BACKENDS_JSON=$json_array" '
          /^LLM_BACKENDS_JSON=/ { print new; next }
          { print }
      ' "$ENV_FILE" > "$ENV_FILE.tmp"
    )
    if ! chmod --reference="$ENV_FILE" "$ENV_FILE.tmp"; then
        rm -f "$ENV_FILE.tmp"
        red "✗ chmod --reference failed — aborting before the file was replaced (nothing written)"
        exit 1
    fi
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

# The S-05 warning is reprinted last. The restart line just above will fail, and the earlier warning has scrolled away.
if echo "$json_array" | jq -e 'any(.[]; has("token_env"))' >/dev/null 2>&1; then
    if ! grep -qE '^[[:space:]]*AGENT_TOKENS=[^[:space:]]' "$ENV_FILE" 2>/dev/null; then
        echo
        red "⚠ AGENT_TOKENS is still not set — the 'systemctl --user restart' above will"
        red "  FAIL (S-05: the gateway refuses to start with a credentialed backend"
        red "  configured and no auth). Mint tokens first:"
        red "    bash shared-memory/scripts/bootstrap_tokens.sh"
        red "  — or set the documented override: ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1"
    fi
fi
