# HANDOFF — C3.1: pre-first-firing fixes to the refold ledger (v0.8.67 C3)

**Task id:** C3.1, per `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §6 "C3.1 — pre-first-firing
fixes to C3 (REQUIRED BEFORE C4)". Base: `main` `54d300a` (v0.8.67, PR #227). Branch:
`fix/c31-refold-ledger-fixes`. **Not merged. No version bump. No CHANGELOG entry.**

## Status: DONE — all three fixes implemented, tested, mutation-checked, and proven on a
throwaway DB. Ready for review.

## What each fix does

### F0 — resurrection gap (HIGH)
Migration 029's partial unique index `community_summaries_axis_level_unique` did not exclude
`superseded` rows, and `_write_summary`'s `ON CONFLICT ... DO UPDATE` never touched
`superseded`/`superseded_at`/`superseded_reason`. A lineage-retired `(project, domain)` row would be
UPDATEd in place by the next fold on that axis key and stay superseded forever.

- **New migration** `shared-memory/migrations/032_axis_index_excludes_superseded.sql` — drops and
  recreates the index with `AND NOT superseded` appended to the partial predicate, one transaction,
  idempotent (`DROP INDEX IF EXISTS` + `CREATE UNIQUE INDEX IF NOT EXISTS`).
- **`shared-memory/scripts/consolidation_loop.py`**, `_write_summary` (inside
  `ConsolidationDaemon._consolidate_clusters`, ~line 2984): the `ON CONFLICT (...) WHERE ...` arbiter
  now carries the same `AND NOT superseded` predicate, so it matches the rebuilt index and a retired
  row no longer participates in the conflict — the next fold INSERTs a fresh ACTIVE row instead.
- No graph-side `SUPERSEDES` edge is drawn (left parked with the Tier-3 supersession item, per the
  plan).

### F1 — unclosable clock rows (MEDIUM)
`below_density_ids = pg_ids_all - all_member_ids` can only ever name a `pg_id` that was already a
member of `pg_ids_all` (the grounded+domained `_find_grounded_fact_groups` scan). A constituent that
is ungrounded or domainless never enters `pg_ids_all` at all, so its open thematic-kind ledger row was
permanently unclosable.

- **New function** `drop_out_of_scan_refold_rows(conn, scanned_pg_ids, context)` in
  `consolidation_loop.py` (right after `drop_below_density_refold_rows`) — closes any OPEN
  `summary_kind='thematic'` row whose `pg_id` is not in `scanned_pg_ids` at all, with distinct
  `closed_reason='out_of_scan'`. Unlike `drop_below_density_refold_rows` it has **no early-return
  guard** — it always runs a query, because we cannot know in advance (without querying) whether
  anything needs closing.
- **Call site**: added right after the existing `drop_below_density_refold_rows` call in
  `_consolidate_clusters`, passed `pg_ids_all` (the full cycle scan — never `all_member_ids`, which is
  the already-gating subset `below_density_ids` is drawn from).
- `summary_kind='thematic'` is load-bearing (I17 spirit) — insight-kind rows are never touched here.

### F2 — false 'refolded' attribution (LOW/MED)
`close_refold_ledger_rows`'s `'refolded'` branch matched ANY active summary containing the `pg_id`,
including one that PREDATES the ledger row (measured live: fact 1149 sits in a third, untouched
summary and would have closed `constituent_folded` with nothing actually folded).

- Added `AND COALESCE(cs.updated_at, cs.created_at) >= o.created_at` to the `EXISTS` subquery in
  `close_refold_ledger_rows`. The fold UPSERT sets `updated_at = now()` on every real fold; a fresh
  INSERT defaults both columns together — so the bound only excludes summaries that could not have
  been the re-fold this row is waiting for.

## Files changed
```
shared-memory/migrations/032_axis_index_excludes_superseded.sql   (new)
shared-memory/scripts/consolidation_loop.py                       (F0/F1/F2 + docstrings)
shared-memory/Documentation/schema.md                             (index/status/written-by updates)
shared-memory-skill/shared-memory/Documentation/schema.md         (synced copy — MANIFEST parity)
tests/test_lineage_invalidation.py                                (6 new tests, all mutation-checked)
tests/test_nrem_confidence.py                                     (fixture realignment — see below)
```

## Tests

`tests/test_lineage_invalidation.py` — 6 new tests appended (31 total in the file, was 25):

- `test_f0_migration_032_rebuilds_the_axis_index_excluding_superseded` — reads the migration file,
  asserts one transaction, idempotent DDL, and `AND NOT superseded` on the CREATE clause.
- `test_f0_write_summary_on_conflict_arbiter_excludes_superseded` — `inspect.getsource` on
  `_consolidate_clusters`, asserts the ON CONFLICT WHERE clause carries the same predicate as the
  index.
- `test_f1_drop_out_of_scan_closes_rows_never_seen_by_the_scan` — direct unit test of the new function.
- `test_f1_drop_out_of_scan_reason_is_distinct_from_below_density` — the two closed_reason strings
  differ.
- `test_f1_consolidate_clusters_calls_out_of_scan_close_with_full_scan_set` — composition check via
  `inspect.getsource`: the call site exists and passes `pg_ids_all`, never `all_member_ids` or
  `below_density_ids`.
- `test_f2_refolded_close_requires_covering_summary_no_older_than_the_row` — asserts the recency
  predicate is present, and that it's additive (existing kind/superseded guards still there).

**Why source-scan (`inspect.getsource`) rather than driving `_consolidate_clusters` directly for F0/F1
composition:** that method is a ~400-line async body with LLM/embedding/graph side effects; the
codebase already uses this pattern for exactly this shape of check (see
`test_rem_axis_gate.py`, `test_migration_ledger.py`, `test_v2_fact_gate.py`'s I1/I2 composition
tests). The direct-execution path for `_consolidate_clusters` IS exercised — by
`test_nrem_confidence.py`'s `_thematic_conn_script`-driven tests (see below), which prove the new
call site doesn't break the real flow.

### Mutation checks — all six, every one killed exactly the intended test and no other

| Guard reverted | Test that died | Others in same `-k` group |
|---|---|---|
| `AND NOT superseded` removed from migration 032's CREATE INDEX | `test_f0_migration_032_...` | `test_f0_write_summary_...` still passed |
| `AND NOT superseded` removed from `_write_summary`'s ON CONFLICT WHERE | `test_f0_write_summary_...` | `test_f0_migration_032_...` still passed |
| `drop_out_of_scan_refold_rows(...)` call site deleted | `test_f1_consolidate_clusters_calls_out_of_scan_close_...` | other 2 F1 tests still passed |
| Call site arg changed `pg_ids_all` → `all_member_ids` | same composition test | same |
| `summary_kind = 'thematic'` filter removed from `drop_out_of_scan_refold_rows`'s SQL | `test_f1_drop_out_of_scan_closes_rows_never_seen_by_the_scan` | other 2 F1 tests still passed |
| `AND COALESCE(cs.updated_at, cs.created_at) >= o.created_at` removed from `close_refold_ledger_rows` | `test_f2_refolded_close_requires_covering_summary_...` | (only F2 test) |

Each mutation was applied via a scripted `python3` patch, the targeted `-k` group re-run, the failure
inspected, then the file restored from a `/tmp` backup and `diff`-confirmed byte-identical to the
pre-mutation state before moving to the next.

### Fixture realignment in `test_nrem_confidence.py`

`drop_out_of_scan_refold_rows` has no early-return guard (unlike `drop_below_density_refold_rows`,
which skips its query entirely when the below-density set is empty), so it always issues one query.
`_thematic_conn_script()` (the scripted `StubConn` that drives real `_consolidate_clusters` calls in
3 tests + the MOCK_LLM end-to-end test) had exactly 9 scripted responses matching the pre-C3.1 query
count; my new call added a 10th query between "coverage census" and "fold dead-letter counts", shifting
every later scripted response by one and causing `cur.fetchone()[0]` to read `None` on the INSERT
(`'NoneType' object is not subscriptable`) 3 tests deep in. Fixed by inserting a
`{"rowcount": 0, "rows": []}` entry at the correct position (documented inline in the fixture) — this
is real evidence the new call site is wired into the actual `_consolidate_clusters` flow, not just
present in source.

## Full suite

```
MOCK_LLM=1 uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary \
  --with httpx --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy \
  pytest tests/ -v
