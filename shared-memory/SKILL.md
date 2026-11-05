---
name: shared-memory
description: Use before every memory search and before answering about history, prior work, a past decision ("what did we decide"), whether something was tested, tried, rejected, or done, a fact:N or decision:N, lineage, status, or saving a fact, decision, or retrospective. Search this store first; never answer those from chat or local notes. Do not use for reading or editing the current files, running code or tests, web research, or questions about what the code does NOW.
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
| Read a known record whole (step two) | `search --ref fact:N [--ref decision:M]` |
| Retract a fact, no replacement | `supersede --pg-id N [--by SUCCESSOR] [--acknowledge-standing ID "operator's words"]` |
| A stale flag is immaterial | `review-hold --summary-id S --pg-id N` |
| Named structural lookup | `query why-to-check\|who-decided\|agent-decisions\|retrospectives` plus that template's flags |
| Raw read-only Cypher | `graph "<cypher>"` |

`graph` and named CLI `query` templates require `full` or `admin` (`POST /memory/graph`): `why-to-check` (`--title` required, `--project`), `who-decided` (`--title`, `--project`), `agent-decisions` (`--assisted-by`, `--project`), `retrospectives` (`--rating`). Role `read` keeps `search`, `lineage`/`status`, and `telemetry`. With auth configured, anonymous `/health` returns only `status`, `version` and `api_version`; it is not a read-role grant. A full payload that includes `"auth_required": false` means auth is off.

Reading is two steps: step one ranks and expands, step two is `search --ref
<ref>`. `read_with` names that call. Every record entry on search
`graph_context`, `lineage` and `/memory/graph` is looked up in Postgres on each
call and carries `text` (never cut), `text_is` (`summary` = a whole shorter
summary, REM's or a decision's rationale; `full_record` = the whole record;
`pointer` = no text, the ref is the only context) and `full_record` `{ref,
chars, read_with}`. A decision's rationale is not the whole decision:
`read_with` gives its choice and alternatives. Any entry can be expanded by its
ref, and a `pointer` entry must be. Relations are triples `{relation: {from,
relation, to, props}, record}`; a null end is a record this caller cannot read.
A non-record far end is an object `{label, name}` and that item has no `record`
key. Search never returns superseded or reversed records. Explicit reads show
them with `obsolete` and `superseded_by` when the successor is readable.
`retired_grounds` on a decision hit means its evidence was retired: read the
successor before relying on it. Each `retired_grounds` item names a relation,
and `superseded_by` when that successor is readable. `latest_verdict` on a
retrospective names the verdict that counts most. `found: false` means there is
nothing to read. `lineage`, `search --ref` and a graph query are those reads. On
`/memory/graph`, nodes inside paths and maps are resolved like any node. Only a
projected property is not the record. `grounded_in`, `consolidated_into` and a
summary's `sources` are references and carry no text.

`save` has no `--project` flag. The client derives the project, or you set `"project"` in the metadata JSON. An explicit value wins.

## What to expect back and when to stop

