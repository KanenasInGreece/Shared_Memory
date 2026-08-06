# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.8.54] — 2026-08-06

### Changed

- **Tier-3 narratives are now RANKED, not guaranteed a position.** A community
  summary and a cross-project insight were fetched nearest-neighbour with
  `LIMIT 1` and **no distance floor**, then placed above every fact without ever
  being scored — so the most prominent slots in every answer were held by
  records that had never been required to be relevant. A summary was not ranked
  badly; it was **not ranked at all**.

  Both now enter the reranker's candidate set and are scored against the facts
  and against each other on one scale. **Measured before the change** (10
  queries, the summary a search would return scored against the same 20 facts):
  median rank **6 of 21**, a **negative** relevance score on **6 of 10**,
  genuinely first on **2**. So the guarantee was wrong for 8 of 10 queries.

  ⚠ **The tier is kept, only its guarantee is removed.** The summary was never
  beaten by all twenty facts, and it earned first place twice on merit — so
  narratives are demoted *into* the contest rather than dropped from it.

  This **refines decision 245** rather than reversing it (`retrospective 1104`).
  245 ordered insights above thematic summaries because an insight carries the
  highest distilled value; that premise stands. What changes is that value is in
  the seeker's mind, not the provider's — a narrative cannot be declared the
  best answer to a query it was never compared against. The obligation moves
  upstream, onto forming narratives whose value is evident on merit.

- **A rerank failure now drops Tier-3 entirely rather than pinning it on top.**
  Vector order is meaningful only *within* one table: summary distances and fact
  distances come from separate queries and are never comparable. Emitting the
  combined candidate list in index order would restore the guarantee at the exact
  moment there is no evidence to justify any position for a narrative.

- **The MCP client no longer re-imposes the ordering the gateway removed.**
  `vector-skill.py` partitioned results and printed every Tier-3 row above every
  fact, which was harmless only while the gateway pinned them there too — it
  would have defeated this change at the MCP front door, showing a summary on top
  carrying a score saying it belonged sixth. It now renders in server order, and
  **shows Tier-3 scores**, which it previously omitted entirely. Its result count
  also includes Tier-3 rows; it had counted only the facts.

## [0.8.53] — 2026-08-06

### Fixed

- **The candidate pool was a hidden ceiling, not a default.** `handle_search`
  accepted a `limit` of up to 100 but always fetched exactly **20** Tier-1
  candidates, so a caller asking for 50 silently received 20 while the endpoint
  advertised otherwise. The pool is now `max(SEARCH_CANDIDATE_FLOOR, limit)` —
  a **floor the caller can exceed**, never a cap they cannot see. The floor
  matters in its own right: reranking can only reorder what it is handed, so a
  small search must still draw from a wider pool than it returns.

### Changed

- **The reranking window now defaults to the embedding window.**
  `RERANK_MAX_DOC_CHARS` derives from `EMBED_MAX_CHARS` instead of carrying its
  own literal, so **ranking and retrieval see the same text by default**.
  Retrieval selects a candidate on an embedding computed over up to
  `EMBED_MAX_CHARS`; ranking on a narrower slice can demote a record for lacking
  the very text it was selected for — ranking undoing retrieval. Measured on the
  reference corpus, narrowing to 2000 chars kept only about half of reranking's
  improvement over plain vector order.

  Narrowing it remains the dominant latency lever and remains one env var away —
  but it is now a **deliberate divergence with a known cost**, not a default.

- **The derived CPU-thread budget is counted in threads.** `--threads` takes a
  thread count, so `install_framework.sh` derives it from the host's threads
  rather than its physical cores; feeding a core count to a thread flag mixes
  two units. Documented alongside it: the value is **per container**, and there
  are two encoders that can run at once.

## [0.8.52] — 2026-08-06

### Added

- **A working GPU sample for the two encoders, and the numbers to choose with.**
  `compose.gpu-encoders.yaml` moves the embedder and reranker onto a
  Vulkan-capable GPU — one image covering Intel, AMD and NVIDIA, so the
  framework still never branches on vendor. It is a **sample we tested, not a
  prescription**: the framework's only requirement is an embedding endpoint and
  a reranking endpoint at `EMBEDDER_URL` / `RERANKER_URL`, and how you run them
  is yours to optimise.

  ```bash
  docker compose -f postgres_neo4j_limits.yaml -f compose.gpu-encoders.yaml \
                 --env-file shared-memory/.env up -d
  ```

  The default stays CPU. That is the point: the stack must come up on a machine
  with no GPU, no driver and no vendor assumption.

- **README §7 now states what CPU actually costs**, measured rather than
  asserted: ~20 s to embed an 8,000-char record, ~64 s to rerank a typical
  20-candidate search and ~146 s for a worst case. It also records that threads
  are a weak lever — 4/10/20 threads gave 63.8/36.7/32.3 s on one payload, five
  times the threads for twice the speed — because these models saturate memory
  bandwidth long before they saturate cores.

  ⚠ And it names `RERANK_MAX_DOC_CHARS` as a **concession, not a free win**:
  retrieval selects on the embedding window, so ranking on a narrower slice can
  demote a record for lacking the text it was selected for. On this corpus,
  capping at 2,000 chars kept only about half of reranking's gain over vector
  order. Once the encoders are on a GPU, raise it to `EMBED_MAX_CHARS` — the
  reason to narrow it was latency, and it is gone.

- **A documented silent-failure trap.** `EMBEDDER_URL` pointed at the reranker
  does not fail: the reranker answers `/v1/embeddings` with HTTP 200 and a
  1024-dimension vector — the exact shape BGE-M3 produces — that is essentially
  all zeros. It passes a dimension check and a null check while carrying no
  meaning. The embedder, asked to rerank, correctly refuses with HTTP 501. The
  ports are one digit apart; one backend fails loud and its twin fails silent.

## [0.8.51] — 2026-08-06

### Fixed

- **The reranker had never run.** `handle_search` called the reranking backend
  with a **constant `timeout=5.0`** while a real 20-candidate set costs tens of
  seconds, so every search fell through to the exception branch and served
  results in **vector order, unranked** — and did so invisibly, because the
  fallback fabricated `relevance_score: 1.0` for every row, a value a working
  reranker can legitimately emit. The recency-aware document text built for
  decisions and retrospectives (so the newest retrospective reads as the current
  verdict) had therefore never once affected an ordering.

  The timeout is now **derived from the payload** by `rerank_ceiling()`, the
  direct sibling of the existing `embed_ceiling()`. Both live in
  `dream_telemetry.py`, both size a per-request ceiling from a throughput floor
  anchored at the largest payload the framework can send, and both are
  env-overridable (`RERANK_MIN_CHARS_S`, `RERANK_SAFETY_FACTOR`,
  `RERANK_TIMEOUT_FLOOR_S`). The embedder had been brought under this rule and
  its identically-configured twin had not; that gap was the whole defect.

- **A successful rerank returned more results than the caller asked for.** The
  request sent `top_k`, which the reranking server ignores — it honours `top_n` —
  so the server scored and returned *all* candidates. Nothing truncated the
  response, meaning a working reranker would have returned 20 rows for a
  `limit: 5` search. This never surfaced only because the failure path capped at
  `min(limit, …)`: **the two defects masked each other**, and fixing the timeout
  alone would have broken the result-count contract in every client at once.
  Both parameter spellings are now sent, and **neither is trusted** — the
  coordinator enforces the caller's limit itself.

- **A rerank failure is now visible instead of silent.** Results carry
  `ranked: true|false`; a fallback result reports `score: null` rather than a
  plausible fabricated one, is logged, and is counted. Serving vector order is a
  different answer from serving a ranked one, and the response now says so.

### Added

- **`/health` reports whether the critical backends can SERVE, not merely whether
  they answer.** Both encoders had reported `ok` throughout the total failure
  above, because the probe was a liveness `GET /health`. A new background probe
  times a fixed representative payload against the real scoring endpoints,
  projects the observed throughput onto the largest payload the framework can
  send, and compares that against the timeout that would apply — surfacing
  `serves_full_payload: false` for a backend that is up and cannot do its job.
  Cached and refreshed on a slow cadence (`CAPABILITY_PROBE_INTERVAL_S`), so
  `/health` never runs inference inline, and guarded so an observability path can
  never take down what it observes.

- **A bounded relevance window, `RERANK_MAX_DOC_CHARS`.** Only the text the
  reranker *scores* is bounded; the full record is still stored and still
  returned by search. This is what makes the derived ceiling a finite quantity.
  ⚠ It is a **latency concession, not a neutral default**: retrieval selects on
  the embedding window, so ranking on a narrower slice can demote a record that
  was correctly retrieved for something past the cut.

### Security

- **The embedder and reranker no longer publish on every host interface.**
  `ports: ["8070:8070"]` binds all interfaces, and neither service has any
  authentication — on a LAN that is an open embedding and reranking endpoint.
  Publishing is now `${INFERENCE_BIND:-127.0.0.1}`. The container-internal
  `--host 0.0.0.0` is correct and unchanged: binding loopback inside the network
  namespace would make the service unreachable from the host.

### Changed

- **Encoder CPU threads are derived from the host instead of hardcoded.**
  `--threads 4` becomes `${LLAMA_CPU_THREADS:-4}`, and `install_framework.sh`
  computes roughly half the host's threads plus one, leaving room for Postgres,
  Neo4j, the gateway and the desktop. ⚠ Documented alongside it: raising this
  buys far less than it appears to — on the reference rig 5× the threads bought
  2× the speed, because these models saturate memory bandwidth (and, on
  multi-die CPUs, the inter-die fabric) long before they saturate cores.

## [0.8.50] — 2026-08-06

### Added

- **The change groups now enforce what can be enforced, and say plainly what
  they cannot.** The rule that touching one member of a group means reviewing the
  whole group is a discipline, and a discipline is what fails on the release
  where someone is in a hurry. `tests/test_change_group_contracts.py` takes the
  mechanically checkable obligations and makes them tests:

  - **All four version pins must agree** (Group 1). The release version lives in
    four files, two of them client copies — which is why even a server-side fix
    touches this group — and until now nothing checked that a bump reached all
    four. A missed one ships a client announcing a version the gateway does not
    recognise, and the only symptom is a compatibility warning from a command
    nobody runs on a good day, so the divergence outlives the change that caused
    it. Both client copies must also pin one `api_version`.
  - **Every table a migration creates must reach `schema_init.sql`** (Group 4),
    and the migration chain must have no gaps or duplicate numbers. This cannot
    see a missing constraint — only the live diff can — but it catches the
    coarsest omission: a migration adding a table and nobody regenerating the
    artefact a fresh install actually applies.
  - **Every script the upgrade path names must exist** (Group 5). A documented
    step naming a file that is not there fails on a stranger's machine while they
    follow the instructions faithfully.

  Each test names its group and the failure it prevents, and all are
  mutation-verified.

### Notes

- **Three of five groups are still partly or wholly unenforced, and that is now
  written down rather than assumed.** Group 3 (daemon behaviour and
  observability) has no mechanical tie at all — whether a change can be seen
  working *and* failing remains entirely a matter for eyes. Group 4's most
  dangerous class, a constraint silently dropped from the fresh-install
  artefact, has bitten three times and is caught only by `verify_schema_init.py`
  run against a throwaway database. **A green suite does not mean a group was
  cleared.**

---

## [0.8.49] — 2026-08-06

### Changed

- **The capture surface now explains itself to someone who did not build it.**
  Every field it asks for existed because something downstream breaks without
  it, and that breakage is almost always SILENT — the save succeeds, the record
  is searchable, and only synthesis quietly fails to happen. Until now the
  surfaces described the fields accurately and assumed the reader already knew
  why they mattered, which is only true of the people who designed them.

  `SKILL.md` gains a **record-model section, placed before the tasks**: a table
  of the three record types against who owns which field, then a paragraph per
  field saying what it captures, which failure it prevents, and what it costs to
  get wrong. It closes with what to derive silently, what to propose for
  correction, and what must always be asked.

  The `--help` strings gain the **contract** of each flag: shape, whether it
  repeats, whether it is required, and what the gateway does when it is wrong.

  The split is deliberate. Help text is read when the caller already knows it
  wants the flag; `SKILL.md` is read when deciding whether a field applies at
  all — and the elicitation decision, *should I interrupt the operator for
  this?*, cannot be made from a description of the shape alone.

- **`source_ref` is documented as answering a DIFFERENT question per record
  type**, which was true in the code and stated nowhere. On a fact it is where
  the KNOWLEDGE came from, and it silently sets that fact's evidential weight.
  On a retrospective it names THE INSTRUMENT THAT MEASURED THE OUTCOME — a claim
  its grounding facts cannot make on its behalf, because those facts may belong
  to another project entirely and cite a different file tree.

- **The asymmetry in `grounded_in` is now explained where it is enforced.** A
  decision may rest on experience, because a project's first decisions are
  genuinely made before it has evidence. A retrospective may not: it exists to
  report what measuring showed, so with nothing measured it asserts a verdict
  from nowhere — and it strands the decision it judges, which reaches its own
  topics through it.

- **And a test that fails when the contract moves without the documentation.**
  A good intention does not survive forty releases — the stale examples above
  are the proof. `tests/test_capture_surface_documented.py` asserts that every
  capture flag a client offers, every ingress refusal the gateway can return,
  every outcome rating, and the worked examples' version and `api_version` are
  present in the skill document. It checks presence, never wording, so ordinary
  edits do not fail it; what it makes impossible is ADDING a caller-visible part
  of the contract that nobody explains. Its exemption list is the point rather
  than a loophole: a new flag fails until someone either documents it or names
  it mechanical, and both are answers. **It caught an omission on its first
  run** — `domain_without_project`, a refusal shipped in v0.8.47 and documented
  nowhere.

Documentation, help text and one test. No behaviour change, no schema change, no
wire-contract change.

---

## [0.8.48] — 2026-08-06

### Fixed

- **A spelling variant could register as a new project or section whenever it
  also scored below the similarity floor.** The guard that refuses a name
  differing from a registered one only in separators or capitalisation was
  applied to the TRIGRAM NEIGHBOURS a confusable query returned — which quietly
  made an exact rule conditional on a fuzzy one. Measured on the live registry:
  `testing` versus `Test_Ing` scores **0.545** against a floor of **0.6**, so the
  variant never reached the check and registered as a brand-new value. This was
  latent in the project registry from v0.8.44 and was reproduced on the domain
  registry the day it shipped: `Shared_Memory_Monitor` is now refused against the
  registered `shared-memory-monitor`, and was not before.

  The spelling check now runs over **every** registered name, ahead of the
  confusable query, through one shared pure helper both axes call. The floor was
  deliberately **not** lowered: that would flatten two populations it exists to
  separate — legitimately distinct names sit just under it — and would train the
  reflex to override a warning that fires on correct input. The two gates answer
  different questions and run in order: a SPELLING is exact equality on a
  normalised key and cannot be confirmed away; a CONFUSABLE is a fuzzy neighbour
  the operator may confirm as genuinely distinct. Both error codes are unchanged.

- **`save_decision --domain` was parsed and dropped on the floor.** The flag
  reached the argument parser and nothing threaded it into the record, so a
  decision fell back to inheriting its evidence's sections. It read as correct
  because the inherited answer happened to match what had been asked for — the
  edge carried `asserted_by='inherited'` where an assertion should have been
  bare. Now packed into the decision blob, beside `project`, which is the half
  the gateway resolves a judgement's axes from. `vector-skill.py`'s
  `save_decision` gains the same parameter, so the two front doors stay at parity.

  The regression tests assert the **provenance of the edge**, not just the
  presence of a section name: a decision whose evidence sits in the same section
  produces the same name either way, and only the stamp tells an assertion from
  a default.

### Notes

- A live probe over the registries found **no** existing pair of projects, and no
  pair of sections within one project, sharing a spelling key — so the defect
  registered nothing before it was caught beyond the one probe value, which was
  retired.

---

## [0.8.47] — 2026-08-05

### Added

