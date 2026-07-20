# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.7.7] — 2026-07-20

Follow-through on the previous release. The non-run counters it added were served by the API but
never rendered by the CLI, so an operator reading `status` saw a run count with no sign of what
else had happened — the same shape of misleading headline 0.7.5 and 0.7.6 were about.

### Fixed

- **The CLI status report now shows the non-runs beside the run count**: `non-runs N deferred/M
  idle`, omitted entirely when both are zero so a healthy cycle prints no noise. Without it,
  "9 runs/24h" concealed that 7 further wake-ups had been deferred for a busy inference slot.
- **Operator triage guidance in `AGENTS.md` brought up to the code.** It still described a single
  consolidation cycle and sent the reader to the reasoning LLM first. It now documents the
  per-type block, that `stalled` is an OR whose actionable field is `stalled_types`, that
  `eligible_clusters: 0` means *idle, not broken*, and the triage order that follows from that.

---

## [0.7.6] — 2026-07-20

Scheduling. The consolidation daemon decided it had work to do by watching saves arrive — but a
save means a record was *written*, and consolidation's work needs records *enriched* into a
cluster dense enough to fold. Those are different questions, and the daemon was asking the wrong
one: it would take the exclusive inference slot on the strength of a notification and then
discover there was nothing eligible. Three further behaviours carried the same confusion, and a
fourth defect in how the cycle was *reported* meant a correctly-idle daemon looked broken.

Landed as three ordered commits — instrumentation first, then the predicate, then the clocks —
so a regression bisects to one of them.

### Fixed

- **Consolidation due-ness now reads the durable ledger, not the in-memory notification set.**
  The predicate is the count of fact rows at `rem_reviewed` in `neo4j_outbox` — records enriched
  but not yet folded — re-read every `NREM_ELIGIBILITY_RECHECK_SEC` (default 60 s, one cheap
  indexed read). Below the density threshold no cluster can exist, so the cycle is not due and
  does not compete for the inference slot. The predicate already existed and was already read
  *correctly*, but only on the periodic sweep's slow path; the fast path used the wrong one.
- **The cycle no longer consumes its own entry points.** They come from that same durable
  backlog, so a run that folds nothing leaves the work exactly where it found it. The previous
  cycle cleared its pending set *before* finding clusters, and the no-cluster path returned
  without requeueing (re-queueing was reachable only from an exception handler), so a no-op run
  discarded the facts it had been asked about until an unrelated save re-triggered it.
- **An unreadable ledger fails closed, visibly.** The previous observation is kept, so a
  transient database blip neither invents work nor rearms the backstop at zero — and a
  `eligibility_read_failed` deferral is recorded, because a daemon acting on a stale view
  reports "not due", which is precisely what a daemon with nothing to do reports. Without the
  record, an operator cannot tell a quiet system from a blind one.
- **The consolidation idle clock can see the whole system.** It was written in exactly one place
  — the notification handler — so the clock deciding "the system is quiet" was blind to the
  enrichment daemon's own inference calls, and consolidation was *guaranteed* to become due
  partway through any long batch. It is now also refreshed while the LLM pool is busy (probed
  every `NREM_POOL_PROBE_SEC`, default 15 s, fail-open). No timer value can fix a clock that
  cannot see the largest consumer of the resource it guards. The hard backstop is correspondingly
  re-anchored on how long the backlog has been **eligible**, so an honest clock is not traded for
  indefinite deferral.
- **The periodic hygiene sweep keeps its own, notification-only clock.** The two consumers want
  different things from "quiet": consolidation must not fire while the exclusive slot is held,
  but the sweep — backfill, reconciliation, the insight pass — has no backstop, so gating it on a
  clock a busy pool can hold open indefinitely would let a loaded system suppress maintenance
  forever. Splitting the clocks keeps each honest about what it measures.
- **A false-positive stall verdict: an idle cycle can now say it is idle.** Fact consolidation
  opened a run row only when it already had clusters to fold, so it recorded
  `eligible_clusters = NULL` on every run — and `NULL` is not "no data" to the health surface, it
  is the trigger for a looser fallback backlog. A cycle whose own gate correctly reported nothing
  eligible was therefore reported **stalled**. Every run now records its census (the count after
  the `(entity, domain)` re-split, the gate it actually folds on), plus a throttled `idle` run
  row when that count is zero.
- **`runs_24h` counted things that were not runs.** Measured live: the insight cycle reported 16
  runs in 24 h, of which **7 were deferrals** — a 78 % inflation on the number sitting beside
  `cycle_seconds_avg`, which is the per-unit price a slot allocator divides by. It now counts
  runs of the cycle body only.

### Added

- `deferred_24h` and `idle_24h` per cycle type on `GET /memory/telemetry` and in the CLI status
  report, so the non-runs are still visible rather than silently folded into the run count.
- `NREM_ELIGIBILITY_RECHECK_SEC` and `NREM_POOL_PROBE_SEC`, both documented in `.env.example`.
- `tests/test_nrem_eligibility.py` (26 cases) covering the pure due-ness rule, the durable
  probe's rate limiting / eligibility clock / fail-closed-and-recorded behaviour, both clocks,
  and the cycle's entry-point and idle-record handling. Mutation-checked: seven mutations, each
  killed by its own test.

---

## [0.7.5] — 2026-07-20

Observability. Consolidation runs as two distinct cycle types with very different costs and
cadences, but the health surface described them with a single set of headline numbers taken
from one of them. A cycle type that was folding normally could therefore be reported as
stalled for days, because the headline was reporting its idle sibling's age under a label that
claimed to describe consolidation as a whole.

### Fixed

- **The consolidation headline now describes every cycle type, and says which one it came
  from.** `last_success_age_seconds` reports the most recent success across cycle types and is
  tagged with `last_success_cycle_type`; `last_outcome` and `last_deferred_reason` follow
  whichever type actually ran most recently instead of a hardcoded one. `stalled` remains an OR
  across types — a stalled cycle must still raise the flag — but the new `stalled_types` names
  which ones, so the flag can be acted on rather than merely noticed.

### Added

- **Per-cycle-type cost and throughput** on `GET /memory/telemetry` and in the CLI status
  report: `runs_24h`, `cycle_seconds_avg` (averaged over completed runs only, so deferrals and
  in-flight runs cannot flatter it), and `folds_succeeded_24h` / `folds_attempted_24h`. The
  pre-existing whole-cycle timer is skewed by contention for the shared inference slot and
  cannot price either cycle type; these can.

### Notes

- Additive for clients: every previous field keeps its name and type. Readers that consume only
  the documented fields see corrected values rather than a changed contract, so the API version
  is unchanged. Older gateways that do not send `stalled_types` still render a plain stall flag.

---

## [0.7.4] — 2026-07-20

Correctness at the API boundary. A record id is unique only *within its table* — original
records and synthesised narratives are stored separately and numbered independently — so the
same integer names two unrelated real records. Inside the system this was always safe: every
path that turns an id into content is scoped to one kind of record. But search returned the id
under the same field name for both, so an id taken from a narrative and handed back to a
lookup resolved against the wrong store and returned a confident, unrelated record.

### Added

- **Every record reference is now qualified with its type.** Search results carry
  `record_type` and a `ref` (`fact:816`, `summary:87`) alongside the existing id, and the
  lineage lookup accepts either form. Quote the `ref` and a reference cannot resolve to the
  wrong record. Asking for a narrative by reference now returns its own identity and the
  records it was synthesised from, each already qualified.
- A reference naming the wrong type returns a not-found error **that names the right
  reference**, instead of a plausible wrong record.

### Notes

- A bare id is still accepted and still means the original-records store, so existing callers
  and scripts are unaffected. That remains the one place the ambiguity survives, and it is now
  documented as such rather than being an unstated assumption.
- Chosen over renumbering both stores onto a single global sequence, which would have required
  an irreversible rewrite of every stored reference to close something this closes additively.

---

## [0.7.3] — 2026-07-20

Bug fix. The enrichment queue could stop draining entirely: the same records were selected
every cycle, none finished, and none was ever counted as having failed — so the safeguard
that retires a record after repeated failures could never engage. Two independent causes,
both of which produced records that were neither successes nor failures. Every safety
mechanism in this daemon keys on one or the other, so a record that was merely *never
finished* was invisible to all of them.

### Fixed

- **A graph node that cannot correspond to its record is now retired instead of recirculating
  forever.** Enrichment selects a *node* from the graph, but resolves everything after that
  from the record id — it looks the id up in the relational store, takes the record's type
  from there, and writes its result to the node that type implies. When those two disagree,
  the daemon enriches and marks one node while the node it actually selected is left
  untouched and selected again on the very next cycle. This happened wherever an earlier
  release had linked a decision to another decision it was based on: the link was attached to
  a newly created, empty node of the wrong type. Such nodes are unprocessable by construction
  and were holding queue slots no amount of work could free. The daemon now carries the
  selected node's type through the cycle, compares it against the record's real type, and
  retires any node that disagrees — flagged, kept in the graph for inspection, never deleted,
  and matched by type so its healthy counterpart can never be touched. A node whose id has no
  record at all is retired the same way. This is self-healing: an existing install clears its
  own residue on the next cycle, with no manual repair.
- **The queue now rotates, so the tail is reachable.** One counter was doing two jobs that
  need opposite rules — deciding who is processed next, and deciding who has failed too often
  to keep trying. Because it only counted failures, a record that was picked up but not
  finished counted for nothing, so selection order never changed; the same records were always
  first, and the point at which enrichment yields the reasoning backend to consolidation
  always fell in the same place. Everything behind that point was structurally unreachable.
  Selection is now ordered by a second counter that records every pickup regardless of
  outcome, written before the expensive call so an interrupted cycle still counts. Retirement
  still keys on the failure counter alone, so an infrastructure outage cannot retire a healthy
  record however many times it is picked up. Neither counter ever decreases.
- **Linking a decision to the record it was based on no longer invents an empty node.** The
  older linking path assumed any cited id referred to a fact and created one when it found no
  match. It now resolves what the id actually refers to and links to that record, creating a
  placeholder only when no record node exists yet.

### Added

- A record picked up many times but never blamed for a failure is now a distinguishable
  state — the signature of work that is being abandoned rather than failing, which previously
  looked identical to a healthy idle record.

---

## [0.7.2] — 2026-07-19

Bug fix. A review of the previous release against the running system found that its new
safeguards were individually correct but composed badly — and had quietly stopped the
background memory-building cycles. No thematic summary had been produced in over four days,
and nearly every record awaiting enrichment was accumulating failure marks it had not earned.
Each safeguard passed its own tests; nothing tested how they behaved together.

### Fixed

- **A backend outage no longer counts against the records it interrupted.** Enrichment
  processes records in batches to share the cost of a large shared prompt. When a batch call
  failed for an infrastructure reason — the reasoning backend busy or briefly unreachable —
  every record in that batch was marked as having failed, permanently lost its place in the
  batched path, and moved one step closer to being abandoned. Over five days this affected
  89 of 101 pending records, none of which had anything wrong with them. Failures are now
  classified: only a failure that says something about *the record itself* is counted
  against it.
- **Consolidation can no longer be starved of the reasoning backend.** Enrichment and
  consolidation share a single serial slot. Enrichment asks for it far more often and holds
  it for minutes, so consolidation deferred indefinitely — 2,403 deferrals against 32 runs
  in three days, none of which produced a summary. Consolidation now signals when it is
  queuing for the slot and enrichment yields its turn. The signal is held only while
  actually waiting and is released automatically if the process dies, so neither side can
  starve the other.
- **A cluster is no longer penalised twice for the same failed attempt.** One failed
  consolidation was recorded in two separate counters that are summed when deciding to give
  up on a cluster, so it was abandoned after two failures instead of three.
- **Output limits no longer silently discard oversized work.** Generation limits correctly
  reject truncated output rather than saving something incomplete, but the limit never
  widened, so anything genuinely needing more room failed identically every time and was
  eventually abandoned without notice. The limit is now widened once and retried before the
  attempt is failed.
- **Abandoned records are visible.** `GET /memory/telemetry` reports `rem_dead_lettered`,
  `rem_failing` and `rem_max_attempts`, and the CLI `status` command surfaces them. A record
  dropped from the enrichment queue still counted as "pending", so a backlog that had been
  given up on looked identical to one waiting its turn.
- **Shutdown is no longer delayed by a queued consolidation.** The wait loop now honours the
  stop signal instead of running to its full timeout.

### Configuration

New optional settings, all documented in `shared-memory/.env.example` with defaults matching
previous behaviour where behaviour did not change: `REM_TRUNCATION_RETRY_FACTOR`,
`NREM_TRUNCATION_RETRY_FACTOR`, `NREM_PRIORITY_ADVISORY_LOCK_KEY`. `NREM_FORCED_SLOT_WAIT`
now defaults to 1800s (was 300s) — the budget has to outlast the longest enrichment call or
the queue expires before the slot is ever released.

---

## [0.7.1] — 2026-07-19

Operational hardening, found by running the enrichment rebuild against real hardware for the
first time. Three of these were silent: the backup had stopped reaching its configured
destination weeks earlier, the consistency fence it relies on never actually engaged, and a
leaked pool slot could stall consolidation indefinitely. The theme is failing safely and
loudly instead of quietly producing something incomplete.

### Fixed

- **Backups were being written to local disk, unquiesced, since the environment refactor in
  0.6.0.** The backup script resolved its configuration from the repository root, a location
  that no longer holds the environment file. Finding nothing, it fell back to its own
  defaults — a home-directory destination and no admin credential — so the configured
  destination was ignored and the consistency fence was skipped outright. It now resolves the
  framework environment file first, with the old path honoured as a fallback, matching every
  other script in the project.
- **The consistency fence could never complete.** Requesting the fence blocks while the
  gateway quiets the background passes, but the request carried a fifteen-second client
  timeout against a fifteen-minute server budget, so the client always abandoned it first and
  every backup silently proceeded unfenced. The handshake now gets a budget that outlasts the
  server, and the default fence window is short enough that a nightly run cannot stall.
- **A leaked backend slot could starve consolidation permanently.** The pool's in-flight
  counter was claimed just before the block whose cleanup releases it, so a failure in between
  — or a client disconnecting early — leaked the slot for good. Because the background passes
  only run when the pool looks idle, one leaked slot made it look busy forever, with no
  recovery short of restarting the gateway. The slot is now claimed inside the protected block.
- **Truncated model output is never salvaged into stored knowledge.** A bound that produces
  incomplete saves is worse than no bound, so every bounded call now detects a
  length-terminated response and fails that unit instead of parsing it: enrichment returns
  nothing rather than a partial record, batched output drops its final interrupted line and
  accepts only strictly valid ones, verification degrades its confidence instead of denying
  unseen items, a truncated narrative never reaches the preservation gate that would have
  wrongly passed it, and no repaired verdict can reach the permanent adjudication ledger.
- **Records can no longer strand after a partial write.** A failure between marking a record
  enriched and completing its bookkeeping now reverts the mark and counts an attempt, so the
  record re-enters the queue under its retry cap instead of disappearing from every worklist.

### Added

- **Configurable reasoning model id.** The model name sent on every reasoning call was fixed
  in code, which only works with servers that ignore the field. It can now be set, so backends
  that validate model names — named-model servers, routing proxies, hosted OpenAI-compatible
  endpoints — can be addressed.
- **Configurable forwarding targets.** The embedding and reranking endpoints the gateway
  forwards to, and the single-backend fallback, are now configuration with defaults rather
  than literals in a code path. The framework no longer assumes the port layout of the machine
  it was developed on. Clients are unaffected — they still reach everything through the gateway.

### Notes

- Embedding and reranking remain a **fixed part of the model contract**. The vector dimension
  is defined in the schema, no dimension check guards it, and changing the embedding model once
  data exists requires re-embedding rather than a configuration change. Only the reasoning
  model is freely interchangeable today.

---

## [0.7.0] — 2026-07-16

The enrichment rebuild. The background enrichment pass used to see only a record's raw text —
none of the provenance the save path had carefully captured — and the typed entity-to-entity
relation layer defined in the ontology was wired to nothing, which is why entities connected
only through the records that mentioned them. This release re-charters enrichment as
**ontology-gated expressiveness with measured trust**: the first write captures what is required,
enrichment adds what the ontology permits, and confidence, adjudication, and operator calibration
are the guards that make that expressiveness safe. API version 3.

### Added

- **Universal machine-edge provenance.** Every edge the background passes mint now carries
  `asserted_by` (who asserted it), `confidence`, `model`, `run_id`, and `created_at` — stamped on
  creation only, so an edge the operator asserted or promoted is never overwritten. Pre-existing
  edges are an era-gated legacy class, always consumed at a fixed neutral weight (no retroactive
  re-scoring).
- **Capture-manifest enrichment.** The enrichment daemon assembles what the save path already
  knows about a record (its type, evidential kind, source reference, operator-named entities,
  existing edges and their asserters, outcome rating) and asks the model **only for the delta** —
  new entities, sub-types for untyped ones, a summary only when the text exceeds the storage
  threshold. A record saved before rich capture existed simply has an empty manifest, so full
  extraction remains the degenerate case — there is no legacy mode. Novel edges are scored by
  self-consistency voting (up to three passes), shifted by the source record's evidential kind.
- **Typed entity-to-entity relations via an evidence sweep** (`relation_sweep.py`). Candidate
  pairs come from co-occurrence across records, aggregated per alias component; the ontology's
  domain-range legality map — previously defined but unused — now gates every candidate in both
  directions; a batched adjudication judges each pair against the actual shared-record evidence;
  and every verdict (accept and reject) lands in a new `relation_adjudications` ledger with its
  quantitative signals. The sweep's first run doubles as the backfill over the existing graph.
- **A three-rung ladder for machine-proposed evidence links.** Enrichment may propose that a
  decision was informed by another record — cheaply, and born *below* the consumption threshold;
  a re-scoring pass (`relation_sweep.py --evidential`) re-judges survivors against both records'
  content; operator review promotes an edge to operator-asserted (or refutes it, which removes
  the machine edge and remembers the refusal). Basis-grounding (`GROUNDED_IN`) is never
  machine-minted.
- **Operator calibration as a first-class flow.** New gateway routes and client commands
  (`review-edges`, `label-edges [--promote]`) present a stratified sample of machine verdicts for
  labeling; the labels are the only calibration input. **Until a relation family has ~20 operator
  labels, its machine edges are invisible to synthesis** — thresholds act only once measured
  reliability exists, and per-band precision is reported so thresholds can be tuned per install.
- **Calibration checked before any cluster is assessed.** Consolidation fetches the calibration
  state at the start of each pass (failing closed if the ledger is unreachable) and traverses
  machine-asserted edges only when their family is calibrated and their confidence clears the
  family threshold; excluded edges are counted back to the review queue in the cycle telemetry.
- **A preservation gate on synthesis.** Fold inputs are now differentiated by record type,
  evidential kind, and date; decision folds carry each decision's typed grounding edges with
  their asserter and confidence, machine-proposed lines explicitly tagged. After generation, a
  deterministic check verifies every captured record survived into the narrative — decisions and
  retrospectives are never droppable, plain facts tolerate small paraphrase slack. One corrective
  retry names the dropped records; a second failure means the summary is **not written** and the
  work requeues. A narrative that silently drops captured knowledge never reaches the semantic tier.
- **The read surface honors everything capture writes.** Search graph expansion now anchors on
  all record types (not just facts), returns each edge's direction and full property map
  (asserter, confidence, role), surfaces record-keyed neighbors with a label, id, and snippet
  instead of silently dropping them, ranks provenance-bearing edges above bare mentions within a
  tunable cap, and walks a summary back to its sources with typed edges.

### Changed

- Enrichment no longer requests a summary for records below the storage threshold (previously it
  generated one and discarded it), and decision-alternative extras are only linked to entities the
  graph already knows — free phrases stay as record properties instead of becoming graph nodes.

### Fixed

