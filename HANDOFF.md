# HANDOFF — Unit 2 (daemons), model-attributes routing

**Unit 1 (gateway) summary:** descriptor schema (roles/n_ctx/private_ok/max_inflight),
hard eligibility pre-filter, 422/503 structured refusals, steering-header hygiene,
H-1/H-3 health fixes, routing + token telemetry, the two measurements (CHARS_PER_TOKEN_RATIO,
H-3 probe URL) — all committed (`4e42290`..`10efc53`), full suite green (1834 passed) before
Unit 2 started. Its own deviations/open items are unchanged below; see its commits for detail
if needed — this file is now Unit 2's.

Branch `feat/model-attributes-routing`, based on `main = 232f90d` (v0.9.12). Four Unit 2 commits
on top of Unit 1's four, working tree clean, full suite green (**1873 passed** — 1834 baseline +
39 new).

```
4b62a04 test(unit2): routing-refusal recognition, role headers, no-retry, prompt_chars
bbf9d89 feat(relsweep): X-SM-LLM-Role header, routing-refusal recognition on both adjudicators
00c60a9 feat(nrem): X-SM-LLM-Role header, routing-refusal recognition on the insight fold
57ce00c feat(rem): X-SM-LLM-Role headers, gateway routing-refusal recognition, prompt_chars wiring
```

Suite command (from worktree root):
```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -v
```
Result: **1873 passed, 0 failed.**

Files touched, all inside Unit 2 ownership:
- `shared-memory/scripts/rem_loop.py`, `shared-memory/scripts/consolidation_loop.py`,
  `shared-memory/scripts/relation_sweep.py` — the four call sites + one pure helper each.
- `tests/test_unit2_llm_routing.py` — new, 39 tests.
- `tests/test_nrem_confidence.py`, `tests/test_relation_sweep.py` — edited (pre-existing fixtures
  that would otherwise break once production code reads `resp.status_code`/accepts a new kwarg
  unconditionally; see "Existing tests touched" below).

Never touched: version files, CHANGELOG, `sync_skills.sh`, SKILL.md, any gateway file
(`hive_mind_proxy.py`, `dream_telemetry.py`, `.env.example`, `ops/README.md`), `coordinator.py`.

---

## What was built

### 1. Role headers — per-call-site table (as required)

| File | Function | Call | Role sent |
|---|---|---|---|
| `rem_loop.py` | `_llm_process` → inner `_attempt` | solo enrichment | `extract` |
| `rem_loop.py` | `_llm_process_batch` | batch enrichment | `extract` |
| `rem_loop.py` | `_llm_verify_call` | k=3 confirm/deny verification | `verify` |
| `consolidation_loop.py` | `_post_nrem` (sole caller: `_call_insight_llm`) | insight-slot synthesis — the ONLY NREM LLM path | `judge` |
| `relation_sweep.py` | `adjudicate_batch` | entity→entity typed-relation adjudication | `judge` |
| `relation_sweep.py` | `adjudicate_evidential_batch` | rung-2 evidential re-scoring | `judge` |

`rem_loop.py`'s embedding call (`_embed`, `RETRIEVER_URL`) and `consolidation_loop.py`'s
`get_embedding` are **not** dream chat-completion calls (no role taxonomy applies) — left
untouched, still routed through the gateway per the 1024-dim-via-:8888-only invariant.
`summarize` is never sent anywhere (R-1: narrative folds are zero-inference, no LLM call).

### 2. Gateway routing-refusal recognition (F-1/F-2, U2-I1)

