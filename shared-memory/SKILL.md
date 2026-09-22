---
name: shared-memory
description: Search, save, and query the shared three-tier memory. Use it before reasoning about history, prior decisions, or whether something was tested, tried, rejected, or done (search first), and after significant work (save facts, decisions, and retrospectives).
---

# Shared Memory

Thin client. `<skill-dir>` is the directory that contains this file. It is not an environment variable and not a home-directory guess. Every command is:

`uv run --with httpx python <skill-dir>/scripts/memory_bridge.py <argv>`

The gateway is `:8888`. Never call the embedder `:8070` or the reranker `:8071`. Never source (`.`) a skill `.env`; the client reads `AGENT_TOKEN` itself. Do not print the token.

**uv on PATH.** Agent shells are non-interactive and non-login. They do not read `~/.bashrc` or `~/.profile`, so a `uv` that lives only under `$HOME/.local/bin` is missing. The command fails and the agent answers some other way, or saves nothing, with no error to the operator. Put `uv` on the default PATH, or set PATH in this agent's own config.

## Commands

| Trigger | argv |
|---|---|
| Client build and wire contract | `--version` |
| Before a save, or when an install looks stale | `doctor` |
| Is the gateway usable | `status` |
| Before reasoning about history | `search "<query>" [limit] [--project NAME] [--domain NAME] [--since ISO]` |
| Durable result of work | `save "<content>" ['<metadata-json>'] [--domain NAME] [--supersedes PG_ID]` |
| Operator confirmed a choice | `save_decision --title "…" --decided-by "…" --rationale "…" [--project NAME] [--domain NAME] [--grounded-in "N:role"] [--alternatives "…"] [--confidence high]` |
| Outcome of a decision | `save_retrospective --pg-id N --rating STATE --notes "…" --grounded-in "N[:role]" [--source-ref PATH]` |
| What happened to this record | `lineage fact:N` |
| Retract a fact, no replacement | `supersede --pg-id N [--by SUCCESSOR]` |
| A stale flag is immaterial | `review-hold --summary-id S --pg-id N` |
| Named structural lookup | `query why-to-check\|who-decided\|agent-decisions\|retrospectives` plus that template's flags |
| Raw read-only Cypher | `graph "<cypher>"` |

Named templates: `why-to-check` (`--title` required, optional `--project`), `who-decided` (`--title`, `--project`), `agent-decisions` (`--assisted-by`, `--project`), `retrospectives` (`--rating`). They call `POST /memory/graph`. They do not hit search or telemetry. `graph` and named CLI `query` templates require `full` or `admin`. `search`, `lineage`/`status`, and `telemetry` remain for `read`. `/health` is anonymous, not a read-role grant. When auth is configured, a bare curl is only `status`, `version`, and `api_version`. A full payload that includes `"auth_required": false` means auth is off.

`save` has no `--project` flag. The client derives the project, or you set `"project"` in the metadata JSON. An explicit value wins.

## Always / Ask / Never

**Always.** Search before reasoning about history, prior decisions, or whether something was tested, tried, rejected, or done. Pass `--project`, `--domain`, and `--since` as flags, never as words inside the query string. Quote `fact:N`, `decision:N`, `summary:N` for `lineage` and for index pointers. That form is not a `--grounded-in` id. A bare number to `lineage` means the facts table. On `stale_sources` or `stale_summaries`, run `lineage` before relying, then repair the stale index pointer: rewrite the line that cited the old id to the current id. Checking without rewriting leaves the next session on the stale id.

**Ask.** An unregistered project or domain. Confirm the spelling; pass `new_project` or `new_domain` only after the operator says so. `new_entities`, only after the operator says the concept is new. Every `save_decision`. A retrospective's `--rating` and the facts in `--grounded-in`.

**Never.** Auto-decide. Overwrite a fact in place. `--supersedes` on a decision — overturn it with a retrospective `--rating reversed`. Invent `source_ref` to obtain a `fact_kind`. Share tokens. Call `:8070` or `:8071`. Source (`.`) a skill `.env`.

## Record types

| | project | domain | entities | source_ref | grounding |
|---|---|---|---|---|---|
| fact | asserts its own | asserts its own sections | mints concept-nouns | a path, or `discussion_context` | optional |
| decision | asserts its own | asserts its own; omit stores none | none | rarely | `--grounded-in` optional; ungrounded is flagged, not refused |
| retrospective | do not send | do not send | none | the instrument that measured the outcome | required |

A fact owns project, domain, and entities. A decision owns project and domain, not entities. A retrospective owns rating, notes, `source_ref`, and grounding.

`source_ref` is a path or the sentinel `discussion_context`. It is not the tokens `tested` or `measured`. Those, with `researched`, `discussion`, and `observation`, are derived `fact_kind` values.

## Patterns

