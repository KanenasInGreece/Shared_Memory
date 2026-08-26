# HANDOFF — v0.9.62 builder (fix/v0.9.62-fallback-headline-rem-wording)

Base: `origin/main` `013c7ca` (v0.9.61). Two independent fixes, one PR, per
`Local_Documentation/ColdBriefs/V0962_Builder_Brief.md`.

## Step 1 — keyword-fallback headline reaches the operator on both front doors

**Files:**
- `shared-memory/scripts/memory_bridge.py` (+ tracked copy
  `shared-memory-skill/shared-memory/scripts/memory_bridge.py`, byte-identical
  after the change — verified with `diff`)
- `mcp/vector-skill.py`
- `shared-memory/SKILL.md` (+ tracked copy
  `shared-memory-skill/shared-memory/SKILL.md`, byte-identical — verified with `diff`)
- `tests/test_client_search_signals.py`, `tests/test_search_axis_filters.py`

**What changed:**
- `memory_bridge.py`: split `search_and_rerank()` into a new
  `_search_payload(query, limit, project=, domains=, since=) -> dict` (the HTTP
  call + all existing error handling, unchanged) and a thin
  `search_and_rerank()` that calls it once and returns
  `result.get("results", result)` exactly as before. Added
  `_fallback_warning(payload) -> str | None`: fires only when `payload` is a
  dict with `fallback == "keyword"`, returns
  `EMBEDDING UNAVAILABLE — keyword (substring) fallback served N result(s), unranked; the embedder is down or still starting (see embedder on /health)`.
  `main()`'s `search` branch now calls `_search_payload` once and derives
  BOTH `_unranked_warning` and `_fallback_warning` from the same payload
  (still exactly one HTTP call); stdout JSON (`results`) is unchanged.
- `vector-skill.py`: same split — `_search_payload(...) -> dict | str` (dict
  on a 2xx reply, an already-phrased error STRING on `GatewayReplyError`/
  transport failure, matching what `hybrid_search_and_rerank` returned
  directly before the split). `hybrid_search_and_rerank` now calls it once,
  branches on `isinstance(payload, str)` for the unreachable-gateway case,
  and otherwise renders as before, prepending `NOTE: <fallback>\n\n` the
  same way it already does for the unranked warning. Added the mirroring
  `_fallback_warning`.
- `SKILL.md` (both copies): one new line under the entity-graph-fallback
  sentence (~line 168) describing the keyword fallback and the CLI's
  `EMBEDDING UNAVAILABLE` stderr line.

**Tests added** (`tests/test_client_search_signals.py`):
- `test_fallback_warning_none_on_normal_payload` / `_error_payload` / `_non_dict` (a)
- `test_fallback_warning_fires_with_zero_results` / `_two_results` (b) — the
  zero-results case is the one that matters (fact:1609)
- `test_fallback_warning_parity_between_clients` (c), parametrized over 6
  fixtures incl. the two keyword-fallback ones and the existing None-cases
- `test_cli_search_prints_fallback_warning_on_empty_keyword_fallback` (d) —
  stderr carries `EMBEDDING UNAVAILABLE` / `0 result(s)`, stdout is exactly `[]`
- `test_mcp_search_prepends_fallback_note_on_empty_keyword_fallback` (e) —
  rendered text starts with `NOTE: EMBEDDING UNAVAILABLE`

**Existing tests updated** (refactor fallout, not new behaviour): `main()`'s
`search` branch now calls `_search_payload` instead of `search_and_rerank`,
so three pre-existing tests that patched `search_and_rerank` to drive
`main()` had to move their patch target:
`test_cli_search_prints_stderr_note_and_leaves_stdout_json_unchanged`,
`test_cli_search_prints_no_stderr_note_when_fully_ranked`
(`test_client_search_signals.py`), and
`test_cli_search_passes_project_domain_since_through`,
`test_cli_search_without_filters_passes_none_through`
(`test_search_axis_filters.py`). Assertions unchanged, only the patch target
and the fake's return shape (payload dict instead of bare list). All four
pass. `test_search_and_rerank_*` in `test_memory_bridge.py` (calling
`search_and_rerank()` directly, real HTTP mock) pass untouched, confirming
its return type/contract is unchanged.