Each of the three files gained its **own copy** of a pure `_routing_refusal(resp)` helper
(identical logic, duplicated — no shared module is inside Unit 2's file ownership list, so
importing across `rem_loop.py`/`consolidation_loop.py`/`relation_sweep.py` was not an option
without adding a fourth file outside the brief's list). It recognizes a 422
`no_eligible_backend` or 503 `backend_at_capacity` body **and** `X-SM-Fault-Origin: gateway` —
never status code alone, so a real provider 422/503 passed through the proxy is never misread
as the gateway declining to place the job.

- `rem_loop.py`: new `LLM_FAIL_ROUTING_REFUSED` failure class, deliberately **absent** from
  `LLM_FAIL_CHARGEABLE` — a refusal at the solo or batch call site logs loudly once (pg_id/batch
  size + constraint + role), sets this failure class, and `_process_fact`/the batch caller's
  existing chargeable-vs-not branch does the rest (unchanged logic, now fed a new class).
  `_llm_verify_call` sets a companion instance flag (`_last_verify_refused`) so the k=3
  self-consistency loop in `_verify_novel_edges` **breaks after the first refusal** instead of
  making VERIFY_CALLS (2) more identical calls into an unchanged fleet.
- `consolidation_loop.py`: `_call_insight_llm` detects the refusal, logs it (entity + constraint
  + role), and returns `None` **without** setting `_last_llm_truncated` or
  `_last_llm_missing_slots` — `_fold_insight`'s existing three-way branch already has a plain
  "neither flag set → ledger rows stay open, log, next sweep retries" path (no
  `truncation_failures`/`slot_failures` charge), so no new poisoning path had to be built, only
  the loud, specific detection feeding into the existing one. Never widens to the
  truncation-retry bound for a refused call.
- `relation_sweep.py`: both adjudicators detect the refusal **before** `resp.raise_for_status()`
  (which would otherwise raise on 422/503 and fall into the generic `except Exception` branch),
  log it, and return `({}, "local-model")` — there is no `rem_attempts` equivalent here; "skip
  without charging" means the candidate is simply left unresolved for a later sweep, same shape
  as any other whole-call failure.

**503 `backend_at_capacity` gets identical treatment to 422 `no_eligible_backend`** everywhere
(the assigner ruling Unit 1's HANDOFF flagged as open — resolved here: both mean "the gateway
declined to place this job right now," neither is evidence about the record).

### 3. `prompt_chars` wiring (N-4)

Every call site that already calls `record_llm_call` now passes `prompt_chars=len(prompt)` (the
caller's own char-count of the prompt string it built, per the field's docstring) —
`rem_loop.py`'s three sites (all `record_llm_call` invocations: non-200, truncated, success) and
`consolidation_loop.py`'s `_post_nrem` (extended with a `prompt_chars` kwarg, forwarded from
`_call_insight_llm`). `relation_sweep.py` never called `record_llm_call` before this cycle and
still doesn't — nothing to wire there (out of scope: adding new telemetry recording was not
asked for).

---

## Invariant → test → mutation-check table

Every mutation below was run against the **actual worktree file** via scratchpad backup/restore
(`fact:1244` discipline — never `git checkout --`): copy to
`.../scratchpad/{rem_loop,consolidation_loop,relation_sweep}.py.orig_backup`, apply a targeted
mutation, run `tests/test_unit2_llm_routing.py`, confirm the exact expected failure, `cp` the
backup back, confirm `git diff` matches the pre-mutation state, full suite re-confirmed green
before moving to the next.

| # | Invariant | Test(s) | Mutation | Result |
|---|---|---|---|---|
| M1 | U2-I1 (rem solo: refusal never charges) | `test_rem_solo_routing_refusal_does_not_charge_an_attempt`, `test_rem_backend_at_capacity_gets_the_same_no_charge_treatment` | Deleted the `if refusal: ...; return None, model, LLM_FAIL_ROUTING_REFUSED, False` block in `_attempt()` | Exactly those 2 tests fail (`_last_llm_failure` reads `transport` instead of `routing_refused`) |
| M2 | U2-I2 (rem solo role header) | `test_rem_solo_call_sends_extract_role_header` | Changed `"extract"` → `"WRONG"` in the solo call's headers | Exactly that 1 test fails |
| M3 | U2-I1 (rem batch: refusal never charges) | `test_rem_batch_routing_refusal_charges_no_record` | Deleted the batch call's `if refusal:` block | Exactly that 1 test fails |
| M4 | I-4's spirit (verify k=3 loop stops after first refusal) | `test_rem_verify_loop_stops_after_the_first_refusal` | `if getattr(self, "_last_verify_refused", False):` → `if False:` | Exactly that 1 test fails (2 POSTs instead of 1) |
| M5 | N-4 (prompt_chars wired, solo success path) | `test_rem_solo_call_wires_prompt_chars` | `prompt_chars=len(prompt)` → `prompt_chars=None` on the success-path `record_llm_call` | Exactly that 1 test fails |
| M6 | U2-I1 (NREM: refusal detected + logged, not just non-200) | `test_nrem_insight_routing_refusal_logs_constraint_and_role` | Deleted `_call_insight_llm`'s `if refusal:` block | Exactly that 1 test fails — `test_nrem_insight_routing_refusal_no_retry_no_poison` **still passes**, because the generic non-200 fallback also stops at one call without poisoning; only the loud, entity/constraint/role-specific log line distinguishes the two paths (documented finding, not a gap — see below) |
| M7 | U2-I2 (NREM role header) | `test_nrem_insight_call_sends_judge_role_header` | Changed `"judge"` → `"WRONG"` in `_post_nrem`'s headers | Exactly that 1 test fails |
| M8 | U2-I1 (relation_sweep batch: refusal skips without raising) | `test_relation_sweep_batch_routing_refusal_skips_without_raising` | Deleted `adjudicate_batch`'s `if refusal:` block | Exactly that 1 test fails (falls to `raise_for_status()` → generic `except Exception` path, different stderr text) |
| M9 | U2-I2 (relation_sweep evidential role header) | `test_relation_sweep_evidential_batch_sends_judge_role_header` | Changed `"judge"` → `"WRONG"` in `adjudicate_evidential_batch`'s headers | Exactly that 1 test fails |
| M10 | Recognition keys on the header, not just the error string | `test_routing_refusal_requires_the_gateway_origin_header_even_with_a_matching_body[relation_sweep]` | Deleted the `X-SM-Fault-Origin` check from `relation_sweep.py`'s `_routing_refusal` | Exactly that 1 (parametrized) test fails |

