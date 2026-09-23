#!/usr/bin/env bash
#
# bootstrap_tokens.sh — mint agent tokens and write AGENT_TOKENS, AGENT_ROLES, and AGENT_INSTALLS into the gateway .env in place.
#
# generate_tokens.py writes each local agent's token into that agent's skill .env (mode 600) and prints only names, digests, and paths. Pass --reveal <name>, repeatable, to show a remote agent's token from this same mint.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh
#   bash shared-memory/scripts/bootstrap_tokens.sh --reveal codex --reveal grok
#
# A registered install path whose directory does not exist yet is refused for that agent, so a token is not minted for nobody to receive. Install the skill package, then re-run.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh --add codex \
#       --install-path ~/.codex/skills/shared-memory/.env
#
# --add registers one new agent and leaves every other digest unchanged. It refuses a name already registered. --remint re-issues one existing token; --force rotates everyone. Omit --install-path for a remote agent and pass --reveal instead.
#
#   bash shared-memory/scripts/bootstrap_tokens.sh --add opencode --mcp \
#       --install-path ~/.config/opencode/shared-memory-mcp/.env
#
# --mcp records an MCP connector install (`name:mcp:path`) so sync_skills.sh delivers the connector package, not the CLI skill. It requires --install-path and only combines with --add / --remint. An entry with no kind stays a CLI skill install.
#
# --reveal shows a token only from this invocation. A later separate reveal is a bulk mint and rotates every agent. --add never rotates anyone, even with --reveal.
#
# If AGENT_TOKENS is already set, a bulk mint refuses unless you pass --force, which rotates every token. --force accepts --reveal on the same invocation. --add is exempt and never changes an existing digest.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# Tokens must land in the file the gateway reads: shared-memory/.env, then the pre-0.6 repo-root fallback. Otherwise auth stays silently off.
ENV_FILE="$REPO_ROOT/shared-memory/.env"
[[ -f "$ENV_FILE" ]] || ENV_FILE="$REPO_ROOT/.env"

red() { printf '\033[31m%s\033[0m\n' "$*"; }
grn() { printf '\033[32m%s\033[0m\n' "$*"; }
ylw() { printf '\033[33m%s\033[0m\n' "$*"; }

# Track every mktemp path here. A trap inside the function would expand a local that is already gone, so an interrupt would leak the temp file.
_CLEANUP_PATHS=()
_cleanup_temp_files() {
    local p
    for p in "${_CLEANUP_PATHS[@]:-}"; do
        [[ -n "$p" ]] && rm -f -- "$p"
    done
    # Under set -e, this trap's last status becomes the script's exit status. return 0 keeps a clean exit from looking like a failure when there is nothing to remove.
    return 0
}
# INT and TERM clean up and exit 130 or 143. Naming them on the EXIT trap without an exit would resume after Ctrl+C.
trap _cleanup_temp_files EXIT
trap '_cleanup_temp_files; exit 130' INT
trap '_cleanup_temp_files; exit 143' TERM

# Replace the first live or commented key= line, or append it. Appending only used to leave two AGENT_TOKENS lines when .env.example shipped a placeholder.
# Write every registry line in one temp file and one rename. A crash between token and role would leave a credential that AGENT_ROLES does not confine, and two runs can each keep the same baseline.
replace_registry_lines() {
    # A key whose line is empty is skipped, so the caller need not know which registries were produced.
    local tmp; tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
    _CLEANUP_PATHS+=("$tmp")
    cp "$ENV_FILE" "$tmp"
    while [[ $# -gt 0 ]]; do
        local key="$1" value_line="$2"; shift 2
        [[ -z "$value_line" ]] && continue
        local inner; inner="$(mktemp "${ENV_FILE}.XXXXXX")"
        _CLEANUP_PATHS+=("$inner")
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
    # --mcp names one registration. A bulk mint already carries each entry's kind, so --mcp there would read as converting all of them.
    red "✗ --mcp only makes sense together with --add or --remint"
    exit 1
fi
if [[ "${#install_kind_flag[@]}" -gt 0 && -z "$install_path" ]]; then
    # Refuse a missing path here, before any mint. This is the documented front door.
    red "✗ --mcp needs --install-path <walled-dir>/.env — an install kind says"
    red "  what to deliver WHERE, and without a registered path there is nowhere."
    exit 1
fi
if [[ -n "${add_role:-}" && -z "$add_name" && -z "${remint_name:-}" ]]; then
    # A bulk mint takes roles from READ_ONLY_AGENTS. --role there would look like it applied to every agent.
    red "✗ --role only makes sense together with --add or --remint"
    exit 1
fi
if [[ ( -n "$add_name" || -n "${remint_name:-}" ) && "$force" -eq 1 ]]; then
    red "✗ --add and --force are mutually exclusive — --add never rotates anyone,"
    red "  --force always rotates everyone. Run them separately."
    exit 1
fi

[[ -f "$ENV_FILE" ]] || { red "✗ .env not found at $ENV_FILE — run: bash shared-memory/scripts/install_framework.sh"; exit 1; }

# One minter at a time. Two runs can read the same AGENT_TOKENS baseline, and the later write drops the earlier agent whose token is already on disk.
# The lock covers the read and the write, and it fails immediately so a second operator is told to wait.
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

# Name a missing uv before the mint starts, instead of a bare "command not found" halfway through.
command -v uv >/dev/null 2>&1 || { red "✗ uv not found on PATH — install uv first (preflight.sh checks this)."; exit 1; }

# --add grows the roster. --remint re-issues one existing agent. Neither path is the bulk mint below.
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
    # Write AGENT_ROLES when the mint emits it. Absence means full read/write, and this path used to drop that line.
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

# Bulk mint: the whole roster, for a first bootstrap or a deliberate rotation.
# Never silently overwrite a live token registry.
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

# generate_tokens.py exits 0 even when some agents failed, because the merged registry keeps a failed agent's old entry. A nonzero exit there would abort under set -e before this script applied that merge.
# After the write, a PARTIAL FAILURE marker still exits 2 so automation can tell the mint was incomplete.
if grep -q "PARTIAL FAILURE" <<<"$out"; then
    echo
    red "⚠ PARTIAL FAILURE during this mint — see the report above for which"
    red "  agent(s) are affected and how to recover. The registry written above"
    red "  IS safe as applied (no working credential was revoked) — but go fix"
    red "  the underlying issue for the affected agent(s) and re-run (bulk, or"
    red "  --add for just that one) once ready."
    exit 2
fi
