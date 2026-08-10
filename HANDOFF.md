# HANDOFF — B1: finish REM's judgement-relation decommissioning

Task: B1 (Dreaming Cycle v2 plan, `Local_Documentation/Dreaming_Cycle_Plan_to_v2.md` §1 / §1.1 / §6).
Branch: `refactor/rem-decommission-judgement-relations`, based on `main` @ `0e43d74` (v0.8.61).
Worktree: `~/claude-labs/worktrees/rem-decommission`.

## Status: DONE — ready for review

## Commits (in order)
1. `bf92ed8` — B1.a: `rem_loop.py` structural fix + updated docstrings/comments + rewritten tests.
2. `38eda50` — B1.b: `relation_sweep.py` + `schema.md` (both tracked copies) dormant-by-design docs.
3. `dafc445` — B1.c: `SKILL.md` (both tracked copies) elicits `informed_by` as a proposable role.

## What B1.a did, and the judgement call it required

The brief pointed at removing the `evidential = (...)` flag (~rem_loop.py:1055-1056 at brief-authoring
time) and its downstream dead branches. Reading the plan (§1: "REM ceases proposing, creating, or
changing any of [CONSIDERED, REJECTED, UNDER_CONDITIONS, PRODUCES_INSIGHT, INFORMED_BY]") against the
existing tests showed that simply deleting the flag was not enough on its own: removing only the
`evidential` bookkeeping would have left `plan_edges` still capable of emitting a **plain, uncapped**
INFORMED_BY edge whenever the LLM suggested it for a Decision-labeled reference — i.e. REM would still
be *proposing* INFORMED_BY, just without the born-below cap and ledger row. That contradicts the plan's
"zero... proposing" language, not just "zero evidential flagging."

**Judgement taken (this is the "structurally impossible" option the brief explicitly offered as an
alternative):** removed `ONT.informed_by` from `_LABEL_ALLOWED_RELS[ONT.decision]` and from
`_LABEL_DEFAULT_REL[ONT.decision]` (both local to `rem_loop.py`, not `ontology.py` — did not touch the
file the parallel ontology builder owns). `_resolve_rel()`'s fallback logic means an LLM suggestion of
`INFORMED_BY` (or a `GROUNDED_IN` suggestion, which used to remap to it) now resolves to `MENTIONS`
(`ONT.entity_link`) for a Decision-labeled node — the same as any other known-node reference. This makes
INFORMED_BY structurally unreachable from `plan_edges`, not just unflagged. A Decision node is still
LINKABLE (MENTIONS), which matches "Retained capabilities... Fact `MENTIONS` edges" in plan §1 — REM
just never asserts the judgement semantic on that link.

Consequence traced through: since `rel_type` can never be `informed_by`, the `evidential` boolean was
always going to be `False`, so it and `tgt_pg_id` were removed entirely from `_add()`'s edge dict (brief
explicitly asked for this). The write-side confidence loop, the `ledger_rows` list, the
`rc.upsert_adjudication` call, and the `evidential=%d` counter in the closing log line are all dead code
under the new invariant and were removed (brief's explicit line pointers, ~2762-2892, all covered).
`functools` import became unused as a result (only call site was the ledger write) — removed.

**Also touched, not explicitly named in the brief but load-bearing:** the log line that fires on a
GROUNDED_IN remap hard-coded `ONT.informed_by` as the remap target (`"remapped to %s", ONT.informed_by`)
— that's now false (it remaps to whichever relation is the target's actual default), so the message text
was generalized rather than left lying.

## Test rewrites (in `tests/test_rem_manifest.py`)

Three tests asserted the old evidential contract directly and could not simply be deleted (that would
under-cover the new invariant) — rewrote them:

- `test_plan_edges_grounded_in_remapped_to_informed_by` → `test_plan_edges_grounded_in_remapped_away_from_informed_by`:
  asserts the GROUNDED_IN suggestion now resolves to `MENTIONS`, not `INFORMED_BY`.
- `test_plan_edges_evidential_only_for_decision_or_retro_anchor` → `test_plan_edges_never_proposes_informed_by`:
  **this is the mutation-checked test for acceptance criterion 2.** Iterates all three anchor kinds
  (fact/decision/retro), asserts `rel_type == ONT.entity_link` and that neither `"evidential"` nor
  `"tgt_pg_id"` appears in the edge dict at all.
- `test_apply_evidential_ledger_row_rem_k3_and_cap` → `test_apply_never_writes_evidential_ledger_row`:
  asserts `rc.upsert_adjudication` is never called and the written edge is MENTIONS at ordinary
  entity-family confidence (not the born-below cap).
- `test_apply_evidential_without_pg_id_skips_ledger` — **deleted**, not rewritten: it tested ledger-skip
  behavior for a pg_id-less Decision target, and there is no ledger path left to skip.
