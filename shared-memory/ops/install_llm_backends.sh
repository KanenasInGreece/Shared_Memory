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
#
# NON-GOAL (W0, recorded deliberately — not an oversight): a credentialed
# entry over plaintext http to a PUBLIC host is silently excluded by the
# gateway's transport rule (hive_mind_proxy.py _bearer_transport_ok,
# `plaintext_ok`) — this script does not ask about or write `plaintext_ok`.
# See shared-memory/ops/README.md, "Reasoning-LLM backends", TRANSPORT RULE.

set -euo pipefail

# ⛔ RULING 4: every operator-facing script accepts -h/--help (prints its own
# header, exits 0, does nothing else) and refuses any argument it does not
# recognise — this script previously had no argument parsing at all, so any
# flag (including --help) was silently ignored and the interactive prompts
# ran anyway.
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

# W0 item ① (SEC M-2, fix round — this header itself was stale after W4):
# of the gateway's three startup guards (S-05, M-5, P-5 in
# shared-memory/scripts/hive_mind_proxy.py), only S-05 still refuses to
# start — any credentialed backend at all while AGENT_TOKENS is unset,
# unless the operator has already set the documented override. M-5 and P-5
# are loud, non-fatal startup WARNINGS since W4/decision:1824: a
# credentialed backend with neither `private_ok` nor `roles` (M-5) is safe
# by construction (simply never selected); auth-off plus an EXPLICIT
# private_ok=false (P-5) is safe by construction only for a backend that
# also carries no `roles` — one still worth asking about up front rather
# than leaving the operator to discover it from a startup log line. This
# block makes the script itself ask the M-5 question and warn about S-05.
#
# ROLE_VOCABULARY: extract judge
# (source of truth: hive_mind_proxy.py's ROUTING_ROLE_NAMES; "summarize" is
# RESERVED_ROLE_NAMES there and is never offered here — pinned by
# tests/test_install_llm_backends_role_vocabulary.py)
#
# >>> BACKEND_ACCESS
# _ROLE_VOCABULARY / yesno_y() / _role_vocabulary_has() /
# _roles_cover_full_vocabulary() live inside this marker (rather than
# beside yesno() above) so the block stays SELF-CONTAINED for
# tests/test_install_llm_backends.py's standalone extraction —
# build_backend_entry() depends on all four.
_ROLE_VOCABULARY="extract judge"

# SEC fix round (H-BLOCKING): yesno_y() now guards its own read the same way
# ask_backend_roles() and the M-5 mode prompt do. Before, an exhausted pipe
# at THIS prompt left `v` empty, the regex `[[ ! "$v" =~ ^[Nn]$ ]]` matched
# (empty does not match ^[Nn]$) and the function returned TRUE -- silently
# writing private_ok:true with no operator answer at all. Now EOF is a
# THIRD state (return 2), distinct from 0=yes/1=no, so a caller can refuse
# to guess rather than defaulting to either branch: an unanswered access
# question must never widen access.
yesno_y() {
    local v
    if ! read -r -p "$1 [Y/n]: " v; then
        return 2
    fi
    [[ ! "$v" =~ ^[Nn]$ ]]
}

_role_vocabulary_has() {
    local candidate="$1" r
    for r in $_ROLE_VOCABULARY; do
        [[ "$r" == "$candidate" ]] && return 0
    done
    return 1
}

# M5 (fix round): a REAL set comparison -- sorts both sides -- rather than
# the earlier "does the joined string contain a space" heuristic, which
# happened to work only because _ROLE_VOCABULARY has exactly two members.
# A future third role added to _ROLE_VOCABULARY (kept in lockstep with the
# gateway's ROUTING_ROLE_NAMES) cannot silently break "did the operator
# choose the full set" detection, because both sides read from the same
# variable.
_roles_cover_full_vocabulary() {
    local chosen_sorted full_sorted
    chosen_sorted="$(printf '%s\n' $1 | sort | tr '\n' ' ')"
    full_sorted="$(printf '%s\n' $_ROLE_VOCABULARY | sort | tr '\n' ' ')"
    [[ "$chosen_sorted" == "$full_sorted" ]]
}

