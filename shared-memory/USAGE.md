# Shared Memory — USAGE

Load this when `SKILL.md` is not enough to make the call. Commands, Always / Ask / Never, the eight flags, the ratings, and the refusal codes stay in `SKILL.md`. This file is the procedure. It is not a second index.

If a command cannot find `uv`, use the PATH tripwire in `SKILL.md`. Do not route around it.

## Elicitation

State what you are about to save, in one line, and let the operator adjust.

- **Fact.** A mention is enough: the content, the derived project, the `source_ref` you inferred, and any entities they already named. If there are no entities, ask once and accept none. Do not invent one from the content.
- **Decision.** Ask before `save_decision`. Batch one prompt: the facts it rests on and the role of each, the alternatives you already generated, confidence (`high`, `medium`, or `low`), and the conditions. Do not send the call and then tell them.
- **Retrospective.** Ask for the target decision (search and confirm if they described it instead of giving a pg_id), the rating, and the facts that measured the outcome.

Null is allowed only as an explicit answer. "No alternatives" is a choice. Skipping the question is not. When the operator took part, pass `--elicited` (CLI) or `elicited: true` (MCP) so coverage counts the ask. Do not stamp it if you did not ask.

The gateway overwrites `source` with the authenticated token identity. Pass any non-empty source the schema requires. On a decision, the assisting model belongs in `--assisted-by`, not inside `--decided-by`.

## Y-statement and grounding

Write the rationale as one sentence: in the context of X, we chose Y over Z, accepting W. Put the conditions and the rejections in that text. `--alternatives` records what was not taken and has no room for why not. When there are no conditions, write `conditions: none`. An absent clause and a deliberate none are different claims.

`--grounded-in` / `grounded_in` grammar is the same on a decision and a retrospective: `601:based_on,602:rejected`. Roles:

| Role | Meaning |
|---|---|
| `based_on` | The evidence the choice rests on |
| `considered` | Weighed, not the basis |
| `rejected` | An option or a fact that was set aside |
| `under_conditions` | A constraint the choice holds under |
| `informed_by` | Soft input, not a hard basis |

A bare id takes a default from that fact's `fact_kind` (`discussion` → `informed_by`, anything else → `based_on`) and is recorded as a system default. Name the role when the operator has a view. Every role confers topics. Pick the role that is true. You may ground a decision on an earlier decision or on the retrospective that overturned one. The topics still come from the facts at the end of that chain.

A decision may be ungrounded only when it really was made on experience before the project had evidence. The gateway flags that. It does not refuse it. Topics arrive later from the retrospective that measures it. Everywhere else, if nobody can name a fact, say so. Search, or save the fact first. A retrospective with no `--grounded-in` is refused. A verdict that measured nothing has nothing to report.

`--alternatives` is one option per flag. The value is stored verbatim and is never split, so a comma inside an option is safe. MCP takes a list, one entry per option. A single string is one option, not a packed list.

## Entities

Only a fact mints entities. A decision or retrospective that sends a non-empty `entities`, or any `new_entities`, is `entities_not_allowed_on_judgement`. An empty list is accepted and means the same as omitting the field.

Name a concept, not a sentence and not the project or section (`LockOrder`, not `must sort locks on the VM`). A phrase becomes a node. The way back is to supersede the fact that named it. Tier-3 folds on project and domain, not on entities. An entity-less fact still consolidates. Entities are how later records navigate to this one.

An unknown name is `entity_unknown`. Ask whether it is new or a spelling of a registered canonical. If it is new, re-send with `new_entities` listing exactly those names, and put each of them in `entities` too. A name only in `new_entities` is `new_entities_invalid`. Minting creates the canonical, not an alias. A registered alias or a case or punctuation variant is rewritten to the canonical before storage. The response field `entities_rewritten` is that list, or null when nothing changed.

`entity_confusable` names the near match. Ask. If it is a different concept, re-send `confirm_distinct_from` listing that match. If it is not, save the existing name. Do not echo the neighbour back as a formality. `entity_reserved` means the name is schema vocabulary or already a project name. It answers where a record belongs, not what it is about. Ask which concept was meant, or drop the name.

