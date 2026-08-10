# HANDOFF — C1 + C1b: fact gate rearchitecture + telemetry coupling
# (Dreaming Cycle Plan to v2)

**Task:** C1 (fact gate rearchitecture) + C1b (telemetry coupling, assigned by Opus after
`coordinator.py` was unblocked mid-task). **Branch:** `feat/nrem-fact-gate-project-domain`,
based on `main` @ `65debc0` (v0.8.63, after two rebases — see *Rebases* below).
**Status: DONE**, ready for review. Full suite green (**1236 passed**).

Plan reference: `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §2.1 (FACT GATE),
§2.6 (I1/I2/I8), §4.2 (NREM Path A). CLAUDE.md's Group 3 rule (metric meaning can invert
while its name stays — rename it) governs C1b.

---

## C1 — what is done

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
   (order 10²), so this is cheap.

2. **`_consolidate_clusters` rewritten.** Takes the flat `rows` `_find_grounded_fact_groups`
   returns (`{"pg_id", "content", "project", "domain"}`), aggregates them into
   `project_map`/`domains_map`/`registered_sections` **directly from the graph rows**
   (no Postgres registry lookup — a DOMAIN_OF/PROJECT_OF edge only exists for a
   registered section, so edge presence already proves registration), and calls
   `eligible_domain_level_clusters` — **the only partitioner it calls** — exactly once.
   No more entity-level work items, no more project-only-level work items. Every fold is
   `level=LEVEL_DOMAIN`, `entity=""`, `aliases=[]` (constants, set once before the loop).

   `gate`/`edge_stats` params dropped entirely: the v2 fact gate never traverses
   MENTIONS/entity-link edges, so there is nothing for `relation_confidence` calibration
   to report for this run type. A clean `fact_consolidation` run's `extra()` is now
   correctly `None` (matches `_CycleRec.extra()`'s own documented contract). **This is a
   `consolidation_runs.extra` shape change for the `fact_consolidation` run type** — a
   monitor reading `edges_awaiting_calibration`/`machine_edges_consumed`/`calibration` off
   a `fact_consolidation` row will now see them absent on a clean run (present, zeroed,
   only when a fold actually failed/retried). `_fold_insight` (the INSIGHT path) is
   **completely unaffected** — it still fetches and reports calibration normally.

3. **`density_threshold` recalibrated 5 → 3** in both `shared-memory/scripts/ontology.py`
   and `ontology.yaml`, kept in step.

4. **Membership = grounded, non-superseded.** `coalesce(f.superseded, false) = false` is
   the only exclusion; `f.consolidated` is **never read** anywhere in the new discovery
   or partition path.

## C1b — what is done (this session, after Opus unblocked `coordinator.py`)

`main` advanced past the parallel builder holding `coordinator.py` while C1 was in
review (now v0.8.63) — the C1 escalation ("kept four legacy names alive solely because
`coordinator.py`'s `_nrem_cycle_counts`/`_count_domain_cycles` still imported them") is
now **closed**, not deferred:

1. **`coordinator._nrem_cycle_counts` rewritten** to mirror `_find_grounded_fact_groups`'s
   own Cypher exactly (same GROUNDED_IN → DOMAIN_OF → PROJECT_OF chain, same
   `proj.name AS project` / `dom.name AS domain` aliasing) and to count via the SAME
   `count_domain_level_cycles` (→ `eligible_domain_level_clusters`) the fold itself calls.
   **No more Postgres `PROJECT_SQL` round-trip for this census at all** — project/domain
   now come straight off the graph edges the fold walks, so the gauge cannot describe a
   different population than the fold even in principle, not just "not today."

2. **`GET /memory/telemetry`'s `"nrem"` response shape changed** — see *Named monitor
   effect* below, mandatory reading for the release notes.

3. **Deleted, now that nothing imports them:**
   - `consolidation_loop.eligible_entity_level_clusters`
   - `consolidation_loop.eligible_domain_clusters` (the project-only wrapper)
   - `consolidation_loop.count_entity_level_cycles`
   - `consolidation_loop.NREM_DOMAIN_THRESHOLD` — see reasoning below
   - `coordinator._count_domain_cycles` (confirmed dead pre-existing — no caller besides
     its own test)
   - `tests/test_domain_clusters.py` (its entire subject matter — the two functions
     above — is gone)

4. **Kept, deliberately, under their existing names:**
   - `consolidation_loop.eligible_domain_level_clusters` / `count_domain_level_cycles` —
     "domain-level" was never a leftover half of a two-level distinction; it names the
     mechanism itself (folds are keyed at (project, domain) granularity — there is no
     other level left to contrast it with). Renaming a name that already says the true
     thing would cost every caller a diff for no clarity gained. **Reasoning stated as
     asked; this is the "state your reasoning either way" answer for that half.**
   - `LEVEL_ENTITY`, `SECTION_NONE` — still needed generically: `fetch_unreconciled`'s
     COALESCE default and `_mark_consolidated_in_graph`'s generic fallback both still
     have to interpret **pre-existing** `level='entity'` rows correctly (the 45 orphaned
     summaries) even though no new fold produces one.

5. **`NREM_DOMAIN_THRESHOLD` vs `ONT.density_threshold` — deleted the former, not kept
   as a second knob.** Reasoning, as asked: `NREM_DOMAIN_THRESHOLD` was a SEPARATE
   env-tunable density knob (`os.environ.get("NREM_DOMAIN_THRESHOLD", str(DENSITY_THRESHOLD))`)
   that C1 had *already* stopped reading in the fold (`_consolidate_clusters` used
   `DENSITY_THRESHOLD` directly) — it survived only inside `_nrem_cycle_counts`'s
   telemetry. Two numbers that are SUPPOSED to track together but are tunable
   independently via two different env vars is exactly the future-drift risk the whole
   C1b task exists to close: an operator setting `NREM_DOMAIN_THRESHOLD` expecting it to
   change the gate would silently do nothing, and nothing would tell them. One knob
   (`ONT.density_threshold` / `DENSITY_THRESHOLD`), read by both the fold and its
   telemetry, cannot drift from itself.

## What is explicitly NOT done (by design)

- **No migration.** `community_summaries_axis_level_unique` already `COALESCE`s `entity`
  to `''`, so entity-free `level='domain'` rows already worked before C1.
- **No write to existing rows.** Verified live again after both rebases and after C1b
  (see *Live verification* below): the 45 active `level='entity'` summaries and their 8
  superseded siblings are untouched. Xenofon's ruling stands: **left as archive.**

## Named monitor effect — mandatory, no mechanical test covers Group 3

`GET /memory/telemetry`'s `"nrem"` key, produced by `_nrem_cycle_counts`, changes shape:

| Field | Before (pre-v2 shape) | After (this branch) |
|---|---|---|
| `fact_cycles` | **VALUE CHANGES** — was `entity_cycles + domain_cycles`, a sum over a level (entity-hub) the daemon could no longer fold since C1 shipped, so this number could report a nonzero backlog the fold structurally could not act on (a live drift, not hypothetical — see *Live verification*, the number is unchanged today only because no entity cluster currently gates, but it WAS reading a stale rule). Now it is directly the (project, domain) FACT GATE census — the actual, only, rule the fold applies. |
| `entity_level_cycles` | present | **REMOVED.** The level it counted no longer exists. |
| `domain_level_cycles` | present | **REMOVED**, merged into `fact_cycles` — there is nothing left to distinguish it from. |
| `decision_cycles` | present | **UNCHANGED** — insight gate, untouched by this branch. |
| `total_cycles` | `fact_cycles + decision_cycles` | **UNCHANGED FORMULA**, new `fact_cycles` meaning flows through. |
| `fact_threshold` | `ONT.density_threshold` (was 5, entity-hub) | **SAME KEY**, new value (3), new meaning (the one gate, not the entity-hub gate). |
| `domain_threshold` | `NREM_DOMAIN_THRESHOLD` | **REMOVED** — no second threshold exists any more. |
| `decision_threshold` | present | **UNCHANGED.** |

**What a monitor operator will see:** a dashboard rendering `entity_level_cycles` or
`domain_level_cycles` (or `domain_threshold`) will show those fields as `undefined`/blank
after this ships — those are gone, not renamed to something a naive rename-map would
catch. A dashboard that only reads `fact_cycles`/`total_cycles`/`fact_threshold`/
`decision_cycles`/`decision_threshold` sees no key error, but **`fact_cycles`'s value
itself is not a continuation of the old series** — it is a different, structurally
smaller-or-equal population (the entity-hub level could never gate MORE candidates than
the union of (project,domain) groups now counted, since every entity-clustered fact
still has to belong to SOME (project,domain) — usually fewer, since untagged-domain
facts that used to count toward an entity cluster never count now). **A monitor
operator watching this number should expect a step change on deploy, not a gradual
drift, and should not read it as an anomaly.** This is exactly the drift CLAUDE.md's
Group 3 rule warns about, closed at the source rather than documented as a caveat.

## Invariants — mutation-checked

Method unchanged from C1: write a test against the REAL code, by-hand mutate
`consolidation_loop.py`/`coordinator.py` to violate it, run the suite, confirm the
intended test(s) died and nothing else did, revert, diff against a pre-mutation backup
to confirm exact restoration. All in **`tests/test_v2_fact_gate.py`** (13 tests total).

| Inv. | Test(s) | Mutation performed | What died |
|---|---|---|---|
| **I1** | `test_i1_discovery_query_never_touches_entity_or_mentions`, `test_i1_partitioner_signature_carries_no_entity_parameter` | Added `MATCH (f)-[:MENTIONS]->(e:Entity)` to the discovery Cypher | `test_i1_discovery_query_never_touches_entity_or_mentions` only |
| **I2** | `test_i2_discovery_query_never_counts_projects`, `test_i2_partitioner_never_counts_distinct_projects` | Appended `, count(DISTINCT proj) AS project_count` to the discovery Cypher's RETURN | `test_i2_discovery_query_never_counts_projects` only |
| **I8** | `test_i8_discovery_query_requires_the_domain_of_project_of_chain`, `test_i8_project_alone_never_forms_a_group`, `test_i8_project_present_but_domain_unregistered_never_forms_a_group`, `test_i8_key_is_the_project_domain_tuple_not_project_alone` | (a) inverted `if (project, section) not in registered: continue` → `if ... in registered: continue` in `eligible_domain_level_clusters` | `test_i8_project_present_but_domain_unregistered_never_forms_a_group`, `test_i8_key_is_the_project_domain_tuple_not_project_alone`, **plus 3 pre-existing tests in `test_nrem_axis_levels.py`** — broader kill than intended, stronger evidence, not weaker |
| **I8** (cont.) | `test_i8_project_alone_never_forms_a_group` | (b) reintroduced the forbidden P15 fallback: `if not sections: bucket under (project, SECTION_NONE)` | `test_i8_project_alone_never_forms_a_group` **and** the pre-existing `test_p16_blank_section_never_forms_domain_level` |
| **C1b — gauge/fold coupling** (unnumbered in the plan; CLAUDE.md Group 3's own rule) | `test_nrem_cycle_counts_reuses_the_folds_own_partitioner` | Replaced the `from consolidation_loop import count_domain_level_cycles` import + call inside `_nrem_cycle_counts` with an inline hand-rolled bucket/threshold reimplementation (functionally near-identical output, structurally a SECOND copy of the rule) | `test_nrem_cycle_counts_reuses_the_folds_own_partitioner` only |

Every mutation was reverted; `diff` against a pre-mutation copy of each file confirmed
byte-identical restoration before the corresponding commit.

**I4** (freshness on judgements) is explicitly **not mine** — C2's job (insight gate).

## Live verification (read-only, against the running Postgres + Neo4j — never written to)

Re-run after **both** rebases and after the C1b telemetry change, per Opus's request.

**1. The actual shipped discovery method**, `ConsolidationDaemon._find_grounded_fact_groups`,
imported from this branch and executed against the live Neo4j:
```
total rows: 21
('shared-memory-GitHub', 'architecture'): 13 grounded facts -> GATES (threshold=3)
('shared-memory-GitHub', 'infrastructure'): 5 grounded facts -> GATES (threshold=3)
('shared-memory-GitHub', 'schema'): 1 grounded facts -> below threshold (threshold=3)
('shared-memory-GitHub', 'delivery'): 1 grounded facts -> below threshold (threshold=3)
<one other registered (project, domain) pair — private project name, omitted per the
 never-leak-project-names rule>: 1 grounded fact -> below threshold (threshold=3)
