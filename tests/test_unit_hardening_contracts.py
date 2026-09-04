"""HYG round, lane eps — source-text contracts for the three gateway-host
systemd `--user` units under `shared-memory/ops/`.

These are TEXT assertions, not behaviour ones: a unit file reading
`LimitCORE=0` proves the directive is present in the source that
`install_service.sh` / `cp` ships to `~/.config/systemd/user/` — it says
nothing about whether the running system actually enforces the limit, which
depends on the systemd version, the cgroup hierarchy, and any drop-in
override layered on top at the deploy host. Behaviour is proven separately,
by the merger's restart-and-verify on the test host (`systemctl --user show
<unit> --property=LimitCORE`, `--property=ProtectSystem`), not by this file.

Every assertion here is against a literal VALUE (the exact directive line),
never a regex shape that would also match a differently-configured line
(fact:1309 — an equality between two derived expressions is only half a
guard; a shape match that also matches the wrong value proves nothing).

Scope, per the ε-lane brief:
  - LimitCORE=0 in all three units (unconditional — S16 ruling).
  - Documentation=<the real URL> in the gateway unit, matching what the
    other two units already carry.
  - ProtectHome ABSENT from all three units (deliberately, per each unit's
    own comment — ruled out, never revisit via an env override).
  - ProtectSystem=full: present in ALL THREE units (operator ruling, HYG
    round — see the ε-lane handoff §9). `full` only makes /usr, /boot, /efi
    and /etc read-only; it never touches /tmp, /run or $XDG_RUNTIME_DIR, so
    it neither protects nor is put at risk by the two write-paths-outside-%h
    the ε-lane build found (the gateway's default AF_UNIX socket under
    $XDG_RUNTIME_DIR/tmp; the backup unit's mktemp -d secrets directory under
    /tmp) — those remain recorded findings in the handoff, not code changes.
"""
import os
import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_OPS = os.path.join(_ROOT, "shared-memory", "ops")

_GATEWAY_UNIT = os.path.join(_OPS, "hive-mind-gateway.service")
_BACKUP_UNIT = os.path.join(_OPS, "shared-memory-backup.service")
_LOGROTATE_UNIT = os.path.join(_OPS, "shared-memory-logrotate.service")

_REAL_DOCUMENTATION_URL = "https://github.com/KanenasInGreece/Shared_Memory"


def _lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


# ── LimitCORE=0 — all three units, unconditionally ──────────────────────────

def test_gateway_unit_pins_limitcore_zero():
    """S16: a crash in the gateway process must never write a core dump — the
    core would hold every secret loaded from shared-memory/.env in plaintext.
    Asserts the literal directive line is present, not merely a `LimitCORE=`
    key with some value."""
    assert "LimitCORE=0" in _lines(_GATEWAY_UNIT)


def test_backup_unit_pins_limitcore_zero():
    """S16, more pointed for this unit: backup.sh's init_secrets_dir() holds
    the auth bearer token and both DB passwords in the process's own
    environment/heap for the run's duration."""
    assert "LimitCORE=0" in _lines(_BACKUP_UNIT)


def test_logrotate_unit_pins_limitcore_zero():
    """S16 applied as defense in depth even though logrotate itself loads no
    framework secret — unconditional across all three units per the ruling."""
    assert "LimitCORE=0" in _lines(_LOGROTATE_UNIT)


# ── Documentation= in the gateway unit ───────────────────────────────────────

def test_gateway_unit_documentation_matches_the_other_two():
    """The gateway unit had shipped a YOUR_GITHUB_USER placeholder while the
    backup and logrotate units already carried the real URL. Asserts all
    three now agree on the literal value, not just that each has SOME
    Documentation= line."""
    gateway_doc = [l for l in _lines(_GATEWAY_UNIT) if l.startswith("Documentation=")]
    backup_doc = [l for l in _lines(_BACKUP_UNIT) if l.startswith("Documentation=")]
    logrotate_doc = [l for l in _lines(_LOGROTATE_UNIT) if l.startswith("Documentation=")]
    assert gateway_doc == [f"Documentation={_REAL_DOCUMENTATION_URL}"]
    assert backup_doc == [f"Documentation={_REAL_DOCUMENTATION_URL}"]
    assert logrotate_doc == [f"Documentation={_REAL_DOCUMENTATION_URL}"]
    assert gateway_doc == backup_doc == logrotate_doc