`entities_provenance` is optional: `{"LockOrder": "operator"}`. Values are `operator` or `agent`. A key outside `entities`, a non-object, or any other value is `entities_provenance_invalid`. Omitting the map still saves. The response note names the gap.

Both lists cap at `ENTITY_LIST_MAX_LEN` (default 50). Each name caps at `ENTITY_NAME_MAX_LEN` (default 200). Those are `entities_list_too_long` and `entity_name_too_long`.

## Project and domain

The canonical project is the project folder's basename. On the CLI, omit it and `save` fills it from the working directory. `save_decision --project` defaults the same way. An explicit metadata `project` wins over derivation. Do not type a different name to "fix" the folder. `SHARED_MEMORY_PROJECT` overrides the walk for a daemon, CI, or cron. The walk stops at `$HOME` and never treats `$HOME` as a project. If the directory walk finds nothing and `source_ref` is an absolute path, the client walks from that path's directory. A relative `source_ref` is not a project guess.

MCP has no project directory. `save_artifact` requires `metadata.project`. Ask the operator. Do not infer one. `general_discussion` is the sentinel for a record that belongs to no project. It saves and searches. It is not folded into a project narrative. The name cannot be registered as a real project.

`project_required` means nothing was supplied and derivation found no root. `project_unknown` means the name is not registered. The body carries `proposals`. Three endings, and only these three: pick a proposal, declare the name new after the operator confirms the exact spelling, or park on `general_discussion`. On `save`, the declaration is metadata `new_project: true`. On `save_decision` it is `--new-project`. MCP uses `new_project: true`. Re-sending the same unregistered name is refused again.

`project_spelling_variant` cannot be confirmed. Save under the registered spelling. A real rename is a ledgered operation, not a save. `project_confusable` names the neighbour. Ask. If it is genuinely separate, re-send metadata `confirm_distinct_from` as a list of those registered names. On `save_decision` the same list is `--distinct-from`, comma-separated. Naming the neighbour is the confirmation. `project_unnameable` has no spelling left. Use at least one letter or digit.

A domain is a section of one project. The same word under two projects is two sections. Pass `--domain` once per section. The value is not split on commas. Metadata accepts `"domain"` or `"domains"`. Elicit a section only when that project already has registered sections. The first section in a project is a deliberate act. A record with none is filed under its project.

`domain_unknown` carries proposals matched on the section name and on its description. Pick one, or re-send `new_domain: true` (CLI decision: `--new-domain`) after the operator confirms. `domain_spelling_variant` is the existing section. `domain_confusable` needs `confirm_distinct_from` naming the near section, or the existing name. `domain_without_project` means the record is on `general_discussion`, which has no sections. `domain_unnameable` needs a letter or digit. `domain_not_allowed_on_judgement` is a retrospective that named a section. Remove it. Fix the decision if the section is wrong.

A new project and its first section may be declared on the same save. The section is registered against the project intent. It still needs the operator's confirmation. Do not invent a placeholder spelling to get the save through.

Naming no domain on a decision stores no section. Nothing copies the evidence's sections onto the decision at write time. The read-side key `belonging` (see `SKILL.md`) can still show same-project sections reached through grounding. That is not a stored axis. A search `--domain` filter matches stored sections, so a decision that asserted none does not satisfy the filter on its own, and neither does a retrospective. It also misses a thematic summary (`metadata.domain`, a string, not `metadata.domains`). If the filter leaves no Tier-1 candidates, the search returns `[]` and does not attach an insight.

`registry_unavailable` (503) means the registry could not be read. Nothing was written. Retry. It is not an unknown name.

Re-saving identical content does not move axes. A different project, domain, or entity set on the same content is `axis_conflict`. Supersede. Domain aliases are not resolved on that re-save, so a section that was renamed also looks like a different domain. Supersede the record. Do not re-save it.

## Search

Keep place and time out of the query text. A project name inside the query ranks records that mention it above records that belong to it, and the records you wanted can fall past `limit`.

```
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py search "lock acquisition order" 5 --project widget-line --domain operations --since 2026-08-01
```

`--project`, `--domain`, and `--since` are optional and additive. `--domain` is OR: any listed section qualifies. Repeat the flag. Do not comma-pack it. `--since` is `2026-08-01` or `2026-08-01T00:00:00`. An unknown project or domain is not a refusal here. It matches nothing. More than 16 domains is `filters_invalid`.

