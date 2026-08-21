# Postflight — post-install verification contract

`preflight.sh` proves a host is *ready* to run the stack; **postflight proves the installed stack
actually works, end to end** — after a first install (AGENTS.md Phase 9) and after every upgrade.
This document is the contract; `shared-memory/scripts/postflight.sh` implements it. Where the
script and this document disagree, this document wins and the script is the defect.

```bash
# On the gateway host, from the repo root. Auth-on installs need a minted agent
# token (any agent's — copy it from that agent's skill .env):
export AGENT_TOKEN=...
bash shared-memory/scripts/postflight.sh
```

- **Exit code:** `0` iff assertions **A1–A5** all pass. A6 is a measurement, never a gate; A7 holds
  by construction and is documented below.
- **Configuration:** `GATEWAY_URL` (default `http://localhost:8888`), `AGENT_TOKEN` (environment
  only), `PG_CONTAINER`/`NEO4J_CONTAINER`/`PG_DB` (defaults `postgres-vector`/`neo4j-memory`/
  `agent_data`, as in `init_db.sh`). `shared-memory/.env` is read key-by-key (grep/cut), never
  sourced.
- **Timeouts are generous by design:** saves can take over 60 s on small hosts (measured on a
  2-core machine), so save/search invocations get client timeouts of at least 180 s. Slowness is
  A6's business — it is never a failure of A4/A5.
- **When auth is configured and `AGENT_TOKEN` is absent:** A1's authenticated half and A4/A5/A6
  fail fast with one clear message naming the missing token — never a cascade of confusing errors.

## Mode selection — canary vs re-baseline

Postflight runs in one of two modes, chosen **at run time from the live corpus, with no new
flag**. The count and the chosen mode are printed at the top of every run, before A1.

- **CANARY MODE** — the corpus holds **zero** live (non-superseded) `community_summaries` rows,
  either kind (thematic or insight). This is the current, unchanged behavior: A4 saves a fresh
  canary and verifies it store-side; A6 also saves a realistic-payload canary to time it. A young
  or freshly-installed corpus always gets this mode.
- **RE-BASELINE MODE** — the corpus holds **at least one** live (non-superseded)
  `community_summaries` row. A4 and A6 stop minting canaries; A5 instead proves the read path
  against real Tier-3 content already in the corpus, selected at run time. This is a **contract
  refinement of the v0.9.17 postflight** (`fact:1402`/`decision:1403` lineage) — the accepted
  trade, stated plainly by the tool: re-triggers no longer re-prove the write path once the corpus
  has matured; that proof stays anchored to the original install canary, since writes were
  measured as the resilient path throughout stress testing.
- If the live-summary count itself cannot be determined (docker missing, or the store is
  unreachable), postflight **falls back to CANARY MODE** — the safer default, since it preserves
  today's verification rather than silently skipping a check it could not confirm was safe to
  skip.

---

## A1 — Liveness & shape

**Check.** Anonymous `GET /health` answers. When auth is configured (`AGENT_TOKENS` present in
`shared-memory/.env`), the anonymous payload contains **exactly** the keys
`{status, version, api_version}` — the S-10 regression check — and the authenticated payload
(bearer `AGENT_TOKEN`) answers with the full shape. On an auth-off install, the full payload
served anonymously is the **correct** result, and the output reports that mode.

**Pass criterion.** Gateway answers; the payload shape matches the install's auth mode exactly.

**Failure meaning.** No answer: the gateway is down or `GATEWAY_URL` is wrong. Extra keys in the
anonymous payload on an auth-configured install: **either** the S-10 slimming has regressed —
operational detail served to unauthenticated peers — **or** the auth mode diverged: the gateway
freezes its auth decision at startup (`AUTH_CONFIGURED_AT_STARTUP`) while this check reads the
*current* `.env`, so tokens added or removed since the gateway last started produce exactly this
shape mismatch. The output names both causes; restart the gateway and re-run before treating it
as a regression. Authenticated payload not full: the token is not being resolved, or the health
assembly is broken.

## A2 — Contract

**Check.** The client's `API_VERSION` (`memory_bridge.py`) equals the gateway's reported
`api_version`, and `/health` `version` equals this checkout's `FRAMEWORK_VERSION` (grepped from
`shared-memory/scripts/coordinator.py`).