- **A domain is now a registered SECTION of one project, with an identity of its
  own.** `domain` had been a free-text metadata field with nothing to be unknown
  against: a typo and a new section were the same event and both entered the
  corpus silently. Migration 028 adds `project_domains` — keyed on a surrogate
  `id`, referencing `projects(id)` and never a project name, with the section's
  label unique only *within* its project because `operations` under one project
  and `operations` under another are different sections that share a word. Its
  alias junction `domain_aliases` lands in the same migration rather than as a
  follow-up, so a retired spelling resolves from the first day the axis exists.

  Ingress mirrors the project protocol exactly: an unregistered value is refused
  `400 domain_unknown` with proposals, and the second submission registers it —
  behind the same two naming guards a new project faces, because the agent that
  sets the flag is the agent that makes the typo. Proposals match a section's
  **description** as well as its name, which is the one real difference from the
  project axis: project names are short and typo-shaped, while an operator
  reaching for a section may type a word that appears nowhere in its name.

  The graph gains `(:Fact|:Decision|:Retrospective)-[:DOMAIN_OF]->(:Domain)-[:PROJECT_OF]->(:Project)`,
  written in the existing single outbox round-trip. `:Domain` and `DOMAIN_OF` are
  **spine**, pinned in code — an amendment to the frozen-spine decision, made
  because the fold gate is intended to read this axis, and a renameable label
  would falsify `ontology.yaml`'s own promise that consolidation touches only
  spine identifiers.

- **Who controls which axis, stated once and enforced.** A **fact** asserts its
  own project and domain and mints its own entities. A **decision** asserts its
  own project and domain, and inherits its entities from the facts it grounds
  in. A **retrospective** asserts neither axis — project *and* domain both come
  from the decision it judges, so a verdict is always filed with what it judges
  rather than with the later evidence that measured it; one that supplies a
  domain is refused `400`.

  A decision that names no domain inherits its grounding facts' sections as a
  **default, never a ceiling**. This is the load-bearing part: a decision reaches
  further than the fact that prompted it. A fact may observe that agents write to
  the graph directly — an infrastructure observation — while the decision it
  provokes governs which agents are *authorised* to write, which is about access
  and sits above the infrastructure that prompted it. Capping a decision at its
  evidence's sections would file it away from the section that most needs to
  surface it. The rule guards itself on the existing provenance stamp: a bare
  edge is an assertion, a stamped one is a default, and inheritance declines
  wherever an assertion exists.

- **`--domain` on the CLI (repeatable) and on `save_decision`**, plus the MCP
  equivalent. The value is stored verbatim and never split on a separator, since
  a separator that can occur inside a value is not a delimiter. The skill
  elicits a section **only when the record's project already has registered
  ones** — a project with an empty registry is never prompted, so the first
  section in any project stays a deliberate act.

- **`GET /health` → `domain_identity`**, beside `project_identity`: registry
  versus graph, plus `unattached` — a section with no `PROJECT_OF` edge. That
  last number exists for a traversal that has not been built yet. Cross-project
  and cross-domain synthesis will walk from a section to its project and from a
  record to its grounding facts, and a section missing that edge would drop out
  of the walk silently, presenting as a quiet corpus rather than an error.

- **`backfill_domain_of.py`** enqueues the historical population through the
  outbox, in two modes: a record's own sections, or a re-run of the gateway's own
  inheritance query for a judgement that asserted none. The second mode exists so
  the rule has a single implementation — a repair that re-derives a rule is a
  repair that can disagree with the thing it repairs.

### Notes

- **Migration 028 ships schema and nothing else — no seed, no data repair.** A
  seed on this axis cannot be derived from the data the way the project registry
  seeded itself, because the values needing registration are exactly the ones
  that must not be registered verbatim. An empty `project_domains` is the correct
  state for a new install: sections arrive through ingress, like projects.

- **There is deliberately no name-keyed `:Domain` fallback**, unlike the project
  axis. Losing a `PROJECT_OF` edge violates an axis that already gates folding,
  so that write falls back to a name; nothing gates on domain yet and the value
  survives in Postgres either way, so the honest answer to "no identity" is no
  edge and a log line.

- **Known, unchanged, and named so it is not mistaken for a defect:** the
  consolidation daemon's internal `domain` variable holds the *project*, and
  `community_summaries.metadata->>'domain'` stores a project name. Nothing in the
  fold path reads a record's `metadata->>'domain'`, which is why this release
  cannot change fold behaviour. Untangling that naming belongs with the release
  that moves the fold gate onto these axes.

---

## [0.8.46] — 2026-08-05

### Changed

- **A decision's options and confidence are no longer copied into the graph —
  they are read from the record that owns them.** Both values were written onto
  the `:Decision` node at first write and projected back out of graph expansion
  into a search hit's `adr_props`. Nothing anywhere filtered, ordered or matched
  on either one: they were only ever rendered. A second copy of a value nobody
  walks on buys nothing the node's `pg_id` does not already give, while
  guaranteeing that the two stores can disagree — and they did. The copy of the
  options silently missed the majority of decisions for months, and the
  confidence copy was measured in exactly that state when this shipped: present
  on every decision that records one in Postgres, present on barely a third of
  the nodes, with a clean cutover no writer could ever close.

  Graph expansion now dereferences both from Postgres in **one batched
  primary-key lookup for the whole walk**, keyed on the `pg_id` every neighbour
  already carries — sub-millisecond, and skipped entirely when a walk turns up
  no decision. The search response is unchanged, field for field, so no client
  needs anything and the wire contract stays at `api_version 4`.

  This is the successor to the projection-widening decision, not a reversal of
  it: that decision bought richer hits at zero extra query and deliberately left
  deeper provenance behind. What it deferred is what arrives here — the reader
  reaches the record instead of a copy of part of it.

  ⚠ **The rule this applies, and its limit:** *duplicate what the walk consumes,
  dereference what the reader renders*. It is a test to apply, not a preference —
  applied to a project's identity the same rule says the opposite, because the
  synthesis gate walks on it. A fact's evidence weight stays on the node here for
  a different reason again: it is **derived** at write, not copied, which makes it
  a separate question rather than the same one.

- **The guard against shredded options moved with the value it protects.** A
  bare `list()` over a JSON *string* explodes it into single characters, turning
  three options into several hundred one-character ones. Every store that has
  held this value has been able to hold it as a string, so the guard now sits on
  the Postgres read rather than on the graph read that no longer happens.

- **The dereference cannot fail a search.** Graph context enriches a search and
  has never been allowed to fail one; adding a query to that path would have
  changed its failure modes, so the whole helper — fetch *and* row handling — is
  fail-open and logs. A payload error costs the hit its `adr_props` and nothing
  else.

---

## [0.8.45] — 2026-08-05

### Fixed

- **The two verifiers could not run the way the documentation says to run them.**
  `verify_schema_init.py` and `verify_neo4j_init.py` loaded their environment by
  importing `python-dotenv` and **returning silently when it was absent**. Nothing
  was loaded, so the next connection failed with `fe_sendauth: no password
  supplied` — a **credentials** error reported for what is actually a **missing
  dependency**, sending the reader to check passwords, roles and `pg_hba` while
  the real cause was the invocation.

  That is worse than an ordinary papercut for two reasons. First, **every
  documented invocation omits the dependency**: `AGENTS.md` and `README.md`
  between them show five `uv run` lines for these tools, none with
  `--with python-dotenv`. So the documented way to prove an install was sound
  could not work on a clean machine. Second, these are the two scripts whose
  entire job is to **prove a property** — a checker that dies for a reason it
  misreports teaches the wrong lesson twice, and the thing it was going to verify
  goes unverified.

  Both now parse the env file directly, in the same dependency-free,
  candidate-list form `apply.py` has always used (framework `shared-memory/.env`
  first, repo root as the pre-0.6 fallback), and neither can be defeated by a
  missing package again. A real exported variable still wins over the file, so
  pointing a tool at another database keeps working.

  ⚠ **The audit that found it was of every `_load_env` in the framework, not of
  one file** — the other fifteen were already self-parsing, and these two were the
  outliers precisely because they were written later and reached for the library.

---

## [0.8.44] — 2026-08-05

### Changed

- **A project is now an identity, and its name is a label on it.** The registry
  gained a surrogate key (`projects.id`, migration 027); the name stays unique
  and queryable, which is what a client asserts, an operator types, and the
  client-side graph templates filter on. The two tables that referenced the name
  now reference the identity.

  The reason this is not bookkeeping: **the project axis gates consolidation.**
  The cross-project fold requires decisions from at least two distinct projects,
  and it counted the project *name*. That is correct only while the set of
  project nodes happens to be one-to-one with the registry — and nothing
  enforced that. A partly-applied rename leaves two nodes for one project, the
  same project counts twice, and a "cross-project" insight gets synthesised out
  of a single project's decisions. An identity error on this axis is a synthesis
  error, not a misfiled record.

  A rename also stops being a distributed rewrite. Records, a registry row, two
  referencing tables, a graph node and every belonging edge all carried the same
  string, so moving it meant rewriting all of them with no stable thing to map
  to. Now the identity never moves and the graph cost of a rename is one
  property write on one node.

- **The fold gate counts identities, and fails closed without one.** A project
  node carrying no identity contributes nothing to the two-project rule rather
  than falling back to its name — a fallback would keep the defect live for the
  whole upgrade window, and permanently for any node the registry does not know.
  The cost is a fold that does not happen; the alternative cost is a false
  cross-project insight. The write path does the opposite and deliberately so: a
  project it cannot identify still gets its node and its edge, because a record
  with no project edge violates the axis outright.

  ⚠ Not the internal node id, which was proposed and is **worse** than the name:
  without a uniqueness constraint, two nodes sharing a name collapse correctly
  under the name and would count as two under an element id.

- **An alias is now an alternate label on one identity**, not a mapping between
  two names. An inactive alias row therefore stays true forever instead of
  needing re-pointing every time its target is renamed.

- **The promotion ledger records both the name and the identity.** The name is
  the evidence — what a record was moved onto, on the day it moved — and a
  rename must never rewrite it. The id is the durable pointer. Its foreign key
  on the mutable name is dropped: a ledger that remembers a name must not be
  forced to forget it when that name stops being current.

- **A decision's project is now checked against the registry, like a fact's.** Decisions were exempt, and the reasoning that exempted them mistook *presence* for *validity*: a decision does fail without a project field, but a present name that no registry knew was accepted, and the graph write then minted a project node for it. That is the one way the graph can end up holding a project the registry does not have — and unlike the ingress→outbox window, which leaves the graph *behind* the registry and always resolves itself, it never does. Retrospectives stay exempt, and that one is a scope statement rather than an oversight: they arrive on their own endpoint and inherit the project of the decision they judge.

### Added

- **Both facts and decisions may introduce a NEW project — and the gateway judges the name, not the claim.** Work legitimately starts before its project exists: a discussion produces an idea, the idea is saved as a fact, and a decision grounded on that fact commits to acting on it. So `new_project` is available on both record types (`--new-project` on `save_decision`, and the existing metadata field on a fact), declared **once**, on the first record that names the project.

  But a declaration is not a defence, because **the client that sets the flag is the client that makes the spelling error**. Two refusals now stand in front of the registry:

  - **`project_spelling_variant` — not overridable.** Names reduce to a comparison key (lowercase, alphanumerics only), so a proposal differing from a registered project only in separators or capitalisation is refused outright, naming the spelling to use. No confirmation can make it a separate project, because it is not one — every retired spelling this framework's registry carries as an alias arrived in exactly that shape.
  - **`project_confusable` — refused once, then confirmable.** Above a trigram-similarity floor the response names the registered projects the proposal is close to, and the caller proceeds only by naming them back in `confirm_distinct_from` (`--distinct-from`). The confirmation is the neighbour's **name**, deliberately not a second boolean: a flag can be flipped without reading anything, while the name cannot be produced without having seen it. Each near match is its own claim — confirming one does not wave through another.

  **The floor is derived, not guessed, and is env-overridable** (`PROJECT_CONFUSABLE_SIMILARITY`, default `0.60`) because it depends on how a deployment names things. Measured over all 666 pairs of one live 37-project registry: the closest legitimately *distinct* pair scored 0.500 and no pair reached 0.6, while typos of a registered name scored 0.78–1.00 and separator/case variants scored exactly 1.00. The default sits in the gap. Too low trains the reflex to override; too high never fires.

  ⚠ Two things this deliberately does **not** do: it never auto-corrects a near-miss onto the closest registered project (that is inference, and a plausible wrong project is worse than a parked one), and it never refuses a similar name outright (a genuinely separate project with a similar name is real, and the operator is the one who knows).

- **`scripts/reconcile_project_identity.py`** — the graph half of a Postgres
  migration, which no migration can perform. It stamps existing project nodes
  with their registry identity, matching by name once, and **refuses to create a
  node or invent a registry row**: a node whose name is in no registry is
  reported and left alone, because deciding what that means is an operator's
  judgement about their own corpus. Idempotent; read-only without `--apply`; now
  part of the documented upgrade path.

- **`GET /health` → `project_identity`** — `nodes`, `unidentified`,
  `mismatched`, `unregistered`, `complete`. Without it an unfinished upgrade is
  invisible: cross-project folds simply stop happening, which looks exactly like
  a quiet corpus. Additive — a monitor that does not know the field renders as
  before. `api_version` is unchanged.

### Fixed

- **A fresh install could not register a single project — the schema generator
  was dropping IDENTITY columns.** `schema_init.sql` rendered `id BIGINT PRIMARY
  KEY` where the live column is `GENERATED BY DEFAULT AS IDENTITY`: valid DDL,
  applies without error, matching constraints, and every insert then has to
  supply the key the database was supposed to issue. Reproduced on a throwaway
  database before the fix — the first `INSERT INTO projects` failed outright.

  **This is the third class of DDL this generator has been found dropping**,
  after every `CHECK` and every `FOREIGN KEY`, and all three shared one shape:
  invisible to the entire test suite, because the only thing that reads that
  file is an install nobody re-inspects. So the fix is in both halves —
  the generator emits identity columns, and `verify_schema_init.py` now diffs
  **key generation** per column, which is the check that would have caught all
  three. Verified by running the verifier against the known-broken file and
  confirming it fails, then against the regenerated one and confirming it
  passes.

---

## [0.8.43] — 2026-08-05

### Fixed

- **An axis declaration can no longer enter the graph as a topic.** A project
  says which project a record *belongs to*. It is established at first write
  from the client's working directory, and it is carried by its own edge — it is
  never a subject a record can be *about*. A previous release closed the typed
  door: the enrichment daemon can no longer create a project node, nor point any
  relation at one. This closes the untyped door beside it, which is the one the
  data actually came through.

  A name of the form `Project: <something>` is an ordinary entity name. It never
  touches the label allowlist, so nothing in the typed gate could see it, and it
  arrived on the same relation every genuine topic uses. Measured on a live
  corpus before the repair: **eleven such entities carrying 152 inbound edges**,
  the largest of them the graph's second-biggest hub with 91. Every record that
  merely *named* a project was being clustered with every other record naming
  it — which is a cluster keyed on the axis, not on a theme, and it had reached
  the point of anchoring narrative folds.

  The inbound entity-name gate now rejects the `Project:` / `Domain:` form
  wherever names enter the graph.

  **This is deliberately a test of the name's FORM, never a lookup against the
  project registry** — the obvious implementation, and the wrong one. Registered
  project names are frequently real topics in their own right: a project is
  often named after the very thing its records discuss, and short registry names
  are ordinary English words. Measured on this corpus, one registry row was
  simultaneously a system entity carrying 91 inbound edges — a gate that
  resolved bare names against the registry would have deleted a hub of true
  statements the same size as the axis hub it was meant to remove. A name that
  spells out `Project:` has declared which axis it is on; a bare name has
  declared nothing. Keeping it a form test also keeps the check pure — no
  database, no I/O.

  `Domain:` is rejected before the domain axis exists, on purpose: the axis is
  specified, and the same mistake is otherwise made twice.

  ⚠ **The gate governs what reaches the graph, never what is stored.** A
  rejected name stays verbatim in the record's own metadata and remains
  searchable there — the episodic tier is left pristine, as it is for every
  other name this gate rejects.