The wait follows the gateway's published encoder cost. A smaller `limit` does not make it faster. If the client reports that the gateway did not answer in time, the gateway is up and slow. Read `backend_capability` on authenticated `/health`. Set `SEARCH_TIMEOUT_S` only when that projection exceeds the ceiling.

Rows are ranked together. A summary or insight sits where its score puts it, which may be first, last, or absent. `score` is a raw logit. `score_normalized` is that logit squashed into (0, 1). `ranked: false` means the reranker did not score the set: vector order, `score` null, no Tier-3 rows. `fallback: keyword` means the embedder is down and the gateway did a substring match. The CLI prints `EMBEDDING UNAVAILABLE` on stderr, including when the list is empty. Empty in that state is not "nothing known".

`stale_sources`: `[{"old": N, "superseded_by": M}]` on a summary or insight. A null `superseded_by` is a retraction or a reversed decision with no replacement. Fetch the successor with `lineage fact:M` and compare before relying. If the change does not matter, `review-hold --summary-id S --pg-id N` stops the re-flag. If it matters, save the corrected understanding. `stale_summaries`: `[{"summary_id": Y, "superseded_reason": "…"}]` on an insight. The insight's own facts may still be fine. The narrative under it moved. `lineage summary:Y` before relying.

A community summary's `source_pg_ids` are fact ids. Thematic summary ids are facts. An insight's `source_pg_ids` are decisions and retrospectives, not decision ids only.

## Lineage

An id is unique only inside its table. Facts, decisions, and retrospectives share one table. Summaries and insights use another sequence. The same integer is one of each. Every search hit carries `record_type` and a qualified `ref` (`fact:816`, `summary:87`). Pass that string to `lineage` or to MCP `record_lineage`.

`lineage fact:816` is the fact. `lineage summary:87` is the narrative plus its sources, already qualified. A bare `816` still means the facts table. A bare id copied off a summary is the wrong record. A qualified ref of the wrong type returns 404 and names the right ref, rather than a plausible other row.

`lineage` returns record state (type, created, superseded, grounded_in), dream-cycle stamps (applied, rem_reviewed, consolidated), and what the record was folded into.

## Supersede

Supersession is explicit. Similarity is not a correctness signal. The old fact is kept, flagged, hidden from search, and left out of consolidation. Dependents are not rewritten on the spot. They show up later as `stale_sources`.

```
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py save "<corrected fact>" '{"source":"grok","entities":["LockOrder"]}' --supersedes 42
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py supersede --pg-id 42 --by 43
uv run --with httpx python <skill-dir>/scripts/memory_bridge.py review-hold --summary-id 12 --pg-id 42
```

`--supersedes` and `supersede` refuse a decision or a retrospective. To overturn a decision, save a retrospective on its pg_id with `--rating reversed`. To change a retrospective, save a new one on the same decision. The newest is the live verdict. Do not retract the old retrospective in place.

