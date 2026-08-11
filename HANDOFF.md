# HANDOFF — fix/nrem-truth-observability: D1–D4 code-review fixes + O1/O2 observability

**Task id:** the six-item remit in the build-cycle brief (plan §7, `Local_Documentation/
Dreaming_Cycle_Plan_to_v2.md`). Base: `main` `01b72c9` (v0.8.69, PR #229 merged — the C4 payload
work). Branch: `fix/nrem-truth-observability`. **Not merged. Not pushed. No PR opened. No version
bump. No CHANGELOG entry. No `sync_skills.sh` run.**

This file **replaces** the stale `HANDOFF.md` this worktree inherited from the C4 PR (branch
`feat/c4-payload`, base `962b6b4`) — that PR's own handoff was never cleaned up at merge and
shipped to `main` as part of commit `01b72c9`, the same process gap that PR's own handoff flagged
about *its* inherited stale file. Everything below is this task's six items only (D1–D4, O1, O2).

## Status: DONE — all six items implemented, tested, mutation-checked, live-verified.

## What shipped, per item

### D1 — HIGH: census counted dead-lettered clusters as eligible backlog forever
`consolidation_loop.py`: in **both** `_consolidate_clusters` (the fact cycle) and
`run_insight_cycle` (the insight cycle), `rec.eligible_clusters`/`rec.eligible_oldest_age` were
computed over **every** density-gated cluster/group **before** the `NREM_FOLD_FAIL_CAP`
dead-letter filter ran. A cluster that fails permanently (3 preservation/truncation failures within
the window, by default) therefore counted as eligible backlog **forever**, because
`coordinator.py`'s `_consolidation_stall_verdict` reads backlog from **exactly this recorded
census and nothing else** (`decision:1121`, I7: *"backlog is the cycle's OWN gate census — no
looser fallback"*). One dead-lettered cluster meant the stall flag could never clear again.

**Fix — same shape in both cycles:** partition dead-lettered clusters out **before** the census
runs, not inside the fold loop. The excluded count is a **new** `_CycleRec` field/telemetry key,
`dead_lettered_clusters`, reported inside `extra()` — **never folded into `eligible_clusters`'
existing meaning** (CLAUDE.md Group 3: a metric whose meaning changes must change name).

- **Fact cycle** (`_consolidate_clusters`, ~line 2860 onward): `fetch_fold_dead_letter_counts()`
  moved up to run **before** the census; a new partition loop builds `eligible_work_items` (only
  the non-dead-lettered work items) and `dead_lettered_count`; the census (`rec.eligible_clusters`,
  `rec.eligible_oldest_age`) now runs over `eligible_work_items` only; the fold loop iterates
  `eligible_work_items` (the old in-loop dead-letter check/`continue` is gone — the list is already
  filtered). The **I7 `refold_ledger` below_density-close logic is deliberately kept scoped to ALL
  density-gated members** (`all_gated_member_ids`, built from the *unfiltered* `work_items`) —
  a dead-lettered cluster's members **did** meet density; they are simply not folding again right
  now, which is a different fact from never having gated, and must not be reported as
  `dropped/below_density`.
- **Insight cycle** (`run_insight_cycle`, the fresh-cluster block): the same partition pattern —
  `_dead_lettered(...)` is now called exactly **once** per fresh cluster, before the census, and
  its logging/`rec.fold_dead_letter` side effect fires exactly once (the fold loop no longer
  re-checks it). `clusters` is reassigned to the filtered list so the census and the fold loop share
  one filtered view.

Both cite `decision:1121`/I7 in their new comments.

### D2 — LOW: the retry log printed the CYCLE-GLOBAL counter, not the per-fold one
`_fold_insight`'s preservation-gate corrective-retry log printed `cyc.preservation_retries`
(a counter that accumulates across **every** fold the cycle attempts) against
`NREM_PRESERVATION_MAX_RETRIES` (the **per-fold** cap) — "attempt 8/2" observed live. The loop
itself was always correctly bounded (`for _ in range(NREM_PRESERVATION_MAX_RETRIES)`); only the
log lied about which attempt it was on.

**Fix:** a local, 1-based `attempt` counter inside `_fold_insight`, incremented alongside
`cyc.preservation_retries` (unchanged, still the cycle-global telemetry total) but logged instead
of it.

**No fact-cycle equivalent exists to fix.** Confirmed by grep: `generate_summary` (the fact-fold's
old LLM+preservation-gate path) was **removed** in the C4 PR (§3.1 replaced it with a
zero/low-inference Zettelkasten index — no LLM call, no preservation gate, no corrective retry).
There is exactly one `summary_preserves` call site left in the whole file, inside `_fold_insight`.

### D3 — MEDIUM: every insight logged/stored `entity:""`
`_find_fresh_insight_clusters` hardcoded `"entity": ""` for every cluster, so every insight fold
logged `Folding insight for ''`, stored `entity:""` in both Postgres and Neo4j metadata, and fed
`'distilling a causal chain of judgements around ''.'` into the LLM synthesis prompt itself.

**Traced every reader before changing anything** (write-side rule: the read side usually carries
the defect one level up):
- **Fold identity (dead-letter key) never used `entity`** — `_dead_lettered`'s dead-letter *key* is
  `_judgement_fold_identity(judgement_ids, types)`, computed from the cluster's own member ids;
  `entity` is used only to build the human-readable `label` for logging (`decision:882`'s
  fold-key/display-label split — confirmed unaffected by this change).
- **`write_insight_summary` is always-INSERT, no `ON CONFLICT`** for `kind='insight'` (migration
  009 exempts insight rows from the `(entity, project, domain, level)` unique index) — so there is
  **no upsert key `entity` could be part of**, proven from the code, not assumed.
- **Every Postgres reader of insight rows filters on `kind='insight'` + `source_pg_ids`, never on
  `entity`**: `fetch_refold_insights`, `fetch_active_insight_rows`, `append_insight_references`,
  `fetch_active_thematic_summary_id` (thematic rows only, excludes `kind='insight'` explicitly).
- **`coordinator.py`'s two readers of the field** (`/memory/status/{pg_id}`'s `consolidated_into`,
  `/memory/status/insight:{id}`'s `entity` key) are pure pass-through display fields — no filter,
  no join key.
- I1 ("no gate predicate reads an entity name" — `Dreaming_Cycle_Plan_to_v2.md` §2.6) is about
  **gating**, never about this string's value; nothing gates on it before or after this change.

No reader depended on the empty string. **Fix:** `entity` is now `f"{project}/{section or
SECTION_NONE}"`, matching the fact cycle's own `label = f"domain:{project}/{section or
SECTION_NONE}"` convention. Multiple components from the same (project, domain) group legitimately
share this label — it is a display value, never a key.