```

**2. The full fold pipeline** (discovery + the real `eligible_domain_level_clusters`
partitioner, the same aggregation `_consolidate_clusters` performs) — unchanged from
before both rebases:
```
work_items produced: 2
  (shared-memory-GitHub, architecture): 13 facts, pg_ids=[1064, 1065, 1079, 1085, 1091, 1093, 1094, 1097, 1108, 1113, 1118, 1119, 1149]
  (shared-memory-GitHub, infrastructure): 5 facts, pg_ids=[1100, 1103, 1105, 1112, 1120]
```

**3. The actual shipped telemetry method**, `MemoryCoordinator._nrem_cycle_counts`,
imported from this branch and executed against the live Neo4j:
```
live /memory/telemetry 'nrem' shape (via real _nrem_cycle_counts):
  fact_cycles: 2
  decision_cycles: 0
  total_cycles: 2
  fact_threshold: 3
  decision_threshold: 2
```
`fact_cycles == 2` matches the 2 gating groups exactly — **the gauge and the fold agree
because they now share one function**, not because the numbers happen to coincide today.

All three are an **exact match** to the plan's §8 expected result ("Exactly two groups
gate corpus-wide today"). A larger fold result, or a `fact_cycles` that disagreed with
the fold's own work-item count, would both mean something had regressed; neither
happened.

**4. The 45-orphaned-summaries baseline — unchanged, untouched**, confirmed after every
edit in this branch:
```sql
SELECT COALESCE(metadata->>'level','entity'), COALESCE(metadata->>'kind','thematic'),
       NOT superseded, count(*)