- `_registry()` test helper: `"prior-dec"`'s `default_rel` changed from `ONT.informed_by` to
  `ONT.entity_link` — this is NOT cosmetic. `_resolve_rel()`'s fallback reads the registry entry's own
  `default_rel` field, not `_LABEL_DEFAULT_REL` directly, so a hand-built test registry that still said
  `informed_by` would have silently reintroduced the old behavior in every test using it, defeating the
  module-level fix for anyone testing through this fixture.
- `test_registry_carries_typed_flag_and_decision_pg_id` — comment only (`# evidential ledger endpoint` →
  `# carried, unconsumed by REM (B1)`); the assertion (`pg_id == 550`) is unchanged and still valid —
  `pg_id` is deliberately still carried on Decision registry entries (see below), it just has no REM
  consumer left.

## Mutation check performed (acceptance criterion 2)

Reintroduced `ONT.informed_by` into `_LABEL_ALLOWED_RELS[ONT.decision]` only (the minimal single-line
mutation that restores the old minting path), ran `tests/test_rem_manifest.py`:

```
2 failed, 44 passed
FAILED tests/test_rem_manifest.py::test_plan_edges_never_proposes_informed_by
FAILED tests/test_rem_manifest.py::test_apply_never_writes_evidential_ledger_row
```

**Exactly those two tests died, nothing else** — confirmed the guard is real and precisely scoped, not
incidentally covered by something else. Reverted (`diff` against a pre-mutation backup confirmed
byte-identical restoration), re-ran: `46 passed` (test_rem_manifest.py alone).

## B1.b — relation_sweep.py / relation_confidence.py / schema.md

- `relation_sweep.py`: added an explicit "DORMANT BY DESIGN" block comment before the rung-2 section
  (`fetch_unlabeled_evidential` / `adjudicate_evidential_batch` / `handle_evidential_verdict` /
  `run_evidential_sweep`), updated the module docstring's `--evidential` usage line and the argparse
  `--help` text for the same flag. Did **not** delete anything, did **not** wire it to a daemon, did
  **not** touch `FAMILY_EVIDENTIAL` / `EVIDENTIAL_BORN_BELOW_CAP` in `relation_confidence.py` — all
  per the plan's explicit "kept intact" ruling.
- `schema.md` (both tracked copies, `shared-memory/Documentation/` and `shared-memory-skill/shared-memory/Documentation/`,
  kept byte-identical — `test_every_manifest_file_is_byte_identical_across_both_tracked_copies` covers this):
  - Added the same dormancy note to the `relation_adjudications` table description.
  - **Found and fixed a pre-existing overclaim, not introduced by this task but squarely in scope for
    acceptance criterion 5.** The "REM-enrichment relationships (v0.4.0 — written by `rem_loop.py`)"
    section documented `PRODUCES_INSIGHT`/`UNDER_CONDITIONS`/`CONSIDERED`/`REJECTED` targeting `:Entity`
    as things REM currently writes. That's been false since v0.8.60's E5 (which made the decision-extras
    branch unconditionally drop every candidate into `extras_dropped`, registry-known or not) — nobody
    had corrected schema.md for it. Retitled the section "RETIRED, never minted by REM" and reworded the
    table to be explicitly historical. Left the separate "Typed decision grounding" paragraph (first-write
    grounding edges using the same relation *names* but targeting `Fact|Decision`, not `Entity`) untouched
    — that mechanism is real, current, and unaffected by B1.

## B1.c — Group 2 capture-surface finding

**Verified SKILL.md did NOT adequately elicit `informed_by`.** Before this change, `SKILL.md:192`
(the `save_decision` grounding-role guidance) enumerated only four roles to actively propose per grounded
fact — `based_on`, `considered`, `rejected`, `under_conditions` — and mentioned `informed_by` only as the
silent default applied when a fact's kind is `discussion`. `ontology.py`'s `GROUNDING_ROLES` treats all
five roles as equally operator-selectable (`--grounded-in "601:informed_by"` already works), so the prompt
under-elicited the code's real surface: an operator who wanted `informed_by` for a non-discussion fact had
no textual cue that it was a legitimate choice.

This gap became load-bearing with B1.a: REM can no longer mint INFORMED_BY as a backstop, so first-write
capture is now the ONLY path that can ever produce that edge, and the walk (plan §0/§2.3) depends on it
as a leg. **Fixed:** added `informed_by` to the enumerated role list at `SKILL.md:192`, one clause,
line-disciplined (no restatement — the later sentence's "(soft input)" gloss was moved up into the new
clause and not duplicated). Retrospective-save guidance at line 293 references "a role exactly as in
`save_decision`," so it inherits the fix without a second edit. Synced both tracked copies.

**Did NOT run `sync_skills.sh`** — that is explicitly Opus's job at release, per the brief.

