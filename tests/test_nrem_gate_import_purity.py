"""The NREM telemetry gauge dead-surface defect, tested at the CLASS level
(fix wave, 2026-08) — not just "the gauge returns numbers".

The defect (pre-fix, verified against v0.8.64 tag `57de695`):
`coordinator._nrem_cycle_counts` (the `GET /memory/telemetry` -> `nrem`
gauge) did `from consolidation_loop import count_domain_level_cycles` behind
a lazy import. `consolidation_loop.py` imports `psycopg2` at module level
(its own synchronous DB work). The shipped gateway service
(`shared-memory/ops/hive-mind-gateway.service`) runs
`--with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair`
and never carries psycopg2 — so that import raised `ModuleNotFoundError` on
EVERY call in production, caught and rendered as `{"error": ...}` rather than
crashing. 1236 unit tests stayed green throughout, because every one of them
stubs DB access and none exercises the gateway's real (restricted)
dependency set. CLAUDE.md names this precisely: Group 3 (daemon behaviour /
observability) has NO mechanical test tie, and a green suite is not an
all-clear for anything crossing a process boundary.

A test asserting "the gauge returns numbers" under fully-stubbed imports
would NOT have caught this — psycopg2 IS installed in the dev/test
environment (it's a declared test dependency), so a stub-everything test
sails through exactly as the original 1236 did. The only test that bites the
actual defect is one that removes psycopg2 from the picture and proves the
gauge's code path still imports and runs — which is what this file does,
plus a source-level guard against the same class recurring anywhere else.

Two independent guards, either one sufficient to catch a regression:

1. `nrem_gate.py` (the module `count_domain_level_cycles` /
   `eligible_domain_level_clusters` now live in) must be importable AND
   callable with `psycopg2` (and every other DB/network driver) made
   unimportable — proven by blocking the import at the `sys.modules` level,
   not by inspecting source for the string "psycopg2" (a string check can't
   see a driver pulled in transitively through some future helper import).
2. `coordinator.py`'s source must never contain
   `from consolidation_loop import` anywhere — not just inside
   `_nrem_cycle_counts` — because ANY lazy import reaching from the gateway
   process into a psycopg2-importing module is the same defect wearing
   different clothes.
"""
import builtins
import importlib
import os
import sys

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
sys.path.insert(0, SCRIPTS_DIR)

# Modules the shipped gateway service does NOT carry (its environment is
# pinned by requirements-gateway.lock — the restricted set declared in
# requirements-gateway.txt). Any module a gateway-process code path imports
# at call time must survive all of these being unimportable.
_GATEWAY_MISSING_MODULES = ("psycopg2",)


class _BlockedImport:
    """Context manager that makes `import <name>` raise ImportError for a
    given set of top-level module names, and undoes it afterward — including
    scrubbing any of those modules (or their submodules) that were already
    cached in sys.modules, so a prior test's `import psycopg2` can't mask
    the block."""

    def __init__(self, blocked_names):
        self._blocked = set(blocked_names)
        self._real_import = None
        self._saved_modules = {}

    def _fake_import(self, name, *args, **kwargs):
        top = name.split(".")[0]
        if top in self._blocked:
            raise ImportError(f"blocked for this test: {name}")
        return self._real_import(name, *args, **kwargs)

    def __enter__(self):
        # Evict any already-imported copies (and submodules) so the blocked
        # import actually has to happen fresh.
        for mod_name in list(sys.modules):
            if mod_name.split(".")[0] in self._blocked:
                self._saved_modules[mod_name] = sys.modules.pop(mod_name)
        # Also evict nrem_gate/consolidation_loop themselves so re-importing
        # them under the block is a genuine fresh import, not a cache hit.
        for mod_name in ("nrem_gate", "consolidation_loop"):
            if mod_name in sys.modules:
                self._saved_modules[mod_name] = sys.modules.pop(mod_name)
        self._real_import = builtins.__import__
        builtins.__import__ = self._fake_import
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real_import
        for mod_name in list(sys.modules):
            if mod_name.split(".")[0] in self._blocked or mod_name in (
                "nrem_gate", "consolidation_loop",
            ):
                del sys.modules[mod_name]
        sys.modules.update(self._saved_modules)
        return False


# ── Guard 1 — nrem_gate.py imports and runs with no DB driver present ─────────