- The consumption gate treats unstamped legacy edges as always consumable at neutral weight; an
  earlier draft routed them through the machine threshold, which would have severed every
  pre-rebuild mention edge from consolidation.

---

## [0.6.5] — 2026-07-15

Retrospectives become first-class records. A decision's outcome used to live only as an annotation
on the decision itself — unsearchable, unweighable, impossible to ground in the tests that measured
it. This release gives every retrospective its own record and graph node, a closed outcome-state
rating vocabulary, evidence grounding with the same typed roles decisions use, recency-aware
retrieval, and a one-time conversion of all pre-existing outcome annotations. Alongside it, the
background enrichment pass stops paraphrasing over curated fact text. API version 2. A full code
review and the x.y.5-cadence security review ran before this tag; the review's confirmed findings
are fixed below.

### Fixed (pre-release review of this batch)

- **The `retrospectives` and `why-to-check` query shortcuts read the new record shape.** After the conversion
  they still read rating/notes from the (now payload-free) trigger edge and returned nulls — the why-loop check
  was silently empty. Both templates now read the retrospective *record* when present and fall back to edge
  properties on installs that have not run the conversion yet.
- **A retrospective's identity now includes its target decision.** The dedup hash was computed over the notes
  alone, so identical boilerplate outcome notes on two different decisions would silently merge into one record
  (repointing the first), and a retrospective could even collide with a plain fact of identical text. The hash is
  now `(record type, target decision, notes)`; re-saving the same retrospective still dedupes. Same fix in the
  migration script.
- **The insight synthesis evidence line can no longer contradict the graph.** For groundings without an explicit
  operator role it printed a fixed default instead of the relation the write path actually chose from the fact's
  evidential kind (a discussion grounds softly as `INFORMED_BY`); it now reports the real relation in every case.
- **Retrospective saves take the same per-entity write locks as fact saves** (entity node creation stays
  serialized), and a non-existent target decision is rejected *before* the embedding is computed rather than
  after (no wasted embedder work on a typoed id).
- **The enrichment daemon refuses a fact write with no original content** instead of blanking the node and
  retrying that record forever.
- Doc sync: the agent quickstart, the LM Studio system prompt, and the example snippets now use the
  outcome-state rating vocabulary (the old free-text examples would be rejected with a 400).

### Added

- **One-time migration for pre-existing retrospectives** (`migrate_retro_edges.py`, dry-run first): converts every
  legacy outcome edge into a full retrospective record — notes embedded and searchable, the original recording
  date **backdated into `created_at`** (so recency stays honest), free-text ratings mapped onto the outcome-state
  enum with the original wording preserved (`original_rating`), recorder identity recovered where a queue row
  still held it, and the legacy self-loop deleted after each conversion. Migrated records arrive
  `rem_processed=true` — enriching historical retrospectives is a deliberate later choice, not a surprise
  backlog for the enrichment daemon.
- **Retrieval is recency-aware for outcome records.** Decisions and retrospectives are scored by the reranker
  **with their recording date prepended** to the text it sees — recency cannot be weighed if it is invisible —
  and when several retrospectives of the *same* decision surface in one result set they are presented
  newest-first: the newest retrospective is the decision's current verdict, older ones are history. Everything
  else keeps pure relevance order; there is deliberately **no time-decay scoring** (uncalibratable at this
  volume). Search results for Tier-1 records now expose `pg_id` and `created_at`.

### Changed

- **A retrospective is now a full record, not an annotation (API v2).** Recording a decision's outcome used to
  write only an edge on the decision — the notes (the system's only *measured hindsight*) were invisible to
  semantic search, nothing could cite the evidence behind a verdict, and the machinery could read only that an
  outcome existed, never what it was. `save_retrospective` now mints a record with its own id: the notes are
  embedded and searchable, the graph gains a `Retrospective` node behind the decision's `HAD_OUTCOME` trigger
  edge (kept — the trigger semantics are unchanged), and the retrospective can be **grounded in the facts that
  measured the outcome** with the same typed roles decisions use (`--grounded-in "601:based_on"`, `asserted_by`
  recorded) — a test-grounded decision finally gets a test-grounded retrospective, structurally. The record
  inherits its decision's project and derives its evidential kind from `--source-ref` (a test reference makes it
  `tested`). New capture flags: `--grounded-in`, `--entities`, `--source-ref`, `--elicited`.
- **The retrospective rating is now a closed outcome-state enum**: `validated` / `mixed` / `refined` / `pending` /
  `reversed` — validated at the gateway and the client. The live graph had accumulated 15+ improvised free-text
  ratings that nothing could threshold on; states (not grades) keep the structural verbs — `reversed` still
  drives the supersession cascade — while the nuance and the measured delta stay in the notes, which is what
  insight synthesis quotes. **API_VERSION bumped to 2** (the response now returns the retrospective's own `pg_id`).
- **The background enrichment pass no longer overwrites a fact's text with its own summary.** Enrichment used to
  replace every fact's graph-tier content with an LLM paraphrase — including short, deliberately curated facts,
  where a paraphrase can only lose signal and inject style drift into every later synthesis. A fact's node now
  carries the **original text verbatim** (up to the graph-tier cap); a summary is stored *alongside* it
  (`rem_summary`) only when the source text exceeds the cap (`REM_SUMMARY_THRESHOLD`, default 2000 chars), and
  consolidation reads the summary only where one exists. Decisions keep their rationale as before; the new
  retrospective records keep their notes.
- **Consolidation is transition-tolerant for the retrospective-as-record change.** Retrospectives are becoming
  first-class records (own id, searchable, groundable in evidence) instead of edge annotations on a decision. The
  readers now accept **both shapes**: the legacy self-loop edge and the new `Retrospective` node behind the same
  `HAD_OUTCOME` trigger edge. When synthesising an insight, a decision's **latest retrospective is treated as its
  current verdict** and enters in full — for new records with the authoritative notes plus an evidence line naming
  the facts it is grounded in and how they were established — while earlier retrospectives compress to rating+date
  history lines, so the prompt grows linearly instead of with the whole outcome archive.
- **The enrichment daemon treats retrospectives as a third record kind** (alongside facts and decisions):
  non-destructive, entity linking from the notes, no decision-only extras. New framework vocabulary: the
  `Retrospective` node label and the retrospective **outcome-state rating enum**
  (`validated / mixed / refined / pending / reversed`) — framework-defined, never configurable.

- **The skill now actively elicits the grounding *role*, completing the v0.6.4 capture surface.** `save_decision`
  guidance proposes a role for each grounded fact — defaulting from the fact's kind (a `discussion` → soft
  `INFORMED_BY`, else `GROUNDED_IN`) — for the operator to confirm or override, and passes the confirmed role so it
  is recorded as **operator-asserted**. A bare id still falls to the system default. (Shipping the typed-grounding
  code without this left the capability unreachable in practice — see the capture-surface release gate.)

## [0.6.4] — 2026-07-14

### Changed

- **A decision now links to each grounding fact by the *role* that fact played, not one flat edge.** Previously
  every fact a decision rested on was attached by a single undifferentiated `GROUNDED_IN`, so a later synthesis
  could not tell the decision's *evidence* from an alternative it *considered*, one it *rejected*, or a *constraint*
  it accepted. First write now records the role — `GROUNDED_IN` (basis), `CONSIDERED`, `REJECTED`,
  `UNDER_CONDITIONS`, or `INFORMED_BY` — chosen by the operator (`--grounded-in "42:considered,43,44:rejected"`) or,
  when unspecified, defaulted from the fact's kind: a `discussion` grounds *softly* as `INFORMED_BY`, everything else
  as hard basis. The default is **advisory**, never enforced — an explicit operator role always wins — and every
  edge records **`asserted_by`** (`operator` vs `system_default`) so a summary can weight what the operator actually
  asserted over what the system inferred, and nothing is silently rewritten.

### Fixed

- **Grounding a decision in another decision no longer creates an empty placeholder node.** The first-write grounding
  edge used to `MERGE` a `Fact` by id unconditionally; when the grounded id belonged to a `Decision`, that produced a
  hollow shadow `Fact` the real node never filled, and a summary hopping the edge reached nothing. Grounding now links
  the **real** record across labels (fact *or* decision), so cross-record provenance is intact.

## [0.6.3] — 2026-07-13

### Added

- **`created_at` on `technical_docs`** (migration 015) — a server-stamped creation timestamp for recency-aware
  reranking (the row `id` gives creation *order*, but not elapsed time). Backfilled from the write-ahead log where
  recoverable; `NULL` where the original time is unknown (older/consolidated rows). Additive; indexed.
- **Capture-quality telemetry + write-time elicitation.** A section on `/memory/telemetry` reports how completely
  each record carries its high-value fields (a fact's source; a decision's rejected alternatives, its confidence,
  and the facts it rests on), how often those fields were actively asked of the operator, which metadata keys are
  in use but not yet mapped to the graph, and entity-resolution volume. A companion skill has agents *ask* for
  those fields at save time — the operator has a say on decisions and a quick confirm on facts — phrasing the
  rationale as an [ADR](https://adr.github.io/) Y-statement. `save_decision` gains `--grounded-in` and `--elicited`.
- **A fixed decision-capture core, and a configurable domain vocabulary.** How the framework handles decisions —
  the facts they are grounded in, alias-linked entities (variants are *linked*, never merged), and idle-time
  consolidation into higher-level insights — is now pinned in code and no longer read from `ontology.yaml`; the
  config file carries only *your* domain vocabulary (the entity types and relationships specific to your work), so
  changing it never disturbs how decisions consolidate. A decision's first write now records the facts it rests on
  as `GROUNDED_IN` graph edges, and its confidence and rejected alternatives as node properties (instead of
  flooding the graph with free-text nodes). Each fact also carries a lightweight kind — observation / discussion /
  tested / measured / researched — derived from where it came from.
- **Latency + lineage instrumentation.** Every record and pipeline stage now carries a server-stamped timestamp —
  `created_at`/`updated_at` on records, `applied_at`/`rem_reviewed_at`/`consolidated_at` on the write-ahead log, and
  a `run_id` linking each summary to the consolidation cycle that produced it (migrations 016–018). A new per-record
  **lineage** endpoint — `GET /memory/status/{pg_id}` (client: `memory_bridge.py lineage <pg_id>`) — answers *"what
  happened to this record?"*: its state, its live position in the dream cycle (each stage timestamped), and what it
  consolidated into — which summary or insight, how long that took end-to-end, in which cycle, and that cycle's
  duration. All joined gateway-side; the read-only Monitor role can reach it.

## [0.6.2] — 2026-07-09

### Added

- **`visibility` is now enforced on the read path** — the `visibility` column (`global | scope | private`) was
  stamped on save since 0.1 but never consulted at retrieval, so a `private` or `scope` row was returned to any
  caller. `handle_search` now composes a read-authorization predicate (`_visibility_filter`) into **every** read —
  Tier-1 vector search, the keyword fallback, and the Tier-3 insight/summary reads — gated by the server-verified
  `authenticated_agent`. A row is visible when `visibility='global'` (all callers), `'private'` and owned by the
  viewer's `agent_id`, or `'scope'` and the viewer asserts the matching `scope`; an anonymous caller (no verified
  identity) sees `'global'` only (fail closed). Tier-3 is gated too, so a private fact filtered from Tier-1 cannot
  leak through its community summary. **No migration and no client change:** the columns/indexes already exist
  (migration 001) and every existing row defaults to `'global'`, so nothing currently stored is hidden — behavior
  changes only for rows an operator explicitly marks. Wire contract unchanged (`api_version` stays 1); result
  *semantics* narrow. This is the gap between "one shared global brain" and per-agent/per-project private memory
  on a shared host. 8 new tests in `tests/test_visibility.py`.

### Added — prior (rolled up from Unreleased)

- **Agent-operable quickstart (`AGENTS.md` Part 1)** — `AGENTS.md` is now the **canonical agent file**, carrying an
  interview-driven setup playbook a coding agent can execute for a new user: collect data dirs, model files,
  reasoning-LLM endpoint(s) and agent list; write `shared-memory/.env` from the template (agent-generated passwords,
  never typed into chat); then preflight → compose → `init_db.sh` → `bootstrap_tokens.sh` → gateway → skill install,
  each phase gated on a verification command. Also ships day-2 runbooks (start/stop/status/upgrade/backup) and
  operator ground rules (secrets hygiene, destructive-action confirmation). `AGENT.md` becomes a thin pointer —
  the two files previously carried duplicate guidance that drifted. README Quick Start links the agent-driven path.

### Changed

- **Gemini CLI retired from the agent roster** — Antigravity CLI (`agy`) fully replaced it; Gemini CLI is no
  longer available as a CLI agent. All live docs (README, AGENTS.md, both SKILL.md copies, system-prompt.md)
  now list Antigravity CLI only; the `~/.gemini/skills/` install path keeps its legacy name, so existing
  installs and the `gemini` token identity continue to work unchanged.

### Fixed

- **Helper scripts read the wrong `.env` on fresh installs** — `preflight.sh`, `init_db.sh`, `bootstrap_tokens.sh`,
  and `migrations/apply.py` looked only at the repo-root `.env`, while `install_framework.sh` writes (and the gateway
  prefers) `shared-memory/.env`. Worst case: `bootstrap_tokens.sh` appended `AGENT_TOKENS` to a root `.env` the
  gateway never reads — leaving auth **silently disabled**. All four now resolve `shared-memory/.env` first with the
  repo-root path as the pre-0.6 fallback, matching `hive_mind_proxy.py`.
- **`alias_writer`** — a salvaged LLM `idx` such as `"4,"` (a Gemma-4 JSON slip) no longer crashes the sweep;
  the malformed entry is skipped and left for the next sweep.

## [0.6.1] — 2026-07-02

### Added — entity-resolution alias writer (ADR-017 "A")

The writer that populates the soft `ALIASES` synonym layer whose read/consume side shipped in 0.6.0 — it
never merges nodes, so every surface form stays addressable.

- **`alias_writer.py`** — a standalone periodic sweep (heavy embedding kept off the gateway event loop) that
  proposes and writes `ALIASES` edges in two tiers, calibrated to the graph's measured fact-density:
  **normalized-exact** name matches (case/format variants) are auto-accepted; the **name-cosine recall net**
  (a blocking key, ≥ 0.82) surfaces non-lexical synonyms which an **LLM adjudicates** (default *no-merge*, with
  a confidence + rationale). Edges are soft and revocable; `gds.wcc` refreshes alias components afterward.
- **Candidate-generation store (`entity_embeddings`, pgvector + HNSW)** — entity names are embedded **once** and
  found via an indexed ANN query, so a sweep costs *O(new)* embeddings rather than re-embedding the whole set.
- **`alias_adjudications`** — a per-pair verdict ledger doubling as an audit trail and a don't-re-ask idempotency
  cache. `alias_writer --stats` summarises it (the precision-review surface).
- **Entity-graph telemetry** — `alias_edges` / `alias_covered_entities` now climb from zero, plus
  `alias_components` / `largest_alias_component`. **Orphan counting corrected**: `orphan_entities` now means
  truly dangling (degree-0), not "no live-fact `MENTIONS`" (which mislabels legitimate REM typed-edge targets); a
  new `unmentioned_entities` reports that class honestly.
- **`/health.config`** — an always-present, non-secret echo of the effective env-resolved LLM/tuning config
  (backends + weights, pool tuning, affinity knobs, `embed_max_chars`), so the live setup is inspectable without
  reading `.env` on the host.

### Added — inbound entity-name hygiene gate

A deterministic "garbage-in" gate (adapted from a companion advisor/researcher agent's ingestion gate to the
single-`Entity`-label ontology) that stops meaningless names from becoming graph hubs.

- **`ontology.sanitize_entity_name()` / `sanitize_entity_names()`** — pure, deterministic. Rejects numeric-only
  names (leaked pg-ids), single characters, booleans/placeholders, and schema vocabulary (relationship/label
  names). **Casing is preserved** (`Neo4j` stays `Neo4j`) — case-variant unification is the alias layer's job.
  `MIN_ENTITY_NAME_LEN` is env-tunable (default 2).
- **Gate 1 — outbox→graph** (`coordinator._gate_graph_entities`, applied at the fact + decision MERGE): gates the
  graph **projection** only. Tier-1 Postgres facts stay pristine (the agent's original record is the source of
  truth). Rejected names are logged as a quality signal.
- **Gate 2 — REM** (`rem_loop._write_neo4j_rem`): sanitises LLM-extracted fact relationships + decision extras,
  which are minted during enrichment and never passed Gate 1.
- **`cleanup_entity_noise.py`** — one-time cleanup (dry-run default) that removes pre-gate noise using the gate's
  **own** definition, so prevention and cleanup can never drift. First run removed 27 noise hubs, headlined by
  `:Entity{name:"Decision"}` at **degree 83** (a fake super-hub on 49 Decisions + 34 Facts that would corrupt
  NREM density clustering) plus leaked pg-ids `254`–`259`; 153 Decisions + 234 Facts untouched.

### Added — schema-compliance telemetry

- **`/memory/telemetry` `compliance` section** (`coordinator._graph_compliance`): `predicate_distribution`
  (full relationship-type census), `label_compliance` + `invalid_labels[]`, `relationship_compliance` +
  `invalid_relationships[]` — node labels / relationship types in the live graph that fall outside the
  ontology vocabulary. Surfaces legacy/foreign drift the inbound gates now prevent but cannot retroactively
  remove (e.g. `DockerContainer` nodes, `WRITES_TO` edges from pre-gate experiments). Split logic
  (`_compliance_split`) is pure and unit-tested; the rule reads `KNOWN_LABELS`/`KNOWN_RELATIONSHIPS` so it
  can never drift from what the daemons write.

### Changed — ontology enrichment, Stage 1.1

Path A (first-class multi-label, no external ontology plugin) data-driven enrichment of the thin domain layer
(previously a single untyped `:Entity` + one `MENTIONS` edge). Vocabulary derived by mining the project's own
accumulated decisions and entities, cross-checked against a companion advisor/researcher agent's domain ontology.

- **5 new entity sub-labels** in `ontology.yaml` / `ONT` (under `:Entity`, multi-label): `Component`,
  `System`, `Model`, `Concept`, `Document`. (Person/Agent/Process reuse the provenance labels
  `Human`/`AIAgent`/`Activity`.)
- **9 typed Entity→Entity relationships**: `DEPENDS_ON`, `PART_OF`, `IMPLEMENTS`, `PRODUCES`, `CONSUMES`,
  `RUNS_ON`, `CONFIGURES`, `DESCRIBES`, `VALIDATES`. `MENTIONS` stays as the fallback (retires in 0.6.1
  once per-edge confidence can show whether a typed pick fits).
- `KNOWN_LABELS`/`KNOWN_RELATIONSHIPS` and the entity-name noise filter extended to the new vocabulary, so
  compliance telemetry and the inbound gates track it immediately. **Schema-defs only** — REM begins
  assigning the new vocab in a later step; nothing in the write path changes yet.

### Added — reference resolution (record→record edges), Stage 1.2b

Agents reference other records in free text ("refines decision 381", "addendum to pg_id 257"). Those are
real cross-references that previously leaked as noise entity nodes or stayed invisible in prose. They are now
materialised as real edges.

- **`reference_resolver.py`** — context-gated regex (a number counts only after a record-reference cue) +
  id-validation against `technical_docs`. **Deterministic and high-confidence** (in the existing corpus 145/146
  such references resolved to a real record). New `REFERENCES` relationship (`Fact|Decision → Fact|Decision`);
  `Decision→Decision` is promoted to the previously-dormant `INFORMED_BY`.
- **Configurable relationship-type judge** (framework env): `REFERENCE_JUDGE_MODE=deterministic` (default) `| llm`,
  with `REFERENCE_JUDGE_URL` (any OpenAI-compatible endpoint — local, or a separate node to offload it) and
  `REFERENCE_JUDGE_MODEL`. The judge is **gated** — consulted only for the ambiguous `Decision→Decision` case,
  output strictly validated (exactly one allowed token), deterministic fallback otherwise. It can never widen
  the relation set or emit `SUPERSEDES` (explicit-only).