**Pass criterion.** Both equalities hold.

**Failure meaning.** An `api_version` mismatch is a wire-contract skew — one side must be
upgraded before the pair can be trusted. A `version` mismatch where the gateway is older is stated
plainly as: **"gateway is running an older build than this checkout — a restart/redeploy is
owed"** — never a bare "mismatch". (The inverse — a checkout older than the running gateway —
means this checkout is stale: pull before trusting any checkout-relative check.)

## A3 — Schema truth

**Check.** Run the two shipped verifiers and incorporate their verdicts — postflight never
duplicates their logic:

```bash
uv run --with psycopg2-binary python shared-memory/migrations/verify_schema_init.py
uv run --with neo4j python shared-memory/migrations/verify_neo4j_init.py
```

**Pass criterion.** Both exit 0.

**Failure meaning.** Postgres verifier failing: a fresh install built from `schema_init.sql` would
not behave like this live database (missing constraints, FKs, IDENTITY columns — the classes it
exists to catch). Neo4j verifier failing: a declared uniqueness constraint is not in force on the
live instance — duplicates can appear silently under a race (`--apply` is its documented remedy).

## A4 — Write path end to end

**In RE-BASELINE MODE, A4 saves nothing.** It prints one explicit informational line stating that
write-path proof stays anchored to the install canary (the accepted trade above), and **it cannot
fail in this mode — including indirectly.** No code path may set A4's exit-code contribution in
re-baseline mode, under any earlier assertion's outcome (e.g. a missing `AGENT_TOKEN` diagnosed at
A1, which in canary mode pre-marks A4 as failed since A4 cannot run without a token — in
re-baseline mode A4 needs no token at all, so that pre-mark must not fire, and A1's own message
must not name A4 among what a missing token skips in this mode). **Spec wins**: where the script
and this rule disagree, the script is the defect (fix-round ruling `decision:1435`, R1).

**In CANARY MODE (below), behavior is unchanged.**

**Check.** Save a canary record through the gateway (via
`uv run --with httpx --with python-dotenv python shared-memory/scripts/memory_bridge.py save ...`
with `AGENT_TOKEN` exported), then verify it in the stores:

- **(a) Embedding dimension EQUALS 1024** — the value is asserted, not an equality between two
  expressions:
  `docker exec postgres-vector psql -U postgres -d agent_data -tAc "SELECT vector_dims(embedding) FROM technical_docs WHERE id=<pg_id>"`
- **(b) Outbox drained** — the `neo4j_outbox` row for that `pg_id` reaches status `'applied'`
  (polled briefly — the worker drains within seconds).
- **(c) Graph mirror exists** — the `:Fact` node with that `pg_id` exists in Neo4j
  (`cypher-shell` inside `neo4j-memory`; `NEO4J_PASSWORD` read from `shared-memory/.env` with
  grep/cut, never by sourcing the file, and passed via the environment, never argv).

**Pass criterion.** Save returns `success` with a `pg_id`; (a) returns exactly `1024`; (b) reaches
`applied`; (c) counts exactly one node.

**Failure meaning.** Save refused: auth, project ingress, or the embedding mandate (a 503 after
retries means the embedder is unreachable — no record is written without a vector, by design).
Wrong/absent dimension: the embedding contract is broken and the record is invisible to semantic
search. Outbox still `pending` after the poll: the worker is either not running **or** healthy
but mid-backoff — a transient store blip re-queues rows as `pending` with exponential backoff, so
check `/health` `failed_age` and the gateway journal before pronouncing the worker dead. Outbox
`failed`: Neo4j was unreachable past the retry window (the AGENTS.md status runbook has the
one-statement recovery).
Node absent while the row reads `applied`: outbox atomicity is broken — the most serious verdict
this assertion can return.

## A5 — Read path, honestly graded

**In CANARY MODE**, the check is as before: `memory_bridge.py search` finds the canary. Each
result carries `ranked` and `score`: a real numeric reranker score **or** an explicit degraded
verdict (`ranked: false`, null scores = vector order served) **both pass**, and the output states
which mode the install is in.