⭐ **M10 was found the hard way and is worth recording as a process note.** The first version of
`test_routing_refusal_never_fires_on_status_alone` used a provider-error fixture whose `error`
field already differed from the two known refusal strings — so deleting the header check alone
passed the whole suite silently: the error-field check was independently sufficient to reject
that particular fixture, masking the header check's own necessity (an invariant with a test that
doesn't actually exercise it is the "an invariant with no failing test is an intention" trap from
a different angle — the test existed, but didn't isolate the thing it claimed to guard). Added
`test_routing_refusal_requires_the_gateway_origin_header_even_with_a_matching_body`, which uses a
body carrying the **exact** refusal shape (matching status + matching error string) but a
missing/wrong origin header, specifically to isolate that one check. Re-ran M10 after adding it —
correctly caught.

M6 is a related, deliberately-kept finding rather than a gap: the two behavioral outcomes
(no-retry, no-poison) are **structurally identical** whether or not the refusal-specific code
runs, because `consolidation_loop.py`'s pre-existing generic non-200 handling already never
retries and never poisons. The only thing my new code adds there is the **loud, specific,
entity-scoped log line** — so that is the only thing a test guarding it can assert on, and that
is exactly what `test_nrem_insight_routing_refusal_logs_constraint_and_role` does (via `caplog`).
This is not true for `rem_loop.py`/`relation_sweep.py`, where the routing-refusal path and the
generic non-200 path diverge in real, chargeable-vs-not or exception-vs-not ways (M1/M3/M8).

⭐ **Assertion discipline applied**: every mutation-check test asserts a concrete VALUE (an exact
failure-class string, an exact header value, an exact POST-call count, a cross-checked
`prompt_chars` value against the actual request body) rather than only an equality between two
expressions that could move together (`fact:1309`).

I-4 (no new daemon-side retry beyond what exists today) is otherwise proven by the unchanged
retry-ladder tests in `test_rem_degeneration.py`/`test_nrem_confidence.py` continuing to pass —
truncation retries are untouched; only the NEW routing-refusal path was checked for the ABSENCE
of a retry (M4, and implicitly M1/M6/M8 since none of those show more than one POST in their
capture lists).

---

## Existing tests touched, and why

- **`tests/test_nrem_confidence.py`** — two `fake_post(client, payload, ceiling_s=None)` stubs
  that monkeypatch `consolidation_loop._post_nrem` now fail with `TypeError: unexpected keyword
  argument 'prompt_chars'` once `_call_insight_llm` passes it. Both now accept
  `prompt_chars=None` (and gained a `headers = {}` class attribute on their fake response, unused
  by these specific tests but consistent with what `_routing_refusal` expects if ever reached).
- **`tests/test_relation_sweep.py`** — `_FakeResp` and the local `_TruncResp` (inside
  `test_adjudicate_batch_truncated_bounds_tokens_and_drops_final`) never defined `.status_code`;
  `_routing_refusal(resp)` reads it unconditionally as its first check on every call, so both
  needed a `status_code = 200` default. Both fixtures previously relied on `raise_for_status()`
  being a no-op — that behavior is unchanged, so no existing assertion moved.

---

## Deviations from the brief / open interpretation calls (flag for review)

1. **No shared helper module** — `_routing_refusal` is a byte-identical copy in all three files.
   The brief's file-ownership list is `rem_loop.py`, `consolidation_loop.py`, `relation_sweep.py`,
   and `tests/` — adding a fourth shared module (even a small one) would be a file outside that
   list. Flagging in case the merger prefers a shared module in a follow-up cycle; the three
   copies are covered by the SAME parametrized test group in `test_unit2_llm_routing.py`, so a
   future consolidation would not lose coverage.