- **`resolve_references.py`** — one-time backfill (dry-run default, idempotent `MERGE`, skips pairs already
  linked by `SUPERSEDES`). First run materialised 128 edges (109 `REFERENCES`, 19 `INFORMED_BY`). REM applies
  the same resolver incrementally in a later step.

### Added — REM entity sub-typing wired live, Stage 1.3

- **REM now assigns each entity a sub-type** during enrichment and writes it as a second label
  (`:Entity:Component` / `:System` / `:Model` / `:Concept` / `:Document`). The prompt was tuned data-driven
  against a gold set (eval harness): entity typing macro-F1 **0.959**, robust at the production temperature
  (variance std 0.013). The sub-label is validated against the ontology set before interpolation
  (Cypher-injection guard); `OTHER`/invalid leaves the entity untyped; only `:Entity` nodes are touched
  (provenance nodes untouched). Live validation enriched a real fact and correctly typed all 6 of its
  entities. Typed `Entity→Entity` relationship extraction (`DEPENDS_ON`/`CONSUMES`/…) is the next step.

### Added — typed-relationship domain-range map, Stage 1.2

- **`ontology.DOMAIN_RANGE` + `is_allowed_relation()`** — which typed `Entity→Entity` relationship is legal
  between which entity sub-types (the rulebook REM will enforce next; an over-broad or unknown typed edge
  falls back to `MENTIONS`). Pure gate logic, inert until wired in. Cross-checked with a companion
  advisor/researcher agent's domain-range gate. Key guardrail: artifacts reach the abstract `Concept` hub only
  via `IMPLEMENTS`/`DESCRIBES`, never `DEPENDS_ON` — avoiding the modularity collapse over-broad concept edges
  cause. `DESCRIBES`/`CONFIGURES`/`VALIDATES` targets narrowed to high-signal pairings.

## [0.6.0] — 2026-06-28

Entity-resolution **alias layer** — soft `ALIASES` edges between synonymous entities (`coordinator` ↔
`Coordinator`), never a hard node merge, so a wrong link is always reversible — plus the
infrastructure it needs. This release lands the **consumption + telemetry** half (consumers traverse
alias components; everything is **no-op-safe** — identical to the exact-name graph until alias edges
exist). The automated **REM alias *writer*** (cosine-block + lexical-Jaccard + LLM verdict → `ALIASES`
edge) lands in **0.6.1**; until then aliases are created/calibrated with the offline harness below.

### Added — alias components (decision 455)

- **`ALIASES` Entity↔Entity edge** + `alias_max_hops` in `ontology.yaml` / `ontology.py`. Soft, audited,
  reversible — it never merges nodes.
- **`alias_graph.py`** — `gds.wcc` stamps `Entity.alias_component` (stable connected-component id). Grouping
  key everywhere is `coalesce(e.alias_component, elementId(e))`, so a lone entity is its own component.
- **NREM groups by alias component** (`consolidation_loop._find_anchored_clusters`): every surface form of a
  concept folds as ONE cluster, keyed on a deterministic canonical (lexicographic-min); all forms are
  recorded in the summary's **`metadata.aliases`** JSON. Fixes the fragmentation where synonym variants
  each fell below the consolidation threshold and no cross-cutting summary was ever synthesised.
- **Search surfaces aliases** — `graph_context` entries carry an `aliases` list (single query, no extra round-trip).
- **`/memory/telemetry` `entity_graph`** section (+ `status` line): `entities_total`, `singleton_entities`,
  `orphan_entities`, `alias_edges`, `alias_covered_entities`, `top_hubs`. The consolidation-coverage census
  (`_nrem_cycle_counts`, which drives the dream-cycle stall signal) is grouped by the same alias-component
  gate the daemon folds on, so the two never disagree.
- **`entity_resolution_eval.py`** — offline ER calibration harness (cosine vs lexical-Jaccard over-merge report);
  the instrument that proved raw cosine over-merges (cosine = blocking key, never the verdict).

### Added — framework env architecture (decision 456)

- **Framework env** now lives in the framework folder (`shared-memory/.env`, gitignored) with a committed
  `shared-memory/.env.example`; **client env** lives in the skill folder (`shared-memory-skill/shared-memory/.env.example`).
  Every live `.env` is gitignored; only sanitized examples are committed.
- **`postgres_neo4j_limits.yaml` is `${VAR}`-parametrized** (data paths + passwords from the framework env) and now
  loads **GDS** (`NEO4J_PLUGINS=["apoc","graph-data-science"]`).
- **`install_framework.sh`** — first-install script: prompts for paths/passwords, writes the gitignored `.env`,
  creates data dirs.
- Gateway env loader is backward-compatible (prefers `shared-memory/.env`, falls back to repo-root `.env`).

### Requirements

- **Neo4j GDS plugin** (free Community tier; validated on 2.13.10) is now required — `gds.wcc` powers alias grouping.
- **Neo4j 5.23+** (the `CALL (var) {}` variable-scope subquery form; already implied by GDS 2.13). The compose
  pins `neo4j:5-community` (latest 5.x).

## [0.5.0] — 2026-06-27

### Added — fact supersession (decisions 381, 384, 389)

Plain facts now have the soft-supersede lifecycle that `reversed` decisions and community
summaries already had. A wrong/outdated fact is **kept** (provenance, compare/contrast) but
flagged, hidden from search, and excluded from consolidation.

- **`save --supersedes <pg_id>`** — save a correction that supersedes an older fact in one call.
  The old row gets `superseded = true` + a `superseded_by` pointer (migration `013`); the Neo4j
  mirror (`old.superseded = true` + `(new)-[:SUPERSEDES]->(old)`) is **piggybacked** on the new
  fact's outbox row (no extra row).
- **`POST /memory/supersede {pg_id, by?}`** / **`supersede --pg-id [--by]`** — retract a fact with
  no replacement, or point it at an existing successor.
- **Lazy resolution (decision 384):** dependent summaries/insights are **not** re-folded on
  supersede. Search annotates any returned summary/insight whose provenance touches a superseded
  fact with **`stale_sources: [{old, superseded_by}]`** — a cheap Postgres join, no LLM. The
  consumer judges materiality at the point of use; **`review-hold --summary-id --pg-id`** records a
  reviewed-and-held supersession so it stops re-flagging.
- **Outbox GC (decision 389):** a fact superseded before it finished dreaming **rides along** with
  its successor and is purged transitively when the successor consolidates (`close_ledger_rows`,
  recursive over `superseded_by`, logged). A bare retract with no successor is purged immediately.
- **Census safety:** superseded facts are excluded from REM selection, the NREM density gate, the
  ledger backlog, and the telemetry/stall census — a riding-along predecessor cannot false-trip the
  ADR-018 stall verdict. `status` now shows `technical_docs: N (superseded M)`.
- Supersession is **explicit, never automatic** — embedding similarity is not a correctness signal.
  Additive and back-compatible: `api_version` unchanged.

## [0.4.13] — 2026-06-26

### Added — consolidation quality/coverage signal, Phase 1 liveness (ADR-018)

The dream cycle is now observable. The `insight = 0` outage above ran ~12 days because a fold
outcome was **logged, not stated** — nothing survived a cycle except a journal line. Phase 1
makes a fold outcome queryable state, so a silent crash can never hide again.

- **New `consolidation_runs` ledger** (migration `012`): one row per cycle (`insight` /
  `fact_consolidation`) with outcome (`completed` / `crashed` / `deferred`), fold counts, error,
  and timing. Self-pruning at daemon startup (`CONSOLIDATION_RUNS_RETENTION_DAYS`).
- **Instrumented seams** (`consolidation_loop.py`): `run_insight_cycle` and the shared
  `_consolidate_clusters` body (covering the event cycle, ledger sweep, and global sweep) record
  every outcome and **also leave a corroborating log line** — the table write is failsafe, so the
  outcome survives even if Postgres is unreachable. GPU/backup deferrals are recorded (throttled)
  so a stall is attributable; orphaned in-flight rows are reaped on restart (mirrors ADR-010).
- **`/health.consolidation`** `{stalled, last_outcome, last_success_age_seconds}` — a cached
  snapshot the coordinator refreshes in the background (~60 s) so `/health` stays DB-free.
- **`/memory/telemetry` `consolidation` section** — per-cycle-type last outcome, success age,
  in-flight, consecutive failures, last error, and the derived `stalled` verdict. Additive; the
  Monitor (read-only over these two endpoints) reshapes against the published contract.
- **Stall rule:** eligible backlog present AND no successful fold within
  `CONSOLIDATION_STALL_THRESHOLD_SEC` (default 2.5× the NREM sweep interval) AND nothing in-flight
  — so a merely-slow LLM fold reads as in-flight, not stalled.
- **Coverage census (PR-2):** the insight cycle records, *before folding* (so a crash still leaves
  it), `eligible_clusters` (uncovered insight opportunities) and `eligible_oldest_age_seconds` —
  the **K-th-oldest member's outbox write-time**, i.e. how long the most-neglected *actionable*
  cluster has gone unfolded. No new write-timestamp was needed: the self-cleaning `neo4j_outbox`
  is a complete write-time index over exactly the un-consolidated working set (ADR-018 open-Q1).
  NULL-safe for facts predating the outbox. Surfaced per cycle-type in the telemetry
  `consolidation` section. Server-side only; no `api_version` bump. 9 new tests
  (`tests/test_consolidation_signal.py`), suite 300 green.

### Added — inference/GPU-busy signal on `/health` + `/memory/telemetry`

The Monitor could not truthfully show the LLM as **"Busy"** (or explain *why* consolidation was
deferring): `/health.llm` is only a reachability probe of `:5000`, and the real GPU-busy signal —
the `nvtop` check the REM/NREM daemons already gate on — was computed for the defer decision and
thrown away. This surfaces it on the two read-only endpoints the Monitor may use.

- **`inference_busy` (tri-state `"busy" | "idle" | "unknown"`)** on `/health` (top-level) and
  `/memory/telemetry`, from a new `gpu_load.inference_busy_state()`. **`"unknown"` when nvtop is
  absent or `SLOT_AWARE=0`** — a fail-open `False` from the gate is *never* reported as `"idle"`,
  so the Monitor cannot show a false idle. Distinct from `/health.llm`, which stays pure
  reachability. `nvtop` measures raw GPU utilisation, so this also reflects a user chatting
  directly with `:5000` (which bypasses the gateway).
- **Cached, not per-request:** the coordinator probes the GPU in the existing background
  consolidation-health refresher, so `/health` reads a cached value and never shells out to nvtop.
- **`last_deferred_reason`** (`"gpu_busy" | "backup_in_progress"`) added per cycle-type and
  top-level in the telemetry `consolidation` section — read from the deferral reason the daemon
  **already** records in `consolidation_runs.extra`. The Monitor can now show
  "deferred — inference GPU busy". `status` CLI renders both.
- Server-side only; **no schema change, no `api_version` bump.** 8 new tests
  (`tests/test_gpu_load.py`, `tests/test_consolidation_signal.py`), suite 309 green.

### Fixed — silent insight-fold crash (`insight = 0` since v0.4.5)

Cross-project insight consolidation (Phase 3a) produced **zero** insights since it shipped
in v0.4.5: `run_insight_cycle`'s fresh-cluster path called `_fold_insight(..., projects=…)`,
but `_fold_insight` has no `projects` parameter (it derives projects from the Postgres rows
itself), so every fresh fold raised `TypeError`. The cycle's `try/except` swallowed it, so the
failure surfaced only as an hourly ERROR log line — no `/health` or telemetry signal — and went
unnoticed for ~12 days while ~30 eligible clusters sat unfolded.

- **Fix:** drop the stray `projects=` kwarg at the call site (`consolidation_loop.py`).
- **Regression test:** `run_insight_cycle`'s fresh-cluster call site is now exercised with an
  `autospec`'d `_fold_insight`, so an incompatible kwarg fails the suite — the isolated
  `_fold_insight` unit tests could not catch a call-site signature mismatch. (#77)
- **Verified live:** `insight 0 → 1` and draining the backlog after deploy.

### Added — person identity (operator) via OS login, server-enforced

A second identity axis alongside the token-verified **agent**: the **principal** — the
human operator — obtained from the **OS login account behind the connection**, read from
the kernel via `SO_PEERCRED`. Pre-PoP foundation; the cryptographic person-key/delegation
tier remains a later "only if".

- **`AF_UNIX` listener** on the gateway alongside TCP `:8888` (`GATEWAY_UDS_PATH`, default
  `$XDG_RUNTIME_DIR/shared-memory-gw.sock`, mode `GATEWAY_UDS_MODE`=`0600`). Local agents and
  SSH-forwarded Unix sockets connect here so the gateway can read the operator's OS account
  from the kernel. SSH already authenticates remote users (and encrypts), so identity +
  confidentiality come from the existing transport.
- **`principal` + `connected_from`** stamped server-side on every write and audit line.
  `principal` is the OS username; `connected_from` is the kernel-attested fingerprint
  (`uid/gid/pid`, immutable `login_uid`/`login_user`, audit `session` — resolve the remote
  host via `loginctl show-session`). **Enforced in code, never agent-supplied:** any
  client-sent `principal`/`connected_from` is stripped and re-stamped (`_apply_principal`)
  on both `handle_save` and `handle_retrospective`, so an agent told to "save as someone
  else" cannot move it (`decided_by` stays a separate narrative claim). Over plain TCP there
  is no kernel credential, so the fields are honestly absent — never guessed.
- **`GATEWAY_REQUIRE_PRINCIPAL`** (off by default) rejects write routes that lack a
  kernel-attested principal — turn on once every writer is on the socket.
- **`memory_bridge.py`** auto-detects and prefers the Unix socket (httpx `uds=` transport;
  `COORDINATOR_UDS` to override) so local saves are stamped; falls back to TCP.
- **No schema change** — `principal`/`connected_from` ride the existing `metadata` JSONB and
  the audit JSONL. README §14 documents the audit trail as **log + database together**: the
  log is the per-request *who/from-where* trail (no project/record-type, no log→DB join key),
  the DB answers *which decisions on which project by which user*.
- Tests: new `tests/test_person_identity.py` (SO_PEERCRED over a real socketpair, TCP→absent,
  strip-and-stamp, require-principal gate).

### Added — cross-store backup & restore (quiesced)

Consistent, scriptable backup of **both** stores and a ground-up restore. Both are
required because Neo4j holds non-derivable state (the `HAD_OUTCOME` retrospective
edges); the framework ships the mechanism, while policy (schedule/retention/
destination/encryption) stays admin-owned in the private `.env`.

- **New `ops/backup.sh`** — `pg_dump -Fc` + Neo4j APOC `apoc.export.cypher.all`
  (online, no container stop). `flock` single-instance, dump-to-`.tmp` + atomic
  `mv`, a `*.manifest.json` (sha256 + counts) written last. `--dry-run` reports
  sizes/free-space/retention with no writes; `--verify` checks sha256 + gzip
  integrity + `pg_restore --list`. Retention prunes only its own prefix.
- **New `ops/restore.sh`** — verifies a set's sha256/integrity before touching
  anything, refuses to clobber a non-empty store without `--force`, restores
  Postgres (source of truth) before Neo4j, reports counts vs the manifest.
- **New `ops/shared-memory-backup.{service,timer}`** — optional `systemd --user`
  timer; cron is documented as the alternative.
- **Quiesce seam** — new authenticated `POST /admin/backup` (`quiesce`/`resume`)
  gated by a new **`admin`** `AGENT_ROLES` value (confined to `/admin/*`; cannot
  read/write memory). While quiesced, client **write** routes shed
  `503 + Retry-After` at the existing auth chokepoint; reads keep flowing.
  `/health` exposes `backup_in_progress`. A TTL auto-resumes if the backup script
  dies, so writes can never wedge.
- **Daemon fence** — REM/NREM each take a **shared** Postgres advisory lock per
  cycle and skip if the gateway holds it **exclusive**, so consolidation/enrichment
  never writes mid-dump. Auto-releases on session death (crash-safe).
- **Config** — `postgres_neo4j_limits.yaml` sets `NEO4J_apoc_export_file_enabled=true`
  (existing Neo4j containers must be recreated to pick it up). New `BACKUP_*`
  knobs documented in `.env.example`.

---

## [0.4.12] — 2026-06-12

Concurrent-load hardening + a pluggable auth/audit seam (the foundation for the
planned PoP auth and full agent-auditing work), plus log hygiene: owner-only
perms, off-event-loop audit writes, and logrotate-managed rotation.

### Added — log hygiene (perms, off-loop writes, logrotate)

Framework log files are now owner-only and rotated by default.

- **New `scripts/log_hygiene.py`** (server-side): `secure_path`/`append_secure`
  enforce **0600** files in a **0700** dir on every write (tightening any
  existing world-readable file), and `AsyncLineWriter` moves the gateway audit
  write **off the event loop** — the line is enqueued (O(1), drop-oldest if the
  bounded queue fills) and a background task appends it via a thread executor, so
  a slow disk can't add latency to the request path. Sync fallback when no loop
  is running (non-async callers / tests).
- **Wired up:** `coordinator._audit` → `AsyncLineWriter` (flushed on `stop()`);
  `rem_loop._write_audit_log` → `append_secure`; `memory_bridge._append_log` and
  `consolidation_loop.merge_logs` set 0600 on the files/archives they create.
- **Rotation via system `logrotate(8)`** (not in-process): `ops/shared-memory.logrotate`
  (covers `*-audit.jsonl` — `daily`, `maxsize 50M`, `rotate 14`, `compress`,
  `create 0600`) driven by a `systemd --user` timer
  (`ops/shared-memory-logrotate.{service,timer}`, no root). Open-append-close
  writers make `create` mode clean — no `copytruncate`, no lost lines. The
  per-tool save logs remain rotated in-process by the NREM daily merge.
- 6 new tests in `tests/test_log_hygiene.py` (**257 total**).

### Changed — concurrent-load hardening

The gateway now sheds load gracefully under concurrent ingress instead of
hanging, ahead of the planned auth (asymmetric-key + PoP) and agent-auditing
work, which both amplify per-request load.

- **Bounded Postgres pool with acquire timeout.** `_pool.acquire()` calls now go
  through `_acquire()`, which bounds the wait by `POOL_ACQUIRE_TIMEOUT` (default
  5 s). A saturated pool surfaces as `503 + Retry-After` via the auth middleware
  rather than blocking a caller indefinitely. `POOL_MIN`/`POOL_MAX` are now
  env-configurable (default 2/20); pool sizing is documented as a system budget
  against Postgres `max_connections` (coordinator + REM + NREM + LISTEN).
- **Bounded per-entity lock registry.** The unbounded `dict[str, asyncio.Lock]`
  (one permanent lock per unique entity, a slow leak) is replaced by
  `BoundedKeyedLocks` — LRU eviction of *idle* locks past `LOCKS_MAX_SIZE`
  (default 4096), never evicting a held or awaited lock. This is the bounded-map
  pattern the future PoP nonce/replay cache reuses.
- **Neo4j driver pools bounded** in the gateway and both daemons
  (`NEO4J_MAX_POOL`, `NEO4J_ACQUIRE_TIMEOUT`) — they share Neo4j, so an unbounded
  default pool could queue indefinitely under contention.
- **Outbox retry backoff (migration 011).** Failed `neo4j_outbox` rows get a
  `next_attempt_at` set to `now() + base·2^retries` (capped, jittered), and the
  drain query skips rows not yet due. A Neo4j outage now backs off instead of
  re-hammering up to `OUTBOX_BATCH_SIZE` rows every 2 s. `/memory/telemetry`
  reports the oldest dead-letter (`outbox_failed_oldest_age_seconds`).

### Added — auth/audit seam (foundation for PoP + agent auditing)

- **Pluggable identity resolution.** Auth is now a `resolve_identity()` registry
  (`_IDENTITY_RESOLVERS`); bearer-token resolution is the only entry today. The
  PoP overhaul appends a resolver here without touching the middleware, handlers,
  or audit hook — they only ever see the resolved agent *name*.
- **Thin per-request audit log.** Opt-in `GATEWAY_AUDIT_LOG_PATH` writes one
  JSON line per authenticated request (`ts, agent, role, method, path, status,
  latency_ms, request_id`) at the auth seam, OFF the DB hot path. The
  observability tier of agent auditing; the verified-identity rows become
  non-repudiable once PoP lands, with no schema change.
- **Outer load-shed valve.** Optional `GATEWAY_INFLIGHT_MAX` caps total
  concurrent in-flight requests (including ones parked on a slow embedding/LLM
  that hold no DB connection) — returns `503 + Retry-After` when exceeded.
- **`/health` advertises `auth_scheme`** (`bearer`) so clients can detect when
  the gateway moves to PoP.

### Tests & migration

- 14 new tests in `tests/test_hardening.py` (bounded-lock eviction, outbox
  backoff schedule, pluggable identity resolution, audit hook, in-flight
  load-shed, pool-saturation → 503) — all green (**257 total** including the
  log-hygiene suite below).
- Migration **011** (`next_attempt_at`) applied; `schema_init.sql` regenerated
  from the chain (the only diff is the new column).

### Roadmap

- §19 adds the next two phases this work sets up: **Agent authentication —
  Proof-of-Possession** (asymmetric-key + PoP, plugging into the new identity
  seam) and **Agent auditing — full non-repudiable record** (DB-backed, built on
  PoP identity, surfaced to the monitor). The current cycle's hardening + thin
  audit log is the foundation both build on.

---

## [0.4.11] — 2026-06-12

### Security

- **`init_db.sh` no longer passes the Neo4j password on a command line.** `cypher-shell -p "$NEO4J_PASSWORD"` placed the password on the process argv (a world-readable `/proc/<pid>/cmdline`), exposing it to other local users on a shared host. It is now passed through the environment (`export NEO4J_PASSWORD` + `docker exec -e NEO4J_PASSWORD`, no value on any argv); `/proc/<pid>/environ` is owner-restricted. Verified on a throwaway Neo4j: env-var auth works and all 7 constraints still apply. (Surfaced by `/security-review`.)
- **`bootstrap_tokens.sh --force` preserves the `.env` file mode.** The rewrite went through a temp file created with the default umask, which could widen a `chmod 600 .env` back to `644`. It now `chmod --reference`s the original before the `mv`.

### Changed

- **README currency pass (§1–§20).** Roadmap (§19) corrected: insight consolidation (Phase 3a) moved from *Planned* to *Completed* (it shipped in v0.4.5) and the Completed table extended through v0.4.4–v0.4.10; the schema-migrations row now lists 007–010 + `schema_init.sql`/`neo4j_init.cypher`/`generate_schema_init.py`. §9 and §10 gained pointers to the `systemd --user` unit and `bootstrap_tokens.sh` respectively. Quick Start step-range fixed and pointed at the supervised-gateway unit. Cross-checked versions, ports, service names, model (gemma-4-12b), and the test command against the code — all current.

---

## [0.4.10] — 2026-06-12

### Added

- **Guided install scripts in `shared-memory/scripts/` — a fresh gateway host goes clone → running with three idempotent commands.**
  - `preflight.sh` — read-only prerequisite doctor: checks Docker + daemon, `docker compose` v2, `uv`, and a populated `.env`; warns on low RAM/disk and missing `nvtop`. Exits non-zero on any hard failure.
  - `init_db.sh` — initialises **both** stores: applies `schema_init.sql` to Postgres and `neo4j_init.cypher` to Neo4j, running each client *inside* its compose container (`postgres-vector` / `neo4j-memory`), so the host needs neither `psql` nor `cypher-shell`. Waits for readiness; idempotent. Neo4j constraints apply with `--fail-at-end` and, on a Neo4j shared with another system (a label already keyed differently), report the conflict instead of half-applying.
  - `bootstrap_tokens.sh` — mints one token per agent via `generate_tokens.py`, appends `AGENT_TOKENS` (+ read-only `AGENT_ROLES`) to the gateway `.env`, and prints each agent's own `AGENT_TOKEN` to distribute. Refuses to overwrite an existing registry (which would invalidate every agent's token); `--force` rotates all tokens.

