# Shared Memory — USAGE

Load this when `SKILL.md` is not enough to make the call. The commands and the refusal codes stay there. This file is the next example. It is not a second index. MCP hosts do not load it.

## Elicitation

Fact, one line, then the call:

```
save "Lock acquisition sorts names first." '{"source":"grok","source_ref":"src/locks.py","entities":["LockOrder"]}'
```

No entities: ask once, accept none, and send the save without `entities`. Do not invent one.

Decision, after one batched ask (facts and roles, alternatives, confidence, conditions):

```
save_decision --title "Sort locks by name" --decided-by "Xenofon" --rationale "In the context of two-lock deadlocks, we chose name order over a global lock, accepting a sort on the hot path." --grounded-in "601:based_on" --alternatives "one global lock" --confidence high --elicited
```

`--assisted-by` is the model. `--decided-by` is the person. The gateway overwrites `source`.

Retrospective, after you confirm the decision id:

```
save_retrospective --pg-id 42 --rating validated --notes "No deadlocks in the soak." --grounded-in "601" --source-ref tests/test_locks.py --elicited
```

"No alternatives" is a choice. Skipping the question is not. `--elicited` only if you asked.

## Grounding

```
--grounded-in "601:based_on,602:rejected"
```

| Role | Use it when |
|---|---|
| `based_on` | The evidence the choice rests on |
| `considered` | Weighed, not the basis |
| `rejected` | Set aside |
| `under_conditions` | The choice holds only under this |
| `informed_by` | Soft input |

A bare `601` becomes `based_on`, or `informed_by` when that fact's `fact_kind` is `discussion`. Name the role when the operator has one. A retrospective with no `--grounded-in` is refused.

```
--alternatives "one global lock" --alternatives "no lock, retry on conflict"
```

One flag, one option, stored verbatim. Why-not stays in `--rationale`.

## Entities

```
save "…" '{"source":"grok","entities":["LockOrder"],"new_entities":["LockOrder"],"entities_provenance":{"LockOrder":"operator"}}'
```

A decision or retrospective that sends `entities` is `entities_not_allowed_on_judgement`. A name only in `new_entities` is `new_entities_invalid`. `entity_unknown`: ask, then re-send `new_entities` for the new names, each also in `entities`. `entity_confusable`: ask, then `confirm_distinct_from` naming the near match, or save the existing name. `entity_reserved`: the name is schema vocabulary or a project name. Drop it or name the concept. Caps: 50 names, 200 characters (`entities_list_too_long`, `entity_name_too_long`).

## Project and domain

```
save "…" '{"source":"grok","source_ref":"OPERATE.md"}'
```

Run from the project directory. The walk stops at `.git`, `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md`, not `$HOME`. `SHARED_MEMORY_PROJECT=name` overrides it. An explicit `"project"` wins. Do not type a different name to fix the folder.

```
save "…" '{"source":"grok","project":"new-name","new_project":true}'
```

`project_required`: nothing supplied. `project_unknown`: not registered. Then one of three: a proposal, `new_project: true` after the operator confirms, or `"project":"general_discussion"`. The same unknown name again is refused. `general_discussion` cannot be registered and has no sections.

```
save_decision --title "…" --decided-by "Xenofon" --rationale "…" --new-project --distinct-from "near-name"
```

`project_spelling_variant`: save the registered spelling. `project_confusable`: `--distinct-from` names the neighbour. `project_unnameable`: use a letter or digit.

```
save "…" '{"source":"grok","domain":"delivery"}'
save_decision --title "…" --decided-by "Xenofon" --rationale "…" --domain delivery --domain operations
```

Repeat the flag. Do not comma-pack. Omit `domain` and the record stores no section. It does not inherit one. `--domain` on search matches stored `metadata.domains` only.

```
save "…" '{"source":"grok","project":"new-name","new_project":true,"domain":"delivery","new_domain":true}'
```

`new_domain: true` and `--new-domain` only after the operator confirms. `domain_unknown`: pick a proposal or declare it. `domain_spelling_variant`: use the registered spelling. `domain_confusable`: `confirm_distinct_from` names the near section. `domain_without_project`: `general_discussion` has no sections. `domain_not_allowed_on_judgement`: drop the domain from the retrospective.

