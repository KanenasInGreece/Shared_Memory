# HANDOFF — C4: payload (Zettelkasten thematic index, insight causal chain, summary_ids)

**Task id:** C4, per `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §6 "C4 — payload (AFTER
C3.1 merges — same file)". Base: `main` `962b6b4` (v0.8.68, PR #228 merged). Branch:
`feat/c4-payload`. **Not merged. No version bump. No CHANGELOG entry.**

This file replaces the STALE `HANDOFF.md` this worktree inherited from C3.1 (PR #228 — that PR's
own handoff was apparently never cleaned up at merge and shipped to `main`; flagging that as a
minor process gap, not fixing it here). Everything below is C4-scoped only.

## Status: DONE — all criteria A–G implemented, tested (mutation-checked on the highest-value
guards), live-verified against the running Postgres/Neo4j. One item under criterion D is a
DELIBERATE SCOPE BOUNDARY, not an escalation — see "Criterion D" below.

## What shipped, per criterion

### A — Thematic payload (§3.1)
`_consolidate_clusters` (`consolidation_loop.py`) no longer calls an LLM for the thematic fold.
`content` is now a **deterministic Zettelkasten index**: `fold_record_line` over each constituent
fact's own tight text (`coalesce(rem_summary, content)`, already what `_find_grounded_fact_groups`
returns), concatenated with `\n`. Zero/low inference, as §3.1 mandates — not a paraphrase.
`metadata` gained `entities` (union of the constituent facts' own human-asserted entities) and
`cypher_query` (`thematic_cypher_query()` — a self-contained Cypher statement rebuilding the
group's Fact→judgement provenance neighbourhood at read time).

**Consequence, stated plainly: `generate_summary()` is REMOVED.** It was the thematic fold's only
caller. Its whole LLM+preservation-gate+truncation-retry apparatus is gone from the thematic path —
correct, per §4.2 Path A step 2 ("Build the Zettelkasten index (zero/low inference)"), and per
`decision:1032`'s dereference principle (the design explicitly moved detail OUT of the vector and
INTO the graph walk). `render_alternative_lines` is removed too (it was only ever called from the
pre-C4 insight fold — see B). The insight path (§3.2) is unaffected — it still synthesises.

### B — Insight payload (§3.2)
`_fold_insight` is rewritten. It fetches each judgement's own row
(`id, content, project, type, metadata`) from `technical_docs` — **decisions AND retrospectives
alike** (criterion C). Each block is `[DECISION|RETROSPECTIVE pg_id=N project=P]\n<content>` —
**strictly** the judgement's own content (Title+Rationale, verbatim — `save_decision`/
`handle_retrospective` both set `content` that way already). No confidence line, no alternatives
line, no retrospective-outcome line, no grounding-edge line. `generate_insight`'s prompt was
rewritten to match: it no longer mentions GROUNDING/CONFIDENCE/ALTERNATIVE instructions at all.
`insight_cypher_query()` is the deferral instrument — CONSIDERED/REJECTED/UNDER_CONDITIONS (and
everything else) are reachable there, never duplicated into the text.

Within-component order is ascending `pg_id` (`ORDER BY id` in the fetch) — this is both §2.4's rule
and the causal order (a retrospective's pg_id always postdates the decision it evaluates). Between-
component order was already correct pre-C4 (`insight_gate.order_components`, C2's work) and each
component still folds as its own, separate insight — C4 did not need to touch that.

### C — The PR #226 seam, fixed BEFORE widening the payload
`_mark_insight_in_graph` used to `MATCH (d:Decision {pg_id: did})` only. Widened to
`MATCH (d) WHERE (d:Decision OR d:Retrospective) AND d.pg_id = jid` — mirrors the exact pattern
`run_lineage_invalidation_pass` already uses to CLEAR the same flag on retirement. Proven live (see
"Live verification" below): both labels resolve correctly by `pg_id`. `run_insight_cycle` now feeds
`judgement_ids` (the full ordered reach) to `_fold_insight`, not `decision_ids`.

### D — Reversal payload obligation (carried, not in §3) — IMPLEMENTED, scoped deliberately
`fetch_reversal_context(conn, judgement_ids)` (new): when this fold's own constituents are about to
close an OPEN `refold_ledger` row whose trigger was a **reversed decision**
(`trigger_kind='technical_docs'`, `summary_kind='insight'`, `status='open'`), this fold is the
DIRECT SUCCESSOR of that reversal. It looks up the reverted decision's title and its reversing
retrospective's content, and `_fold_insight` builds `reversal_lines` from that, passed into
`generate_insight` as a distinguished `[BEGIN REVERSALS]...[END REVERSALS]` block with an explicit
instruction to state what was reverted and why. The reversing retrospective's own content is a HARD
preservation anchor (must survive synthesis, same rule as every judgement anchor).

**Why this needed no escalation despite the plan's two open §2.2a edge cases:** it is driven
entirely by `refold_ledger` TRIGGER PROVENANCE (a Postgres table), never by walk/gate/component
membership. It never asks "is the reversing retrospective walked into the reach" (edge case #1) or
"does it appear in the payload AS A MEMBER" (edge case #2) — it treats the reversal as external,
out-of-band context about a DIFFERENT (excluded) judgement, sourced from the ledger's own record of
why re-folding was triggered. **The two edge cases remain genuinely open** — this does not resolve
them, it sidesteps needing them resolved. Still owed to Xenofon per the plan's own text.

### E — `decision:1032` field-by-field ruling

| Field | Who reads the STORED copy (cannot walk instead) | Ruling |
|---|---|---|
| `source_pg_ids` | `supersede_covered_summaries` (subset compare, Mechanism A); `fetch_active_insight_rows`→`classify_identity` (§2.5, every insight cycle); `fetch_refold_insights` (re-fold trigger source); `fetch_invalidated_summaries` leg 2 (reversed-decision membership); `coordinator.py:_status_of_summary` — **wire contract**, joins straight to `technical_docs` | **STORE.** Consumed by the walk (supersession/identity) AND read Postgres-side with no graph access (the API). |
| `summary_ids` | `fetch_invalidated_summaries` leg 3 (cascade); `append_insight_references` (§2.5 'same'-case merge target) | **STORE.** This IS the mechanism §3.2 mandates — the reason it's a separate field is precisely to avoid the id-space collision a walk-only design would still need solved. |
| `project` | `fetch_active_thematic_summary_id` (pure Postgres `WHERE`, no graph access, called every insight cycle for the §2.5 same-case lookup) | **STORE.** Postgres-side filtering with no graph access — the stated exception. |
| `domains` | Same Postgres-side reasons (`coordinator.py:_status_of_summary` API response; a future dashboard) | **STORE**, with the caveat stated explicitly: this is a materialised UNION of each judgement's own OWNED domain (not independently asserted on the insight) — legitimate under the OWNS/DERIVES split because the consumer is Postgres-only. |
| `entities` | Same reasoning as `domains` | **STORE**, same caveat. |
| `cypher_query` | N/A — this is not consumed BY a walk, it IS the walk-deferral instrument `decision:1032` asks for | **STORE** (this is what "dereference what the reader renders" produces, not what it forbids). |

No §3.2-listed field was dropped; nothing is stored without a stated consuming reason.

### F — C3 leg 3 gets its first real coverage
`tests/test_lineage_invalidation.py::test_leg3_cascades_using_the_real_summary_ids_c4s_fold_actually_writes`
runs the REAL `ConsolidationDaemon._fold_insight` (with `summary_ids=[173]`), captures the ACTUAL
JSON it writes to `community_summaries.metadata`, then feeds that real value into a StubConn shaped
like `fetch_invalidated_summaries`'s leg 3 and proves it cascades. This is the missing half of the
pre-existing `test_leg3_cascades_from_leg1_retired_summary_ids` (which proved the SQL contract with
a hand-typed fixture) — this one proves the WRITER (C4) and the READER (C3) agree on the JSON shape.
Mutation-checked: reverting `_fold_insight`'s `"summary_ids": summary_ids` to `"summary_ids": []`
kills exactly this test.

### G — Identity (§2.5)
`fetch_active_insight_rows` (replaces `fetch_active_insight_judgement_sets` — now returns
`(id, judgement_set, metadata)` triples, not bare sets) + `append_insight_references` (new): a fresh
cluster whose judgement set exactly matches ('same') an existing active insight's no longer just
gets filtered out silently — the triggering thematic summary id (via
`fetch_active_thematic_summary_id`) and domain are APPENDED to the existing insight's
`summary_ids`/`domains`, deduplicated, in one Postgres UPDATE. A 'covered' match (the existing
insight's reach is a strict SUPERSET — `insight_gate.classify_identity`'s own defensive extra case,
not in §2.5's LOCKED table) is skipped with **no write at all** — nothing new to add, and folding it
would create a redundant duplicate. 'supersedes'/'overlap'/'disjoint' fold as normal (Mechanism A
resolves 'supersedes' at write time, unchanged).

## Files changed
```
shared-memory/scripts/consolidation_loop.py          (thematic + insight fold rewrite; see above)
shared-memory/scripts/coordinator.py                 (_status_of_summary exposes domain+domains+summary_ids)
shared-memory/Documentation/schema.md                (community_summaries metadata contract, both shapes)
shared-memory-skill/shared-memory/Documentation/schema.md   (synced copy — MANIFEST parity)
tests/test_insight_consolidation.py                  (near-total rewrite — judgement-inclusive fold)
tests/test_nrem_confidence.py                        (thematic preservation-gate tests removed —
                                                        structurally impossible now; insight coverage kept)