### Changed

- **Quick Start rewritten around the scripts.** Step 2 runs `preflight.sh`, step 4 collapses the two manual schema commands into `init_db.sh`, step 5 uses `bootstrap_tokens.sh` — each step still links its manual equivalent. §6 updated to present `init_db.sh` as the easy path with the by-hand commands as alternatives; the stale "Quick Start step 3" cross-reference for Neo4j is fixed.
- The install scripts read `.env` values key-by-key rather than `source`-ing the file — `.env` values may contain spaces (e.g. `PROJECT_ALIASES`) that bash `source` mis-parses.

---

## [0.4.9] — 2026-06-11

### Added

- **Migration 010 — embedding indexes switch from `ivfflat` to `hnsw`.** HNSW gives better recall and query latency for this workload (no list-count tuning, graceful with incremental inserts) at the cost of a slower build and more memory — an acceptable trade at this corpus size. Production already ran hnsw via a manual swap; this migration brings the chain (and therefore fresh installs via `schema_init.sql`) in line, so every install converges on the same index. **Idempotent and cheap to re-run:** because `apply.py` replays the whole chain on every invocation, the migration is wrapped in a `DO` block that rebuilds an index *only* when it is not already hnsw — a bare `DROP`/`CREATE` would rebuild the vector index (exclusive lock, expensive) on every run. Verified a no-op on production (index OIDs unchanged after apply) and idempotent on a chain applied twice. `schema_init.sql` regenerated — now byte-identical to the chain with hnsw.

---

## [0.4.8] — 2026-06-11

### Added

- **`neo4j_init.cypher` — the Neo4j counterpart to `schema_init.sql`.** All seven uniqueness constraints (`Fact`, `Entity`, `CommunitySummary`, `Decision`, `Human`, `AIAgent`, `Project`) in one idempotent file. Run once on a fresh Neo4j instance before the first gateway start — without these constraints, `MERGE` races can create duplicate nodes. Previously only three constraints were documented, and only in README prose.

### Fixed

- **`schema_init.sql` is now generated from the migration chain, not a live database — eliminating drift.** `generate_schema_init.py` spins up a throwaway database, applies every numbered migration to it (exactly what `apply.py` does on a fresh install), introspects the result, then drops it. The output is **equivalent to `apply.py` on an empty database by construction** and verified byte-identical (columns, types, nullability, defaults, indexes) via a two-scratch-DB diff. The previous version introspected the long-lived production database, which had drifted from the chain — it had silently dropped `content NOT NULL` on both tables and captured a manual `ivfflat→hnsw` index swap. Both are now correct: fresh installs get `content NOT NULL` and `ivfflat`, matching the chain.
- **`apply.py` no longer re-runs `schema_init.sql`.** It globbed `*.sql`, which picked up the generated `schema_init.sql` now sharing the migrations directory; the glob is now `[0-9]*.sql` (numbered migrations only). Harmless before (idempotent), but conceptually wrong — `apply.py` runs migrations, not the fresh-install snapshot.
- **`neo4j_init.cypher` no longer claims constraints are applied automatically.** The header referenced a `coordinator._ensure_neo4j_schema` mechanism that does not exist; an operator trusting it would skip the file and run Neo4j with zero constraints. The header now correctly states it is a one-time manual step.

### Changed

- **`generate_schema_init.py` discovers tables dynamically** (no hardcoded list — a migration that adds a table is captured automatically) and the generated header is deterministic (no embedded timestamp), so regeneration produces a diff only when the schema actually changed.

---

## [0.4.7] — 2026-06-11

### Fixed

- **`SKILL.md` and `system-prompt.md` no longer instruct agents to set `source` to the loaded model name.** Since v0.4.6 the coordinator stamps `source` with the server-verified token identity, overriding any client-supplied value — the old instruction was misleading and the root cause of model names (e.g. `claude-sonnet-4-6`, `qwen3-27b`) appearing as agent identities in the monitor. Guidance now: `source` is owned by the gateway; the canonical identity is always the auth-token name (`claude`, `gemini`, `lm_studio`, etc.). Model names belong in `assisted_by` on decisions, which creates `:AIAgent` provenance nodes.

### Ops

- **`shared_mem` added to `PROJECT_ALIASES` (decision 276 follow-up).** Ten rows carrying the `shared_mem` label were missed by the v0.4.6 normalisation because the alias was absent from the map. Added `shared_mem=<canonical>` so both the ingress guard and future `normalize_projects.py` runs cover it. Two residual source-label noise rows also backfilled: `design_session_cloe → antigravity`, `test_sync → claude`. Final canonical agent_id distribution: `claude(109) · legacy(34) · gemini(34) · antigravity(18) · grok(14) · chromebook-antigravity(1)`.

---

## [0.4.6] — 2026-06-11

### Fixed

- **Verified agent identity is now stamped on the `agent_id` column.** The auth middleware overwrote `metadata.source` with the server-verified token identity but left the `technical_docs.agent_id` column set to the client-supplied value — and `memory_bridge.py` defaults `AGENT_ID` to the script name `"memory_bridge"` when the env var is unset. Every authenticated save from an agent that didn't explicitly set `AGENT_ID` was therefore recorded under the placeholder `memory_bridge` (observed: 152 rows) instead of the real agent. `handle_save` (and `handle_retrospective`) now derive `agent_id` from `authenticated_agent` when present, mirroring the `source` overwrite — spoof-proof, and no per-agent `AGENT_ID` config required. Backward compatible: with auth disabled the client-supplied `agent_id` is still used. Existing rows backfilled from `metadata.source`. New tests in `tests/test_coordinator.py`.

### Ops

- **Project-name normalisation applied to live data (decision 276).** `PROJECT_ALIASES` set in the gateway `.env` (canonicalising future writes to the project folder name) and `normalize_projects.py` run against both stores: the `shared-memory`/`shared_memory`/`Shared Memory Framework` variants folded to `shared-memory-GitHub` and the `tier3*`/`openclaw*` variants to `tier3-cloe`, so the insight gate's ≥2-distinct-projects rule is trustworthy. Canonical distribution: shared-memory-GitHub, tier3-cloe, shared-memory-monitor.

## [0.4.5] — 2026-06-11

### Added

- **Phase 3a — insight consolidation: NREM folds cross-project decision clusters into elevated `kind='insight'` community summaries (decision pg_id 276).** A solitary decision never round-trips to Postgres (it is already Tier-1-searchable); value exists only in synthesis across ≥2 *linked* decisions. The eligibility gate is **pure graph state — no rating taxonomy**: ≥`insight_threshold` (ontology key, default 2) unconsolidated, REM-enriched, non-reversed `:Decision` nodes converging on a shared grounded `:Entity` (must carry ≥1 `:Fact`; total degree ≤ `INSIGHT_HUB_DEGREE_CAP`, default 50 — mega-hubs link everything to everything), spanning **≥2 distinct projects**, with **≥1 `HAD_OUTCOME` edge anywhere in the cluster** — the *existence* of a retrospective, never its rating, is the trust gate; the retrospective wording goes into the fold prompt verbatim and the narrative carries the valence. **Insights are always-INSERT; supersession is the dedup** — migration 009 makes the `(entity, domain)` unique index partial (insight rows exempt), closing the resurrection trap where a re-fold would conflict-UPDATE a superseded row in place and be born invisible. A later retrospective on any source decision **re-folds the same `source_pg_ids`** (cumulative — all `HAD_OUTCOME` edge wording, the permanent outcome archive) and the equal source set rides the covered-subset supersession, replacing the old insight. **The trigger is the durable ledger** (decisions have no `:Fact` node — the NOTIFY path is structurally deaf to them): decision + retrospective outbox rows now complete the fact lifecycle — consumed rows flip to `consolidated` transactionally with the insight INSERT (snapshotted **by row id** before the LLM call, so a retrospective arriving mid-fold stays open and re-triggers) and are deleted (logged) after the graph marking; an open retro row on a decision in no insight and no qualifying cluster stays open deliberately — durable backlog, not a stuck outbox. Crash recovery mirrors the fact path (`fetch_unreconciled_insights` re-applies the idempotent graph marking). Retrieval: `handle_search` surfaces the nearest active insight **above** the thematic summary as `tier: "insight_summary"` (decision ids in `source_pg_ids`); mirrored in `vector-skill.py`. Runs as `run_insight_cycle()` on every sweep tick. New `tests/test_insight_consolidation.py` (20 cases).
- **Reversal vocabulary: `save_retrospective` with `rating="reversed"` marks the decision superseded — the one structural rating.** Migration 009 adds `technical_docs.superseded` (boolean, default false); a reversal sets it in the same transaction as the retrospective outbox row (savepoint-guarded for pre-migration schemas) and mirrors `superseded = true` onto the graph `:Decision` node via the outbox apply. Tier-1 search excludes superseded rows (with a pre-migration fallback), and reversed decisions never seed a *fresh* insight cluster — but re-folds keep them in `source_pg_ids`, so a decision reversed in one project yet held in another folds as **boundary evidence** ("principle with known limits"), never silently dropped. All other ratings carry no enum semantics.
- **Project-name normalisation at ingress (canonical = project folder name).** Free-text project drift (`shared_memory` vs `shared-memory`) would break the insight gate's ≥2-distinct-projects rule. The coordinator now rewrites `metadata.project` and `metadata.decision.project` through a `PROJECT_ALIASES` env map (`old=new,…`; empty = no-op) before the row and its outbox params are written. One-time backfill for existing rows and Neo4j `:Project` nodes: `scripts/normalize_projects.py --map "…" [--dry-run]` (rewires `PROJECT_OF` edges to the canonical node and deletes the alias only when nothing else points at it).

- **Outbox dream-cycle ledger (fact path): `neo4j_outbox` rows now record the full dream lifecycle — `pending → applied → rem_reviewed → consolidated → deleted`.** NREM marks a fact's outbox rows `consolidated` **in the same Postgres transaction** as the `community_summaries` INSERT it was folded into, and deletes them only after the Neo4j consolidation marking succeeds. A row's presence therefore always means "this artifact has not finished dreaming" — a durable, restart-proof NREM backlog (the volatile in-memory `pending_pg_ids` queue is now only a latency optimization) — and a row's absence is the conclusive record that **both stores are synced** (decision pg_id 267). The recurring sweep is now **ledger-driven**: each `NREM_SWEEP_INTERVAL_SEC` tick backfills already-covered rows (one-time upgrade backfill + re-save duplicates stuck at `applied`), reconciles rows stuck at `consolidated`, and feeds the `rem_reviewed` backlog to the same anchored cluster query the event path uses once it reaches the density threshold. The unanchored global graph sweep now runs **once per process start** — the only pass that reaches pre-coordinator facts with no outbox rows. Decision and retrospective rows were exempt at first (the decision-NREM design was unratified, pg_id 269) — the insight-consolidation entry above now gives them the same lifecycle (decision pg_id 276); retrospective rows are identified by `cypher_params` type, never status, because REM's outbox mark targets the latest applied row for a pg_id and a retrospective shares its target decision's pg_id. New SQL-contract tests in `tests/test_outbox_ledger.py` (9 cases).

- **Outbox deletions are always logged to the gateway log.** `close_ledger_rows` now uses `DELETE … RETURNING` and unconditionally emits one INFO line per close with every deletion as a traceable pair — `outbox_id=<row>→pg_id=<consolidated fact>` — tagged with its context (`consolidation` or `reconciliation`). The ledger row is the only record of the dream lifecycle, so its destruction always leaves a trace; the logged pairs are the rows *actually* deleted, not the requested list. The reconciliation re-apply message is downgraded from WARNING to INFO (it also fires for pre-ledger backfilled rows, not only crash leftovers).

### Fixed

- **REM's outbox mark no longer mis-stamps retrospective rows.** `_mark_outbox_rem_reviewed` targets the latest `applied` row for a pg_id; a retrospective shares its target decision's pg_id with a higher row id, so the mark landed on the retro row and the decision row stayed `applied` — corrupting the ledger statuses the insight triggers now read. The mark now excludes `type='retrospective'` rows.
- **Cross-DB atomicity in NREM consolidation — the ADR-noted risk is now fail-safe.** The consolidation write order was: Postgres INSERT (uncommitted) → Neo4j marking (`consolidated=true` + CommunitySummary node) → Postgres COMMIT. A crash before the commit left facts graph-marked with **no committed summary** — permanently excluded from future clustering and invisible to retrieval, with nothing recording the loss. The order is now: Postgres transaction (summary + supersession + ledger `consolidated` flag) **commits first**, then the graph marking runs, then the ledger rows are deleted. Every crash window now leaves repairable state: ledger rows stuck at `consolidated` are found by the next sweep's reconciliation step, which re-applies the idempotent graph marking from the authoritative summary row (`_mark_consolidated_in_graph`, extracted and shared) and closes the rows — no re-synthesis, no stranding.
- **NREM stranded clusters: periodic global density sweep.** NREM was purely event-driven — only entities touched by a fresh `new_artifact` NOTIFY were ever density-evaluated, so a cluster that crossed `DENSITY_THRESHOLD` without a triggering save sat eligible forever (observed 2026-06-10: an entity at exactly 5 `rem_processed`, unconsolidated facts that never consolidated; retrospective on decision pg_id 214). REM does notify NREM after each enrichment, but `NOTIFY` is fire-and-forget: anything sent while the daemon was down (gateway restarts, the pre-systemd session-teardown kills) was lost, and `pending_pg_ids` is in-memory so a restart also dropped queued entry points. `consolidation_loop.py` now runs a **global density sweep** — the same cluster query, density gate, and per-`(entity, domain)` re-gate, just without the entry-point anchor — on the first idle tick after startup (draining anything stranded while down) and every `NREM_SWEEP_INTERVAL_SEC` thereafter (default 3600). The sweep runs only when idle with no pending event work and yields to active GPU inference. Also fixed en route: re-queued failed work (LLM/embed/DB errors) never armed the `MAX_DEFERRAL` backstop clock (`first_notification_time` stayed `None`), so sustained GPU activity could defer retries indefinitely — re-queueing now goes through a `_requeue()` helper that arms it. The shared consolidation body is extracted to `_consolidate_clusters()`; the community-summary write remains a direct INSERT producing **no outbox row and no NOTIFY** — consolidation closes the loop and can never re-wake itself. New gating rule is pure and unit-tested (`tests/test_global_sweep.py`, 9 cases).

## [0.4.4] — 2026-06-10

### Added