```
**1297 passed, 1 failed.** The 1 failure (`test_rem_grounding_slice.py::
test_grounding_slice_merges_batch_round_robin`) is **pre-existing on `main` `54d300a`** —
verified by running the same test in isolation against the unmodified base commit before any
C3.1 change; it fails there too (`assert 'fallback' == 'knn'` — unrelated to consolidation_loop.py
or refold_ledger). Not touched by this branch.

## Throwaway-DB proof

Database `c31_scratch` created via `CREATE DATABASE`, `schema_init.sql` applied, then migration 032
applied on top. Real INSERTs proved:

**(a) Active + superseded coexist on the same axis key.** Inserted an active row (id 1, project
`alpha-service`), retired it (`superseded=true, superseded_reason='lineage'`), then ran the *exact*
`ON CONFLICT` INSERT statement from `_write_summary` again on the same axis key. Result: a **new**
row (id 2) was created — row 1's content (`v1 content`) and `source_pg_ids` (`[1,2,3]`) were
**unmodified**. `SELECT` over the axis key showed both rows: `(1, superseded=True, 'v1 content')`,
`(2, superseded=False, 'v2 content (post-retirement)')`.

**(b) The retired row is never resurrected.** Confirmed directly above — row 1's content/
source_pg_ids after the second INSERT were byte-identical to what was written before retirement.

**(c) Two ACTIVE rows on one axis key are still rejected.** Running the same `ON CONFLICT ... DO
UPDATE` INSERT a third time (with an active row 2 already present) updated row 2 in place (content
became `'v3 -- should collide with row2'`), row count stayed at 2 (1 retired + 1 active) — proving
the arbiter still enforces at-most-one-active-row via `DO UPDATE`. To rule out "the DO UPDATE branch
is just silently absorbing a would-be violation", a **bare `INSERT` with no `ON CONFLICT` clause** was
also run for a second active row on the same key: it was rejected outright by Postgres —
`duplicate key value violates unique constraint "community_summaries_axis_level_unique"`. A second
**superseded** row on the same axis key (bare INSERT, `superseded=true`) was accepted normally,
confirming superseded rows are excluded from the index's uniqueness scope as designed.

Database dropped after (`DROP DATABASE c31_scratch`). Exact statements are in this file's git history
via the shell commands issued during the build (see PR description for the condensed version); not
re-pasted here to keep this file navigable — ask if the verbatim transcript is needed.

## Live DB (read-only) verification

Ran the SELECT-form of all three changed predicates verbatim against live `agent_data` — no writes:

- F0 arbiter predicate (`WHERE COALESCE(metadata->>'kind','thematic') <> 'insight' AND NOT
  superseded`) — parsed, returned 5 sample ids.
- F1 out-of-scan predicate (`WHERE status='open' AND summary_kind='thematic' AND NOT (pg_id =
  ANY(...))`) — parsed, returned **0 rows** (consistent with the plan's statement that
  `refold_ledger` is empty — the lineage pass has never fired live).
- F2 recency-gated EXISTS subquery — parsed, returned **0 rows** (same reason).

All three column names and joins are valid against the live schema. **Migration 032 itself was NOT
applied to the live database** — per the task brief, that is the release step, not the builder's.

## Owed at release (NOT done here — explicitly out of scope for a builder)

1. **Apply migration 032 to the live `agent_data` database** (`migrations/apply.py`).
2. **Regenerate `schema_init.sql`** (`migrations/generate_schema_init.py`) — ⚠ per `CLAUDE.md`'s
   standing warning, the generator has **silently dropped DDL classes before** (CHECK constraints,
   FOREIGN KEYs, IDENTITY columns) and a **partial-index predicate is exactly the same class of thing**
   — verify by hand that the regenerated `schema_init.sql`'s `community_summaries_axis_level_unique`
   line actually carries `AND NOT superseded`, don't just trust the generator ran without error.
3. **Run `verify_schema_init.py`** against a fresh throwaway DB built from the regenerated
   `schema_init.sql` and diff against the live database, per the standing Group 4 obligation.
4. Standard release mechanics (not done by builders per the plan's §7.3): version bump (+0.0.1),
   CHANGELOG entry, `sync_skills.sh` (this branch already manually synced the one drifted file,
   `Documentation/schema.md`, to keep the suite green — a real `sync_skills.sh` run at release time
   is still the authoritative step and should be run anyway), restart `hive-mind-gateway.service`,
   verify `/health`.

## Judgement calls the diff doesn't show

- **`drop_out_of_scan_refold_rows` deliberately has no early-return guard for an empty `scanned_pg_ids`
  list.** If `pg_ids_all` is empty (the fact scan found nothing this cycle at all), the function closes
  **every** open thematic row as `out_of_scan` — this was left as specified rather than special-cased,
  because it is the textually correct reading of "closes open thematic rows whose pg_id is NOT in
  `pg_ids_all` at all": if nothing was scanned, nothing in `pg_ids_all`, so nothing is "in scan" by
  definition. Flagging in case this reads as too aggressive on review — an alternative would be to
  skip the close entirely when `pg_ids_all` is empty (treating an empty scan as inconclusive rather
  than "definitely out of scope"), but that reintroduces a possible-stall ambiguity I judged worse.
- **Index name kept identical** (`community_summaries_axis_level_unique`) across the drop+recreate in
  migration 032, rather than renaming — `ON CONFLICT` matches by expression+predicate, not by name, so
  a rename wasn't required; kept the name so any code/docs referring to it by name don't need updating.
- **`shared-memory-skill/shared-memory/Documentation/schema.md`** was manually copied (not via
  `sync_skills.sh`, which builders are told never to touch) to keep
  `test_every_manifest_file_is_byte_identical_across_both_tracked_copies` green. This is a plain file
  copy, not a script run — flagging in case the reviewer wants the real `sync_skills.sh` run instead
  at merge time (it would produce the same result for this one file, but also re-verifies the other
  MANIFEST entries).
