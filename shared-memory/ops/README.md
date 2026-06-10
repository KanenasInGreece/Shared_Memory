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
