# HANDOFF — Unit 1 (gateway), model-attributes routing

Branch `feat/model-attributes-routing`, based on `main = 232f90d` (v0.9.12). Four commits on
top, working tree clean, full suite green (1834 passed).

```
ee9e16d test(gateway): add explicit I-4 regression — no new gateway-side retry on a plain 4xx/5xx
dc76fc4 test(gateway): isolate M-5 startup-refusal test from P-5 (found during mutation checking)
3099984 docs(gateway): document model-attributes routing fields, fit-check ratios, ...
4e42290 feat(gateway): model-attributes routing — descriptor schema, eligibility pre-filter, ...
```

Suite command (from worktree root):
```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -v
```
Result: **1834 passed, 0 failed.** 47 tests are new (`tests/test_model_attributes_routing.py`);
4 existing test files were edited (see "Existing tests touched" below).

Files touched, all inside Unit 1 ownership:
- `shared-memory/scripts/hive_mind_proxy.py` (the bulk of the work)
- `shared-memory/scripts/dream_telemetry.py` (N-4 only — additive `prompt_chars` param)
- `shared-memory/.env.example`, `shared-memory/ops/README.md` (docs)
- `tests/test_model_attributes_routing.py` (new, 47 tests)
- `tests/test_llm_steering_headers.py`, `tests/test_llm_backend_secrets.py`,
  `tests/test_llm_fault_origin.py`, `tests/test_credentialed_route_allowlist.py` (edited —
  see below)

Never touched: version files, CHANGELOG, SKILL.md, sync_skills.sh, any daemon file
(`rem_loop.py`, `consolidation_loop.py`, `relation_sweep.py`), `coordinator.py`.

---

## What was built

### 1. Descriptor schema (`_load_llm_backends`)

`LLM_BACKENDS_JSON` entries gained four new optional fields, all additive:
- `roles`: list from `{extract, verify, judge}`. `summarize` is explicitly rejected with a
  distinct "RESERVED" message (not a generic unknown-name error). Unknown names are collected
  into module global `_LLM_BACKEND_ROLE_CONFIG_ERRORS` at parse time (never raised there —
  every test in this repo imports the module freely) and raised as `SystemExit` only from
  `require_valid_llm_routing_config()`, called from `main()`.
- `n_ctx`: int. Non-int value excludes the backend (loud), same pattern as `extra_body`.
- `private_ok`: bool. Default = `token is None` (no `token_env` resolved → `True`; a resolved
  token → `False`). `LLM_BACKEND_PRIVATE_OK_EXPLICIT` tracks whether the value was explicit,
  which the M-5 refusal needs.
- `max_inflight`: int, per-backend concurrency ceiling.
- `price_per_mtok_in` / `price_per_mtok_out`: floats, stored + surfaced on `/health`,
  **never read by any routing function** — proved by
  `test_price_metadata_never_read_by_selection`.

Legacy comma-form (`LLM_BACKENDS`) and the `DEFAULT_TARGET` fallback populate all four as the
serves-all degenerate case: `roles=None`, `private_ok=True`, `private_ok_explicit=False`,
`max_inflight=None` — byte-identical selection to v0.9.12 (I-5a).

### 2. Startup refusals — `require_valid_llm_routing_config()`, called from `main()` only

Same placement reasoning as the existing `require_auth_when_provider_keys_configured()`: an
unconditional check at parse/import time would kill test collection itself.

1. **Unknown role name** (incl. `summarize`) → `SystemExit` naming the backend and the bad
   name(s).
2. **M-5 (Critical)**: a credentialed backend (`token_env` resolved) with neither `roles` nor
   an *explicit* `private_ok` → `SystemExit` demanding the operator pick one.
3. **P-5**: `AUTH_CONFIGURED_AT_STARTUP` False + any `private_ok=false` backend → `SystemExit`,
   governed by the same `ALLOW_UNAUTHENTICATED_PROVIDER_KEYS=1` override S-05 uses (warns
   instead of refusing).

### 3. Selection restructure (`_select_llm_backend`, `_eligible_backends`)