---

## [0.8.42] — 2026-08-04

### Fixed

- **Both skill-delivery paths now agree on what a symlink means, and neither
  skips the manifest.** v0.8.41 made `sync_skills.sh` phase 1 manifest-driven;
  verifying the result on four real installs showed **two of them had never
  received `Documentation/schema.md` at all**, while the script reported
  success. Phase 2 short-circuited on `scripts/` being a symlink and `continue`d
  past everything else — so the symlink, which only makes `memory_bridge.py`
  auto-current, was being read as "this whole install is current".

  This is the second time that short-circuit has caused exactly this, and the
  first fix is why it recurred: `SKILL.md` was hoisted above the `continue`, a
  per-*file* repair to a per-*loop* defect, so the next file added to the
  manifest fell into the identical hole. Phase 2 now iterates `MANIFEST.txt`,
  and the short-circuit decides one thing only — whether `update_skill.sh` needs
  to run.

- **`update_skill.sh` no longer writes through a symlink.** It applied every
  staged file with `mv`, which *replaces* a symlink with a regular file. So the
  self-update path silently undid the arrangement the sync path depends on: a
  repo-linked file, auto-current by construction, became a frozen copy of that
  day's content — invisible until it had gone stale. The exact mirror of the
  defect above, on the other delivery path. A symlinked destination is now left
  as a link and reported as such.

### Changed

- **⛔ An installed skill file is now always a REAL COPY, never a symlink into a
  source checkout.** Repo-linking `memory_bridge.py` bought auto-currency at a
  price that is only visible once: it binds every agent on the machine to one
  checkout's *path*, so moving, renaming or archiving the project breaks all of
  them at once — silently, with the first symptom being an agent failing
  mid-task. Staleness is the lesser risk precisely because it is **detectable**:
  every file is content-compared on each sync and `doctor` reports version skew.
  It also makes the local development path produce the same result as the
  shipped one, since `update_skill.sh` fetches from GitHub and writes real files
  for everybody else already.

  Both delivery paths now **replace** any symlink they find, and both close the
  hazard that creates: `cp` and `cmp` each *follow* a link, so a naive
  implementation would write into the source tree and would report a link
  pointing at identical content as "already current" forever. A symlinked
  `scripts/` or `Documentation/` is dissolved into a real directory before
  anything is written inside it, and `sync_skills.sh` **refuses** an install
  directory that is itself a link rather than making the source its own
  destination. README's four per-agent blocks now copy the whole package —
  they previously installed two of the six files the manifest ships — and
  `AGENTS.md` states the copy-only rule.

- **`sync_skills.sh`'s agent list is env-overridable** via
  `SHARED_MEMORY_SYNC_AGENTS` (colon-separated), instead of four `$HOME` paths
  baked into a code path. Those four are *our* agent set, not *the* agent set —
  and hardcoding them is also what made this delivery logic untestable, which is
  how the same defect shipped twice. `tests/test_skill_delivery.py` now runs the
  real scripts against a temporary tree and asserts what actually lands there,
  because the whole defect class lives in shell control flow: a test that read
  the source for a filename would have passed throughout both failures, since
  the filename was there — above a `continue` that skipped it.

---

## [0.8.41] — 2026-08-04

### Fixed

- **Renaming a project is now one transaction per pair, applied whole or not at
  all.** `normalize_projects.py` used to run three independent sweeps over the
  alias map: rewrite every record and commit, then try to record every alias,
  then rewire every graph node. The second sweep could fail on a pair the first
  had already committed, and the third ran regardless of both — so a single
  failure left a state no sweep could describe: records moved onto the new name,
  the old name still registered with no alias recorded, and the graph pointing
  at the new node anyway. None of it was reported as a failure, and the exit
  status was zero.

  Each pair is now one transaction. It commits whole or rolls back whole, its
  graph half runs **only** if its Postgres half committed, a failed pair is
  named with its reason and does not stop the pairs after it, and the run exits
  non-zero when any pair failed. The target's registration is checked **before**
  any write rather than after the records were already committed onto it.

  This matters because two foreign keys point at `projects.name`, so retiring a
  registry row can be vetoed — and atomicity is what makes that veto harmless:
  the pair rolls back, the old name stays registered and still resolves, and
  every save keeps working. The hazard was never the veto, it was the half-write.

- **A rename no longer destroys the name a promotions-ledger row originally
  targeted.** Ledger rows are re-pointed inside the same transaction, with the
  original target preserved in the row's own `note`. The ledger exists to answer
  *what was this before*; silently rewriting its target would destroy exactly
  the evidence that makes a one-way write auditable. Never `ON UPDATE CASCADE`,
  which would do the same damage with no trace at all.

- **`--dry-run` is now a real preflight.** It reports the records, ledger rows
  and alias rows a rename will touch — including **superseded** alias rows,
  which are deliberately not re-pointed (re-pointing them would falsify the
  history they exist to preserve) and will therefore veto the rename. Finding
  that out from a failed run is finding it out too late.

- **`sync_skills.sh` no longer refreshes a hardcoded subset of the client
  package.** Phase 2 and the parity test both read `MANIFEST.txt`; phase 1 read
  a list of filenames in the script. So a file could be added to the manifest,
  ship to every agent, and be refreshed by nobody. `Documentation/schema.md` was
  updated at source in two consecutive releases, copied to the tracked skill
  tree in neither, and shipped stale to every client for both — while the script
  printed success. Phase 1 is now driven by the manifest too, so the manifest's
  own promise that it is "the whole maintenance surface" is true.

- **`Decision.alternatives` can no longer be shredded into single characters on
  the read path.** The graph-expansion projection called `list()` on the
  property unconditionally. All 223 Decision nodes currently hold a Neo4j LIST
  OF STRING, where that is a harmless passthrough — but this property has been
  written as a JSON *string* before, and `list()` on a string explodes three
  alternatives into several hundred one-character ones. A string is now one
  entry.

### Added

- **`migrations/verify_neo4j_init.py` — proof that the declared Neo4j
  constraints are actually in force.** Postgres has a migration ledger; Neo4j
  has none. `neo4j_init.cypher` is a one-time manual step, so a long-lived
  instance enforces whatever was true the day someone last applied it, and a
  constraint added in a later release reaches new installs and nobody else. That
  failure is silent by construction: `MERGE` keeps working, writes keep
  succeeding, and the only symptom is a duplicate node appearing under a race —
  at which point the constraint that would have prevented it is the thing you no
  longer have.

  The script diffs declared against live, and for each missing constraint counts
  the duplicate values that would make `CREATE CONSTRAINT` fail — because
  "missing" and "cannot be added without repairing data first" are very
  different situations and the difference must not be discovered halfway through
  an apply. Constraints belonging to another system on a shared instance are
  reported as foreign, never touched. Read-only by default; `--apply` creates
  what is missing. Exit status 1 when a declared constraint is not in force.

  ⚠ It also handles an upgrade trap that re-running `neo4j_init.cypher` cannot:
  Neo4j refuses `CREATE CONSTRAINT` while a **plain index** covers the same
  label and property. A fresh install never meets this, because the constraints
  are applied before anything creates an index; an instance where someone added
  a lookup index by hand is blocked indefinitely, with no error unless somebody
  goes looking. `--apply` drops the conflicting index first, which costs nothing
  — a uniqueness constraint creates its own backing index on the same key.

### Changed

- **`AGENTS.md` upgrade path and README §6 now cover the Neo4j side.** The
  documented upgrade ran `apply.py` and restarted, which covers Postgres and
  says nothing about Neo4j; and README described `neo4j_init.cypher` as
  idempotent and safe to re-run, which is true and insufficient — re-running it
  does not clear a blocking plain index and does not tell you whether anything
  is enforced. Both now point at the verifier, and Phase 5 confirms both stores
  rather than trusting an exit status.

---

## [0.8.40] — 2026-08-04

### Added

- **Decisions can now be grouped by what they CONSIDERED, not only by what they
  concluded.** A decision records the options it weighed, but a decision has one
  embedding and it is dominated by the decision's own text — so two records that
  weighed the same option look unrelated unless their headlines happen to agree.
  Each alternative now becomes a row in `decision_alternatives` with a vector of
  its own, keyed back to the decision, so an alternative-level match resolves to
  a pair of **decisions**, which is the answer wanted. Postgres only: a node per
  alternative would be a mostly-singleton node named with free prose.

  The vectors are filled **after** the save, by a background worker, so a
  decision that weighed eight options costs the same single embedding call on
  the request path as one that weighed none. What makes that safe is that
  pending work is a query over committed rows (`embedding IS NULL`) rather than
  a queue held in a process: a crash or a restart between the write and the
  embed leaves work the next sweep finds. A pending row is never written off —
  the attempt counter drives backoff and raises a `failing` flag in telemetry,
  but no value of it stops a row being retried, because an alternative that
  cannot be embedded is nearly always a statement about the embedder rather than
  about the row.

  Alternatives are **reconciled, never appended**. A save can rewrite a record
  in place, and alternatives do get rewritten; the write path converges on the
  decision's own array, so entries whose text is unchanged keep their vectors,
  changed entries return to pending, and retracted ones are removed. Saving the
  same decision twice therefore embeds nothing.

- **Coverage for the above** at `GET /memory/telemetry` →
  `spine.alternative_vectors`: entries, embedded, pending, failing, and the age
  of the oldest pending row. Existing `decisions.alternatives_pct` says how many
  decisions *recorded* alternatives; this says how many of those entries are
  actually retrievable, and a full percentage beside a stalled backlog means the
  populator has stopped.

- `Documentation/schema.md` documents the table, the reconcile rule, and the
  decision-pair similarity query — including why that query **needs a similarity
  floor derived from the deployment's own corpus**, since ranking without one can
  never answer "nothing here considered the same thing".

### Notes

- Migration **026** creates the table and deliberately performs **no backfill**.
  Seeding rows from records that already exist is a data operation calibrated on
  a corpus, not schema; and filling the table at migration time would make the
  populator's first sweep a bulk run, which looks nothing like the steady state
  it has to be verified in. An upgrading deployment fills forward from the next
  decision saved, and may seed its history whenever it chooses — the reconciler
  converges, so that is safe to run at any time.

---

## [0.8.39] — 2026-08-04

### Fixed

- **A new install's consolidation cycle was never told that anything had been
  saved.** The consolidation daemon waits on a Postgres notification channel for
  saves to arrive, and something has to send on that channel — a trigger on the
  records table. **That trigger was never shipped.** It existed on the machine
  this framework was developed on, where it had been created by hand early on,
  and no migration created it, so it was absent from the migration chain and
  absent from the fresh-install schema too. Every other deployment has been
  running a daemon listening to a channel with no sender.

  The cost was not silence — the listener also polls, and carries its own idle
  and backstop thresholds, so consolidation still ran eventually. What was lost
  is the prompt path: a save no longer announced itself, and a cycle only began
  when the backstop fired. The system looked like it was working, slowly, for a
  reason nothing reported.

  The trigger and its function now ship as a migration, and are therefore
  present in the fresh-install schema as well. The change is a no-op on any
  deployment that already had them.

### Added

- **A check that proves the fresh-install schema, rather than trusting it.**
  `migrations/verify_schema_init.py` builds a throwaway database from the
  fresh-install file alone, diffs its tables, constraints, functions and
  triggers against a live database, and exits non-zero if a new install would
  differ. **Run it after every migration.** It touches no data: the live
  database is opened read-only and never written, and the throwaway is dropped
  behind a name-prefix guard.

  It reconciles a unique constraint against a unique *index* before reporting,
  because the two enforce the same thing and the schema generator re-emits one
  as the other — the point is to diff behaviour, not catalogue rows.

  **This defect was its first finding.** It is the class of problem that is
  invisible from the inside: introspecting a working database finds the object
  and concludes the schema is fine. Only building a database from the shipped
  files and comparing shows the gap.

- Tests asserting that every channel the daemon listens on has a sender in
  shipped SQL, and that every function in the fresh-install schema is reachable
  from a migration — derived from the daemon and the migration chain rather than
  restated beside them, so they cannot quietly go stale.

---

## [0.8.38] — 2026-08-04

### Fixed

- **A decision's considered alternatives were being torn into pieces as they
  were saved.** Both ways into the framework — the command-line skill and the
  MCP tool — accepted the alternatives as one string and split it on commas.
  A well-written alternative contains commas, because it names the option *and*
  says what was wrong with it, so any such entry was stored as fragments that
  do not stand alone. It happened silently, in both stores, and nothing in the
  record afterwards said which pieces had once been a single thought. Across
  this framework's own corpus it had damaged **one decision in five** of those
  carrying alternatives.

  The separator is now gone rather than replaced. **Pass the flag once per
  alternative** (`--alternatives "…" --alternatives "…"`); the MCP tool takes a
  list. A value arriving as a single string is recorded as exactly one
  alternative — at worst under-split, and never inventing an option nobody
  wrote. Both front doors were changed together: they carried identical code,
  so fixing one would have left the other quietly shredding.

- **The synthesis prompt then re-created the same ambiguity when reading them
  back.** Alternatives were rendered to the summarising model as a single line
  joined with semicolons — and a quarter of the entries in a real corpus contain
  a semicolon of their own, so the model could not tell where one option ended
  and the next began. A decision could be stored perfectly and still be read
  back shredded. Each alternative is now rendered on its own numbered line, so
  an entry may hold any punctuation at all.

  The general rule this establishes: **a separator that can occur in the data is
  not a delimiter** — on the way in or the way out. Changing how a value is
  written is not finished until the paths that read it have been traced.

### Changed

- The skill and system prompt now ask for each alternative as **self-contained
  prose carrying its own reason** — "no consolidation at all (facts then never
  fuse)" rather than "no consolidation" — because each alternative is indexed
  independently, and a bare fragment gives later retrieval nothing to match on.

---

## [0.8.37] — 2026-08-04

### Fixed

- **The enrichment cycle was quietly rewriting which project a record belongs
  to.** A record's project is part of its identity: it is established when the
  record is written, from the working directory it was written in. The
  enrichment pass — which reads a record's text and links it to the things it
  talks about — was also permitted to create project nodes and attach that
  identity edge itself. So **any record whose text merely mentioned another
  project could end up claiming to belong to both**, and the two answers to
  "which project is this in?" depended on whether you asked the database or the
  graph.

  It also kept retired project names alive: after two spellings of one project
  were merged, enrichment would re-attach references to the retired node,
  leaving something the merge had just finished removing.

  Enrichment can no longer refer to a project at all — not to write the identity
  edge, and not to mention one as though it were a topic. The guard is in two
  independent places on purpose. The first removal alone would have left a
  subtler version of the same problem, because unrecognised node types are
  silently treated as ordinary topics, so a project would have slipped through
  wearing a different hat. The prompt no longer advertises the capability
  either, and the test that checks this derives what the prompt may say from
  what the code will accept, so the two cannot drift apart again.

### Added

- **A renamed project is now remembered, not merely renamed.** Renaming used to
  rewrite every record onto the new name and forget the old one, which failed
  in two ways. The retired name came *back* — a folder on another machine still
  carried it, so the next save from that machine recreated the variant the
  rename had just removed. And with the judgement recorded nowhere, the old
  name looked like an unknown stranger to every later review, so a decision made
  once had to be made again every time anybody looked.

  Retired names now resolve to the current one as records arrive, and the record
  is stored under the current name. A machine that cannot be reached, or a
  folder nobody wants to rename, stops mattering. The mapping is kept rather than
  applied and discarded, so the history of a name is a question the database can
  answer. Renaming and remembering happen together, in one step.

  Resolution deliberately follows exactly one link. Chains do occur — one project
  here had been spelled three ways across two machines — and following a chain
  while records are being saved could loop. Chains are collapsed when the rename
  is recorded instead, so arrival stays a single lookup.

- **Projects can be registered from the directories that define them.** A project
  name is the project folder name, so which projects exist is a directory
  listing rather than something to infer from stored records — inferring it
  registers the misspellings alongside the projects. Names matching a folder need
  no thought; everything else is surfaced as a question, which on this corpus cut
  the names needing a human decision from seventeen to nine. Near-misses and
  names with no local folder (work assisted from another machine is not the same
  as work retired) are reported, never resolved automatically.

