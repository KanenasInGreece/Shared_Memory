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
