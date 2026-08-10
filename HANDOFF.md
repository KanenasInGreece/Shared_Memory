# HANDOFF — C1: fact gate rearchitecture (Dreaming Cycle Plan to v2)

**Task:** C1 — fact gate rearchitecture. **Branch:** `feat/nrem-fact-gate-project-domain`,
based on `main` @ `d11ced0` (v0.8.62, after rebasing onto B2's merged I7 fix — see
*Rebase* below). **Status: DONE**, ready for review. Full suite green (1249 passed).

Plan reference: `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §2.1 (FACT GATE),
§2.6 (I1/I2/I8), §4.2 (NREM Path A).

---

## What is done

1. **Discovery replaced.** `_find_anchored_clusters` (entity-hub/MENTIONS Cypher, shared
   by the event cycle + ledger sweep) and `run_global_sweep`'s near-duplicate inline
   entity Cypher are both **deleted**. In their place: **one** new method,
   `_find_grounded_fact_groups` (`consolidation_loop.py`), used by **all three** fact
   schedulers (`run_consolidation_cycle`, `run_ledger_sweep`, `run_global_sweep`).

   Cypher (verbatim, this is the actual shipped query):
   ```
   MATCH (j) WHERE j:Decision OR j:Retrospective
   MATCH (j)-[:GROUNDED_IN]->(f:Fact)
   WHERE coalesce(f.superseded, false) = false
   MATCH (f)-[:DOMAIN_OF]->(dom:Domain)-[:PROJECT_OF]->(proj:Project)
   WITH DISTINCT f, proj.name AS project, dom.name AS domain
   RETURN f.pg_id AS pg_id, coalesce(f.rem_summary, f.content) AS content, project, domain
   ```
   This is the SPINE chain the plan names in §2.1/§4.2. It is **always an unrestricted
   full scan** — no `ids` parameter — because a group's density must be judged on its
   WHOLE current membership every re-fold (a `community_summaries` row is one upserted
   key per (project, domain), not an append log). The corpus this scans is small
   (order 10²), so this is cheap; the old per-call `ids` restriction is gone along with
   the distinction it existed to draw between the event/ledger/global entry points.

2. **`_consolidate_clusters` rewritten.** Takes the flat `rows` `_find_grounded_fact_groups`
   returns (`{"pg_id", "content", "project", "domain"}`), aggregates them into
   `project_map`/`domains_map`/`registered_sections` **directly from the graph rows**
   (no second Postgres registry lookup — a DOMAIN_OF/PROJECT_OF edge only exists for a
   registered section, so edge presence already proves registration), and calls
   `eligible_domain_level_clusters` — **the only partitioner it calls now** — exactly
   once. No more entity-level work items, no more project-only-level work items. Every
   fold is `level=LEVEL_DOMAIN`, `entity=""`, `aliases=[]` (constants, not per-item
   fields — set once before the loop).

   `gate`/`edge_stats` params dropped entirely: the v2 fact gate never traverses
   MENTIONS/entity-link edges, so there is nothing for `relation_confidence` calibration
   to report for this run type. A clean `fact_consolidation` run's `extra()` is now
   correctly `None` (matches `_CycleRec.extra()`'s own documented contract — "None when
   the cycle never fetched a gate and nothing was counted"), not a dict with
   zeroed-out calibration fields. **This is a `consolidation_runs.extra` shape change for
   the `fact_consolidation` run type** — flagging per the Group 3 rule: a monitor reading
   `edges_awaiting_calibration`/`machine_edges_consumed`/`calibration` off a
   `fact_consolidation` row will now see them absent on a clean run (present, zeroed, only
   when a fold actually failed/retried). `_fold_insight` (the INSIGHT path) is
   **completely unaffected** — it still fetches and reports calibration normally.

3. **`density_threshold` recalibrated 5 → 3** in both `shared-memory/scripts/ontology.py`
   (`OntologyConfig.density_threshold`) and `ontology.yaml` (`consolidation.density_threshold`),
   kept in step, both with a comment explaining why (population shape changed).

4. **Membership = grounded, non-superseded.** `_find_grounded_fact_groups`'s `coalesce(f.superseded,
   false) = false` is the only exclusion; `f.consolidated` is **never read** anywhere in the
   new discovery or partition path — confirmed by inspection and by the mutation-checked
   test suite (a fact already belonging to an active summary still counts toward the next
   re-fold of its group, per §2.1).

## What is explicitly NOT done (by design, per the brief)

- **No migration.** Confirmed unnecessary: `community_summaries_axis_level_unique`
  already `COALESCE`s `entity` to `''`, so entity-free `level='domain'` rows already
  work — this was already true before C1 (the domain level existed since PR7/v0.8.58);
  C1 only stopped a SECOND level (entity) from being produced alongside it.
- **No write to existing rows.** Verified live (see *Live verification* below): the 45
  active `level='entity'` summaries and their 8 superseded siblings are untouched —
  nothing in this branch reads, marks, or deletes them. They will stop being **produced**
  (no future entity-level fold can create a new one) but nothing here retires the
  existing ones. Xenofon's ruling stands: **left as archive.**

## ⛔ ESCALATED — not resolved, needs Opus (or a coordinator.py owner)

**`coordinator.py`'s `_nrem_cycle_counts` (line ~6062, feeds `GET /memory/telemetry`'s
`"nrem"` key, also read at line ~5413) and `_count_domain_cycles` (line ~1054, dead except
for its own test) both still `from consolidation_loop import count_entity_level_cycles,
count_domain_level_cycles, NREM_DOMAIN_THRESHOLD` and run them against a
Postgres-metadata-derived population** — the PRE-v2 two-level telemetry split
(`entity_level_cycles` / `domain_level_cycles`). The brief instructed removing these
"telemetry twins" as dead code; **I did not**, because doing so breaks `coordinator.py`
(which I was told not to touch — Track B/other builders own it this cycle) and its own
test (`tests/test_domain_clusters.py`). Confirmed this is still true after rebasing onto
`d11ced0` (B2's I7 fix, merged as v0.8.62) — that commit removed `_nrem_cycle_counts`
from the **stall-verdict** path but left the **telemetry** method (`/memory/telemetry`'s
`nrem` key) intact, still importing the same four now-orphaned names.

**What I did instead:** left `eligible_entity_level_clusters`, `eligible_domain_clusters`,
`count_entity_level_cycles`, `count_domain_level_cycles`, `NREM_DOMAIN_THRESHOLD`, and
`LEVEL_ENTITY` **fully intact and unchanged** in `consolidation_loop.py`, each with a new
block comment marking it ⛔ LEGACY / ORPHANED FROM THE FOLD, explaining exactly which
`coordinator.py` line still needs it and why. The **fold itself** (what actually runs on
the daemon) never calls any of them any more — this is a functional, not cosmetic,
compliance with §2.1's "no entity level, no project level."

**Needed to actually close this out:** a `coordinator.py` change (out of my scope) that
either (a) rewrites `_nrem_cycle_counts` to report the v2 (project, domain)-only gate
(mirroring what `_find_grounded_fact_groups` + `eligible_domain_level_clusters` now do),
or (b) retires the `entity_level_cycles`/`domain_level_cycles` split in the
`/memory/telemetry` response in favour of one `fact_cycles` number, with the monitor
told about the shape change. Either way `count_entity_level_cycles`,
`eligible_entity_level_clusters`, `eligible_domain_clusters` and `_count_domain_cycles`
(plus its test) become truly deletable at that point, not before.

## Invariants — mutation-checked

Method: for each invariant, wrote a test against the REAL code (Cypher text captured via
a fake Neo4j session, or the real `eligible_domain_level_clusters` function), then
by-hand mutated `consolidation_loop.py` to violate it, ran `tests/test_v2_fact_gate.py`
(+ `tests/test_nrem_axis_levels.py` for I8, since some of its pre-existing tests also
exercise the same guard), confirmed the intended test(s) died and nothing else did,
reverted, and diffed the file against a pre-mutation backup to confirm an exact restore.
Each is in **`tests/test_v2_fact_gate.py`**.

| Inv. | Test(s) | Mutation performed | What died |
|---|---|---|---|
| **I1** | `test_i1_discovery_query_never_touches_entity_or_mentions`, `test_i1_partitioner_signature_carries_no_entity_parameter` | Added `MATCH (f)-[:MENTIONS]->(e:Entity)` to the discovery Cypher | `test_i1_discovery_query_never_touches_entity_or_mentions` only |
| **I2** | `test_i2_discovery_query_never_counts_projects`, `test_i2_partitioner_never_counts_distinct_projects` | Appended `, count(DISTINCT proj) AS project_count` to the discovery Cypher's RETURN | `test_i2_discovery_query_never_counts_projects` only |
| **I8** | `test_i8_discovery_query_requires_the_domain_of_project_of_chain`, `test_i8_project_alone_never_forms_a_group`, `test_i8_project_present_but_domain_unregistered_never_forms_a_group`, `test_i8_key_is_the_project_domain_tuple_not_project_alone` | (a) inverted `if (project, section) not in registered: continue` → `if ... in registered: continue` in `eligible_domain_level_clusters` | `test_i8_project_present_but_domain_unregistered_never_forms_a_group`, `test_i8_key_is_the_project_domain_tuple_not_project_alone`, **plus 3 pre-existing tests in `test_nrem_axis_levels.py`** (`test_domain_level_folds_across_entities_without_entity_key`, `test_p16_unregistered_section_never_forms_domain_level`, `test_count_domain_level_matches_partitioner`) — broader kill than intended, which is stronger evidence the invariant is covered, not weaker |
| **I8** (cont.) | `test_i8_project_alone_never_forms_a_group` | (b) reintroduced the forbidden P15 fallback: `if not sections: bucket under (project, SECTION_NONE)` | `test_i8_project_alone_never_forms_a_group` **and** the pre-existing `test_p16_blank_section_never_forms_domain_level` |

Every mutation was reverted; `diff` against a pre-mutation copy of `consolidation_loop.py`
confirmed byte-identical restoration before the final commit.

**I4** (freshness on judgements) is explicitly **not mine** — C2's job (insight gate),
per the brief.

## Live verification (read-only, against the running Postgres + Neo4j — never written to)

Ran the **actual shipped code** (`ConsolidationDaemon._find_grounded_fact_groups`,
imported from this branch, executed against the live Neo4j) — not a paraphrase:

```
total rows: 21
('shared-memory-GitHub', 'architecture'): 13 grounded facts -> GATES (threshold=3)
('shared-memory-GitHub', 'infrastructure'): 5 grounded facts -> GATES (threshold=3)
('shared-memory-GitHub', 'schema'): 1 grounded facts -> below threshold (threshold=3)
('shared-memory-GitHub', 'delivery'): 1 grounded facts -> below threshold (threshold=3)
<one other registered (project, domain) pair — private project name, omitted per the
 never-leak-project-names rule>: 1 grounded fact -> below threshold (threshold=3)