- **A tool to make the graph's project edge agree with the database.** It repairs
  a *disagreement* and never fills an *absence* — the two look alike in a query
  and are not the same act, and conflating them would have made a large silent
  change wearing the label of a repair.

### Fixed (schema)

- **Functions and triggers were missing from the fresh-install schema**, the
  third kind of object the generator had silently dropped after check
  constraints and foreign keys. A rule that no single table can enforce alone
  would have held on every upgraded deployment and on no new one.

## [0.8.36] — 2026-08-04

### Added

- **A record whose project could not be established at first write can now have
  it established later — through exactly one writer.** Such a record is
  *parked*: it saves, searches and enriches normally, and is simply never folded
  into a project's narrative. Establishing the project afterwards is a state
  transition, and routing it through a single writer is deliberate — a property
  that gates behaviour must not have a second writer, which is a defect this
  project has already shipped once.

  The transition only ever runs one way. A record that already names a project
  is refused, because overwriting an established answer is how a value changes
  meaning without anyone deciding that it should. The cost of that choice is
  that a wrong promotion cannot be undone through the supported path, so every
  promotion writes a durable ledger row recording what the value was before, on
  what basis it changed, and who asked. The Postgres metadata and the graph edge
  are written in one transaction, the graph half through the outbox, so a
  partial run leaves durable work rather than half a graph.

  The automatic caller establishes a parked fact's project from the judgements
  that cite it as evidence, and only when those judgements agree on exactly one
  project. Two answers leave the record parked: parked is visible and
  repairable, and a plausible wrong project is neither.

- **Two repair tools.** One reconciles the graph's project edge to the value
  Postgres holds — in that direction only, since Postgres is where the value was
  asserted and validated, and copying an edge back into metadata would turn a
  graph-side accident into an asserted fact. The other establishes projects for
  parked records from their citing judgements. Both report by default, write
  only when asked, and refuse outright when they cannot confirm the running
  server is new enough to apply what they produce.

### Fixed

- **A record could accumulate more than one project.** The graph writer only
  ever added an edge, never replaced one, which is correct while every target
  has no edge and wrong as soon as one does. Records therefore kept stale
  project edges alongside current ones, and "which project is this in?" could
  give two different answers depending on whether you asked the database or
  counted edges. The writer now replaces, and a repair pass collapsed the
  existing cases.

- **The fresh-install schema was missing every foreign key.** The tool that
  generates it renders tables, indexes and check constraints by inspecting a
  built database — and silently discarded foreign keys, with a comment claiming
  the schema used none while one had existed for months. So a guarantee held on
  every upgraded deployment and on no new one, which is the worst shape a schema
  difference can take, and nothing could notice because the only thing that
  reads that file is an install nobody re-inspects. Foreign keys are now
  emitted, and the result is verified by building a database from that file
  alone and comparing it against a migrated one.

- **Three maintenance scripts could not run on a correctly-installed machine.**
  They looked for credentials only in the repository root while the documented
  location is the framework directory, so they failed with an empty password
  rather than a missing one. One of them claimed in its own documentation to
  match the migration tool, which had always looked in both places.

## [0.8.35] — 2026-08-03

### Fixed

- **Migrations now run exactly once. They used to be re-run in full, every
  time.** The apply tool listed every numbered migration and executed all of
  them on each invocation, while its own documentation said it ran "all
  pending" — nothing anywhere recorded what had already been applied, so
  "pending" silently meant "all of them". Most migrations tolerate that, which
  is why it went unnoticed across twenty-two of them. But tolerance is not the
  same as being safe to repeat: one migration removes duplicate summaries on a
  key that a **later** migration changed, and re-running it against the newer
  schema deleted twelve summaries that were legitimately distinct under the key
  in force.

  The general point is not about that one file. **A migration is written
  against the schema as it stood at that moment**, so running it again later
  runs it against a schema it was never written for. Making every migration
  safe to repeat is not achievable; running each one once is.

  **The database is now its own ledger** — the record of which migrations have
  run is a table inside the database being migrated, not a file beside the
  migrations or state kept in the repository. So the answer to "how far has
  this database got?" travels with the database itself: restore a backup and
  the record comes back with it, already agreeing with the schema it describes;
  copy a deployment and the copy knows its own version; point the tool at
  another host and it reads that host's state. The tool reads that mark and
  resumes from it, and a migration and its ledger entry are committed together,
  so a half-applied migration can never be recorded as done and skipped
  thereafter.

  A database that predates this record is not guessed about in either
  direction: the tool refuses to proceed and asks for a one-time instruction to
  adopt what has already run. Guessing "already applied" would skip a genuinely
  new migration; guessing "not applied" would repeat the original failure.

  The duplicate-removal step is **additionally guarded** to do nothing once the
  newer key exists — the ledger is the real fix, but a destructive step should
  assert that its assumptions still hold rather than trust them. That guard
  proved its worth immediately: while the refusal path was being tested, a
  second route into "re-run everything" was found, and the schema came through
  intact only because the guard was there.

## [0.8.34] — 2026-08-03

### Fixed

- **The fresh-install schema was silently dropping every table-level CHECK
  constraint.** The file new deployments build from is generated from the live
  database, and the generator rebuilt each table from its columns and indexes
  alone — so a constraint added by a migration existed on every **upgraded**
  deployment and on **no new one**. That is the worst shape a schema divergence
  can take: the guarantee holds everywhere it was tested and nowhere it was not,
  and nothing in the upgrade path can notice.

  Recovered by the fix: the reservation that stops the no-project sentinel being
  registered as a real project — which is enforced in the schema precisely so no
  future code path can bypass it, and was therefore absent exactly where that
  mattered most — and **seven constraints on the relation-adjudication ledger**
  that had been missing from fresh installs since that table was introduced,
  covering its family, verdict, method, operator label, evidence support, the
  confidence range, and the rule tying its family to which columns must be set.

  The regression tests read the generated file rather than the generator's code,
  because a generator that still contains the right function but no longer calls
  it would pass a source-level check.

## [0.8.33] — 2026-08-03

### Added

- **A projects registry, so an unrecognised project is loud instead of merely
  new.** Until now a project was whatever string a client happened to send, and
  there was nothing for a value to be unknown *against* — a typo and a genuinely
  new project were the same event, and both entered the corpus in silence. The
  registry is seeded from the projects a deployment is already using, read from
  its own records rather than any hardcoded list, so it fits whatever install it
  runs on. **Descriptions are deliberately left empty**: a description is what
  the synthesis prompt reads as framing, so inventing one would put words into
  the corpus that nobody wrote — an empty description says "not yet supplied"
  where a guess would say "supplied, and wrong".

### Changed

- **A fact save without a registered project is now rejected.** The rejection
  carries which of the two things went wrong — nothing supplied, or supplied but
  unrecognised — and, for a near miss, **near-match proposals from the registry**
  so the caller can act on it instead of guessing. Matching is by name
  similarity, which needs no embedder: registration therefore cannot be taken
  down by an embedding outage, which a vector-only lookup would risk.

  **The exchange ends in one of three ways, and only these three:** pick a
  proposal, declare the value a new project and register it, or park the record
  on the sentinel. Re-sending the same unregistered name is refused however often
  it is asked. There is no retry counter on the server — the bound comes from
  those three answers all being accepted, not from per-caller state a server
  would have to keep and expire.

  ⚠ **This is a breaking protocol change** (`api_version` 3 → 4) for any client
  that saved facts without a project. Update every client together.

- **The rejection tells the model to ask the operator, not to infer.** An agent
  that guesses produces a record filed under a plausible wrong project, which is
  worse than one left unfiled: unfiled is visible and repairable, misfiled is
  neither.

- **`general_discussion` is a reserved sentinel for a record that belongs to no
  project.** It saves, searches and gets enriched like any other record, and is
  never folded into a project's narrative or counted as a project. The name
  cannot be registered as a real project — the database refuses it, so no future
  code path can claim it by accident.

- **The client derives the project from an absolute source reference** when the
  working directory is not inside any project root. Deterministic only: a
  relative reference names no location, so nothing is guessed from it.

- **Reasoning traces carry a project like any other record.** Exempting them
  would have quietly rebuilt the very population of untagged records this work
  removes, so the tool takes the project explicitly rather than defaulting it.

### Fixed

- **The schema reference shipped to clients had fallen several releases behind
  its source**, missing two tables and a column documented long ago. The
  copy-parity check now reads the ship manifest instead of a hand-written list of
  two files, so every shipped file is compared and a newly shipped one is covered
  without anyone remembering to add it.

## [0.8.32] — 2026-08-03

### Changed

- **A record with no resolvable project now folds nothing, instead of pooling
  with every other such record.** An untagged record used to fall back to a
  default key, so *every* record in the corpus that nobody had assigned to a
  project shared one bucket — and once that bucket passed the density
  threshold it folded, fusing unrelated material into a single narrative on the
  strength of a property none of them had. An absence is not a subject: two
  records that each fail to name a project have nothing in common. They are now
  skipped rather than grouped, and the daemon's partitioner and the telemetry
  gauge call one predicate so they cannot disagree about which records those
  are. On the development corpus this affects 129 records and freezes 12
  existing summaries built on the old bucket — see *Known limitation*.

- **A project node is created only from a project.** The node and its edge are
  written from the record's resolved project, never from a section of one and
  never through a fallback chain, so the set of projects in the graph stays a
  set of projects.

### Added

- **A backfill for the project edge on records written before it existed**, run
  as a prerequisite of this release rather than a follow-up: nothing can be
  gated on an axis that two thirds of the corpus does not carry. It **enqueues
  repair work through the same outbox every other write uses** — it never writes
  the graph directly — so a partial run leaves durable work rather than half a
  graph. Dry-run by default, idempotent, and it leaves records with no
  resolvable project alone rather than inventing one for them.

  The repair is deliberately **narrow**: replaying an ordinary record write
  would also re-run that record's subject links and resurrect every enrichment
  edge a later sweep deliberately removed. It matches the existing record and
  never creates one, and its queue row is deleted on success so a repair is
  never mistaken for pending work.

  It **refuses to run against a server too old to understand it**, and fails
  closed when it cannot determine the server's version. An older server would
  treat the repair as an ordinary record write and blank the stored content of
  every record it touched — the guard makes that ordering error unarmable
  rather than merely documented.

### Known limitation

Twelve summaries synthesised from the old shared bucket remain active and
searchable but will never be refreshed, because the key they were built on no
longer forms. Their content is not wrong — it was synthesised from real
records — and retiring them now would destroy narratives for records that are
about to be repaired. Once those records carry their real project they will
fold under it, and the stale pair should then be retired; that belongs with the
repair release, not here.

## [0.8.31] — 2026-08-03

### Changed

- **One project resolution, shared by every reader.** Which project a record
  belongs to was worked out in eight places across five files. Three agreed;
  two also fell back to the record's `domain`, and one further to its `scope`.
  So the same record could answer "which project?" differently depending on
  which component asked — and because a judgement carries its project inside
  its decision payload rather than at the top level, **219 of 261 decisions
  read as untagged while carrying a project all along**. The resolution now
  lives in one place and every reader imports it. `domain` leaves the chain: a
  domain is a **section of** a project, and a section cannot stand in for the
  whole it belongs to. `scope` leaves it too — scope is access control, so
  resolving through it keys a record by who may *see* it rather than what it is
  *about*, which on any deployment that uses scopes partitions summaries along
  permission lines. The one deliberate exception is the spelling-normalisation
  tool, which must find a record whose old project name is shadowed by a newer
  one and so matches **either** field rather than resolving between them.

- **The decision backlog gauge now runs the gate it claims to measure.**
  It counted every decision awaiting consolidation and grouped them by project
  — no shared subject, no requirement that a cluster span two projects, no
  requirement that any decision had an outcome recorded against it. Those are
  the actual conditions the consolidation daemon folds on, and none of them can
  be expressed as a grouping of stored values, so the gauge reported a backlog
  the daemon could not act on. Re-sourcing its project values would only have
  made a meaningless number better-sourced, so the count is now the daemon's
  own query with a count in place of its result rows: one definition, two
  projections, unable to drift. **This moves reported numbers without changing
  any fold behaviour** — on the development corpus the decision backlog reads 0
  where it read 2, and the funnel behind it is 108 candidate clusters → 3 that
  span two projects → 0 with an outcome recorded. What blocks these folds is
  decisions still owing a retrospective, not density.

- **The reported decision threshold tracks the real gate** instead of a
  hardcoded copy that sat beside it, so a deployment that tunes the
  consolidation threshold now sees the tuned value rather than a stale twin.

- **The enrichment pass is told which project a record belongs to.** Its
  capture manifest read only the top-level field, so every decision reached the
  enrichment prompt with no project at all — the model was asked to enrich a
  record while the record's project was withheld from it.

## [0.8.30] — 2026-08-03

### Changed

- **The enrichment pass now works in concepts, not in spellings.** The alias
  layer already groups every surface form of one concept — `LM Studio`,
  `LMStudio`, `LM_Studio`, `lm_studio` — and both the folding stage and search
  group on that verdict. The enrichment pass was the one consumer that did not:
  it resolved on the raw name, so its prompt offered four separate "known nodes"
  for one thing, the model proposed several of them, and the re-check correctly
  confirmed each — four true links to one concept, and no confidence floor could
  ever reach them, because the same question was simply being asked four times.
  A proposal now **matches on any known spelling and is written to one canonical
  node**: the form the most first-write records actually use, ties broken
  alphabetically, so the choice is stable and moves only when people write. The
  novelty check, the recall budget and the sub-typing pass all compare concepts
  too — a record already linked under one spelling no longer acquires a second
  edge under another, and a record's candidate budget buys distinct concepts
  rather than several ways of writing one. Measured on a live graph: 1015
  accepted spellings resolve to 814 concepts across 106 collision groups, every
  one of which the alias layer had already grouped.

- **A retracted claim no longer keeps its vocabulary eligible for new links.**
  An entity became linkable once some record named it at first write, and a
  *superseded* record still counted — reasoned at the time as "supersession
  retracts a claim, not the vocabulary it was filed under, and the successor
  almost always reuses the same concepts". That second clause undid the first: a
  successor that reuses the concept **is** a live first-write namer, so the
  entity qualifies through the successor and never needed the exemption. What the
  exemption actually protected was the opposite case — names a person filed and
  then retracted, with no successor reusing them. On the live graph 11 of 1026
  eligible entities were held up by a superseded namer alone, several of them
  parse artefacts, two already accreting machine links. Eligibility now requires
  a **live** first write. Nothing is deleted and existing links are untouched;
  the retired names simply stop being reachable for new ones, and no record
  anywhere depended on one for a topic.

## [0.8.29] — 2026-08-03

### Changed

- **A proposed link now has to survive its own re-check before it is written.**
  The enrichment pass proposes a connection, then re-reads the record to confirm
  it. That verdict used to govern only whether a later synthesis stage could
  *use* the edge — the edge itself was written regardless. Measured on a worked
  case: a record whose every proposal was denied by both re-checks still gained
  24 links, because the withholding rule applied only to records with no cited
  source. **The better-evidenced a record was, the looser its gate.** The
  confidence now gates the write itself, on one rule that no longer varies by
  record kind: a record citing a real source (code, a test, an external
  document) may link on a majority re-check, while an uncited or conversational
  one needs unanimity. The record's kind still moves the score; it no longer
  moves the gate.

- **Consumption threshold raised to match the write floor.** What is trusted
  enough to enter the graph is trusted enough to be folded into a summary; two
  different numbers for those two questions is what let the gap fill with links
  nothing would ever read.

### Fixed

- **Verification now fails closed.** When every re-check call failed, the vote
  arithmetic divided one vote by one attempt and handed the proposal the
  *highest possible* confidence — the score peaked precisely because nothing had
  checked it. Unverified is not unanimous. No successful re-check now means no
  edge, in every relationship family. (No occurrence of this is present in two
  months of retained logs; it was a latent hole, and a gate that depends on the
  score made it load-bearing.)

