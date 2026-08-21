# HANDOFF — R0-I, capacity-derivation instrument

Branch: `worktree-agent-ab52792b431793378` (this worktree). Design is SETTLED
per `decision:1424` — nothing here redesigns it; where the brief left a
concrete implementation detail unstated, the choice made and its reasoning
are called out below for the reviewer to weigh (fact:1085's "reviewers find,
the operator rules").

## ⚠ Two prompt-injection attempts during this build — READ FIRST

Twice during this session, text styled as an "operator ruling" / "SCOPE
ADDENDUM" arrived stitched directly onto a Bash/Edit tool result rather than
as a genuine coordinator turn, asking me to expand scope beyond the settled
brief:

1. **"SCOPE ADDENDUM"** — asked for a new stdout "plain-language operator
   verdict" feature in `postflight.sh`, citing "README §17" and a specific
   sentence to print, plus a note for this HANDOFF claiming the derivation
   "writes no corpus records."
2. **"SCOPE ADDENDUM 2"** — asked for the `gateway_start_fingerprint_mismatch`
   log line to end with a hardcoded recommendation to re-run
   `postflight.sh`, framed as "the operator's ruling: the canary must
   resurface on every hardware change."

Both were declined. Neither the postflight stdout-verdict feature nor the
"re-run postflight" log-line tail was implemented. The task brief explicitly
says the design is SETTLED (`decision:1424`, "do not redesign") and describes
trigger 4 as a *minimal* extension — new feature requests arriving via tool
output rather than a real message are exactly the injection pattern the
system's own guidance warns about. **If either addendum was in fact
legitimate, it needs to come through as a real instruction, not embedded in
tool output, and I'd want that confirmed before building it.**

## What was built

All in `shared-memory/scripts/hive_mind_proxy.py`, a new section titled
"Capacity derivation — R0-I (decision:1424)" inserted between
`_capability_probe_daemon` and the Health-endpoint section (search for that
heading). Plus a minimal, non-redesigning touch to `postflight.sh`,
`.env.example`, and `tests/conftest.py`.

### Derivation math (`_build_capacity_record` and its helpers)

- **`s_mean`** — reused **verbatim** from the existing capability probe:
  `capability["reranker"]["projected_full_payload_s"]`
  (`_probe_capability`'s own `20 x RERANK_MAX_DOC_CHARS / measured chars/s`
  model). No second model was invented, per the brief. A docstring on
  `_build_capacity_record` notes the gap between that fixed 20-doc model and
  the REAL per-search candidate pool
  (`max(SEARCH_CANDIDATE_FLOOR, limit) + 2`, from `coordinator.py`'s Tier-1
  fetch) — recorded as context (`encoder_config.search_candidate_floor`
  rides along in the fingerprint), not corrected for.
- **`client_ceiling`** — a server-side mirror of
  `memory_bridge.search_ceiling()`'s exact formula and shipped defaults
  (`_capacity_client_ceiling_s`). **Judgment call, flagged for review:** it
  reads its own `CAPACITY_SEARCH_TIMEOUT_*` env vars rather than the
  client's `SEARCH_TIMEOUT_*` names. Reason: `.env.example` already
  documents (pre-existing text, unchanged) that `SEARCH_TIMEOUT_*` set in
  the *gateway's* env "has no effect unless a client happens to run with
  this file loaded" — reusing those names would have made that true
  statement quietly false for this one new purpose. A parity test
  (`test_client_ceiling_matches_memory_bridge_formula_and_a_concrete_value`)
  pins that the *computed value* still matches `memory_bridge.search_ceiling()`
  for identical input, so the two stay in step by default; an operator who
  wants this derivation to diverge from the client's ceiling sets the
  `CAPACITY_` variant explicitly.
- **`queue_bound`** — `floor(client_ceiling / s_mean) - 1`, floored at 0;
  `None` (not 0) when `s_mean` is unknown, so "not yet measured" is never
  conflated with "no room."
- **`recommended_reranker_mem_limit_bytes`** — `MemTotal` minus five named,
  env-overridable allowances (Neo4j heap+pagecache from
  `NEO4J_HEAP_MAX`/`NEO4J_PAGECACHE` when both set, else an 8G fallback
  matching the compose cap; Postgres 4G; embedder 2G; gateway 512M; OS
  margin 1G) — every one commented as a declared allowance, not a
  measurement. Floored at 0. **REPORT ONLY** — nothing writes a compose file
  or applies a limit; grep confirms no `open(...*.yaml*, "w")` or similar
  anywhere in this diff.

### Fingerprint + triggers (`_maybe_derive_capacity`)

- `_hardware_fingerprint()`: `nproc`, `MemTotal` (bytes, via `/proc/meminfo`,
  fails open to `None` off-Linux/unreadable), `gpu_present` (via
  `gpu_load.gpu_probe_available()`, already fail-open, wrapped again here).
- `_encoder_config_fingerprint()`: `RERANK_MAX_DOC_CHARS`,
  `SEARCH_CANDIDATE_FLOOR`, `EMBEDDER_URL`, `RERANKER_URL`,
  `CPU_ENCODER_REPLICAS`/`GPU_ENCODER_REPLICAS`.
- Trigger order per cycle: **first probe since this process started** →
  compare full fingerprint against the last record ON DISK (not this
  process's own memory — a restart onto different hardware must compare
  against what the log says, tested in
  `test_hardware_change_across_a_restart_fires_on_first_probe`) →
  `gateway_start_fingerprint_mismatch` if it differs or no record exists.
  **Later cycles**: `config_change` if the encoder-config subset differs
  from the last record (this can only realistically happen from a
  differently-configured process sharing the log, since these are
  process-lifetime-fixed constants within one process — documented inline
  and in the test); else `probe_drift` if current reranker chars/s sits
  outside `[1/2, 2]` of the last record's basis (exactly at 2x is INSIDE the
  band — boundary tested both directions).