**A — Search.** The query is the what. `--project NAME` keeps records that belong to that project. `--domain NAME` (repeatable, OR) matches stored `metadata.domains` only — not read-side `belonging`, a retrospective (no stored section), a decision that omitted domain, or a thematic summary (`metadata.domain`, a string) — and if that filter leaves no Tier-1 candidates the search returns `[]` and does not attach an insight. `--domain` without `--project` is a literal string across projects, not one project's section. `--since ISO` (date or datetime) keeps rows created at or after it. Filters combine. They never fall back to an unfiltered search. An unregistered name is not refused on the read path; it matches nothing. More than 16 `--domain` values is `filters_invalid`. Use the qualified `ref` on each hit. Tens of seconds is the reranker, not a hang. `ranked: false` is vector order, and Tier-3 rows are omitted. `EMBEDDING UNAVAILABLE` on stderr means keyword fallback even when rows come back. Those rows have `score_normalized` 0.5, no `ref`, and no `ranked`. Do not treat 0.5 as a rerank and do not pass them to `lineage`. `stale_sources`: `old` may be a fact, a decision, or a retrospective. Pass `lineage` the qualified ref. A 404 names the real ref. Do not force `fact:`. `stale_summaries` is a moved thematic summary under an insight. `lineage` the successor, then repair the index pointer.

**B — Create.** Run `save` from the project directory. The client walks up to `.git`, `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md`, stops at `$HOME`, and does not pass `$HOME`. `SHARED_MEMORY_PROJECT` overrides the walk. Do not hand-type a project that differs from that folder. Empty project is `project_required`. An unregistered name is `project_unknown` (with `proposals`). Second submission: pick a proposal, re-send with `new_project: true` after the operator confirms, or park on `general_discussion`. Re-sending the same unknown name does not succeed. Declare a new project once; later saves use the registered name and need no flag.

`--domain` is a registered section. Repeat the flag. Elicit one only when that project already has sections. A record with no domain is filed under its project. `"new_domain": true` (or `--new-domain` on `save_decision`) only after the operator confirms.

Entities are concept nouns the operator named (`LockOrder`, not a sentence and not the project). Ask once and accept none. Mint with `new_entities` only after they confirm; every minted name must also be in `entities`. Stamp `entities_provenance` as `operator` or `agent` per name when you know it. Omitting it still saves.

A decision sends no entities. Confirm with the operator before the call. The rationale is a Y-statement: in the context of X, we chose Y over Z, accepting W. `--grounded-in` is `pg_id:role` pairs (`based_on`, `considered`, `rejected`, `under_conditions`, `informed_by`). Ids are bare integers (`601:based_on`). `fact:601` is for `lineage` and for search refs. The grounding parser drops a non-numeric id and does not error. If every id is dropped, the decision is stored ungrounded and only flagged. A bare id takes the fact-kind default. `--alternatives` is one option per flag, stored verbatim. Naming no `--domain` stores no section. A decision does not inherit its evidence's sections.

A retrospective names the decision's `--pg-id` and no project, domain, or entities. `--rating` is an outcome state. `--grounded-in` cites the facts that measured the outcome. `--source-ref` is that instrument.

**C — Edit.** Correct a fact with `save … --supersedes OLD`, or retract it with `supersede --pg-id`. Do not re-save the same content onto a different project, domain, or entity set (`axis_conflict`). A renamed section is not resolved on that re-save; supersede instead. Overturn a decision only with a retrospective `--rating reversed` (the decision leaves Tier-1 search; the verdict stays). A later retrospective on the same decision is the live verdict. After `lineage`, rewrite the index pointer to the successor id.

## Capture index

Meaning-bearing flags. Type the flag form.

| Flag | Use |
|---|---|
| `--alternatives` | One considered option per flag on `save_decision`. Not comma-split. |
| `--confidence` | `high`, `medium`, or `low` on a decision. |
| `--domain` | Section of the project. Repeat the flag. On `search`, a filter. |
| `--grounded-in` | `id[:role],…`. Required on a retrospective. Optional on a decision. |
| `--rating` | Retrospective outcome state. Also the filter on `query retrospectives`. |
| `--since` | Search filter. ISO date or datetime. Not query text. |
| `--source-ref` | Path or `discussion_context`. On a retrospective, the measuring instrument. |
| `--supersedes` | Fact only. Soft-retires that pg_id (kept, hidden from search). |

Ratings: `validated` (held), `mixed` (partly), `refined` (evolved), `pending` (not yet judged), `reversed` (withdrawn; supersedes the decision). Nuance goes in `--notes`.

Refusals. Branch on `error`. One recovery; the second-submission essays are in `USAGE.md`.