### D4 — MEDIUM: the corrective-retry instruction was stricter than the gate it serves
`corrective_block`'s retry instruction told the LLM each dropped anchor fragment must appear as
**one EXACT, character-for-character substring** — same spelling, hyphenation, punctuation, as one
phrase. `summary_preserves` (the actual gate) checks **token-level** containment: every
whitespace-separated **word** of the fragment must appear **somewhere** in the text, independently,
in any order, not required to stay adjacent (`all(tok in text for tok in anchor.lower().split())`).
The instruction was strictly harder than the check, forcing the LLM to weave constructed multi-word
fragments (`preservation_anchor`'s output — e.g. a decision's longest word plus its first
significant title words) in verbatim as one phrase, often ungrammatical word-salad.

**Fix:** rewrote the instruction text only, to state the real per-word requirement. Did **not**
touch: `summary_preserves`' semantics (still token-level, still the same coverage math); the hard/
zero-tolerance rule for required (judgement) anchors; `preservation_anchor`'s fragment
*construction* (explicitly flagged in the brief as a separate, out-of-scope design question — not
"improved" here).

### O1 — refold_ledger telemetry breakdown
New `coordinator.py` method `_refold_ledger_telemetry()`, wired into `GET /memory/telemetry` as
`snap["refold_ledger"]`. Two breakdowns over the ledger's own columns (confirmed live — see
"Live verification" below):

- **`by_status_reason`** — `[{"status", "closed_reason", "count"}, ...]`, grouped straight off
  `refold_ledger`'s `(status, closed_reason)`. Distinguishes `dropped/below_density` and
  `dropped/out_of_scan` (I7, `decision:1121` — a candidate the cycle scanned and correctly did
  **not** gate this pass) from a genuinely `open` row, which is what an actual stall looks like.
- **`by_trigger_kind`** — `{trigger_kind: count}`. `trigger_kind='technical_docs'` means a
  superseded fact or reversed decision triggered this row **directly**; `'community_summaries'`
  means a **retired summary's own retirement cascaded** to this row (C3's cascading/lineage
  supersession — one summary's retirement raising another).

