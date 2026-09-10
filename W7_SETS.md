# W7 — PART 2: The three explicit sets, derived mechanically

Derivation command (the AST extractor T1's test file implements; run standalone
for this document via a throwaway copy of the same logic):

```
python3 - <<'PY'
# Walks shared-memory/scripts/*.py + shared-memory/migrations/*.py (fail-closed
# glob, exclude list empty) plus shared-memory-skill/shared-memory/scripts/
# memory_bridge.py, AST-parses each for os.environ.get(...)/os.getenv(...)/
# os.environ[...] string-literal names, splits by whether the reading file is
# memory_bridge.py (-> client) or anything else (-> framework), then diffs each
# half against its own .env.example.
PY
```
(The actual script used: `tests/test_env_example_covers_every_variable_read.py`'s
own `_collect()` / `_template_covers()` functions — this file states the result,
not a second implementation.)

## CATCH-SET (mechanical — every name the extractor found absent from its template)

**Client** (memory_bridge.py, both copies, checked against
`shared-memory-skill/shared-memory/.env.example`):
```
PROJECT_ROOT_MARKERS, SHARED_MEMORY_PROJECT
```
(SECURE_ENV_FILE, AGENT_ID, XDG_RUNTIME_DIR are also read and also absent from
the template — they land in ALLOWLIST-SET, not here.)

**Framework** (everything else in the glob, checked against
`shared-memory/.env.example`):
```
CREDENTIALS_DIRECTORY, DOMAIN_CONFUSABLE_SIMILARITY, EMBED_RERANK_BUFFER_CAP,
EMBED_URL, ENTITY_CONFUSABLE_SIMILARITY, HOME, LLM_WEDGE_SUSPECT_AGE,
MAX_ENTITY_NAME_WORDS, MIN_ENTITY_NAME_LEN, MOCK_LLM, NEO4J_URI, NEO4J_USER,
NREM_INSIGHT_SLOT_INPUT_CHARS, NREM_TEMPERATURE, PATH, PG_CONN, PG_DATABASE,
PG_HOST, PG_MAINTENANCE_DB, PG_PORT, PG_USER, POOL_STATUS_URL,
PROJECT_CLOSE_MATCH_CUTOFF, PROJECT_CONFUSABLE_SIMILARITY, PROJECT_ROOTS,
REM_TEMPERATURE, SECURE_ENV_FILE, SECURE_ENV_SECRET_FILE_MAX_BYTES,
SMEM_ONTOLOGY_PATH, SM_PRE_UPDATE_VERSION, WRITE_QUIESCE_SEC, XDG_RUNTIME_DIR
```

## TEMPLATE-SET (what THIS PR adds)

**Client** (`shared-memory-skill/shared-memory/.env.example`):
```
SHARED_MEMORY_PROJECT, PROJECT_ROOT_MARKERS
```

**Framework** (`shared-memory/.env.example`):
```
PG_HOST, PG_PORT, PG_USER, PG_DATABASE, PG_MAINTENANCE_DB,
NEO4J_URI, NEO4J_USER, PROJECT_CLOSE_MATCH_CUTOFF, PROJECT_ROOTS
```
(the brief's "nine variables" — confirmed present in the mechanical
FRAMEWORK CATCH-SET above, all nine.)

## ALLOWLIST-SET (fixed by name in the brief)

```
SECURE_ENV_FILE  — process-env-only bootstrap selector, read BEFORE any .env
                   loads; it SELECTS which file loads, so a value written
                   into the file it would select is inert.
AGENT_ID         — deliberately untemplated; with gateway auth on, the server
                   overwrites the client's agent_id from the token, so
                   templating teaches an operator to set a name the server
                   ignores.
XDG_RUNTIME_DIR  — standard OS/session variable; the session sets it, never
                   the operator via this framework's own config files.
```

## THE ARITHMETIC — does CATCH = TEMPLATE ∪ ALLOWLIST?

