# Operations artifacts (gateway host only)

These files run on the **single gateway host**, alongside the daemons in
`shared-memory/scripts/`. They are **not** part of the thin client and must never
ship with the skill (see ADR-014 / the client-vs-server split in the top-level
`CLAUDE.md`).

## `hive-mind-gateway.service`

A `systemd --user` unit that supervises the Hive-Mind gateway
(`hive_mind_proxy.py 8888`) and the coordinator + consolidation + REM daemons it
spawns.

**Why a service:** started by hand (e.g. a background `&` job in a terminal or an
agent session), the gateway receives `SIGTERM` and shuts down when that session
ends — `nohup` does not help, because it only blocks `SIGHUP`. A user service
decouples the gateway's lifetime from any login session.

### Install

```bash
bash shared-memory/ops/install_service.sh
```

Idempotent — substitutes the unit's `WorkingDirectory`/`Documentation` for this
checkout, `daemon-reload`s, `enable --now`s, and runs `loginctl enable-linger`,
then prints the verify/log commands. `install_framework.sh` also offers this as
a prompt at the end of first-time setup. Safe to re-run any time (e.g. after
moving the repo).

Equivalent by hand, if you'd rather see every step:

```bash
# 1. Edit the unit: set WorkingDirectory to your repo root and the Documentation URL.
cp shared-memory/ops/hive-mind-gateway.service ~/.config/systemd/user/

# 2. Let user services keep running with no active login session (survives logout/reboot).
loginctl enable-linger "$USER"

# 3. Enable + start.
systemctl --user daemon-reload
systemctl --user enable --now hive-mind-gateway.service

# 4. Verify.
systemctl --user status hive-mind-gateway.service
curl -s localhost:8888/health
```

### Operate

```bash
systemctl --user restart hive-mind-gateway.service
journalctl --user -u hive-mind-gateway.service -f
```

`Restart=on-failure` brings it back after a genuine crash; the `enable-linger`
step is what makes it survive logout and reboot.

### Audit-log rotation