**A judgement call on wording, flagged for the reviewer rather than silently decided:** the brief
asked for a breakdown "by trigger source — outbox-triggered vs lineage-triggered rows." There is no
literal "outbox" column on `refold_ledger` (that table is `run_lineage_invalidation_pass`'s own
ledger, distinct from `neo4j_outbox`). I read this as asking for `trigger_kind` — its two values are
exactly "the record itself was the trigger" (`technical_docs`, which normally enters via the
outbox) vs "a cascade from another summary's retirement was the trigger" (`community_summaries`,
the lineage mechanism) — which satisfies "use the ledger's own columns" literally. If the intended
meaning was something else, the fix is a one-line rename of the output key, not a new query.

### O2 — insight-kind reconciliation read
Same method, `insight_reconciliation_stuck` key. Per I17/`decision:1181`: an insight-kind
`refold_ledger` row is an **attribution row**, never a clock entry — nothing in this codebase reads
insight-kind rows for due-ness (the insight re-fold trigger is the graph's own `consolidated` clear,
not this ledger). `run_lineage_invalidation_pass`'s Neo4j half (clearing `consolidated=false` on the
retired insight's judgement nodes, **after** the Postgres commit, best-effort) can silently fail with
no other visibility — this read is that visibility.

**Implementation:** (1) Postgres — `DISTINCT pg_id` of `refold_ledger` rows where
`status='open' AND summary_kind='insight'`. (2) If non-empty, one Neo4j `UNWIND`/`MATCH` for
`(Decision OR Retrospective)` nodes among those pg_ids still reading `consolidated = true` — meaning
the graph-side clear never landed, so G3 (`insight_gate.py`'s freshness check) can never re-gate
that insight. (3) A final Postgres count of `refold_ledger` **rows** (not distinct pg_ids — a pg_id
can carry more than one open row) scoped to exactly those stuck pg_ids. Currently reads `0` live —
the open-insight-kind-row population is empty right now (no C3 cascade has fired against an insight
yet), which is the honest current state, not a defect; the query and path are exercised and correct.

## Files changed
```
shared-memory/scripts/consolidation_loop.py   D1 (both cycles), D2, D3, D4
shared-memory/scripts/coordinator.py          D1 (surfacing), O1, O2
tests/test_insight_consolidation.py           D1 (insight), D3
tests/test_nrem_confidence.py                 D1 (fact), D2, D3 (extra() shape), D4
tests/test_consolidation_signal.py            D1 (coordinator surfacing)
tests/test_coordinator.py                     O1, O2
```
No migrations, no schema changes, no version/CHANGELOG/sync_skills.sh touches, no Documentation/
schema.md changes (the `consolidation_runs.extra` JSONB column is already documented as a free-form
bag — `preservation_retries`/`truncation_failures`/`fold_dead_letter` etc. are not individually
enumerated there either, so `dead_lettered_clusters` joining them does not create new drift).

## Tests

**Full suite** (from worktree root):
```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -q
```
→ **1310 passed** (base was 1304; +6 new tests, all green).

**Isolation** — every changed/new-test file run alone:
```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy \
  pytest tests/test_insight_consolidation.py -q   # 49 passed
  pytest tests/test_nrem_confidence.py -q          # 39 passed
  pytest tests/test_consolidation_signal.py -q     # 21 passed
  pytest tests/test_coordinator.py -q              # 93 passed
```
All four green in isolation, matching their full-suite verdicts.

### Mutation checks — one per fix, applied to the live file, run, confirmed the exact test died, reverted, `git diff` confirmed byte-identical before the next

| Fix | Mutation applied | Test that died |
|---|---|---|
| D1 (fact cycle) | `rec.eligible_clusters = len(eligible_work_items)` → `len(work_items)` (pre-filter count restored) | `test_fact_cycle_dead_lettered_cluster_excluded_from_eligible_census` — `eligible_clusters` read 2, not 1 |
| D1 (insight cycle) | Dropped the `clusters = eligible_clusters` reassignment (post-partition list never takes effect) | `test_run_insight_cycle_dead_lettered_cluster_excluded_from_eligible_census` — `await_count` was 2, not 1 (the fold loop ALSO regressed, since it no longer re-checks dead-letter status once the census filters) |
| D1 (coordinator surfacing) | Hardcoded `"dead_lettered_clusters": None` in `_compute_consolidation_health`'s output dict | `test_dead_lettered_clusters_surfaced_per_cycle_type` — read `None`, not `2` |
| D2 | Logged `cyc.preservation_retries` instead of the new per-fold `attempt` counter | `test_insight_preservation_retry_log_uses_per_fold_attempt_number` — fold 2's own first retry logged "attempt 2/2" instead of "attempt 1/2" |
| D3 | `"entity": f"{project}/{section or SECTION_NONE}"` → `"entity": ""` | `test_fresh_insight_clusters_returns_shape_for_a_gating_group` — read `''`, not `'proj/dom'` |
| D4 | Restored the old "EXACT, literal, character-for-character substring" wording | `test_corrective_block_demands_per_word_verbatim_not_whole_phrase` AND `test_generate_insight_corrective_paragraph_names_dropped_anchors` — both died |
| O1 | `"by_status_reason": []` hardcoded | `test_refold_ledger_telemetry_breakdowns_with_no_open_insight_rows` |
| O2 | Final PG count query scoped to the full open `pg_ids` list instead of the graph-filtered `stuck_ids` | `test_refold_ledger_telemetry_o2_counts_only_rows_still_consolidated_in_graph` — `fetchval_params` was `[10,20,30]`, not `[10,30]` |

Every mutation was hand-applied via a targeted Python string-replace against the working tree
(never a backup-and-restore across the whole file — each mutation touched exactly the one line
under test), the targeted test re-run to confirm the EXACT predicted failure, then reverted and
`git diff --stat` confirmed empty before moving to the next.

## Live verification (read-only, no writes) — every new/changed SQL and Cypher string, run verbatim

Credentials: `shared-memory/.env` in the sibling non-worktree checkout
(`~/claude-labs/projects/shared-memory-GitHub/shared-memory/.env`, gitignored, read-only — this
worktree has no `.env` of its own). Postgres via `asyncpg` (matching the coordinator's own driver,
so `$1`-style placeholders match reality exactly, not psycopg2's `%s`); Neo4j via the `neo4j` driver
at `bolt://localhost:7687`, `auth=("neo4j", NEO4J_PASSWORD)` (matching `coordinator.py`'s own
`NEO4J_AUTH`).

**`refold_ledger` schema, confirmed against the live table** (columns exactly match
`Documentation/schema.md`'s table): `id, pg_id, summary_id, summary_kind, trigger_kind, trigger_id,
status, closed_at, closed_reason, created_at`.

**O1's two breakdown queries**, run verbatim:
```sql
SELECT status, closed_reason, count(*) AS n FROM refold_ledger GROUP BY status, closed_reason;
-- → [('refolded','constituent_folded',6), ('dropped','out_of_scan',37)]
SELECT trigger_kind, count(*) AS n FROM refold_ledger GROUP BY trigger_kind;
-- → [('technical_docs', 43)]
```

**O2's three-step read**, run verbatim:
```sql
SELECT DISTINCT pg_id FROM refold_ledger WHERE status = 'open' AND summary_kind = 'insight';
-- → [] (0 rows — no open insight-kind row exists on the live corpus right now)
SELECT count(*) FROM refold_ledger WHERE status = 'open' AND summary_kind = 'insight'
  AND pg_id = ANY($1::bigint[]);  -- exercised with dummy ids [1,2,3] → 0
```
The Neo4j half (`UNWIND $ids AS pid MATCH (d) WHERE (d:Decision OR d:Retrospective) AND
d.pg_id = pid AND d.consolidated = true RETURN collect(DISTINCT pid) AS stuck_ids`) was run with
dummy ids `[1, 2, 3]` (since live has no open insight-kind rows to seed real ids from) and returned
`stuck_ids=[]` — proves the Cypher syntax/shape is valid against the live graph; the real population
it will act on is currently empty.

**D1's modified `_compute_consolidation_health` roll-up query** — the full query with its new
`dead_lettered_clusters` CTE column, run verbatim via `asyncpg.connect(...).fetch(qtext, 600)`
(the same `CONSOLIDATION_ORPHAN_TIMEOUT_SEC` value the coordinator passes):
```
fact_consolidation: {..., 'eligible_clusters': 2, 'eligible_oldest_age': 2193,
                      'dead_lettered_clusters': None, ...}
insight:            {..., 'eligible_clusters': 7, 'eligible_oldest_age': 3649990,
                      'dead_lettered_clusters': None, ...}
```
`dead_lettered_clusters` reads `None` for both cycle types — expected and correct: no
`consolidation_runs` row yet carries the new `extra.dead_lettered_clusters` key, because the fix
has not been deployed to the running gateway (this worktree never restarts or writes to the live
system, per the builder's remit). It will start populating on the daemon's first pass after this
branch is merged and the gateway is restarted (the merger/reviewer's job, not this builder's).

No live writes were made anywhere in this verification — every statement above is a `SELECT` (or a
read-only `MATCH`, no `SET`/`CREATE`/`MERGE`).

## Group obligations cleared (CLAUDE.md "Change groups")

- **Group 3 (daemon/observability)** — the group this whole task lives in, and the one CLAUDE.md
  names as having **no mechanical tie**. Explicitly checked here: D1's new `dead_lettered_clusters`
  key is verified as a genuinely NEW name (not a repurposed old one — `_CycleRec.extra()`'s existing
  keys are untouched), and its absence-vs-zero distinction is preserved end to end (daemon `None`
  default only once a census runs → coordinator `None` when no row carries the key → tests assert
  both states explicitly). O1/O2's `snap["refold_ledger"]` key is additive to `/memory/telemetry`'s
  existing JSON shape — no existing key renamed, moved, or reinterpreted.
- **Group 4 (storage/schema)** — touched **read-only**. No migration, no new table/column, no
  `schema_init.sql` change. `refold_ledger`'s existing columns are read as-is.
- **Group 1/2/5** — not applicable; no client-surface, capture-surface, or install-surface files
  were touched.

## Monitor contract — new keys, one-line meaning each (owed to shared-memory-monitor)

`GET /memory/telemetry`'s `consolidation.<cycle_type>` block gains:
- **`dead_lettered_clusters`** (int or `null`) — clusters this cycle type's latest gate census
  excluded because `NREM_FOLD_FAIL_CAP` dead-lettered them; `null` means no census has recorded this
  yet (pre-deploy / fresh install), never "zero dead-lettered."

`GET /memory/telemetry` gains a new top-level section, `refold_ledger`:
- **`refold_ledger.by_status_reason`** — `[{status, closed_reason, count}]`, every
  `(status, closed_reason)` pair on the ledger. `closed_reason` is `null` for open rows.
- **`refold_ledger.by_trigger_kind`** — `{trigger_kind: count}`; `technical_docs` = a superseded
  fact/reversed decision triggered the row directly, `community_summaries` = a cascaded retirement
  (C3's lineage mechanism).
- **`refold_ledger.insight_reconciliation_stuck`** (int) — open insight-kind ledger rows whose
  judgement node still reads `consolidated=true` in Neo4j, meaning the graph-side clear that would
  let the insight re-gate never landed. Non-zero is worth alerting on; zero is the healthy steady
  state (also the entire current population).

None of these change the meaning of any existing key. `snap["consolidation"]` and
`snap["refold_ledger"]` are independent top-level keys in the telemetry payload.

## Escalations

None. No genuine design ambiguity blocked implementation. The one interpretive call (O1's
"outbox-triggered vs lineage-triggered" wording vs the literal `trigger_kind` column values) is
documented above under O1, with the reasoning and the one-line fix if the reviewer disagrees — it
did not block shipping because both readings are the same underlying data, differing only in the
output key's English label.

## Commands run (exact, for re-verification)

```bash
# Full suite
cd ~/claude-labs/worktrees/nrem-truth-observability
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -q

# Live SQL/Cypher verification (read-only)
uv run --with asyncpg --with neo4j --with python-dotenv python - <<'EOF'
import os, asyncio, asyncpg
from dotenv import load_dotenv
load_dotenv(os.path.expanduser("~/claude-labs/projects/shared-memory-GitHub/shared-memory/.env"))
async def main():
    conn = await asyncpg.connect(f"postgresql://postgres:{os.environ['PG_PASSWORD']}@localhost:5432/agent_data")
    print(await conn.fetch("SELECT status, closed_reason, count(*) AS n FROM refold_ledger GROUP BY status, closed_reason"))
    await conn.close()
asyncio.run(main())
EOF
```

## What to do next (the reviewer/merger's job, not this builder's)
1. Review the diff; rule on O1's `trigger_kind`-naming interpretive call if it needs a different key
   name.
2. Merge via PR (never direct to `main`).
3. Bump version `+0.0.1`, CHANGELOG entry, tag + GitHub Release, `sync_skills.sh` if the capture
   surface changed (it did not — this is a pure daemon/coordinator/observability change; no
   client-facing skill file was touched).
4. Restart `hive-mind-gateway.service`, verify `/health` and `/memory/telemetry` show the new
   `dead_lettered_clusters`/`refold_ledger` keys on the next consolidation pass.
5. Notify `shared-memory-monitor` of the new `refold_ledger` telemetry section (Monitor contract
   above) so the dashboard can render it.