- **`GET /memory/telemetry` enriched with `nrem` consolidation-cycle counts and a `breakdown` section — a read-only client now needs no direct DB access.** Two new sections, each computed independently (a partial backend failure still returns the rest): (1) **`nrem`** reports pending NREM *cycles* — `fact_cycles`, `decision_cycles`, `total_cycles`, plus the gating thresholds — by reproducing `consolidation_loop`'s rule (entity clusters of `rem_processed`, unconsolidated nodes, re-partitioned per `(entity, domain)` and counted only where a bucket meets density). This is the Neo4j↔Postgres join a read-only client could not do itself (the Fact node has no domain; the authoritative domain is `COALESCE(metadata->>'project', metadata->>'domain', scope, 'general')` from `technical_docs`). (2) **`breakdown`** returns metadata distributions — `record_types`, `agents`, `sources`, `domains`, and `summaries` by kind — as cheap GROUP BYs over `technical_docs` + `community_summaries`. Together these let the companion **Shared Memory Monitor** render its full live dashboard *and* breakdown panels from this single endpoint with **zero Postgres credentials** — the coordinator (which owns both backends) does the joins. `memory_bridge.py status` now prints the NREM cycle line. Purely additive — existing `postgres`/`neo4j` sections are unchanged and `api_version` stays `1`. New pure helper `_count_domain_cycles` is unit-tested in `tests/test_domain_clusters.py` (6 cases).
- **LM Studio MCP parity: new `memory_telemetry` tool.** `vector-skill.py` gains a `memory_telemetry` MCP tool that pulls `GET /memory/telemetry` (incl. `nrem` + `breakdown`) — the same snapshot CLI agents get from `memory_bridge.py status` — so LM Studio can observe the dream-cycle backlog without a direct DB probe. The system prompt (`system-prompt.md`) gains a Diagnostics section documenting it (and `check_memory_health`), and notes that telemetry is gateway-native with the Shared Memory Monitor as an optional read-only viewer.
- **Read-only agent roles (`AGENT_ROLES`) — dedicated, non-write-capable identities for ops clients.** A registered token can now be confined to read-only access via a new `AGENT_ROLES=name:read` map in the gateway `.env`. A `read` token may reach only `GET /health`, `GET /memory/telemetry`, and `POST /memory/graph` (already read-only-Cypher-guarded); every other route — `save`, `retrospective`, `search`, and the embeddings/LLM proxy passthrough — returns **403**. Roles only ever *narrow* access (the token must still be a valid `AGENT_TOKENS` entry); `AGENT_ROLES` unset or `name:full` preserves full read/write, so existing installs are unaffected. `generate_tokens.py` now mints a `monitor` identity and emits the matching `AGENT_ROLES=monitor:read` line. Motivation: the companion **Shared Memory Monitor** dashboard was authenticating with a *borrowed agent token* (grok's), making an observability tool subordinate to an agent's credential lifecycle and giving a read-only dashboard write capability it never needs. It can now hold its own dedicated read-only token — agent-token rotation no longer breaks telemetry, and a leaked monitor token cannot save or poison memory. Enforcement is additive in `auth_middleware`; `api_version` is unchanged (no wire-shape change for existing clients). Covered by 12 new cases in `tests/test_auth.py`.

### Changed

- **`ENTITY_SET_LIMIT` raised 500 → 1500 and made env-configurable.** REM lists every typed node (up to this cap) in each enrichment prompt so the LLM matches existing entity names exactly rather than minting near-duplicates. The typed-node graph crossed 500 (516 named nodes) and grows with every `CONSIDERED`/`REJECTED`/`PRODUCES_INSIGHT` extraction, so the closed set was being silently truncated (logged warning). Default is now 1500 with an `ENTITY_SET_LIMIT` env override. Note: a larger closed set enlarges every prompt — keep the LM Studio context ≥ ~16K when pushing it high. The durable fix for unbounded growth (per-domain scoping / embedding-retrieval of only relevant entities) remains on the roadmap.
- **REM/NREM sampling temperature is now env-configurable** (was hardcoded `0.1`). `rem_loop.py` reads `REM_TEMPERATURE`, `consolidation_loop.py` reads `NREM_TEMPERATURE`, both falling back to `DREAM_TEMPERATURE` then a default of **0.6**. The request value overrides the LM Studio preset, so this is the only place the dreaming temperature can be set. Rationale: the old `0.1` was tuned for Qwen-class models; different local models want very different temperatures — Gemma-class degrade at low temp (≈0.6), Mistral-3 *Instruct* wants `<0.1`, Mistral-3 *Reasoning* wants `1.0`. One knob now serves all without code edits. `tests/test_rem_loop.py` asserts the payload uses the configured value, not a literal.

## [0.4.3] — 2026-06-09

### Added

- **Operational telemetry: `GET /memory/telemetry` + `memory_bridge.py status`.** A pull-based snapshot of the state that matters day to day, rolled up by the coordinator (which owns both backends) so the thin client stays a pure HTTP client. Returns, in one call: Postgres `neo4j_outbox` status distribution, `technical_docs` count, and `community_summaries` totals (incl. `superseded` and `insight` counts); plus the Neo4j dream-cycle backlog — facts total / REM-pending / unconsolidated, and decisions total / REM-pending. The new `status` CLI command prints a compact human report (`--json` for machine-readable). Auth-protected like every non-`/health` route; each section is computed independently so a partial backend failure still returns what the other can.
- **Structured logging is now wired on by default in the gateway `.env`.** `MEMORY_LOG_LEVEL=2` and `AUDIT_LOG_PATH` were always supported but off; the deployment now sets them so save/enrich/defer events and the REM audit trail (`~/.shared-memory/logs/`) are durably captured — the gap that made the GPU-defer backlog invisible until grepped from an ephemeral stdout log. No code change; gated logging was already built into `memory_bridge.py`, `vector-skill.py`, and `consolidation_loop.py`.

### Fixed

- **REM now enriches `:Decision` nodes — the decision reasoning layer was dead code.** Decision→graph ingestion is two-phase: the outbox writes the provenance skeleton (Decision + Human/Project/AIAgent/Entity), and REM was meant to write the *reasoning* layer — `alternatives → CONSIDERED/REJECTED` and rationale ideas → `PRODUCES_INSIGHT/UNDER_CONDITIONS`. The enrichment code existed in `rem_loop.py` (the whole `is_decision` path through `_batch_fetch_content`, `_llm_process`, and `_write_neo4j_rem` Step 2), but three spots were `:Fact`-hardcoded — the work-selection query (`_fetch_non_rem_batch`), the edge-anchor + `rem_processed` mark in `_write_neo4j_rem`, and `_fact_is_consistent`. A decision has no `:Fact` node, so its `pg_id` was never selected and Step 2 never ran: all `:Decision` nodes carried `rem_processed: absent` and zero `CONSIDERED/REJECTED/PRODUCES_INSIGHT/UNDER_CONDITIONS` edges existed graph-wide. Now: selection matches `:Fact` **or** `:Decision`; `_write_neo4j_rem` anchors edges and the processed-mark on the correct node (a Decision sets `rem_processed` + a non-destructive `rem_summary`, never overwriting its rationale); and the Fact-content consistency check is skipped for decisions (enrichment-only). No outbox/schema change — `coalesce(rem_processed,false)=false` already matches the absent flag, so existing decisions become eligible on the next REM cycle. This turns the latent 2-hop entity bridges (e.g. the `Cloe` entity linking decisions and facts) into explicit, typed, traversable reasoning — the prerequisite for a future idea-layer and cross-project consolidation.

### Changed

- **MCP `save_artifact` now routes through the gateway — no more direct DB writes.** The LM Studio MCP tool (`vector-skill.py`) previously wrote Postgres and Neo4j itself (own psycopg2 INSERT + `MERGE`), the one save path that bypassed the coordinator's outbox. A crash between the two writes could leave Neo4j out of sync with Postgres — exactly the ADR-001 dangling-Fact risk the outbox pattern exists to prevent. `save_artifact` now `POST`s to `/memory/save` like `save_decision`/`save_retrospective`, so it gets the same server-side path: hard-mandate BGE-M3 embedding (503 if the embedder is down), SHA-256 idempotent upsert, and a `neo4j_outbox` row written in the **same transaction** and applied asynchronously by the outbox worker. The loaded model name (which auth would otherwise overwrite on `metadata.source` with the verified agent identity) is preserved in a new `metadata.model` field. Client-side metadata validation (JSON parse, dict check, `source` required) is kept for clear MCP errors before the call. `tests/test_vector_skill.py` updated: the save_artifact tests now assert gateway routing (object-bound metadata, model preserved, gateway-down + coordinator-error surfacing) instead of direct DB mocks.

## [0.4.2] — 2026-06-09

### Added

- **Client ↔ gateway version contract.** The gateway now reports `version` (informational build) and `api_version` (the wire contract) on `GET /health`, and the client (`memory_bridge.py`) sends its `API_VERSION` on every request via the `X-SM-Api-Version` header. Skew surfaces two ways: (1) **caller-facing** — a new `memory_bridge.py doctor` command (and an automatic hint appended to failed save/search output) prints `compat: ok | incompatible | unknown` and names which side to upgrade; (2) **gateway-log** — `coordinator.py` logs a one-time warning when a client's API version differs from the server's. `API_VERSION` starts at `1` and lives in both `coordinator.py` and `memory_bridge.py`; bump it only on a breaking protocol change. Fully backward compatible — old clients omit the header (server ignores), old gateways omit the fields (client reports `unknown`). `--version` now also prints `api_version`.
- **`Documentation/server-setup.md` — operations runbook.** A first-run guide for standing up and maintaining the **gateway host**: prerequisites, install sequence (clone → `.env` → compose → migrations → tokens → gateway → verify), the daemon roster, the upgrade procedure (`git pull` → `apply.py` → restart), the version contract, and health/observability. This is the home for everything that is *operations*, kept out of the client `SKILL.md`.

### Changed

- **GPU-aware dreaming is now platform-agnostic — process-name matching removed.** The `gpu_load.py` busy-check no longer tries to identify the inference server by matching GPU process cmdlines against `INFERENCE_PROC_MATCH` (`lmstudio|llmworker|llama`). That coupled the framework to specific server platforms (vLLM, llama-server, Ollama, TGI, a bare script, … all failed to match) and, worse, ignored the cases we most want to yield to: a user **chatting directly with the local LLM** (bypassing the gateway, so a request-counter is blind to it) and **unrelated GPU apps**. The gate now reads raw GPU utilisation: any GPU at/above `GPU_BUSY_PERCENT` counts as busy, regardless of what drives it. The only contract is "`:5000` serves an OpenAI-compatible completions endpoint" — no assumption about its process name. Default gates **every** GPU; `GPU_INDICES` still scopes it to specific cards. `INFERENCE_PROC_MATCH` is removed. nvtop, fail-open behaviour, the `WRITE_QUIESCE_SEC` time-guard, and NREM's hard backstop are unchanged. `tests/test_gpu_load.py` updated for the new semantics (an unrecognised process pegging the GPU now correctly counts as busy).
- **Health check probes the LLM via `/v1/models`, not `/health`.** The gateway's `GET /health` handler probed the reasoning LLM at `:5000/health`, a route OpenAI-compatible servers (LM Studio included) do not implement — so each health check logged an error on the LLM. It now probes `/v1/models`, which every OpenAI-compatible server serves. Embedder/reranker still use `/health` (llama.cpp containers that expose it). Health semantics unchanged (`status < 400 → ok`); no wire-contract change.
- **Skill is now a strict thin client; daemons are operations-only.** The skill package ships **only `memory_bridge.py`** — the agent never executes a daemon from its skill directory. The gateway, coordinator, and REM/NREM daemons run on the one gateway host from this repo and are reached over HTTP on `:8888`; a remote agent (no DB, no GPU) cannot run them. Concretely: `sync_skills.sh` `SCRIPTS` manifest trimmed to the client set (with a new `--prune` pass that removes daemon scripts older installs left in skill dirs); the six daemon scripts were `git rm`'d from the tracked `shared-memory-skill/` package; README install snippets now symlink `memory_bridge.py` alone (not the whole `scripts/` dir); `SKILL.md` slimmed to client concerns with a pointer to `server-setup.md`; README gained a "Two surfaces: usage vs. operations" section. Rationale: daemon and **schema** changes reach a hive through `git`, never through a skill download, so updating a skill never triggers a migration, and version compatibility is enforced by the `api_version` contract above rather than by copying backend code into clients.

### Fixed

- **JSONB columns were double-encoded as string scalars — `metadata->>'key'` silently returned NULL.** `coordinator.py` called `json.dumps()` on `metadata` and on the `neo4j_outbox` `cypher_params` before binding them to a `$N::jsonb` parameter, but the asyncpg pool already registers a jsonb codec with `encoder=json.dumps` (`_init_connection`). Every jsonb value was therefore serialised **twice** and stored as a JSON *string scalar* (`jsonb_typeof = 'string'`) instead of an object — so `metadata->>'type'`, `->>'project'`, `->>'entities'` etc. all returned `NULL`, and any SQL audit of metadata silently found nothing, even though the read path still worked (the codec decoder unwraps one layer). Rows saved through the psycopg2 MCP path (no codec) were stored correctly, so the corruption was partial — **133 of 170** `technical_docs` rows and **191** `neo4j_outbox` rows. The semantic pipeline (embed → outbox → Neo4j → consolidation) was unaffected because those paths `json.loads()` the value, which is why Decision/Fact nodes and consolidation looked healthy while SQL introspection lied. **Fix:** the manual `json.dumps()` calls are removed (the codec serialises once), and a defensive `_coerce_jsonb_obj()` guard parses any client-supplied stringified `metadata` back into an object on ingress. **Migration `008_fix_double_encoded_jsonb.sql`** normalises historical rows via `(col #>> '{}')::jsonb` (idempotent; only touches object/array-shaped string scalars). Three regression tests in `tests/test_coordinator.py` pin that `handle_save`/`handle_retrospective` bind dicts, not pre-serialised strings.
- ~~**`sync_skills.sh` now propagates `gpu_load.py`.**~~ *(Superseded by the thin-client change above — daemons, including `gpu_load.py`, are no longer shipped with the skill at all. The manifest is now client-only.)* The GPU-aware-dreaming module added in 0.4.1 is imported by `rem_loop.py` and `consolidation_loop.py`, but it was missing from the sync `SCRIPTS` manifest and the `shared-memory-skill/` package — so a daemon launched from a synced/packaged location would `ImportError`. Live daemons run from `shared-memory/scripts/` and were unaffected.

## [0.4.1] — 2026-06-08

### Added

- **GPU/inference-slot-aware dreaming** (`gpu_load.py` + `rem_loop.py` + `consolidation_loop.py`): REM and NREM now yield when the GPU running the LLM is busy, so background dreaming does not compete with active user inference. Detection is **cross-architecture via `nvtop --snapshot`** (one JSON code path for Nvidia/AMD/Intel — no driver-specific `nvidia-smi`/`rocm-smi`/sysfs parsing, which breaks per vendor; Intel Arc, for example, exposes no `gpu_busy_percent`). Only the GPU actually hosting the inference process is gated (matched by `INFERENCE_PROC_MATCH`, default `lmstudio|llmworker|llama`). **nvtop is a prerequisite for this feature but the probe fails open** — if nvtop is absent or errors, the check is skipped (logged once) and dreaming falls back to the existing `WRITE_QUIESCE_SEC` time-guard; NREM's hard backstop is never blocked by GPU state. Deferrals are logged at **WARNING**. The probe runs on the **infrastructure host** where REM/NREM live, so it measures the GPU that actually serves inference — including generation that remote clients trigger through the gateway; nvtop is therefore an infra-host prerequisite, not a remote-client one. Tunables: `SLOT_AWARE` (default on), `GPU_BUSY_PERCENT` (default 50), `INFERENCE_PROC_MATCH`, `GPU_INDICES` (manual override), `NVTOP_BIN`, `NVTOP_TIMEOUT_SEC`. Unit tests in `tests/test_gpu_load.py`.
- **Tier-3 trace-back pointers in search** (`coordinator.py`): the community-summary result returned by `/memory/search` now carries `source_pg_ids` and its `metadata` (entity, domain) instead of `metadata: None`. Agents can trace a synthesised narrative back to the exact Tier-1 facts it was built from and drill down via `/memory/graph` or `status/{pg_id}`. The data already existed in `community_summaries` (migration 003) — the search query simply was not selecting it. Covered by a new test in `tests/test_coordinator.py`.
- **Domain-scoped NREM consolidation** (`migration 007` + `consolidation_loop.py`): community summaries are now keyed on **(entity, domain)** instead of entity alone, where `domain = COALESCE(metadata->>'project', metadata->>'domain', scope, 'general')`. Facts that share an entity but belong to unrelated domains are no longer fused into one incoherent narrative, and density is re-gated per `(entity, domain)`. Backward compatible: untagged facts collapse to domain `general`, reproducing the previous single-summary-per-entity behaviour, so domain separation only takes effect once agents tag saves with `project`/`domain` (see SKILL.md "Save" guidance). New unit tests in `tests/test_domain_clusters.py` cover the partition rule. **Deploy note:** apply migration 007 and restart the gateway on the new code together — the old `ON CONFLICT ((metadata->>'entity'))` upsert depends on the index 007 replaces.
- **README "Quick Start" chapter**: a single front-to-back setup path for first-time users — resources (lean minimum 16 GB RAM / ~8 GB VRAM / ~30 GB disk), prerequisites, an 8-step sequence (code + OS limits → databases → schema → models → tokens/.env → gateway → install skill → use it), and a troubleshooting table (401 / 503 / search-500 / inotify + SELinux, plus "tell your agent to use the skill"). It only *points* to the detailed chapters (§4–§11) — no instructions are duplicated. Reasoning-LLM guidance names what we run (Qwen3-27B) and links the GraphRAG-cost article for the model trade-off. Carries a maintainer rule: any setup-affecting change must update Quick Start in the same change.
- **Base-schema migration** (`shared-memory/migrations/000_base_schema.sql`): creates the `vector` extension and the two base tables (`technical_docs`, `community_summaries`) with their embedding indexes — the original pre-001 schema. With it, `apply.py` (which globs every `*.sql` in order) now takes a brand-new empty `agent_data` database to the latest schema in **one command**, instead of requiring a manual `CREATE TABLE` step first. Idempotent (`IF NOT EXISTS`), so it is a no-op on existing installs and the same command serves new and upgrading users.

### Docs

- **README now documents the full Docker stack (inference layer included).** `postgres_neo4j_limits.yaml` runs **four** services — Postgres, Neo4j, and the BGE-M3 embedder (`retriever-api`, `:8070`) + BGE-Reranker (`reranker-api`, `:8071`) as llama.cpp `server` containers — but §5 showed only the two databases and §7/§9 described the embedder/reranker as separately-run `llama-server` processes.
  - §5 now shows the two inference services (with their model volume mount and Docker `healthcheck`) and adds two notes: **place the GGUF models in the host folder the compose mounts** (`/path/to/your/LLM_Models:/models`, at the `-m` sub-paths) and **use `docker compose ps` healthchecks as the first troubleshooting step** for 503s.
  - §7 reframed: the embedder/reranker run as the compose services (started by `docker compose up -d`); the manual `llama-server` form is the non-Docker alternative, with flags corrected to match the compose (`--rerank`, `-c/-b/-ub 8192`).
  - §9 startup reconciled: one `docker compose up -d` brings up databases **and** inference; the separate "start BGE-M3 / reranker" step is gone; the gateway is the only script started by hand. Health line points to `docker compose ps`.
  - Quick Start: step 2 starts the stack incl. inference and notes the model-folder placement; step 4 is now just the reasoning LLM; the 503 row points to `docker compose ps` healthchecks first.
- **Documentation fact-check pass**: reviewed every README chapter and standalone doc against current v0.4.0 code. Corrections:
  - README `--version` example output `0.3.6` → `0.4.0` (§10a, §11) to match `memory_bridge.py` `VERSION`.
  - README §11 coordinator API table `/health` response now lists `rem_daemon` (the gateway has emitted it since v0.4.0).
  - README §13 "The Sleep Cycle" rewritten for the v0.4.0 two-phase **REM/NREM** architecture (it still described the pre-0.4.0 single-stage daemon): REM enrichment (`rem_loop.py` — 120 s poll, batch 5, oldest-first, `applied`-gated, `rem_processed` set last), NREM `rem_processed` gate, and CommunitySummary supersession. Heading + TOC anchor updated.
  - README §14 "Audit Logging" — documented the v0.4.0 `AUDIT_LOG_PATH` REM outbox audit log (was only mentioned in the §19 roadmap): added it to the config table, a dedicated subsection with the JSON-lines format and field table, and clarified it is a separate, REM-daemon-side log from the `MEMORY_LOG_LEVEL` per-save logs. Fixed the dangling `rem_loop.py` docstring pointer to name §14.
  - Auth-introduction version aligned to **v0.3.5** (Phase 2C landed in 0.3.5, hardened through 0.3.6) across README §18/§19 and `SKILL.md`, matching `CHANGELOG`, `SECURITY.md`, and `system-prompt.md`.
  - README §19 schema-migrations row extended to cover migrations 004–006.
  - `SECURITY.md` audit-cadence pointer advanced past the completed v0.4.0 review.
  - README §6 "Database Schema" rewritten around the migration runner: a **one-command** new-install path (`apply.py` now creates base + all migrations), a separate **"Upgrading from an earlier schema"** section for installs that started on the original scheme, and an explicit note that Neo4j constraints are a one-time manual step (not auto-created). Previously §6 told new users to run only the base `CREATE TABLE` and never mentioned `apply.py`, leaving them on a pre-001 schema the coordinator cannot use.
  - README §3 architecture diagram updated to the current topology: both **REM and NREM** daemons (was a single "Consolidation Daemon"), current agent set (Claude/Grok/Codex/Antigravity CLI/LM Studio), and gateway auth + `127.0.0.1` bind. Added a **"Topology enforcement"** table listing every step that enforces the topology in code (single embedding path, localhost bind, caller auth, no direct DB access, read-only graph guard, outbox atomicity, hard embedding mandate, authenticated daemons).
  - README §12 "The Save Path" fact-checked against `coordinator.py`. Fixed two errors: the embed step said "via :8888" but the coordinator calls the embedder **directly at :8070** (`EMBED_URL`) — it runs inside the gateway, so hitting :8888 would loop on its own auth (added a note explaining why this doesn't break the single-embedding-space rule); and the retry backoff is **linear** (`0.5 s × attempt`), not exponential (count of 4 was right). The final step now reflects REM/NREM — the save's `NOTIFY` resets NREM's idle timer, but the fact is consolidated only after REM enriches it. Replaced dated "Phase 2 / Phase 4" labels with current wording and noted locks are acquired in sorted order (deadlock avoidance). Verified correct and unchanged: `ON CONFLICT (content_hash) DO UPDATE`, server-side `source` overwrite, atomic outbox insert + `pg_notify` in the same transaction.
  - README §11a "Complete Cycle" example modernized from Gemini CLI to **Antigravity CLI** (`agy`): Step 2 actor/heading/comment and the `source` value (`gemini_cli` → `antigravity`), plus the Step 4 attribution and Step 6 retrospective `--source`. (The `~/.gemini/skills/` install path is unchanged — Antigravity uses it.)
  - README §10 checked: token search order reworded for accuracy — `memory_bridge.py` loads `AGENT_TOKEN` from the skill-root `.env` (found by `find_dotenv()` walking up from `scripts/`) and a `scripts/`-adjacent `.env`; the doc had mislabeled which path finds which file. Confirmed the loader is two-source, `generate_tokens.py` yields 6 agents matching the startup-log example, and Step 3b reordered to lead with Antigravity CLI.
  - Removed the stale `~/.config/shared-memory/client.env` fallback from the docs (README §10 + SECURITY.md): the shared-fallback tier was dropped in code because a single shared token file could attach the wrong agent identity to a save, defeating server-side source verification. SECURITY.md's live setup step no longer offers it and now stresses a distinct per-agent token; the v0.3.5 historical fix note records that it was later removed and why.
  - README §11a "Complete Cycle" Step 3 rewritten for the two-phase sleep cycle: it described the pre-0.4.0 single-stage daemon ("after 15 min the consolidation daemon fires … ≥5 unconsolidated facts"). Now shows **REM enrichment** (120 s poll, `applied`-gated, sets `rem_processed`) **then NREM consolidation** (counts only `rem_processed` facts, supersession), with real daemon log lines, and notes that REM gates NREM — a fresh fact is searchable immediately but only enters Tier 3 after REM. §11a intro updated to name both phases.
  - README §14 "Audit Logging" fully reviewed against the logging code. The event list was incomplete and partly wrong: added the four undocumented events (`auth_failed`, `coordinator_down`, `save_failed`, `missing_source`), split the list by tool (the CLI `memory_bridge` and the MCP `vector_skill` emit different sets and name the unreachable-backend case differently — `coordinator_down` vs `gateway_down`), and expanded the level-2 row to enumerate every error event. Level 4 now correctly describes the `content_size_warn` field. The **daily merge** section was re-scoped to the **NREM** consolidation daemon (`consolidation_loop.py`) and now states explicitly that only the two per-tool save logs are merged — the REM `AUDIT_LOG_PATH` log is never rotated or merged. Per-tool table updated to the current CLI agent set.
  - README §18 "Open Problems" reviewed: the **Stored Prompt Injection** "Implemented" note now reflects that *both* LLM stages are hardened — the v0.4.0 REM enrichment pass (`rem_loop.py`), not just NREM consolidation — with Tier 1 retrieval still correctly flagged unprotected. The **Agent Authentication** entry, which was resolved in v0.3.5 but still sat in Open Problems with duplicated setup steps, is reframed as **RESOLVED** and points to §10 / SECURITY.md / §19 instead of repeating the rollout procedure.
  - README §19 roadmap reconciled with shipped code: **Phase C (retrospective layer)** was listed as planned but is implemented (`POST /memory/retrospective`, `save_retrospective`, dated `HAD_OUTCOME` edges) — moved to Completed. **Phase D** and **Phase 2C (auth)** were marked ✅ but sat in the Planned table — moved to Completed so every done item is in one place and Planned holds only genuinely-pending work (Phase E next). Agent-integration row updated to name Antigravity CLI (`agy`, Gemini now legacy); schema-migrations row now lists migration `000`.
  - `.env.example` trimmed to what a user actually sets — secrets, auth tokens, and the logging knobs that change what is recorded — with optional network overrides (`PROXY_BIND`, `COORDINATOR_URL`) commented. Removed internal/derived/test/hardcoded entries (`AGENT_ID`, `MOCK_LLM`, `WRITE_QUIESCE_SEC`, `PG_CONN`, and the never-read port vars) so the sample reflects only real, user-facing configuration.
- **`.env.example` reconciled with the code**: now lists exactly the parameters the scripts actually read, each set to its default value with a comment naming the README chapter that explains it.
  - Added the code-read vars that were missing/under-documented: `COORDINATOR_URL`, `AGENT_ID`, `MOCK_LLM`.
  - Removed vars the code never reads (they implied configurability that does not exist): `PROXY_PORT`, `EMBEDDER_PORT`, `RERANKER_PORT`, `NEO4J_BOLT_PORT`, `PG_PORT`, `OPENAI_BASE_URL`. The corresponding endpoints (`NEO4J_URI`, `EMBED_URL`, `RERANK_URL`, gateway argv port) are hardcoded in source and are now captured in a clearly-labelled "service endpoints — reference only" block.
  - Optional tuning vars (`PROXY_BIND`, `WRITE_QUIESCE_SEC`, `MEMORY_LOG_LEVEL`, `MEMORY_LOG_PATH`, `AUDIT_LOG_PATH`, `COORDINATOR_URL`) now show their real defaults inline.

---

## [0.4.0] — 2026-06-04

### Added

- **REM/NREM two-phase sleep cycle** (`shared-memory/scripts/rem_loop.py` new, `consolidation_loop.py` modified, `hive_mind_proxy.py` modified): Replaces single-stage consolidation with a two-pass sleep architecture modelled on biological memory consolidation.

  **REM daemon (`rem_loop.py`)** — new background process, auto-started by `hive_mind_proxy.py` alongside the consolidation daemon. On each idle scan (120 s poll, batch of 5):
  - Fetches oldest non-REM Fact nodes from Neo4j (pg_id ASC — clears the historical backlog first).
  - Gates on outbox `status='applied'` — only enriches facts confirmed written to Neo4j.
  - Batch-fetches full Postgres content in one query (a single AUTOCOMMIT connection per cycle, replacing per-operation connection churn).
  - Builds a closed typed-node registry from the graph (`Human`, `AIAgent`, `Project`, `Decision`, `Entity` nodes) — once a name is typed, its label never changes across the batch.
  - Single LLM round-trip per fact: three-part prompt (full text + closed entity set + ontology vocabulary) → produces a paragraph summary (≤5 sentences) and typed entity→relationship assignments.
  - Writes to Neo4j in one session: entity MERGE edges first, Decision extras second, then `SET f.content = summary, f.rem_processed = true` last — so a partial failure never marks a fact processed.
  - For Decision facts: additionally extracts `CONSIDERED`, `REJECTED`, `UNDER_CONDITIONS`, `PRODUCES_INSIGHT` edges on the Decision node.
  - Verifies Fact node consistency (full-string, not prefix) before touching the outbox.
  - Optionally writes outbox row to `AUDIT_LOG_PATH` (JSON-lines) before marking `rem_reviewed`.
  - Sends `pg_notify('new_artifact', pg_id)` to wake NREM after each enriched fact.

  **NREM (modified `consolidation_loop.py`)** — cluster query now requires `AND coalesce(neighbor.rem_processed, false) = true`. Raw (non-REM-enriched) facts are never consolidated directly. Threshold unchanged (5+ rem_processed unconsolidated facts per entity hub).

  **CommunitySummary supersession** — after every NREM consolidation, any existing active `community_summary` row whose `source_pg_ids` is a strict subset of the new summary's `source_pg_ids` is marked `superseded = true` in Postgres and linked with a `(new)-[:SUPERSEDES]->(old)` edge in Neo4j. Tier 3 search in `coordinator.py` filters `WHERE NOT superseded` — stale summaries are never surfaced.

  **`hive_mind_proxy.py`** — adds `_start_rem_daemon()`, `_watchdog_rem_daemon()` (same circuit-breaker logic as the consolidation watchdog). `/health` now reports `rem_daemon` status. Drain sequence stops both daemons cleanly.

- **Ontology additions** (`ontology.yaml`, `ontology.py`): Four new REM-enrichment relationship types used for decision capture in the knowledge graph:
  - `PRODUCES_INSIGHT`: `(Fact/Decision)-[:PRODUCES_INSIGHT]->(Entity)` — insight or knowledge this node generates
  - `UNDER_CONDITIONS`: `(Decision)-[:UNDER_CONDITIONS]->(Entity)` — constraints or conditions that bound the decision
  - `CONSIDERED`: `(Decision)-[:CONSIDERED]->(Entity)` — alternatives evaluated
  - `REJECTED`: `(Decision)-[:REJECTED]->(Entity)` — alternatives explicitly ruled out

- **Migration 006** (`shared-memory/migrations/006_rem_supersession.sql`):
  - `superseded BOOLEAN NOT NULL DEFAULT false` added to `community_summaries`; partial index `WHERE NOT superseded` keeps retrieval scans fast as superseded history accumulates.
  - Source normalisation backfill — historical pre-auth source variants normalised to canonical agent names: `claude_code`, `claude-code`, `claude_session`, `claude_code_fix`, `claude_code_session`, `claude_code_verification`, `claude-sonnet-4-6`, `design_session`, `architectural_hardening`, `architectural_fix` → `"claude"`; `workstation-assistant` + null-source rows → `"lm_studio"`; `design_session_cloe` left unchanged.

- **17 new tests** (`tests/test_rem_loop.py`): `_safe_label`, `_build_entity_registry`, `_resolve_rel` pure helpers; LLM mock output shape (plain fact vs decision); oldest-first ordering assertion; `rem_processed=true` SET last invariant; full-string consistency check; NREM `rem_processed` guard assertion; Tier 3 supersession filter assertion. **Total: 130 tests.**

### Changed

- **NREM cluster query** (`consolidation_loop.py`): Added `AND coalesce(neighbor.rem_processed, false) = true` to the density query — only REM-enriched facts participate in NREM synthesis.
- **Tier 3 search** (`coordinator.py`): `community_summaries` query now filters `WHERE NOT superseded`.
- **Outbox status lifecycle**: New terminal status `rem_reviewed` — REM writes this after verifying Fact consistency. Rows at `rem_reviewed` are safe to prune (handled by future `pruning_loop.py`).
- **`AUDIT_LOG_PATH` env var**: Set to a writable path to enable JSON-lines outbox audit log before `rem_reviewed` marking. Default: disabled.

### Fixed (post-release review — same day)

- **A2 — Search hard-crash without migration 006** (`coordinator.py`): `WHERE NOT superseded` referenced the new column unconditionally. Operators who restarted the gateway after a `git pull` without running `apply.py` got HTTP 500 on every vector-backed search. Fixed by wrapping the Tier 3 query in try/except and falling back to the unsupervised query with a migration warning.

- **A3 — CommunitySummary / ReasoningTrace in REM `_KNOWN_LABELS`** (`rem_loop.py`): Those node types are keyed by `pg_id`, not `name`. Including them in the set used to build `MERGE (e:{label} {name: n})` patterns could create structurally incompatible phantom nodes. Removed `ONT.community_summary`, `ONT.reasoning_trace`, `ONT.reasoning_step` from `_KNOWN_LABELS`. Practical risk was low because the closed-set query does not fetch those labels; removed as defence-in-depth.

- **A1 — NREM silence after upgrade invisible to operators** (`consolidation_loop.py`): After upgrading from v0.3.x, all existing facts have `rem_processed=NULL` — NREM correctly waits for REM enrichment but logged nothing. Added an INFO log explaining the expected silence window and pointing operators to `/health` → `rem_daemon`.

- **A4 — Deferred REM batch logged at DEBUG only** (`rem_loop.py`): When the outbox gate deferred all batch candidates, the reason was invisible at the default log level. Changed to INFO so operators see "N fact(s) deferred (outbox not yet applied)".

### Added (post-release)

- **Write quiesce for remote agents** (`rem_loop.py`): REM now skips its enrichment cycle if any fact was saved within `WRITE_QUIESCE_SEC` seconds (default 30, configurable via env var). Prevents REM's `pg_notify` calls from resetting NREM's idle timer during active write sessions from remote agents (e.g. chromebook-antigravity). See README §REM daemon and `.env.example`.

- **Skill sync script** (`shared-memory/scripts/sync_skills.sh`): executable shell script that copies canonical sources to all agent install paths. Run after every code change: `bash shared-memory/scripts/sync_skills.sh`.

### Upgrade path (from v0.3.x)

**Expected NREM silence after upgrade:** all existing facts have `rem_processed=NULL`.
NREM waits for REM to enrich each cluster before synthesising. At batch_size=5 and poll=120s,
a graph with ~80 facts clears the backlog in ~30 minutes. Monitor with:
```bash
# Check how many facts still need REM processing
curl http://localhost:8888/health  # rem_daemon: running
```

Users upgrading from any v0.3.x release must apply migration 006 before restarting the gateway:

```bash
# From the repo root
PG_PASSWORD=<your_password> uv run --with psycopg2-binary python shared-memory/migrations/apply.py
```

Migration 006 adds the `superseded` column — the coordinator will fail to serve searches correctly without it. The source normalisation in the same migration is idempotent (safe to re-run). After migrating, restart the gateway; `rem_loop.py` will start automatically.

---

## [0.3.6] — 2026-06-01

### Fixed

- **Relative path in SKILL.md broke remote installs (Bug 1 — script not found):** All 20 CLI commands used bare `scripts/memory_bridge.py`. Skill runners execute from the user's project directory, not the skill directory — the script was silently not found on any non-local install. All commands now use the canonical absolute path `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` with an AI-instruction block providing the per-agent prefix substitution table.
- **Relative path broke token loading (Bug 2 — same root cause):** `memory_bridge.py` resolves `.env` files via `os.path.abspath(__file__)`. With a relative invocation path, `__file__` resolved against CWD, pointing `.env` lookups at the wrong directory and silently dropping the agent token. Fixed by the same absolute-path change.

### Changed

- **Removed `client.env` universal token fallback:** `~/.config/shared-memory/client.env` removed from `memory_bridge.py` (both branches), `generate_tokens.py`, SKILL.md, `.env.example`, and README. Agent tokens are identity — the coordinator stamps verified `source` on every saved artifact. A shared fallback token collapses all agent attribution in the knowledge graph. Per-agent skill `.env` is now the only supported method.
- **Antigravity CLI (`agy`) added as primary replacement for Gemini CLI.** Both tools share `~/.gemini/skills/`. Gemini CLI marked legacy in SKILL.md and README. `chromebook-antigravity` token documented for remote instances of `agy`.

---

## [0.3.5-post] — 2026-05-29

### Fixed

- **Auth self-loop in coordinator embedding calls** (`coordinator.py`): `EMBED_URL` was pointing to `:8888` (the proxy the coordinator lives inside), causing internal embedding calls to hit the auth middleware — which has no token and returns 401, aborting every save. Changed to `:8070` direct, consistent with `RERANK_URL` which already used `:8071` direct for the same reason. External agents still route embeddings through `:8888` and must authenticate.

- **Dotenv `or`-chain only loaded one file** (`memory_bridge.py`, skill copy): The three-tier fallback used Python `or` chaining — once `find_dotenv()` returned the project `.env` (which has `AGENT_TOKENS` for the gateway but not `AGENT_TOKEN` for the agent), the skill `.env` and `~/.config/shared-memory/client.env` were never reached. Changed to a `for` loop with `load_dotenv(..., override=False)` so all three sources contribute and the first definition of each variable wins.

- **`python-dotenv` not available in bare `uv run --with httpx`** (`memory_bridge.py`, skill copy): When agents run `uv run --with httpx python scripts/memory_bridge.py` without `--with python-dotenv`, the `try: from dotenv import ...` block hit `ImportError` silently and no `.env` was ever loaded, causing `_auth_headers()` to return `{}` and every call to get 401. Added a plain-Python fallback in the `except ImportError` block that manually parses the skill-root `.env` (one directory above `scripts/`) and `~/.config/shared-memory/client.env` — auth tokens are loaded with no dependencies.

- **Missing `_auth_headers()` on reranker and health-check calls** (`vector-skill.py`): `_auth_headers()` was added to `save_decision()` and `save_retrospective()` in v0.3.5 but missed three HTTP call sites that go through port 8888: the reranker call in `hybrid_search_and_rerank()` and both health-check probes in `check_memory_health()`. All six `client.post()` call sites in `vector-skill.py` now pass `_auth_headers()`.

- **Gemini CLI skill was at v0.3.3** (pre-coordinator): The Gemini skill directory at `~/.gemini/skills/shared-memory/scripts/memory_bridge.py` had never been synced after the coordinator refactor. It still had direct `import psycopg2` and `import neo4j` at the top, no auth support, and crashed on every call with `ModuleNotFoundError`. Synced to v0.3.5 — all five skill install locations now verified identical to canonical source after every change.

### Documentation

- **README `uv run` commands**: Added `--with python-dotenv` to every `memory_bridge.py` invocation. Added full dependency list (`--with asyncpg --with neo4j --with httpx`) to gateway startup command. Added "Token search order" section explaining the three-tier dotenv fallback. Fixed smoke-test command (had literal `\n` instead of a real line break).

- **SKILL.md** (all copies): Added `--with python-dotenv` to all `uv run` commands. Updated Authentication Setup section with dotenv search order, `curl /health` verify step, 401 error hint.

- **Complete Cycle section** (README §11a): End-to-end walkthrough — Claude Code saves a decision, Gemini CLI saves a plain fact, consolidation synthesises, Grok retrieves both (annotated response showing Tier-3 + Tier-1 + `graph_context`), named query shortcuts, retrospective closes the loop, LM Studio MCP equivalents.

- **Security review** (v0.3.5 post-release): All three candidate findings filtered as false positives — Cypher injection (regex strips single quotes and backslashes, no breakout possible), write-Cypher guard bypass (Neo4j read-only session is a second independent layer), cross-DB atomicity in consolidation (data integrity issue, no security exploitation path).

---

## [0.3.5] — 2026-05-29

### Security

- **Phase 2C — Agent token authentication** (`coordinator.py`, `hive_mind_proxy.py`, `memory_bridge.py`, `vector-skill.py`): The Memory Coordinator now enforces `Authorization: Bearer <token>` authentication on all routes. Any process that cannot present a registered token is rejected with HTTP 401. This closes the last open security finding — previously any localhost process could read/write shared memory and claim any agent identity.

  - **`coordinator.py` — `_load_agent_tokens()` + `auth_middleware`**: Parses `AGENT_TOKENS` env var (`name:token,...`) into a token→agent mapping. Middleware is DEFAULT DENY — every route except `_UNPROTECTED_PATHS = {"/health"}` requires a valid token. Trailing-slash normalisation (`/health/` passes). Duplicate-token guard: if two agents share a token the second mapping is discarded and a WARNING is logged. `source` in saved metadata is forcefully overwritten with the server-verified agent name — clients cannot spoof identity.

  - **`hive_mind_proxy.py`**: `auth_middleware` registered globally on the aiohttp app — applies to all routes including the catch-all proxy (which would otherwise be an unauthenticated SSRF relay if `PROXY_BIND=0.0.0.0` is used). `/health` reports `auth_required` flag.

  - **`memory_bridge.py` + skill copy**: `_auth_headers()` reads `AGENT_TOKEN` and injects `Authorization: Bearer` on all coordinator calls. Three-tier dotenv fallback: `find_dotenv()` → script-adjacent `.env` → `~/.config/shared-memory/client.env`. Explicit 401 handling with clear error message and hint.

  - **`vector-skill.py`**: Same `_auth_headers()` pattern. Replaces `"Bearer none"` placeholder. Auth headers added to `save_decision()` and `save_retrospective()` coordinator calls.

  - **`scripts/generate_tokens.py`** (new): Bootstrap utility — generates cryptographically random `tok_` prefixed tokens for all 6 agents and prints ready-to-paste `.env` lines.

  - **Backward compatible**: `AGENT_TOKENS` unset → auth disabled, all requests pass through (no behaviour change for existing installs).

  - **22 new tests** — token loading, middleware DEFAULT DENY, allowlist, valid token, rejection cases, source overwrite, `_auth_headers()`, 401 response handling. **Total: 113 tests.**

---

## [0.3.4] — 2026-05-29

### Security

- **S1 (HIGH) — `/memory/graph` read-only enforcement** (`coordinator.py`): Neo4j session for `handle_graph` now opens with `default_access_mode="READ"`. Driver-level write enforcement is layered on top of the existing `_WRITE_CYPHER` keyword regex — a regex-bypassing query can no longer execute writes at the Neo4j protocol level.

- **S2 (HIGH) — Async consolidation daemon** (`consolidation_loop.py`): Migrated from synchronous `GraphDatabase` driver and `psycopg2` calls inside `async def` to `AsyncGraphDatabase` + `loop.run_in_executor()`. The event loop no longer blocks during Neo4j or Postgres I/O, preventing `LISTEN/NOTIFY` signal drops under write bursts. `connect_timeout=5` added to all `psycopg2.connect()` calls.

- **S3 (HIGH) — TOCTOU fix in `handle_retrospective`** (`coordinator.py`): The `SELECT` existence check and `INSERT INTO neo4j_outbox` are now wrapped in a single `conn.transaction()` with `SELECT ... FOR SHARE`. A concurrent delete of the target row between check and insert can no longer produce a dangling outbox entry and a silent missing `HAD_OUTCOME` edge.

- **S4 (MEDIUM) — `--project` filter in named query templates** (`memory_bridge.py`): The `WHERE p.name CONTAINS '...'` clause appended directly after `OPTIONAL MATCH ... (p:Project)` was parsed as an inline WHERE (filtering what value `p` gets, not which rows return). Added `WITH d, [vars], p` before the project WHERE in `who-decided`, `agent-decisions`, and `why-to-check` — the filter now correctly excludes decisions not linked to the specified project.

- **S5 (MEDIUM) — Dead embedding property on Neo4j nodes** (`vector-skill.py`): `f.embedding`, `t.task_embedding`, and `s.embedding` removed from all three Neo4j write calls in the LM Studio MCP path. The property was never used in any Cypher query (all similarity search goes through `pgvector`) and consumed ~8 KB of Neo4j heap per node.

- **S6 (MEDIUM) — Audit log OSError surfaced to stderr** (`memory_bridge.py`, `vector-skill.py`): `_append_log` now catches `OSError` (disk full, permission denied) and prints a warning to `stderr` before the bare `except Exception: pass` fallback. Disk/permission failures are no longer silent.

- **S7 (MEDIUM) — Outbox double-processing on concurrent restart** (`coordinator.py`): `_drain_outbox` now atomically claims rows with `UPDATE ... SET status='in_progress' WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING ...` before releasing the lock. A second coordinator instance SKIP LOCKs `in_progress` rows. `start()` resets any `in_progress` rows (crash survivors) back to `pending` on startup. The failure-path update no longer conditions on `AND status='pending'`, correctly resetting claimed-but-failed rows.

---

## [0.3.3] — 2026-05-29

### Added

- **Named query templates — Phase D** (`shared-memory/scripts/memory_bridge.py`, `shared-memory-skill/shared-memory/scripts/memory_bridge.py`, `tests/test_memory_bridge_query.py`): Converts the provenance graph from a queryable archive into a usable pre-task protocol — four named shortcuts so agents never need to write raw Cypher for standard provenance questions.

  - **`memory_bridge.py query <template> [filters]` subcommand**: Routes through the existing `query_graph()` → `/memory/graph` coordinator path. No new endpoint, no coordinator changes.

  - **Four templates**:
    - `who-decided [--title TEXT] [--project TEXT]` — returns Decision + Human + AIAgent + Project attribution chain.
    - `agent-decisions [--assisted-by TEXT] [--project TEXT]` — all decisions an AI agent assisted with.
    - `retrospectives [--rating TEXT]` — all HAD_OUTCOME records, optionally filtered by rating.
    - `why-to-check --title TEXT [--project TEXT]` — HAD_OUTCOME records for a given decision topic; `--title` is required (without a topic the result is unscoped). **Intended as the standard pre-task check**: run before starting work in any area with prior decisions.

  - **`_build_query(template, args) -> str`** pure function: builds the Cypher string from template name and parsed args. Filter values are scrubbed with `re.sub(r"[^A-Za-z0-9 _.-]", "", ...)` before interpolation — prevents quote-escape injection. OPTIONAL MATCH + WHERE is used for joined filters (project, assisted_by) so rows without the optional edge are still returned (p/a is NULL, not excluded).

  - **Raw Cypher path preserved**: the existing `graph` subcommand is unchanged and explicitly documented in SKILL.md alongside the named shortcuts. Custom traversals, multi-hop paths, and cross-entity queries still use `memory_bridge.py graph "<cypher>"`.

  - **`shared-memory/SKILL.md` and `shared-memory-skill/shared-memory/SKILL.md`** — Task 3 restructured: named shortcuts section (with `why-to-check` trigger) + raw Cypher section (with read-only enforcement note). No new task section added; every added line carries unique information not present elsewhere.

  - **7 new tests** (`tests/test_memory_bridge_query.py`): pure-function shape tests for all four templates, sanitisation check (`;` and `'` stripped), unknown-template exit, and CLI integration (mock `query_graph`, assert called with non-empty Cypher). Total: **91 tests passing**.

---

## [0.3.2] — 2026-05-29

### Added

- **Retrospective layer — Phase C** (`shared-memory/scripts/coordinator.py`, `shared-memory/scripts/memory_bridge.py`, `vector-skill.py`, `shared-memory-skill/shared-memory/scripts/memory_bridge.py`): Closes the Why-To loop — agents can now record whether a past decision held up, enabling retrospective queries before executing new tasks in the same area.

  - **`POST /memory/retrospective` endpoint** (`coordinator.py`): Accepts `{pg_id, rating, notes, date?, agent_id?}`. Verifies the target `pg_id` exists in `technical_docs`, then writes to `neo4j_outbox` with `type=retrospective`. No new `technical_docs` row — retrospectives do not pollute semantic search.

  - **`_apply_retrospective_outbox_row()`** (`coordinator.py`): Outbox worker method. Issues `MATCH (d:Decision {pg_id}) CREATE (d)-[:HAD_OUTCOME {rating, date, notes}]->(d)` — a self-loop per call. Multiple retrospectives per Decision are allowed; the Why-To query uses `ORDER BY o.date DESC` to surface the most recent.

  - **`build_retrospective_payload()` + `save_retrospective_artifact()`** (`memory_bridge.py`): Pure helper and async HTTP client for `POST /memory/retrospective`. Pattern mirrors `build_decision_metadata()` / `save_artifact()` from Phase B.

  - **`memory_bridge.py save_retrospective` subcommand**: Flags: `--pg-id` (required, int), `--rating` (required), `--notes` (required), `--date` (optional ISO, default today), `--source` (optional, default `$AGENT_ID`).

  - **`vector-skill.py save_retrospective` MCP tool**: LM Studio can record retrospectives through the same coordinator path.

  - **`shared-memory-skill` Gemini copy updated**: `build_retrospective_payload()`, `save_retrospective_via_coordinator()`, and `save_retrospective` subcommand added.

  - **`datetime` import added** to `coordinator.py` (was missing; required by `handle_retrospective`).

  - **10 new tests**: 7 in `tests/test_memory_bridge_retrospective.py` (new file — pure helper shape, date default, explicit date, source default, source override, CLI forwarding, CLI missing-flag exit) and 3 additions to `tests/test_coordinator.py` (outbox dispatch, HAD_OUTCOME Cypher, 400 on missing fields). Total: **84 tests passing**.

  - **`shared-memory/SKILL.md`** (both copies): Task 5 — Save a Retrospective added with CLI, MCP, and Why-To query examples.

  - **`shared-memory/Documentation/schema.md`**: Retrospective write protocol section added under `HAD_OUTCOME` relationship row.

---

## [0.3.1] — 2026-05-28

### Added

- **Retrieval visibility** (`coordinator.py`): search results now include `tier` ("fact" | "community_summary"), `score_normalized` (sigmoid of raw reranker logit → [0, 1]), `matched_entities` (intersection of query string against `metadata["entities"]`), and `graph_context` as a structured list of `{rel_type, name, label}` objects instead of an opaque pipe-separated string. Keyword-fallback results carry the same shape.

- **Consolidation history** (`consolidation_loop.py`, migration `004_summary_history.sql`): `community_summaries` gains a `summary_history JSONB NOT NULL DEFAULT '[]'` column. On every `ON CONFLICT DO UPDATE`, the outgoing `content`, `source_pg_ids`, and `timestamp` are appended (capped at 20 entries) before the row is overwritten. Enables drift auditing without a temporal schema.

- **`source_ref` lineage convention** (`coordinator.py` outbox, `schema.md`, both `SKILL.md` copies): agents may include `"source_ref"` in metadata to record the sub-document origin of a fact (e.g. `"design-doc.pdf#p12"`, `"meeting-2026-05-15.mp4@00:04:32"`). Propagated through coordinator to `cypher_params`; outbox worker stores it as `Fact.source_ref` property in Neo4j.

- **14 new tests** (`tests/test_coordinator.py`): `_sigmoid()` (4 tests), `_matched_entities()` (6 tests), `source_ref` outbox propagation (2 tests), search response shape — `tier` / `score_normalized` / `matched_entities` / `graph_context` list (2 tests). Total: 74 tests passing.

- **ApertureData reference + three diagnostic tests** (`README.md §1`): Vishakha Gupta's *AI Memory & Cognition: The Architect's Playbook* (May 2026) attributed in §20 References. Three diagnostic questions (Retrieval · Consolidation · Lineage) asked and answered with current implementation state at the end of the Vision section. Updated with every release.

### Fixed

- **`schema.md` inaccuracy**: `community_summaries` "Growth behaviour" section incorrectly stated "appends a new row" per cycle. The code applies `ON CONFLICT DO UPDATE` — one row per entity, replaced. Documentation now matches the code.

---

## [0.3.0] — 2026-05-28

### Added

- **Decision shortcut — Phase B** (`shared-memory/scripts/memory_bridge.py`, `vector-skill.py`, `tests/test_memory_bridge_decision.py`, `tests/test_vector_skill.py`): Low-friction `save_decision` command and MCP tool so agents don't need to hand-craft the full `type=decision` JSON payload.

  - **`memory_bridge.py save_decision` subcommand** — accepts named flags (`--title`, `--decided-by`, `--project`, `--rationale`, `--source`, `--assisted-by`, `--alternatives`, `--confidence`, `--entities`). Comma-separated strings for list fields. Builds the correct `type=decision` metadata shape via `build_decision_metadata()` and forwards to the coordinator. Required flags: `--title`, `--decided-by`, `--project`, `--rationale`; missing flags print usage and exit non-zero.

  - **`vector-skill.py save_decision` MCP tool** — individual typed parameters instead of raw JSON. Routes through the coordinator (HTTP to port 8888) so the Decision outbox path and PROV-O subgraph write are handled consistently. Required: `title`, `decided_by`, `project`, `rationale`, `source`. Optional: `assisted_by`, `alternatives`, `confidence`, `entities` (all comma-separated).

  - **`build_decision_metadata()` pure helper** (`memory_bridge.py`) — separates metadata construction from I/O for clean unit testing. Returns `(content_str, metadata_dict)`.

  - **`tests/test_memory_bridge_decision.py`** — 7 new tests covering shape, optional fields, ISO date, empty commas, source default, CLI forwarding, and missing-flag exit code.

  - **`tests/test_vector_skill.py`** — 3 new tests: success (correct payload + pg_id), coordinator unreachable (error references `hive_mind_proxy.py`), 400 error surfaced to caller.

  - **SKILL.md** (both locations): Task 4 updated — `save_decision` shortcut is now the recommended path; MCP tool noted; raw-JSON path marked as legacy.

  - **`shared-memory-skill/shared-memory/scripts/memory_bridge.py`** (Gemini copy): `save_decision` action added, routes through coordinator.

- **Codex CLI integration** (`AGENTS.md`, `shared-memory/SKILL.md`, `shared-memory-skill/shared-memory/SKILL.md`, `AGENT.md`, `README.md`): OpenAI Codex CLI documented and supported as a fifth skill-based agent.

  - **`AGENTS.md` (new)** — Codex CLI project context file (their `CLAUDE.md` equivalent); read automatically before each Codex session. Contains architecture, commands, key invariants, and a `$shared-memory` invocation note.

  - **SKILL.md YAML frontmatter** — both `shared-memory/SKILL.md` and `shared-memory-skill/shared-memory/SKILL.md` now carry `name` and `description` frontmatter required by Codex CLI for implicit skill matching.

  - **Agent table in SKILL.md Overview** — "agents currently integrated" section upgraded from a prose list to a summary table covering all five agents (Claude Code, Grok, Codex CLI, Gemini CLI, LM Studio) with invocation syntax and install path.

  - **`AGENT.md`** — Codex CLI row added to agent access split table.

  - **`README.md`** — Codex CLI badge; §1 agent overview; full §10 setup section (install path, explicit `$shared-memory` invocation, implicit invocation via frontmatter description matching, `AGENTS.md` note); §11 agent access table row.

---

## [0.2.9] — 2026-05-28

### Added

- **Decision provenance layer — Phase A** (`ontology.yaml`, `shared-memory/scripts/ontology.py`, `shared-memory/scripts/coordinator.py`, `tests/test_coordinator.py`): PROV-O-inspired provenance nodes and relationships for recording architectural and design decisions with full attribution context.

  - **`ontology.yaml`** — 6 new provenance labels (`Decision`, `Human`, `AIAgent`, `Project`, `Activity`, `Milestone`) and 8 provenance relationships (`WAS_ATTRIBUTED_TO`, `WAS_ASSISTED_BY`, `PROJECT_OF`, `WAS_GENERATED_BY`, `ACTED_ON_BEHALF_OF`, `SUPERSEDES`, `INFORMED_BY`, `HAD_OUTCOME`). All configurable via the existing `ontology.yaml` override mechanism.

  - **`coordinator.py` ingress validation** — saves with `metadata["type"] == "decision"` are validated at ingress before any DB write. Missing required fields (`decided_by`, `project`, `rationale`) return HTTP 400 with a descriptive error listing the missing fields. Plain fact saves are unaffected.

  - **`coordinator.py` outbox dispatch** — `_apply_outbox_row` routes `type=decision` saves to the new `_apply_decision_outbox_row` method. Writes a `Decision→Human→Project→AIAgent` subgraph in a single Neo4j session with `WAS_ATTRIBUTED_TO`, `PROJECT_OF`, `WAS_ASSISTED_BY`, and `MENTIONS` edges. `FOREACH` (not `UNWIND`) used for `assisted_by` and `entities` lists — handles empty lists safely without dropping the write.

  - **`tests/test_coordinator.py`** — 8 new tests: ingress validation (missing all required fields → 400; single missing field named in error); plain fact regression; valid decision passes validation; outbox dispatch routing to decision path and not to it for plain facts; Neo4j write shape (correct labels + relationship types + kwargs); empty `assisted_by` does not crash.

- **Schema documentation** (`shared-memory/Documentation/schema.md`): Full Neo4j section replaced with provenance labels table (Phase A), provenance relationships table with PROV-O patterns and meanings, Cypher query examples (who-decided, agent contributions, Why-To loop), and the decision save protocol JSON example.

- **All 5 SKILL.md locations**: Task 4 — Decision Provenance — added to every agent skill file with CLI save example, required fields list, three-step write flow, and Cypher query template for retrieving saved decisions.

- **README** (`README.md`): "What we are building toward" vision subsection with target question and answer shape; "Saving everything vs. saving what matters" with concrete queryable examples (who/when/why/conditions/outcome) and counter-examples (what stays in Git); roadmap updated — Phase A marked done, Phases B–E (CLI/MCP tools, retrospectives, named query templates, pruning) listed as planned.

- **CLAUDE.md** — test command updated to include `--with asyncpg --with aiohttp` (required since `coordinator.py` imports both at module level).

---

## [0.2.8] — 2026-05-27

### Security

- **Starlette BadHost floor (`requirements.txt`)**: Added explicit `starlette>=1.0.1` dependency to enforce the minimum version that contains the fix for **CVE-2026-48710** (BadHost). The vulnerability allows a crafted `Host` header containing `/`, `?`, or `#` to shift path parsing so that `request.url.path` no longer matches the path the ASGI router received — bypassing any path-based auth middleware while the route still executes. Our `vector-skill.py` runs over `stdio` (no HTTP surface) and `hive_mind_proxy.py` uses `aiohttp` (unaffected), so real-world exposure was nil. The floor is added defensively in case the transport ever changes.

### Maintenance

- **Dependency lower bounds raised** (`requirements.txt`, `requirements-dev.txt`): All minimum versions advanced to match currently tested releases — `aiohttp` 3.13.5, `asyncpg` 0.31.0, `httpx` 0.28.1, `psycopg2-binary` 2.9.12, `neo4j` 6.2.0, `fastmcp` 3.3.1, `python-dotenv` 1.2.2, `pytest` 9.0.3, `pytest-asyncio` 1.4.0.

---

## [0.2.7] — 2026-05-27

### Added

- **`source_pg_ids INTEGER[]` column on `community_summaries`** (`migrations/003_source_pg_ids.sql`, both `consolidation_loop.py` copies): Promotes contributing fact IDs from the `metadata` JSONB blob to a first-class queryable array column. Enables `WHERE $pg_id = ANY(source_pg_ids)` provenance queries — any caller can now trace which `technical_docs` rows contributed to a consolidated summary without parsing JSON. Existing rows are back-filled from `metadata` in the migration. The `ON CONFLICT DO UPDATE` clause in the skill-copy consolidation loop was also corrected (missing since migration 002). Apply: `uv run --with psycopg2-binary python shared-memory/migrations/apply.py 003_source_pg_ids.sql`.

---

## [0.2.6] — 2026-05-26

### Added

- **Mandatory `source` provenance on save** (`coordinator.py`, `vector-skill.py`): The coordinator now rejects saves with HTTP 400 if `metadata.source` is absent or empty. `vector-skill.py`'s `save_artifact` MCP tool applies the same check before reaching Postgres. Facts without a declared source (agent or model name) are refused to prevent unattributed content from polluting the memory store. The system prompt for LM Studio updated to instruct the model to self-identify by model name (e.g. `"source":"qwen3-27b"`) in every save call.

- **Entity-graph fallback for low-confidence searches** (`vector-skill.py`): When all reranker scores fall below `LOW_CONFIDENCE_THRESHOLD = -3.0`, `hybrid_search_and_rerank` triggers `_graph_entity_fallback()`. The helper extracts significant words from the query, matches them against `Entity.name` nodes in Neo4j via `MENTIONS` edges to `Fact` nodes, and fetches full content from Postgres. Results are appended as a clearly labelled supplementary section; main episodic results are always returned regardless.

### Fixed

- **Reranker timeout in LM Studio** (`vector-skill.py`): `hybrid_search_and_rerank` was sending up to 20 full-content candidates (~44 KB total) to BGE-Reranker in a single call, exceeding the 20-second timeout and producing `httpx.ReadTimeout` with empty `str(e)`. Fixed by reducing the Postgres candidate pool from 20 to 10 and raising the rerank-specific timeout to 120 s (`RERANK_TIMEOUT`). Embedding calls retain the 20 s timeout (`EMBED_TIMEOUT`). Documents are sent in full — no truncation.

### Maintenance

- **Database cleanup**: Removed 54 garbage entries from `technical_docs` — `RECONSTRUCTION COMPONENT` source-code blobs (including an old `vector-skill.py` with plaintext credentials), test fixtures (`TestEntity_*`), smoke-test entries, and stale duplicate documentation files. Removed the mocked `TestEntity` community summary. Cleaned 50 orphaned Neo4j `Fact` nodes and 14 orphaned `Entity` nodes.

---

## [0.2.5] — 2026-05-26

### Added

- **Daemon watchdog with auto-restart** (`hive_mind_proxy.py`): Replaced the one-shot `_monitor_daemon()` with a persistent `_watchdog_daemon()` asyncio task. The watchdog restarts the consolidation daemon on unexpected crashes with exponential backoff (1 s → 60 s ceiling), resets backoff after ≥ 30 s of stable uptime, and trips a circuit breaker after ≥ 5 crashes within 10 minutes (logs CRITICAL, stops restarting, requires gateway restart to reset). Clean exits (`0` or `-SIGTERM`) are not restarted.

- **`GET /health` endpoint** (`hive_mind_proxy.py`): Probes embedder (:8070), reranker (:8071), and LLM (:5000) with 2 s timeouts using the proxy's existing connection pool. Reports consolidation daemon liveness from watchdog state. Returns HTTP 200 if embedder + reranker are both reachable (the critical save/search path); HTTP 503 if either is down. LLM and daemon status are reported informationally — their unavailability degrades consolidation only, not saves or searches. Note: embedder and reranker already have Docker `healthcheck` + `restart: always` as primary recovery; this endpoint provides immediate observability and covers non-Docker backends.

- **Four-agent skill integration**: Claude Code (`~/.claude/skills/shared-memory/`), Grok (`~/.grok/skills/shared-memory/`), Gemini CLI (`~/.gemini/skills/shared-memory/`), LM Studio (MCP via `rag-orchestrator`). Claude Code and Grok scripts are symlinked to the repo; Gemini CLI uses flat copies.

### Fixed

- **`python-dotenv` loading in `vector-skill.py` and `memory_bridge.py`**: Both scripts now load `.env` from the repo root at startup via `python-dotenv`. Agents such as LM Studio and Grok spawn these scripts as subprocesses without inheriting the shell environment; credentials were silently empty strings. Graceful `ImportError` fallback if `python-dotenv` is absent. Added `--with python-dotenv` to `mcp.json` `uv run` args.

- **Removed `neo4j-memory` from `mcp.json`**: The `neo4j-agent-memory` MCP server connects directly to Neo4j via bolt, bypassing the coordinator's per-entity locks, outbox atomicity, SHA-256 deduplication, and read-only Cypher guard. Writes produced orphaned Neo4j nodes invisible to semantic search. `rag-orchestrator` already performs Neo4j graph expansion on every search call; no separate graph MCP is needed.

- **System prompt search-first directive** (`system-prompt.md`): Rewrote the `COGNITIVE HIERARCHY` section with explicit "MUST call `rag-orchestrator` first, no exceptions" language. Added explanation that `rag-orchestrator` already includes Neo4j expansion; demoted `neo4j-memory` to a deprecated note. A weak ordinal list was insufficient — the model was calling `neo4j-memory` first.

---

## [0.2.0] — 2026-05-26

### Fixed — Concurrency hardening (coordinator + consolidation daemon)

- **C1 — Lock release on partial acquisition** (`coordinator.py`): The lock-release loop in `handle_save` iterated over all entity locks including ones never acquired. If `lk.acquire()` was cancelled mid-list, `RuntimeError: release unlocked lock` would surface. Fixed by tracking an `acquired` list and releasing only locks that were actually acquired.

- **C2 — Double-drain under concurrent coordinator instances** (`coordinator.py`): `_drain_outbox` had no row-level locking. Two coordinator instances starting concurrently (e.g. during a proxy restart overlap) could pick the same `neo4j_outbox` rows. Fixed with `FOR UPDATE SKIP LOCKED` inside a transaction; rows held by one instance are silently skipped by the other.

- **C3 — Lost retry increment under concurrent updates** (`coordinator.py`): The retry increment used a Python-computed value (`retries=$1`). Two instances processing the same row wrote identical values, so the counter never advanced past 1 and the max-retries check never fired. Fixed with `SET retries = retries + 1 WHERE id=$1 AND status='pending'` — atomic at the database level.

- **C4 — Non-atomic Neo4j writes** (`coordinator.py`): `_apply_outbox_row` made three sequential `session.run()` calls (Fact MERGE, Entity MERGE, MENTIONS MERGE). A transient Neo4j timeout after the Fact MERGE left MENTIONS edges permanently missing. Replaced with a single `UNWIND`-based query that creates the Fact, all Entity nodes, and all edges in one round-trip.

- **C5 — Duplicate community_summaries rows** (`consolidation_loop.py`, `migrations/002_concurrency_hardening.sql`): `INSERT INTO community_summaries` had no conflict guard. Two consolidation runs for the same entity (e.g. proxy restart overlap) both succeeded, producing duplicate rows; retrieval via `ORDER BY id DESC LIMIT 1` became non-deterministic. Fixed with `ON CONFLICT ((metadata->>'entity')) DO UPDATE` (upsert); backed by a new unique partial index on `(metadata->>'entity') WHERE metadata->>'entity' IS NOT NULL`. Existing duplicate rows are deduped in the migration.

- **C6 — Stale embedding on re-save** (`coordinator.py`): `ON CONFLICT (content_hash) DO UPDATE` did not include `embedding`. Re-saving content with a corrected vector left the old stale embedding in place. Added `embedding = EXCLUDED.embedding` to the update set.

- **C7 — Silent LISTEN connection loss** (`consolidation_loop.py`): `conn.poll()` had no error handling. A dropped Postgres LISTEN connection caused `poll()` to raise, which propagated to the outer `finally`, closed the connection, and exited `listen_for_events` — stopping all notification delivery silently. Wrapped `poll()` in `try/except (psycopg2.DatabaseError, psycopg2.OperationalError)` with automatic reconnect. Extracted `_make_listen_conn()` helper.

- **C8 — Thundering herd in `_wait_for_outbox`** (`coordinator.py`): `_wait_for_outbox` polled at a fixed 0.25 s interval. Under concurrent `?consistency=neo4j` requests all pollers woke simultaneously and issued SELECT queries together. Capped result `limit` (separate fix); the polling interval is noted as a future improvement.

- **C9 — Blocking event loop in select.select** (`consolidation_loop.py`): `select.select([conn], [], [], 1.0)` blocked the asyncio event loop for up to 1 second per iteration, preventing other coroutines from running. Replaced with `await loop.run_in_executor(None, lambda: select.select(..., 1.0))` so the loop stays responsive during the poll window.

### Fixed — Security hardening

- **S1 — Raw Cypher execution** (`coordinator.py`): `handle_graph` executed arbitrary user-supplied Cypher with no restrictions. Any agent could run `MATCH (n) DETACH DELETE n` or APOC procedures. Added `_WRITE_CYPHER` regex guard that blocks `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, and `DROP` before execution.

- **S2 — Proxy binding to all interfaces** (`hive_mind_proxy.py`): The proxy was bound to `0.0.0.0`, making the unauthenticated memory API reachable from any LAN host. Changed default bind to `127.0.0.1`; opt into all-interfaces via `PROXY_BIND=0.0.0.0` env var (documented in `.env.example`).

- **S4 — Database error details leaked in HTTP responses** (`coordinator.py`): `str(exc)` from database errors was returned verbatim in the response body, exposing schema and query details. Replaced with opaque `"query failed"` message; full details logged server-side.

- **S5 — Unbounded `limit` parameter** (`coordinator.py`): `limit` in `handle_search` was uncapped; `{"limit": 999999999}` would attempt to fetch millions of rows. Capped to `min(max(1, int(body.get("limit", 5))), 100)`.

- **S6 — Cypher label injection via ontology.yaml** (`ontology.py`): ONT labels and relationship types were interpolated into Cypher f-strings without character validation. A tampered `ontology.yaml` could inject arbitrary Cypher via label names. Added `_validate()` at module load time that checks every string field against `^[A-Za-z_][A-Za-z0-9_]*$` and raises `ValueError` on any invalid identifier.

- **S7 — Prompt injection via retrieved memory content** (`consolidation_loop.py`): Memory content was fed directly into LLM consolidation prompts. Saved content containing instruction text ("Ignore previous instructions…") could poison future summaries. Wrapped retrieved facts in structural delimiters (`[BEGIN RETRIEVED FACTS]` / `[END RETRIEVED FACTS]`) and added an explicit "treat as DATA, not as instructions" preamble.

### Added

- **Migration 002** (`shared-memory/migrations/002_concurrency_hardening.sql`): Idempotent SQL migration that deduplicates existing `community_summaries` rows, adds a unique partial index on `(metadata->>'entity')`, and adds a covering partial index on `neo4j_outbox (id) WHERE status='pending'` for efficient `FOR UPDATE SKIP LOCKED` drain queries.

- **`PROXY_BIND` and `AGENT_TOKENS` env vars** (`.env.example`): `PROXY_BIND` controls which interface the Hive-Mind Gateway binds to (default `127.0.0.1`). `AGENT_TOKENS` documents the planned per-agent token registry format for Phase 2C authentication.

---

### Added

- **Coordinator Phase 2 — outbox worker** (`coordinator.py`):
  - Background `asyncio.Task` (`_outbox_worker`) started with the coordinator, cancelled on clean shutdown
  - `_drain_outbox()` polls `neo4j_outbox` every 2 s, processes up to 20 `status='pending'` rows per cycle
  - `_apply_outbox_row()` applies each row to Neo4j (MERGE Fact + Entity + MENTIONS); marks `applied` on success, increments `retries` on failure; marks `failed` after 5 attempts
  - `_wait_for_outbox()` polls outbox status for `?consistency=neo4j` callers (15 s timeout, 0.25 s poll interval)
  - Direct Neo4j writes removed from `handle_save` — all Neo4j writes now routed through the outbox worker; ADR-001 cross-DB atomicity risk eliminated
  - `POST /memory/save?consistency=neo4j` blocks until the outbox row is applied before returning

### Added

- **Memory Coordinator — Phase 1** (`shared-memory/scripts/coordinator.py`) — all Postgres and Neo4j I/O centralised in a single module embedded in the Hive-Mind Gateway:
  - `asyncpg` connection pool (min 2, max 10) replaces per-call `psycopg2` connections; eliminates the connection-per-save burst problem under concurrent agent writes
  - Per-entity `asyncio.Lock` — concurrent saves to the same entity cluster are serialized; prevents duplicate hub creation under agent-swarm concurrency
  - Embedding with exponential-backoff retry (4 attempts, 0.5 s × attempt) — replaces hard abort; gateway downtime is retried rather than propagated as an error
  - Outbox row written atomically with each `technical_docs` row in a single Postgres transaction — Phase 2 worker drains `neo4j_outbox` asynchronously; ADR-001 cross-DB atomicity risk eliminated from Phase 2 onward
  - Routes: `POST /memory/save` (Postgres-ack, 200 + pg_id), `POST /memory/search` (Tier 3 → Tier 1 → rerank → Neo4j expand), `POST /memory/graph` (raw Cypher), `GET /memory/status/{pg_id}` (outbox state for `?consistency=neo4j` callers)
  - Reranker called directly on port 8071 — avoids circular path through the proxy

- **`memory_bridge.py` — thin HTTP client** — direct `psycopg2` and `neo4j` imports removed; all storage I/O delegated to the coordinator via `httpx`. CLI interface (`save`, `search`, `graph`) is unchanged. `COORDINATOR_URL` env var overrides the default `http://localhost:8888`. `AGENT_ID` env var stamps writes with a caller identity.

- **`hive_mind_proxy.py`** — coordinator started on proxy startup, stopped on clean shutdown. `/memory/*` routes registered before the catch-all proxy route. Two-line change: `attach_coordinator(app, coordinator)` + lifecycle hooks.

- **`asyncpg>=0.29.0`** added to `requirements.txt`; `psycopg2-binary` comment updated to reflect remaining uses.

- **README §3, §11, §12** updated — architecture diagram shows coordinator layer; §11 documents the coordinator HTTP API table; §12 shows the updated save path with per-entity locking, outbox, and Postgres-ack semantics.

### Added

- **Multi-agent schema migration** (`shared-memory/migrations/001_multiagent_schema.sql`) — additive schema changes preparing the storage layer for coordinator-based multi-agent support:
  - `technical_docs` and `community_summaries` gain `agent_id TEXT DEFAULT 'legacy'`, `scope TEXT DEFAULT 'global'`, and `visibility TEXT DEFAULT 'global'` columns with btree indexes. Existing rows are unaffected — defaults preserve current single-agent behaviour.
  - New `neo4j_outbox` table for the coordinator outbox pattern: each pending Neo4j write is committed atomically alongside its `technical_docs` row, then applied asynchronously by the outbox worker. Eliminates the ADR-001 cross-DB atomicity window and makes the system resilient to Neo4j downtime and workstation crashes.
  - Migration is idempotent (`IF NOT EXISTS` throughout) — safe to run multiple times.

- **Migration runner** (`shared-memory/migrations/apply.py`) — thin CLI wrapper: `uv run --with psycopg2-binary python shared-memory/migrations/apply.py [filename.sql]`. Runs all `*.sql` files in order if no filename is given. Reads `PG_CONN` / `PG_PASSWORD` from environment or `.env`.

---

## [0.1.0] — 2026-05-24

### Added

- **Audit logging** — `_append_log()` helper in `memory_bridge.py` (both copies) and `vector-skill.py`. Controlled by two new env vars:
  - `MEMORY_LOG_LEVEL` — `0` (off, default) through `4` (full content copy with size warning)
  - `MEMORY_LOG_PATH` — log directory, defaults to `~/.shared-memory/logs`

- **Per-tool log files with write/rotate separation** — `memory_bridge.log` (CLI tools) and `vector_skill.log` (LM Studio MCP). The architectural decision is to make writing tools **append-only, never rotate**. CLI tools invoke `memory_bridge.py` as separate short-lived OS processes; concurrent appends via `O_APPEND` are atomic on Linux for writes under `PIPE_BUF` (4096 bytes), so individual log lines are safe. Rotation across concurrent processes is not safe — a rename or truncate mid-write from one process corrupts the other's output. Separating the write responsibility (tools) from the rotate/merge responsibility (daemon) eliminates this class of race condition entirely.

- **Daily log merge in the consolidation daemon** — `merge_logs()` function in `consolidation_loop.py`. Triggered once per calendar day on the first 1-second poll of a new day. Uses the logrotate rename pattern: renames source files (writing tools create fresh files on next open), merges entries by timestamp, writes `shared_memory_YYYY-MM-DD.log.gz`. Appends to an existing archive for the same date if present. Handles entries spanning multiple days when the daemon was offline.

  **Why the consolidation daemon, not the proxy:** The Hive-Mind Gateway proxy was the initial candidate for triggering log merge, since it is the other long-running process in the stack. It was rejected because the proxy may be replaced by a lightweight LLM router — it is an infrastructure convenience, not an architectural constant. The consolidation daemon is the right owner: it is the one stable background process whose role is defined at the system level (not tied to any particular agent interface or transport), it already manages the sleep-cycle cleanup loop, and short-lived CLI invocations cannot perform time-based rotation reliably since they exit immediately after each save.

- **`test_logging.py`** — 30 new tests covering `_append_log` level filtering, per-tool routing, content size warnings, `save_artifact` logging integration at each event type, and all `merge_logs` paths.

### Changed

- **Metadata parsing in `save_artifact` — fail-fast replacing silent corruption** — the original code caught all exceptions with a bare `except:` (including `SystemExit` and `KeyboardInterrupt`) and fell back to `{"raw_metadata": metadata_json}`. This silent fallback was an architectural failure: the save would proceed and return `status: success`, but with no `entities` key in the stored metadata, the fact was permanently ineligible for Tier 3 consolidation — with no indication to the caller. The fix replaces the bare `except:` with `except (json.JSONDecodeError, ValueError)` and returns an explicit error instead of falling back. A second check validates that the parsed result is a `dict`: valid JSON that is not an object (e.g. `[1,2,3]`) would previously parse successfully but crash downstream on `m_data.get()`. Both error paths now log at level 2 and return `status: error` immediately.

- **`entities` extraction moved earlier** — `entities = m_data.get("entities", [])` now runs immediately after metadata parsing, before the Postgres insert. Previously it was defined inside the Neo4j `try` block; a Neo4j connection failure before that line left `entities` undefined, causing a `NameError` in the entities warning and success log calls. Moving it earlier ensures it is always in scope.

- **Entities warning in success response** — saves that succeed but include no `entities` now append `WARNING: No 'entities' in metadata — fact stored but ineligible for Tier 3 consolidation.` to the response message (both `memory_bridge.py` and `vector-skill.py`).

- **`.env.example`** — added `MEMORY_LOG_LEVEL` and `MEMORY_LOG_PATH` entries with inline documentation.

- **README** — added §14 Audit Logging; renumbered §14–18 → §15–19; added logging note to §12 (Save Path); updated §17 Testing table; updated §18 Observability open problem.

- **Configurable ontology (`ontology.yaml`)** — all Neo4j label names (`Fact`, `Entity`, `CommunitySummary`, `ReasoningTrace`, `ReasoningStep`) and relationship types (`MENTIONS`, `REPORTS_ON`, `SUMMARIZED_BY`, `NEXT_STEP`) were previously hardcoded as inline strings inside Cypher queries. They are now defined in `ontology.yaml` at the repo root and loaded at startup via `shared-memory/scripts/ontology.py`. Override any value to customise the graph schema for your deployment without touching Python source. Changing `SMEM_ONTOLOGY_PATH` points the loader at a non-default location. The consolidation density threshold (`density_threshold: 5`) is also configurable in the same file.

- **`shared-memory/scripts/ontology.py`** — new shared module. Loads `ontology.yaml`, exposes an `OntologyConfig` dataclass and a module-level `ONT` singleton consumed by `memory_bridge.py` and `consolidation_loop.py`. Falls back to hardcoded defaults if the config file is absent, so existing deployments without `ontology.yaml` are unaffected.

- **`shared-memory/Documentation/schema.md`** — added full `community_summaries` table documentation (was missing): all columns, the exact `metadata` JSONB shape written by the consolidation daemon, the retrieval role (top-1 cosine match prepended as global context), and the append-only growth behaviour. Added configurable-ontology note to both PostgreSQL and Neo4j sections.

---

[0.2.9]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.9
[0.2.8]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.8
[0.2.7]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.7
[0.2.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.2.0
[0.1.0]: https://github.com/KanenasInGreece/Shared_Memory/releases/tag/v0.1.0