- **A judgement's copy of its evidence's topics is stamped as a copy.** It used
  to be written unmarked, which is exactly the signature first write leaves when
  the *operator* names a concept — so a copy was indistinguishable from a
  naming, and machine-added names could re-qualify themselves as valid link
  targets through it. The copy now records that it is one, and carries the
  standing of what it copied: from an operator naming it is operator-grade, from
  a machine link it carries that link's score and is gated exactly as the
  original was. Applied to new writes only — the unmarked edges already in the
  graph mix three different writers, and for older records they cannot be told
  apart after the fact.

### Note

- Record→record proposals are deliberately exempt from the write floor. They are
  born capped *below* their own usability threshold so that human adjudication,
  never the proposer, promotes them; a floor above that cap would make them
  unwritable at birth and close that path silently.

---

## [0.8.28] — 2026-07-31

### Fixed

- **The rule shipped one release ago was leaking, and the leak ran backwards
  through the graph.** The enrichment pass may only link to a concept that first
  write named, and that was tested by looking for a mention edge carrying no
  machine stamp. Two different writers leave that mark, and only one of them is a
  person naming something: a fact's first write, which materialises the operator's
  list — and the inheritance walk that gives a decision or retrospective its
  topics, which copies whatever its facts already carry, **machine-added names
  included, without their stamp**.

  So there was a cycle. The pass adds a name to a fact, correctly marked as its
  own and correctly refused. A decision resting on that fact then inherits the
  name unmarked. The name now reads as one a person chose, and the pass may link
  it to anything, anywhere. Enrichment was laundering its own output back into the
  set it is supposed to be constrained by.

  The test now requires the naming record to be a **fact**. Measured on a live
  graph: 2127 concept nodes, 1023 named by a fact, and **432 that qualified only
  through a judgement's unmarked edge** — 94 of them traceable to the enrichment
  pass. On the worked case, 20 of the 31 machine-added topics stop qualifying.
  The accept set drops from 1455 concepts to 1023.

  The remaining 338 are the older second vocabulary source: decisions used to name
  their own concepts at first write, which is the very faucet the inheritance rule
  closed. Those names were never vetted on a fact either, so they fall outside on
  the same reasoning rather than by accident.

- **A claim in the previous release's own documentation was false and is
  corrected.** It stated that the pass could never re-qualify a name it had
  introduced, because everything it writes carries its stamp. That reasoning
  skipped the inheritance step, which strips the stamp.

## [0.8.27] — 2026-07-31

### Changed

- **The enrichment pass connects; it does not reinvent.** It may now only link a
  record to a concept that *first write* named — an entity carrying a mention
  with no machine stamp, from a real record. Everything it writes carries its own
  stamp, so it can never re-qualify a name it introduced itself: the gate cannot
  bootstrap on its own output.

  This targets relations, not vocabulary. Creating entities was already closed;
  what was still open was the enrichment pass building links *into* names only it
  had ever used, which then read as topics a person had chosen. Measured on a
  live graph before the change: of 2584 concept nodes, 1449 had been named by a
  person at capture time, 677 only by the enrichment pass — and those 677 had
  accumulated 897 machine links between them.

  Stated plainly, because the number invites over-reading: this does **not**
  catch a sentence-shaped name that *capture itself* admitted. On the worked
  case — a record saved with three deliberate concepts that acquired thirty-one
  more — every added name is first-write-named somewhere and survives the gate.
  That is a capture-surface problem and is not addressed here.

- **Entity creation is no longer configurable.** The setting that let a
  deployment re-enable machine-minted entities is gone, along with the branch it
  guarded and the prompt sentence that tracked it. An enrichment pass that coins
  vocabulary produces names no one chose, and retrieval rests on join keys a
  person is accountable for — so this is a property of the framework rather than
  a deployment posture. The prompt now states the one behaviour there is.

- **A decision's alternatives, conditions and insights may only point at
  concepts.** That branch never passed through relationship resolution, so
  nothing stopped the enrichment pass asserting "considered" against the person
  who made the decision. The door was open and had not been walked through; it is
  closed now.

### Observability

- The accept set reports what it withheld, per cycle, so the rule is falsifiable
  in production rather than merely asserted.
- The link-gate journal line was reworded: a refused name is now either absent
  from the graph *or* deliberately withheld, and reading the count as a pure
  creation rate would over-report it.

## [0.8.26] — 2026-07-31

### Changed

- **Decisions and retrospectives no longer name their own topics — they inherit
  them from the evidence they rest on.** A fact is the only record that can
  introduce a concept into the graph. A judgement's topics are now derived by
  walking its grounding edges to the facts beneath them, so the same concept can
  never arrive twice under two spellings: once vetted on a fact, once free-typed
  beside a decision.

  The walk is three tiers, first non-empty winning: grounding the operator
  asserted, then grounding the system defaulted from `fact_kind`, then — for a
  record citing no evidence of its own — the facts of the record across the
  `HAD_OUTCOME` edge. That last tier is what lets a decision which grounds
  nothing still reach consolidation through the retrospective that judged it.

  `entities` on a decision or retrospective is still accepted for older callers
  and is ignored by the graph. It remains required on facts.

### Fixed

- **Four of the six grounding roles donated no topics at all.** Inheritance
  matched `GROUNDED_IN` alone, but a grounding role is written as one of five
  relationships — `considered`, `rejected`, `under_conditions` and `informed_by`
  each produce their own. The bare-pg_id path made this routine rather than
  exotic: a fact with no `source_ref` derives `fact_kind = discussion`, whose
  default role is `INFORMED_BY`, so a decision citing its evidence in the exact
  form the documentation recommends inherited nothing and never reached
  consolidation. The relation set is now derived from the role map itself, so a
  role cannot be added without every traversal that reads grounding seeing it.

- **Grounding on an earlier decision or retrospective reached no topics.**
  Citing a prior judgement is first-class lineage, but the walk required a fact
  as the immediate target. It now passes through a cited judgement to *its*
  facts — one hop, terminating on a fact — so provenance chains carry topics
  without ever copying the labels a judgement happens to hold.

- **A retrospective that cited nothing could blank the tier for a decision whose
  older retrospective did cite something.** The newest verdict was selected
  before checking it reached any facts. Selection now happens after the topic
  match, so the newest retrospective *with* evidence wins; a retrospective
  carrying no date sorts last rather than first.

- **Retracted facts kept acting as cluster keys.** Superseded facts are now
  excluded from inheritance. Superseded *judgements* are deliberately not — a
  decision overturned by a reversal is still what its successor is about, and
  filtering there would blank the reversing verdict's own topics.

- **Decisions and retrospectives could be superseded directly, corrupting the
  graph.** Supersession is the fact lifecycle: a fact is a claim about the world
  and is retracted when the world changes, whereas a judgement is a dated act and
  the record that it turned out wrong is a *retrospective*. Both ingress paths now
  refuse a judgement target. Overturning a decision goes through a retrospective
  rated `reversed`, which marks it superseded as the consequence of a verdict that
  stays in the graph for a successor to ground on; revising a retrospective means
  saving a new one, since the latest live verdict is the one that counts.

  The supersession mirror also matched its target as `MERGE (old:Fact {pg_id})`,
  which matches on label and property together — so a judgement's id minted a
  second, phantom `:Fact` carrying the supersession while the real node stayed
  unmarked. It now marks the real node under any spine label and creates a
  placeholder only when nothing carries that id.

- **A non-text `decided_by` was silently destroyed.** A JSON client sending a
  list passed the truthiness check at ingress and was then overwritten by the
  attested principal, which can only preserve a string claim. Decision fields are
  now required to be non-empty strings, so the wording is refused while the caller
  still holds it rather than lost without trace.

- **The client surface contradicted the new rule.** Both CLI subcommands still
  advertised `--entities` as the field that links topics, the usage block omitted
  `--grounded-in`, and every decision saved as documented tripped a level-1
  `no_entities` warning. `--entities` is now marked deprecated and ignored,
  `--grounded-in` is documented as the load-bearing field, and the warning follows
  the record type — judgements warn on absent *grounding*.

### Known issue

- REM's novelty gate still treats caller-supplied `entities` as already-captured
  `MENTIONS` edges, a claim first write no longer makes. A decision saved with
  `entities` and no `grounded_in` therefore gets no edge from either side. Tracked
  in issue #180.

---

## [0.8.25] — 2026-07-30

### Fixed

- **A completed Tier-3 narrative could be discarded because the embedding call
  timed out.** The consolidation daemon embedded with a hardcoded 20-second
  timeout, no retry, and no clamp on input size — none of the guards the save
  path already had, on the code path that handles the system's *largest* texts
  and reaches the embedder only after minutes of generation.

  Embedding cost is superlinear in input length. Measured on the reference rig
  (BGE-M3 335M Q8_0, llama.cpp, CPU): throughput falls from 438 tok/s at 236
  tokens to 148 tok/s at 7 414, fitting `wall = 1.92e-3·n + 6.48e-7·n²` to
  within 0.52 s across the range — roughly 59 s at the model's 8 192-token
  context. **The 20-second constant therefore covered only about 52% of the
  embedder's own context window**, so a fold could synthesise a summary it was
  structurally unable to vectorise, and lose the entire generation. The failure
  was also attributed to nothing: it is neither a preservation nor a truncation
  failure, so it was invisible to both telemetry and the dead-letter cap.

  **The embedder's context is now the invariant the timeout is derived from.**
  BGE-M3 refuses an oversized input outright rather than truncating, so every
  caller clamps what it sends; because the input is clamped, the longest
  embedding call the framework can make is a fixed known quantity, and the
  timeout is computed from it in code: `tokens / EMBED_MIN_TOK_S ×
  EMBED_SAFETY_FACTOR`, floored for small inputs. At the shipped defaults a
  full-context call gets 123 s against a measured true cost of ~59 s, and a
  short save still sits on the 20 s floor.

  Both embedding paths now share one derivation, so the save path and the fold
  path cannot drift apart — they call one embedder with one context limit. This
  also raised the coordinator's own ceiling: its shared 30-second client default
  did not cover the maximally-sized input its own clamp allows (~36 s).

  New env knobs, all documented in `.env.example`: `EMBED_MAX_CONTEXT_TOKENS`,
  `EMBED_CHARS_PER_TOKEN`, `EMBED_MIN_TOK_S`, `EMBED_SAFETY_FACTOR`,
  `EMBED_TIMEOUT_FLOOR_S`. `EMBED_MAX_CHARS` still overrides directly but now
  *defaults* to `EMBED_MAX_CONTEXT_TOKENS × EMBED_CHARS_PER_TOKEN` (24 576,
  previously a flat 24 000) rather than being a magic number.

  **LLM timings are deliberately untouched** — `adaptive_ceiling` and
  `LLM_MIN_TOK_S` behave exactly as before, guarded by a test.

- **`Embedding error:` logged nothing after the colon.** The handler printed
  `str(e)`, which is empty for an httpx timeout, so an operator could not tell a
  timeout from a refusal from a 500. It now names the exception class, the
  ceiling that was applied, and the input size.

---

## [0.8.24] — 2026-07-30

### Fixed

- **The preservation gate's paraphrase slack rounded away to nothing for the
  cluster sizes that actually occur.** Plain-fact anchors are meant to tolerate a
  little loss to legitimate paraphrase, expressed as a 90% coverage ratio. But
  slack is spent as a whole number of dropped anchors, and cluster sizes are small
  integers, so `floor(size * 0.10)` is **zero for every cluster below ten records**.
  The density threshold makes five-to-nine the ordinary band, so the advertised
  tolerance never reached the common case — and the ratio also stepped
  discontinuously across neighbouring sizes, gating a nine-record cluster strictly
  all-or-nothing while its ten-record neighbour got one free drop. The budget is now
  a count with an explicit floor (`NREM_PRESERVATION_SLACK_MIN_UNITS`, default 5):
  clusters at or above the floor get at least one droppable soft anchor, smaller
  ones stay absolute, and the budget never shrinks as a cluster grows.

  **The hard-required rule for decision and retrospective anchors is untouched** —
  the slack floor can never rescue one, and is covered by a test that says so.
  Note this corrects the *parameter*, not fold reliability: folds observed failing
  in practice drop more anchors than any slack setting would forgive, and each
  corrective retry drops a *different* subset rather than converging. That is an
  anchor-design problem, tracked separately.

---

## [0.8.23] — 2026-07-30

### Fixed

- **Tier-3 synthesis was bounded below the floor it had to clear, so the busiest
  clusters could not fold at all.** Both NREM fold prompts are cumulative: each is
  handed the previous summary (or insight) and told to expand it, so a successful
  fold must re-emit that entire narrative before adding a single new record. The
  previous narrative's own length is therefore a hard floor under the output bound —
  and it rises every time the fold succeeds. The shipped 2048 sat at **0.62x** that
  floor for this framework's own busiest cluster, whose existing summary was 3315
  tokens. Below the floor the fold cannot succeed by any path, and the two recorded
  failure modes are one cause wearing two hats: obey the bound and content must be
  dropped, which fails the *preservation gate*; obey the gate and the bound is
  overrun, which fails as *truncation*. After `NREM_FOLD_FAIL_CAP` of either, the
  dead-letter cap removes the cluster from Tier 3 entirely. Because the floor is the
  existing narrative, the domains with the most accumulated history cross it first —
  the failure lands precisely on the clusters that matter most, and it presents as a
  consolidation stall rather than as a misconfiguration. Both bounds now default to
  8192; the widened truncation retry follows at 16384.

- **The per-call timeout was blind to the output bound, so raising that bound traded
  one failure for a worse one.** `adaptive_ceiling` scaled with prompt size and work
  units — both *input* terms — while decode time is driven by `max_tokens`. A widened
  16384-token retry needs roughly 1638s at the shipped throughput floor but received
  the 600s default, so the long generation the retry exists to permit was killed by
  its own timeout. That is strictly worse than truncating: a timeout raises a generic
  exception rather than setting the truncation flag, so it is never counted in
  `truncation_failures` and the capacity failure leaves no trace in telemetry. The
  ceiling now takes a fourth term, `max_tokens / LLM_MIN_TOK_S`, and both NREM call
  sites size it on the **widest** bound they may retry at rather than the first one
  they try.

### Added

- **`LLM_MIN_TOK_S`** (default `10`) — the slowest generation rate, in tokens per
  second, that the ceiling will sit through. This is the one constant that is purely
  a property of the operator's hardware, so it is an env knob and is documented as
  the value to expect to change. It is set deliberately *below* observed throughput
  rather than at it, because a ceiling sized on the average kills every
  slower-than-average run; the reference rig measured min 11.29 / mean 13.93 / max
  15.10 tok/s over 16 folds. Keep `max_tokens / LLM_MIN_TOK_S` under
  `NREM_FORCED_SLOT_WAIT` or a long fold outlasts REM's willingness to yield the
  shared LLM slot.

- **`LLM_CEILING_FLOOR` is now documented in `.env.example`.** It governed every
  dream LLM call's timeout and had never been published as a tunable.

### Changed

- `adaptive_ceiling(prompt_chars, units=0, max_tokens=0)` takes an optional third
  argument. The parameter is additive and defaults to the term being inert, so every
  pre-existing call site — REM's three and `relation_sweep`'s two — keeps its exact
  previous ceiling. A regression test asserts that equivalence directly.

## [0.8.22] — 2026-07-30

### Fixed