# ── ProtectHome — absent from all three, deliberately ────────────────────────

def _no_protecthome_directive(lines: list[str]) -> bool:
    """True iff no non-comment line sets ProtectHome= to any value. A
    commented-out `#ProtectHome=...` example line would not trip this (there
    is none in any of these three units today, and the units' own prose
    explains ProtectHome was evaluated and rejected, not merely never
    considered) — this checks the ACTIVE directive set, matching what
    `systemctl --user show <unit> --property=ProtectHome` would report."""
    return not any(
        l.strip().startswith("ProtectHome=") for l in lines
    )


def test_gateway_unit_has_no_protecthome_directive():
    assert _no_protecthome_directive(_lines(_GATEWAY_UNIT))


def test_backup_unit_has_no_protecthome_directive():
    assert _no_protecthome_directive(_lines(_BACKUP_UNIT))


def test_logrotate_unit_has_no_protecthome_directive():
    assert _no_protecthome_directive(_lines(_LOGROTATE_UNIT))


# ── ProtectSystem=full — all three units (operator ruling, HYG round §9) ────
#
# `full` makes only /usr, /boot, /efi and /etc read-only for the unit — it
# never touches /tmp, /run or $XDG_RUNTIME_DIR (systemd.exec(5)). Two of
# these three units have a write path outside %h (recorded as a finding in
# the ε-lane handoff §5/§9, not resolved by this directive either way):
# hive-mind-gateway.service's default AF_UNIX socket path
# ($XDG_RUNTIME_DIR/shared-memory-gw.sock, falling back to /tmp) and
# shared-memory-backup.service's backup.sh init_secrets_dir() mktemp -d
# directory (default /tmp). Neither write path is under /usr, /boot, /efi or
# /etc, so `full` is harmless to add regardless of the %h finding — the
# operator ruled it onto all three units on that basis.

def test_gateway_unit_pins_protectsystem_full():
    assert "ProtectSystem=full" in _lines(_GATEWAY_UNIT)


def test_backup_unit_pins_protectsystem_full():
    assert "ProtectSystem=full" in _lines(_BACKUP_UNIT)


def test_logrotate_unit_pins_protectsystem_full():
    """Every write path of this unit is confirmed under %h (the --state
    file, the rotated *-audit.jsonl files, and their .gz siblings) — see the
    unit's own comment and README.md's Hardening section. Unlike the other
    two units, this one has no write-path-outside-%h finding at all."""
    assert "ProtectSystem=full" in _lines(_LOGROTATE_UNIT)


# ── Forbidden directives — ABSENT, pinned (step-2 review F2) ─────────────────
#
# The ruling forbids `PrivateTmp` in every unit and reserves `RestrictAddressFamilies`
# for the merger's test-host drop-in trial; neither may appear as a directive. And
# `LimitCORE=0` is only a fix while it is the ONLY LimitCORE line — a later
# `LimitCORE=infinity` would win and the `in` assertions above would stay green.

def _directive_lines(lines: list[str], name: str) -> list[str]:
    return [l.strip() for l in lines if l.strip().startswith(f"{name}=")]


@pytest.mark.parametrize("unit", [_GATEWAY_UNIT, _BACKUP_UNIT, _LOGROTATE_UNIT])
def test_unit_has_no_privatetmp_and_no_restrictaddressfamilies_directive(unit):
    lines = _lines(unit)
    assert _directive_lines(lines, "PrivateTmp") == [], unit
    assert _directive_lines(lines, "RestrictAddressFamilies") == [], unit


@pytest.mark.parametrize("unit", [_GATEWAY_UNIT, _BACKUP_UNIT, _LOGROTATE_UNIT])
def test_unit_has_exactly_one_limitcore_line_and_it_is_zero(unit):
    assert _directive_lines(_lines(unit), "LimitCORE") == ["LimitCORE=0"], unit
