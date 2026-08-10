# HANDOFF — task B2 (I7 stall contract)

**Branch:** `fix/i7-stall-contract`, based on `main` @ `0e43d74` (v0.8.61).
**Plan:** `Dreaming_Cycle_Plan_to_v2.md` §2.6 (I7), §6 Track B, task B2.
**Status: DONE.** All acceptance criteria met. Not merged, no PR opened (per brief).

## What is done

1. **The fix** — `shared-memory/scripts/coordinator.py`:
   - `_consolidation_backlog(eligible_clusters, nrem_count)` → `_consolidation_backlog(eligible_clusters)`.
     No more fallback to the looser NREM density count. `eligible_clusters is None` (no
     recorded census) now returns `0`, not a substitute count. Docstring cites `decision:1121`
     and states I7 in plain terms.
   - `_consolidation_stall_verdict` — **logic unchanged** (the brief is explicit: it was already
     correct). Docstring extended to cite `decision:1121` and note that its correctness now
     depends entirely on what `_consolidation_backlog` passes it.
   - `_compute_consolidation_health` — the `try: nrem = await self._nrem_cycle_counts() … backlog
     = {...}` block is **removed entirely**, not just bypassed. The call site is now
     `_consolidation_backlog(elig)` — one argument. Docstring updated to say `_nrem_cycle_counts`
     is no longer consulted here and remains a separate, purely informational gauge elsewhere
     (`snap["nrem"]` in the telemetry snapshot, built by a *different* method at a different
     call site — untouched).

2. **Tests** — `tests/test_consolidation_signal.py`:
   - `test_backlog_prefers_gate_census_over_nrem` (asserted the OLD contract) →
     `test_backlog_is_the_gate_census_only` (asserts the I7 contract: `_consolidation_backlog(None)
     == 0`).
   - **New composition test**: `test_no_census_is_not_reported_as_a_stall_composition`. Stubs
     `coord._acquire()` to return one `consolidation_runs` roll-up row for `"insight"` with
     `eligible_clusters = None` (no census ever recorded), calls the real
     `coord._compute_consolidation_health()`, and asserts:
     - `out["insight"]["backlog"] == 0`
     - `out["insight"]["stalled"] is False`
     - a spy on `coord._nrem_cycle_counts` was **never called** — this is the assertion that
       bites the *composition*, not just the pure function: it proves the fallback path doesn't
       exist anywhere between the roll-up and the verdict, not merely that the pure function
       handles `None` correctly in isolation.

## Mutation check — performed, and reverted cleanly

Reintroduced the exact old defect in `coordinator.py` (three edits, each marked `# MUTATION` while
applied): `_consolidation_backlog` signature restored to take `nrem_count`, its body restored to
`return eligible_clusters if eligible_clusters is not None else nrem_count`, and
`_compute_consolidation_health` restored to call `self._nrem_cycle_counts()` and pass
`backlog.get(ct, 0)` into `_consolidation_backlog`.

Ran `tests/test_consolidation_signal.py`. **Two tests died**, both as expected:

- `test_backlog_is_the_gate_census_only` — `TypeError: missing 1 required positional argument:
  'nrem_count'` (weak signal — just an arity mismatch).
- `test_no_census_is_not_reported_as_a_stall_composition` — **`AssertionError: assert 5 == 0`** on
  `out["insight"]["backlog"]`. This is the meaningful one: the composition test's `nrem_spy` was
  configured to return `decision_cycles=5`, and with the mutation live, that 5 leaked all the way
  through the roll-up into the reported backlog — exactly the defect I7 rules out. The
  `nrem_spy.assert_not_called()` line never even got reached because the prior assertion failed
  first, which is itself further confirmation the spy WAS called under the mutation.

All 18 other tests in the file stayed green under the mutation, confirming the mutation was scoped
correctly (it didn't break unrelated things, so the 2 failures are attributable to this specific
change).

Reverted with `git checkout -- shared-memory/scripts/coordinator.py`; `git diff` against the prior
commit is empty; re-ran the file, 20/20 green again.

## Docstrings citing decision:1121 / stating I7

Present on: `_consolidation_backlog`, `_consolidation_stall_verdict` (contract note, logic
untouched), `_compute_consolidation_health`. All three state the plain-terms I7 rule: gated-but-
not-folding is a stall; not-gating is a correct outcome, not backlog.

## Monitor effect — named explicitly

Traced the full read path before writing this:

- `GET /health` (unauthenticated, cached, polled frequently — `coordinator.consolidation_health()`,
  `coordinator.py:5986`) exposes only the **compact roll-up**: `consolidation.stalled` (OR across
  cycle types), `consolidation.stalled_types`, `last_outcome`, `last_success_age_seconds`,
  `last_success_cycle_type`. It does **not** expose per-type `backlog` / `eligible_clusters` at all.
- `GET /memory/telemetry` (auth-scoped — `_consolidation_telemetry()` at `coordinator.py:5982`,
  which is a direct pass-through of `_compute_consolidation_health()`) exposes the **full**
  per-type dicts: `consolidation.insight.backlog`, `consolidation.insight.eligible_clusters`,
  `consolidation.insight.stalled`, and the same three keys under `consolidation.fact_consolidation`.