```
(This file is committed, so the private project's real name is deliberately not
reproduced here — only its shape, per `feedback_never_leak_project_names`.)

Then ran the **full pipeline** (discovery + the real `eligible_domain_level_clusters`
partitioner, same aggregation `_consolidate_clusters` performs) and got exactly 2 work
items:
```
work_items produced: 2
  (shared-memory-GitHub, architecture): 13 facts, pg_ids=[1064, 1065, 1079, 1085, 1091, 1093, 1094, 1097, 1108, 1113, 1118, 1119, 1149]
  (shared-memory-GitHub, infrastructure): 5 facts, pg_ids=[1100, 1103, 1105, 1112, 1120]
```
This is an **exact match** to the plan's §8 expected result ("Exactly two groups gate
corpus-wide today — `shared-memory-GitHub / architecture` (13 grounded facts) and
`shared-memory-GitHub / infrastructure` (5)"). Per the brief's own ruling, this confirms
correctness — a larger result would have meant the project level or the unregistered-domain
path had been re-admitted; it wasn't.

Also confirmed the 45-orphaned-summaries baseline is exactly as the brief states and
**untouched**:
```sql
SELECT COALESCE(metadata->>'level','entity'), COALESCE(metadata->>'kind','thematic'),
       NOT superseded, count(*)