**Mutation check:** removed the `fallback == "keyword"` condition from
`_fallback_warning` in both memory_bridge copies (guard reduced to an
unconditional `return None`), on a scratch backup. Result: `(b)` — both
`test_fallback_warning_fires_with_zero_results` and `_two_results` — died;
the parity test `(c)` also died (memory_bridge now returns `None` while
vector_skill still returns the real warning); `(d)` —
`test_cli_search_prints_fallback_warning_on_empty_keyword_fallback` — died.
Restored from backup, re-ran `test_client_search_signals.py` +
`test_search_axis_filters.py` + `test_memory_bridge.py` +
`test_rem_degeneration.py` (162 passed) to confirm the restore was exact.
`git status` clean of stray changes afterward (diffed against backup, byte-identical).

**Invariants held (why):**
- stdout JSON of `search` unchanged for every payload — `results =
  payload.get("results", payload)` is exactly what `search_and_rerank()`
  always computed; nothing new touches stdout.
- `_unranked_warning` behaviour unchanged — untouched function, still fed
  the unwrapped `results` list; both warnings print independently (verified
  by (d)/(e) firing without an unranked note, and the pre-existing
  B2 tests still passing unmodified in assertion content).
- One HTTP call per search — `_search_payload` is the only network call;
  `main()`/`hybrid_search_and_rerank` call it exactly once each.
- `search_and_rerank()` return type/contract unchanged — `test_search_and_rerank_*`
  pass untouched (not edited).
- Both clients at feature parity — parity test (c) covers 6 fixtures.
- Tracked skill copy identical — `diff` verified after every change to the
  source copy.

## Step 2 — REM double-truncation ERROR wording (Group 3, wording only)

**Files:** `shared-memory/scripts/rem_loop.py`, `tests/test_rem_degeneration.py`

Changed only the message text of the non-degenerate ("honest") double-truncation
`logger.error(...)` at (now) ~line 2491 — same call, same `pg_id, retry_bound`
args, no control-flow change. Old wording ended with "Raise REM_MAX_TOKENS_SOLO
if this record is legitimately large" — exactly the bump `decision:1330`
measured and rejected. New wording: the unit fails now and retries on a later
pick-up (`rem_attempts +1`; dead-letters at `REM_MAX_ATTEMPTS`); if that later
pick-up completes well under the bound, both truncations were a repetition loop
the classifier couldn't see — do NOT raise `REM_MAX_TOKENS_SOLO`
(`decision:1330`); raise it only if every pick-up truncates with a differing,
non-repeating tail. The sibling `degenerate` branch (already correctly worded,
already cites `fact:1329/1330`) was left untouched.

**Test added:** `test_honest_double_truncation_error_advises_retry_not_bump`
— forces two consecutive honest (non-degenerate) truncations via
`_length_resp`, asserts (via `caplog`) the ERROR text contains `decision:1330`
and `rem_attempts`/"retry", and does NOT contain `"Raise REM_MAX_TOKENS_SOLO if"`.

**Mutation check:** reverted the wording to the old string on a scratch copy
of `rem_loop.py`, re-ran the new test alone — it failed exactly as expected
(`AssertionError: assert 'decision:1330' in '...Raise REM_MAX_TOKENS_SOLO if this record is legitimately large.'`).
Restored from backup; `tests/test_rem_degeneration.py` (20 passed) and
`git status`/`diff` confirmed a clean, exact restore.

## Suite

```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -q -x
```
`2983 passed, 1 skipped, 2 warnings in 103.31s` — `EXIT=0`. Baseline was
2969 passed / 1 skipped; +14 net new tests (13 fallback-warning tests +
1 REM wording test), 0 regressions.

## Change groups cleared

- **Group 1** (client surface + delivery): both `memory_bridge.py` copies
  edited and diffed byte-identical; `mcp/vector-skill.py` (the "easy one to
  forget" — MCP door) got the same split and the same new warning, so the
  two front doors stay at parity. `MANIFEST.txt` untouched (no new files
  shipped, both edited files were already listed).
- **Group 2** (capture + ontology): not touched by this change — no new
  capture field, no ontology change. `SKILL.md` line added honestly
  describes the new headline (one line, per line-discipline).
- **Group 3** (daemon behaviour + observability): `rem_loop.py` wording-only;
  no metric key, no telemetry shape, no monitor contract touched — confirmed
  by grepping `rem_loop.py` diff, it is exactly the one `logger.error` string.

## Findings / open questions

- None. The brief's design was followed exactly; no mid-build design
  questions arose. The only judgment call was the exact shape of the
  mutation-check ("remove the fallback=='keyword' condition") — implemented
  as reducing the guard to an unconditional `return None` (guard disabled),
  which is the interpretation that produces the (b)/(d) failures the brief
  names, and additionally killed the parity test (c) as a byproduct — noted
  above, not a concern.
