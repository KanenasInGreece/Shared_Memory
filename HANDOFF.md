# HANDOFF — C2: insight gate rebuild (Dreaming Cycle Plan to v2)

**Task:** C2 (Track C, serial chain C1→C2→C3→C4).
**Branch:** `feat/nrem-insight-gate-v2`, based on `main` @ `f2f33cb` (v0.8.65, rebased
from the original `57de695` base after `fix/nrem-telemetry-gauge-dead` landed).
**Status:** ✅ **ACCEPTED by Opus (independent re-verification against the live graph —
walk counts, the `CommunitySummary` pg_id collision guard, the 2/13/5 census, suite,
private-name audit).** One fix requested post-review and applied — see the
`decision_threshold` note in **What is done, §3** and the **Monitor effect** section.
Both escalations RULED — see **ESCALATIONS** (now closed, not open questions for C3/C4).
⛔ Payload synthesis (C4) and supersession cascade (C3) deliberately NOT built — see
**SEAM** below.

---

## What is done

1. **`shared-memory/scripts/insight_gate.py` — REWRITTEN.** The old 1-hop shared-Entity
   gate (`insight_cluster_cypher`, `INSIGHT_THRESHOLD`, `INSIGHT_HUB_DEGREE_CAP`) is
   **deleted**, not deprecated. New driver-free (no `psycopg2`/`asyncpg`/`neo4j`/`httpx`
   import — the walk driver takes an already-constructed driver object as a plain
   parameter) module holding:
   - `CLOSED_RELATION_TYPES` / `WALK_STEP_CYPHER` (`_walk_step_cypher()`) — the one-hop
     BFS layer Cypher: undirected, over `GROUNDED_IN|INFORMED_BY|CONSIDERED|REJECTED|
     UNDER_CONDITIONS|HAD_OUTCOME`, matched only against `{Fact,Decision,Retrospective}`
     on both ends, excluding a reversed Decision (`superseded=true`) on both ends (I10).
   - `_UnionFind` + `walk_reached_graph(seed_fact_ids, fetch_neighbors)` — the pure
     fixpoint BFS (I3): terminates when a layer discovers nothing new (bounded by the
     finite node count, not a hop/edge/hub cap), returns `(labels, consolidated,
     components)` where `labels`/`consolidated` are judgement-only (facts pass through
     but never appear) and `components` are the raw §2.4 partition (union-find over the
     whole explored graph, so shared-fact connectivity — I5 — falls out for free).
   - `walk_group_reached_set(driver, seed_fact_ids)` — the real-I/O wrapper (duck-typed
     `driver.session()`, matches both `consolidation_loop.ConsolidationDaemon.driver`
     and `coordinator.MemoryCoordinator._neo4j`).
   - `passes_insight_gate(labels, consolidated)` — G2 (>=1 Retrospective in the FULL
     reached set) + G3 (>=1 judgement with `consolidated=false`, I4: read only from the
     judgement dict, never a fact). Evaluated ONCE per group, not per component.
   - `order_components(components, labels)` — §2.4 deterministic ordering (retrospective
     components first by smallest retro pg_id, then decision-only by smallest pg_id;
     ascending pg_id within a component).
   - `classify_identity(new_ids, existing_ids)` — §2.5 LOCKED set-identity resolution:
     `same` / `supersedes` / `covered` (the reverse case, not named in the plan's table
     but the same set logic settles it) / `overlap` / `disjoint`.
   - `INSIGHT_AGE_CENSUS_K` — `INSIGHT_THRESHOLD` renamed, not repurposed silently: it is
     no longer a gate parameter (v2 has none — G2/G3 are each "at least one"), retained
     only as the telemetry K for `_kth_oldest_age_seconds`.

2. **`consolidation_loop.py`:**
   - `_find_fresh_insight_clusters` rewritten. G1 is **reused, not re-derived** — it
     calls the SAME `_find_grounded_fact_groups` + `nrem_gate.eligible_domain_level_clusters`
     C1 already built (same call `_consolidate_clusters` makes). For every gating group
     it walks (`insight_gate.walk_group_reached_set`), gates (`passes_insight_gate`),
     orders components (`order_components`), and returns **one row per component**:
     `{entity: "", decision_ids, projects, domain, judgement_ids, has_retrospective}`.
     A component with zero Decision members after filtering (retrospective-only — the
     §2.2a edge case) is skipped and logged, not folded with nothing to write.
   - `run_insight_cycle` gained one new step: identity resolution (§2.5) filters out any
     cluster whose FULL `judgement_ids` set exactly matches an existing active insight's
     `source_pg_ids` (new `fetch_active_insight_judgement_sets(conn)`), via
     `classify_identity(..., ...) == "same"`. The existing fold loop body (lines consuming
     `c["entity"]`/`c["decision_ids"]`, the dead-letter check, `_fold_insight` call) is
     **UNCHANGED** — see SEAM below for why.
   - `INSIGHT_THRESHOLD` → `INSIGHT_AGE_CENSUS_K` at its one call site
     (`_kth_oldest_age_seconds`).