2. **The generic (pre-existing) non-200 log wording in `rem_loop.py`'s `_process_fact`** — changed
   from a hardcoded `" (transport failure — attempt NOT charged)"` to `f" ({failure} — attempt NOT
   charged)"` so it reads correctly for the new `routing_refused` class too (it previously always
   said "transport failure" regardless of which non-chargeable class actually applied). Small,
   in-file, not called out in the brief explicitly but a direct consequence of adding a second
   non-chargeable class.
3. **`_verify_novel_edges`'s early-stop optimization** (breaking the k=3 loop after the first
   refusal) is not explicitly named as a separate invariant in the brief's I-4 pointer, but is a
   direct reading of "No daemon-side retry of a refused call within the same cycle... hammering it
   changes nothing." Flagging as an interpretation, not a literal instruction — the alternative
   (looping all VERIFY_CALLS anyway, degrading to a lower vote count) would also have been
   defensible, just less in the spirit of I-4.
4. **`consolidation_loop.py`'s `_post_nrem`** also runs its own (redundant-looking)
   `_routing_refusal(resp)` call purely to pick the telemetry `note` field
   (`routing_refused_<error>` vs `http_<code>`) — a second call to the same pure function per
   request, cheap, kept for telemetry accuracy even though the LOUD, entity-scoped log lives one
   level up in `_call_insight_llm`.

## Out-of-scope findings (reported, not fixed)

- **R-2 identity gap (relation_sweep cannot steer under auth-on) — a real, verified blocker, not
  fixed here.** `hive_mind_proxy.py`'s `_may_steer_llm` (S-14) permits exactly two agent names —
  `DAEMON_AGENT_NAMES = {"consolidation", "rem_daemon"}` — both minted ONLY via
  `_mint_daemon_token()`, called only from the gateway's own child-process spawn path for the REM
  and NREM/consolidation daemons it launches itself. `relation_sweep.py` is a **standalone**
  script (not spawned by the gateway) that authenticates with a static `AGENT_TOKENS` entry via
  `secure_env.get_secret("AGENT_TOKEN")` — under auth-on, its resolved agent name is whatever the
  operator named that token, never one of the two hardcoded strings, so `X-SM-LLM-Role: judge` is
  silently stripped by S-14 before reaching the routing logic (the request itself still succeeds —
  it just falls back to role-less, privacy-only eligibility). The only other identity class
  `_may_steer_llm` accepts is `role == "admin"` via `AGENT_ROLES` — but `_may_steer_llm`'s own
  docstring notes admin tokens are confined to `/admin/*` by `auth_middleware` and can **never
  reach `handle_proxy` at all**, so assigning `relation_sweep` an admin role would not fix
  steering, it would 403 the LLM call outright. No fix is reachable from `relation_sweep.py`
  itself, from any `.env`/`AGENT_ROLES` configuration, or from any other Unit 2 file — this is
  squarely `hive_mind_proxy.py`'s `DAEMON_AGENT_NAMES` frozenset (Unit 1/gateway ownership).
  **Under auth-off installs this is a non-issue** (`_may_steer_llm` returns `True`
  unconditionally when `AUTH_CONFIGURED_AT_STARTUP` is `False`). The `X-SM-LLM-Role: judge` header
  is still sent unconditionally from `relation_sweep.py` (harmless under auth-on — it is simply
  stripped — and correct/needed under auth-off), so no code change is needed once the gateway-side
  allowlist is widened; this is purely a report for whoever owns that file next.
- Everything the brief named out of scope (gateway-side capacity-503 telemetry, `reference_resolver.py`'s
  direct-backend bypass, L2 chunking, weight rework, per-role slot accounting, REM→coordinator
  dream-counter reporting, `/pool/status`'s advisory pre-check) — untouched, not re-verified beyond
  what the plan/brief already state.

## For the merger

- `_routing_refusal` is duplicated three times (deviation 1) — a candidate for extraction into a
  shared module in a later cycle, not this one.
- The R-2 gateway-side gap above blocks `relation_sweep.py` from ever actually steering under
  auth-on until `hive_mind_proxy.py`'s `DAEMON_AGENT_NAMES` is widened (or some other steer-permit
  mechanism is added for standalone, non-gateway-spawned daemons) — worth a security-review line
  item and a follow-up ticket.
- 503 `backend_at_capacity` and 422 `no_eligible_backend` are treated identically everywhere in
  Unit 2, resolving the "open call" Unit 1's HANDOFF left for whoever built this side.