1. **Stop after step one when `content` or `text` already answers.** Read each hit's `content` and the `text` of each `graph_context` entry first. If they already answer, stop and answer from them (after lifecycle checks: `stale_sources` and `retired_summaries` → `lineage` before relying; `retired_grounds` → read the successor) — do not call `search --ref` (`read_with`) to confirm (fact:3527: a cold model expanded records whose answer it already held). Expand an entry with `read_with` only when the question needs more than its `text`: a `pointer` entry has no text, so it must be expanded before its relevance can be judged, and a decision's `text` is only its rationale; `read_with` gives its choice and alternatives.
2. **A `summary` entry is a shorter version of its record, and whether the question needs the whole record is the reader's call.** Before answering that something is not recorded, weigh whether an on-topic summary may hold it (fact:3527: a baseline run concluded "not recorded" without expanding the summary that held the answer).
3. **Near-zero ranked scores on an unfiltered search mean nothing relevant came back.** When an unfiltered search was ranked (no `EMBEDDING UNAVAILABLE` note, no `fallback: keyword`, no `ranked: false`) and every hit scores near zero on `score_normalized` (if you used a filter, drop it once first; an empty result is not a near-zero result), say "not found" with the scope you searched and stop. An unranked or empty reply in the fallback state is not "nothing known".
4. **In-domain, the score cannot tell you the answer is missing**, because the score ranks topic, not answerability (fact:3133: an in-domain question with no answer scored above two genuine answers). Judge from the content you read: rephrase once, and drop any filter once (`--domain` misses retrospectives, decisions without sections, and a thematic summary whose section is stored in the singular `metadata.domain`). If a retry finds it, answer from it; answer "not found in the store" (naming the searches you ran) only if the rephrased and unfiltered searches, and any expansions they need, still lack the answer.
5. **A hit whose `tier` is `insight_summary` or `community_summary` is a thematic narrative over several records.** It answers "what is the pattern, the principle, the history of this theme"; for a summary hit, answer from its `content`. For a specific fact, number, or decision not in its `content`, open the refs in its `graph_context` or run `lineage` with the hit's own `ref` (`summary:N` or `insight:N`) and read its `sources`; never pass a bare `source_pg_ids` number to `lineage`, which reads it as a fact.

## Always / Ask / Never

**Always.** Search before reasoning about history, prior decisions, or whether something was tested, tried, rejected, or done. Pass `--project`, `--domain`, and `--since` as flags, never as words inside the query string. Quote `fact:N`, `decision:N`, `summary:N` for `lineage` and for index pointers. That form is not a `--grounded-in` id. A bare number to `lineage` means the facts table. On `stale_sources` or `retired_summaries`, run `lineage` before relying, then repair the stale index pointer: rewrite the line that cited the old id to the current id. Checking without rewriting leaves the next session on the stale id.

**Ask.** An unregistered project or domain. Confirm the spelling; pass `new_project` or `new_domain` only after the operator says so. `new_entities`, only after the operator says the concept is new. Every `save_decision`. A retrospective's `--rating` and the facts in `--grounded-in`.

**Never.** Auto-decide. Overwrite a fact in place. `--supersedes` on a decision — overturn it with a retrospective `--rating reversed`. Invent `source_ref` to obtain a `fact_kind`. Share tokens. Call `:8070` or `:8071`. Source (`.`) a skill `.env`.

## Record types

| | project | domain | entities | source_ref | grounding |
|---|---|---|---|---|---|
| fact | asserts its own | asserts its own sections | mints concept-nouns | a path, or `discussion_context` | optional |
| decision | asserts its own | asserts its own; omit stores none | none | rarely | `--grounded-in` optional; ungrounded is flagged, not refused |
| retrospective | do not send | do not send | none | the instrument that measured the outcome | required |

`source_ref` is a path or the sentinel `discussion_context`. It is not the tokens `tested` or `measured`. Those, with `researched`, `discussion`, and `observation`, are derived `fact_kind` values.

## Patterns

**A — Search.**

```
search "reranker cache-ram" 5 --project shared-memory-GitHub --domain delivery --since 2026-09-01
lineage fact:2678
query why-to-check --title "AGENTS.md recut"
search --ref fact:2672
```

`--domain` matches stored `metadata.domains` only (not read-side `belonging` or summary `metadata.domain`; unknown names match nothing). Do not `lineage` a keyword-fallback row (`EMBEDDING UNAVAILABLE`, `ranked: false`, `score` 0.0, `score_normalized` 0.5, no Tier-3). In `stale_sources`, `old` may be a fact, decision, or retrospective (`lineage decision:2522`; a 404 names the real ref).

**B — Create.** Run in the project folder (not `$HOME`; `SHARED_MEMORY_PROJECT` overrides). Omit `--domain` for no section (none inherited; metadata `"domain"` becomes `domains`). Empty project is `project_required`; an unknown name is `project_unknown` (never retry unchanged; see `USAGE.md`).