**Client: YES, exactly.**
CATCH (client) = {PROJECT_ROOT_MARKERS, SHARED_MEMORY_PROJECT, SECURE_ENV_FILE,
AGENT_ID, XDG_RUNTIME_DIR} (5 names — the 2 the coverage test flags as
uncovered, plus the 3 the allowlist explicitly carves out).
TEMPLATE ∪ ALLOWLIST (client) = {SHARED_MEMORY_PROJECT, PROJECT_ROOT_MARKERS}
∪ {SECURE_ENV_FILE, AGENT_ID, XDG_RUNTIME_DIR} = the same 5 names. **Balances.**

**Framework: NO — it does NOT balance.**
CATCH (framework) has **31 names**. TEMPLATE ∪ ALLOWLIST (framework) covers
only **9** of them (the D5 nine — XDG_RUNTIME_DIR is already counted on the
client side above but is read framework-side too, by hive_mind_proxy.py; it
is still just one allowlist entry, not double-counted). **22 framework names
are read by the code, absent from `shared-memory/.env.example`, and named by
NEITHER the brief's TEMPLATE-SET NOR its ALLOWLIST-SET:**

```
CREDENTIALS_DIRECTORY, DOMAIN_CONFUSABLE_SIMILARITY, EMBED_RERANK_BUFFER_CAP,
EMBED_URL, ENTITY_CONFUSABLE_SIMILARITY, HOME, LLM_WEDGE_SUSPECT_AGE,
MAX_ENTITY_NAME_WORDS, MIN_ENTITY_NAME_LEN, MOCK_LLM, PATH, PG_CONN,
POOL_STATUS_URL, PROJECT_CONFUSABLE_SIMILARITY, REM_TEMPERATURE,
NREM_TEMPERATURE, SECURE_ENV_SECRET_FILE_MAX_BYTES, SMEM_ONTOLOGY_PATH,
SM_PRE_UPDATE_VERSION, WRITE_QUIESCE_SEC, NREM_INSIGHT_SLOT_INPUT_CHARS
```
(21 listed; XDG_RUNTIME_DIR is the 22nd, already allowlisted globally by name
— so 0 additional action needed for it, leaving 21 genuinely unaccounted-for.)

## RULING APPLIED: STOP AND REPORT, DO NOT ALLOWLIST THE REMAINDER

Per the brief's own PART 3 instruction — *"CATCH must equal TEMPLATE ∪
ALLOWLIST, exactly. If it does not balance, stop and report — do not
allowlist the remainder to make it balance"* — **the framework side does not
balance, and nothing further is added to either set to force it to.** T1 is
built to the correct, comprehensive mechanism the brief specifies (AST, full
glob, fail-closed), and it stays honestly RED on these 21 names after this
PR's fix. See the build report's "FINDING" section for the per-variable
classification (OS/systemd-supplied vs. test/internal-only vs. genuine
undocumented tuning knob) — each needs an individual operator decision this
builder is not authorized to make unilaterally, exactly as SECURE_ENV_FILE
and AGENT_ID needed one in D4.

Two smaller, separate mechanical disagreements, also reported rather than
silently resolved:

- **D5 says "eight scripts"; the mechanical PG_HOST-family reader count is
  seven**: `backfill_domain_of.py`, `backfill_project_of.py`,
  `backfill_promote_grounded.py`, `reconcile_project_edges.py`,
  `reconcile_project_identity.py`, `sync_project_registry.py`,
  `migrations/verify_schema_init.py`. No eighth PG_HOST-family reader was
  found by `grep -rli 'pg_host' shared-memory/scripts/*.py
  shared-memory/migrations/*.py` or by the AST extractor. The framework
  `.env.example` comment block added in Phase 3 names these seven, not eight.
- Of those seven, only three additionally read `NEO4J_URI`/`NEO4J_USER`
  (`backfill_project_of.py`, `reconcile_project_edges.py`,
  `reconcile_project_identity.py`); `PG_MAINTENANCE_DB` is read by exactly
  one (`migrations/verify_schema_init.py`). The added comment block states
  this precisely per-key rather than claiming uniform eight-way coverage.