| Code | Recovery |
|---|---|
| `axis_conflict` | Axes are fixed at first write. Supersede. Do not re-save the same content onto other axes. |
| `cypher_rejected` | Neo4j rejected the Cypher. Fix the query. Retrying it unchanged will not succeed. |
| `domain_confusable` | Ask. If it is a different section, re-send `confirm_distinct_from` naming the near match. Otherwise use the existing name. |
| `domain_not_allowed_on_judgement` | Do not send a domain. A retrospective does not store sections, and the decision's axes do not move onto it. |
| `domain_spelling_variant` | It is that registered section. Save under that spelling. It cannot be confirmed as new. |
| `domain_unknown` | Ask. Pick a proposal, or re-send `new_domain: true` after the operator confirms. Omitting the domain is valid. |
| `domain_unnameable` | Name the section with at least one letter or digit. |
| `domain_without_project` | `general_discussion` has no sections. File under a real project, or drop the domain. |
| `entities_list_too_long` | Split the save. The cap is `ENTITY_LIST_MAX_LEN` (default 50) on `entities` and on `new_entities`. |
| `entities_not_allowed_on_judgement` | A decision or retrospective carries no entities. Mint the concept on a fact and cite it. An empty list is the same as omitting it. |
| `entities_provenance_invalid` | Object only. Each key must be in `entities`. Each value must be `operator` or `agent`. |
| `entity_confusable` | Ask. Confirm with `confirm_distinct_from` naming the near match, or save the existing name. |
| `entity_name_too_long` | A concept noun, not a sentence. The cap is `ENTITY_NAME_MAX_LEN` (default 200). |
| `entity_reserved` | Schema or axis vocabulary is not an entity. Also a registered project name, including this record's own project. Name the concept, or drop it. |
| `entity_unknown` | Ask. Re-send `new_entities` listing exactly those names, each also in `entities`, or use the registered canonical. |
| `filters_invalid` | The search `--domain` list is over the 16-entry cap. Narrow it. |
| `graph_row_cap_exceeded` | Read-only Cypher returned more than `GRAPH_QUERY_ROW_CAP` rows (default 10000). Narrow the query. |
| `new_entities_invalid` | A list of strings. Each name must also be in `entities`. A name that normalizes to nothing cannot be minted. |
| `project_confusable` | Ask. If it is a different project, re-send `confirm_distinct_from` naming the neighbour. Otherwise use the existing name. |
| `project_spelling_variant` | It is that registered project. Save under that name. A rename is not a save. |
| `project_unnameable` | Name the project with at least one letter or digit. |
| `registry_unavailable` | The registry could not be read (503). Nothing was written. Retry. |
| `unavailable` | A telemetry probe (`encoders` or `gateway`) failed. Read the sibling block. The rest of the snapshot may still be valid. |
| `unknown_type` | On `save`, omit `type` or send `fact`. A decision uses its own command. Do not invent a type. |

Graph expansion on a judgement hit returns `belonging`: `{project, domains}`. The key is derived on read, never written. A fact has no such key; its belonging is the axes it stored. The domain set is the same-project union of sections asserted on the decision and sections reached through grounding. Another project's facts do not contribute sections.

Worked contract. Anonymous `/health`, then `--version`:

```json
{"status":"ok","version":"0.9.108","api_version":4}
```

```json
{"version": "0.9.108", "api_version": 4, "tool": "shared-memory-framework"}
```

`doctor` compares this client's `api_version` with the gateway and names which side to upgrade.

## Self-repair

Run `doctor`. If `compat` is not `ok`, run `bash <skill-dir>/scripts/update_skill.sh`. While incompatible, search is safe and writes pause (fact:870): do not `save`, `save_decision`, or `save_retrospective`. The script fetches every file named in `MANIFEST.txt`, now including `USAGE.md`, and never overwrites `.env`.

## Load when needed

- `USAGE.md` — elicitation, the Y-statement, second submission, search and lineage, MCP examples.
- `Documentation/schema.md` — store schema (labels, tables). Not the field contract.
- `CONSTITUTION_SNIPPET.md` — the standing block. Ask before splicing it into a constitution file.

## Two surfaces

CLI is `memory_bridge.py` in `<skill-dir>`. MCP is the host's connector `vector-skill.py`, in that host's own install, not this file. Hosts are OpenClaw, LM Studio, Claude, Codex, Grok, and agy. Each has its own install path. This file implies none of them.

MCP tools: `hybrid_search_and_rerank`, `save_artifact`, `archive_reasoning_trace`, `save_decision`, `save_retrospective`, `supersede`, `review_hold`, `check_memory_health`, `memory_telemetry`, `record_lineage`, `graph_query`. Named CLI `query` shortcuts have no MCP twin. `graph_query` is the raw Cypher form and requires `full` or `admin`. `check_memory_health` reads `/health`. `memory_telemetry` reads `/memory/telemetry`. CLI `status` prints both. CLI `doctor` is the compat check. `archive_reasoning_trace` posts `type` `reasoning_trace`, which ingress refuses as `unknown_type`. Do not retry it. Save the conclusion as a fact.