3. **`coordinator.py`:**
   - Import changed from the retired `insight_cluster_cypher`/`INSIGHT_THRESHOLD`/
     `INSIGHT_HUB_DEGREE_CAP` to `walk_group_reached_set, passes_insight_gate` (top-level
     — safe, `insight_gate.py` needs no DB driver import; only `psycopg2` is genuinely
     absent from the gateway's dependency set, and `insight_gate.py` never imports it).
   - `_nrem_cycle_counts`'s `decision_cycles` now counts **gating groups**, not the old
     entity-cluster count: for each `(project, domain)` group `eligible_domain_level_clusters`
     already gates (same call `fact_cycles` makes), it runs `walk_group_reached_set` +
     `passes_insight_gate` — the SAME predicate the fold uses, one definition. Lazy
     `nrem_gate` import kept as two separate `from nrem_gate import X` lines (not combined)
     so the pre-existing `test_nrem_cycle_counts_reuses_the_folds_own_partitioner`
     source-substring check still matches.
   - ✅ **CORRECTED per Opus's review: `decision_threshold` is REMOVED from the returned
     dict, not repurposed.** My first pass kept the field name and set it to `1`,
     reasoning that an existing monitor consumer should not see a field silently vanish
     — Opus ruled that backwards: I had already applied "rename, don't repurpose" one
     level down (`INSIGHT_THRESHOLD` → `INSIGHT_AGE_CENSUS_K`) but not to this
     outward-facing field, where it matters *more* because the monitor is a separate repo
     with no visibility into my reasoning. `decision_threshold: 1` would read as "the
     threshold was lowered from 2 to 1" — false; there is no decision threshold any more
     (G2/G3 are each "at least one" conditions, not a tunable count), and a replacement
     field that still reads as a bare number would recreate the same trap. **Same
     precedent v0.8.64 already set for `domain_threshold`** (removed outright with
     `NREM_DOMAIN_THRESHOLD`, never repointed at a new number) — see the monitor-effect
     list below, written to be carried into the release notes verbatim alongside it.

4. **Tests:**
   - New `tests/test_insight_gate.py` (31 tests) — pure walk/gate/component/identity
     functions in isolation, no real Neo4j.
   - Rewrote the fresh-cluster integration tests in `tests/test_insight_consolidation.py`
     (2 tests, end-to-end through `_find_fresh_insight_clusters`).
   - Rewrote the `decision_cycles` tests in `tests/test_project_axis.py` (4 tests, one
     added for the `decision_threshold` removal) and retired the P22 section of
     `tests/test_project_identity.py` (6 tests removed — they pinned
     `insight_cluster_cypher`'s Cypher text, which no longer exists; noted why in a
     comment rather than silently deleting).
   - Fixed `tests/test_v2_fact_gate.py`'s `_nrem_cycle_counts` query-capture helper to
     dispatch on the fact-discovery-query marker instead of excluding the now-gone
     `"count(*) AS cycles"` insight-count query, and updated its returned-dict
     wire-contract pin (`test_nrem_cycle_counts_returned_dict_has_exactly_the_new_shape`)
     to the 4-key shape with `decision_threshold` explicitly asserted absent.
   - **Test count: 1265 passing (baseline `main` = 1236; net +29).**

---

## ⛔ SEAM — where C2 deliberately stops (C4's payload territory)

`_find_fresh_insight_clusters` returns each component's **decision-only** ids in
`decision_ids` (feeding today's `_fold_insight` unchanged) and the **full** reach
(decisions + retrospectives) separately in `judgement_ids`. This is not laziness — I
traced why judgement-inclusive ids cannot go through today's fold without silent
corruption:

- `_mark_insight_in_graph` (`consolidation_loop.py`) does
  `MATCH (d:Decision {pg_id: did}) SET d.consolidated = true` — **Decision-label-only**.
  Feeding it a Retrospective pg_id would silently never mark it consolidated, so **G3
  freshness would never clear for that judgement — the same group would re-trigger
  every cycle forever.**
- `_fetch_decisions` labels every block `"[DECISION pg_id=...]"` regardless of what the
  id actually is — a Retrospective's content would be synthesised as if it were a
  decision.
- `generate_insight`'s metadata still writes `source_pg_ids: src_ids` under the OLD
  (decision-only) contract, not `summary_ids` / multi-valued `domains` (§3.2 — an
  explicitly NEW field C2 must not invent ahead of C4).

Making the fold judgement-inclusive — correct labelling, marking both labels
consolidated, `source_pg_ids` = judgements only, `summary_ids` new field — is exactly
§3.2/§4.3 steps 4-5, i.e. **C4's payload rewrite**. C2 stops here and hands C4 the full,
correctly-computed, correctly-ordered reach (`judgement_ids`, `has_retrospective`) to
build on.

**Identity resolution (§2.5) is implemented for real** (`classify_identity`,
`fetch_active_insight_judgement_sets`, wired into `run_insight_cycle`) but is
**transitionally inert** against today's decision-only persisted `source_pg_ids`: an
exact `"same"` match against the LOCKED full-judgement-set definition essentially cannot
occur until C4 makes `source_pg_ids` judgement-inclusive. This is intentional — the
correct definition is implemented now rather than a decision-only approximation C4 would
have to revisit. See the live proof under **Live verification** below: two gating groups
currently reach the **identical** 10-judgement set, and the pre-existing `folded`-id
tracking in `run_insight_cycle`'s loop (unchanged) already prevents a same-cycle double
fold of it — `classify_identity` will additionally prevent a *cross-cycle* re-fold once
C4 lands and an active insight's `source_pg_ids` actually carries that full set.

**C3 (supersession cascade) and its re-enqueue mechanics are not built.** Nothing here
assumes them; `fetch_active_insight_judgement_sets` is a pure read.

---

## ⚠ Monitor effect — `GET /memory/telemetry`'s `nrem` key changed shape again

For the release notes, alongside v0.8.64's own list (same endpoint, same key, two
releases in a row — a monitor author reading both should see one consistent rule, not
two different ones):

- **Removed:** `decision_threshold`. There is no decision threshold any more — G2
  (>=1 Retrospective reached) and G3 (>=1 fresh judgement reached) are each "at least
  one" conditions, not a tunable count, so there is no number left to report under that
  name. **Not repurposed** — a bare `1` under the old name would read as "the threshold
  was lowered from 2 to 1", which is false. No replacement field: G2/G3 are not "a
  threshold with a new name," so a field that still reads as a number would recreate the
  exact meaning-inversion trap this removal exists to avoid.
- **`decision_cycles` keeps its name but is not a continuation of the old series** — same
  framing v0.8.64 used for `fact_cycles`. It used to count entity-hub clusters spanning
  >=2 distinct projects; it now counts **gating `(project, domain)` groups whose full
  walked reach passes G1+G2+G3** (`insight_gate.passes_insight_gate`, the exact predicate
  the fold folds on). **This is a step change on deploy, not an anomaly.**
- Unchanged: `fact_cycles`, `fact_threshold`, `total_cycles`.
- The gauge now runs the SAME walk and the SAME gate predicate the fold runs
  (`insight_gate.walk_group_reached_set` / `passes_insight_gate`), so telemetry can no
  longer drift from the gate it claims to measure — covered by
  `tests/test_project_axis.py`'s `test_decision_cycles_counts_a_group_whose_walk_reaches_a_retrospective`
  and the wire-contract pin in `tests/test_v2_fact_gate.py`.

---

## Invariants — mutation-checked (all confirmed against the real source, then reverted)

Every mutation below was performed by editing `shared-memory/scripts/insight_gate.py`
directly, running `uv run --with pytest --with pytest-asyncio pytest tests/test_insight_gate.py -q`,
observing the failure, then `git checkout -- shared-memory/scripts/insight_gate.py` to
restore. Confirmed clean (`git diff --stat` empty) after every restore, and the full
suite (1264 at the time these were run, before the `decision_threshold` fix below added
one more test — 1265 now) reconfirmed green at the end. `insight_gate.py` itself is
untouched by the `decision_threshold` fix, so these results still hold as-is.

| Inv. | Mutation performed | Result |
|---|---|---|
| **I1** | Added `OPTIONAL MATCH (m)-[:MENTIONS]->(e:Entity)` into `_walk_step_cypher` | Exactly `test_i1_walk_cypher_never_touches_entity_or_mentions` failed. 1/31. |
| **I2** | Added a `WITH pid, m, collect(DISTINCT 1) AS project_ids` clause | Exactly `test_i2_walk_cypher_never_touches_project_identity` failed. 1/31. |
| **I3** | Added `_hops = 0` / `while frontier and _hops < 4` to `walk_reached_graph`'s loop | Exactly `test_i3_walk_is_unbounded_no_hop_cap` failed (a 12-hop chain reached only 4 nodes). 1/31. |
| **I4** | Removed the `if dst_label in _JUDGEMENT_LABELS:` guard (let Facts populate `labels`/`consolidated`) | 3/31 failed: the dedicated I4 test, plus `test_walk_never_returns_a_fact_as_a_reached_judgement` and `test_i5_...` — this guard is shared infrastructure for "facts never appear in the reached set" broadly, of which I4 (freshness-on-judgements) is one consequence. Documented as shared, not mis-isolated. |
| **I5** | Moved `uf.union(src, dst)` inside the `if dst not in seen:` guard (skip union on a same-batch rediscovery) | Exactly `test_i5_two_judgements_sharing_a_fact_are_one_component` failed (2 separate components instead of 1). 1/31. |
| **I6** | `has_retrospective = True` (hardcoded) in `passes_insight_gate` | Exactly `test_i6_gate_requires_at_least_one_retrospective` failed. 1/31. |
| **I10** | Deleted the `AND NOT (m:Decision AND coalesce(m.superseded, false) = true)` clause | Exactly `test_i10_walk_cypher_excludes_a_reversed_decision_on_both_ends` failed. 1/31. |

I1/I2 are also enforced structurally in `insight_gate.py`'s docstrings and by
`test_i2_passes_insight_gate_signature_carries_no_project_argument` (the gate function's
own signature carries no project argument at all).

---

## Live verification (read-only, `shared-memory-GitHub` corpus, 2026-08-10)

Run via a throwaway script in the scratchpad (not committed), connecting directly to
Neo4j with credentials from the MAIN repo's `shared-memory/.env` (never copied into the
worktree). **No writes** — every query below is a plain `MATCH`/`RETURN`.