**Pass criterion (canary mode).** The canary's `pg_id` appears in the results, in either mode.

**Failure meaning (canary mode).** Canary missing: retrieval is broken end to end (embedding,
vector search, or the gateway search path). A fabricated uniform score would be a defect — a dead
reranker must be distinguishable from a confident one; the honest degraded verdict is a pass with
a named mode, because failure ≠ idle and degraded ≠ broken.

**In RE-BASELINE MODE**, A5 proves the read path against real Tier-3 content instead of a canary:

- **Select.** At run time, read the single most-recently-updated live (non-superseded)
  `community_summaries` row (either kind) — never a pinned id, since supersession would orphan a
  pinned check. Its qualified reference is `summary:<id>` (thematic) or `insight:<id>` (insight
  kind), matching `record_ref.py`'s `summary_record_type`.
- **Extract a distinctive phrase, deterministically.** A pure function of the row's `content`:
  strip any leading `[TAG ...]` bracket prefix from each line (the zero-inference thematic fold's
  `fold_record_line` format, e.g. `[FACT]` or `[DECISION kind=... pg_id=123]`), join what remains,
  and take the first up-to-8 whitespace-separated tokens. Falls back to the raw content when every
  line is prefix-only. Same content always yields the same phrase; survives unicode (splits on
  Unicode whitespace) and short content (returns however many words exist, down to one).
