# HANDOFF — fix/first-upgrade-fresh-install

Builder run implementing four ruled fixes from today's measured upgrade-test findings
(corpus `fact:1511` / `fact:1512`). Branch `fix/first-upgrade-fresh-install`, one commit,
built from `main` @ `0c44cd1` (v0.9.42). Full suite: 2506 passed / 1 skipped (baseline
2488 passed / 1 skipped — 18 new tests, all additions, nothing removed).

## Fix A — fresh installs end with a populated migration ledger

**Where it landed:** `shared-memory/scripts/init_db.sh`, immediately after the
`✓ Postgres schema applied` line and before the Neo4j section begins.

**Why there, not `install_framework.sh`:** `install_framework.sh` never runs
`init_db.sh` itself — it only prints the command for the operator to run afterward
(`Initialise both schemas: bash shared-memory/scripts/init_db.sh`). `init_db.sh` is the
one script that actually creates `schema_init.sql`'s tables, so it is the only place
that knows, first-hand, that a fresh schema was just born — which is exactly the
"vouchable" condition the fix requires (auto-adopt only immediately after THIS run
created the schema, never for an unknown pre-existing database).

**Mechanism gap it crosses, and how:** the rest of `init_db.sh` runs both DB clients
*inside* the compose containers via `docker exec` (documented in the file's own header)
so the host needs neither `psql` nor `cypher-shell`. `apply.py` instead connects **out**
via `psycopg2` from the host (matching how `update_framework.sh` already invokes it) —
so the new `adopt_ledger()` function runs on the **host**, via `uv run --with
psycopg2-binary`, not via `docker exec`. This works because
`ops/postgres_neo4j_limits.yaml` publishes Postgres to `127.0.0.1:5432` by default, and
`apply.py`'s own default DSN targets exactly that.

**Idempotency / safety:**
- `apply.py --adopt` is unconditionally idempotent — it always
  `INSERT ... ON CONFLICT (filename) DO NOTHING` for every migration file and returns 0,
  regardless of whether the ledger is empty or already populated. Verified by reading
  the `if adopt:` branch (`shared-memory/migrations/apply.py`); no new guard was needed.
- `adopt_ledger()` degrades to a **warning, never a failure**, when `uv` is absent from
  PATH or when `apply.py --adopt` itself fails (e.g. Postgres unreachable from the host)
  — the Postgres/Neo4j schema work has already succeeded by that point; ledger adoption
  is a convenience layered on top, not a new hard requirement of `init_db.sh`.
- `apply.py`'s stance toward a database it did **not** just create is unchanged —
  nothing calls `--adopt` automatically for any other path.

**Known limitation, not fixed here (pre-existing, out of scope):** `apply.py` does not
read `PG_DB`/`PG_CONTAINER` overrides for its own DSN — only `PG_CONN`/`PG_PASSWORD`. An
operator who overrides `PG_DB` away from `agent_data` when running `init_db.sh` needs to
also set `PG_CONN` for `adopt_ledger()`'s `apply.py --adopt` call to reach the right
database. This limitation already existed for `update_framework.sh`'s own `apply.py`
call; not introduced or worsened by this fix.

**Test:** `tests/test_init_db_ledger_adoption.py` (7 tests, new file). Extracts the
`adopt_ledger()` function body between `# >>> ADOPT_LEDGER` / `# <<< ADOPT_LEDGER`
markers (same idiom as `tests/test_install_service_linger.py`'s `enable_linger()`
extraction) and runs it standalone with a PATH-stubbed `uv`. Notably had to solve a
determinism trap: on this workstation `uv` is symlinked into `/usr/bin`, the same
directory as `bash` — an earlier draft that added `dirname(bash)` to PATH to satisfy the
stub's `#!/usr/bin/env bash` shebang silently leaked the REAL `uv` back in for the
"uv absent" scenario. Fixed by giving the stub script an absolute-path shebang
(`#!{bash_bin}`) instead, so PATH can stay exactly `bin_dir` (with or without the stub)
in every scenario — no directory needs adding to make `uv` resolvable-or-not
deterministically.

- `test_the_function_is_actually_called_after_schema_applied` — pins that the shipped
  script actually calls `adopt_ledger` after schema success (not merely defines it), and
  before Neo4j begins.
- `test_uv_present_and_adopt_succeeds_reports_success` / `..._fails_warns_but_does_not_fail_the_script`
  / `test_uv_absent_warns_and_names_the_manual_command` — the three outcome branches.
- `test_header_documents_the_new_step` — the file's own header prose mentions the ledger.

**Mutation check:** commented out the `adopt_ledger` call line in `init_db.sh` (kept the
function definition). `test_the_function_is_actually_called_after_schema_applied` died;
the other 6 tests in the file stayed green (they test the function in isolation, proving
the mutation coverage is specifically on the CALL, not the function body). Restored via
scratchpad backup copy-back (never `git checkout --`); `git status` confirmed clean
(back to the pre-mutation modified state) after restore.

## Fix B — `apply.py`'s exit-2 message names both origins

**Where:** `shared-memory/migrations/apply.py` — the module docstring's "ADOPTING AN
EXISTING DATABASE" section, the real refusal path (`needs_adoption(...)` branch inside
`main()`, non-`--status`), and the `--status` advisory printout. All three now state
plainly that a database created by `schema_init.sql` (the fresh-install case) is a
second, common origin for this state — not just a pre-v0.8.35 backup — and that it is
"exactly the case you CAN vouch for." Also touched `update_framework.sh`'s own `die()`
text at the equivalent point (it printed a second, narrower copy of the same
explanation) so it does not contradict the corrected `apply.py` message printed just
above it.

