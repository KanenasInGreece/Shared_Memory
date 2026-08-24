# HANDOFF — fix/uninstall-that-can-say-it-failed

Fixes the five defects in the measured cascade (corpus `fact:1515`): F3/F5/F4 in
`uninstall_framework.sh`, F7/F8 in `init_db.sh`, F6 (mintlock) in
`uninstall_framework.sh`. Branch `fix/uninstall-that-can-say-it-failed`, one commit.
No version bump, no CHANGELOG, no `sync_skills.sh` run (builder scope).

Files touched: `shared-memory/scripts/uninstall_framework.sh`,
`shared-memory/scripts/init_db.sh`, three new test files. `install_framework.sh`,
`AGENTS.md`, `README.md` **not touched** — nothing in them describes the uninstall
behavior in enough detail to go stale, and `init_db.sh`'s "idempotent" claim in
both docs stays true (the new check adds a verification step, doesn't change
what it applies or how many times it's safe to run).

## Fix 1+2+3 (F3/F5/F4) — `compose_down_and_verify()`

New function, marker-wrapped `# >>> COMPOSE_DOWN_AND_VERIFY` / `# <<<` around
line 257 of `uninstall_framework.sh` (replaces the old 4-line block that piped
`down`'s output through `tail -3`, discarding the exit code).

**Env-file present (the normal case):** `docker compose -f "$COMPOSE_FILE"
--env-file "$ENV_FILE" down -v` — same shape the install side already prints
(`install_framework.sh:178`, `preflight.sh:354`, `AGENTS.md`). This alone fixes
the measured cascade, since in the actual incident `.env` was still present at
this point (it gets removed later in the script).

**Env-file absent — the "re-run after partial uninstall" fallback.** The
compose file (`shared-memory/ops/postgres_neo4j_limits.yaml`) requires
`NEO4J_HOST_DIR` / `PG_DATA_DIR` to interpolate at all
(`${VAR:?set ... in shared-memory/.env}`), and that requirement is checked by
compose's config parser for *every* subcommand including `down`, not just `up`
— so `down` cannot even start without some value for those two keys. The
fallback supplies explicit dummy values (`/uninstall-env-file-missing`) for
**only those two keys**, nothing else. This is safe, not a workaround, because:
`down -v` never mounts or reads bind-mount paths, and this compose file
declares **no top-level `volumes:` key at all** — every volume in it is an
inline bind mount, so `-v` has no named Docker volume of its own to remove
either way. The real data directories are removed separately, later in the
script, by `remove_data_dir()`, using the *actual* `NEO4J_HOST_DIR`/`PG_DATA_DIR`
values read from `.env` while it still existed (`env_get()`, near the top of
the script) — the dummy values in the fallback never touch that code path.

Containers are addressed by their pinned `container_name:` (compose's
`name: shared-memory` project field also stays fixed regardless of cwd), so an
env-less `down` still finds and stops/removes the real containers; it just
can't know the real data paths, which it was never going to touch anyway.

**Exit code (F5):** checked directly (`down_rc=$?`), no pipe. On failure:
prints the last 10 lines of compose's own stderr/stdout and returns 1 —
nothing below it runs, so data directories and `$ENV_FILE` stay untouched.

**Post-condition verification (F4):** after a 0 exit, `docker ps -a --format
'{{.Names}}'` is checked against the container names discovered by grepping
`container_name:` out of `$COMPOSE_FILE` itself (not a hardcoded copy that
could drift — `test_container_list_is_read_from_the_compose_file_not_hardcoded`
pins this against the *shipped* file too: `neo4j-memory`, `postgres-vector`,
plus the two encoder pairs). Any survivor is named in red output and the
function returns 1. Only on a **verified-empty** `docker ps -a` does it print
`✓ compose stack down, verified gone` and return 0 — this exact string is the
only thing that unblocks the rest of the `data`/`all` teardown.

The caller (`echo "Removing containers and volumes ..."` onward) now does:
```
if ! compose_down_and_verify; then
    ... "Uninstall INCOMPLETE" ... ; exit 1
fi
```
replacing the old unconditional `grn "✓ compose stack down, volumes removed"`.

## Fix 5 (F6) — mintlock removal, both candidate paths

`bootstrap_tokens.sh`'s `_LOCKFILE="${ENV_FILE}.mintlock"` had no cleanup
anywhere. Added removal alongside the existing `$ENV_FILE` removal (`data`/`all`
only — `service` exits before reaching this code, so it never touches either
file, matching the existing "service is fully reversible" contract).

**One thing I found and fixed beyond the literal ask:** `ENV_FILE` resolution
(`shared-memory/.env`, falling back to repo-root `.env` if the first doesn't
exist) is *dynamic* — it re-evaluates based on which file currently exists. On
a genuine re-run after a partial uninstall (first run removed `.env` but died
before reaching mintlock, or removed it in an earlier version of this script),
`$ENV_FILE` on the second run silently resolves to the *other* candidate, and
a mintlock stranded next to the now-gone first candidate would never be found.
Fixed by introducing `_ENV_CANDIDATES=("$REPO_ROOT/shared-memory/.env"
"$REPO_ROOT/.env")` near the top (same two paths, same order, as
`apply.py`'s `_load_env()` and every other loader in this project) and looping
over **both** candidates for mintlock removal (and the "WILL BE REMOVED"
inventory display), independent of which one `$ENV_FILE` currently resolves
to. `test_partial_uninstall_rerun_env_already_gone_mintlock_still_cleared`
covers exactly this. `$ENV_FILE` itself is untouched — still resolves the same
way it always did for the actual `.env` removal and every other use.

## Fix 4 (F7/F8) — `authenticated_connectivity_check()` in `init_db.sh`

New functions, marker-wrapped `# >>> AUTHENTICATED_CONNECTIVITY_CHECK` /
`# <<<`, placed **after** both the Postgres schema apply and the Neo4j
constraint apply (per the build brief's explicit ordering — not interleaved
with the v0.9.44 `SCHEMA_PREEXISTENCE`/`ADOPT_LEDGER`/`LEDGER_GATE_DECISION`
gate, which is untouched). Right before the final `Both stores initialised`
line, which now only prints if both checks pass.

**What it connects to and how (the part that needed investigation).**
Everything else in `init_db.sh` that touches Postgres runs `docker exec ...
psql -U postgres` with **no `-h`**, which resolves to a Unix-domain-socket
connection inside the container. Postgres's own `pg_hba.conf` (written by the
official image's entrypoint) routes a `local` (socket) connection through
`trust` — no password checked, ever — and routes a `host` (TCP) connection
through whatever `POSTGRES_HOST_AUTH_METHOD` the image started with (default
`scram-sha-256`, since this compose file never overrides it). Adding **just
`-h 127.0.0.1`** to a `docker exec ... psql` call routes it through the TCP
loopback *inside the container*, hitting the password-checked `host` rule —
the exact authentication class the gateway itself is subject to when it
connects from the host to the published port, without needing any new
dependency (no psql/psycopg2/`uv` on the host — same `docker exec` idiom this
file already uses throughout):
```
PGPASSWORD="$PG_PASSWORD" docker exec -e PGPASSWORD -i "$PG_CONTAINER" \
    psql -q -t -A -h 127.0.0.1 -U postgres -d "$PG_DB" -c "SELECT 1"
```
`PG_PASSWORD` is newly read from `.env` (`read_env PG_PASSWORD`, refused if
empty, mirroring the existing `NEO4J_PASSWORD` check) — it was never read at
all before this fix.

I looked at `verify_schema_init.py` / `apply.py` (host-side `psycopg2`, `uv
run --with psycopg2-binary`) and `verify_neo4j_init.py` (host-side `neo4j`
driver, `uv run --with neo4j`) as the two established host-facing patterns.
Went with the `docker exec -h 127.0.0.1` route instead of either: it adds
**zero** new dependencies to a script whose whole design point (its own
docstring) is "the host needs neither psql nor cypher-shell" — pulling in
`uv`+`psycopg2-binary` here would contradict that, and would also introduce a
"what if `uv` is absent" branch that the build brief didn't ask for and that
would have to decide, arbitrarily, whether a *missing test tool* counts as an
auth failure. `docker exec` is unconditionally required already (line 68).

**Neo4j did not have this gap.** `cypher-shell` (used everywhere else in this
file for Neo4j) has no unauthenticated mode — it always connects over bolt
with real credentials (`NEO4J_PASSWORD`, exported and forwarded the same way
this fix forwards `PGPASSWORD`). A stale Neo4j data directory already fails
loudly today, mid-script, during "Applying neo4j_init.cypher". I added
`neo4j_authenticated_check()` anyway, calling the same `cypher-shell -u neo4j
"RETURN 1"` already used by the readiness-wait loop just above it, so that:
(a) both stores are verified by the same explicit, equally-worded mechanism
rather than one being correct only as a side effect of unrelated work, and
(b) a Postgres-only failure and a Neo4j-only failure both name which store
failed (`test_one_store_failing_still_reports_that_store_by_name`).

**Failure wording** (pinned by value in tests) names the likely cause and
points away from editing credentials:
> `✗ Postgres REFUSED this .env's credentials over the password-checked
> connection the gateway will actually use to reach it.`
> `Likely cause: this data directory pre-existed this install — a previous
> cluster's credentials are still in force. ... This is a data-directory
> problem, not a credentials problem — do not edit the .env to make this
> pass. ... or clear the stale data first: uninstall_framework.sh --level data`

Exits 1 on either store failing (`_auth_failures` counter, both checks always
run so a Postgres failure doesn't hide a Neo4j one), before the final success
line.

## Tests + mutation evidence

`tests/test_uninstall_compose_down.py` (10 tests) — extracts
`compose_down_and_verify()` between its markers, PATH-stubbed `docker`
(argv-recording, scriptable `down` exit code / output and `ps -a` output via
env vars `DOCKER_DOWN_RC`/`DOCKER_DOWN_OUT`/`DOCKER_PS_OUTPUT`). Mutation-killed:
- `test_env_file_present_down_is_invoked_with_env_file_and_compose_file` —
  died when `--env-file "$ENV_FILE"` was stripped from the down invocation
  (line ~300). Confirms fix 1.
- `test_down_failure_is_not_reported_as_success_and_exits_nonzero` — died when
  the `if [[ "$down_rc" -ne 0 ]]` check (line 315) was replaced with `if
  false`. Confirms fix 2's guard is load-bearing.
- `test_leftover_container_after_a_clean_exit_is_named_and_fails`,
  `test_two_leftover_containers_are_both_named`,
  `test_container_list_is_read_from_the_compose_file_not_hardcoded` — all
  three died when the `if [[ ${#leftover[@]} -gt 0 ]]` check (line 328) was
  replaced with `if false`. Confirms fix 3's guard.
- Restored from scratchpad backup after each mutation; `git status` /
  `git diff --stat` confirmed identical to the pre-mutation state each time.

`tests/test_uninstall_mintlock.py` (5 tests) — runs the **full script**
non-dry-run at `--level data`/`all`/`service` (safe: the fixture never creates
a systemd unit file, so the script's own `[[ ! -f "$UNIT_PATH" ]]` guard skips
`systemctl` entirely before it could reach the real session bus — the exact
hazard `test_uninstall_guards.py`'s own comment documents; `docker` is
PATH-stubbed to a no-op success). Mutation-killed:
- `test_level_data_removes_the_mintlock_alongside_the_env_file`,
  `test_level_all_also_removes_the_mintlock`,
  `test_partial_uninstall_rerun_env_already_gone_mintlock_still_cleared` — all
  three died when the `for _cand in ...; do rm -f ...; done` loop body (line
  ~414) was replaced with a no-op `:`. Confirms fix 5, including the
  both-candidates fix (the third test specifically exercises the candidate
  the naive single-`$ENV_FILE`-check version would have missed).
- `test_level_service_never_touches_the_env_file_or_the_mintlock` and
  `test_no_mintlock_present_is_not_an_error` correctly stayed green under the
  same mutation (they assert *absence* of removal in scenarios where nothing
  should be removed either way) — expected, not a gap.

`tests/test_init_db_authenticated_check.py` (10 tests) — extracts all three
`AUTHENTICATED_CONNECTIVITY_CHECK` functions, PATH-stubbed `docker` answering
by matching `SELECT 1` / `RETURN 1` in the logged argv, exit codes via
`PG_AUTH_RC`/`NEO4J_AUTH_RC`. Mutation-killed:
- `test_pg_check_success_reports_authenticated` — died (assertion on `-h
  127.0.0.1` in the logged argv) when `-h 127.0.0.1` was removed from
  `pg_authenticated_check()`, reverting it to the same peer-trust path every
  other Postgres call in this script uses. This is the mutation that matters
  most: it proves the test would have caught the *original* F8 defect (an
  authenticated-check that silently isn't).
- `test_both_checks_are_invoked_after_neo4j_constraints_and_gate_the_final_message`
  — died when the two `authenticated_connectivity_check "Postgres"
  .../ "Neo4j" ...` call lines (~329-330) were replaced with a no-op. Confirms
  the checks are actually wired into the script, not just defined.
- Restored from scratchpad backup after each; verified via `git status`.

Full suite from repo root: **2541 passed, 1 skipped** (baseline was 2516
passed / 1 skipped; +25 new tests, 0 regressions). `shared-memory/.env` does
not exist in this worktree at all (gitignored, never present), so the mtime
canary is trivially satisfied — nothing was read, written, or executed
against it.

## Second commit — review integration (Ops-14, Critical)

The Ops & Release Integrity review found the post-condition verification
itself FAILS OPEN: `mapfile -t containers < <(grep -E '^[[:space:]]*container_name:'
"$COMPOSE_FILE" | sed ...)` yields an **empty array** whenever the compose
file's syntax changes, it's renamed, or it's simply missing by the time this
runs — the leftover-detection loop then iterates zero times, finds zero
leftovers, and the function prints `✓ compose stack down, verified gone`.
That is the exact unearned-checkmark class this whole branch exists to
remove (F4/F5's original shape: a green result nobody actually measured),
reintroduced one layer up, inside the very function that was supposed to
close it.

**Fix:** immediately after the `mapfile`, in `compose_down_and_verify()`
(`shared-memory/scripts/uninstall_framework.sh`, ~line 321), an empty
`$containers` array is now refused as a verification **failure**, not read
as "nothing to check":
```
if [[ ${#containers[@]} -eq 0 ]]; then
    red "  ✗ could not parse any container_name: entries from $COMPOSE_FILE --"
    red "    the teardown CANNOT be verified. This does not mean nothing is"
    red "    running: it means this function has no list to check docker ps"
    red "    against. Check by hand:"
    red "        docker ps -a"
    return 1
fi
```
Returns 1, which flows into the same failure path a failed `down` already
uses (the caller's `if ! compose_down_and_verify; then ... exit 1; fi` — no
new branch needed there). No fallback heuristic (guessing container names
from the image list, or similar) — an honest "cannot verify" beats a clever
guess, per the ruling.

**Tests** (`tests/test_uninstall_compose_down.py`, +2, now 12 in that file):
- `test_empty_container_list_refuses_to_claim_success` — (a) from the
  ruling: a compose fixture with zero `container_name:` lines
  (`_NO_CONTAINER_NAMES_COMPOSE`) must produce an absent success line, the
  "could not parse any container_name" / "CANNOT be verified" / `docker ps
  -a` wording present (pinned by value), and a nonzero return — and confirms
  the down itself DID run first (this is a verification failure, not a down
  failure).
- `test_empty_container_list_is_distinct_from_a_leftover_failure` — the
  "STILL PRESENT" leftover wording must not appear, so an operator can tell
  the two failure modes apart.

**Mutation evidence:** scratchpad-backed up `uninstall_framework.sh`
(`cp` to the scratchpad dir, never `git checkout --`), replaced the new
`if [[ ${#containers[@]} -eq 0 ]]; then` with `if false; then`, reran
`tests/test_uninstall_compose_down.py`: exactly
`test_empty_container_list_refuses_to_claim_success` died (asserted
`returncode != 0`, got the old unconditional success line and exit 0); all
11 other tests in that file — including
`test_empty_container_list_is_distinct_from_a_leftover_failure`, every
leftover-container test, and every success-path test — stayed green, as the
ruling specified. Restored via `cp` from the scratchpad backup;
`git status`/syntax-checked clean afterward.

**Fallout caught and fixed:** `tests/test_uninstall_mintlock.py`'s fixture
compose file (`postgres_neo4j_limits.yaml` written by its own `_fake_install`)
previously declared `services: {}` — zero `container_name:` entries. With
the new guard in place this correctly started refusing that fixture's
teardown as unverifiable, which made all four of that file's non-dry-run
scenarios fail before ever reaching the mintlock-removal code (an honest
regression, not a flaky one — the guard was doing exactly its job against a
fixture that no longer matched reality). Fixed by giving that fixture's
compose file two real `container_name:` entries
(`neo4j-memory`/`postgres-vector`, matching its already-empty `DOCKER_PS_OUTPUT`
stub so no leftover is found), restoring all 5 of that file's tests to green
without touching its actual mintlock-removal assertions.

**Suite:** full run from the worktree root — **2543 passed, 1 skipped**
(prior state after commit 1: 2541 passed/1 skipped; +2 new tests, 0
regressions once the mintlock fixture was updated).

## Nothing proposed for README.md

Checked: README.md and AGENTS.md mention `init_db.sh` only as "idempotent" /
"applies schema" (both still true) and never mention `uninstall_framework.sh`
at all. Nothing in either document needed a wording change.

## For the merger

- Version bump (patch), CHANGELOG entry, `sync_skills.sh` (not needed here —
  no skill/client-surface files touched) are all yours per scope.
- Consider whether the fix-4 Neo4j check (fully redundant with existing
  behavior, added for symmetry/explicitness) is worth keeping vs. trimming —
  I judged it worth the ~15 lines for consistent operator-facing wording and
  test coverage parity between the two stores, but it's a judgment call, not
  a forced one.
- The `_ENV_CANDIDATES` array refactor in `uninstall_framework.sh` (top of
  file) is slightly wider than a literal reading of fix 5 required — flagging
  it explicitly since it changes how `ENV_FILE` itself is *constructed*
  (same two paths, same order, same resulting value — just now built from an
  array `_ENV_CANDIDATES[0]`/`[1]` instead of two bare literals). Worth a
  second look; I don't believe it changes `$ENV_FILE`'s value in any scenario,
  only what mintlock-removal additionally checks.
