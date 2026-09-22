# AGENTS.md

Coding agents in this checkout. Install, update, and uninstall are [`OPERATE.md`](OPERATE.md). A coding session does not run Phase 1, does not pipe empty passwords, and does not run `postflight.sh`.

Architecture and Quick Start: [`README.md`](README.md). Do not clone it.

## Commands

From the repo root. The suite is mocked. No live database. One-test flags are README §23. The pre-PR suite is the `CONTRIBUTING.md` block (those flags plus `--with numpy`).

| Action | Command |
|---|---|
| One test | `uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx --with neo4j --with asyncpg --with aiohttp --with json-repair pytest tests/test_derived_belonging.py::test_the_derivation_writes_nothing -v` |
| Before a PR | `uv run --with pytest --with pytest-asyncio --with fastmcp --with psycopg2-binary --with httpx --with neo4j --with asyncpg --with aiohttp --with json-repair --with numpy pytest tests/ -v` |
| Locked env | `uv venv && uv pip sync requirements.lock` then `uv pip install -r requirements-dev.txt` |
| GitGuard | `bash Local_Documentation/gitguard/check.sh` |
| Where to work | Work in a worktree. Do not commit, pull, or reset in the live checkout. On a public clone, branch in your fork. |

Replace the node id to cover a different edit. Same `--with` list as the one-test row. The one test is not the only gate.

GitGuard runs only when that script exists. It is gitignored local tooling. Expect `gitguard: ALL LAYERS PRESENT`. A gap means do not dispatch builders. A public clone has no script. Do not invent one.

Gateway start, stop, and backup live in `OPERATE.md`. Do not paste them here.

## Layout

- `shared-memory/scripts/` — gateway and helpers.
- `shared-memory/ops/` — compose file and the systemd unit.
- `shared-memory/migrations/` — Postgres ledger. Neo4j has none.
- `shared-memory-skill/shared-memory/MANIFEST.txt` — files a skill install ships.
- `mcp/` — connector. `tests/` — mocked suite.
- `Local_Documentation/` — gitignored. Not required to build or to install.

## Conventions

Match the file you edit. No linter is enforced. A comment says why, and only when that is not obvious.

Embed and rerank go through the gateway on `:8888`. These four are the `CONTRIBUTING.md` bar:

- A save aborts when the gateway is unreachable. A row with no vector is invisible.
- `pg_notify` fires in the same transaction as the INSERT, before commit.
- Entities are optional and never gate Tier-3 consolidation. Do not invent them.
- `ON CONFLICT (content_hash) DO UPDATE` stays. Re-saving the same content is safe.

## Always

- Use the `uv` commands above. Run the one test that covers the edit while working. Run the full `CONTRIBUTING.md` suite before a PR.
- Keep the diff to the task. Match the surrounding file.
- When changing an install helper, drive the script. Do not mirror it. The empty password pipes are first-install only and live in `OPERATE.md`.

## Ask first

- New dependencies, lockfiles, public API, auth, CI, or compose image pins.
- Migrations, `bootstrap_tokens.sh --force`, `ops/restore.sh`, data-dir deletion, `reconcile_stack.sh` (it recreates database containers), VERSION, CHANGELOG, or a release. The repository owner merges.
- Editing `README.md`. Setup commands stay aligned with Quick Start. Do not rewrite README from a coding task.

## Never

- Generate the two database passwords, or any API key, in your shell (fact:1499). The install script generates passwords in its own process. That procedure is not a coding-session step.
- `cat`, `grep`, or `.` a `.env`, or put a token in the transcript. The operator runs `--reveal` in their own terminal. Confirm ignore with `git check-ignore shared-memory/.env`.
- Commit `.env`, tokens, credentials, or customer data.
- Call `:8070` or `:8071`. Send that traffic to `:8888`.
- Commit, pull, or reset in the live checkout. Do not add `Co-Authored-By` or `Generated-with`.
- Paste install, update, or uninstall procedures into this file.
- Invent APIs, config keys, or results for tests you did not run.

## Git

Feature branch, not `main`. PR against `main`. No direct push. Apache-2.0. No GPL dependency without asking. Work in a worktree. On a public clone, branch in your fork. Do not commit, pull, or reset in the live checkout.

## Load when needed

- `README.md` — architecture and Quick Start.
- `OPERATE.md` — install, update, and uninstall.
- `CONTRIBUTING.md` — review bar, the four invariants, and the pre-PR suite.
- `AGENT.md` — pointer only. Code is this file. Install is `OPERATE.md`. Architecture is `README.md`.
- `shared-memory/Documentation/schema.md` — persistence changes.
- `shared-memory/Documentation/postflight.md` — install proof. A coding session does not run it.
