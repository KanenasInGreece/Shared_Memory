# HANDOFF — B1: finish REM's judgement-relation decommissioning

Task: B1 (Dreaming Cycle v2 plan, `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §1 / §1.1 / §6).
Branch: `refactor/rem-decommission-judgement-relations`, rebased onto `main` @ `d11ced0` (v0.8.62,
PR #222 — landed while this task was in flight; rebase was clean, no conflicts).
Worktree: `~/claude-labs/worktrees/rem-decommission`.

## Status: DONE — ready for review (round 2, after Opus's finding was fixed)

## Commits (in order, post-rebase)
1. `30def69` — B1.a round 1: `rem_loop.py` structural fix (INFORMED_BY unreachable) + tests.
2. `3b8d095` — B1.b: `relation_sweep.py` + `schema.md` (both tracked copies) dormant-by-design docs.
3. `b10361e` — B1.c: `SKILL.md` (both tracked copies) elicits `informed_by` as a proposable role.
4. `f3f51ce` — first HANDOFF.md (now superseded by this one).
5. `3036012` — **B1.a round 2, Opus's review fix:** Decision target is DROPPED, not downgraded to MENTIONS.

## ⚠ Read this first: round 1 shipped a real defect, found by Opus's review, now fixed

Round 1 removed `INFORMED_BY` from `_LABEL_ALLOWED_RELS[ONT.decision]` / `_LABEL_DEFAULT_REL[ONT.decision]`
so an LLM suggestion of it could no longer resolve to `INFORMED_BY` — but `plan_edges` still fell
through to `_add()` for a Decision-labelled proposal, so it resolved to the label's new fallback,
`MENTIONS`, and got **written**: `(record)-[:MENTIONS]->(:Decision)`. Opus measured this against the
live graph: `MENTIONS` targeting a `Decision` has **0** occurrences ever, non-`Entity` `MENTIONS`
targets are only `AIAgent`(7)/`Human`(7), and `Decision→Decision INFORMED_BY` is 32 (so the path was
genuinely reachable, not theoretical). This violated **E1** (`:Entity` nodes exist strictly on Facts
via `(f:Fact)-[:MENTIONS]->(e:Entity)` — MENTIONS is the Fact→Entity relation, not a general-purpose
record link) and plan §1's retained-capability language ("Fact `MENTIONS` edges", not "any-record
MENTIONS-to-Decision edges"). A decommissioning task had converted one graph-shape defect
(judgement-relation minting) into a smaller but real new one (a MENTIONS shape the corpus has never
contained) as a side effect. **This is fixed in commit `3036012`** — see below.

## What round 1 (`30def69`) got right, and what it missed

**Right, and kept:** the brief pointed at deleting the `evidential = (...)` flag; reading plan §1
("REM ceases proposing... INFORMED_BY") against that showed a flag-only removal would leave
`plan_edges` still able to *propose* a plain, uncapped INFORMED_BY edge — so the fix needed to be
structural. Removing `ONT.informed_by` from `_LABEL_ALLOWED_RELS[ONT.decision]` /
`_LABEL_DEFAULT_REL[ONT.decision]` (both local to `rem_loop.py`, not `ontology.py`) makes an
INFORMED_BY *suggestion* structurally unable to resolve to one. Opus confirmed this instinct was
correct and kept it — it is now **layer one of two** (see round 2 below).

**Missed:** removing INFORMED_BY from the allowed set doesn't remove the *fallback* — `_resolve_rel`
still returns SOME relation for a known Decision node (now MENTIONS, the label's `entity_link`
default), and round 1 let that fallback reach `_add()` and get written. The plan's "REM ceases
proposing... any of them" reads narrowly as "never INFORMED_BY specifically," but the deeper invariant
(confirmed by measurement) is "REM never links to a Decision at all" — MENTIONS was never a legitimate
substitute, it's a different relation with its own, separately-violated invariant (E1).

**Round 1's dead-code removal, doc fixes, `_add()` signature change (`evidential`/`tgt_pg_id` keys
gone), and comment updates all stand unchanged** — only the Decision-target *outcome* changed (drop
vs. downgrade), not the surrounding cleanup.

## Round 2 fix (`3036012`) — what changed

In `plan_edges`, added a `label == ONT.decision` branch immediately after the existing `label ==
ONT.entity` (E4) branch, before the GROUNDED_IN remap check:

```python
if label == ONT.decision:
    # B1: REM performs zero linking to :Decision nodes, full stop —
    # symmetric with E4's Entity drop, not a downgrade to some other
    # relation. [... full comment in the diff ...]
    dropped_names.append(name)
    continue
```

**Chose `dropped_names` over a dedicated counter** (Opus left this to me): same list E4 already uses
for its Entity drop, same downstream telemetry line ("REM gate rejected N LLM-extracted name(s)"), no
new surface for one more retired mechanism. If a future reviewer wants Decision-drops distinguished
from Entity-drops in telemetry, that's a separate, larger change (would need a labelled drop reason
per name, not just a flat list) — not something this fix should grow into on its own.

**Ordering choice:** placed the Decision-drop *before* the GROUNDED_IN remap-logging check, mirroring
E4's existing precedent (an Entity-labelled GROUNDED_IN suggestion was already silently dropped without
reaching `grounded_in_remaps`, pre-dating this task). So a GROUNDED_IN suggestion against a Decision now
also skips `grounded_in_remaps` and only shows up in `dropped_names` — consistent with how the Entity
case was already handled, not a new asymmetry.

**`_LABEL_ALLOWED_RELS`/`_LABEL_DEFAULT_REL[ONT.decision]` changes from round 1 are KEPT**, per Opus:
with the `plan_edges` drop in place they're defence in depth — they stop `INFORMED_BY` specifically;
the `plan_edges` drop is the layer that actually prevents *any* edge (including the fallback) from
being written. Comments at both dict entries and in `plan_edges`'/module docstrings were rewritten to
state this two-layer structure precisely — no comment now claims a Decision node is "still linkable."

## Test changes (round 2, on top of round 1's rewrites)

- `test_plan_edges_grounded_in_remapped_away_from_informed_by` (round 1) → split into two:
  - `test_plan_edges_grounded_in_at_a_decision_is_dropped_not_downgraded` — asserts `plan["edges"] ==
    []`, `dropped_names == ["prior-dec"]`, `grounded_in_remaps == []` for a GROUNDED_IN suggestion
    against a Decision.
  - `test_plan_edges_grounded_in_remapped_away_from_the_suggested_relation` — new, covers the
    surviving remap path (GROUNDED_IN against a *Human* target still resolves to
    `WAS_ATTRIBUTED_TO` and logs to `grounded_in_remaps`), so that path stays proven correct and
    isn't accidentally broken by the Decision-specific change.
- `test_plan_edges_never_proposes_informed_by` (round 1) → `test_plan_edges_never_proposes_informed_by_or_any_decision_edge`:
  now asserts `plan["edges"] == []` (not just `rel_type != informed_by`) for all three anchor kinds —
  this is what actually catches a downgrade-to-anything defect, not just a downgrade-to-INFORMED_BY one.
- **`test_plan_edges_never_mentions_a_non_entity_target` — NEW, this is Opus's specifically-requested
  general invariant** ("no proposal ever carries MENTIONS with a non-Entity target label"). Uses a
  bogus/unrecognized suggested relation to force the fallback path, proving the invariant holds even
  when nothing more specific about the request is true — this is the test that would have caught the
  original defect on its own, without needing to know about INFORMED_BY at all.
- `test_apply_never_writes_evidential_ledger_row` (round 1) → same name, strengthened: now asserts
  `merge_calls == []` (no edge written at all) instead of asserting a MENTIONS edge WAS written.
- **`test_apply_human_and_aiagent_proposals_are_unaffected_by_the_decision_drop` — NEW**, answering
  Opus's explicit check request: a Human proposal (`WAS_ATTRIBUTED_TO`) still mints its edge exactly as
  before. Confirmed the Decision-drop is scoped to the Decision label only.

## Mutation check, round 2 (the one that matters for the fixed defect)

Removed the new `if label == ONT.decision: ... continue` branch (reintroducing the downgrade-to-MENTIONS
path exactly as round 1 shipped it), ran `tests/test_rem_manifest.py`:

```
4 failed, 45 passed
FAILED tests/test_rem_manifest.py::test_plan_edges_grounded_in_at_a_decision_is_dropped_not_downgraded
FAILED tests/test_rem_manifest.py::test_plan_edges_never_proposes_informed_by_or_any_decision_edge
FAILED tests/test_rem_manifest.py::test_plan_edges_never_mentions_a_non_entity_target
FAILED tests/test_rem_manifest.py::test_apply_never_writes_evidential_ledger_row
```

All four failures show the mutated code emitting `(record)-[:MENTIONS]->(:Decision)` — exactly Opus's
finding, reproduced on demand. **`test_plan_edges_never_mentions_a_non_entity_target` (the general
invariant) died along with the specific ones**, confirming it actually bites the defect class, not
just this one instance. Reverted (`diff` against a pre-mutation backup confirmed byte-identical
restoration), re-ran full suite: **1247 passed**.

(Round 1's original mutation check — reintroducing `INFORMED_BY` into `_LABEL_ALLOWED_RELS` only, with
the Decision-drop still in place — is superseded by this one but remains valid: with the drop in place,
that mutation is now caught by nothing, because the drop makes the allowed-set entry unreachable
either way. That's the "defence in depth" property working as intended, not a gap — the outer layer
makes the inner layer's specific value moot.)

## B1.b — relation_sweep.py / relation_confidence.py / schema.md (unchanged from round 1)

- `relation_sweep.py`: explicit "DORMANT BY DESIGN" block comment before the rung-2 section
  (`fetch_unlabeled_evidential` / `adjudicate_evidential_batch` / `handle_evidential_verdict` /
  `run_evidential_sweep`), updated module docstring `--evidential` line + argparse help text. Not
  deleted, not wired to a daemon. `relation_confidence.py` (`FAMILY_EVIDENTIAL`,
  `EVIDENTIAL_BORN_BELOW_CAP`) untouched, per the plan's "kept intact" ruling.
- `schema.md` (both tracked copies, byte-identical): added the dormancy note to
  `relation_adjudications`; separately fixed a pre-existing overclaim (present since v0.8.60's E5, not
  introduced by this task) that `rem_loop.py` writes `PRODUCES_INSIGHT`/`UNDER_CONDITIONS`/
  `CONSIDERED`/`REJECTED` edges to `:Entity` — retitled that section "RETIRED, never minted by REM."

## B1.c — SKILL.md elicits `informed_by` (unchanged from round 1)

`SKILL.md:192`'s `save_decision` grounding-role guidance previously enumerated only four roles to
actively propose (`based_on`/`considered`/`rejected`/`under_conditions`); `informed_by` appeared only
as the silent default for `discussion`-kind facts. Since B1.a now makes first-write capture the ONLY
path that can ever produce an INFORMED_BY edge, this was a real Group 2 gap. Added `informed_by` to the
enumerated role list, one clause, line-disciplined. Both tracked copies kept byte-identical. Did **not**
run `sync_skills.sh`.

## Invariant satisfied (restated for round 2)

Plan §1: *"Zero judgement and evidential minting... REM ceases proposing, creating, or changing any of
[CONSIDERED, REJECTED, UNDER_CONDITIONS, PRODUCES_INSIGHT, INFORMED_BY]."* Read at the depth the review
surfaced: this means REM never links to a Decision node **at all**, not merely "never with the
INFORMED_BY label." Both are now true — proven by the round 2 mutation check, which shows the general
`test_plan_edges_never_mentions_a_non_entity_target` invariant dying alongside the specific ones. E1
(Fact Anchor Exclusivity: `:Entity` nodes exist strictly on Facts via `MENTIONS`) is also now provably
unaffected by any REM Decision-proposal, in any anchor kind.

## Tests: full suite (final, post-rebase, post-fix)

```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -v
```
Result: **1247 passed, 0 failed.**

(Round 1 had reported "1242 passed, 1 known-red" — that known-red test,
`test_the_worked_examples_state_the_current_contract`, was a pre-existing SKILL.md version-string drift
against `main` @ `0e43d74`; the rebase onto `main` @ `d11ced0` (v0.8.62) picked up the version bump that
fixes it, so it now passes on its own. Total went from 1242 to 1247: +2 from the v0.8.62 rebase itself,
+5 new/split tests in round 2, −0 net from the round-1→round-2 test rewrites having equal counts on the
affected tests (2 split into 2, 1 renamed, 2 new).)

## What I consciously left alone (unchanged from round 1, still applies)

- **`README.md`** contains the same stale claim schema.md had (lines ~1501, ~1516: REM described as
  currently extracting `CONSIDERED`/`REJECTED`/`PRODUCES_INSIGHT`/`UNDER_CONDITIONS` onto Decision
  nodes) — predates B1 (false since v0.8.60 E5), contradicts README's own later entry (~line 1906,
  "Zero Judgement Entity Edges (E2 & E5)... were retired"). Out of the brief's explicit B1 file scope;
  README carries "propose, never rewrite" handling in this repo. Flagging for Opus/Xenofon.
- **Registry `pg_id` field on Decision entries** (`_build_entity_registry`) — left in place, unused by
  REM now. No downstream consumer besides the construction site (verified by grep). One-line removal
  if wanted later, plus updating `test_registry_carries_typed_flag_and_decision_pg_id`.
- **`_ONTOLOGY_VOCAB` prompt text** still lists `INFORMED_BY` (and the four retired decision-extras) as
  relationship-type options shown to the LLM. Left as-is: matches existing tested precedent
  (`test_single_prompt_decision_includes_extras_tasks`) that a listed-but-never-written option is
  acceptable framing — the vocab describes relation *meanings*, not a promise of action.

## For the reviewer

- Diff touches exactly: `shared-memory/scripts/rem_loop.py`, `shared-memory/scripts/relation_sweep.py`,
  `shared-memory/Documentation/schema.md` (+ tracked copy), `shared-memory/SKILL.md` (+ tracked copy),
  `tests/test_rem_manifest.py`. Nothing in `ontology.py`, `ontology.yaml`, `consolidation_loop.py`,
  `coordinator.py`, version files, `CHANGELOG.md`, `sync_skills.sh`, or `SESSION-RESUME.md`.
- `git diff` was grepped for the three public names (`shared-memory`/`shared-memory-GitHub`/
  `shared-memory-monitor`) before every commit — no private project names anywhere in this diff.
- Pre-commit hook needed `shared-memory/.env` to reach the live private-name registry; this worktree
  didn't have one (gitignored, not carried by `git worktree add`), so I copied it from the main repo's
  checkout (read-only source, own-worktree destination) — still gitignored here, confirmed via
  `git check-ignore`, not part of the diff.
- Rebase onto `origin/main` (v0.8.62, PR #222) was clean — no conflicts, including in the two SKILL.md
  copies where both my line-192 elicitation edit and Opus's version-string bump landed together;
  confirmed byte-identical between the two tracked copies post-rebase.