**What can flip, and when:** `consolidation.<type>.backlog` and `consolidation.<type>.stalled` (on
`/memory/telemetry`), and — only when that flip changes the OR across both types — the top-level
`consolidation.stalled` / `consolidation.stalled_types` on `/health` too. The flip happens **only**
in the state where a cycle type has **never recorded a census** (`eligible_clusters IS NULL` on
every one of its `consolidation_runs` rows — a fresh deploy before the gate has run once, or every
run since crashed before reaching the gate-evaluation line). In that state: **before**, backlog
could read a positive number (the density count) and `stalled` could read `true` on a system that
had simply never gated yet; **after**, backlog reads `0` and `stalled` reads `false`.
`eligible_clusters` itself is never touched by this fix — its value and how it's written
(`consolidation_loop.py`, untouched) are exactly as before.

**No field's MEANING inverts, so no rename is warranted.** `stalled` still means the same thing it
always did — "a gated backlog is stuck past the threshold with nothing in-flight." What changed is
the *population* counted as backlog (gating-only, not gating-plus-raw-density), which is a
correctness fix to the input, not a redefinition of the output. I considered CLAUDE.md's
"rename if meaning inverts" rule explicitly and judged it does not apply here — flagging that
judgement per the brief's instruction rather than silently deciding it.

**Live impact right now: none, measured.** Queried `consolidation_runs` on the live DB (read-only,
`shared-memory/.env` copied into this worktree from the main repo's `shared-memory/.env` — see
below):

```
cycle_type          total_rows  rows_with_census  rows_without_census  latest_recorded_census
fact_consolidation        7250              1488                 5762                       0
insight                    4215               390                 3825                       0
```

Both cycle types have a **recorded** census on their most recent run (`latest_recorded_census = 0`
for both, not NULL) — so on the current live gateway, `eligible_clusters is not None` for both
types today, meaning the OLD code and the NEW code compute the identical value (`0`) right now. The
fix changes nothing observable on this deployment as it stands; it only matters for a future
fresh-deploy window or a crash-before-gate-evaluation window. This matches the captured `/health`
snapshot below (`stalled: false` already).

## Live `/health` observed (read-only, gateway NOT restarted, v0.8.61 still running)

```json
"consolidation": {
  "domain_identity": {"complete": true, "mismatched": 0, "nodes": 16, "registry_rows": 16, "unattached": 0, "unregistered": 0},
  "fresh": true,
  "graph_invalid_nodes": 0,
  "inference_busy": "idle",
  "last_outcome": "completed",
  "last_success_age_seconds": 227705,
  "last_success_cycle_type": "fact_consolidation",
  "project_identity": {"complete": true, "mismatched": 0, "nodes": 11, "unidentified": 0, "unregistered": 0},
  "stalled": false,
  "stalled_types": []
}
```
No "after" capture: the gateway process still runs the old code (not restarted, per instruction),
so this is the only observation possible for this task — it also happens to be the value that
persists unchanged until Opus deploys, per the "no live impact right now" finding above.

`/memory/telemetry` (which would show the per-type `backlog`/`eligible_clusters` fields directly)
requires a bearer token; not queried (not in scope — read-only `/health` + direct read-only SQL
against `consolidation_runs` were sufficient to establish the live-impact finding above, and
pulling a token felt like more access than this observation needed).

## A judgement the diff does not show

Removed the `_nrem_cycle_counts()` call and the `backlog = {...}` dict from
`_compute_consolidation_health` **entirely**, rather than leaving them computed-but-unused. This
was a deliberate choice, not required word-for-word by the brief: leaving a computed-but-ignored
Neo4j+Postgres query in place is exactly the shape of thing that grows a silent re-introduction of
the fallback later (someone sees `nrem` sitting right there and "restores" its use). Removing it
also drops an extra Neo4j+Postgres round-trip from every `/health` background refresh cycle
(`CONSOLIDATION_HEALTH_REFRESH_SEC`, default 60s) — a minor performance side-benefit, not the
reason for the choice. `_nrem_cycle_counts()` itself is untouched and still used by the unrelated
`snap["nrem"]` telemetry field (`coordinator.py:5394`, inside `handle_telemetry`, a different
method) — confirmed by grep before deleting anything, so this is not a silent removal of the only
caller.

## Tests

- `tests/test_consolidation_signal.py`: 20/20 passing (mutation-checked as above).
- Full suite from worktree root: **1244 passed, 1 pre-existing failure unrelated to this task.**
  ```
  uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
    --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -q
  ```
  The one failure —
  `tests/test_capture_surface_documented.py::test_the_worked_examples_state_the_current_contract`
  — asserts that `SKILL.md`'s worked examples mention the current `memory_bridge.VERSION`
  (`0.8.61`). This branch never touches `SKILL.md` or `memory_bridge.py` (`git diff main --stat`
  shows only `coordinator.py` and `tests/test_consolidation_signal.py` changed), and the same
  version-drift condition is present on `main` unmodified — it is Track-1/Group-1 client-surface
  bookkeeping (a version-bump-time chore), not something B2 introduced or is scoped to fix.

## What is NOT committed / worktree-local only

- `shared-memory/.env` — copied from the main repo's `shared-memory/.env` into this worktree so
  the pre-commit private-name guard hook and the live-DB verification step could run (it's
  gitignored everywhere, including here; `git status` confirms it is untracked and will stay
  that way). Same file, same machine, same credentials the gateway already uses — nothing new was
  provisioned.

## What is next

Nothing — B2 is complete per the brief's acceptance criteria. Ready for Opus to review/merge
alongside B1 (parallel Track B) and Track C.