```
[1] _find_grounded_fact_groups rows: 22
[2] Gating (project, domain) groups (G1, density>=3): 2
    - shared-memory-GitHub / architecture: 13 grounded facts
    - shared-memory-GitHub / infrastructure: 5 grounded facts
```

This matches the plan's own §8 measurement (13 and 5) exactly — G1 (reused from C1) is
unchanged, as expected.

```
[3] architecture: reached judgements: 19 (G2 True, G3 True) -> PASSES
    components (3): sizes = [5, 10, 4]
[3] infrastructure: reached judgements: 10 (G2 True, G3 True) -> PASSES
    components (1): sizes = [10]

[4] TOTAL gating groups that PASS the full insight gate: 2 / 2
```

**Both groups pass — small, as the brief expects (1-2).** ⚠ The plan's §2.4 worked
example (authored earlier the same day) measured architecture's walk at **16** judgements
in **three components of 10, 5, 1** — live right now it is **19** judgements in
**10, 5, 4**. This is corpus growth, not a defect: `decision:1146` /
`retrospective:1147` / (evidently also `1150`) were saved during today's session
(§8 documents `1146`/`1147` explicitly as a same-day worked example of the walk
reconnecting a refinement chain through a shared fact) and have joined what was
previously the lone `decision:1144` into a 4-member component — **the exact mechanism
§8 predicts, now visibly larger**. The 10/5-member components are byte-identical to the
plan's own ids.