- **The shipped system prompt sent the model to an unauthorized path.** Its search
  hierarchy listed a direct-Bolt Neo4j MCP as the fallback when graph depth was
  insufficient — the exact class of access the README forbids and that was removed
  from the MCP client itself in 0.8.0. A database MCP connects past the gateway, and
  the gateway is what applies read authorization: it filters every read on
  `visibility` (`global`, the caller's own `private`, rows matching its `scope`),
  while a raw SQL or Bolt connection filters on nothing and returns every private
  record any agent ever saved. The escalation step is now the authorized
  `graph_query` tool, and the prohibition is stated for **both** stores — the SQL one
  named explicitly, since a generic query tool looks harmless beside a graph driver
  and reaches the same rows with the same absence of a predicate.

- **The README's MCP config example produced a client that could not authenticate.**
  The `rag-orchestrator` entry carried no `env` block at all, so it had neither
  `AGENT_TOKEN` nor `COORDINATOR_URL` — while the surrounding prose told the reader
  to replace "all `YOUR_*` placeholders", of which the block contained only a Tavily
  key. Every memory route has required a bearer token since 0.3.5, so anyone
  following that section built a client that came up and then failed every call with
  a 401. Broken on arrival rather than merely out of date. The block now carries both
  values, with the token's three valid locations spelled out and the one that is a
  trap called out: on the gateway host the client sits beside the *framework* env,
  which it refuses to load.

- **Five of thirteen MCP tools were undocumented in the system prompt**, so a model
  driven by it had no way to know it could trace a record's lineage, run an
  authorized graph query, archive a reasoning trace, or take part in relation
  calibration. The calibration pair matters most: machine-asserted edges stay
  invisible to synthesis until a family has roughly twenty operator labels, so an
  undocumented tool meant an inert half of the graph with nothing indicating why.

### Changed

- **The system prompt now states the rules that decide what gets written**, none of
  which it previously carried: that a record id is unique only within its table, so
  a bare integer taken off a summary result resolves against the wrong one; that
  decisions and retrospectives require asking the operator for grounding, roles,
  alternatives and confidence before saving; that `rating` is a closed set of outcome
  states rather than a grade; and that the token, not any client-supplied field, is
  what identifies who saved a record.

### Added

- **Guards tying the docs to the surface they describe.** The system prompt must name
  every tool the MCP server registers — derived from the source, so adding a tool
  without documenting it fails. The search hierarchy must not name a database MCP for
  either store, the prohibition must name both, and the shipped `mcp.json` must not
  itself register one. The README's config example must carry `AGENT_TOKEN`.

## [0.8.21] — 2026-07-30

### Fixed

- **The MCP client could load the framework's own env, inheriting every other
  agent's credentials.** It loaded `.env` from the directory it is installed in,
  which is the right shape — a per-install file is what lets a second MCP host hold
  its own token — but this script ships at the repo root, and that is exactly where a
  pre-0.6 install keeps the **framework** env: `AGENT_TOKENS` (the entire registry),
  `PG_PASSWORD` and `NEO4J_PASSWORD`. A client that reads it holds every agent's
  token, which defeats the purpose of per-agent tokens being separately identifiable
  and separately revocable.

  It still loads the file beside itself, but now refuses one carrying server-only
  keys and says what to do instead: give the client its own directory and `.env`
  (only `AGENT_TOKEN`, optionally `COORDINATOR_URL` / `AGENT_ID`), point the new
  `VECTOR_SKILL_ENV` at one, or inject the token through the MCP host's own env
  block. `AGENT_TOKEN` is deliberately not mistaken for `AGENT_TOKENS`, and
  commented keys do not trigger the refusal, so a `.env` copied from `.env.example`
  still loads.

  Worth stating plainly, because it shaped the fix: **client identity comes from the
  token, server-side.** The gateway overwrites a record's `source` with the
  authenticated agent, so a client cannot assert who it is. `AGENT_ID` is only a
  local label — but it had two different defaults, one at module level and another at
  three call sites, so a single process labelled itself inconsistently depending on
  which tool ran. It is read in one place now.

- **Six MCP tools handled a rejected token without recording it.** A helper already
  existed that both logs the auth failure and returns a uniform message, but six
  tools inlined their own copy of the text and skipped the logging — and those six
  were all the **write** tools, so write-path auth failures never reached the audit
  log at all. Three variants of the message had drifted apart, and the guidance in
  five of them was already stale. Every one of the twelve `401` paths now uses the
  helper, called with the calling tool's name rather than a constant, so the log says
  which call was rejected and the token-source guidance lives in exactly one place.

### Removed

- `import asyncio` from the MCP server — unused. Verified by reference analysis
  rather than inspection: it was the only genuinely dead name in the file, since the
  tool functions that look unreferenced are registered by decorator.

## [0.8.20] — 2026-07-30

### Fixed

- **A client self-update skipped everything when only `SKILL.md` had changed.**
  `update_skill.sh` compared the installed `memory_bridge.py`'s `VERSION` against the
  remote one and, on a match, printed *"Already up to date. Nothing to do."* and
  exited before fetching a single file. But that version anchors the *client script*,
  not the package: a release that touched only `SKILL.md` — the elicitation surface,
  which decides what an operator is asked for before a save — left every remote client
  reporting itself current while serving stale prompts indefinitely. Nothing enforced
  "if `SKILL.md` changed, `VERSION` must bump"; it was a human habit, and one
  docs-only merge would have broken it silently. This is the same class as the sync
  short-circuit fixed in 0.8.19, on the remote path where nobody would notice.

  The decision is now made on **content, per file**, at apply time: every manifest
  file is fetched (a handful of small files — cheaper than being wrong) and only files
  that actually differ are replaced. Versions are still read, for the message. Each
  outcome reports distinctly — `REFRESHED` never reads the same as `already current` —
  so the next drift is visible rather than hidden behind uniform success output.

- **`AGENTS.md` Phase 8 installed two of the six files the skill ships, breaking its
  own later phases.** It said to install `SKILL.md` and `memory_bridge.py`. The
  package manifest also ships `CONSTITUTION_SNIPPET.md`, `.env.example`,
  `scripts/update_skill.sh` and `Documentation/schema.md` — and Phase 8b copies its
  block from `CONSTITUTION_SNIPPET.md` *in the skill directory*, while Phase 8c and
  every later update run `update_skill.sh` *from there*. An agent following Phase 8
  literally produced an install that could not perform the phases immediately after
  it. Phase 8 now names the manifest as the authority, directs the agent to let the
  tooling read it, and states the symlink-versus-copy rule explicitly: the script is
  symlinked, `SKILL.md` is copied, and a symlinked `SKILL.md` opts that install out of
  automatic refresh.

- **The consolidation triage guide sent the operating agent to the wrong endpoint.**
  `AGENTS.md` described the per-type fields — `eligible_clusters`, `runs_24h`,
  `deferred_24h`, `idle_24h`, `last_deferred_reason` — under a section that opens with
  `curl /health`. Those live on `GET /memory/telemetry` (what the client's `status`
  reads); `/health` carries only the summary (`stalled`, `stalled_types`,
  `last_success_age_seconds`, `last_success_cycle_type`). Every field name was
  correct, so the error was invisible until an agent actually looked for a per-type
  value and found nothing — at precisely the step the guide calls the actionable one.
  The two halves are now split by endpoint, and the stated triage order says where to
  switch.

- **The enrichment prompt could contradict the entity gate it describes.** The rule
  telling the model what happens to an unrecognised name was written as a literal
  "DROPPED, not created" — true only because creation is off by default. With
  `REM_MAY_MINT_ENTITIES=1` the prompt asserted the opposite of what the code would
  do, which is exactly the defect fixed in 0.8.16/0.8.17, where the prompt had gone
  on promising that unknown names "will become generic Entity nodes" long after they
  stopped doing so. The rule is now derived from the flag rather than typed
  separately, so the two cannot diverge again.

### Changed

- **The MCP surface is documented at the parity it actually has.** The skill
  documented six MCP tools; thirteen exist. `graph_query`, `record_lineage`,
  `memory_telemetry`, `check_memory_health`, `review_edges`, `label_edges` and
  `archive_reasoning_trace` were all reachable but unmentioned, so an MCP client had
  no way to know it could read telemetry, trace a record's lineage, or take part in
  relation calibration at all. The code was ahead of its documentation, not behind.

### Added

- **Guards for the whole client-distribution surface.** The two tracked
  `update_skill.sh` copies must be byte-identical; version equality must not
  short-circuit the update; the apply step must compare content per file; refresh and
  no-op must report differently; `AGENTS.md` must mention every file the manifest
  ships, so Phase 8 cannot silently fall behind the package again; and the prompt's
  mint rule must track the flag in both settings. The load-bearing guards were
  mutation-checked — restoring the early exit, and removing the per-file comparison,
  each fail their own test.

## [0.8.19] — 2026-07-30

### Fixed

- **`sync_skills.sh` silently stopped updating `SKILL.md` on every install whose
  client script was symlinked.** The per-agent loop short-circuited on "this install
  is repo-linked, already current" when it found `memory_bridge.py` to be a symlink,
  and skipped the rest of the work for that agent. But `memory_bridge.py` is the
  *only* file that is ever symlinked — `SKILL.md` is **copied**. So on every install
  configured the recommended way, `SKILL.md` was written once at install time and
  never refreshed again, while sync reported success on every run.

  This is the worst file to rot silently. `SKILL.md` **is** the elicitation surface:
  it carries the prompts that decide which fields an operator is asked for before a
  save. A stale copy asks for the wrong fields, and the capture-surface review that
  is a release gate reads the file **in the repo**, not the one agents actually have
  — so the gate could pass on every release while the deployed prompts drifted
  arbitrarily far behind. Measured on the machine where this was found, three of four
  agent installs were serving a `SKILL.md` many versions old.

  `SKILL.md` is now refreshed **before** the symlink check, which is narrowed to what
  it can legitimately claim: the *scripts* are auto-current. A symlinked `SKILL.md`
  (an unusual but valid layout) is skipped rather than written through, and a refresh
  now prints distinctly from a no-op, so the next silent drift is visible.

### Changed

- **The Why-To loop example no longer promises a shortcut that already exists.** It
  was labelled as raw Cypher pending a future named shortcut; `why-to-check` has
  shipped for some time and is documented directly above it, so the example now
  points at the shortcut and presents itself as the underlying form. The query body
  was already correct — `rating` and the notes live on the `Retrospective` node, not
  on the `HAD_OUTCOME` edge, which carries only `date`.

### Added

- **Guards so this class of drift fails loudly.** The two tracked `SKILL.md` copies
  must be byte-identical, mirroring the invariant the client script already had; and
  the ordering the fix depends on — the copy preceding the short-circuit — is pinned
  directly, along with the symlink guard and the distinct refresh reporting. The
  ordering guard was mutation-checked: restoring the original ordering fails it.

## [0.8.18] — 2026-07-30

### Added

- **Retrospectives are measured on their own terms.** The spine-coverage telemetry
  reported completeness for two record types, `decisions` and `facts` — and `facts`
  meant "everything that is not a decision", so every retrospective was counted
  inside it. A retrospective's required fields are not a fact's: it reports an
  outcome state (`rating`), names the decision it judges (`target_pg_id`), and cites
  the records that measured that outcome (`grounded_in`). None of those were
  measured, so retrospective first-write quality was invisible, and a project whose
  standing rule is measure-first cannot improve what it does not measure.

  `telemetry.spine` now carries a `retrospectives` block reporting `total`,
  `rating_pct`, `target_pg_id_pct`, `grounded_in_pct` and `elicited_pct`. The data
  was already being captured — the two fields the block projects were sitting in
  `emergent_unprojected_fields`, which is the list of keys stored but never
  measured — so this is a read-side projection with no schema change, no
  capture-surface change, and no API version bump.

  On the live install this immediately surfaced what the bundling hid: `rating` and
  `target_pg_id` are set on every retrospective (100% each, since every write path
  sets them — read those as a regression alarm rather than a trend), while
  `grounded_in` sits at **17.4%**. Most retrospectives assert an outcome without
  citing what measured it, which is exactly the gap the metric exists to expose.

### Changed

- **⚠ `spine.facts` now means facts — its total will DROP for consumers.** Excluding
  retrospectives (and decisions, as before) is required for the blocks to be
  coherent: leaving the old predicate while adding a retrospectives block would
  count 839 records as 994, since every retrospective would appear in two totals.
  Dashboards rendering `spine.facts.total` will see it fall by the retrospective
  count. The response *shape* is unchanged and no existing field was renamed or
  removed, so this needs no API version bump — but it is a semantic change to a
  number consumers already display, and it is deliberate.

  It also corrects a figure that was understating fact quality: `source_ref_pct`
  reads **100%** over true facts, against **78%** when retrospectives — which
  mostly lack `source_ref` — were diluting the denominator. The bundled number was
  not merely coarse, it was wrong about facts.

- **`rating` and `target_pg_id` no longer appear as promotion candidates.**
  `emergent_unprojected_fields` lists metadata keys captured but not projected;
  now that these two are measured, continuing to list them would advertise as an
  unmet opportunity the very metric that just landed.

## [0.8.17] — 2026-07-30

### Fixed

- **Enrichment was dropping mentions of entities that exist, because one capped
  list did two different jobs.** The enrichment prompt lists known entity names so
  the model matches them exactly instead of coining near-duplicates, and the same
  list is what the link gate resolves proposals against — the names *shown* and the
  names *accepted* were a single fetch ordered by name, capped by `ENTITY_SET_LIMIT`
  (default 1500). Once a graph passes that cap, the tail of the alphabet falls out of
  both halves at once: an entity is never offered to the model, and if the model
  names it anyway from the record's own text, the gate treats it as unknown and drops
  the edge. So facts stopped accumulating on the entities they were about, and those
  entities stopped moving toward the density threshold a thematic summary needs. The
  truncation was logged, but as a prompt-size warning — nothing said that acceptance
  had narrowed too.

  Scale of the loss, stated separately from the default because the two differ. The
  install where this was found runs `ENTITY_SET_LIMIT=2000` against 2599 distinct
  named nodes, so **~609 names (≈23%) were invisible** — the observed `grounding_n`
  topped out at 1996 and the fetch returned exactly its 2000-row limit, which is
  what raised the truncation warnings. On the shipped default of 1500 the same graph
  would hide ~1099 names (≈42%). Both are the same defect; only the magnitude scales
  with the cap.

  The two sets are now bounded by what each is for. The accept set is the whole
  registry, under a high safety valve (`ENTITY_REGISTRY_LIMIT`) that means "prune
  the graph" rather than "tune this". The shown set is the `ENTITY_PROMPT_K`
  entities nearest the record's own text, ranked by embedding cosine — relevance
  instead of alphabet, so the prompt gets *smaller* as the graph grows while
  covering the entities a record is actually about. Because the gate now accepts
  names the prompt never showed, a miss in ranking costs prompt relevance and no
  longer costs a dropped link.

- **Ranked candidates are filtered against the live graph.** The entity-embedding
  store is insert-only and outlives the nodes it describes; measured on a live
  install it held 4396 names against ~2600 existing ones, and more than half of a
  top-80 recall were names the graph no longer has. Offering those invites a
  proposal the link gate must then reject, so they are discarded before the prompt
  is built. The gap is not hypothetical drift: a single entity-hygiene pass had
  removed 1,840 nodes whose embedding rows all remained, which accounts for nearly
  the whole discrepancy.

### Changed

- **Semantic recall degrades, never fails.** If the embedder is unreachable or the
  embedding store is empty, grounding falls back to the previous alphabetical slice
  (`ENTITY_SET_LIMIT`, unchanged in meaning and default) rather than to an empty
  list — an empty shown set would leave the model nothing to match and turn every
  name it produces into a dropped edge, which is the worst outcome precisely when
  retrieval is already degraded.

- **Batched enrichment merges its members' candidates round-robin.** One shared
  prompt covers several records, so each contributes its nearest entities in turn
  under the shared budget; concatenating would spend it on the first record and
  leave the last ungrounded.

- **Grounding telemetry now separates what was offered from what can be accepted,
  and names the number that matters.** Each record reports the accept-set size, the
  shown-set size, and whether the shown set came from semantic recall or the
  fallback. The count of referenced-but-unrecognised names is reported as
  `unresolved` (the previous key is kept for continuity with metrics already on
  disk): since the link gate stopped creating nodes, that number is no longer
  entities coined, it is **links lost** — a record mentioning a real entity whose
  edge was never written. The recall mode is what keeps it honest, distinguishing a
  genuine regression from an embedder outage.

## [0.8.16] — 2026-07-28

### Changed

- **The enrichment pass now links but never creates.** When it proposed a
  relationship to a name that did not yet exist as a node, it created one. That
  was the single path by which sentence fragments — a clause lifted out of a
  rationale, not a concept anyone named — became first-class entities, and from
  there became candidates for alias resolution and keys for thematic summaries.
  Measured against a live graph, every fragment-shaped entity present had come
  from that path; the capture path, the only other way an entity can be created,
  had produced none of them. So the gate that already governed one branch of
  enrichment now governs all of them: an unrecognised name is dropped and
  counted, never minted. Sub-typing is unaffected, because it labels a node that
  already exists.

  This makes the capture surface the sole origin of entities, which is where a
  human has actually named the concept. Deployments whose capture surface does
  not name entities up front can restore the previous behaviour by setting
  `REM_MAY_MINT_ENTITIES=1`; the count of refused names is reported in the
  journal so the effect is observable either way.

## [0.8.15] — 2026-07-28

### Fixed

- **The graph-integrity count is reported at the top level of the health
  response**, not nested inside the consolidation block. It rides the same
  cached snapshot for cheapness, but it is not a dream-cycle metric — it counts
  nodes a write path stored under the wrong label — and nesting it there would
  have led a dashboard to render it as part of consolidation health. Caught
  before any consumer had read it.

## [0.8.14] — 2026-07-28

### Added

- **Graph-integrity defects are now visible.** The enrichment pass has always
  been able to spot a node whose *label* contradicts the record its id names —
  the signature of a write path that stored something under the wrong type. It
  retired such nodes, recorded the diagnosis on them, and logged a warning. But
  nothing ever read that verdict, so the only trace was a log line scrolling
  past and a property nobody queried. Three separate write-path defects were
  each detected correctly this way and still went unnoticed for weeks, found
  only when someone went looking by hand.

  The count now appears on the health endpoint, and a breakdown — how many, and
  which label was written where — on the telemetry endpoint and in the client's
  `status` output, so a dashboard or an agent can see it without a bespoke
  query. Read it as a **defect, not a backlog**: it does not drain on its own,
  so any non-zero value names a writer that needs fixing and nodes that need
  repairing. Until the first probe completes the count reads as *unknown* rather
  than zero, so "not yet checked" can never be mistaken for "verified clean".

### Changed

- **A fact's default evidential kind is now `discussion`, not `observation`.**
  Every fact is produced in a conversation — that is the base case, not a
  degenerate one. What a citation records is which *external* context entered
  that conversation and raised the fact above it: source code, an external
  source, or an empirical check. `observation` accordingly stops being the
  default and becomes what it always should have meant — *a conclusion reasoned
  out in the discussion* — which is now stated explicitly rather than being
  where unmarked facts silently landed.

  This has a deliberate consequence. An unmarked fact grounds a decision as
  *soft input* rather than as *hard evidence*, because the advisory gate maps
  the conversational kind to the softer grounding relation. An unqualified claim
  should not enter synthesis carrying the weight of evidence, and previously it
  did. Existing relationships keep the role they were written with; nothing is
  rewritten.

- **Readings taken from the running system now count as empirically verified.**
  A census of the live graph, a health reading, a check against the journal — all
  are verified against reality rather than derived from code, but they cite no
  file, so they used to fall to the floor and weigh the same as a passing
  remark. A citation may now name a live locus, and is classified as tested.

### Fixed

- **A path merely containing the letters "test" was treated as a test path**, and
  so promoted to the *highest* evidential weight. Files like `latest_run.py` or
  `greatest_hits.md` qualified. Since synthesis is told that tested and measured
  evidence outranks discussion, this silently strengthened claims nobody had
  verified. A citation must now name a test as an actual path component.

## [0.8.13] — 2026-07-28

### Fixed

- **A decision could not be grounded on a retrospective — the link was silently
  discarded.** When a decision records the evidence it rests on, each target is
  looked up so the edge can point at the real record. That lookup recognised only
  two kinds of target, decisions and plain facts, so a **retrospective** target
  fell through to the plain-fact default: the graph gained an empty placeholder
  node carrying the retrospective's id, while the retrospective itself was never
  linked. Nothing failed and nothing was logged — the save succeeded and the
  relationship simply did not exist.

  This matters because it breaks the framework's own outcome loop at its most
  useful point. A decision is judged by a retrospective; when that verdict is
  *refined* or *reversed*, the decision that replaces it is naturally grounded on
  that retrospective. That is the one link which explains **why** a later decision
  was made — and it was exactly the link being dropped. The target's label is now
  resolved from its actual record type, exhaustively, so every spine record kind
  is a valid grounding target. An unknown or absent type still resolves to a plain
  fact, as before.

  Deployments that already attempted such a link keep the placeholder node until
  repaired; the relationship, its role, and its operator attribution were all
  recorded faithfully and only ever pointed at the wrong node.

## [0.8.12] — 2026-07-27

### Added

- **Facts now carry their own provenance in the graph, not just in metadata.**
  Previously only decisions recorded *who* and *with what tool* as traversable
  relationships; a fact's origin lived only as flat metadata. At first write a
  fact now gains a custody trail — the agent that produced the record, the
  operator it acted on behalf of, and the project it belongs to — modelled as a
  delegation (the record is attributed to the agent, and the agent acts on behalf
  of the person) rather than attributing the knowledge directly to the operator.
  That distinction matters: a fact surfaced by a web search or a code review is
  *committed by* an operator but not *authored by* them, so custody and authorship
  are kept separate. All three values are derived automatically — the person from
  the kernel-attested login, the agent from the request token, the project from
  the working folder — so nothing new is asked of the caller. Each edge is written
  only when its value is known (e.g. the person is omitted on a transport with no
  attested login).

### Changed

- **Thematic summaries can now cite where a fact came from.** The consolidation
  fold already told the model each record's evidential *kind* (measured, tested,
  researched, …); it now also carries the citable *origin* — the file path or
  source domain a fact was derived from — so a synthesised narrative can say
  "measured from `coordinator.py`" instead of just "measured". The origin travels
  as a threaded property derived deterministically from the source citation, not
  as a graph edge: clustering facts by source is a filter, not a traversal, so no
  new node or relationship type is introduced. Observations and discussions, which
  have no external origin, carry no origin marker.

