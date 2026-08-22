# HANDOFF — agent-path-check

Builder brief: detect and warn when `uv` is reachable only via the operator's own shell
profile, not via a profile-free shell (the shape every agent spawns to run the skill).
Base `main`@`ea5c338` (v0.9.29).

## Status: COMPLETE, green, ready for merger review.

## What shipped

1. **`shared-memory/scripts/preflight.sh`** — after the existing `command -v uv` check
   (kept, unchanged, still a hard failure if uv is absent entirely), added a second check:
   does uv resolve with `env -i PATH="$(getconf PATH)" sh -c 'command -v uv'` — i.e. with
   the WHOLE environment cleared and PATH forced to the platform's own compiled-in
   default. If not, prints a `warn()` + `need()` (non-fatal — preflight still exits 0 on
   this alone) naming the agent-facing silent-failure consequence and two remedies
   (symlink uv onto a system-default-PATH directory, or set PATH in the agent's own
   config). If `getconf` itself can't answer, the check says nothing (no false claim
   either way).

   Mechanism choice: `env -i PATH="$(getconf PATH)" sh -c ...` over `bash --noprofile
   --norc` — the latter does NOT clear inherited `PATH`, so run from the operator's own
   (already-profile-loaded) shell it would silently inherit the same PATH and never
   detect the problem it exists to catch. `getconf PATH` costs nothing (glibc, not
   uv/python) and is the most portable proxy for "what a shell has before anything
   user-specific runs."

2. **`shared-memory/scripts/sync_skills.sh`** — same check, placed right after the
   `AGENTS` array is finalized (before the per-agent loop), gated on **at least one
   target directory already existing on disk** (sync is the one place that knows an
   agent install is real) and printed **once per run**, not once per directory.
   Non-fatal; does not touch the exit code or delivery.

3. **`AGENTS.md`** Phase 8 — new paragraph after the "COPY EVERY FILE" callout: states
   the requirement plainly, explains why an agent's shell differs from the operator's,
   and tells the operator to re-run `preflight.sh` **after** installing an agent too
   (this is a per-agent-shell condition, not a one-time per-host one).

4. **`shared-memory/SKILL.md`** (+ byte-identical tracked copy
   `shared-memory-skill/shared-memory/SKILL.md`) — new troubleshooting callout right
   after the existing "use absolute paths" AI-instruction block: what `uv: command not
   found` means, why it can coexist with the operator's own `which uv` succeeding, and
   — the sharpest point — that the agent does NOT report a broken memory system, it
   silently falls back to something else or saves nothing.

## Tests (new, 12 total: 11 pass + 1 legitimate skip)

- `tests/test_preflight_uv_path_check.py` (6 tests) — executable, drives the real
  `preflight.sh` via subprocess with a controlled PATH (stub `uv` + stub `getconf`,
  never touching the real system PATH's other tools). Covers I-P1 (warn + names agent
  consequence, existing hard-fail-on-absent-uv untouched), I-P2 (answer doesn't depend
  on uv actually being executable, or on python — python/python3 poisoned with a
  failing stub and the check still answers correctly), I-P3 (no false alarm when the
  stub uv is included in the fake getconf answer), plus a defensive test for
  getconf-itself-unavailable (says nothing either way). One test
  (`test_operator_only_absence_is_still_a_hard_failure`) self-skips if the sandbox's own
  minimal PATH happens to already contain uv — precondition guard, not a defect; it did
  skip in this sandbox (uv lives at `/usr/bin/uv` here).
- `tests/test_sync_uv_path_warning.py` (6 tests) — same stubbing technique against
  `sync_skills.sh` via `SHARED_MEMORY_SYNC_AGENTS` / `SHARED_MEMORY_SYNC_SKIP_TRACKED=1`
  (matches the existing pattern in `test_skill_delivery.py`). Covers: warns once when an
  existing install can't reach uv profile-free; no warning when nothing is installed
  yet; warns exactly ONCE across two existing install dirs (not per-directory); stays
  non-fatal and delivery still succeeds; no false alarm on a correctly-set-up host;
  answer doesn't depend on uv executing successfully.

**Mutation-checked both guards** (scratchpad-copy method, never `git checkout --`):
inverted the WARN condition in each script (`preflight.sh`: `elif ... ; then` →
`elif true; then`; `sync_skills.sh`: the warn `if` → `if false; then`), reran the
corresponding test file, confirmed exactly the tests that assert WARN-emission failed
(3 of 6 in each file — the I-P3/no-op/getconf-unavailable tests correctly kept passing
since that mutation doesn't touch those paths), then restored from the scratchpad backup
and reconfirmed green. See prior conversation turns for the exact failure output.

## Full suite

Baseline 2181 passed. This branch: **2192 passed, 1 skipped** (2181 + 12 new items, 1 of
which self-skips on this sandbox as noted above). Run from worktree root:

```
uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx \
  --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -q
```

## Scope respected

Touched only: `preflight.sh`, `sync_skills.sh`, `AGENTS.md`, both `SKILL.md` copies,
`tests/`. Did NOT touch `postflight.sh`, `Documentation/postflight.md`, `coordinator.py`,
`hive_mind_proxy.py`, `generate_tokens.py`, `bootstrap_tokens.sh`, `memory_bridge.py`, any
version-bearing file, or `CHANGELOG.md`/`README.md`.

## Findings for the merger (not rulings — my own judgment, not yet decided)

- **No design changes discovered that needed escalation.** The addendum mid-task
  (uv is installed via the upstream curl installer, deliberately, as policy — not a
  distro package) was folded into the warning text and AGENTS.md addition directly:
  remedies name "symlink onto system default PATH" or "set PATH in the agent's own
  config", never "switch to a distro package."
- **Judgment call, not measured**: made the new check a WARNING (not a hard preflight
  failure). Reasoning: it doesn't block `docker compose up` / the gateway itself working,
  and a host running only MCP/LM-Studio (no CLI agent spawning non-login shells) is not
  actually broken by this at all — hard-failing would block a fully-functional install.
  This is my judgment under "decide and justify," not a ruling — flag if you want it
  escalated to a hard fail instead.
- **Placement of the SKILL.md troubleshooting note**: put it as a new callout right after
  the existing "use absolute paths" AI-instruction block (before the record-model
  section) rather than creating a new "## Troubleshooting" top-level section, since no
  such section currently exists and this is tightly coupled to the invocation-mechanics
  callout already there. `tests/test_capture_surface_documented.py` still passes
  (it doesn't pin section structure, only flags/refusals/ratings/version strings).
- **No new env var added** for testability — I considered adding an override like
  `SHARED_MEMORY_SYSTEM_DEFAULT_PATH` to make the getconf answer test-controllable, but
  instead the tests shadow the real `getconf` binary via PATH ordering, which drives the
  actual production code path with zero new configuration surface. Judgment call under
  "don't add an unmeasured parameter" — flag if a real override seam is wanted instead.