```
save "v0.9.109 shipped" '{"source":"grok","source_ref":"OPERATE.md"}' --domain delivery
save "first note" '{"source":"grok","project":"new-name","new_project":true}'
save "lock order" '{"source":"grok","entities":["LockOrder"],"new_entities":["LockOrder"],"entities_provenance":{"LockOrder":"operator"}}'
save_decision --title "Ship the skill index" --decided-by "Xenofon" --rationale "In the context of a 70KB refusal, we chose a 15KB index over keeping the essay, accepting a second file." --grounded-in "2672:based_on" --alternatives "keep the 70KB file" --confidence high
save_retrospective --pg-id 2675 --rating validated --notes "The index loads." --grounded-in "2678" --source-ref OPERATE.md
```

On `save_decision`, `--grounded-in` ids are bare (`fact:601` drops silently); `--new-domain` requires confirmation; no entities.

**C — Edit.**

```
save "the split shipped in v0.9.109" '{"source":"grok","source_ref":"OPERATE.md"}' --supersedes 2672
supersede --pg-id 2672 --acknowledge-standing 2522 "still stands without the new fact"
save_retrospective --pg-id 2522 --rating reversed --notes "The split shipped." --grounded-in "2678" --source-ref OPERATE.md
review-hold --summary-id 411 --pg-id 2672
```

On `decision_loses_last_ground`, do not retry `--supersedes`. `save_decision --supersedes 2522` is not an overturn. Re-save does not resolve a renamed section. A newer retrospective on `--pg-id` is the live verdict.

## Capture index

Meaning-bearing flags. Type the flag form.

| Flag | Use |
|---|---|
| `--acknowledge-standing` | Repeat per decision: `--acknowledge-standing ID "operator's words"` (map of decision id to non-empty words; bare id, empty string, placeholder, or `id:answer` is refused). |
| `--alternatives` | One option per flag on `save_decision` (not comma-split). |
| `--confidence` | `high`, `medium`, or `low` on a decision. |
| `--domain` | Project section (repeat flag); filter on `search`. |
| `--grounded-in` | `id[:role],…` (required on retrospective, optional on decision). |
| `--rating` | Retrospective outcome state; filter on `query retrospectives`. |
| `--ref` | On `search`: read records by ref (repeat flag; no query with it). |
| `--since` | Search filter (ISO date/datetime, not query text). |
| `--source-ref` | Path or `discussion_context` (measuring instrument on retrospective). |
| `--supersedes` | Fact only; soft-retires that pg_id (kept, hidden from search). |

Ratings: `validated` (held), `mixed` (partly), `refined` (evolved), `pending` (not yet judged), `reversed` (withdrawn; supersedes the decision). Nuance goes in `--notes`.

Refusals. Branch on `error`. One recovery; the second-submission essays are in `USAGE.md`.