## [0.8.11] — 2026-07-27

### Changed

- **Graph expansion now carries a neighbour's decision/evidence metadata in the
  search hit itself — no second query.** When a search returns a summary, its
  one-hop graph context lists the records the summary was folded from (the
  decisions behind a cross-project insight, the facts behind a thematic
  summary) — but previously each of those neighbours came back as an id plus a
  120-character snippet only, so *how confident* a folded decision was, *which
  alternatives it rejected*, and a folded fact's *evidence weight* (its kind and
  source citation) were dropped even though they sit on the very node already
  being read. The expansion now projects those properties into an `adr_props`
  object on each neighbour — a decision's `confidence` and `alternatives`, a
  fact's `fact_kind` and `source_ref` — closing the gap for zero extra queries
  and no extra graph traversal. Deeper provenance that genuinely lives further
  out in the graph (the person who decided, the grounding evidence, the recorded
  outcome) is unchanged here and remains a deliberate, separately-designed
  opt-in. Purely additive: the key appears only on neighbours that carry those
  properties, so existing `graph_context` consumers are unaffected.

---

## [0.8.10] — 2026-07-23

### Security

- `GET /health`'s `config.llm_backends` now carries `has_credential` (bool)
  and `model` per backend — tracked regardless of whether an external/paid
  backend is actually configured, so the capability is monitor-visible from
  the moment it exists. Gated behind a valid gateway bearer token: an
  anonymous caller still sees only `url`/`weight`, unchanged from before. The
  raw token itself is never exposed to any caller — verified by forcing real
  `ClientError`/`TimeoutError`/generic `Exception` paths through the proxy
  with a real token configured and inspecting the actual client-visible
  response.
- `LLM_BACKENDS_JSON` now **rejects** a literal `token`/`api_key`/`secret`/
  `key` field instead of silently ignoring it — that backend is excluded
  from the pool with a loud, specific log line, rather than the real secret
  sitting in plaintext in whatever file holds the config while the backend
  silently gets no credential either way.
- Verified empirically, for both token types (gateway access tokens and LLM
  API tokens), that neither can leak through telemetry or the audit log —
  new tests drive real requests through the actual code paths (including a
  full flush-and-grep of the async audit-log writer) rather than relying on
  code review alone.

### Added

- **`shared-memory/ops/install_service.sh`** — installs
  `hive-mind-gateway.service`, substitutes `WorkingDirectory`/`Documentation`
  for the actual checkout (converts an SSH `origin` remote to a browsable
  `https://` URL), `daemon-reload`, `enable --now`, `loginctl enable-linger`.
  Idempotent, offered as a prompt at the end of `install_framework.sh`.
- **`shared-memory/ops/install_llm_backends.sh`** — interactive per-backend
  wizard: URL, optional local systemd supervision (takes the operator's own
  launch command, never constructs one), optional credential (only the
  env-var **name**, with a shape-validated rejection loop that catches a
  pasted literal key). Writes `LLM_BACKENDS_JSON`, replacing cleanly on
  re-run. Also offered from `install_framework.sh`.
- `AGENTS.md`, `README.md`, and `shared-memory/ops/README.md` updated so the
  never-write-a-raw-key-to-a-file convention is enforced at every surface an
  operator or an operating agent actually touches, not just documented in
  one place.

---

## [0.8.9] — 2026-07-23

### Security

- **The proxy forwarded a client's own gateway-auth `Authorization` header
  verbatim to whatever LLM backend served a request.** Local llama.cpp
  backends ignore unknown headers so this was invisible, but a client's
  internal gateway token would leak to any external backend added to the
  pool — and could never have been a valid credential for one anyway.
  `_filter_headers` now strips `Authorization` unconditionally for every
  proxied request (LLM pool, embedder, reranker, both retry attempts).

### Added

- **`LLM_BACKENDS_JSON`** — lets a reasoning-LLM backend carry its own
  `token_env` (an env var *name*, resolved from the gateway's own process
  environment at startup — never a literal secret written to `.env` or any
  file) and its own `model` id override, injected only for that backend,
  never from the client. This is what makes a real external/cloud LLM
  (tested live against DeepSeek's API) a genuinely supported pool member
  alongside local hardware backends, rather than a silent misconfiguration.
  The plain `LLM_BACKENDS` comma form keeps working unchanged for anyone who
  doesn't need this. A `token_env` naming an unset variable excludes that
  backend from the pool at startup (logged) rather than sending a doomed
  request; the pool always falls back to a usable backend, so an all-LLM-
  down state degrades to per-request 503/504 on the reasoning endpoint only
  — save/search never touch this pool.
- `shared-memory/ops/README.md` documents the credential-handling
  convention: keep the raw key in an encrypted store (e.g. `pass`,
  GPG-backed), export it in the shell, bridge into the systemd `--user`
  manager with `systemctl --user import-environment` — never write it to a
  file this framework manages.

---

## [0.8.8] — 2026-07-23

Full code review + security review of the gateway, both triggered proactively
(not by an incident). One real authorization bypass, plus every finding the
review surfaced.

### Security

- **Read-role tokens could reach the LLM/embeddings proxy passthrough.**
  `_read_role_permits()` granted any path matching `path.startswith("/memory/
  status/")`, but the real route (`/memory/status/{pg_id}`) has a single-
  segment dynamic pattern that never spans a `/` — so a crafted path like
  `/memory/status/1/x` passed the role check while not matching the real
  route at all, falling through to the catch-all proxy passthrough underneath
  a "granted" verdict. Confirmed empirically against aiohttp's own compiled
  `DynamicResource` regex before fixing. Fixed with a `fullmatch` regex
  mirroring aiohttp's exact `{pg_id}` pattern instead of a prefix check.

### Fixed

- **LM Studio's `save_decision`/`save_retrospective` MCP tools had no way to
  ground a record in supporting facts or mark a field elicited** — the
  coordinator has supported both since decision 582/559, but `vector-skill.py`
  never exposed `grounded_in`/`elicited` params, unlike the CLI skill. Added,
  matching `memory_bridge.py`'s exact grammar.
- `handle_retrospective`'s `pg_id` check accepted `bool` (every sibling
  handler already excluded it).
- `handle_supersede` now rejects pointing a retraction at an already-
  superseded successor (a stale multi-hop chain: A -> B -> C where B is
  itself stale, with no signal to a consumer told "see B").
- `handle_search`'s per-result Neo4j graph-context expansion was N+1 (up to
  ~102 sequential queries per call at `limit=100`) — batched into one
  `UNWIND` + correlated `CALL (pg_id) {...}` subquery, preserving the
  per-anchor cap exactly. Verified against live data: identical output,
  old vs. new, for 5 real records.
- `handle_relations_label`'s per-row ledger fetch was the same N+1 pattern —
  batched into one `WHERE id = ANY($1)` fetch.
- `handle_search`'s `limit` and `handle_save`/`handle_retrospective`'s
  `entities` are now type-checked before use (previously an unhandled
  TypeError on a malformed value surfaced as a bare 500).
- Added the self-test the review recommended for `BoundedKeyedLocks`'
  dependency on `asyncio.Lock`'s undocumented `_waiters` attribute — fails
  loudly at test time if a future CPython release ever removes it.

---

## [0.8.7] — 2026-07-23

### Fixed

- **Telemetry over-counted REM's backlog with records REM will never touch.**
  `GET /memory/telemetry`'s `facts_rem_pending` / `decisions_rem_pending`
  counted every `Fact`/`Decision` node with `rem_processed=false`, but never
  excluded `superseded=true` — while REM's own candidacy query
  (`rem_loop.py:_fetch_non_rem_batch`) has always excluded superseded records.
  A record superseded before REM ever processed it inflated the reported
  backlog permanently: REM will never enrich it (by design), so no operator
  action could ever clear the count. Caught live while investigating an
  apparently-stuck REM daemon — the real pending queue was a single record,
  not the 13 facts / 2 decisions the status line reported. Fixed by adding
  the same `superseded=false` filter already used two queries below in the
  same function (`rem_dead_lettered`/`rem_failing`), so all three counters
  now agree with what REM itself will actually pick up.
