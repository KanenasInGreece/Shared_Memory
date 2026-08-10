# HANDOFF — NREM telemetry gauge dead surface (fix/nrem-telemetry-gauge-dead)

Branch: `fix/nrem-telemetry-gauge-dead`, based on `main` @ `57de695` (v0.8.64).
No PR, no merge — this is a builder handoff for the operator/merger to review.

## The defect

`GET /memory/telemetry` → `telemetry.nrem` returned `{"error": "No module
named 'psycopg2'"}` on every call, on the live gateway, since before
v0.8.63 (verified pre-existing — same lazy import existed at that tag under
the old two-level function names).

`coordinator._nrem_cycle_counts` did `from consolidation_loop import
count_domain_level_cycles` behind a lazy import (only reached when the
gauge was actually called). `consolidation_loop.py` imports `psycopg2` at
module level (line ~56, for the daemon's own synchronous DB work). The
shipped gateway service
(`shared-memory/ops/hive-mind-gateway.service`) runs:

```
uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python shared-memory/scripts/hive_mind_proxy.py 8888
```

— no psycopg2. So the import raised `ModuleNotFoundError` every time,
caught somewhere upstream and rendered as an `{"error": ...}` payload rather
than crashing the gateway. 1236 unit tests stayed green throughout: every
one of them stubs DB access, and none exercises the gauge under the
gateway's real (restricted) dependency set. This is exactly CLAUDE.md's
Group 3 gap ("daemon/observability has NO mechanical test tie") and the
"green suite is not an all-clear" rule.

## The fix

Extracted the two PURE gate functions — `eligible_domain_level_clusters`
(the v2 fact-gate partitioner) and `count_domain_level_cycles` (its
count-only twin) — out of `consolidation_loop.py` into a new module:

**`shared-memory/scripts/nrem_gate.py`**

Chosen name: it names what the module IS (the NREM fact gate) rather than
where it came from — a future reader who has never heard of this defect
should still find it obviously the right home for a pure gate function.
Considered and rejected: `nrem_pure.py` (describes an implementation
property, not the module's role — every module here should try to be as
pure as possible, "pure" isn't distinctive); `fact_gate.py` (drops the
NREM/domain-level qualifier that distinguishes this from the insight/decision
gate in `insight_gate.py`, which is a real sibling module with a parallel
name).

Its only imports besides stdlib are `project_axis.fold_eligible` (which
itself only imports `os` and `ontology`) and `ontology` transitively — both
verified to import no DB driver, no network client.

**Re-export, not update-every-caller.** `consolidation_loop.py` now does
`from nrem_gate import eligible_domain_level_clusters,
count_domain_level_cycles` at its own module top level and keeps both names
in its namespace. Chosen over updating every caller because:
- `consolidation_loop.py`'s own fold code (`_consolidate_clusters` etc.)
  calls these functions unqualified — no diff needed there.
- Three existing test files (`test_nrem_axis_levels.py`,
  `test_nrem_confidence.py`, `test_v2_fact_gate.py`) do
  `from consolidation_loop import eligible_domain_level_clusters,
  count_domain_level_cycles` — all keep working unchanged, because Python
  resolves that import against `consolidation_loop`'s namespace, which now
  contains the re-exported names.
- This is a pure location split for the psycopg2-free callers (coordinator),
  not a rename or a behaviour change for anyone who legitimately needs the
  full daemon module anyway.

**`coordinator._nrem_cycle_counts`** now does `from nrem_gate import
count_domain_level_cycles` — never reaches into `consolidation_loop` again.

## Other lazy-import surfaces checked

Grepped `coordinator.py` and `hive_mind_proxy.py` for every lazy
`from <module> import` / `import <module>` reaching any of the 12 scripts
that import psycopg2 at module level (`backfill_project_of.py`,
`reconcile_project_edges.py`, `backfill_domain_of.py`,
`migrate_retro_edges.py`, `normalize_projects.py`,
`reconcile_project_identity.py`, `resolve_references.py`,
`sync_project_registry.py`, `consolidation_loop.py`, `rem_loop.py`,
`relation_confidence.py`, `relation_sweep.py`). **Result: the
`_nrem_cycle_counts` import was the ONLY one.** Both files' module-level
imports were also checked — neither imports any of those 12 modules at
top level either. No second dead surface found.

## Verification on the running system (no restart, no writes)

Ran the actual import and call under the gateway's exact `--with` list
(`aiohttp asyncpg neo4j httpx json-repair`, no psycopg2):

```
$ uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python -c "
import sys; sys.path.insert(0, 'shared-memory/scripts')
import nrem_gate
print('pure import OK:', nrem_gate.count_domain_level_cycles([], {}, {}, 3, set()))
"
pure import OK: 0
```

Before/after control — the SAME environment against the old path:

```
$ uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python -c "
import sys; sys.path.insert(0, 'shared-memory/scripts')
import consolidation_loop
"
ModuleNotFoundError: No module named 'psycopg2'
```

Confirmed `coordinator.py` itself now imports cleanly under the restricted
set, and its `_nrem_cycle_counts` source contains `from nrem_gate import
count_domain_level_cycles` and nothing referencing `consolidation_loop`.

**Live corpus proof (read-only, no restart, no writes):** ran the identical
Cypher `_nrem_cycle_counts` executes against the live Neo4j (credentials
read from the main repo's `shared-memory/.env` — not present in this
worktree, gitignored), fed the real rows into `nrem_gate.count_domain_level_cycles`
under the restricted dependency set:

```
LIVE fact_cycles via nrem_gate (read-only, gateway dep set): 2
rows fetched: 21 distinct pg_ids: 21
```

Matches the briefed expectation of 2 gating groups on the live corpus.

## Test obligations

**`tests/test_nrem_gate_import_purity.py`** (new, 4 tests) — the
class-level guard:
1. `test_nrem_gate_imports_and_runs_with_psycopg2_unimportable` — blocks
   `psycopg2` at the `sys.modules`/`builtins.__import__` level (a real
   ImportError on `import psycopg2`, not a string check) and proves
   `nrem_gate` imports and its function computes the right count anyway.
2. `test_consolidation_loop_import_DOES_fail_with_psycopg2_unimportable` —
   the control: proves the block is real, and proves WHY the split is
   necessary (consolidation_loop legitimately can't import in that
   environment).
3. `test_coordinator_never_imports_from_consolidation_loop` — scans the
   WHOLE `coordinator.py` source (not just one method) for any import
   reaching `consolidation_loop`, so a *different* future lazy import
   elsewhere in the file would also be caught.
4. `test_nrem_gate_source_imports_no_db_or_network_driver` — regex over
   actual `import`/`from` STATEMENT lines (not the docstring prose, which
   legitimately discusses psycopg2 while explaining the defect) for
   psycopg2/psycopg/asyncpg/neo4j/httpx/aiohttp.

Also updated **`tests/test_v2_fact_gate.py`**'s
`test_nrem_cycle_counts_reuses_the_folds_own_partitioner` — it used to
assert the OLD (defective) import string was present; now asserts
`from nrem_gate import count_domain_level_cycles` is present and
`from consolidation_loop import` is absent from that method's source.

### Mutation checks performed

1. **Reintroduced the original defect** — changed
   `coordinator._nrem_cycle_counts` back to
   `from consolidation_loop import count_domain_level_cycles`. Ran the full
   suite: **exactly 2 tests died** —
   `test_nrem_gate_import_purity.py::test_coordinator_never_imports_from_consolidation_loop`
   and
   `test_v2_fact_gate.py::test_nrem_cycle_counts_reuses_the_folds_own_partitioner`
   — 1238 passed, 2 failed. Reverted; suite back to 1240 passing.

2. **Reintroduced a DB driver import into `nrem_gate.py`** — added
   `import psycopg2` at its top level. Ran the purity test file:
   **exactly 2 tests died** —
   `test_nrem_gate_imports_and_runs_with_psycopg2_unimportable` (the
   functional guard — the module itself now fails under the block) and
   `test_nrem_gate_source_imports_no_db_or_network_driver` (the source
   guard). Reverted; suite back to 1240 passing.

Both mutations were caught by the guards designed for them, and by nothing
unrelated (no other test flickered).

## Final numbers

`uv run --with pytest --with pytest-asyncio --with fastmcp --with
psycopg2-binary --with httpx --with neo4j --with asyncpg --with aiohttp
--with json-repair --with numpy pytest tests/ -v` → **1240 passed**
(baseline 1236 + 4 new).

## Group 5 note

The shipped gateway service file (`shared-memory/ops/hive-mind-gateway.service`)
needs **no change** — that was the point of the fix. Its `--with` list is
correct as-is once the gauge no longer needs psycopg2 to compute its number.

## Not done (out of scope per brief)

No version bump, no CHANGELOG entry, no `sync_skills.sh` run (server-side
daemon/gateway code only — not a client-surface file per the client/server
split), no service restart, no deploy. Left for the operator/merger.
