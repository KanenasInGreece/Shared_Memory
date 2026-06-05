---
description: Fact-check every documentation chapter against current code, then update local + GitHub docs
---

# /doc-audit — Documentation Fact-Check & Sync

Review **every chapter** of the documentation, verify each claim against the
current code, fix what has drifted, and propagate the result to both the
public (GitHub) docs and the local-only docs. Run this before any release, or
whenever code has outpaced the prose.

The code is the source of truth. When prose and code disagree, the code wins
unless the code is the bug — in which case stop and flag it, do not silently
"fix" the docs to describe a defect.

Argument (`$ARGUMENTS`): optional scope — a single file or README section
number (e.g. `13`, `README §13`, `SECURITY.md`). No argument = full sweep.

---

## Phase 0 — Prep

1. `git status` must be clean (or stash). Confirm current branch; if on `main`,
   create a branch `docs/audit-<date>` (all changes go through a PR — see
   `[[feedback-pr-workflow]]`).
2. Read the latest `CHANGELOG.md` version header to know the "current" version
   the docs should describe.
3. Pull prior context: `Skill(shared-memory)` → search `"documentation audit drift"`.

## Phase 1 — Build the chapter inventory

Documentation surface to audit (skip any not in `$ARGUMENTS` scope):

**Public — committed to GitHub:**
| Doc | What it must match |
|---|---|
| `README.md` §1–§20 + §10a, §11a | see the per-chapter matrix below |
| `CHANGELOG.md` | latest version reflects merged code; no unreleased drift |
| `SECURITY.md` | authoritative security history; matches auth/token code |
| `system-prompt.md` | gateway behaviour + agent contracts |
| `shared-memory/Documentation/schema.md` | Postgres columns + Neo4j labels/rels |
| `AGENT.md` / `AGENTS.md` / `CONTRIBUTING.md` | commands + agent contracts |
| `shared-memory-skill/.../SKILL.md` | tool surface; propagated by `sync_skills.sh` |

**Local-only — NEVER committed (`[[local-documentation-private]]`):**
| Doc | Note |
|---|---|
| `Local_Documentation/ADR.md` | architecture-decision log; maintain but never `git add` |
| `Local_Documentation/*` | design notes, dreaming-cycle drafts, HTML reports |

## Phase 2 — Per-chapter fact-check matrix

For each README chapter, verify the listed claims against the named source file.
Read the source, not your memory. Record every mismatch in the ledger (Phase 3).

| Chapter | Source of truth | Verify |
|---|---|---|
| §1 Vision / 3 diagnostic tests | `[[reference-aperturedata-three-tests]]`, README | the three test answers reflect shipped capabilities |
| §3 Architecture — Three Tiers | `coordinator.py`, `schema.md` | tier names, stores, query order (Tier 3→1→Neo4j) |
| §4 OS Prerequisites | sysctl values in §4 | inotify limit numbers |
| §5 Docker Compose | compose snippet in §5, `postgres_neo4j_limits.yaml` | service names, ports, volume mounts |
| §6 Database Schema | `migrations/*.sql`, `ontology.yaml` | columns, constraints, ontology defaults, **migration count (currently 006)** |
| §7 Inference Backends | ports in code | BGE-M3 :8070, Reranker :8071 |
| §8 Gateway | `hive_mind_proxy.py` | routes, async rationale, port 8888 |
| §9 Starting the Stack | `hive_mind_proxy.py` health handler (~L479-486) | the `/health` JSON string **exactly** (rem_daemon, auth_required, etc.) |
| §10 Agent Integration | `generate_tokens.py`, `.env.example`, `sync_skills.sh` | token steps, per-agent token enforcement, install paths |
| §10a Remote Clients | SSH tunnel steps | port forwards, persistence |
| §11 / §11a Agent Access + Cycle | `memory_bridge.py`, `vector-skill.py`, `mcp.json` | CLI commands, MCP tool names, end-to-end example output |
| §12 Save Path | `coordinator.py` | outbox atomicity, SHA-256 idempotency, embedding mandate, entities-required |
| §13 Sleep Cycle | `consolidation_loop.py`, `rem_loop.py` | `DENSITY_THRESHOLD`, REM/NREM phases, idle/backstop timers, supersession |
| §14 Audit Logging | audit code path | what is logged, where |
| §15 Retrieval | `coordinator.py` search | three-tier lookup order, superseded filter |
| §16 LM Studio MCP | `mcp.json`, `vector-skill.py` | placeholder keys, absolute path, env block |
| §17 Testing | `CLAUDE.md` test cmd, `tests/` | the `uv run --with ...` invocation, `MOCK_LLM=1` |
| §18 / §19 Open Problems + Roadmap | `CHANGELOG.md`, code | nothing listed "open" that is already shipped; roadmap matches phase status |
| §20 References | links | links resolve |

Cross-cutting invariants to confirm appear correctly everywhere they are stated:
1024-dim via :8888 only · hard embedding mandate (503 after 4 retries) ·
SHA-256 `ON CONFLICT DO UPDATE` · outbox atomicity · `entities` required for
consolidation. Also re-check every **version number, port, file path, and CLI
command** literally — these drift silently.

## Phase 3 — Discrepancy ledger

Produce a table before editing anything:

```
| Doc:loc | Claim in docs | Current code says | Action |
|---------|---------------|-------------------|--------|
```

Classify each: **STALE** (docs wrong → fix docs) · **CODE-BUG** (code wrong →
stop, flag, do not edit docs) · **AMBIGUOUS** (ask the user). Present the ledger
and get a go-ahead before bulk edits if there are CODE-BUG or AMBIGUOUS rows.

## Phase 4 — Apply fixes

1. Edit the public docs to match code. Preserve template placeholders verbatim —
   never fill in `[YOUR NAME]` / `YOUR_*` (`[[feedback-public-repo-placeholders]]`).
2. Update `Local_Documentation/ADR.md` for any architectural decision uncovered.
3. Add a `CHANGELOG.md` entry under the current version (or an `Unreleased`
   block) summarising the doc corrections.
4. Propagate SKILL.md + scripts — **do not hand-copy**:
   ```bash
   bash shared-memory/scripts/sync_skills.sh
   ```

## Phase 5 — Guardrails & leak audit (mandatory)

```bash
# Local_Documentation, research, memory, env files must be UNTRACKED on remote
git ls-files Local_Documentation/ research/ memory/ MEMORY.md CLAUDE.md '*.env'
```
Any output = a leak. For an already-tracked path: `git rm --cached <path>` and
commit (`[[local-documentation-private]]`). gitignore alone will not untrack it.

## Phase 6 — Ship

1. `git diff` review. Commit with a `docs:` message. **No `Co-Authored-By`**
   (`[[feedback-no-coauthor]]`); Xenofon is the author.
2. `gh pr create` — never push docs straight to `main` (`[[feedback-pr-workflow]]`).
   `--admin` self-merge is acceptable for solo doc work.
3. `Skill(shared-memory)` → save a retrospective: which chapters drifted, what the
   root cause was, entities `["Documentation","<chapters>"]`, so the next audit
   starts informed.

## Done when
Every in-scope chapter has been read against its source file · the ledger has no
unresolved STALE rows · `sync_skills.sh` ran · the leak audit returns empty ·
changes are on a PR.