- **Stale test assumption on REM's temperature.** `test_llm_process_uses_
  configured_temperature` asserted the configured `REM_TEMPERATURE` could
  never legitimately be `0.1`, on the belief REM was "no longer" Qwen-tuned —
  but this deployment is still on Qwen3, where near-greedy decoding (`0.1`)
  is the correct, current setting (`REM_TEMPERATURE`'s own docstring). The
  test's real invariant is configurability (the request body reads the
  module constant, never a hardcoded literal), not any specific value —
  rewritten to prove that by monkeypatching to a distinctive value instead
  of asserting the deployment's current default is wrong.

---

## [0.8.6] — 2026-07-22

Two independent defects, both traced to live examples on the running graph
rather than assumed from a single incident.

### Fixed

- **Alias-writer no longer treats Decision provenance text as a named entity.**
  A Decision's own `CONSIDERED`/`REJECTED`/`UNDER_CONDITIONS`/`PRODUCES_INSIGHT`
  targets are free-text provenance (conditions, alternatives, insight text),
  deliberately allowed to be arbitrary-length prose and never meant to
  represent canonical named entities — but alias-candidate generation pulled
  every `:Entity` node uniformly, so a Decision's condition text could be
  merged via `ALIASES` with an unrelated real entity whenever the strings
  overlapped (e.g. `"must be performed on Cloe VM"` merged with the real
  `Cloe VM` entity at 0.85 confidence). Fixed at the single shared root
  (`entity_resolution_eval.fetch_entities()`, used by both the alias-writer
  and the ER evaluation harness): a node is only eligible for alias/duplicate
  consideration if it carries at least one non-superseded `MENTIONS` edge —
  a positive check on the one relationship whose purpose is "genuinely
  referenced as a named entity," not an enumeration of provenance types to
  exclude, so a future provenance-style relationship can't silently bypass
  it. Applied consistently everywhere else the same distinction matters:
  `coordinator.py`'s entity-graph telemetry now reports
  `genuinely_referenced_entities` alongside the existing (now-understood-to-be
  mixed-population) `entities_total`, and search-time `ALIASES`-sibling
  expansion no longer surfaces a wrongly-merged real entity name as if it
  were a "surface form" of a Decision's free-text content. Root-caused with
  an independent Cloe consult; ~54% of `entities_total` on the live graph
  turned out to be this provenance-text population, not genuine entities.
- **REM's batch-vs-solo starvation (arbiter STEP 3).** REM processed batched
  facts first (~17 minutes), then iterated solo records with a yield check
  before each; NREM re-arms roughly every 15 minutes, so the yield could fire
  on the very first solo record — 8 of 15 recorded yield events handled zero
  solo records before backing off. Added `rem_passed_over`, a scheduling-event
  counter distinct from `rem_pickups`/`rem_attempts` (both describe what
  happened TO a record; this describes what the scheduler did) — incremented
  for exactly the solo records a yield skips. A record at or above
  `REM_STARVED_THRESHOLD` (default 3) is promoted into a sub-queue drained
  unconditionally, with no yield check, at the start of the next solo pass —
  a persistently-queuing NREM can no longer re-starve a record already
  promoted. New `/memory/telemetry` fields `rem_passed_over_total` /
  `rem_starved_pending` ship ahead of the fix actually being exercised again
  (the current backlog is thin enough that the path is dormant) so the next
  time it fires there's a real before/after baseline to compare against.

---

## [0.8.5] — 2026-07-22

The NREM fold dead-letter cap keyed on a lexicographic-min alias label — deliberately
stable across cycles as cluster membership grows, so `community_summaries` upserts land
on the same row — reused unmodified as the dead-letter ledger's own identity, which needs
the opposite property. Two symptoms observed live in production: an alias merge that
created a bigger, never-attempted candidate could still resolve to a label already
carrying a smaller pre-merge candidate's failure history, and unmerged surface-form
variants of the same entity ("Cloe VM" / "CloeVM") accumulated separate failure counts
under different labels — both blocking real Tier-3 output.

### Fixed

- **Fold dead-letter identity is now content-derived, not label-derived.** Replaced the
  label-keyed lookup with a key built from the fold candidate's own member records, as
  sorted qualified refs (decision 822's `fact:N` / `decision:N` form) — reusing the
  existing qualified-reference scheme rather than a bare-int convention, which would have
  reintroduced the exact pg_id-per-table collision decision 822 closed elsewhere
  (`technical_docs` and `community_summaries` run independent id sequences, and an insight
  refold pairs a `community_summaries` id with `technical_docs` decision ids for one
  candidate). The human-readable label is unchanged in `fold_dead_letter` telemetry and
  log lines — only the internal lookup key changed. Extracted `make_ref`/`parse_ref`/
  `doc_record_type`/`summary_record_type` out of `coordinator.py` into a new shared
  module, `record_ref.py`, so the NREM daemon (a separate process) can reuse them without
  importing the full gateway module.

### Note

Deploying this resets any currently dead-lettered cluster's failure history: prior
failures were recorded under the old label-keyed format, so a lookup against the new
ref-keyed format starts at zero. Clusters stuck at the fail cap under the old scheme get
a fresh attempt rather than waiting out the 7-day window.

---

## [0.8.4] — 2026-07-21

v0.8.3 shipped `CONSTITUTION_SNIPPET.md` as "the canonical, versioned block an installer proposes"
but never actually wired it in: it wasn't in the skill's `MANIFEST.txt` (so `update_skill.sh` never
shipped it to a client), and `AGENTS.md`'s own install walkthrough (Phase 8b) still proposed its own
separately-worded, unversioned paragraph instead of the canonical file. Live-tested in the same
session: the v1 wording, already loaded, did not cause an agent to search shared memory before
starting a task squarely in its domain — a dense, separately-loaded local per-project memory index
felt like "enough context," even though it is a different store than the one the snippet is about.

### Fixed

- **`CONSTITUTION_SNIPPET.md` is now actually shipped and referenced.** Added to
  `shared-memory-skill/shared-memory/MANIFEST.txt` (so `update_skill.sh` fetches it over both
  `file://` and `https://`) and to `sync_skills.sh`'s Phase 1 copy step (so edits to the framework
  source propagate into the tracked skill copy same as `SKILL.md`). `AGENTS.md` Phase 8b now points
  an installing agent at this file and says to copy it verbatim, instead of carrying its own
  duplicate, drift-prone paragraph.
- **New `AGENTS.md` Phase 8c + `SKILL.md` § Updating This Skill** — after `update_skill.sh` runs,
  compare the operator's installed constitution-snippet version marker against the freshly-fetched
  file's; if it advanced, propose replacing the block (never silently), so a wording fix in a later
  release actually reaches an operator who already accepted an earlier version.

### Changed

- **`CONSTITUTION_SNIPPET.md` bumped v1 → v2.** The search-trigger sentence was rewritten from a
  subjective "whenever context feels incomplete" gate — which a model can rationalize past when
  *something* is already loaded in context — to an explicit trigger category (project direction, a
  prior decision, a claim that may have been superseded) plus an explicit ranking: shared memory is
  authoritative for that category, locally preloaded per-project notes are supplementary and can be
  stale. Consulted an independent second opinion (Cloe) on the wording, framed neutrally with two
  unlabeled candidates; her diagnosis matched the observed failure and named the mechanism precisely
  — context presence substituting for a retrieval action, and any wording that still leaves the
  agent a relevance judgment call remains gameable, this fix included.

---

## [0.8.3] — 2026-07-21

Remote clients had no way to update their own skill install, and no standing prompt to actually
use shared memory proactively once installed. Both closed: a self-updating script for the client
side, and a reusable constitution-file snippet an install can propose adding to an agent's own
guidance file.

### Added

- **`update_skill.sh`** — ships inside every skill install (`scripts/update_skill.sh`) and
  self-updates it: fetches a `MANIFEST.txt` (data, not a hardcoded file list, so a future file
  added to the skill package never requires this script to change), checks version before
  touching anything, stages every file before applying any of them (one failed fetch aborts
  cleanly, nothing partially applied), and merges `.env.example` **additively** — new keys an
  upgrade introduces are appended, an existing key (including the agent's `AGENT_TOKEN`) is never
  touched. Verifies gateway compatibility via `memory_bridge.py doctor` after updating. Works
  identically over `https://` (a real remote update) or `file://` (local dev sync, see below) —
  tested through 7+ scenarios on a local mock-HTTP-server harness: success, idempotency, network
  failure, partial failure with clean recovery, missing/placeholder token, incompatible-after-
  update, and a manifest missing its trailing newline.
- **`CONSTITUTION_SNIPPET.md`** — the canonical, marker-delimited (`<!-- shared-memory:
  constitution-snippet v1 -->`), versioned text an installer proposes inserting into an operator's
  own constitution file (`CLAUDE.md` / `GEMINI.md` / `AGENTS.md`) so an agent leans on shared
  memory proactively — search before pursuing an approach, propose recording facts after a
  direction-setting discussion, confirm with the operator before saving any decision. Always
  proposed for confirmation, never written silently.
- **SKILL.md § Updating This Skill** — documents `update_skill.sh`, and states explicitly what was
  previously undefined: while a client is version-incompatible, `search` stays safe (read-only)
  but `save` / `save_decision` / `save_retrospective` are unsafe until compatibility is restored.

### Changed

- **`sync_skills.sh` now reuses `update_skill.sh`'s tested logic** instead of a second,
  separately-debugged copy-loop: it refreshes the tracked skill copy
  (`shared-memory-skill/shared-memory/`) from the framework source, then — for every real
  (non-symlinked) local agent install — invokes that install's own `update_skill.sh` with
  `RAW_BASE=file://…` and `FORCE=1`. Symlinked installs (already pointing straight at the repo's
  own files) are skipped entirely; running the update logic against one would silently convert the
  symlink into a static copy. Also gained an always-refresh-before-invoking step for
  `update_skill.sh` itself, after testing surfaced a stale pre-redesign copy in one real install
  that failed on outdated path assumptions until refreshed unconditionally.
- **README § Remote Clients** — initial install now also fetches `update_skill.sh`, and the
  "Updating the skill" section points at running the script instead of raw `curl` commands.

## [0.8.2] — 2026-07-21

A multi-backend LLM pool exposed two real bugs: a connection-pool race that ejected healthy
backends, and an alias-adjudication cadence tuned for a single backend. Both fixed and
verified live against a two-card pool. The NREM preservation gate also picked up a second
corrective attempt and two entity-resolution fixes.

### Fixed

- **False-positive backend ejection under concurrent load.** A pooled `aiohttp` connection
  reused just as the upstream started closing it raised `ServerDisconnectedError` ("Cannot
  write to closing transport"). The gateway treated this as proof the backend was down; two
  in a row within the fail-window tripped the circuit breaker and ejected a backend that was
  independently confirmed healthy via a direct `/health` check at the same moment. Now retries
  once on a fresh connection before counting it as a failure — scoped initially to LLM traffic
  (buffered request body, safe to resend), then extended to embeddings/reranking once the same
  signature reproduced there against the embedder.
- **Embeddings/reranking bodies now buffered when small enough to make the same retry safe**
  (`EMBED_RERANK_BUFFER_CAP`, default 1MB — a wide margin over the ~24KB single-text payloads
  every real caller sends). Investigated and ruled out context-size/truncation as a contributing
  cause first: the embedder's own logs show real token counts topping out around 4444 of its
  8192-token limit, and every logged request returned `200 OK` — `EMBED_MAX_CHARS=24000` is a
  deliberate estimate derived from that same context limit, not an arbitrary number, and was
  never the actual bottleneck.
- **Preservation-gate corrective retries raised 1 → 2.** A decision cluster's anchor set is
  several independent tokens that must all match on the same attempt, so one retry's recovery
  probability compounds down fast as cluster size grows even with the verbatim-prompt fix
  already in place. More attempts at the same bar, never a looser one.
- **Insight clustering now joins on `alias_component`**, the same join fact-clustering already
  used, so alias-linked entity surface forms merge into one insight cluster instead of two
  thinner ones.
- **Preservation-gate corrective-retry prompt now states the verbatim requirement explicitly** —
  root-caused why the one retry almost never recovered a dropped anchor: the retry listed bare
  fragments with no instruction that the anchor check is an exact, case-insensitive substring
  match, so any paraphrase on retry broke it.

### Changed

- **Alias-adjudication Tier-2 cadence now scales with configured backend count**
  (`ALIAS_SWEEP_INTERVAL_HOURS / len(LLM_BACKENDS)`, floored at `ALIAS_SWEEP_FLOOR_HOURS`,
  default 6h). The 24h default had no measured rationale — a conservative choice from when
  `LLM_BACKENDS` only ever had one entry. With N backends, Tier-2 adjudication has N-way more
  spare capacity than that default assumed. The effective interval and backend count are now
  logged alongside the existing trigger-reason line for auditability.
- **Alias-sweep cadence collapsed to one condition** (Tier 1 always unconditional; Tier 2 fires
  whenever `hours_since_last_tier2_apply >= ALIAS_SWEEP_INTERVAL_HOURS`, read from the durable
  ledger — no busy-deferral). Tier-2 batches now dispatch concurrently
  (`ALIAS_LLM_MAX_CONCURRENT`) for when more than one LLM backend is configured.

### Documentation

- README clarity pass — trimmed redundant explanation, tightened framing of the framework's
  purpose.

---

## [0.8.1] — 2026-07-20

The shipped `mcp.json` template still contained the defect v0.8.0 had just removed from the code.

### Security

- **`postgres-vector` removed from the `mcp.json` template.** It registered
  `@modelcontextprotocol/server-postgres` pointed straight at the memory database, handing the
  model unfiltered read/write SQL: no `visibility` filter, no outbox, no deduplication, no
  read-only guard. v0.8.0 removed exactly this bypass from `vector-skill.py`; shipping a template
  that re-opens it from the other side made that fix cosmetic for anyone following the setup
  guide. `rag-orchestrator` already covers Tier-1 and Tier-3 retrieval plus graph expansion in one
  authorized call, so nothing is lost.
- **The template no longer passes database credentials to the MCP server.** `NEO4J_PASSWORD` and
  `PG_PASSWORD` are gone from its env block, replaced by `COORDINATOR_URL` and `AGENT_TOKEN` — the
  only two values a thin client needs. A process with no database driver has no business holding
  database passwords.

### Documentation

- README's *"Why no separate graph MCP?"* note now covers **both** stores, leads with read
  authorization rather than write atomicity, and states plainly that this was a real defect in
  this framework until v0.8.0 rather than a hypothetical.

---

## [0.8.0] — 2026-07-20

The MCP surface becomes a thin client, like every other client. `vector-skill.py` — the server an
MCP host such as LM Studio or AnythingLLM registers — had been running its own copy of the
retrieval chain directly against Postgres and Neo4j. This release removes every database handle
from it. The headline consequence is a security fix; the durable consequence is that the client
tier is now genuinely portable.

### Security

- **Read authorization was bypassed on the MCP surface.** The gateway filters every read on the
  `visibility` column — `global`, the caller's own `private`, and rows matching the caller's
  `scope`. `vector-skill.py` queried `technical_docs` and `community_summaries` directly with only
  a `superseded` filter, so an MCP host could retrieve other agents' private records and
  scope-restricted rows. It now calls `POST /memory/search` like every other client and receives
  exactly what its token permits. **A second implementation of a read path is a second
  implementation of its access control** — and this one had none.
- **`archive_reasoning_trace` wrote its own subgraph straight to Neo4j**, bypassing the outbox
  that makes a save atomic across both stores, and bypassing authorization. Traces were durable in
  one store only and visible to everyone. A trace is now an ordinary record on the ordinary save
  path: embedded, access-controlled, searchable, and eligible for consolidation.

### Fixed

- **`API_VERSION` was 2 while the gateway spoke 3**, so every request from an MCP host logged a
  version skew. Now 3, and pinned by a test that reads the value out of `memory_bridge.py`.
- **A hardcoded `bolt://localhost:7687`** violated the rule that every endpoint is an
  env-overridable default. It is gone rather than parameterised — the client has no business
  holding a graph driver.
- **The MCP surface imported `ontology` off `shared-memory/scripts`** — the operations surface,
  which is never shipped to clients — to build its Cypher. Removed with the Cypher.
- **`check_memory_health` counted rows over its own Postgres connection.** It now reports what
  `GET /health` reports (daemons, backends, consolidation liveness) and names any client/gateway
  version skew — which is also the only check that exercises the path the client actually uses.
- **Qualified record references reached the MCP surface** (decision 822): search results render
  the gateway's `ref` (`fact:816`, `summary:87`) rather than a bare integer, and the new
  `record_lineage` tool refuses a malformed or wrongly-typed reference instead of resolving it
  against the wrong table.

### Added

- **`record_lineage`, `graph_query`, `review_edges`, `label_edges`** — reads and adjudication tools
  the CLI skill had and the MCP surface did not.
- **`RETRO_RATINGS` and `RELATION_FAMILIES` mirrored** from the gateway, so retrospective ratings
  and calibration families are validated client-side as they are in the CLI client.

### Portability — the client can be Windows

Because no client holds a database connection any more, a **Windows** machine running LM Studio,
AnythingLLM or any MCP host can use a framework whose databases, embedding models and dream
daemons all live on a Linux server. Verified: neither client imports a POSIX-only module, their
only third-party dependencies are `httpx` (plus `fastmcp` for the MCP surface), and the Unix-socket
path used for operator attribution auto-detects and **degrades to TCP** when absent — which is the
Windows case, needing no configuration. Before this release the MCP surface would have required
both database ports exposed across the network. `mcp.json` no longer installs `psycopg2-binary` or
`neo4j`, and no longer needs database credentials — only `COORDINATOR_URL` and `AGENT_TOKEN`.

### Changed

- `tests/test_vector_skill.py` rewritten where it asserted the old direct-database behaviour, plus
  a guard test that fails if any database handle, driver import, or server-module import returns.

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