tests/test_lineage_invalidation.py                   (+1 end-to-end leg-3 test)
tests/test_fold_origin.py                             (docstring only — "feeds the LLM" → "feeds the index")
```

## Tests

Full suite: `MOCK_LLM=1 uv run --with pytest --with pytest-asyncio --with fastmcp --with
psycopg2-binary --with httpx --with neo4j --with asyncpg --with aiohttp --with json-repair --with
numpy pytest tests/ -q` → **1303 passed** (this PR's own tests) **+ 1 pre-existing standing red**
(`test_rem_grounding_slice.py::test_grounding_slice_merges_batch_round_robin`, confirmed failing in
isolation against unmodified `main` — not touched by this branch; it is also visibly FLAKY when run
inside the full suite depending on interleaving, passing sometimes — this is a pre-existing property
of that test, not something this PR introduced or should paper over).

### Mutation checks performed (backup → mutate → run → confirm exact test dies → restore → diff clean)

| Guard reverted | Test that died |
|---|---|
| `_mark_insight_in_graph`'s widened `(d:Decision OR d:Retrospective)` reverted to `(d:Decision {pg_id: jid})` | `test_fold_insight_full_path` |
| `source_pg_ids` mutated to include `summary_ids` (`sorted(set(src_ids) \| set(summary_ids))`) | `test_fold_insight_full_path`, `test_fold_insight_summary_ids_never_mixed_with_source_pg_ids` (both) |
| §2.5 'same'/'covered' append branch short-circuited to a bare `continue` (pre-C4 skip-only behaviour) | `test_run_insight_cycle_same_identity_appends_reference_not_a_new_fold` |
| `fetch_reversal_context` call replaced with `reversals = []` | `test_fold_insight_reversal_note_injected_and_anchored` |
| `_fold_insight`'s written `summary_ids` replaced with `[]` | `test_leg3_cascades_using_the_real_summary_ids_c4s_fold_actually_writes` |

Each mutation was applied to a full backup copy of `consolidation_loop.py`, the targeted test(s)
re-run to confirm the EXACT expected failure (not a different, unrelated one), then the file was
restored and `diff`-confirmed byte-identical before the next mutation.

### A test-isolation trap found and fixed along the way (not a mutation, a real pre-existing bug class)

`tests/test_rem_loop.py` dynamically re-execs `consolidation_loop.py` at COLLECTION time and
overwrites `sys.modules["consolidation_loop"]` (never restored). Two of my new
`test_insight_consolidation.py` tests did a LOCAL `import consolidation_loop as cl` inside the test
function body (copying a pattern the pre-existing `test_run_insight_cycle_calls_fold_with_compatible_signature`
already used) — at TEST-RUNTIME this resolves against whatever `sys.modules` currently holds, which
by then is `test_rem_loop.py`'s swapped-in copy, a DIFFERENT module object than the one
`ConsolidationDaemon` (imported at file-collection time) was defined in. Monkeypatching `cl.X` then
patches the wrong module, and the daemon falls through to REAL module-global functions and a REAL
`psycopg2.connect` against the LIVE local Postgres — which is why this only reproduced in the FULL
suite, never in isolation, and why the failure mode was a `ValueError` about unpacking, not an
obviously-related assertion. **Fixed** by capturing `import consolidation_loop as cl` ONCE at
`test_insight_consolidation.py`'s own module (collection) scope and removing all three in-function
local imports (mine and the pre-existing one) in favour of that single reference. Flagging because
this class of bug is NOT specific to my tests — any future test in this file (or a sibling file
collected before `test_rem_loop.py`) that does a local `import consolidation_loop as cl` inside a
function body is at risk of the same silent mis-patch. Worth a standing note somewhere if this
recurs.

## Live verification (read-only — no writes)

All against the running `agent_data` Postgres + Neo4j (see `shared-memory/.env` for creds, read from
the sibling non-worktree checkout since `.env` is gitignored and worktrees don't share untracked
files):

- `fetch_judgement_types`'s exact SQL — ran, 2 rows, correct types.
- `fetch_active_insight_rows`'s exact SQL — ran, 2 live insight rows returned with real metadata
  (both still pre-C4 shape: `domain: "insight"` placeholder + `projects` array — confirms the
  "pre-C4 rows keep the old shape" note in schema.md is accurate, not hypothetical).
- `fetch_active_thematic_summary_id`'s exact SQL, seeded with `("shared-memory-GitHub",
  "architecture", "domain")` — returned `173`, a real active thematic row.
- `append_insight_references`'s `SELECT ... FOR UPDATE` — ran (0 rows for a throwaway id, as
  expected — no live write attempted).
- `fetch_reversal_context`'s both legs — ran, 0 rows (consistent: `refold_ledger` is empty on the
  live corpus, as `fact:1180`/the C3.1 report already established — the lineage pass has not fired
  live yet).
- `_fold_insight`'s exact judgement-content SELECT (with the real `PROJECT_SQL` macro from
  `project_axis.py`, not an approximation) — ran against real decision pg_ids `[245, 267, 1095,
  1098]`, correct project/type values returned.
- `fetch_refold_insights`'s widened SELECT (now includes `metadata`) — ran, 0 rows (no open retros
  right now), query parses.
- `thematic_cypher_query([6,7,16,54,55])` and `insight_cypher_query([245,267,1095,1107])` — both
  CALLED FOR REAL (not just string-inspected) against the live Neo4j: the thematic query returned 5
  Fact rows with correctly-empty judgement arrays (none of these particular facts are grounded-in by
  anything yet); the insight query returned 16 rows spanning the real grounding/outcome edges of
  those 4 judgement ids.
- Criterion C's widened Cypher predicate, run read-only (`MATCH` only, no `SET`) against real pg_ids
  spanning both labels — `245/267/1095/1098` (Decision) and `1107` (Retrospective) all resolved
  correctly with the right label.
- `SELECT elem::bigint FROM jsonb_array_elements_text('[173,179]'::jsonb) AS elem` — the exact
  expression leg 3 uses, run against a literal matching `_fold_insight`'s write shape — returned
  `[(173,), (179,)]`. Proves the round-trip without touching any table.

No live writes were made anywhere in this verification.

## Judgement calls the diff doesn't show

- **The insight `content` prompt no longer includes ANY of confidence, alternatives, retrospective-
  rating, or grounding-edge detail** — a deliberate, large narrowing from the pre-C4 prompt, read
  from §3.2's "extracting strictly the Title and Rationale" plus the explicit CONSIDERED/REJECTED/
  UNDER_CONDITIONS exclusion. This also makes the entire "stage 5" calibration-gated grounding-edge
  rendering apparatus (`_fetch_outcome_edges`, `_fetch_grounding_edges`, `fetch_retro_records`,
  `render_alternative_lines`) DEAD inside this module and they are REMOVED. `relation_confidence.py`
  itself is untouched (still used by `relation_sweep.py`/`rem_loop.py`) — only its consumption
  INSIDE the insight fold is gone. `run_insight_cycle` still fetches and records the general
  family-calibration snapshot (`rec.calibration`) once per cycle — kept as harmless standalone
  telemetry — but `machine_edges_consumed`/`edges_awaiting_calibration` will now always read 0 for
  insight runs, since nothing renders a grounding edge any more. This is not a metric MEANING
  inversion (0 correctly means "zero rendered", which is now permanently true) but the counter is
  now permanently inert — flagging for the monitor's own awareness (Group 3).
- **`project` on an insight row is SINGULAR**, matching §3.2's literal text, even though the walk
  can in principle cross projects (not just domains — nothing in the walk mechanism itself respects
  project boundaries any more than domain ones). §3.2 does not make `project` multi-valued the way
  it does `domains`; I implemented exactly what is written, not what would be "more consistent". If
  cross-project walks turn out to be common enough to matter, `project` becoming a list is a
  follow-up, not a defect in this PR.
- **`summary_ids` on a FRESH insight fold is a single-element list from ONE Postgres lookup**
  (`fetch_active_thematic_summary_id` on the SEEDING group's own project/domain) — not an attempt to
  resolve a thematic summary for every domain the walk happened to touch. Cross-domain/cross-project
  judgements the walk pulled in are represented via `domains`/`entities`/`cypher_query` (all
  graph-walkable), but a full "which thematic summary does each touched domain currently have
  active" resolution was judged out of scope for one payload PR — flagging explicitly rather than
  silently under-delivering. A RE-FOLD carries the previous insight's `summary_ids` FORWARD
  unchanged (a re-fold is triggered by a new retrospective, not a change in which thematic summaries
  it rests on).
- **README.md's Phase 2b/3a insight-consolidation section is now further stale** (it describes the
  pre-v2, pre-C1 entity-hub design in detail) — this predates C4 (C1/C2/C3 apparently left it too)
  and is a large, separate documentation debt across the whole Dreaming Cycle v2 track, not something
  owed uniquely by this PR. Flagging rather than attempting a partial rewrite of Xenofon's-voice prose.
- **`INSIGHT_DOMAIN = "insight"` and `render_alternative_lines` are REMOVED**, not deprecated —
  confirmed zero other callers/tests before removing (grepped).

## Owed at release (NOT done here — explicitly out of scope for a builder)
1. Version bump (+0.0.1), CHANGELOG entry, `sync_skills.sh` (this branch already manually synced the
   one drifted tracked-copy file, `Documentation/schema.md`, to keep the suite green — a real
   `sync_skills.sh` run at release time is still the authoritative step).
2. Restart `hive-mind-gateway.service`, verify `/health`.
3. The two §2.2a edge cases the plan leaves open (does a reversing retrospective satisfy G2; does it
   appear in the payload as a member) — still owed to Xenofon, unaffected by this PR's ledger-driven
   reversal-note mechanism.
4. The `summary_ids`-for-every-touched-domain question flagged above, if it turns out to matter on
   real cross-domain walks.
