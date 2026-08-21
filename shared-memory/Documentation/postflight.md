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

**In RE-BASELINE MODE**, A5 proves the read path against real Tier-3 content instead of a canary,
via a **bounded multi-candidate probe** (fix-round-2 ruling `decision:1439`, QA-01 — replacing the
single-summary gate `decision:1435`'s C1 shipped, which fix round 2 found still false-fails on a
healthy install):

- **Select up to 3 candidates.** At run time, read the **3** most-recently-updated live
  (non-superseded) `community_summaries` rows (either kind, in order — never pinned ids, since
  supersession would orphan a pinned check); fewer than 3 live rows exist, use what exists. Each
  candidate's qualified reference is `summary:<id>` (thematic) or `insight:<id>` (insight kind),
  matching `record_ref.py`'s `summary_record_type`.
  **Why 3, measured, not chosen** (fact:1438 sweep, all 21 live rows on the reference install
  probed with the shipped selector at limit 20): exactly **1 of 21** rows false-fails
  individually — both its Tier-3 candidate slots lost the rerank cut against 20 verbatim Tier-1
  facts (see the two-preconditions note below). At that measured rate, no set of 3 *distinct* rows
  drawn from this corpus can consist entirely of failures, while a genuine, wholesale Tier-3
  retrieval break still fails all 3 candidates loudly. This is a property of *this corpus at this
  moment*, not a constant — the fresh-install VM test is where the rate gets re-measured on a
  young corpus, and a materially different rate reopens the choice of 3.
- **Extract a distinctive phrase, deterministically, per candidate.** A pure function of each
  row's `content`: strip any leading `[TAG ...]` bracket prefix from each line (the zero-inference
  thematic fold's `fold_record_line` format, e.g. `[FACT]` or `[DECISION kind=... pg_id=123]`),
  join what remains, and take the first up-to-8 whitespace-separated tokens. Falls back to the raw
  content when every line is prefix-only. Same content always yields the same phrase; survives
  unicode (splits on Unicode whitespace) and short content (returns however many words exist, down
  to one). **Control characters are stripped from the final phrase** (fix-round-2 ruling
  `decision:1439`, SEC-01 — see the dedicated note below) before it is ever printed or searched.
- **Try candidates in order; search and grade each.** Search a candidate's phrase through the
  bridge, **limit 20** (no project filter — summaries are not scoped to `install-verification`).
  The **whole probe is timed as one measurement** — it lands under `search_rebaseline` (see A6;
  `decision:1435` R2), covering however many of the (up to 3) searches were actually attempted;
  postflight does **not** mint a second timing key per candidate. Each candidate's result is one
  of five shapes, distinguished by the **presence**, not merely the value, of the `ranked` key on
  a returned row (`decision:1435` C1/C2):
  - **Present** (`ranked` key `true`, the candidate's ref among the — **up to 20**, never an
    absolute count — returned rows): **the probe passes immediately** on this candidate. The
    output names which candidate succeeded and at what rank (e.g. "candidate 2 of 3, insight:451
    at rank 17 of 20"), so the operator sees the margin, not just a bare pass.
  - **Degraded** (`ranked` key present and `false`, the honest waiver) — **the probe passes
    immediately**, short-circuiting exactly like Present: results were returned, and the
    summary-presence assertion is waived with an explicit printed line stating Tier-3 narratives
    are omitted in degraded mode by design (measured in the 2026-08-21 stress test; v0.8.54 ruling
    "ranked, not guaranteed").
  - **Keyword fallback** (`ranked` key ABSENT entirely — the coordinator's keyword-fallback shape,
    served when the embedder is unreachable; rows carry no `ranked`, no `ref`, no `pg_id`) — an
    **immediate hard failure that stops the probe** without trying further candidates. The
    embedder being gone is not a per-row problem that a different candidate could route around;
    trying more candidates against a dead embedder would only waste the client timeout budget
    three times over for no better answer. Conflating this with honest degraded mode would let a
    dead embedder exit the whole run green, since re-baseline A4 performs no save and so never
    independently trips the embedding mandate.
  - **Absent** (`ranked: true`, the candidate's ref not among the — up to 20 — returned rows) or
    **Empty** (zero results for that candidate's phrase) — **try the next candidate.** Neither is
    a probe failure on its own; the probe fails only if every attempted candidate ends this way (or
    the keyword-fallback case fires).
  - **Error / unparseable** (a transport error, or no parseable JSON — a timeout by another name) —
    **try the next candidate**, same as Absent/Empty. If the probe's **final** outcome is decided
    by this shape (every candidate ended here, or the last one attempted did, with no earlier
    Present/Degraded/Keyword-fallback), the probe's timing is recorded as `null` in
    `search_rebaseline` rather than the timeout ceiling — invariant 6 applied to the whole probe's
    decisive attempt, not to an individual search that a later candidate's real result superseded.

**Pass criterion (re-baseline mode).** The FIRST candidate (of up to 3, tried in order) that comes
back Present or Degraded passes the whole probe. Fail only if the keyword-fallback shape is
detected (immediate) or if none of the attempted candidates comes back at all.

**Failure meaning (re-baseline mode).** No live summary readable when the mode-selection count
said at least one should exist: the count and this read disagree — check the store directly. All
attempted candidates Absent/Empty/unparseable: the failure message names the **two preconditions**
a candidate must clear, so a reader can tell a rerank cut from a broken read path — **(a)** the
summary must first win its kind's single Tier-3 candidate slot, chosen by vector nearest-neighbour
(`coordinator.py:6164-6205` — the caller's search `limit` does not widen this slot count), and
**(b)** it must then survive the rerank cut against the Tier-1 candidates in the pool. A failure at
this point, with 3 independent candidates all clearing neither, is a genuine read-path break, not
a rank complaint on any single row. `ranked` key missing: semantic search itself is down (keyword
fallback is serving), which A4's zero-save contract would otherwise leave undetected in this mode.

**SEC-01 — control-character stripping** (`decision:1439`, correcting `fact:1437`'s CRITICAL
"terminal escape sequence injection" finding to REQUIRED). `select_summary_phrase` strips C0
(`0x00`–`0x1F`, ESC `0x1B` included) and C1 (`0x80`–`0x9F`) control characters from the phrase
before it is ever printed or searched. **The mechanism, stated correctly**: `postflight.sh`'s
`printf '%s'` idiom does not interpret escapes in its argument, so no code executes — the real
impact is that raw ESC/control bytes, if left in a phrase extracted from corpus content, pass
through verbatim to whatever terminal or log viewer renders postflight's output. That is
operator-visible **output spoofing and log poisoning** on a diagnostic tool (by an actor who can
already write corpus content — not privilege escalation), which is a real, cheaply-fixed class
even though it is not code execution.

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