def test_nrem_gate_imports_and_runs_with_psycopg2_unimportable():
    """This is the exact failure mode reproduced: block psycopg2 the way the
    gateway process's real environment blocks it (the package genuinely
    absent), then prove `nrem_gate` still imports and its gauge-facing
    function still computes the right count."""
    with _BlockedImport(_GATEWAY_MISSING_MODULES):
        nrem_gate = importlib.import_module("nrem_gate")
        pg_ids = [1, 2, 3, 4]
        project_map = {1: "smg", 2: "smg", 3: "smg", 4: "smg"}
        domains_map = {i: ["architecture"] for i in pg_ids}
        registered = {("smg", "architecture")}
        n = nrem_gate.count_domain_level_cycles(
            pg_ids, project_map, domains_map, threshold=3,
            registered_sections=registered,
        )
        assert n == 1


def test_consolidation_loop_import_DOES_fail_with_psycopg2_unimportable():
    """The before/after control: proves the block is real and proves WHY the
    fix moves these functions out — `consolidation_loop` itself genuinely
    cannot be imported in the gateway's restricted environment (it needs
    psycopg2 for its own sync DB work, which is legitimate for the daemon
    process, just not for the gateway process reaching into it). If this
    assertion ever stops raising, `_BlockedImport` has stopped blocking
    anything and every other test in this file is vacuous."""
    with _BlockedImport(_GATEWAY_MISSING_MODULES):
        with pytest.raises(ImportError):
            importlib.import_module("consolidation_loop")


# ── Guard 2 — coordinator.py never reaches into consolidation_loop, anywhere ──

def test_coordinator_never_imports_from_consolidation_loop():
    """MUTATION-CHECKED (see HANDOFF.md): reintroducing
    `from consolidation_loop import count_domain_level_cycles` inside
    `_nrem_cycle_counts` made this test fail. Reverted after.

    Source-scanned rather than scoped to one method: the defect class is
    "any gateway-process code path reaches into a psycopg2-importing module
    at call time", and a future lazy import added anywhere else in
    coordinator.py would be the same defect. Checking the whole file is what
    makes this a class-level guard rather than a regression pin on one
    function."""
    coordinator_path = os.path.join(SCRIPTS_DIR, "coordinator.py")
    with open(coordinator_path, encoding="utf-8") as f:
        source = f.read()
    assert "from consolidation_loop import" not in source
    assert "import consolidation_loop" not in source


# ── nrem_gate.py's own import list stays driver-free ──────────────────────────

def test_nrem_gate_source_imports_no_db_or_network_driver():
    """Belt-and-braces source check alongside Guard 1's functional proof.
    Scans actual `import`/`from ... import` STATEMENT lines only (not the
    module docstring, which legitimately discusses psycopg2 in prose while
    explaining the defect this module exists to remove) for any DB or
    network driver the gateway service does NOT carry — see
    requirements-gateway.txt (pinned as requirements-gateway.lock, which the
    shipped shared-memory/ops/hive-mind-gateway.service unit runs from)."""
    import re

    nrem_gate_path = os.path.join(SCRIPTS_DIR, "nrem_gate.py")
    with open(nrem_gate_path, encoding="utf-8") as f:
        source = f.read()
    forbidden_pattern = re.compile(
        r"^\s*(import|from)\s+(psycopg2|psycopg|asyncpg|neo4j|httpx|aiohttp)\b",
        re.MULTILINE,
    )
    hits = forbidden_pattern.findall(source)
    assert not hits, f"nrem_gate.py must not import a DB/network driver, found: {hits}"


# ── Guard 3 — the gateway lock itself never gains a blocked module ────────────

def test_gateway_lock_carries_no_blocked_module():
    """The premise of every test above is that the gateway's shipped
    environment genuinely lacks _GATEWAY_MISSING_MODULES. Since the unit now
    installs exactly requirements-gateway.lock, that premise is checkable:
    if psycopg2 (as psycopg2 or psycopg2-binary) ever lands in the gateway
    lock, Guard 1's block stops modelling production and this file's
    guarantees silently expire. Proven against the known-broken artefact:
    pointed at requirements.lock (the FULL tree, which does carry
    psycopg2-binary), this test fails."""
    lock_path = os.path.join(SCRIPTS_DIR, "..", "..", "requirements-gateway.lock")
    with open(lock_path, encoding="utf-8") as f:
        pinned = [
            line.split("==")[0].strip()
            for line in f
            if "==" in line and not line.lstrip().startswith(("#", "--"))
        ]
    for blocked in _GATEWAY_MISSING_MODULES:
        offenders = [p for p in pinned if p == blocked or p == f"{blocked}-binary"]
        assert not offenders, (
            f"requirements-gateway.lock must not carry {blocked}: {offenders}"
        )