| Code | Recovery |
|---|---|
| `axis_conflict` | Axes are fixed at first write; supersede, do not re-save onto other axes. |
| `cypher_rejected` | Neo4j rejected the Cypher; fix the query, do not retry unchanged. |
| `decision_loses_last_ground` | Ask per decision in `decisions_needing_answer` (`acknowledge_standing` is a map; a list is refused). Still stands: `--acknowledge-standing ID "operator's words"`. Save the new fact first on support or reversal, then a retrospective with `--grounded-in "NEW_ID:based_on"`, then supersede. ⛔ Refuse a supersession that removes a decision's last based-on fact until the operator answers (decision:2802); the operator's words are carried per decision (fact:2809). |
| `decision_not_visible` | Caller cannot read that decision; do not send its id. |
| `domain_confusable` | Ask: re-send `confirm_distinct_from` naming the near match, or use the existing name. |
| `domain_not_allowed_on_judgement` | Do not send a domain on a retrospective; it stores no sections and inherits none. |
| `domain_shape_invalid` | A section is a string or a list of strings. Fix the value. |
| `domain_spelling_variant` | Save under that registered spelling; it cannot be confirmed as new. |
| `domain_unknown` | Ask: pick a proposal, re-send `new_domain: true` after confirmation, or omit the domain. |
| `domain_unnameable` | Name the section with at least one letter or digit. |
| `domain_without_project` | `general_discussion` has no sections; use a real project or drop the domain. |
| `entities_list_too_long` | Split the save; `ENTITY_LIST_MAX_LEN` (default 50) caps `entities` and `new_entities`. |
| `entities_not_allowed_on_judgement` | Decisions and retrospectives carry no entities (empty list = omit); mint on a fact and cite it. |
| `entities_provenance_invalid` | Object only: keys must be in `entities`, values must be `operator` or `agent`. |
| `entity_confusable` | Ask: pass `confirm_distinct_from` naming the near match, or use the existing name. |
| `entity_name_too_long` | Use a concept noun, not a sentence (cap `ENTITY_NAME_MAX_LEN`, default 200). |
| `entity_reserved` | Schema/axis vocabulary and registered project names (including this record's own project) cannot be entities; name the concept or drop it. |
| `entity_unknown` | Ask: re-send `new_entities` (each also in `entities`) after confirmation, or use the canonical name. |
| `fact_already_superseded` | Already superseded; nothing was written. Re-read lineage. |
| `fact_cannot_carry_decision` | A fact cannot carry a `decision` object; drop it, or set `metadata.type` to `decision` with `decided_by`, `project`, and `rationale`. |
| `filters_invalid` | Search `--domain` list exceeds the 16-entry cap; narrow it. |
| `graph_row_cap_exceeded` | Cypher exceeded `GRAPH_QUERY_ROW_CAP` rows (default 10000); narrow the query. |
| `new_entities_invalid` | List of non-empty strings, each also in `entities`; a name normalizing to empty cannot be minted. |
| `project_confusable` | Ask: re-send `confirm_distinct_from` naming the neighbour, or use the existing name. |
| `project_spelling_variant` | Save under that registered project name; a rename is not a save. |
| `project_unnameable` | Name the project with at least one letter or digit. |
| `registry_unavailable` | Registry unreadable (503); nothing was written. Retry. |
| `refs_invalid` | `--ref` takes at most 16 `type:id` refs by default; fix the list. |
| `unavailable` | Telemetry probe (`encoders` or `gateway`) failed; read the sibling block (the rest may be valid). |
| `unknown_type` | On `save`, omit `type` or send `fact` (decisions use `save_decision`); do not invent a type. |
| `use_retrospective_endpoint` | Do not use `save` for a retrospective; nothing was written. Use `save_retrospective` (`POST /memory/retrospective`). |

Graph expansion on a judgement hit derives `belonging`: `{project, domains}` on read (never written; facts have no `belonging` key). `domains` is the same-project union of sections on the decision and sections reached through grounding; another project's facts contribute none.

Worked contract. Anonymous `/health`, then `--version`:

```json
{"status":"ok","version":"1.1.0","api_version":6}
```

```json
{"version": "1.1.0", "api_version": 6, "tool": "shared-memory-framework"}
```

`doctor` compares this client's `api_version` with the gateway and names which side to upgrade.

## Self-repair

Run `doctor`. If `compat` is not `ok`, run `bash <skill-dir>/scripts/update_skill.sh`. While incompatible, search is safe; pause writes (fact:870): do not `save`, `save_decision`, or `save_retrospective` until updated. The script fetches every file named in `MANIFEST.txt`, now including `USAGE.md`, and never overwrites `.env`.

## Load when needed

- `USAGE.md` — elicitation, the Y-statement, second submission, search and lineage.
- `Documentation/schema.md` — store schema (labels, tables). Not the field contract.
- `CONSTITUTION_SNIPPET.md` — the standing block. Ask before splicing it into a constitution file.