FROM community_summaries GROUP BY 1,2,3 ORDER BY 1,2,3;
-- ('domain', 'thematic', True, 1)
-- ('entity', 'thematic', False, 8)
-- ('entity', 'thematic', True, 45)
```

**Not reproducible from the repository:** all four live results above (they read the
running Postgres/Neo4j state on this machine); the numbers will drift as the corpus
grows, but the SHAPE (exactly two gating groups, gauge==fold) is what the plan's §8
measured too, independently.

## Tests

Full suite: **1236 passed** (was 1231 immediately after the second rebase, before C1b's
5 new tests and 5 deleted entity-level tests netted the difference — see below).

**C1 changes** (unchanged from the prior handoff — see git log for detail): 4 tests
removed from `test_nrem_confidence.py` (asserted on the deleted entity-hub Cypher);
`test_nrem_eligibility.py`'s cycle tests repurposed (discovery takes no `ids` now);
`test_rem_loop.py`'s `rem_processed`-requirement test replaced with its deliberate
opposite.

**C1b changes, this session:**
- **`tests/test_domain_clusters.py` deleted** — its entire subject
  (`eligible_domain_clusters` + `coordinator._count_domain_cycles`) no longer exists.
- **`tests/test_nrem_axis_levels.py`** — removed the entity-level test block
  (`test_entity_level_groups_by_project_and_section`,
  `test_p15_domainless_facts_still_fold_on_project`,
  `test_multi_domain_fanout_counts_fact_in_each_section`,
  `test_p2_unresolvable_project_skipped`, `test_count_entity_level_matches_partitioner`)
  and their now-deleted imports; domain-level tests untouched.
- **`tests/test_project_axis.py::test_the_fold_key_query_no_longer_invents_a_bucket`**
  updated — its old assertion pinned the literal Postgres query text
  `"SELECT id, {PROJECT_SQL} AS project"` inside `coordinator.py`, which no longer
  exists (the rewritten `_nrem_cycle_counts` has no Postgres project-resolution query at
  all). Replaced with the Cypher-form equivalent of the SAME naming-trap guard: asserts
  `"proj.name AS project"` is present and `"proj.name AS domain"` /
  `"dom.name AS project"` are absent.
- **`tests/test_v2_fact_gate.py`** — 5 new tests: the mutation-checked gauge/fold
  coupling proof, a Cypher-chain check on `_nrem_cycle_counts`'s actual executed query
  (captured via a fake session, not source-text inspection — see the note below), a
  composition test feeding the fake graph session real row shapes and checking
  `fact_cycles` against an independent `eligible_domain_level_clusters` call on the same
  data, a return-shape pin, and a `hasattr`-based confirmation the five deleted names
  are actually gone.

**A test-writing mistake caught and fixed during this session, worth recording:** my
first draft of the two Cypher-chain/shape tests used `inspect.getsource()` and checked
for evaluated strings like `-[:GROUNDED_IN]->` in the SOURCE TEXT — but `getsource`
returns the literal Python (`f"...{ONT.grounded_in}..."`, unevaluated), so those
assertions could never pass and were failing for the wrong reason (a genuine bug in the
test, not the code). Fixed by running the real method against a fake Neo4j session
(mirroring `test_project_axis.py`'s established `fake_run` dispatch pattern) and
asserting on the actually-executed, evaluated query string instead — the same technique
`test_i1`/`test_i2`/`test_i8`'s Cypher tests already used correctly for
`_find_grounded_fact_groups`. A second, smaller mistake: an early version of the
"removed keys" test substring-matched `"domain_level_cycles"` against source text and
false-positived on my OWN explanatory code comment (which legitimately quotes that
string) and on the substring inside `count_domain_level_cycles` the import statement —
fixed by checking `hasattr` on the module/class instead of grepping prose.

## Rebases

1. **First rebase**, mid-C1: `main` advanced from `0e43d74` (v0.8.61, this branch's base)
   to `d11ced0` (v0.8.62 — B2's I7 fix, #222). Clean, no conflicts.
2. **Second rebase**, at the start of C1b, per Opus's message: `main` advanced again to
   `65debc0` (v0.8.63 — REM decision-linking removal, #223). Also **clean, no conflicts**
   — `rem_loop.py`/`relation_sweep.py`/`schema.md`/both `SKILL.md` copies/version pins all
   changed, none of them files this branch touches. Full suite re-ran green both times
   (1249 → after rebase 2, 1231 before C1b's own test changes → 1236 now).

Both clean rebases are exactly the payoff the plan names for the "builders never touch
version/CHANGELOG/sync_skills.sh" rule (§7.6): a branch that respects file ownership
survives `main` moving twice underneath it with zero merge conflicts.

## Judgement calls not visible in the diff

1. **Discovery is unscoped (no `ids` parameter)** on all three call sites. Deliberate
   reading of §2.1's membership rule (the WHOLE group, always, not a delta).
2. **Reversed decisions are NOT filtered out of the fact gate's GROUNDED_IN walk.**
   §2.2a reads as scoped to the INSIGHT gate (C2's job), not §2.1's FACT gate. Not my
   call to make unilaterally; flagged for C2 to confirm or extend.
3. **`registered_sections` is derived from the SAME rows `_find_grounded_fact_groups`
   (and now `_nrem_cycle_counts`) return**, not from a fresh Postgres join — a
   DOMAIN_OF/PROJECT_OF edge is only ever written for an already-registered name.
4. **C1b — deleted rather than kept-as-legacy, now that the blocker is gone.** C1 had to
   choose "keep four names alive, unused by the fold, because coordinator.py needs them"
   as the SAFE option under a real cross-file constraint (another builder owned that
   file). C1b had no such constraint, so the same four names (plus the now-provably-dead
   `_count_domain_cycles` and the newly-identified `NREM_DOMAIN_THRESHOLD`) come out
   entirely rather than staying as permanent legacy scaffolding. This is the intended
   two-step shape the escalation was written for, not a reversal of the C1 judgement —
   C1's choice to escalate rather than guess was what made this clean second step
   possible at all.

## Next (C2)

`Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §2.2–§2.5, §4.2 Path B. Start from
`insight_gate.py` (`insight_cluster_cypher`). `consolidation_loop.py` and `coordinator.py`
are both now free of every pre-v2 two-level artifact in the FACT path; C2's insight path
is the one remaining place `relation_confidence` calibration and the pre-v2-style
telemetry/fold coupling question will come up again — `_nrem_cycle_counts`'s
`decision_cycles` half (already delegating to `insight_gate.insight_cluster_cypher`
count-only) is the existing model to extend, not replace.