`graph` and `graph_query` are read-only. `CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, and `DROP` are blocked for every role. A Cypher error from Neo4j is `cypher_rejected` (400), carrying Neo4j's message. A 500 `query failed` is the gateway or Neo4j, and a retry can be reasonable. `graph_row_cap_exceeded` means the read returned more than the cap. Narrow it.

## MCP

The eleven tool names are listed in `SKILL.md`. Shapes below match `vector-skill.py`. There is no working directory, so a fact's project is an argument, not a derivation. Named CLI `query` templates have no MCP twin. Use `graph_query` for raw read-only Cypher. It requires `full` or `admin`, same as CLI `graph` and `query`.

Search (`hybrid_search_and_rerank`). `domains` is a list, or one name. `project` and `since` are separate arguments. All three are filters, not words in `query`.

```json
{"query": "lock acquisition order", "limit": 5, "project": "widget-line", "domains": ["operations"], "since": "2026-08-01"}
```

Fact (`save_artifact`). `metadata_json` is a JSON string. `source` is required by the tool even though the gateway overwrites it. `project` is required here.

```json
{"content": "Lock acquisition sorts names before taking the locks.", "metadata_json": "{\"source\":\"grok\",\"project\":\"widget-line\",\"entities\":[\"LockOrder\"],\"entities_provenance\":{\"LockOrder\":\"operator\"},\"source_ref\":\"src/locks.py\"}"}
```

`source_ref` is that path, or `discussion_context` for a conversation. Do not put `tested` or `measured` there. Those strings are not citations. The gateway derives `fact_kind` from a real path.

Decision (`save_decision`). Required: `title`, `decided_by`, `project`, `rationale`, `source`. `alternatives` is a list. `domain` is one name or a list. Do not comma-pack a string if a section name itself contains a comma: the tool splits a string on commas. `grounded_in` uses the same grammar as the CLI. `new_project` and `new_domain` only after the operator confirms. `confirm_distinct_from` is a comma-separated string of the registered names from a confusable refusal. The tool sends that as a list.

Retrospective (`save_retrospective`). Required: `pg_id` of the decision, `rating`, `notes`, `source`. Pass `grounded_in`. Do not pass a project, a domain, or entities. The tool does not take them.

`supersede` takes `pg_id` and optional `by` (`0` or omit for a retraction with no successor). `review_hold` takes `summary_id` and `pg_id`. `record_lineage` takes the qualified ref. `check_memory_health` and `memory_telemetry` are the health and telemetry reads in the next section.

`archive_reasoning_trace` is a real tool and a broken save. It posts `metadata.type` `reasoning_trace`. Ingress accepts a fact (type omitted or `fact`), a decision, or a retrospective, and refuses anything else as `unknown_type`. Do not retry the tool and do not invent a type to get it through. Save the conclusion as a fact if the path is worth keeping.

A read token may call `hybrid_search_and_rerank`, `record_lineage`, and `memory_telemetry`. Writes, `graph_query`, and the named CLI templates need `full` or `admin`. A 403 on a write is the role. Do not retry it with the same token.

## Telemetry

CLI `status` prints the `/health` verdict and then `GET /memory/telemetry`. MCP splits the pair: `check_memory_health` is the health payload and names an `api_version` skew when the two sides differ; `memory_telemetry` is the telemetry payload. CLI `doctor` is the compat check and exits non-zero when the client and gateway disagree. The field list — type, unit, which release added it — is `Documentation/telemetry-contract.md`. Do not paste that contract into a prompt. Read the file when a key's meaning matters.

Anonymous `/health` is three keys: `status`, `version`, `api_version`. The dependency enums, consolidation summary, and warnings need a bearer token. `status: degraded` is visible anonymously. Which backend is down is not. HTTP 503 from `/health` means the embedder or the reranker is down, so a save cannot embed. Other verdicts are HTTP 200 with the enum in the body.

`/health` `consolidation` is only the summary. `stalled: true` means at least one cycle type is stalled. `stalled_types` names which. The per-type census (`eligible_clusters`, `last_deferred_reason`, deferred versus failed) is on `GET /memory/telemetry`, not on `/health`. `eligible_clusters: 0` is idle, not broken. `backend_capability` can keep the previous encoder's numbers after a swap. Check `probed_at` before acting on it.

A non-null outbox `failed_age` means Neo4j rows exhausted their retries. Tier 1 still has the record. Recovery is an operator statement on the outbox, not a client retry. Do not invent that statement here. It is in the operations runbook.

## Token and update

The token lives in `<skill-dir>/.env` as `AGENT_TOKEN`, mode 600. The client finds that file itself. Do not `.` it, do not `cat` it, and do not paste it into a transcript. A 401 means this file's token is not in the gateway registry. One token per agent. Never share one.

`bash <skill-dir>/scripts/update_skill.sh` fetches every `MANIFEST.txt` entry, including `USAGE.md`, and never overwrites `.env`. New optional keys from `.env.example` are merged in. A partial fetch is not applied. While `doctor` says incompatible, search stays available and writes stay paused.

If this agent's constitution already has a `<!-- shared-memory:constitution-snippet vN -->` block, compare that marker with `<skill-dir>/CONSTITUTION_SNIPPET.md` after the update. A newer marker means propose a replacement and say what changed. Do not overwrite the block silently. No marker means the block was never offered. Ask before splicing one. An MCP host uses `CONSTITUTION_SNIPPET_MCP.md` and its own marker, from that host's walled directory, not this CLI snippet.