```
save "same words" '{"source":"grok","domain":"other"}' 
```

Same content, other axes: `axis_conflict`. Supersede instead. `registry_unavailable` (503): nothing was written. Retry.

## Search

```
search "lock acquisition order" 5 --project widget-line --domain operations --since 2026-08-01
```

A project name inside the query ranks mentions above members. `--domain` is OR. Repeat it. More than 16 is `filters_invalid`. An unknown name matches nothing. A smaller `limit` does not make the wait shorter. On a timeout, read `backend_capability` on authenticated `/health`. `SEARCH_TIMEOUT_S` only when that projection exceeds the ceiling.

`ranked: false`: vector order, `score` null, no Tier-3. `fallback: keyword` and stderr `EMBEDDING UNAVAILABLE`: the embedder is down. Empty in that state is not "nothing known".

```
lineage fact:816
lineage summary:87
review-hold --summary-id 12 --pg-id 42
```

`stale_sources` is `[{"old": N, "superseded_by": M}]`. `old` may be a fact, a decision, or a retrospective. A null successor is a retraction. `lineage` the qualified ref. Do not force `fact:`. `stale_summaries` is `[{"summary_id": Y, "superseded_reason": "…"}]` on an insight. `lineage summary:Y`. An insight's `source_pg_ids` are decisions and retrospectives. A community summary's `source_pg_ids` are facts.

## Lineage

```
lineage fact:816
lineage summary:87
lineage 816
```

`fact:816` is the fact. `summary:87` is the narrative. A bare `816` is the facts table, so a bare id copied off a summary is the wrong record. A qualified ref of the wrong type is a 404 that names the right ref.

## Supersede

```
save "Lock acquisition sorts names first." '{"source":"grok","entities":["LockOrder"]}' --supersedes 42
supersede --pg-id 42 --by 43
review-hold --summary-id 12 --pg-id 42
save_retrospective --pg-id 42 --rating reversed --notes "The global lock won." --grounded-in "601"
```

`--supersedes` and `supersede` refuse a decision or a retrospective. A new retrospective on the same `--pg-id` is the live verdict.

```
graph "MATCH (n:Fact) RETURN n.pg_id LIMIT 5"
```

`CREATE`, `DELETE`, `DETACH DELETE`, `SET`, `REMOVE`, `MERGE`, `CALL`, `LOAD CSV`, and `DROP` are blocked. Neo4j's own refusal is `cypher_rejected`. More rows than the cap is `graph_row_cap_exceeded`.

## Telemetry

```
status
doctor
```

`status` is `/health` then `GET /memory/telemetry`. `doctor` exits non-zero when the client and gateway disagree. Anonymous `/health` is `status`, `version`, `api_version`. Which backend is down needs a bearer token. HTTP 503 from `/health` means a save cannot embed.

`consolidation.stalled: true` means at least one cycle type is stalled. `stalled_types` names which. `eligible_clusters: 0` on `GET /memory/telemetry` is idle. `backend_capability` can keep the previous encoder's numbers. Check `probed_at`. The field list is `Documentation/telemetry-contract.md`. Do not paste it into a prompt.

A non-null outbox `failed_age` is an operator statement in the runbook, not a client retry.

## Token and update

```
bash <skill-dir>/scripts/update_skill.sh
```

The token is `AGENT_TOKEN` in `<skill-dir>/.env`, mode 600. Do not `.` the file, `cat` it, or paste it. The script never overwrites `.env`. It appends any example key the file does not already name. If the secret is under another name, that append adds the placeholder `AGENT_TOKEN` and the client sends it. Move the real value onto `AGENT_TOKEN` before the next call.

While `doctor` says incompatible, `search` still works and `save`, `save_decision`, and `save_retrospective` stay paused.

After the update, compare `<!-- shared-memory:constitution-snippet vN -->` in the constitution with `<skill-dir>/CONSTITUTION_SNIPPET.md`. A newer marker: show the operator the new block and what changed. Do not overwrite it. No marker: ask before splicing one.