`_eligible_backends(role, est_prompt_tokens, effective_max_tokens)` computes the **hard
pre-filter** (role+privacy+fit) first. `_select_llm_backend` restructured so every fallback
tier — affinity hit, the protected/cold cascade, the cooldown-ignoring last resort — operates
strictly inside that set; the final fallback tier now bottoms out at `eligible`, never
`LLM_POOL` (this was the actual defect class P-1/P-2 named — see the mutation-check table
below for the concrete before/after). `_ordered_llm_backends` (dead code, zero callers) is
deleted.

Eligibility rule implemented exactly as specified:
- Role-carrying traffic: `(roles absent AND private_ok) OR role in roles`. Note the second
  branch is **not** gated by `private_ok` at all — an explicit `roles` list is itself the
  per-function privacy opt-in, so a `private_ok=false` backend that lists a role IS eligible
  for that role's traffic.
- Role-less traffic: `private_ok` alone; a backend's `roles` list is completely ignored.

### 4. The 422 refusal (`I-2a`)

Empty eligible set → `HTTP 422 {"error":"no_eligible_backend","constraint":"role"|"privacy"|
"fit","role":<role-or-null>}` + `X-SM-Fault-Origin: gateway`, computed and returned **before**
any inflight accounting (the increment happens later, inside the `try:` block — this refusal
returns from a point in the code that never reaches it). `_classify_no_eligible_constraint`
picks the constraint label:
- `"fit"` if role+privacy alone would have left a non-empty set (size was the only problem).
- else `"privacy"` if a serves-all (`roles` absent) candidate exists in the fleet (blocked
  only by `private_ok=false`), or if the traffic was role-less at all (privacy is the only
  axis role-less traffic can fail on).
- else `"role"` (nothing in the fleet is configured to serve this function, privacy moot).

This classification algorithm is **not specified verbatim in the plan** — I derived it from
the eligibility formula and documented the reasoning in `_classify_no_eligible_constraint`'s
docstring. Flagging it explicitly in case the merger/reviewer wants a different tie-break rule.

### 5. `max_inflight` concurrency cap (I-8/I-8b)

A backend at its cap is excluded from the `not_capped` candidate list computed from the
(already eligibility/health-filtered) `cold` set; if every remaining candidate is capped,
`_select_llm_backend` returns `None`. `handle_proxy` distinguishes the two `None` cases by
calling `_eligible_backends()` itself first (empty → 422, no wait) — only when it's
non-empty does it call the new async `_select_backend_waiting_on_capacity()`, a bounded poll
loop (`LLM_MAX_INFLIGHT_WAIT_S`/`LLM_MAX_INFLIGHT_POLL_S`, default 120s/0.5s) around
`_select_llm_backend`. If the wait is exhausted, `handle_proxy` returns
**`HTTP 503 {"error":"backend_at_capacity"}`** — deliberately *not* `422`, because the 422
body's `constraint` enum is fixed to `role|privacy|fit` in the plan and capacity-exhaustion is
none of those. **This status/shape choice is my interpretation, not spelled out in the
plan** — flag for review. Neither the eligibility 422 nor the capacity-wait/503 path ever
touches `_llm_inflight` (I-8b) — the increment only happens after a real backend is chosen and
dispatch begins.

### 6. P-6 steering-header hygiene