# ask_backend_roles() — loops until it has >=1 role from _ROLE_VOCABULARY
# (the gateway's ROUTING_ROLE_NAMES; "summarize" is reserved and always
# invalid here, same as at the gateway). Blank input (Enter) means "both" —
# that is the documented default, not a re-ask condition. Anything else
# that yields zero valid roles (an unknown name, "summarize", or a garbage
# token) DOES re-ask — it must never fall through to an empty roles list,
# which is itself a separate fatal shape at the gateway. Exhausted stdin
# fails loudly (same convention as install_framework.sh's ask_secret)
# rather than spinning. stdout carries ONLY the final space-separated role
# list; everything else is stderr.
ask_backend_roles() {
    local raw role lc valid=() bad
    while true; do
        if ! read -r -p "  Which roles — extract, judge, or both (Enter = both)? " raw; then
            echo "  No more input on stdin — refusing to guess which roles this backend serves." >&2
            return 1
        fi
        if [[ -z "$raw" ]]; then
            printf '%s' "$_ROLE_VOCABULARY"
            return 0
        fi
        raw="${raw//,/ }"
        valid=()
        bad=0
        for role in $raw; do
            lc="$(printf '%s' "$role" | tr '[:upper:]' '[:lower:]')"
            if _role_vocabulary_has "$lc"; then
                case " ${valid[*]:-} " in
                    *" $lc "*) ;;
                    *) valid+=("$lc") ;;
                esac
            else
                bad=1
            fi
        done
        if [[ "$bad" -eq 0 && ${#valid[@]} -gt 0 ]]; then
            printf '%s' "${valid[*]}"
            return 0
        fi
        echo "  Enter one or more of: $_ROLE_VOCABULARY (never summarize — reserved)." >&2
    done
}

# build_backend_entry(url, weight, model, token_env, env_file) — elicits the
# M-5 access choice (credentialed) or the general-traffic choice
# (uncredentialed), prints every S-05/P-5/dream-slot caveat that applies,
# and echoes the COMPLETE jq backend entry (url/weight/model/token_env plus
# exactly one of private_ok/roles) on stdout. It ALWAYS writes an EXPLICIT
# choice — "private_ok": true (general-traffic) or "roles": [...]
# (role-scoped) — on every path, credentialed or not: under W4 default-deny
# (decision:1824) an entry with neither key defaults to private_ok=false and
# serves no role-less traffic (M-5 is a startup WARNING now, not a refusal,
# but this script's own output never triggers it either way, since it always
# writes one of the two). It never writes an explicit "private_ok": false
# (that is a real, opt-in scoping decision an operator states by hand, not
# one this script guesses on their behalf) and it never writes "roles": []
# (a separate fatal shape at the gateway). Every prompt/warning/caveat goes
# to stderr; stdout carries only the finished JSON entry — the caller's
# `entry="$(build_backend_entry ...)"` capture depends on that separation.
# An unanswered access question (exhausted stdin at ANY point in here)
# always returns 1 and writes NOTHING to stdout — never a default guess.
build_backend_entry() {
    local url="$1" weight="$2" model="$3" token_env="$4" env_file="$5"
    local auth_off=0 priv="" roles_str="" mode="" general_rc

    if [[ -n "$token_env" ]]; then
        # S-05: the gateway refuses to start with ANY credentialed backend
        # while AGENT_TOKENS is unset/empty, unless the operator has already
        # set ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1. Value-sensitive check —
        # matches only a NON-EMPTY AGENT_TOKENS= line; a cleared
        # `AGENT_TOKENS=` is auth-OFF too (coordinator.py keys on
        # bool(_AGENT_TOKENS)). Prints nothing itself either way.
        if ! grep -qE '^[[:space:]]*AGENT_TOKENS=[^[:space:]]' "$env_file" 2>/dev/null; then
            auth_off=1
            echo "  ⚠ AGENT_TOKENS is not set in $env_file — the gateway will REFUSE TO" >&2
            echo "    START (S-05) with this credentialed backend configured, until you" >&2
            echo "    mint tokens:" >&2
            echo "      bash shared-memory/scripts/bootstrap_tokens.sh" >&2
            echo "    or set the documented override: ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1" >&2
        fi

        echo "  This backend is credentialed — the gateway needs to know how it may" >&2
        echo "  serve traffic (M-5: neither choice below is optional for a credentialed" >&2
        echo "  backend)." >&2
        while true; do
            if ! read -r -p "  Serve any eligible request (private_ok), or only specific roles (roles)? [private_ok/roles]: " mode; then
                echo "  No more input on stdin — refusing to write a credentialed backend with" >&2
                echo "  neither private_ok nor roles chosen (M-5: the gateway would boot but" >&2
                echo "  never select it — declare one explicitly)." >&2
                return 1
            fi
            mode="$(printf '%s' "$mode" | tr '[:upper:]' '[:lower:]')"
            case "$mode" in
                private_ok|roles) break ;;
                *) echo "  Enter exactly 'private_ok' or 'roles'." >&2 ;;
            esac
        done

        if [[ "$mode" == "private_ok" ]]; then
            priv="true"
        else
            roles_str="$(ask_backend_roles)" || return 1
            # W4 default-deny (decision:1824): this "never serves role-less
            # traffic" claim is now TRUE on BOTH paths — credentialed and
            # uncredentialed alike. An explicit `roles` list with no
            # `private_ok` key leaves the EFFECTIVE private_ok at its
            # default, FALSE, for every backend regardless of credential —
            # `_role_eligible`'s role-less branch (which falls back to
            # effective private_ok, ignoring `roles`) excludes it either
            # way. The uncredentialed branch below prints the identical
            # note now, rather than the inverted one it used to need.
            echo "  Note: a roles-only backend never serves role-less (ad-hoc) traffic." >&2
            if ! _roles_cover_full_vocabulary "$roles_str"; then
                echo "  Note: with only these roles, this backend does not count toward dream" >&2
                echo "  slots — if no other backend qualifies, REM and NREM will never run" >&2
                echo "  against this fleet." >&2
            fi
            if [[ "$auth_off" -eq 1 ]]; then
                echo "  Note: with auth off you will hit the S-05 refusal first (see above) —" >&2
                echo "  P-5′ (the auth-off degraded warning) does NOT apply to this entry: it" >&2
                echo "  only fires on an EXPLICIT private_ok:false, and this script never" >&2
                echo "  writes one." >&2
            fi
        fi
    else
        if yesno_y "  Does this backend serve general (role-less) traffic?"; then
            priv="true"
        else
            general_rc=$?
            # H2 fix round: general_rc is 1 here ("no" answered) UNLESS
            # yesno_y hit exhausted stdin (2) -- distinguish them rather
            # than treating any non-zero as "no, go elicit roles".
            if [[ "$general_rc" -eq 2 ]]; then
                echo "  No more input on stdin — refusing to guess whether this backend serves" >&2
                echo "  general traffic (an unanswered access question must never widen access)." >&2
                return 1
            fi
            roles_str="$(ask_backend_roles)" || return 1
            # W4 default-deny (decision:1824): the "never serves role-less
            # traffic" claim is now TRUE on THIS (uncredentialed) path too —
            # _role_eligible's role-less branch falls back to the EFFECTIVE
            # private_ok, which now defaults to FALSE regardless of
            # credential. A roles-only entry written here correctly serves
            # no role-less traffic; this is the identical note the
            # credentialed path above prints.
            echo "  Note: a roles-only backend never serves role-less (ad-hoc) traffic." >&2
            if ! _roles_cover_full_vocabulary "$roles_str"; then
                echo "  Note: with only these roles, this backend does not count toward dream" >&2
                echo "  slots — if no other backend qualifies, REM and NREM will never run" >&2
                echo "  against this fleet." >&2
            fi
        fi
    fi

    if [[ -n "$priv" ]]; then
        jq -n --arg url "$url" --arg weight "$weight" --arg model "$model" --arg token_env "$token_env" '
            {url: $url, weight: ($weight | tonumber)}
            + (if $model != "" then {model: $model} else {} end)
            + (if $token_env != "" then {token_env: $token_env} else {} end)
            + {private_ok: true}
        '
    else
        jq -n --arg url "$url" --arg weight "$weight" --arg model "$model" --arg token_env "$token_env" --arg roles "$roles_str" '
            {url: $url, weight: ($weight | tonumber)}
            + (if $model != "" then {model: $model} else {} end)
            + (if $token_env != "" then {token_env: $token_env} else {} end)
            + {roles: ($roles | split(" "))}
        '
    fi
}
# <<< BACKEND_ACCESS

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