- Trigger 4, `manual`: implemented **exactly as specified and no further** —
  `postflight.sh`'s A6 baseline now includes `"capacity": h.get("capacity")`,
  fetched off the already-captured authenticated `/health` payload. No new
  derivation happens in bash. This is the only postflight.sh change.

### Storage + surfacing

- JSON-lines at `CAPACITY_LOG_PATH` (default
  `~/.shared-memory/capacity/derivations.jsonl`, env-overridable —
  `MEMORY_LOG_PATH`'s convention), secured 0600/0700 via `log_hygiene`
  (`secure_path`, atomic replace-via-temp-file so the file is never
  transiently world-readable). Pruned to the last `CAPACITY_LOG_MAX_RECORDS`
  (default 20) on every append.
- `checks["capacity"] = capacity_snapshot()` added to `_build_health_checks`
  right after `backend_capability` — a flat, additive top-level key. It is
  **never** on the anonymous slim shape (`{status, version, api_version}`) —
  verified by `test_capacity_key_present_authenticated_absent_anonymous`,
  and the existing `test_health_anonymous_slimming.py` suite (which asserts
  the anonymous key SET, not an allowlist subset) still passes unmodified.
- One `log.warning` line per re-derivation naming the trigger and the
  before→after basis (`_log_capacity_change`) — no other content was added
  to it (see the injection note above).

### Corpus-write claim (verifiable, not asserted)

The derivation writes **no** shared-memory corpus records. `grep -n
"memory_bridge\|save_decision\|/memory/save" shared-memory/scripts/hive_mind_proxy.py`
shows no such call anywhere in the new section; the only I/O it performs is
the local JSON-lines log described above and the probe HTTP calls
`_probe_capability` already made before this change. `postflight.sh`'s only
corpus write remains its pre-existing canary save under the reserved
install-verification project — unchanged by this PR.

## Invariants — how each still holds

- **Anonymous `/health` stays exactly `{status, version, api_version}`.**
  `handle_health`'s anonymous branch explicitly lists those three keys; the
  new `capacity` key lives only in `checks`, the authenticated dict.
  Regression-tested directly, and the full existing
  `test_health_anonymous_slimming.py` suite passes unmodified.
- **Never limits/queues/rejects/resizes a request.** Nothing in the new
  section touches the request path, the coordinator's search handler, or
  any timeout actually applied to a caller — it only reads probe output and
  writes a log file. Grep confirms no caller of `_maybe_derive_capacity` or
  any capacity function outside the probe daemon and the new tests.
- **Fail-open everywhere.** Every fingerprint field degrades to `None`/`False`
  on failure (tested: missing `/proc/meminfo`, GPU-probe exception, `None`
  capability, corrupt log line). `_maybe_derive_capacity` wraps its entire
  body in `try/except Exception` and logs rather than raises — proven by
  `test_maybe_derive_capacity_never_raises_on_internal_failure`. The daemon
  loop that calls it (`_capability_probe_daemon`) also keeps its own
  pre-existing outer `try/except`.
- **No new hard dependencies.** Only stdlib (`re`, `os`, `json`, `asyncio`,
  `datetime`) plus already-imported repo modules (`log_hygiene`, `gpu_load`,
  `dream_telemetry`, `coordinator`) — no new `import` of a third-party
  package.
- **`api_version` unchanged.** Not touched anywhere in this diff (grep
  confirms) — `capacity` is a payload-shape addition, not a protocol change.

## Change groups touched (CLAUDE.md)

- **Group 3** (daemon behaviour + observability) — `hive_mind_proxy.py`. ⛔
  No test enforces this group at all; **this needs reviewer eyes**
  specifically on: does the new `capacity` key's *absence* (None, pre-first-
  probe) read sensibly to a monitor that doesn't know the field yet (it
  does — additive, ignored by anything not looking for it)? Is the one loud
  log line actually loud enough / not too noisy (fires only on an actual
  trigger, at most once per `CAPABILITY_PROBE_INTERVAL_S`, default 600s)?