`shared-memory-logrotate.{service,timer}` + `shared-memory.logrotate` (install steps
in that file's own header comment) rotate every `*-audit.jsonl` in the log directory —
the gateway audit log, the REM audit log, and the credential-events log
(`credential-audit.jsonl`, `CREDENTIAL_AUDIT_LOG_PATH`) are all covered by that one glob,
with nothing further to configure per file.

## Reasoning-LLM backends (`LLM_BACKENDS_JSON`) — credentials, never in a file

> **Local backends that need a key use the same mechanism.** A llama-server or vLLM on your LAN or tailnet that sits behind a token is configured exactly like a cloud API below — a JSON entry with `token_env`, the key delivered through `$CREDENTIALS_DIRECTORY` or a `<NAME>_FILE` pointer, default location **`~/.shared-memory/creds/<name>`** (mode 600). The only difference is transport: plaintext `http` is accepted to a private path, refused to a public one (see `plaintext_ok`).

```bash
bash shared-memory/ops/install_llm_backends.sh
```

Interactive, per backend: URL, whether this machine should supervise it as a
systemd service (takes *your* launch command — it won't construct one, GPUs
and models vary too much), and whether it needs a credential — if so, it takes
**only** the env-var name, with a shape check that rejects anything that looks
like a pasted literal key rather than a name. The script then asks the
mandatory routing choice too — `private_ok: true` or `roles: [...]` for a
credentialed backend, general-traffic vs. roles-only for one that isn't —
and warns loudly, without printing the credential, if `AGENT_TOKENS` isn't
set yet (the gateway refuses to start with a credentialed backend and no
auth, S-05, until you mint tokens or set the documented override). Writes
`LLM_BACKENDS_JSON` below. Safe to re-run (each run replaces the line
fresh). `install_framework.sh` also offers this as a prompt at the end of
first-time setup (default Yes, interactively).

By hand: `LLM_BACKENDS_JSON` (see `shared-memory/.env.example`) lets the
gateway route to more than one reasoning LLM, local or remote, including a
paid cloud API. Each entry is a URL plus an optional `token_env` — the
**name** of an env var, never a literal key:

**Per-entry parameters** (all optional except `url`):

| field | type | default | what it does |
|---|---|---|---|
| `url` | string | *(required)* | The backend's base URL. Trailing `/` is stripped; a base ending in `/v1` is probed without doubling it. |
| `token_env` | string | none | **Name** of the env var holding the API key — never the key itself. Resolved once at startup, sent as `Authorization` only to this backend. An unresolvable name excludes the backend (logged). |
| `model` | string | none | Model id rewritten into every request body routed here — a cloud endpoint needs its real id, not the `local-model` clients send. |
| `extra_body` | object | none | Merged into every chat payload routed here, overriding the caller's fields — provider-specific switches (e.g. disabling hybrid-model thinking). A non-object excludes the backend. |
| `weight` | float | `1.0` | ⚠ Currently affects **no** live routing decision — dispatch is cache-affinity then least-in-flight. Stored and displayed only. |
| `roles` | list | absent = serves all | Which dream functions this backend may serve: `extract` (REM's per-record summary call) or `judge` (NREM's insight fold) (`summarize` is reserved and refused). An explicit list is itself the per-function privacy opt-in; an **empty** list refuses startup. |
| `n_ctx` | int ≥ 1 | absent = always fits | The model's usable context. When set, a request whose estimated size cannot fit is excluded here rather than sent and truncated. |
| `plaintext_ok` | bool | `false` | **Transport assertion.** A credentialed entry is accepted over `https`, or over plaintext `http` to a private path (loopback, RFC1918, Tailscale CGNAT, link-local, ULA, an unqualified name, `.local`/`.lan`/`.internal`/`.home`/`.ts.net`). Over plaintext to a **public** address or FQDN it is **excluded at startup** unless you set this `true` — you asserting the path is private (VPN, reverse tunnel). The gateway never sends a provider key in the clear across the internet; the `/health` probe follows the same rule. |
| `private_ok` | bool | `true` without `token_env`, `false` with | May record content land here as unrestricted/role-less traffic? A **credentialed entry with neither `roles` nor an explicit `private_ok` refuses gateway startup** — the choice is yours to state, loudly, once. |
| `max_inflight` | int ≥ 1 | absent = unbounded | Per-backend concurrency ceiling. At cap the backend counts busy; a sole-eligible capped backend makes requests wait (bounded), never overrides the cap. |
| `price_per_mtok_in` / `price_per_mtok_out` | float | none | Operator-maintained prices surfaced on the authenticated `/health` for a dashboard to multiply against the per-backend token counters. Never read by routing. |

### Installing a provider API key, start to finish

The key itself belongs in an **encrypted store** — `pass`, a GPG-encrypted
file, or systemd's `systemd-creds encrypt`. A plaintext file on disk, even
mode 600, is the *minimum* the framework accepts, not the recommendation —
the framework's own files never hold the key either way.

1. **Store the key encrypted.** With systemd: `systemd-creds encrypt` and a
   `LoadCredential=`/`LoadCredentialEncrypted=` line on the gateway unit —
   the key then appears only in `$CREDENTIALS_DIRECTORY`, never at rest in
   plaintext. Without systemd credentials: keep it in `pass`/GPG and emit it
   at boot to a **runtime** file (e.g. under `/run/user/<uid>/`, tmpfs), or —
   the floor — `install -m 600 /dev/null <keyfile>` and paste the key in
   with an editor (never `echo`; shell history keeps it). The conventional
   location for that file is **`~/.shared-memory/creds/<name>`** (mode 600,
   in a 0700 directory), beside the framework's other per-host state.
   ⚠ **A runtime/tmpfs path is erased on reboot.** Unless something re-emits
   it at every boot, the gateway restarts with that backend excluded — and if
   it is the only backend, the dreaming passes stop with no error anywhere.
   Choose the persistent path or automate the re-emit; do not leave it to
   memory.
   ⚠ **Write the file with `printf '%s' '<key>' > <path>`** — no trailing
   newline needed. Trailing CR/LF are tolerated (stripped on read), but any
   *other* control character in the file — an embedded `\r`, a stray tab, a
   NUL — refuses the key at load with one journal line naming the file, and
   the backend is excluded from the pool rather than failing every request.
2. **Point the gateway at it** — one line in `shared-memory/.env`:
   `DEEPSEEK_API_KEY_FILE=/absolute/path/to/keyfile` — the pointer is used
   verbatim, so `~` is **not** expanded here even though other path settings
   in `.env` do expand it (skip this line entirely when
   using `LoadCredential=` — the credentials directory is checked first;
   resolution order is `$CREDENTIALS_DIRECTORY` > `<NAME>_FILE` > a plain
   env var, which is advisory-warned).
3. **Write the backend entry** in `LLM_BACKENDS_JSON` with
   `"token_env":"DEEPSEEK_API_KEY"` — the var **name** from step 2 — plus
   the mandatory routing choice: `"private_ok": true` (may serve everything)
   or `"roles": [...]` (per-function opt-in). A credentialed entry with
   neither refuses gateway startup, on purpose.
4. **Restart and verify:** `systemctl --user restart hive-mind-gateway.service`,
   then the authenticated `/health` — the backend reads `ok` in
   `llm_backends` (a bare-probe 401/403 from a credentialed backend counts
   as alive) and its roster entry shows `has_credential: true`.

### Routing and sizing knobs (the long version of `.env.example`'s one-liners)

**Dispatch** is cache-affinity first (a request whose large prompt prefix is
already warm on a backend's KV cache goes back there), then least-in-flight
among eligible backends, protecting warm cards from eviction. `weight` plays
no part (see the table).

**Fit check** (`LLM_CHARS_PER_TOKEN_RATIO`, `FIT_MARGIN`,
`FIT_DEFAULT_OUTPUT_TOKENS`) — only active for backends declaring `n_ctx`.
The estimate is `body_chars / RATIO`. The shipped `1.2` is the **most
conservative measured ratio** from 20 live `/tokenize` comparisons (v0.9.13
build, Qwen3 tokenizer, prose/JSON/code/SQL/Greek samples ranging
1.205–6.905 chars per token); a live chat-completion check showed this floor
already overestimates real chat-template prompt tokens by 83–525%, so the
ratio — not the margin — does the protective work. Re-measure for a
materially different tokenizer (denser tokenization needs a lower ratio).
`FIT_MARGIN` (0.10) and `FIT_DEFAULT_OUTPUT_TOKENS` (2048) are deliberately
flagged **unmeasured** conservative buffers. A fit rejection is a structured
`422` naming the estimate, and a gateway journal line says which knob to
retune.

**Capacity wait** (`LLM_MAX_INFLIGHT_WAIT_S`, `LLM_MAX_INFLIGHT_POLL_S`,
`LLM_MAX_CAPACITY_WAITERS`) — when every eligible backend sits at its
`max_inflight` cap, a request waits (bounded) rather than overriding the cap
or widening eligibility; waiting requests are themselves capped because each
one holds an admitted request slot for up to the full window — beyond the
waiter cap the gateway answers `503 backend_at_capacity` immediately. The
waiter bound is shape-chosen, not measured.

**Token accounting** (`LLM_USAGE_CAPTURE_CAP_BYTES`,
`TOKEN_LIFECYCLE_SUM_INTERVAL_S`) — always on: per-backend prompt/completion
counters with paired last-event timestamps on the authenticated `/health`.
**Counters reset on every gateway restart** — compute dashboard deltas
restart-aware via the timestamps. One summable lifecycle line per backend is
written to the journal (and the gateway audit JSONL, if configured) on
graceful shutdown; a nonzero interval adds periodic lines for bounded loss
on a hard kill. The byte cap bounds how much of a response body is buffered
for the `usage` parse; compressed and streaming responses skip capture.

```json
LLM_BACKENDS_JSON=[{"url":"http://localhost:5000"},
                    {"url":"https://api.deepseek.com/v1",
                     "token_env":"DEEPSEEK_API_KEY","model":"deepseek-chat",
                     "extra_body":{"thinking":{"type":"disabled"}}}]
```

An optional `extra_body` object is merged into every chat payload routed to
that backend, overriding the caller's fields — the place for provider-specific
request switches the daemons don't know to send, such as disabling a hybrid
reasoning model's thinking mode. A non-object `extra_body` excludes the
backend from the pool (logged): for a metered backend, being reached without
its configured overrides is exactly the misconfiguration to prevent.

**Model-attributes routing** (`roles`, `n_ctx`, `private_ok`, `max_inflight`,
`price_per_mtok_in`/`price_per_mtok_out`) — see the **per-entry parameter
table above** for the full reference, and `shared-memory/.env.example` for
the same fields documented inline where you edit them.

The gateway resolves `token_env` once at startup from its **own process
environment** and sends it as `Authorization` only to that backend — the
client's own gateway auth token is never forwarded anywhere past the gateway
(`hive_mind_proxy.py`, `_filter_headers`). If the named var isn't actually set,
that one backend is dropped from the pool with a loud startup warning, not
silently sent a doomed request.

**Never put the raw key in `.env`, in the unit file, or in any file this
framework writes for you** — that defeats the point of keeping it in an
encrypted store in the first place. `token_env` names an env var the gateway
process reads at startup; `secure_env.get_secret()` (SEC-06, PR A4) resolves
that var through three tiers, **RECOMMENDED for a systemd deployment, highest
precedence first:**

```bash
# 1. systemd LoadCredential= — never in argv/environ for any OTHER process,
#    never inherited by another user unit, and survives a headless boot.
#    ⚠ Corrected (fix round 1, R3): NOT root-mediated for a --user unit —
#    the per-user service manager reads $XDG_CONFIG_HOME/credstore/ etc.,
#    owned by YOUR OWN account (verified: `systemd-path
#    user-credential-store`). See server-setup.md, "Credential delivery",
#    for exactly what this tier does and does not isolate. Uncomment + adapt
#    the commented BARE-ID example already in hive-mind-gateway.service (no
#    ":PATH" — an absolute path into /etc/credstore is the SYSTEM store and
#    fails a --user unit's start outright). No <VAR_NAME>_FILE line needed
#    alongside it — secure_env reads $CREDENTIALS_DIRECTORY/<var_name,
#    lowercased> directly. Put the file at
#    ~/.config/credstore/deepseek_api_key, mode 600:
#    LoadCredential=deepseek_api_key

# 2. <VAR_NAME>_FILE (Docker official-images convention) — point the var at
#    a file instead of a literal value. Works with any secret store that can
#    write a file (pass, a mounted Docker/K8s secret, vault agent template).
echo "DEEPSEEK_API_KEY_FILE=/run/secrets/deepseek_api_key" >> shared-memory/.env
systemctl --user restart hive-mind-gateway.service
```

**Deprecated: `systemctl --user import-environment`.** It still works — a
value it lands in the gateway's own exec environment is honoured (an
operator-exported value always wins, ahead of `LoadCredential=`/`_FILE`; see
`secure_env.py`'s precedence statement) and SEC-06 (ii) logs one advisory
line naming the key, never the value, when it detects this — but it is no
longer the recommended path, precisely BECAUSE it wins unconditionally and is
the most exposed of the three (`/proc/<pid>/environ`, `show-environment`,
every later user unit):

```bash
# ~/.bashrc — decrypt once per login, never touches disk in plaintext
export DEEPSEEK_API_KEY=$(pass show api/deepseek)

# DEPRECATED — readable by ANY same-uid process via `systemctl --user
# show-environment` (not just the gateway) and inherited by EVERY user unit
# started afterward, not only this one. Prefer the two tiers above.
systemctl --user import-environment DEEPSEEK_API_KEY
systemctl --user try-restart hive-mind-gateway.service
```

**`EnvironmentFile=` in the unit is likewise an anti-pattern for a secret
key** — the same class of exposure as `import-environment` (the whole file's
contents land in the unit's exec environment, visible to
`/proc/<pid>/environ` and to a child that inherits the environment
wholesale), for a mechanism designed for cleartext config, not credentials.
Use `LoadCredential=` for a secret; `EnvironmentFile=` is fine for a
`shared-memory/.env` containing ONLY non-secret keys, but this framework's
`.env` mixes both, so in practice `EnvironmentFile=` should not point at it
at all — the gateway reads `shared-memory/.env` itself via `secure_env`,
which is the whole reason `ExecStart=` needs no `EnvironmentFile=` line
today.

**Known tradeoff of the `import-environment` path, by design (unchanged
since it is still a supported fallback):** `hive-mind-gateway.service`
survives a headless reboot via `loginctl enable-linger` (see above) with **no
login required** — but `import-environment` only ever runs from an
interactive shell. So on a headless boot, a backend whose `token_env` is
ONLY ever delivered that way is dropped (logged, not fatal) until you next
log in and re-export/re-import. **A `token_env` delivered via
`LoadCredential=` or `_FILE` has no such gap — it survives a headless boot
like any other systemd-managed secret.** Backends with no `token_env` (local
hardware) are unaffected either way.

## `check_config.py` — audit the effective config without starting the gateway

Standalone; renders the framework's effective configuration and what the gateway will DO with it,
without ever booting it. Two phases:

- **Phase A (environment half)** — STDLIB ONLY, never imports a daemon module. Loads
  `shared-memory/.env` (or reports "environment-only" honestly when there is none — a legitimate
  headless state, not an error) and renders a three-valued state per env-overridable setting:
  `declared` (present, non-empty) · `present-but-empty` (present but empty — whether that falls
  back to the default depends on the SITE'S OWN idiom; both the state and the true effective value
  are always shown) · `inherited default` (absent entirely). Every secret-classified key
  (`PG_PASSWORD`, `NEO4J_PASSWORD`, `AGENT_TOKENS`, `PG_CONN`, ...) is answered ONLY as a boolean
  `has_credential` — the value itself is never rendered, matching the same discipline the
  authenticated `/health` payload already applies.
- **Phase B (backend half)** — `import hive_mind_proxy` inside `except Exception`, so a
  misconfiguration (a bad encoder URL, a malformed `LLM_BACKENDS_JSON` entry, or the daemon
  dependencies simply not being installed under whatever python ran this) degrades to "Phase A
  printed, Phase B unavailable, exit 2" rather than a raw traceback. On success, renders one line
  per configured LLM backend (url — credential-scrubbed, weight, model, roles, `n_ctx`,
  `has_credential`, `private_ok` effective + explicit) and then calls the gateway's OWN startup
  guard functions (never re-implementing their predicates) to say whether it would actually boot.

```bash
# Phase A only — plain python3, no third-party packages needed at all:
python3 shared-memory/scripts/check_config.py --phase-a-only

# Both phases — needs the daemon dependencies:
uv run --with aiohttp --with asyncpg --with httpx --with neo4j \
    python3 shared-memory/scripts/check_config.py
```

**Exit codes** (a renderer, never an enforcer):

| Code | Meaning |
|---|---|
| `0` | Config readable AND neither of the gateway's own startup guards would refuse to boot. |
| `1` | Readable, but the gateway WILL refuse to start (reachable only once Phase B's import succeeds). |
| `2` | Could not read/render at all — an unreadable-but-PRESENT `.env`, or a Phase-B import crash. An ABSENT `.env` is NOT exit 2. |

⛔ **Not wired into `preflight.sh`.** preflight's exit contract is 0/1 (hard requirements only) and
it deliberately runs before `shared-memory/.env` is expected to exist — this script's 0/1/2
contract is different on purpose, and it has nothing useful to say that early. Run it any time after
Phase 1 has written the `.env`, especially before a restart when you have just hand-edited it.

## `backup.sh` / `restore.sh` (+ `shared-memory-backup.{service,timer}`)

Consistent backup of **both** stores (Postgres + Neo4j — the latter holds the
non-derivable `HAD_OUTCOME` retrospective edges) and a ground-up restore. The
scripts ship the mechanism; **policy (schedule/retention/destination/encryption)
is yours**, set in the private `.env`. Full reference: [README §20 — Backups &
Disaster Recovery](../../README.md#20-backups--disaster-recovery).

Before dumping, `backup.sh` quiesces the gateway over `POST /admin/backup`
(needs a `backup:admin` token — see `.env.example`): client writes shed
(`503 + Retry-After`), the REM/NREM daemons are fenced by a Postgres advisory
lock, and the outbox drains. A `trap` resumes on any exit; the gateway's TTL
auto-resumes if the script dies.

```bash
bash shared-memory/ops/backup.sh             # full quiesced backup
bash shared-memory/ops/backup.sh --dry-run   # sizes / space / retention, no writes
bash shared-memory/ops/backup.sh --verify    # integrity-check the latest set
bash shared-memory/ops/restore.sh --force    # ground-up restore (see README §20)
```

### Schedule — cron or the timer (pick one)

```bash
# Option A — systemd --user timer (mirrors the gateway/logrotate units):
cp shared-memory/ops/shared-memory-backup.{service,timer} ~/.config/systemd/user/
# edit WorkingDirectory in the .service to your repo root, then:
systemctl --user daemon-reload
systemctl --user enable --now shared-memory-backup.timer
systemctl --user list-timers shared-memory-backup.timer

# Option B — cron:
#   30 3 * * *  cd /path/to/shared-memory-GitHub && bash shared-memory/ops/backup.sh >> ~/.shared-memory/logs/backup.log 2>&1
```