**Notable finding, not a defect:** the infrastructure group's single 10-member component
is **the identical judgement set** as architecture's 10-member component (`245, 1095,
1098, 1099, 1104, 1106, 1107, 1109, 1114, 1121`) — a judgement grounded in one domain's
facts pulled in the other domain's reach, exactly as §2.3 warns ("the walk may leave the
group, and that is intended"). Checked live: `community_summaries` currently holds
**zero** `kind='insight'` rows, so on a first real cycle both groups would propose this
same component. The **pre-existing** `folded`-id tracking in `run_insight_cycle` (which
I did not touch) already prevents a same-cycle double fold since the two clusters' decision
ids fully overlap; `classify_identity` is the cross-cycle guard once C4 lands. This is a
clean, real-world confirmation that §2.5 identity resolution is answering a genuine
question, not a hypothetical one.

**I3 fixpoint stability, demonstrated the way the plan itself demonstrates it** (hop-capped
variable-length Cypher `[:RELS*1..N]` for increasing N, run independently of the production
BFS):

```
hop cap  1: 10   hop cap  2: 15   hop cap  3: 17   hop cap  4: 18
hop cap  5: 19   hop cap  6: 19   hop cap  7: 19   hop cap  8: 19
hop cap  9: 19   hop cap 10: 19   hop cap 11: 19   hop cap 12: 19
hop cap 13: 19   hop cap 14: 19   hop cap 15: 19
```

Stabilizes at hop 5, holds through hop 15 — the fixpoint is real, and the production
`walk_reached_graph` (unbounded, terminates when a layer adds nothing new) reaches the
same **19**, confirmed by `[3]`'s own count above.

---

## ✅ ESCALATIONS — RULED BY OPUS, not C2's to resolve or revisit

### A. `ontology.py:568` `canonical_fixpoint_entity_cypher` (shipped v0.8.60)

**RULING: AGAINST RETIREMENT. Left untouched — do not delete or re-scope it.**

I found it has zero callers anywhere in the repo and recommended retiring it. Opus
applied "cite the decision that created it before removing it," found `fact:1141` (the
v0.8.60 E1–E5 specification), and ruled that the function defines **how a non-Fact node
reads its own entities** — walk to live Facts, read `MENTIONS`. That is a **different
job** from the v2 insight walk (which reaches judgements, not entities), so "it disagrees
with the v2 walk" was comparing two things with different purposes, not a real conflict.
Its zero-caller status is real and still unresolved — a specified capability that was
never wired — but Opus is surfacing that to the operator directly, not through C3/C4.
**`ontology.py` remains untouched by this branch.**

### B. The two §2.2a edge cases (reversing retrospective without its decision)

**RULING: goes to the operator, not C3/C4.** Leave the current (unintended,
un-special-cased) behaviour exactly as built and documented — do not add handling for
either case pre-emptively:

1. Does a reversing retrospective still satisfy G2? **Current code answers "yes"** —
   `passes_insight_gate` only checks `labels.values()` for `== Retrospective`, and I10
   only excludes the DECISION a reversing retrospective evaluates, never the retrospective
   itself. Not verified as the intended answer; stated precisely so it isn't silently
   inherited as a default by whoever builds C3/C4.
2. Does it still appear in the payload? Unanswered — C4's territory, and now the
   operator's ruling to make first.

---

## What C3/C4 build on

- `insight_gate.walk_group_reached_set` / `passes_insight_gate` / `order_components` /
  `classify_identity` are the stable API surface — pure (except the walk's I/O), tested,
  live-verified. C3/C4 should call these, never re-derive the walk or the gate.
- `_find_fresh_insight_clusters`'s `judgement_ids` field is the full §2.3 reach per
  component, already ordered per §2.4 — C4's Title/Rationale extraction and causal-chain
  synthesis should iterate this, not `decision_ids`.
- `fetch_active_insight_judgement_sets(conn)` is C3's read-side building block for the
  cascade; C3 still owns the actual re-enqueue/supersession writes (§5.1-§5.3), the
  kind-scoping fix on `supersede_covered_summaries` (currently called with `level=None`
  on the insight path — untouched here, still a C3 TODO per the plan), and the two-store
  `consolidated=false` reset.
- The `decision_ids`-only seam documented above is precisely what C4's `_fetch_decisions`
  → judgement-aware fetch, `_mark_insight_in_graph` → both-label marking, and
  `generate_insight`/metadata → `summary_ids`+multi-valued `domains` rewrite needs to
  close.

## Consciously left alone

- `run_insight_cycle`'s "1. Re-folds" step (`fetch_open_retro_decision_ids`,
  `fetch_refold_insights`) — a pre-existing, different re-fold trigger (an open
  retrospective on an ALREADY-active insight's decision). Not part of §2.2's gate;
  presumably superseded by C3's proper re-enqueue mechanics.
- `supersede_covered_summaries`'s `level=None` kind-isolation gap on the insight path —
  explicitly named as C3's job in the plan (§5.3's closing note).
- `decision_threshold` removal is DONE (see the Monitor effect section above), but the
  `shared-memory-monitor` repo itself is out of scope for both of us to edit — Opus
  carries the removal into the release notes so the monitor's own author can update it.
- `ontology.py`'s `canonical_fixpoint_entity_cypher` — RULED against retirement by Opus
  (A above). Not touched, and not to be touched by C3/C4 either.