`X-SM-LLM-*` headers are now stripped from `upstream_headers` **unconditionally**, right
before the upstream call — daemons included. Before this cycle, a daemon/admin identity's
`X-SM-LLM-Role` header survived all the way to the provider; now it is read once (for the
gateway's own routing decision, via `steer_headers`) and then stripped again
(`_strip_llm_steering_headers(steer_headers)`) before `_filter_headers` builds the outbound
header set. **This deliberately changes three existing pinned assertions** in
`tests/test_llm_steering_headers.py` (see below).

### 7. H-1/H-2/H-3 health display

- H-1: a bare-probe 401/403 from a backend with `LLM_BACKEND_TOKENS[b] is not None` now reads
  `"ok"` in `backend_status`/`checks["llm"]` — it answered, and no Authorization header is ever
  attached to this probe (no per-poll key spraying). Genuinely down (connect error / 5xx)
  unaffected.
- H-3: new `_v1_models_probe_url()` avoids `/v1/v1/models` doubling when the configured base
  already ends in `/v1` (`LLM_BACKENDS_JSON`'s own documented shape,
  `"https://api.deepseek.com/v1"`). Used in both probe call sites
  (`_probe_backend_alive` and `_build_health_checks`'s backend loop).
  **Live-verified** (bare curl, no key): both `https://api.deepseek.com/v1/v1/models` and
  `.../v1/models` return `401` — DeepSeek's edge auth governor rejects *any* path uniformly
  (confirmed with a bogus path too, also 401), so the doubling bug happened to be invisible
  for this specific provider. The fix stands anyway: it is a real URL-construction defect
  regardless of what one provider's auth gate does with it, and a path-sensitive gate on a
  different provider would answer the two URLs differently.

### 8. Telemetry (Group 3)

- `checks["llm_routing"]`: `routed_role_extract`/`_verify`/`_judge` counters + paired
  `_last_ts`, `routing_no_eligible_backend` + `_last_ts`, `routing_fit_rejected` + `_last_ts`.
  Surfaced unconditionally (not gated on pool size) on the authenticated `/health` payload.
- `checks["llm_token_usage"]`: per-backend `tokens_prompt_total`/`tokens_completion_total` +
  `tokens_last_ts`, parsed from `usage` on a **non-streaming, uncompressed** proxied response.
  Accumulated during the existing chunk-passthrough loop (bounded by
  `LLM_USAGE_CAPTURE_CAP_BYTES`, default 2 MiB — capture is abandoned, not attempted, past
  that), parsed once after the loop. A compressed response (`Content-Encoding` set) skips
  capture entirely — **out of scope this cycle**, see "Out-of-scope findings" below.
- A2 lifecycle sum lines: `_emit_token_lifecycle_sums()` — direct synchronous write (journal
  via `log.info`, plus `GATEWAY_AUDIT_LOG_PATH` via `log_hygiene.append_secure()` directly,
  **never** `AsyncLineWriter`, per the explicit shutdown-flush-hang warning). Called
  unconditionally in `main()`'s drain sequence, plus optionally on
  `TOKEN_LIFECYCLE_SUM_INTERVAL_S` (0 = disabled, the default) via a new background task.
- `/pool/status` F-3: `free_slots` now counts only `serves_all` (`roles absent AND
  private_ok`) backends; the `backends` roster gained an additive `serves_all` field per entry
  so a monitor can still see everything.
- N-4: `dream_telemetry.record_llm_call()` gained an additive, keyword-only `prompt_chars:
  int | None = None` parameter, written into the JSONL row. **Not yet wired into any call
  site** — that's Unit 2's job (REM/NREM/relation_sweep all live in daemon files I don't own).

---

## The two required measurements

### 1. chars→tokens ratio (`CHARS_PER_TOKEN_RATIO`, `FIT_MARGIN`)

Ran `/tokenize` against **both** configured local backends (`localhost:5000`, `localhost:4000`
— confirmed identical model, `Qwen3-14B-Q4_K_M.gguf`, on both) with 20 varied prompt samples
(prose, JSON-shaped dream-prompt bodies, Python/SQL code, a 120-entity grounding-prefix block,
a 40-fact NREM-cluster block, Greek text, and three degenerate single-token samples I then
EXCLUDED as non-representative — see below).

Raw per-sample results (identical on both backends, confirming one shared tokenizer):

| sample (abbreviated) | chars | tokens | ratio |
|---|---:|---:|---:|
| "The quick brown fox…" | 71 | 16 | 4.438 |
| "Shared memory frameworks…" | 145 | 21 | 6.905 |
| "In the beginning…" | 123 | 26 | 4.731 |
| "Xenofon reviewed the pull request…" | 143 | 27 | 5.296 |
| "A gateway that owns routing…" | 143 | 27 | 5.296 |
| JSON `{"role":"system",…}` | 66 | 18 | 3.667 |
| JSON `{"facts":[…]}` | 99 | 35 | 2.829 |
| JSON `{"grounding":[…]}` | 110 | 28 | 3.929 |
| JSON `{"messages":[…]}` | 131 | 44 | 2.977 |
| JSON `{"decision":"adopt",…}` | 102 | 35 | 2.914 |
| Python function def | 195 | 50 | 3.900 |
| Python code block | 137 | 37 | 3.703 |
| SQL query | 116 | 29 | 4.000 |
| 120-entity grounding prefix | 1473 | 726 | 2.029 |
| 40-fact cluster block | 2544 | 732 | 3.475 |
| Greek text | 53 | 44 | **1.205** |
| "Thessaloniki, Greece…" | 61 | 25 | 2.440 |
| ~~"ok"~~ (excluded, degenerate) | 2 | 1 | 2.000 |
| ~~"1234567890"~~ (excluded, degenerate) | 10 | 10 | 1.000 |
| ~~"{}"~~ (excluded, degenerate) | 2 | 1 | 2.000 |

**Excluded the three single/near-single-token samples** as non-representative of real dream
call bodies (which are always hundreds+ characters, never a bare `"{}"`) — including them would
have pulled the "most conservative" ratio down to 1.000 (every char = one token), which is
excessively conservative for the actual traffic this estimates. Documenting the exclusion
explicitly rather than silently cherry-picking.

**RATIO = 1.2** — the measured floor (1.205, Greek text — the densest real sample), rounded
down slightly. `CHARS_PER_TOKEN_RATIO` env default.

**A second live check** measured the gap between this JSON-body-char estimate and the REAL
chat-template-rendered prompt size (4 real `max_tokens:1` chat completions against
`localhost:5000`, reading `timings.prompt_n`):

| body_chars | est_tokens (RATIO=1.2) | real prompt_n | est/real | gap |
|---:|---:|---:|---:|---:|
| 332 | 276.7 | 50 | 5.53x | +453% |
| 1633 | 1360.8 | 745 | 1.83x | +83% |
| 2743 | 2285.8 | 752 | 3.04x | +204% |
| 90 | 75.0 | 12 | 6.25x | +525% |

The estimator **overestimates real prompt tokens by 83%–525% in every sample tried** — i.e.
RATIO's own conservatism (not FIT_MARGIN) is what does the real protective work against
under-counting. Given that, **FIT_MARGIN = 0.10** is a modest, deliberately non-zero residual
buffer — flagged **unmeasured** (fact:1338 discipline) — for content denser than anything
sampled (heavy CJK/emoji, deeply repeated JSON) and general n_ctx bookkeeping the char
estimator doesn't model, not for correcting an under-count the measurement above didn't find.

**FIT_DEFAULT_OUTPUT_TOKENS = 2048** — explicitly unmeasured (flagged in `.env.example`), only
used when a caller's body has no `max_tokens` at all.

### 2. H-3 probe URL check

Done — see section 8 above. Live curl confirmed both the doubled and correct URL forms return
401 uniformly against `api.deepseek.com` (its auth governor rejects any path, verified with a
bogus path too). Fix applied regardless; not empirically distinguishable through this one
provider.

---

## Verify-at-build item: llama-server tolerance of unknown body keys

Live `POST /v1/chat/completions` to `localhost:5000` with two bogus top-level keys
(`totally_unknown_key_xyz`, `another_bogus_field`) alongside a normal chat body: **HTTP 200,
request succeeded normally**, unknown fields silently ignored. No code change needed —
`extra_body` dialects on a shared pool are safe against this specific backend.

---

## Invariant → test → mutation-check table

Mutation checks were run against the **actual worktree file** via scratchpad backup/restore
(`fact:1244` discipline — never `git checkout --`): copy to
`/tmp/.../scratchpad/hive_mind_proxy.py.orig_backup`, apply a targeted mutation via a small
Python script, run the relevant test(s), confirm the exact expected failure, `cp` the backup
back, verify `git diff` is empty. Every mutation below was restored and the full suite
re-confirmed green before moving to the next.

| Invariant | Test(s) | Mutation | Result |
|---|---|---|---|
| P-2 (affinity hit outside eligible = MISS) | `test_i1a_affinity_hit_outside_eligible_set_is_a_miss` | Removed the `eligible_set` gate from BOTH the outer affinity-hit condition AND `_usable()` (removing only one left the other as a redundant guard — confirmed via a first, informative no-op attempt) | Exactly that 1 test fails |
| P-1 Critical (cold fallback bottoms at `LLM_POOL`, not `eligible`) | `test_i1a_cold_fallback_never_widens_past_eligible` | Changed the last two fallback tiers from `eligible`/`[b for b in eligible ...]` back to `LLM_POOL`/`list(LLM_POOL)` | Exactly that 1 test fails |
| I-2a (422 refusal) | 5 tests (`test_422_*`) | `if not _eligible_backends(...)` → `if False and not _eligible_backends(...)` (with `LLM_MAX_INFLIGHT_WAIT_S=0.05` env override to keep the resulting hang bounded — first attempt at real defaults hit my own 60s command timeout, itself informative: confirms disabling the refusal makes the request WAIT instead, exactly the "never queues" property it guards) | Exactly those 5 tests fail |
| I-3 (fit check) | `test_422_fit_constraint_when_oversized`, `test_fits_boundary_math` | `_fits()` body replaced with `return True` unconditionally | Exactly those 2 tests fail |
| M-5 (credentialed-choice refusal) | `test_m5_credentialed_backend_with_no_choice_refuses_startup` | `if needs_explicit_choice:` → `if False and needs_explicit_choice:` | First attempt: test still passed — P-5 was independently refusing the same scenario (`private_ok` defaults False for this backend too), so the test wasn't actually isolating M-5. **Fixed the test** (auth-on, so only M-5 can fire) — re-ran the same mutation: exactly that 1 test fails. Committed as `dc76fc4`. |
| P-5 (auth-off + private_ok=false refusal) | `test_p5_auth_off_plus_private_ok_false_refuses_startup` | `if not private_false:` → `if True:` | Exactly that 1 test fails |
| I-8 (max_inflight cap) + I-8b (accounting) | `test_i8_cap_never_widens_eligibility`, `test_i8b_capacity_wait_exhausted_never_touches_inflight` | `_at_cap()` body replaced with `return False` | Exactly those 2 tests fail (the other two I-8 tests correctly still pass — they don't depend on the cap actually excluding anything in their specific scenario) |
| H-1 (credentialed 401/403 = ok) | `test_h1_credentialed_backend_401_reads_ok` | Disabled the credentialed-exception branch in the probe loop | Exactly that 1 test fails |
| H-3 (no /v1 doubling) | `test_v1_models_probe_url_avoids_doubling` | Removed the `endswith("/v1")` branch | Exactly that 1 test fails |
| P-6 (steering headers stripped for everyone) | 4 tests (2 new + 2 updated existing) | `upstream_headers = self._filter_headers(_strip_llm_steering_headers(steer_headers))` → `self._filter_headers(steer_headers)` | Exactly those 4 tests fail (the "role still drives routing" test correctly still passes) |
| F-3 (`/pool/status` free_slots) | `test_f3_free_slots_excludes_role_scoped_backend` | `free += 1 if (avail and serves_all) else 0` → `free += 1 if avail else 0` | Exactly that 1 test fails |
| N-4 (`prompt_chars` additive) | `test_n4_record_llm_call_prompt_chars_additive` | Removed the `"prompt_chars": prompt_chars,` line from `record_llm_call`'s `rec` dict | Exactly that 1 test fails |

I-4, I-5a, I-6a, I-7 are proven by tests but were not separately mutation-checked as "new
invariants" — they are explicitly **unchanged-behavior** claims (I-4: no new retry added at
all — nothing to mutate; I-5a/I-6a/I-7: proven by the full existing suite continuing to pass
plus the additive-field assertions in `test_i7_*`).

⭐ **Assertion discipline applied**: every mutation-check test above asserts a concrete VALUE
(a specific backend URL, a specific HTTP status, a specific constraint string, a specific
count/boolean) rather than only an equality between two expressions that could move together.

---

## Existing tests touched, and why

- **`tests/test_llm_steering_headers.py`** — 3 assertions **deliberately changed** (P-6): a
  daemon/admin identity's `X-SM-LLM-Role` header used to survive to the upstream forward; it
  no longer does, for any identity. Updated `test_daemon_identity_keeps_steering_header`,
  `test_rem_daemon_identity_also_keeps_steering_header`, and
  `test_auth_off_install_keeps_backward_compatible_pass_through` (this last one is the exact
  line the plan named — `:98` in the pre-cycle file). Docstrings updated to point at the new
  tests in `test_model_attributes_routing.py` that prove the role still drives *routing*
  despite the strip.
- **`tests/test_llm_backend_secrets.py`**, **`tests/test_llm_fault_origin.py`**,
  **`tests/test_credentialed_route_allowlist.py`** — added `"private_ok": true` to every
  credentialed-backend fixture that sends **role-less** traffic and expects it to actually
  reach dispatch (S-04 allowlist checks, credential-fault classification, backend/key_attached
  stash). Without it, the new eligibility pre-filter correctly 422s this traffic before any of
  those mechanics run — these tests are about the mechanics downstream of dispatch, not about
  M-5's startup choice (which has its own coverage), so making the explicit choice in their
  fixtures keeps them testing what they always tested.

---

## Deviations from the brief / open interpretation calls (flag for review)

1. **`_classify_no_eligible_constraint`'s role/privacy tie-break algorithm** is not specified
   in the plan — I derived it (see section 4 above) and documented the reasoning inline.
2. **Capacity-wait exhaustion returns `503 {"error":"backend_at_capacity"}`**, not `422` — the
   plan's 422 body's `constraint` enum is fixed to `role|privacy|fit`, none of which fit
   "every eligible backend is momentarily at its cap." Chose 503 to reuse the existing
   "backend problem" status class rather than invent a fourth constraint value.
3. **`CHARS_PER_TOKEN_RATIO` and `LLM_MAX_INFLIGHT_WAIT_S`/`LLM_MAX_INFLIGHT_POLL_S`** env var
   names are my own choice — not named in the plan/brief.
4. Compressed (`Content-Encoding` set) LLM responses **skip token-usage capture entirely**
   (see out-of-scope findings) rather than attempting the bounded-prefix decompress
   `coordinator.py`'s fault-peek helper uses — that helper caps at 8192 *decompressed* bytes,
   which would likely truncate before reaching a trailing `usage` object on anything but a
   small completion. Full decompression felt out of proportion to this cycle's scope; flagged
   rather than silently accepted.
5. `checks["llm_routing"]` and `checks["llm_token_usage"]` are surfaced **unconditionally**
   on the authenticated `/health` payload (not gated behind `len(LLM_BACKENDS) > 1` the way
   `llm_backends`/`llm_pool`/`llm_affinity` are) — routing/token telemetry is meaningful even
   for a single role-scoped backend, so I didn't see a reason to withhold it for single-backend
   installs.

## Out-of-scope findings (reported, not fixed)

- `reference_resolver.py` calling a backend **directly**, bypassing the gateway (R-2, named in
  the plan as its own follow-up) — did not touch it, did not re-verify it beyond what the plan
  already states.
- Compressed proxied LLM response bodies never get token-usage accounting (see deviation 4
  above) — the counter is simply never incremented for that call; never breaks the proxy path.
- The xAI reasoning-off parameter-name verify-at-build item (named in the plan's *original*
  section, not repeated in the REVISED DESIGN or in this brief's explicit list) — not checked;
  no grok/xAI entry is configured anywhere in this repo's tests or docs yet, so it wasn't
  reachable to verify.
- Per-role `/pool/status` slot accounting remains fully deferred (F-3 explicitly named this as
  a non-goal this cycle) — only the binary `serves_all` filter was built.

## For Unit 2 (daemons)

- `dream_telemetry.record_llm_call(..., prompt_chars=...)` is ready; no call site passes it
  yet.
- The gateway now returns **422** (not 413) on `no_eligible_backend`, and **503** on
  `backend_at_capacity` (new, not named in the earlier draft plan) — daemon-side recognition
  needs to handle both, per F-1/F-2's "skip without charging rem_attempts" ruling. The 503
  capacity case probably wants the SAME treatment (loud, once, skip) since it's also "the
  gateway declined to serve this job right now," not a record defect — but that's a call for
  whoever builds Unit 2, not decided here.
- `X-SM-LLM-Role` is the only header daemons need to send (A-3 simplification confirmed — the
  gateway computes its own prompt-size estimate from the buffered body).