**Test:** `tests/test_migration_ledger.py`, three new tests using the file's existing
`_run_main()` stub-connection harness (`_FakeConn`/`_FakeCursor`, no real database):
- `test_the_refusal_message_names_the_fresh_install_origin` — real (non-`--status`)
  path, `rc == 2`, asserts both origins present on stderr AND that the OLD single-origin
  framing ("This database predates migration tracking") no longer survives verbatim —
  pins the fix, not merely an addition alongside stale text.
- `test_the_status_advisory_also_names_the_fresh_install_origin` — same for `--status`'s
  stdout printout.
- `test_a_genuinely_ahead_database_still_refuses_before_adoption_wording` — control: the
  `rc == 3` (AHEAD) path must never carry this wording; it is a different state with a
  different fix.

**Mutation check:** `sed`-replaced the string `"A FRESH INSTALL"` inside the refusal
message with unrelated text. `test_the_refusal_message_names_the_fresh_install_origin`
died (the other two origin tests are on different code paths/strings and correctly
stayed green — only that message's own assertion should die from that mutation).
Restored from scratchpad backup; `git status` clean.

## Fix C — `--dry-run` stops enumerating once step 0 predicts a refusal

**Where:** `shared-memory/scripts/update_framework.sh`, step 0 (the pre-`git pull`
branch guard). `pull_blocked` is now declared once, before the `.git`-vs-tarball branch
(previously only declared inside the `.git` branch, leaving the tarball route's own
`refuse()` unable to signal it — fixed the tarball branch's `|| true` to `|| pull_blocked=1`
too, for the same honesty reason, since it is the same code path and the same
finding). Immediately after the whole step-0 block, a new check:

```bash
if [[ "$pull_blocked" == "1" ]]; then
    echo
    red "✗ [DRY RUN] the real run stops here — step 0 would refuse (see above)."
    red "  Steps 1 onward are UNREACHABLE from this state and were not evaluated."
    exit 1
fi
```

This is reachable **only** under `--dry-run` — on a real run, `refuse()` calls `die()`
and the process has already exited before this line could ever be read. Matches the
KNOWN TRAP warning about `run()`'s DRY_RUN early return: this fix does **not** touch
`run()`/`run_soft()` at all — it prevents them from ever being called again after a
predicted refusal, by exiting the whole script first.

**This directly REVERSES a previously-intentional design** (RULING 1's old comment:
"let the rest of the dry run's step previews keep printing, exiting 0"), which two
existing tests asserted as correct behaviour. Both were rewritten in place (not left
alongside new ones) since they pinned the exact defect this fix closes:
`test_dry_run_no_upstream_predicts_refusal_and_stops_enumerating` and
`test_dry_run_detached_head_predicts_refusal_and_stops_enumerating` in
`tests/test_update_framework_branch_guard.py` — renamed, and now assert `returncode != 0`,
absence of `backfill_domain_of.py` (a later step) in the output, and absence of the
unconditional "Dry run complete" banner. Added a third refusal-state test
(`test_dry_run_remote_branch_deleted_predicts_refusal_and_stops_enumerating`, the RULING
A measured case) and a control (`test_dry_run_with_a_healthy_branch_still_enumerates_every_step`)
proving the truncation is scoped to an actual predicted refusal, not a side effect of
`--dry-run` itself.

### Fix C addendum (scope addition from a live measurement mid-build)

The coordinator flagged a second instance of the same defect class in the same file:
the domain-backfill step (step 6) labelled its dry-run preview "preview (enqueues
nothing)" and invoked `backfill_domain_of.py` **without** `--apply` — but a real run
always passes `--apply` by default (measured live: 318 outbox rows enqueued on one
deployment) unless `--no-domain-backfill` is given. Fixed by making the dry-run and
real-run branches invoke the **identical command** (both now carry `--apply`), with the
dry-run label rewritten to state plainly that a real run applies by default, what that
means (a write via the outbox), and the opt-out flag:

```
"domain backfill — a REAL run APPLIES this by default (writes domain rows via the
 outbox; pass --no-domain-backfill to skip it for one run)"
```

**Tests:** `tests/test_update_framework_no_domain_backfill.py`, two new tests —
`test_dry_run_preview_shows_the_apply_flag_a_real_run_would_use` and
`test_dry_run_preview_states_it_applies_by_default_and_names_the_opt_out`. A code
comment cross-references `test_update_framework_live_execution.py`'s existing
`test_live_run_without_flag_invokes_backfill_with_apply` (which independently pins the
REAL run's actual `--apply` invocation) rather than duplicating that assertion here.

**Mutation checks (two, both in `update_framework.sh`):**
1. Changed the guard condition from `"$pull_blocked" == "1"` to `"$pull_blocked" == "99"`
   (never true). All three refusal-state dry-run tests died
   (`test_dry_run_no_upstream_predicts_refusal_and_stops_enumerating`,
   `..._detached_head_...`, `..._remote_branch_deleted_...`); the control test
   (`test_dry_run_with_a_healthy_branch_still_enumerates_every_step`) and all other files
   stayed green. Restored from scratchpad backup; `git status` clean.
2. Reverted the domain-backfill dry-run branch back to the old label/command (dropped
   `--apply`, restored "preview (enqueues nothing)"). Both addendum tests died; all other
   tests in the file (flag-skip, step-numbering, `--from-restore` interaction) stayed
   green — proving the mutation coverage is specific to the dry-run command/label text,
   not a broad regression. Restored from scratchpad backup; `git status` clean.

## Fix D — detached-HEAD remedy names `main`; AGENTS.md documents it

**Where:** `shared-memory/scripts/update_framework.sh`'s detached-HEAD `refuse()`
message now reads *"This framework's release branch is main: run `git checkout main` to
return to it... (If you deliberately want a specific pinned tag instead of the moving
branch, check that tag out instead — or use the tarball route...)"* — replacing the old
generic "Check out the release branch or tag you intend to run" (which named no ref at
all). `AGENTS.md`'s `### Upgrade (gateway host)` section gained a matching paragraph
right after the existing tarball-route paragraph, explaining the likely cause (`git
checkout <tag>` right after cloning — a defensible reading of "install the release"
that this repo doesn't actually want) and the same recovery, `git checkout main`.

**README.md was NOT touched**, per the build brief. Proposed wording, for the merger to
weigh (README currently gives no tag-checkout instruction at Quick Start step 1, so
nothing there actively induces the detached-HEAD state — this is a defensive addition,
not a bug-in-README fix):

> *(Optional addition to README §3 Quick Start, step 1, after "Clone the repo...")*
> Stay on the default branch (`main`) after cloning — it **is** the release branch.
> Checking out a release tag directly leaves the repo on a detached HEAD, which
> `update_framework.sh` will refuse to `git pull` from later; `git checkout main`
> recovers it.

**Test:** `tests/test_detached_head_recovery_documented.py` (4 tests, new file).
Extracts the exact `refuse(...)` string literal from `update_framework.sh` via regex
(not hardcoded here) and the `### Upgrade (gateway host)` section of `AGENTS.md`, then:
- `test_the_refusal_names_main_as_the_concrete_remedy` — `git checkout main` present,
  tag alternative preserved.
- `test_the_old_unnamed_remedy_wording_does_not_survive_verbatim` — pins the fix, not an
  addition alongside stale text.
- `test_agents_md_documents_the_detached_head_case` — scoped to the Upgrade section
  specifically (not "anywhere in the file").
- `test_agents_md_and_the_script_name_the_same_remedy` — **cross-check**: both sides
  must name the identical ref, extracted independently from each source, so a future
  rename of the release branch can't update one and silently leave the other stale.

Also cross-pinned for free: `test_dry_run_detached_head_predicts_refusal_and_stops_enumerating`
(fix C's test file) asserts `"git checkout main" in out` on the live dry-run output, so
fix D is independently verified from two different test files reading two different
sources.

**Mutation checks (two, run separately):**
1. `update_framework.sh` side: `sed` replaced `git checkout main` with `git checkout
   MUTATED_wrong_ref`. `test_the_refusal_names_main_as_the_concrete_remedy` and
   `test_agents_md_and_the_script_name_the_same_remedy` died, as did the
   cross-pinned `test_dry_run_detached_head_predicts_refusal_and_stops_enumerating` in
   the branch-guard test file. Restored from scratchpad backup; `git status` clean.
2. `AGENTS.md` side: changed the recovery line's ref to `git checkout somewhere-else`.
   `test_agents_md_documents_the_detached_head_case` and
   `test_agents_md_and_the_script_name_the_same_remedy` died. Restored from scratchpad
   backup; `git status` clean.

## Test summary

18 new tests across 5 files (2 new, 3 extended), baseline 2488 passed / 1 skipped →
final 2506 passed / 1 skipped, all green, no regressions, no test deleted. Every guard
was mutation-checked individually (7 mutations total across the four fixes, see above),
each restored via scratchpad copy-back — never `git checkout --` — with `git status`
confirmed clean after each restore. No live database, docker, or service was touched at
any point; `shared-memory/.env` does not exist in this worktree at all (canary trivially
satisfied — nothing to move).

## For the merger

- Version bump, CHANGELOG, `sync_skills.sh` are explicitly NOT done here (builder scope
  excludes version files/CHANGELOG per the build-cycle workflow, `fact:1184`/`decision:1320`).
- The README wording above is a proposal only — weigh whether it's worth adding given
  README currently doesn't itself suggest checking out a tag.
- Fix A's `PG_DB`/`PG_CONTAINER` override limitation (noted above) is pre-existing in
  `apply.py`'s DSN resolution, not introduced by this change — flagging for awareness,
  not asking for a fix in this cycle.
- All four fixes are independent by file region within `update_framework.sh` (fix C
  touches step-0 + step-6; fix D touches only the detached-HEAD string inside step 0) —
  reviewed as one PR since they came from one measured session, per the brief.