# awk (not sed) for the same reason install_framework.sh uses it: the JSON value
# contains slashes and quotes that would need fragile escaping as a sed replacement.
#
# R5 (fix round 1, Opus review, probe-confirmed): the PREVIOUS comment here
# claimed "no window where the secrets-bearing file sits at default
# permissions" — false. `chmod --reference` runs only AFTER the awk write
# completes, so $ENV_FILE.tmp held every secret in shared-memory/.env
# (PG_PASSWORD, NEO4J_PASSWORD, AGENT_TOKENS, BACKUP_ADMIN_TOKEN, …) at the
# process umask (0644 under a common 022 umask) for the ENTIRE write — probe
# reproduced this live: "MODE OF TMP RIGHT AFTER awk: 644". `chmod
# --reference ... || true` also FAILED OPEN: a non-GNU chmod (busybox,
# non-coreutils) errors silently and the 0644 file gets `mv`'d into place
# with the script still printing its success banner.
#
# Fixed the same way S-07 fixed install_framework.sh one file over: `umask
# 077` wraps the write in a subshell so $ENV_FILE.tmp is 600 from the byte it
# is created, never 644 even for an instant. `chmod --reference` still runs
# afterward for MODE FIDELITY (matching whatever $ENV_FILE's own mode
# actually is, in case an operator widened it deliberately) — but a failed
# chmod is now FATAL, aborting before the mv, rather than silently shipping
# a wrongly-permissioned file.
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

# M1 (fix round, QA review): a per-backend S-05 warning printed during
# elicitation can scroll off-screen by the time the operator reaches
# "Restart the gateway to pick this up" -- exactly the one state where that
# restart command will NOT work. Re-check (value-sensitive, same as
# build_backend_entry's own check; prints nothing itself either way) and, if
# it still applies, re-print the warning HERE -- the true LAST thing this
# script prints -- rather than trusting the operator to have scrolled back
# up to see it.
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