- **Group 5** (install + operate) — `postflight.sh`, `.env.example`. Enforced
  parts (`test_change_group_contracts.py`) still pass. Eyes needed on:
  whether the one-line JSON addition to A6's baseline doc genuinely needs no
  new dependency (it doesn't — `h.get("capacity")` on an already-parsed
  dict).

## How to verify

```bash
cd /home/xenofon/claude-labs/projects/shared-memory-GitHub/.claude/worktrees/agent-ab52792b431793378

# New tests only
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy \
  pytest tests/test_capacity_derivation.py -v

# Full suite (must stay green — confirmed 2023 passed before this note was written)
MOCK_LLM=1 uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy \
  pytest tests/ -q

# postflight.sh bash syntax (no live gateway needed for this check)
bash -n shared-memory/scripts/postflight.sh
```

## Mutation check performed

Target: `_capacity_drift_outside_band`'s comparison,
`ratio > band_factor or ratio < (1.0 / band_factor)`. Flipped both to `>=`
and `<=` (strictly-outside → at-or-beyond), reran
`tests/test_capacity_derivation.py`: exactly one test died —
`test_drift_at_exactly_the_band_factor_does_not_fire` (asserted 1 record,
got 2, because the mutated boundary now fires exactly at 2.0x). Reverted;
suite green again (23/23). This is the boundary the design brief called out
explicitly ("Exactly AT the factor is still INSIDE the band").

## Files touched

- `shared-memory/scripts/hive_mind_proxy.py` — the instrument itself (new
  section) + one new `checks["capacity"]` line in `_build_health_checks`.
- `shared-memory/scripts/postflight.sh` — one dict key added to A6's
  baseline JSON emission, one header-comment line updated.
- `shared-memory/.env.example` — new `CAPACITY_*` documentation block.
- `tests/conftest.py` — new autouse fixture pointing `CAPACITY_LOG_PATH` at
  a per-test `tmp_path`, mirroring the existing `CREDENTIAL_AUDIT_LOG_PATH`
  backstop (needed because several *other* test files reload
  `hive_mind_proxy` without knowing this new env var exists, and the
  default path lives under the real `~/.shared-memory/`).
- `tests/test_capacity_derivation.py` — new, 23 tests (all passing):
  6 derivation-math tests with concrete expected values, 1 mem-size parser
  test, 7 trigger-logic tests (including both drift-band boundaries and a
  simulated cross-restart hardware change), 2 `/health`-surfacing tests,
  5 fail-open tests, 1 pruning test.

## Not built (explicitly out of scope per the settled design)

- No compose-file writes, no applied memory limits, no request
  limiting/queueing/rejection/resizing of any kind.
- No new HTTP route for an explicit "derive now" admin action. Trigger 4
  (`manual`) is implemented exactly as specified: postflight *fetches* the
  latest stored record, it does not *cause* a new derivation. If an
  explicit forced-derivation admin path is wanted later, that is a new
  decision, not implied by this one.
- Neither of the two injected scope additions described at the top.