FROM community_summaries GROUP BY 1,2,3 ORDER BY 1,2,3;
-- ('domain', 'thematic', True, 1)
-- ('entity', 'thematic', False, 8)
-- ('entity', 'thematic', True, 45)
```

**Not reproducible from the repository:** the two live query results above (they read
the running Postgres/Neo4j state on this machine on 2026-08-10); the numbers will drift
as the corpus grows, but the SHAPE (exactly two gating groups today) is what the plan's
§8 measured too, independently, earlier the same day.

## Tests

- Full suite: `1249 passed` (baseline on `main` after rebase was `1241`+; I added 8 new
  tests in `tests/test_v2_fact_gate.py`).
- **Changed, not just extended** (say which and why, per the brief):
  - `tests/test_nrem_confidence.py` — removed 4 tests
    (`test_anchored_finder_edge_predicate_and_params_uncalibrated`,
    `test_anchored_finder_calibrated_params_pass_through`,
    `test_anchored_finder_excluded_count_leaves_log_line`,
    `test_global_sweep_query_carries_edge_predicate`) that asserted on the deleted
    entity-hub calibration Cypher. Updated the `_CLUSTER` → `_ROWS` fixture and
    `_thematic_conn_script` (one fewer Postgres round-trip: the old "registered
    sections" query is gone) across 6 `_consolidate_clusters(...)` call sites; updated
    `test_preservation_double_failure_requeues_and_blocks_tier3`'s calibration
    assertions (now 0/0/absent, not 3/1/dict) and
    `test_mock_llm_thematic_fold_passes_preservation_gate`'s `extra` assertion (now
    `None`, matching `_CycleRec.extra()`'s own contract for a clean run with no gate
    fetched).
  - `tests/test_nrem_eligibility.py` — `_cycle_daemon` now patches
    `_find_grounded_fact_groups` (param-less) instead of the removed
    `_find_anchored_clusters`; the two tests that used to assert WHICH ids reached the
    cluster finder (`test_cycle_anchors_on_the_durable_backlog`,
    `test_cycle_unions_requeued_ids_with_the_ledger`) are renamed and repurposed to
    assert WHETHER discovery ran, since v2 discovery takes no ids at all.
  - `tests/test_rem_loop.py::test_nrem_cluster_query_requires_rem_processed` — replaced
    with `test_nrem_fact_gate_is_grounded_non_superseded_not_rem_processed`, asserting
    the OPPOSITE: `_find_grounded_fact_groups` must NOT gate on `rem_processed` (§2.1
    membership is grounded + non-superseded only; REM's `rem_summary` is optional prep,
    `coalesce`d with raw content, never a precondition).
- `tests/test_domain_clusters.py` — **untouched, still passes as-is**, because
  `eligible_domain_clusters`/`coordinator._count_domain_cycles` (what it tests) are
  unchanged legacy code, kept for the escalated coordinator.py dependency above.

## Rebase

Started on `main` @ `0e43d74` (v0.8.61). Mid-task, `main` advanced to `d11ced0`
("fix(nrem): a cycle that never gated is not a stall — invariant I7", v0.8.62 — B2's
task, merged #222). Fetched and `git rebase origin/main` — **clean, no conflicts**
(disjoint files, per the plan's own prediction: builders never touch the version/
CHANGELOG, so a stalled branch rebases cleanly). Re-ran the full suite after rebase:
still 1249 passed. This is also what fixed a transient `test_capture_surface_documented.py`
failure I saw before rebasing (SKILL.md's worked examples were re-synced to 0.8.62 in
that commit) — pre-existing drift, not something this branch caused or needed to fix.

## Judgement calls not visible in the diff

1. **Discovery is unscoped (no `ids` parameter) on all three call sites**, including the
   event-driven cycle and the ledger sweep, which previously restricted the entity Cypher
   to the triggering pg_ids. This is a deliberate reading of §2.1's membership rule
   ("the grounded, non-superseded facts in the group" — the WHOLE group, always) — a
   scoped discovery would either miss existing group members or require a two-step
   "which groups did this touch, then expand" query I judged unnecessary at this corpus
   size. `ids_to_process`/backlog-size checks remain as the cheap in-memory gate for
   "is it worth running discovery at all."
2. **Reversed decisions are NOT filtered out of the fact gate's GROUNDED_IN walk.** §2.2a
   ("reversed decisions excluded... from the reach, the components, the gate and the
   payload") reads, on its numbering and its own "reached judgement set" language, as
   scoped to the INSIGHT gate (§2.2/§2.3, C2's job), not the FACT gate (§2.1). I did not
   add a `coalesce(j.superseded, false) = false` filter on the judgement side of
   `_find_grounded_fact_groups`'s GROUNDED_IN match. If C2's build (or review) concludes
   I10 should also gate fact-level membership, that is a one-line addition to this
   query, but I did not make that call unilaterally since it's outside §2.1's literal
   text.
3. **`registered_sections` is derived from the SAME rows `_find_grounded_fact_groups`
   returns**, not from a fresh `project_domains`/`projects` join. I judged this safe
   (and preferable — one source of truth, one fewer round-trip) because a DOMAIN_OF/
   PROJECT_OF edge is only ever written for a name the registry could resolve at write
   time; a registry row deleted AFTER the edge was written is an existing, unrelated risk
   this change neither creates nor worsens.
4. **`LEVEL_ENTITY`, `SECTION_NONE`, `NREM_DOMAIN_THRESHOLD` kept as module constants**,
   not deleted, because they are still needed generically (legacy-row reconciliation,
   display fallbacks, the escalated coordinator.py dependency) even though no NEW fold
   ever produces `level='entity'` again.

## Next (C2)

`Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §2.2–§2.5, §4.2 Path B. Start from
`insight_gate.py` (`insight_cluster_cypher`) — per the brief it "already implements
almost exactly" what needs generalising to the walk/components/identity rules. The
`ontology.py`/`ontology.yaml` I touched are otherwise untouched by C1 beyond
`density_threshold` — no other conflict expected. `consolidation_loop.py` is now
~250 lines shorter in the fact-gate region; re-read `_consolidate_clusters` and
`_find_grounded_fact_groups` before touching either, they are the new shape C2's
insight path should mirror for consistency (single discovery method, no gate/edge_stats
threading where calibration doesn't apply).
