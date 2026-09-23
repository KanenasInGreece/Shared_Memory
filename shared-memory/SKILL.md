---
name: shared-memory
description: Use before every memory search and before answering from memory — prior work, a past decision, whether something was tried or rejected, "what did we decide", a fact:N or decision:N, lineage, or status. Search this store first; do not answer those from the chat or from local notes. Also use it to save a fact, a decision, or a retrospective after the operator confirms.
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

Search, then create, then edit. Copy the command. The line under it is what that command does.

**A — Search.**

```
search "reranker cache-ram" 5 --project shared-memory-GitHub --domain delivery --since 2026-09-01
```

→ hits carry `ref` `fact:2678`. Then:

```
lineage fact:2678
```

```
query why-to-check --title "AGENTS.md recut"
```

Filters are flags. `search "reranker --project shared-memory-GitHub --domain delivery"` searches those words. It does not set the flags.

`--domain` matches stored `metadata.domains` only. A miss, and the call returns `[]` with no insight: read-side `belonging`, a retrospective, a decision that omitted a section, a thematic summary's `metadata.domain` string. `--domain delivery` with no `--project` is that literal in every project. A 17th `--domain` is `filters_invalid`. An unknown name matches nothing.

Keyword fallback, do not `lineage` it. Stderr is `EMBEDDING UNAVAILABLE`. `ranked: false` drops Tier-3.

```json
{"score_normalized": 0.5, "ranked": false}
```

`stale_sources`: `old` may be a fact, a decision, or a retrospective. A 404 names the real ref.

```
lineage decision:2522
```

Then rewrite the index line from `decision:2522` to the successor id.

**B — Create.** Run from the project directory. The walk stops at `.git`, `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md`, not `$HOME`. `SHARED_MEMORY_PROJECT=shared-memory-GitHub` overrides it.

```
save "v0.9.109 shipped" '{"source":"grok","source_ref":"OPERATE.md","domain":"delivery"}'
```

Omit `domain` and the record stores no section. It does not inherit one.

Operator confirmed a new project:

```
save "first note" '{"source":"grok","project":"new-name","new_project":true}'
```

Empty project is `project_required`. The same unknown name again is `project_unknown` and does not succeed. Second submission is in `USAGE.md`.

Operator named the concept:

```
save "lock order" '{"source":"grok","entities":["LockOrder"],"new_entities":["LockOrder"],"entities_provenance":{"LockOrder":"operator"}}'
```

Decision, after the operator confirms. Ids are bare. `fact:601` in this slot is dropped with no error; if every id is dropped the decision is stored ungrounded and only flagged.

```
save_decision --title "Ship the skill index" --decided-by "Xenofon" --rationale "In the context of a 70KB refusal, we chose a 15KB index over keeping the essay, accepting a second file." --grounded-in "2672:based_on" --alternatives "keep the 70KB file" --confidence high
```

`--new-domain` only after the operator confirms. No entities on this command.

Retrospective. No project, no domain, no entities.

```
save_retrospective --pg-id 2675 --rating validated --notes "The index loads." --grounded-in "2678" --source-ref OPERATE.md
```

**C — Edit.**

```
save "the split shipped in v0.9.109" '{"source":"grok","source_ref":"OPERATE.md"}' --supersedes 2672
```

```
supersede --pg-id 2672
```

```
save_retrospective --pg-id 2522 --rating reversed --notes "The split shipped." --grounded-in "2678" --source-ref OPERATE.md
```

```
review-hold --summary-id 411 --pg-id 2672
```

`save_decision --supersedes 2522` is not an overturn. Re-saving the same content onto other axes is `axis_conflict`. A renamed section is not resolved on that re-save. `--rating reversed` drops the decision from Tier-1 search and keeps the verdict. A later retrospective on the same `--pg-id` is the live verdict. After `lineage`, the index line uses the successor id.

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
{"status":"ok","version":"1.0.0","api_version":4}
```

```json
{"version": "1.0.0", "api_version": 4, "tool": "shared-memory-framework"}
```

`doctor` compares this client's `api_version` with the gateway and names which side to upgrade.

## Self-repair

Run `doctor`. If `compat` is not `ok`, run `bash <skill-dir>/scripts/update_skill.sh`. While incompatible, search is safe and writes pause (fact:870): do not `save`, `save_decision`, or `save_retrospective`. The script fetches every file named in `MANIFEST.txt`, now including `USAGE.md`, and never overwrites `.env`.

## Load when needed

- `USAGE.md` — elicitation, the Y-statement, second submission, search and lineage.
- `Documentation/schema.md` — store schema (labels, tables). Not the field contract.
- `CONSTITUTION_SNIPPET.md` — the standing block. Ask before splicing it into a constitution file.