---

## ROUND 2/3 RESOLUTION — the framework side now balances

Round 1 (above) left the framework side unbalanced by 21 names, per its own
"stop and report" instruction. The operator ruled on all 21 across two
follow-up rounds:

**Round 2 — 16 TEMPLATED** (see `W7_KNOBS.md` for the full evidence table
the ruling was made from — default, `file:line`, what each controls, and
whether a measured/derived input already exists for it):
`DOMAIN_CONFUSABLE_SIMILARITY`, `ENTITY_CONFUSABLE_SIMILARITY`,
`PROJECT_CONFUSABLE_SIMILARITY`, `MIN_ENTITY_NAME_LEN`,
`MAX_ENTITY_NAME_WORDS`, `REM_TEMPERATURE`, `NREM_TEMPERATURE`,
`NREM_INSIGHT_SLOT_INPUT_CHARS`, `WRITE_QUIESCE_SEC`,
`EMBED_RERANK_BUFFER_CAP`, `LLM_WEDGE_SUSPECT_AGE`,
`SECURE_ENV_SECRET_FILE_MAX_BYTES`, `SMEM_ONTOLOGY_PATH`, `EMBED_URL`,
`POOL_STATUS_URL`, `PG_CONN`. `PG_CONN` is presence-only in the default-value
test (its code default is credential-bearing — see its own section in
`shared-memory/.env.example` and the test's docstring); `SMEM_ONTOLOGY_PATH`
and `EMBED_URL` are also presence-only (computed / no-longer-literal
defaults respectively — `EMBED_URL`'s own default changed under round 3's
C1 fix, below).

**Round 2 — 5 ALLOWLISTED**, each with a falsifiable reason (why templating
is wrong, not merely unnecessary): `HOME`, `PATH` (OS-supplied), `CREDENTIALS_DIRECTORY`
(systemd `LoadCredential=`-supplied), `MOCK_LLM` (test-only switch,
`rem_loop.py:1115`), `SM_PRE_UPDATE_VERSION` (internal parent-to-child
handoff, `update_framework.sh` to `migrate_env.py`).

**Round 3 corrections:**
- **C1** — `migrate_retro_edges.py`'s `EMBED_URL` no longer has a literal
  code default; it now derives from `GATEWAY_URL.rstrip("/") + "/v1/embeddings"`
  by default, and `EMBED_URL` itself is read only as a one-release deprecated
  override (warned to stderr when set). Updated in both
  `shared-memory/.env.example` and this test's default-assertion (moved from
  the literal-default dict to the presence-only check, alongside
  `SMEM_ONTOLOGY_PATH`).
- **C4** — `AGENT_ID`'s allowlist reason (in the CLIENT set, not this
  framework set) was corrected: it is real, documented, operator-facing
  config for the MCP client (`mcp/system-prompt.md:158`), excluded from the
  CLI skill's `.env.example` specifically because gateway auth overrides it
  (verified: `coordinator.py:7717`,
  `agent_id = request.get("authenticated_agent") or body.get("agent_id", ...)`),
  not because it is obscure or internal.

**Final result: the framework side now balances.** Round 1's mechanical
CATCH-SET (31 framework names absent from the template) is now fully
accounted for — 9 from round 1's own D5 fix, 16 templated in round 2 (one of
which, `EMBED_URL`, changed shape again under round 3's C1 fix), and 5
allowlisted in round 2 (`XDG_RUNTIME_DIR` is read on both the client and
framework sides but is one allowlist entry, not counted twice). Rather than
re-deriving the arithmetic by hand here a second time — exactly the kind of
manual bookkeeping that drifts — the test itself is the authority:
`test_env_example_covers_every_variable_read` (the actual CATCH ⊆ TEMPLATE ∪
ALLOWLIST check) is **GREEN**, verified by running it (4/4 tests pass in
`tests/test_env_example_covers_every_variable_read.py`). See the full exit-gate
run for the final, current counts.