- **Search and grade.** Search the phrase through the bridge, **limit 20** (no project filter —
  summaries are not scoped to `install-verification`), and time it. **This timing is NOT the
  `search` field of A6's baseline** — it lands under its own key, `search_rebaseline` (see A6;
  fix-round ruling `decision:1435`, R2). Three result shapes, distinguished by the **presence**,
  not merely the value, of the `ranked` key on a returned row (fix-round ruling `decision:1435`,
  C1/C2):
  - **`ranked` key present and `true`** — reranking is live. Pass requires the selected summary's
    qualified ref to appear **anywhere among the (up to 20) returned rows — never a rank or
    top-*N* position**. Measured basis (reviewer probe, reference install, 8 most-recently-updated
    live summaries): 2/8 false-fail at a top-5 assertion (worst observed rank 16 of 20), 8/8
    present within 20. A rank assertion re-asserts exactly the guarantee the v0.8.54 ruling
    removed ("ranked, not guaranteed") — Tier-3 rows compete against Tier-1 facts for rank and are
    not owed a position, only retrievability. Absent from all 20 rows: a genuine A5 failure — the
    failure message states plainly that the summary was **absent from a 20-row result set**, never
    that "the read path is broken" (that framing implied a rank guarantee this check does not
    make).
  - **`ranked` key present and `false`** (honest degraded mode) — the summary-presence assertion
    is **waived**, with an explicit printed line stating Tier-3 narratives are omitted in degraded
    mode by design (measured in the 2026-08-21 stress test; v0.8.54 ruling "ranked, not
    guaranteed"). Pass requires results to be returned at all — an empty result set still fails,
    exactly as in canary mode. Never a silent pass.
  - **`ranked` key ABSENT entirely** — the coordinator's keyword-fallback shape, served when the
    embedder is unreachable (rows carry no `ranked`, no `ref`, no `pg_id`; semantic search has
    been replaced by substring matching). This is indistinguishable from honest degraded mode by
    *value* but not by *key presence*, and it is a **genuine A5 failure**, never the DEGRADED
    waiver: "semantic search is not serving — keyword fallback shape detected." Conflating the two
    would let a dead embedder exit the whole run green, since re-baseline A4 performs no save and
    so never independently trips the embedding mandate.

**Pass criterion (re-baseline mode).** `ranked: true`: the selected summary's ref is present
anywhere in the (up to 20) results. `ranked: false` (key present): at least one result is
returned (the summary-presence check is waived, not skipped). `ranked` key absent (keyword
fallback): always fails.

**Failure meaning (re-baseline mode).** No live summary readable when the mode-selection count
said one should exist: the count and this read disagree — check the store directly. Present
`ranked: true` but the summary absent from all 20 rows: the summary was not retrieved at all —
this is a presence failure, not a rank complaint. Zero results even in degraded mode: retrieval is
broken end to end. `ranked` key missing: semantic search itself is down (keyword fallback is
serving), which A4's zero-save contract would otherwise leave undetected in this mode.

## A6 — Baseline emission (measurement, never a gate)

**Check.** The baseline JSON always carries a `"mode": "install" | "re-baseline"` field naming
which mode produced it.

**In CANARY MODE**, behavior is unchanged: time three operations — a short save (~160 chars, A4's
canary), a realistic save (~3.5 KB), and a search — and write a baseline JSON to
`~/.shared-memory/postflight/baseline-<UTC ISO8601>.json` containing: the three timings, the
`/health` `backend_capability` block, a hardware fingerprint (thread count via `nproc`,
`MemTotal`, the `lspci` VGA line always, plus an `nvtop` presence boolean), framework version and
date. Save contents are unique per run (a timestamp is embedded) so SHA-256 idempotency never
short-circuits the timing. **Measurement honesty:** the `uv` environment is warmed *untimed*
before the first timed operation (on a fresh host uv's resolution would otherwise dominate the
number); each timing window contains exactly one save; a timed-out operation records `null`,
never the timeout ceiling; and the JSON carries a note stating all three rules.

**In RE-BASELINE MODE**, A6 performs **no saves of any kind** (mirroring A4's zero-saves
contract). `save_short` and `save_realistic` are both `null`, with a note naming the accepted
trade — write-path timing stays anchored to the original install canary. **`search` stays
canary-search-only and is `null` in this mode**, with a note explaining why (fix-round ruling
`decision:1435`, R2: a metric whose meaning silently changes between modes while its name stays
constant is the framework's known monitor-class defect — canary-mode `search` times a
project-filtered search for a unique marker; re-baseline's phrase search is unfiltered and
whole-corpus, a different workload). A5's summary-search timing instead lands under its **own**
key, `search_rebaseline` — `null` in canary mode, populated in re-baseline mode. Everything else —
`backend_capability`, `capacity`, `hardware`, `framework_version`, `date` — is unchanged,
read-only, and rendered identically to canary mode; **the capacity verdict section (below the JSON
write) is untouched by mode.**

**Pass criterion.** None — A6 is a measurement. A written baseline is the deliverable; a slow
number is information, not a failure. It gives a later "the system feels slow" session a
same-hardware number to diff against.

**Failure meaning.** Not applicable. If the JSON cannot be written, that is reported as a warning.

## A7 — Conduct constraints (by construction)

The script itself is bound by the framework's standing invariants, which bind tooling too:

- It talks **only to the gateway on `:8888`** — never to `:8070`/`:8071` directly.
- Its **own** container access is `docker exec` with read-only verification queries. One
  incorporated tool is broader: A3's `verify_schema_init.py` connects over TCP and **creates and
  drops a prefix-guarded throwaway database** — that is *its* documented contract, stated here so
  A7's claims stay true rather than certifying conduct the construction does not have.
- Writes outside the gateway path: A6's baseline JSON, and A3's throwaway verification database.
- Every canary lands under the **reserved project name `install-verification`** (metadata
  `{"project": "install-verification", "new_project": true}`). The flag is idempotent-safe **by
  construction**: a registered project short-circuits ingress before `new_project` is read, so it
  is never refused for "already exists". The only `new_project` refusals are confusable/
  spelling-variant names (an existing project trigram-near `install-verification`); that needs
  the operator, and the failure message surfaces the gateway's reply — there is no retry.
- **The canary STAYS in the corpus** — it is the install's birth certificate: dated, attributable,
  and the first record every later triage can look for. Two consequences, stated honestly:
  the reservation is **convention, not server-enforced** (nothing gateway-side exempts the name),
  and canaries are **fold-eligible** — repeated runs accumulate records in one project group that
  the Tier 3 consolidation can eventually summarize as the filler it is. Both are accepted for
  now; server-side reservation is the recorded follow-up if either becomes noise.

**Pass criterion.** Holds by construction of the script; the script states these constraints in
its output rather than testing them.

**Failure meaning.** A change to the script that violates one of these is a defect against this
contract, whatever its tests say.