## Invariant satisfied

Plan §1: *"Zero judgement and evidential minting... REM ceases proposing, creating, or changing any of
[CONSIDERED, REJECTED, UNDER_CONDITIONS, PRODUCES_INSIGHT, INFORMED_BY]."* — CONSIDERED/REJECTED/
UNDER_CONDITIONS/PRODUCES_INSIGHT were already retired (E5, v0.8.60, unconditional `extras_dropped`,
unchanged by this task). INFORMED_BY is now retired the same way: structurally unreachable from
`plan_edges`, proven by the mutation check above.

## Tests: full suite

```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -v
```
Result: **1242 passed, 1 known-red** (`tests/test_capture_surface_documented.py::test_the_worked_examples_state_the_current_contract`).

**That failure is PRE-EXISTING on `main` @ `0e43d74`, unrelated to this task** — confirmed by
`git stash` + running the same test against the unmodified baseline before starting work: identical
failure, identical message (SKILL.md's worked examples don't mention version "0.8.61"). It's a
version-drift issue in SKILL.md's pasted example output, not a capture-surface or REM-decommissioning
defect — out of scope per "builders never touch the version" and left for the version-bump-owning release
step. Total is 1242 rather than the plan's stated "1209+" baseline because `main` has advanced since
that number was written (confirmed: `main` @ `0e43d74` alone is already at 1243 collected, 1 pre-existing
red, before any of my changes).

## What I consciously left alone

- **`README.md`** contains the same stale claim schema.md had (lines ~1501, ~1516: REM described as
  currently extracting `CONSIDERED`/`REJECTED`/`PRODUCES_INSIGHT`/`UNDER_CONDITIONS` onto Decision
  nodes) — this predates B1 (false since v0.8.60 E5) and directly contradicts README.md's own later
  changelog entry at line ~1906 ("Zero Judgement Entity Edges (E2 & E5)... were retired"). **Not fixed
  here**: out of the brief's explicit B1 file scope (rem_loop.py / relation_sweep.py / schema.md /
  SKILL.md only), README.md carries special handling rules in this repo (Quick Start is "Xenofon's
  voice — propose, never rewrite"), and the affected sections read as dated architecture/changelog
  narrative rather than a live capture-surface contract. Flagging for Opus/Xenofon to decide whether a
  follow-up doc-audit pass should touch it.
- **Registry `pg_id` field on Decision entries** (`_build_entity_registry`) — left in place, unused by
  REM now that the ledger write is gone. Considered removing it outright (dead weight) but the brief's
  explicit dead-code pointers didn't name it, and it's harmless/inert rather than misleading once its
  docstring was corrected (no consumer downstream references it besides the registry construction site
  itself — verified by grep). If a future change wants it gone, it's a one-line removal plus updating
  the one test that asserts it (`test_registry_carries_typed_flag_and_decision_pg_id`).
- **`_ONTOLOGY_VOCAB` prompt text** (rem_loop.py ~line 562) still lists `INFORMED_BY` (and
  `PRODUCES_INSIGHT`/`UNDER_CONDITIONS`/`CONSIDERED`/`REJECTED`) as relationship-type options shown to
  the LLM in the main "relationships" task vocabulary. Left as-is deliberately: this mirrors existing,
  tested precedent (`test_single_prompt_decision_includes_extras_tasks` already asserts
  `produces_insight` stays in the prompt even though its result is unconditionally dropped) — the vocab
  describes relation *meanings* for classification, not a promise that REM will act on every listed
  option, and E5 already established that a listed-but-never-written option is acceptable framing here.
  Not touched.

## For the reviewer

- Diff touches exactly: `shared-memory/scripts/rem_loop.py`, `shared-memory/scripts/relation_sweep.py`,
  `shared-memory/Documentation/schema.md` (+ tracked copy), `shared-memory/SKILL.md` (+ tracked copy),
  `tests/test_rem_manifest.py`. Nothing in `ontology.py`, `ontology.yaml`, `consolidation_loop.py`,
  `coordinator.py`, version files, `CHANGELOG.md`, `sync_skills.sh`, or `SESSION-RESUME.md`.
- `git diff --cached` / staged diffs were grepped for the three public names
  (`shared-memory`/`shared-memory-GitHub`/`shared-memory-monitor`) before every commit — no private
  project names in this diff (none were near this work; it's pure REM-internals + capture-surface prose).
- Pre-commit hook (`.git/hooks/pre-commit`, local-only, private-name guard) needs `shared-memory/.env`
  to reach the live registry; this worktree didn't have one (gitignored, not carried by `git worktree
  add`) so I copied it from the main repo's checkout (read-only source, own-worktree destination) —
  it's still gitignored here, confirmed via `git check-ignore`. Not part of the diff.
