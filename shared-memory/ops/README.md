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

## Reasoning-LLM backends (`LLM_BACKENDS_JSON`) — credentials, never in a file

`LLM_BACKENDS_JSON` (see `shared-memory/.env.example`) lets the gateway route to
more than one reasoning LLM, local or remote, including a paid cloud API. Each
entry is a URL plus an optional `token_env` — the **name** of an env var, never
a literal key:

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
encrypted store in the first place. The recommended pattern mirrors keeping it
in your shell already (e.g. `pass`, GPG-backed):

```bash
# ~/.bashrc — decrypt once per login, never touches disk in plaintext
export DEEPSEEK_API_KEY=$(pass show api/deepseek)
export XAI_API_KEY=$(pass show api/xai)

# Bridge into the systemd --user MANAGER's environment — a service does NOT
# inherit an interactive shell's exports on its own. No file involved: the
# value only ever lives in process memory (yours, then systemd's).
systemctl --user import-environment DEEPSEEK_API_KEY XAI_API_KEY
systemctl --user try-restart hive-mind-gateway.service   # picks up the newly-imported tokens
```

**Known tradeoff, by design:** `hive-mind-gateway.service` survives a headless
reboot via `loginctl enable-linger` (see above) with **no login required** — but
`import-environment` only ever runs from an interactive shell. So on a headless
boot, any backend with a `token_env` is dropped (logged, not fatal) until you
next log in and re-export/re-import. **Backends with no `token_env` (local
hardware) are unaffected and come up fully unattended, same as always** — this
tradeoff only applies to paid/cloud backends, and requiring a live unlock of
the secret store before a cost-bearing API key becomes reachable is the
intended behaviour, not a bug to route around.

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
