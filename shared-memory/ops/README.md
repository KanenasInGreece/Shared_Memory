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

```bash
bash shared-memory/ops/install_llm_backends.sh
```

Interactive, per backend: URL, whether this machine should supervise it as a
systemd service (takes *your* launch command — it won't construct one, GPUs
and models vary too much), and whether it needs a credential — if so, it takes
**only** the env-var name, with a shape check that rejects anything that looks
like a pasted literal key rather than a name. Writes `LLM_BACKENDS_JSON` below.
Safe to re-run (each run replaces the line fresh). `install_framework.sh` also
offers this as a prompt at the end of first-time setup.

By hand: `LLM_BACKENDS_JSON` (see `shared-memory/.env.example`) lets the
gateway route to more than one reasoning LLM, local or remote, including a
paid cloud API. Each entry is a URL plus an optional `token_env` — the **name**
of an env var, never a literal key:

```json
LLM_BACKENDS_JSON=[{"url":"http://localhost:5000"},
                    {"url":"https://api.deepseek.com/v1",
                     "token_env":"DEEPSEEK_API_KEY","model":"deepseek-chat"}]
```

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
# 1. systemd LoadCredential= (PREFERRED) — root-mediated delivery, file
#    typically 0400 and owned by the service, never in argv/environ for any
#    OTHER process. Uncomment + adapt the commented example already in
#    hive-mind-gateway.service. No <VAR_NAME>_FILE line needed alongside it —
#    secure_env reads $CREDENTIALS_DIRECTORY/<var_name, lowercased> directly.
#    LoadCredential=deepseek_api_key:/etc/credstore/deepseek_api_key

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
