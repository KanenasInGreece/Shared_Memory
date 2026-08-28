<!-- GENERATED FROM shared-memory/scripts/telemetry_contract.py — DO NOT EDIT BY HAND. -->
<!-- Regenerate: uv run python shared-memory/scripts/telemetry_contract.py > shared-memory/Documentation/telemetry-contract.md -->

# The Telemetry Contract

Contract version **0.9.74**. Every key the gateway emits on `GET /health` and `GET /memory/telemetry`, what it means, what it is measured in, when it arrived, and where it is going.

## The roles

**Rule of thumb:** up/down → health · a number → telemetry · number > limit → telemetry keeps the number, health raises the warning, the log records the crossing.

`GET /health` answers *can I use it now, and what should I expect*: one status enum, one enum per dependency, the warnings a limit crossing raised, identity/version, and the sizing a client needs to set its own timeouts. It is served from a short TTL cache over a 60-second refresher and **makes no database call at request time**.

`GET /memory/telemetry` is **the numbers**: counters, gauges, percentiles and censuses, with the limit stated next to the number it bounds. Bounded cost per request — the whole payload is cached for `TELEMETRY_CACHE_S`, and the unbounded insight walk is computed by the refresher, not per request.

The logs are the final word on *what happened at 03:12*: every dependency state transition and every warning raised or cleared writes one line named after the key that changed (the **log twin** column). Never a line per poll.

## HTTP status codes — unchanged by this release

`/health` returns **503 if and only if the embedder or the reranker is down**. That is the save mandate: without an encoder a save cannot produce a vector, and a row with no vector is invisible to semantic search. **Every other verdict — a degraded encoder, a dead Postgres, a failing outbox, a stalled daemon — is served 200 with the enum in the body.** A consumer that inferred the verdict from the status code must read `status` and `dependencies` instead.

An anonymous caller on an auth-configured install receives exactly `{status, version, api_version}` and nothing else.

## Meaning changes

A key whose **value** changed meaning while its **name** stayed. Enumerated because a consumer reading only the key list would see nothing wrong.

| endpoint | key | version | was | now | what to do |
|---|---|---|---|---|---|
| telemetry | `breakdown.domains` | 0.9.74 | the PROJECT distribution (it was built from PROJECT_SQL) | the DOMAIN distribution, from metadata->'domains' | read breakdown.projects for the old value |
| health | `role` | 0.9.74 | two-valued: read | write — an admin token reported `write` | three-valued: read | write | admin | a consumer testing `role == 'write'` for may-I-save now correctly excludes an admin token, which cannot save |
| health | `llm_backends.*` | 0.9.74 | a 401/403 from a CREDENTIALED backend was reported `ok` — the bare probe carries no key, so the rejection was read as 'the server answered' | http_401 / http_403 for that backend, and it counts as down | expect a credentialed backend that rejects the liveness probe to read down rather than ok |
| health | `status` | 0.9.74 | ok | degraded — degraded iff embedder or reranker was not ok | ok | degraded | down, derived from `dependencies` and `warnings`; a degraded encoder, a failed outbox row, a REM dead-letter or an unreadable registry all reach it | ⛔ THE HTTP CODE IS UNCHANGED — 503 still means exactly 'embedder or reranker is down'. A consumer that inferred the code from the enum must now read the code itself. |

## Removed outright in 0.9.74

Not moved — **removed**. Each had no writer and had therefore read `0` since it shipped.

| endpoint | key | why |
|---|---|---|
| telemetry | `entity_graph.alias_edges` | no writer — the ALIASES relationship was never emitted by any code path, and the name collides with the live alias TABLES |
| telemetry | `entity_graph.alias_covered_entities` | same writer-less ALIASES relationship |
| telemetry | `entity_graph.alias_components` | read Entity.alias_component, which the retired gds.wcc caller was the only writer of |
| telemetry | `entity_graph.largest_alias_component` | same retired gds.wcc stamp |

## `GET /health`

Paths are relative to the response object.

### liveness

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `agent` | str | — | <=0.9.73 | — | — | — | authenticated callers only |
| `auth_required` | bool | — | <=0.9.73 | — | — | — | — |
| `auth_scheme` | str | — | <=0.9.73 | — | — | — | — |
| `backup_in_progress` | bool | — | <=0.9.73 | — | — | — | — |
| `role` | str | — | <=0.9.73 | — | — | — | read \| write \| admin. `admin` since 0.9.74 — an admin token is confined to /admin/* and cannot save either, so reporting it as `write` overstated it. |
| `status` | str | — | <=0.9.73 | — | — | — | ok \| degraded \| down. down if any dependency is down; degraded if any dependency is degraded OR warnings is non-empty; else ok. ⛔ THE HTTP CODE IS A DIFFERENT QUESTION: 503 iff embedder or reranker is down (the save mandate) — every other verdict is served 200 with the enum. |

### dependencies

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `dependencies.embedder.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.embedder.state` | str | — | 0.9.74 | — | — | `health.embedder` | — |
| `dependencies.llm_pool.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.llm_pool.state` | str | — | 0.9.74 | — | — | `health.llm_pool` | — |
| `dependencies.neo4j.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.neo4j.state` | str | — | 0.9.74 | — | — | `health.neo4j` | — |
| `dependencies.nrem_daemon.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.nrem_daemon.state` | str | — | 0.9.74 | — | — | `health.nrem_daemon` | — |
| `dependencies.outbox.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.outbox.state` | str | — | 0.9.74 | — | — | `health.outbox` | — |
| `dependencies.postgres.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.postgres.state` | str | — | 0.9.74 | — | — | `health.postgres` | — |
| `dependencies.registry.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.registry.state` | str | — | 0.9.74 | — | — | `health.registry` | — |
| `dependencies.rem_daemon.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.rem_daemon.state` | str | — | 0.9.74 | — | — | `health.rem_daemon` | — |
| `dependencies.reranker.reason` | str/null | — | 0.9.74 | — | — | — | — |
| `dependencies.reranker.state` | str | — | 0.9.74 | — | — | `health.reranker` | — |

### warnings

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `warnings[]` | list | — | 0.9.74 | — | — | — | One entry per limit crossing. The THRESHOLD lives server-side so every consumer sees the same verdict — the monitor stops deriving health from telemetry numbers client-side. |
| `warnings[].key` | str | — | 0.9.74 | — | — | `health.warning.<key>` | — |
| `warnings[].limit` | int/float | — | 0.9.74 | — | — | — | — |
| `warnings[].observed` | int/float | — | 0.9.74 | — | — | — | — |
| `warnings[].unit` | str | — | 0.9.74 | — | — | — | — |

### capacity

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `capacity.derived.client_ceiling_s` | float/null | _s | <=0.9.73 | — | — | — | — |
| `capacity.derived.payload_basis` | str | — | <=0.9.73 | `telemetry:capacity.derived.payload_basis` | 0.9.75 | — | — |
| `capacity.derived.payload_basis_sample_count` | int | — | <=0.9.73 | `telemetry:capacity.derived.payload_basis_sample_count` | 0.9.75 | — | — |
| `capacity.derived.payload_max_chars_measured` | int/null | _chars | <=0.9.73 | `telemetry:capacity.derived.payload_max_chars_measured` | 0.9.75 | — | — |
| `capacity.derived.payload_mean_chars_measured` | int/float/null | _chars | <=0.9.73 | `telemetry:capacity.derived.payload_mean_chars_measured` | 0.9.75 | — | — |
| `capacity.derived.queue_bound` | int/null | — | <=0.9.73 | `telemetry:capacity.derived.queue_bound` | 0.9.75 | — | — |
| `capacity.derived.recommended_reranker_mem_limit_bytes` | int/null | _bytes | <=0.9.73 | `telemetry:capacity.derived.recommended_reranker_mem_limit_bytes` | 0.9.75 | — | — |
| `capacity.derived.s_max_measured_s` | float/null | _s | <=0.9.73 | — | — | — | — |
| `capacity.derived.s_mean_measured_s` | float/null | _s | <=0.9.73 | `telemetry:capacity.derived.s_mean_measured_s` | 0.9.75 | — | — |
| `capacity.derived.s_mean_s` | float/null | _s | <=0.9.73 | — | — | — | — |
| `capacity.derived.single_search_exceeds_wait` | bool | — | <=0.9.73 | `telemetry:capacity.derived.single_search_exceeds_wait` | 0.9.75 | — | — |
| `capacity.derived.tolerable_wait_s` | float/null | _s | <=0.9.73 | `telemetry:capacity.derived.tolerable_wait_s` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.cpu_encoder_replicas` | str/int/null | — | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.cpu_encoder_replicas` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.embedder_url` | str | — | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.embedder_url` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.gpu_encoder_replicas` | str/int/null | — | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.gpu_encoder_replicas` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.rerank_max_doc_chars` | int | _chars | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.rerank_max_doc_chars` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.reranker_url` | str | — | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.reranker_url` | 0.9.75 | — | — |
| `capacity.fingerprint.encoder_config.search_candidate_floor` | int | — | <=0.9.73 | `telemetry:capacity.fingerprint.encoder_config.search_candidate_floor` | 0.9.75 | — | — |
| `capacity.fingerprint.hardware.gpu_present` | bool | — | <=0.9.73 | `telemetry:capacity.fingerprint.hardware.gpu_present` | 0.9.75 | — | — |
| `capacity.fingerprint.hardware.mem_total_bytes` | int | _bytes | <=0.9.73 | `telemetry:capacity.fingerprint.hardware.mem_total_bytes` | 0.9.75 | — | — |
| `capacity.fingerprint.hardware.nproc` | int | — | <=0.9.73 | `telemetry:capacity.fingerprint.hardware.nproc` | 0.9.75 | — | — |
| `capacity.probe.embedder_chars_per_s` | int/float/null | — | <=0.9.73 | `telemetry:capacity.probe.embedder_chars_per_s` | 0.9.75 | — | — |
| `capacity.probe.embedder_measured_at` | str/null | — | <=0.9.73 | `telemetry:capacity.probe.embedder_measured_at` | 0.9.75 | — | — |
| `capacity.probe.probe_stale` | bool | — | <=0.9.73 | — | — | — | — |
| `capacity.probe.probed_at` | str/null | — | <=0.9.73 | `telemetry:capacity.probe.probed_at` | 0.9.75 | — | — |
| `capacity.probe.reranker_chars_per_s` | int/float/null | — | <=0.9.73 | `telemetry:capacity.probe.reranker_chars_per_s` | 0.9.75 | — | — |
| `capacity.probe.reranker_measured_at` | str/null | — | <=0.9.73 | `telemetry:capacity.probe.reranker_measured_at` | 0.9.75 | — | — |
| `capacity.probe.reranker_status` | str/null | — | <=0.9.73 | `telemetry:capacity.probe.reranker_status` | 0.9.75 | — | — |
| `capacity.timestamp` | str | — | <=0.9.73 | — | — | — | — |
| `capacity.trigger` | str | — | <=0.9.73 | `telemetry:capacity.trigger` | 0.9.75 | — | — |

### encoders

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `backend_capability.*.ceiling_s` | float | _s | <=0.9.73 | — | — | — | — |
| `backend_capability.*.last_ok_at` | str/null | — | <=0.9.73 | — | — | — | — |
| `backend_capability.*.latency_s` | float | _s | <=0.9.73 | — | — | — | — |
| `backend_capability.*.probe_chars` | int | _chars | <=0.9.73 | — | — | — | — |
| `backend_capability.*.projected_full_payload_s` | float | _s | <=0.9.73 | — | — | — | — |
| `backend_capability.*.projection_age_s` | float/null | _s | <=0.9.73 | — | — | — | — |
| `backend_capability.*.projection_stale` | bool | — | <=0.9.73 | — | — | — | — |
| `backend_capability.*.serves_full_payload` | bool | — | <=0.9.73 | — | — | — | — |
| `backend_capability.*.status` | str | — | <=0.9.73 | — | — | — | — |
| `backend_capability.*.throughput_chars_s` | int/float | — | <=0.9.73 | — | — | — | — |
| `backend_capability.gateway_host_load1` | float/null | — | <=0.9.73 | — | — | — | — |
| `backend_capability.probed_at` | str/null | — | <=0.9.73 | — | — | — | — |
| `backend_capability.status` | str/null | — | <=0.9.73 | — | — | — | — |
| `config.embed_max_chars` | int | _chars | <=0.9.73 | `telemetry:config.embed_max_chars` | 0.9.75 | — | — |
| `embedder` | str | — | <=0.9.73 | — | — | — | ok \| timeout \| down \| http_<code> |
| `reranker` | str | — | <=0.9.73 | — | — | — | ok \| timeout \| down \| http_<code> |

### postgres

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `pgvector.iterative_scan` | bool | — | <=0.9.73 | `telemetry:postgres.pgvector.iterative_scan` | 0.9.75 | — | — |
| `pgvector.version` | str/null | — | <=0.9.73 | `telemetry:postgres.pgvector.version` | 0.9.75 | — | — |

### llm

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `config.llm_affinity.max_inflight` | int | — | <=0.9.73 | `telemetry:config.llm_affinity.max_inflight` | 0.9.75 | — | — |
| `config.llm_affinity.prefix_chars` | int | _chars | <=0.9.73 | `telemetry:config.llm_affinity.prefix_chars` | 0.9.75 | — | — |
| `config.llm_affinity.ttl_s` | float/int | _s | <=0.9.73 | `telemetry:config.llm_affinity.ttl_s` | 0.9.75 | — | — |
| `config.llm_backends[]` | list | — | <=0.9.73 | `telemetry:config.llm_backends` | 0.9.75 | — | — |
| `config.llm_backends[].max_inflight` | int/null | — | <=0.9.73 | `telemetry:config.llm_backends[].max_inflight` | 0.9.75 | — | — |
| `config.llm_backends[].model` | str/null | — | <=0.9.73 | `telemetry:config.llm_backends[].model` | 0.9.75 | — | — |
| `config.llm_backends[].n_ctx` | int/null | — | <=0.9.73 | `telemetry:config.llm_backends[].n_ctx` | 0.9.75 | — | — |
| `config.llm_backends[].price_per_mtok_in` | float/null | — | <=0.9.73 | `telemetry:config.llm_backends[].price_per_mtok_in` | 0.9.75 | — | — |
| `config.llm_backends[].price_per_mtok_out` | float/null | — | <=0.9.73 | `telemetry:config.llm_backends[].price_per_mtok_out` | 0.9.75 | — | — |
| `config.llm_backends[].private_ok` | bool | — | <=0.9.73 | `telemetry:config.llm_backends[].private_ok` | 0.9.75 | — | — |
| `config.llm_backends[].roles` | list/null | — | <=0.9.73 | `telemetry:config.llm_backends[].roles` | 0.9.75 | — | — |
| `config.llm_backends[].url` | str | — | <=0.9.73 | `telemetry:config.llm_backends[].url` | 0.9.75 | — | — |
| `config.llm_backends[].weight` | float | — | <=0.9.73 | `telemetry:config.llm_backends[].weight` | 0.9.75 | — | — |
| `config.llm_pool_tuning.cooldown_s` | float/int | _s | <=0.9.73 | `telemetry:config.llm_pool_tuning.cooldown_s` | 0.9.75 | — | — |
| `config.llm_pool_tuning.fail_threshold` | int | — | <=0.9.73 | `telemetry:config.llm_pool_tuning.fail_threshold` | 0.9.75 | — | — |
| `config.llm_pool_tuning.fail_window_s` | float/int | _s | <=0.9.73 | `telemetry:config.llm_pool_tuning.fail_window_s` | 0.9.75 | — | — |
| `config.llm_pool_tuning.max_tries` | int | — | <=0.9.73 | `telemetry:config.llm_pool_tuning.max_tries` | 0.9.75 | — | — |
| `consolidation.gpu_probe.consecutive_hangs` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.gpu_probe.leaked_children` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.gpu_probe.state` | str | — | <=0.9.73 | — | — | — | — |
| `consolidation.inference_busy` | str | — | <=0.9.73 | — | — | — | the top-level inference_busy, inside the cached snapshot |
| `gpu_probe.consecutive_hangs` | int | — | <=0.9.73 | `telemetry:gpu_probe.consecutive_hangs` | 0.9.75 | — | — |
| `gpu_probe.leaked_children` | int | — | <=0.9.73 | `telemetry:gpu_probe.leaked_children` | 0.9.75 | — | — |
| `gpu_probe.state` | str | — | <=0.9.73 | `telemetry:gpu_probe.state` | 0.9.75 | — | — |
| `inference_busy` | str | — | <=0.9.73 | — | — | — | busy \| idle \| unknown |
| `llm` | str | — | <=0.9.73 | — | — | — | ok \| down — ok iff ANY backend answered |
| `llm_affinity.hit_rate` | float/null | — | <=0.9.73 | `telemetry:llm.affinity.hit_rate` | 0.9.75 | — | — |
| `llm_affinity.hits` | int | — | <=0.9.73 | `telemetry:llm.affinity.hits` | 0.9.75 | — | — |
| `llm_affinity.hot_prefixes` | dict | — | <=0.9.73 | `telemetry:llm.affinity.hot_prefixes` | 0.9.75 | — | empty when no prefix is hot |
| `llm_affinity.hot_prefixes.*.backend` | str | — | <=0.9.73 | `telemetry:llm.affinity.hot_prefixes.*.backend` | 0.9.75 | — | — |
| `llm_affinity.hot_prefixes.*.hits` | int | — | <=0.9.73 | `telemetry:llm.affinity.hot_prefixes.*.hits` | 0.9.75 | — | — |
| `llm_affinity.misses` | int | — | <=0.9.73 | `telemetry:llm.affinity.misses` | 0.9.75 | — | — |
| `llm_backends.*` | str | — | <=0.9.73 | — | — | — | per-backend enum: ok \| timeout \| down \| http_<code> |
| `llm_latency.*.latency_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.latency.*.latency_last_ts` | 0.9.75 | — | — |
| `llm_latency.*.latency_max_s` | float | _s | <=0.9.73 | `telemetry:llm.latency.*.latency_max_s` | 0.9.75 | — | — |
| `llm_latency.*.latency_sum_s` | float | _s | <=0.9.73 | `telemetry:llm.latency.*.latency_sum_s` | 0.9.75 | — | — |
| `llm_latency.*.requests_failed_total` | int | _total | <=0.9.73 | `telemetry:llm.latency.*.requests_failed_total` | 0.9.75 | — | — |
| `llm_latency.*.requests_total` | int | _total | <=0.9.73 | `telemetry:llm.latency.*.requests_total` | 0.9.75 | — | — |
| `llm_oldest_inflight_age_s` | float | _s | <=0.9.73 | `telemetry:llm.oldest_inflight_age_s` | 0.9.75 | — | — |
| `llm_pool.*.cooldown` | float | _s | <=0.9.73 | `telemetry:llm.pool.*.cooldown` | 0.9.75 | — | — |
| `llm_pool.*.fails` | int | — | <=0.9.73 | `telemetry:llm.pool.*.fails` | 0.9.75 | — | — |
| `llm_pool.*.inflight` | int | — | <=0.9.73 | `telemetry:llm.pool.*.inflight` | 0.9.75 | — | — |
| `llm_pool.*.reserved` | bool | — | <=0.9.73 | `telemetry:llm.pool.*.reserved` | 0.9.75 | — | — |
| `llm_pool.*.routed` | int | — | <=0.9.73 | `telemetry:llm.pool.*.routed` | 0.9.75 | — | — |
| `llm_pool.*.routed_pct` | float | _pct | <=0.9.73 | `telemetry:llm.pool.*.routed_pct` | 0.9.75 | — | — |
| `llm_pool.*.weight` | float | — | <=0.9.73 | `telemetry:llm.pool.*.weight` | 0.9.75 | — | — |
| `llm_reserved[]` | list | — | <=0.9.73 | `telemetry:llm.reserved` | 0.9.75 | — | — |
| `llm_routing.routed_role_extract` | int | — | <=0.9.73 | `telemetry:llm.routing.routed_role_extract` | 0.9.75 | — | — |
| `llm_routing.routed_role_extract_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.routing.routed_role_extract_last_ts` | 0.9.75 | — | — |
| `llm_routing.routed_role_judge` | int | — | <=0.9.73 | `telemetry:llm.routing.routed_role_judge` | 0.9.75 | — | — |
| `llm_routing.routed_role_judge_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.routing.routed_role_judge_last_ts` | 0.9.75 | — | — |
| `llm_routing.routing_backend_at_capacity` | int | — | <=0.9.73 | `telemetry:llm.routing.routing_backend_at_capacity` | 0.9.75 | — | — |
| `llm_routing.routing_backend_at_capacity_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.routing.routing_backend_at_capacity_last_ts` | 0.9.75 | — | — |
| `llm_routing.routing_fit_rejected` | int | — | <=0.9.73 | `telemetry:llm.routing.routing_fit_rejected` | 0.9.75 | — | — |
| `llm_routing.routing_fit_rejected_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.routing.routing_fit_rejected_last_ts` | 0.9.75 | — | — |
| `llm_routing.routing_no_eligible_backend` | int | — | <=0.9.73 | `telemetry:llm.routing.routing_no_eligible_backend` | 0.9.75 | — | — |
| `llm_routing.routing_no_eligible_backend_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.routing.routing_no_eligible_backend_last_ts` | 0.9.75 | — | — |
| `llm_suspect_wedged[]` | list | — | <=0.9.73 | `telemetry:llm.suspect_wedged` | 0.9.75 | — | — |
| `llm_token_usage.*.tokens_completion_total` | int | _total | <=0.9.73 | `telemetry:llm.token_usage.*.tokens_completion_total` | 0.9.75 | — | — |
| `llm_token_usage.*.tokens_last_ts` | str/null | — | <=0.9.73 | `telemetry:llm.token_usage.*.tokens_last_ts` | 0.9.75 | — | — |
| `llm_token_usage.*.tokens_prompt_total` | int | _total | <=0.9.73 | `telemetry:llm.token_usage.*.tokens_prompt_total` | 0.9.75 | — | — |

### rem

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `rem_daemon` | str | — | <=0.9.73 | `health:rem_daemon_process` | 0.9.75 | — | the REM daemon's PID check under its pre-0.9.74 name |
| `rem_daemon_process` | str | — | 0.9.74 | — | — | — | running \| stopped — a PID check, nothing more |

### nrem/consolidation

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `consolidation.fresh` | bool | — | <=0.9.73 | — | — | — | false means the 60 s refresher's last pass FAILED — the snapshot is stale, not a verdict about the system |
| `consolidation.last_outcome` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.last_success_age_seconds` | int/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.last_success_cycle_type` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.stalled` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.stalled_types[]` | list | — | <=0.9.73 | — | — | — | — |
| `daemon` | str | — | <=0.9.73 | `health:nrem_daemon_process` | 0.9.75 | — | the NREM daemon's PID check under its pre-0.9.74 name |
| `nrem_daemon_process` | str | — | 0.9.74 | — | — | — | running \| stopped — a PID check, nothing more |

### axes/registry

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `consolidation.domain_identity.complete` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.domain_identity.mismatched` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.domain_identity.nodes` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.domain_identity.registry_rows` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.domain_identity.unattached` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.domain_identity.unregistered` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.project_identity.complete` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.project_identity.mismatched` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.project_identity.nodes` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.project_identity.unidentified` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.project_identity.unregistered` | int | — | <=0.9.73 | — | — | — | — |
| `domain_identity.complete` | bool | — | <=0.9.73 | `telemetry:axes.domain_identity.complete` | 0.9.75 | — | — |
| `domain_identity.mismatched` | int | — | <=0.9.73 | `telemetry:axes.domain_identity.mismatched` | 0.9.75 | — | — |
| `domain_identity.nodes` | int | — | <=0.9.73 | `telemetry:axes.domain_identity.nodes` | 0.9.75 | — | — |
| `domain_identity.registry_rows` | int | — | <=0.9.73 | `telemetry:axes.domain_identity.registry_rows` | 0.9.75 | — | — |
| `domain_identity.unattached` | int | — | <=0.9.73 | `telemetry:axes.domain_identity.unattached` | 0.9.75 | — | — |
| `domain_identity.unregistered` | int | — | <=0.9.73 | `telemetry:axes.domain_identity.unregistered` | 0.9.75 | — | — |
| `project_identity.complete` | bool | — | <=0.9.73 | `telemetry:axes.project_identity.complete` | 0.9.75 | — | — |
| `project_identity.mismatched` | int | — | <=0.9.73 | `telemetry:axes.project_identity.mismatched` | 0.9.75 | — | — |
| `project_identity.nodes` | int | — | <=0.9.73 | `telemetry:axes.project_identity.nodes` | 0.9.75 | — | — |
| `project_identity.unidentified` | int | — | <=0.9.73 | `telemetry:axes.project_identity.unidentified` | 0.9.75 | — | — |
| `project_identity.unregistered` | int | — | <=0.9.73 | `telemetry:axes.project_identity.unregistered` | 0.9.75 | — | — |

### credentials

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `config.allow_unauthenticated_provider_keys` | bool | — | <=0.9.73 | `telemetry:config.allow_unauthenticated_provider_keys` | 0.9.75 | — | present ONLY while the S-05 override is actually in effect |
| `config.llm_backends[].has_credential` | bool | — | <=0.9.73 | `telemetry:config.llm_backends[].has_credential` | 0.9.75 | — | — |

### versions

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `api_version` | int | — | <=0.9.73 | — | — | — | — |
| `version` | str | — | <=0.9.73 | — | — | — | — |

### graph

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `consolidation.graph_invalid_nodes` | int/null | — | <=0.9.73 | — | — | — | — |
| `graph_invalid_nodes` | int/null | — | <=0.9.73 | `telemetry:graph_integrity.invalid_nodes` | 0.9.75 | — | — |

## `GET /memory/telemetry`

The envelope is `{"status": "success", "telemetry": {…}}`; paths below are relative to the `telemetry` wrapper. Every section is computed independently and degrades to `{"error": "…"}` on its own failure, so one dead backend never blanks the payload.

### liveness

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `generated_at` | str | — | 0.9.74 | — | — | — | when this payload was BUILT. Differs from `timestamp` by up to TELEMETRY_CACHE_S — a cached payload is served stale on purpose, and a reader must be able to tell how stale. |
| `timestamp` | str | — | <=0.9.73 | — | — | — | when this payload was SERVED |

### capacity

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `capacity` | null | — | 0.9.74 | — | — | — | null until the first derivation of this deployment's lifetime |
| `capacity.derived.client_ceiling_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `capacity.derived.payload_basis` | str | — | 0.9.74 | — | — | — | — |
| `capacity.derived.payload_basis_sample_count` | int | — | 0.9.74 | — | — | — | — |
| `capacity.derived.payload_max_chars_measured` | int/null | _chars | 0.9.74 | — | — | — | — |
| `capacity.derived.payload_mean_chars_measured` | int/float/null | _chars | 0.9.74 | — | — | — | — |
| `capacity.derived.queue_bound` | int/null | — | 0.9.74 | — | — | — | — |
| `capacity.derived.recommended_reranker_mem_limit_bytes` | int/null | _bytes | 0.9.74 | — | — | — | — |
| `capacity.derived.s_max_measured_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `capacity.derived.s_mean_measured_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `capacity.derived.s_mean_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `capacity.derived.single_search_exceeds_wait` | bool | — | 0.9.74 | — | — | — | — |
| `capacity.derived.tolerable_wait_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.cpu_encoder_replicas` | str/int/null | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.embedder_url` | str | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.gpu_encoder_replicas` | str/int/null | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.rerank_max_doc_chars` | int | _chars | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.reranker_url` | str | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.encoder_config.search_candidate_floor` | int | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.hardware.gpu_present` | bool | — | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.hardware.mem_total_bytes` | int | _bytes | 0.9.74 | — | — | — | — |
| `capacity.fingerprint.hardware.nproc` | int | — | 0.9.74 | — | — | — | — |
| `capacity.probe.embedder_chars_per_s` | int/float/null | — | 0.9.74 | — | — | — | — |
| `capacity.probe.embedder_measured_at` | str/null | — | 0.9.74 | — | — | — | — |
| `capacity.probe.probe_stale` | bool | — | 0.9.74 | — | — | — | — |
| `capacity.probe.probed_at` | str/null | — | 0.9.74 | — | — | — | — |
| `capacity.probe.reranker_chars_per_s` | int/float/null | — | 0.9.74 | — | — | — | — |
| `capacity.probe.reranker_measured_at` | str/null | — | 0.9.74 | — | — | — | — |
| `capacity.probe.reranker_status` | str/null | — | 0.9.74 | — | — | — | — |
| `capacity.timestamp` | str | — | 0.9.74 | — | — | — | — |
| `capacity.trigger` | str | — | 0.9.74 | — | — | — | — |

### encoders

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `config.embed_max_chars` | int | _chars | 0.9.74 | — | — | — | — |
| `encoders.embed.calls` | int | — | 0.9.74 | — | — | — | — |
| `encoders.embed.errors` | int | — | 0.9.74 | — | — | — | — |
| `encoders.embed.last_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.embed.last_payload_chars` | int/null | _chars | 0.9.74 | — | — | — | — |
| `encoders.embed.max_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.embed.p50_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.embed.p95_ms` | float/null | _ms | 0.9.74 | — | — | `health.warning.encoder_p95_ms` | — |
| `encoders.embed.window` | int | — | 0.9.74 | — | — | — | observations the percentiles were computed over — NOT the ring's capacity. p95 over 3 calls is not a p95. |
| `encoders.limit_ms` | float/null | _ms | 0.9.74 | — | — | — | ENCODER_LATENCY_WARN_MS — the limit the p95s above are compared against; null means it is derived per-encoder from backend_capability.ceiling_s rather than pinned by env. |
| `encoders.rerank.calls` | int | — | 0.9.74 | — | — | — | — |
| `encoders.rerank.errors` | int | — | 0.9.74 | — | — | — | — |
| `encoders.rerank.last_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.rerank.last_payload_chars` | int/null | _chars | 0.9.74 | — | — | — | — |
| `encoders.rerank.max_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.rerank.p50_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `encoders.rerank.p95_ms` | float/null | _ms | 0.9.74 | — | — | `health.warning.encoder_p95_ms` | — |
| `encoders.rerank.window` | int | — | 0.9.74 | — | — | — | — |
| `rerank_fallbacks_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `rerank_fallbacks_total` | int | _total | <=0.9.73 | — | — | — | — |
| `rerank_payload_chars_max` | int | _chars | <=0.9.73 | — | — | — | — |
| `rerank_payload_chars_total` | int | _total | <=0.9.73 | — | — | — | — |
| `rerank_payload_docs_total` | int | _total | <=0.9.73 | — | — | — | — |
| `rerank_successes_total` | int | _total | <=0.9.73 | — | — | — | — |

### gateway

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `gateway.by_status.2xx` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.401` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.403` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.409` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.4xx` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.503` | int | — | 0.9.74 | — | — | — | — |
| `gateway.by_status.5xx` | int | — | 0.9.74 | — | — | — | — |
| `gateway.inflight` | int | — | 0.9.74 | — | — | — | — |
| `gateway.inflight_max` | int | — | 0.9.74 | — | — | — | GATEWAY_INFLIGHT_MAX; 0 = valve disabled |
| `gateway.latency_p50_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `gateway.latency_p95_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `gateway.latency_window` | int | — | 0.9.74 | — | — | — | — |
| `gateway.requests_total` | int | _total | 0.9.74 | — | — | — | — |
| `gateway.shed_503_total` | int | _total | 0.9.74 | — | — | `health.warning.pool_shedding` | — |

### outbox

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `outbox.age_limit_s` | int | _s | 0.9.74 | — | — | — | OUTBOX_AGE_WARN_S — the limit oldest_pending_age_s is compared against |
| `outbox.applied` | int | — | 0.9.74 | — | — | — | — |
| `outbox.apply_latency_p50_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `outbox.apply_latency_p95_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `outbox.apply_latency_window` | int | — | 0.9.74 | — | — | — | — |
| `outbox.drain_rate_per_min` | float/null | — | 0.9.74 | — | — | — | — |
| `outbox.error` | str | — | 0.9.74 | — | — | — | present only when this section's own query failed |
| `outbox.failed` | int | — | 0.9.74 | — | — | `health.outbox` | ⛔ ALWAYS PRESENT, 0 when zero. The pre-0.9.74 `postgres.outbox` census omitted the key entirely at zero, so absence and zero were indistinguishable to every consumer. |
| `outbox.oldest_failed_age_s` | int/null | _s | 0.9.74 | — | — | — | — |
| `outbox.oldest_pending_age_s` | int/null | _s | 0.9.74 | — | — | `health.warning.outbox_age` | — |
| `outbox.pending` | int | — | 0.9.74 | — | — | — | — |
| `outbox.rem_reviewed` | int | — | 0.9.74 | — | — | — | — |
| `postgres.outbox` | dict | — | <=0.9.73 | `telemetry:outbox` | 0.9.75 | — | emitted as an empty dict when the outbox is empty |
| `postgres.outbox.*` | int | — | <=0.9.73 | `telemetry:outbox` | 0.9.75 | — | the status census; a status with zero rows was OMITTED, which is why it moved |
| `postgres.outbox_failed_oldest_age_seconds` | int/null | _seconds | <=0.9.73 | `telemetry:outbox.oldest_failed_age_s` | 0.9.75 | — | — |

### postgres

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `postgres.community_summaries.superseded` | int | — | <=0.9.73 | — | — | — | — |
| `postgres.community_summaries.total` | int | — | <=0.9.73 | — | — | — | — |
| `postgres.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `postgres.pgvector.iterative_scan` | bool | — | 0.9.74 | — | — | — | — |
| `postgres.pgvector.version` | str/null | — | 0.9.74 | — | — | — | — |
| `postgres.pool_free` | int | — | 0.9.74 | — | — | — | — |
| `postgres.pool_in_use` | int | — | 0.9.74 | — | — | — | — |
| `postgres.pool_size` | int | — | 0.9.74 | — | — | — | — |
| `postgres.pool_wait_p50_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `postgres.pool_wait_p95_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `postgres.pool_wait_window` | int | — | 0.9.74 | — | — | — | — |
| `postgres.technical_docs` | int | — | <=0.9.73 | — | — | — | — |
| `postgres.technical_docs_superseded` | int | — | <=0.9.73 | — | — | — | — |

### neo4j

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `neo4j.cypher_rejected_total` | int | _total | 0.9.74 | — | — | — | the /memory/graph route's guard refusals |
| `neo4j.decisions_total` | int | — | <=0.9.73 | — | — | — | — |
| `neo4j.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `neo4j.facts_total` | int | — | <=0.9.73 | — | — | — | — |
| `neo4j.query_p50_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `neo4j.query_p95_ms` | float/null | _ms | 0.9.74 | — | — | — | — |
| `neo4j.query_window` | int | — | 0.9.74 | — | — | — | — |
| `neo4j.tx_failures_total` | int | _total | 0.9.74 | — | — | — | failed graph-route queries + failed outbox applies |

### llm

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `config.llm_affinity.max_inflight` | int | — | 0.9.74 | — | — | — | — |
| `config.llm_affinity.prefix_chars` | int | _chars | 0.9.74 | — | — | — | — |
| `config.llm_affinity.ttl_s` | float/int | _s | 0.9.74 | — | — | — | — |
| `config.llm_backends[]` | list | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].max_inflight` | int/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].model` | str/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].n_ctx` | int/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].price_per_mtok_in` | float/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].price_per_mtok_out` | float/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].private_ok` | bool | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].roles` | list/null | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].url` | str | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].weight` | float | — | 0.9.74 | — | — | — | — |
| `config.llm_pool_tuning.cooldown_s` | float/int | _s | 0.9.74 | — | — | — | — |
| `config.llm_pool_tuning.fail_threshold` | int | — | 0.9.74 | — | — | — | — |
| `config.llm_pool_tuning.fail_window_s` | float/int | _s | 0.9.74 | — | — | — | — |
| `config.llm_pool_tuning.max_tries` | int | — | 0.9.74 | — | — | — | — |
| `gpu_probe` | null | — | 0.9.74 | — | — | — | null until the first probe |
| `gpu_probe.consecutive_hangs` | int | — | 0.9.74 | — | — | — | — |
| `gpu_probe.leaked_children` | int | — | 0.9.74 | — | — | — | — |
| `gpu_probe.state` | str | — | 0.9.74 | — | — | — | — |
| `inference_busy` | str | — | <=0.9.73 | — | — | — | busy \| idle \| unknown |
| `llm.affinity.hit_rate` | float/null | — | 0.9.74 | — | — | — | — |
| `llm.affinity.hits` | int | — | 0.9.74 | — | — | — | — |
| `llm.affinity.hot_prefixes` | dict | — | 0.9.74 | — | — | — | empty when no prefix is hot |
| `llm.affinity.hot_prefixes.*.backend` | str | — | 0.9.74 | — | — | — | — |
| `llm.affinity.hot_prefixes.*.hits` | int | — | 0.9.74 | — | — | — | — |
| `llm.affinity.misses` | int | — | 0.9.74 | — | — | — | — |
| `llm.backends` | dict | — | 0.9.74 | — | — | — | empty when no backend is configured |
| `llm.backends.*` | str | — | 0.9.74 | — | — | — | — |
| `llm.faults` | dict | — | 0.9.74 | — | — | — | empty until a fault occurs |
| `llm.faults.*.gateway.count` | int | — | 0.9.74 | — | — | — | — |
| `llm.faults.*.gateway.last` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.faults.*.llm.transient.count` | int | — | 0.9.74 | — | — | — | — |
| `llm.faults.*.llm.transient.last` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.latency` | dict | — | 0.9.74 | — | — | — | empty when no backend is configured |
| `llm.latency.*.latency_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.latency.*.latency_max_s` | float | _s | 0.9.74 | — | — | — | — |
| `llm.latency.*.latency_sum_s` | float | _s | 0.9.74 | — | — | — | — |
| `llm.latency.*.requests_failed_total` | int | _total | 0.9.74 | — | — | — | — |
| `llm.latency.*.requests_total` | int | _total | 0.9.74 | — | — | — | — |
| `llm.oldest_inflight_age_s` | float/null | _s | 0.9.74 | — | — | — | — |
| `llm.pool.*.cooldown` | float | _s | 0.9.74 | — | — | — | — |
| `llm.pool.*.fails` | int | — | 0.9.74 | — | — | — | — |
| `llm.pool.*.inflight` | int | — | 0.9.74 | — | — | — | — |
| `llm.pool.*.reserved` | bool | — | 0.9.74 | — | — | — | — |
| `llm.pool.*.routed` | int | — | 0.9.74 | — | — | — | — |
| `llm.pool.*.routed_pct` | float | _pct | 0.9.74 | — | — | — | — |
| `llm.pool.*.weight` | float | — | 0.9.74 | — | — | — | — |
| `llm.reserved[]` | list | — | 0.9.74 | — | — | — | — |
| `llm.routing.routed_role_extract` | int | — | 0.9.74 | — | — | — | — |
| `llm.routing.routed_role_extract_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.routing.routed_role_judge` | int | — | 0.9.74 | — | — | — | — |
| `llm.routing.routed_role_judge_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_backend_at_capacity` | int | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_backend_at_capacity_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_fit_rejected` | int | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_fit_rejected_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_no_eligible_backend` | int | — | 0.9.74 | — | — | — | — |
| `llm.routing.routing_no_eligible_backend_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.status` | str/null | — | 0.9.74 | — | — | — | ok \| down — ok iff ANY backend answered. Read off the /health probe cache, so it is null until the first /health build of this process: a telemetry request must never fire the backend fan-out itself. |
| `llm.suspect_wedged[]` | list | — | 0.9.74 | — | — | — | — |
| `llm.token_usage` | dict | — | 0.9.74 | — | — | — | empty when no backend is configured |
| `llm.token_usage.*.tokens_completion_total` | int | _total | 0.9.74 | — | — | — | — |
| `llm.token_usage.*.tokens_last_ts` | str/null | — | 0.9.74 | — | — | — | — |
| `llm.token_usage.*.tokens_prompt_total` | int | _total | 0.9.74 | — | — | — | — |
| `llm_faults` | dict | — | <=0.9.73 | `telemetry:llm.faults` | 0.9.75 | — | empty until a fault occurs |
| `llm_faults.*.gateway.count` | int | — | <=0.9.73 | `telemetry:llm.faults.*.gateway.count` | 0.9.75 | — | — |
| `llm_faults.*.gateway.last` | str/null | — | <=0.9.73 | `telemetry:llm.faults.*.gateway.last` | 0.9.75 | — | — |
| `llm_faults.*.llm.transient.count` | int | — | <=0.9.73 | `telemetry:llm.faults.*.llm.transient.count` | 0.9.75 | — | — |
| `llm_faults.*.llm.transient.last` | str/null | — | <=0.9.73 | `telemetry:llm.faults.*.llm.transient.last` | 0.9.75 | — | — |

### rem

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `latency.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `latency.rem_ms.by_model[]` | list | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].backend` | str/null | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].contention_ms.p50` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].contention_ms.p95` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].max_batch_size` | int/null | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].model` | str/null | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].n` | int | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].n_service` | int | — | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].service_ms.p50` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].service_ms.p95` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].timing_source` | str | — | <=0.9.73 | — | — | — | server \| wall \| mixed |
| `latency.rem_ms.by_model[].wall_ms.p50` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.by_model[].wall_ms.p95` | float/null | _ms | <=0.9.73 | — | — | — | — |
| `latency.rem_ms.note` | str | — | <=0.9.73 | — | — | — | — |
| `neo4j.decisions_rem_pending` | int | — | <=0.9.73 | — | — | — | — |
| `neo4j.facts_rem_pending` | int | — | <=0.9.73 | — | — | — | — |
| `neo4j.rem_dead_lettered` | int | — | <=0.9.73 | `telemetry:rem.dead_lettered` | 0.9.75 | — | — |
| `neo4j.rem_failing` | int | — | <=0.9.73 | `telemetry:rem.failing` | 0.9.75 | — | — |
| `neo4j.rem_max_attempts` | int | — | <=0.9.73 | `telemetry:rem.max_attempts` | 0.9.75 | — | — |
| `neo4j.rem_passed_over_total` | int | _total | <=0.9.73 | `telemetry:rem.passed_over` | 0.9.75 | — | — |
| `neo4j.rem_starved_pending` | int | — | <=0.9.73 | `telemetry:rem.starved_pending` | 0.9.75 | — | — |
| `rem.dead_lettered` | int | — | 0.9.74 | — | — | `health.rem_daemon` | — |
| `rem.degeneration_firings` | int/null | — | 0.9.74 | — | — | — | ⚠ ALWAYS NULL AT 0.9.74, and null is the honest value. REM runs in a SEPARATE PROCESS (rem_loop.py); its anti-degeneration detector writes a log line and nothing durable, so the gateway cannot see it. Reporting 0 would claim it never fired. A durable counter is owed. |
| `rem.error` | str | — | 0.9.74 | — | — | — | present only when this section's own query failed |
| `rem.failing` | int | — | 0.9.74 | — | — | — | — |
| `rem.max_attempts` | int | — | 0.9.74 | — | — | — | REM_MAX_ATTEMPTS — the limit dead_lettered counts arrivals at |
| `rem.passed_over` | int | — | 0.9.74 | — | — | — | — |
| `rem.starved_pending` | int | — | 0.9.74 | — | — | — | — |
| `rem.throughput_per_hour` | float/null | — | 0.9.74 | — | — | — | records REM stamped in the last hour, from the durable technical_docs.rem_timing clock |

### nrem/consolidation

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `breakdown.summaries[]` | list | — | <=0.9.73 | — | — | — | — |
| `breakdown.summaries[].active` | int | — | <=0.9.73 | — | — | — | — |
| `breakdown.summaries[].kind` | str | — | <=0.9.73 | — | — | — | — |
| `breakdown.summaries[].superseded` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.backlog` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.consecutive_failures` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.cycle_seconds_avg` | float/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.*.dead_lettered_clusters` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.deferred_24h` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.eligible_clusters` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.eligible_oldest_age_seconds` | int/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.*.folds_attempted_24h` | int | — | <=0.9.73 | — | — | `health.nrem_daemon` | — |
| `consolidation.*.folds_succeeded_24h` | int | — | <=0.9.73 | — | — | `health.nrem_daemon` | — |
| `consolidation.*.idle_24h` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.in_flight` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_deferred_reason` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_error` | null | — | <=0.9.73 | — | — | — | null when no error is on record |
| `consolidation.*.last_error.age_seconds` | int/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_error.class` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_error.msg` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_error.superseded` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_outcome` | str/null | — | <=0.9.73 | — | — | — | per cycle type — `insight` and `fact_consolidation` today |
| `consolidation.*.last_started` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.last_success_age_seconds` | int/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.*.runs_24h` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.singleton_clusters` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.slot_failures` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.slot_failures_24h` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.stalled` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.truncation_failures` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.truncation_failures_24h` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.*.unchanged_clusters` | int | — | <=0.9.73 | — | — | — | — |
| `consolidation.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `consolidation.last_active_cycle_type` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.last_deferred_reason` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.last_outcome` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.last_success_age_seconds` | int/null | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.last_success_cycle_type` | str/null | — | <=0.9.73 | — | — | — | — |
| `consolidation.stall_threshold_seconds` | int | _seconds | <=0.9.73 | — | — | — | — |
| `consolidation.stalled` | bool | — | <=0.9.73 | — | — | — | — |
| `consolidation.stalled_types[]` | list | — | <=0.9.73 | — | — | — | — |
| `latency.nrem_cycle_seconds` | dict | — | <=0.9.73 | — | — | — | empty dict when no cycle has finished |
| `latency.nrem_cycle_seconds.n` | int | — | <=0.9.73 | — | — | — | — |
| `latency.nrem_cycle_seconds.note` | str | — | <=0.9.73 | — | — | — | — |
| `latency.nrem_cycle_seconds.p50` | float/null | _seconds | <=0.9.73 | — | — | — | — |
| `latency.nrem_cycle_seconds.p95` | float/null | _seconds | <=0.9.73 | — | — | — | — |
| `latency.nrem_cycle_seconds.window_days` | int | — | <=0.9.73 | — | — | — | — |
| `neo4j.facts_unconsolidated` | int | — | <=0.9.73 | — | — | — | — |
| `nrem.as_of` | str/null | — | 0.9.74 | — | — | — | when the 60 s refresher last computed this section. MEASURED 2026-08-28 on this corpus: the insight walk is 149 SEQUENTIAL Neo4j round-trips (8 gating groups x 9-26 BFS layers each, unbounded by construction — the walk has no hop cap), so it moved out of the request path. |
| `nrem.error` | str | — | <=0.9.73 | — | — | — | present only when the refresher's last pass failed |
| `nrem.fact_cycles` | int | — | <=0.9.73 | — | — | — | — |
| `nrem.fact_threshold` | int | — | <=0.9.73 | — | — | — | ONT.density_threshold |
| `nrem.total_cycles` | int | — | <=0.9.73 | — | — | — | — |

### insight

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `nrem.decision_cycles` | int | — | <=0.9.73 | — | — | — | — |
| `postgres.community_summaries.insight` | int | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_status_reason[]` | list | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_status_reason[].closed_reason` | str/null | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_status_reason[].count` | int | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_status_reason[].status` | str | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_trigger_kind` | dict | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.by_trigger_kind.*` | int | — | <=0.9.73 | — | — | — | — |
| `refold_ledger.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `refold_ledger.insight_reconciliation_stuck` | int | — | <=0.9.73 | — | — | — | — |

### axes/registry

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `axes.domain_identity` | null | — | 0.9.74 | — | — | — | null until the first refresher pass |
| `axes.domain_identity.complete` | bool | — | 0.9.74 | — | — | — | — |
| `axes.domain_identity.mismatched` | int | — | 0.9.74 | — | — | — | — |
| `axes.domain_identity.nodes` | int | — | 0.9.74 | — | — | — | — |
| `axes.domain_identity.registry_rows` | int | — | 0.9.74 | — | — | — | — |
| `axes.domain_identity.unattached` | int | — | 0.9.74 | — | — | — | — |
| `axes.domain_identity.unregistered` | int | — | 0.9.74 | — | — | — | — |
| `axes.project_identity` | null | — | 0.9.74 | — | — | — | null until the first refresher pass |
| `axes.project_identity.complete` | bool | — | 0.9.74 | — | — | — | — |
| `axes.project_identity.mismatched` | int | — | 0.9.74 | — | — | — | — |
| `axes.project_identity.nodes` | int | — | 0.9.74 | — | — | — | — |
| `axes.project_identity.unidentified` | int | — | 0.9.74 | — | — | — | — |
| `axes.project_identity.unregistered` | int | — | 0.9.74 | — | — | — | — |
| `axis_registry_read_failures_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `axis_registry_read_failures_total` | int | _total | <=0.9.73 | `telemetry:registry.read_failures_total` | 0.9.75 | — | — |
| `breakdown.domains[]` | list | — | <=0.9.73 | — | — | — | ⚠ MEANING CHANGED IN 0.9.74 — see _meaning_changes. Before 0.9.74 this carried the PROJECT distribution; it now carries the DOMAIN distribution from metadata->'domains'. The project distribution is breakdown.projects. |
| `breakdown.domains[].count` | int | — | <=0.9.73 | — | — | — | — |
| `breakdown.domains[].key` | str | — | <=0.9.73 | — | — | — | — |
| `breakdown.projects[]` | list | — | 0.9.74 | — | — | — | the PROJECT distribution, under its true name |
| `breakdown.projects[].count` | int | — | 0.9.74 | — | — | — | — |
| `breakdown.projects[].key` | str | — | 0.9.74 | — | — | — | — |
| `registry.aliases` | int | — | 0.9.74 | — | — | — | — |
| `registry.domains` | int | — | 0.9.74 | — | — | — | — |
| `registry.error` | str | — | 0.9.74 | — | — | — | present only when this section's own query failed |
| `registry.projects` | int | — | 0.9.74 | — | — | — | — |
| `registry.read_failures_total` | int | _total | 0.9.74 | — | — | `health.registry` | — |
| `registry.refusals.axis_conflict` | int | — | 0.9.74 | — | — | — | — |
| `registry.refusals.entities_not_allowed_on_judgement` | int | — | 0.9.74 | — | — | — | — |
| `registry.refusals.entity_confusable` | int | — | 0.9.74 | — | — | — | — |
| `registry.refusals.entity_reserved` | int | — | 0.9.74 | — | — | — | — |
| `registry.refusals.entity_unknown` | int | — | 0.9.74 | — | — | — | — |
| `registry.refusals.new_domain_refused` | int | — | 0.9.74 | — | — | — | aggregates the domain-naming refusals: domain_unnameable, domain_spelling_variant, domain_confusable, domain_unknown, domain_without_project, domain_not_allowed_on_judgement |
| `registry.refusals.new_project_refused` | int | — | 0.9.74 | — | — | — | aggregates the project-naming refusals: project_unnameable, project_spelling_variant, project_confusable |

### credentials

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `config.allow_unauthenticated_provider_keys` | bool | — | 0.9.74 | — | — | — | — |
| `config.llm_backends[].has_credential` | bool | — | 0.9.74 | — | — | — | — |
| `credentials.audit_log_dropped` | int | — | <=0.9.73 | — | — | — | — |
| `credentials.audit_log_dropped_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `credentials.credentialed_route_denied` | int | — | <=0.9.73 | — | — | — | — |
| `credentials.credentialed_route_denied_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `credentials.daemon_tokens_issued` | int | — | <=0.9.73 | — | — | — | — |
| `credentials.daemon_tokens_issued_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `credentials.token_verify_failed` | int | — | <=0.9.73 | — | — | `health.warning.token_verify_failed` | — |
| `credentials.token_verify_failed_last_ts` | str/null | — | <=0.9.73 | — | — | — | — |
| `credentials.token_verify_warn_per_min` | int/float | — | 0.9.74 | — | — | — | TOKEN_VERIFY_WARN_PER_MIN — the limit the warning is raised at |
| `llm.faults.*.llm.credential.count` | int | — | 0.9.74 | — | — | — | — |
| `llm.faults.*.llm.credential.last` | str/null | — | 0.9.74 | — | — | — | — |
| `llm_faults.*.llm.credential.count` | int | — | <=0.9.73 | `telemetry:llm.faults.*.llm.credential.count` | 0.9.75 | — | — |
| `llm_faults.*.llm.credential.last` | str/null | — | <=0.9.73 | `telemetry:llm.faults.*.llm.credential.last` | 0.9.75 | — | — |

### versions

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `clients.versions_seen` | dict | — | 0.9.74 | — | — | — | empty until a 0.9.74+ client calls |
| `clients.versions_seen.*` | int | — | 0.9.74 | — | — | — | {client VERSION string: requests seen this process}. Fed by the X-Shared-Memory-Client header both front doors now send. |

### spine

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `breakdown.agents[]` | list | — | <=0.9.73 | — | — | — | — |
| `breakdown.agents[].count` | int | — | <=0.9.73 | — | — | — | — |
| `breakdown.agents[].key` | str | — | <=0.9.73 | — | — | — | — |
| `breakdown.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `breakdown.record_types[]` | list | — | <=0.9.73 | — | — | — | — |
| `breakdown.record_types[].count` | int | — | <=0.9.73 | — | — | — | — |
| `breakdown.record_types[].key` | str | — | <=0.9.73 | — | — | — | — |
| `breakdown.sources[]` | list | — | <=0.9.73 | — | — | — | — |
| `breakdown.sources[].count` | int | — | <=0.9.73 | — | — | — | — |
| `breakdown.sources[].key` | str | — | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.decisions` | int | — | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.embedded` | int | — | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.embedded_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.entries` | int | — | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.failing` | int | — | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.oldest_pending_age_s` | int/float/null | _s | <=0.9.73 | — | — | — | — |
| `spine.alternative_vectors.pending` | int | — | <=0.9.73 | — | — | — | — |
| `spine.decisions.alternatives_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.decisions.confidence_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.decisions.elicited_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.decisions.grounded_in_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.decisions.total` | int | — | <=0.9.73 | — | — | — | — |
| `spine.emergent_unprojected_fields[]` | list | — | <=0.9.73 | — | — | — | — |
| `spine.emergent_unprojected_fields[].key` | str | — | <=0.9.73 | — | — | — | — |
| `spine.emergent_unprojected_fields[].n` | int | — | <=0.9.73 | — | — | — | — |
| `spine.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `spine.facts.elicited_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.facts.source_ref_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.facts.total` | int | — | <=0.9.73 | — | — | — | — |
| `spine.retrospectives.elicited_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.retrospectives.grounded_in_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.retrospectives.rating_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.retrospectives.target_pg_id_pct` | float | _pct | <=0.9.73 | — | — | — | — |
| `spine.retrospectives.total` | int | — | <=0.9.73 | — | — | — | — |

### graph

| key | type | unit | since | moved to | removed in | log twin | notes |
|---|---|---|---|---|---|---|---|
| `compliance.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `compliance.invalid_labels[]` | list | — | <=0.9.73 | — | — | — | — |
| `compliance.invalid_labels[].count` | int | — | <=0.9.73 | — | — | — | — |
| `compliance.invalid_labels[].name` | str | — | <=0.9.73 | — | — | — | — |
| `compliance.invalid_relationships[]` | list | — | <=0.9.73 | — | — | — | — |
| `compliance.invalid_relationships[].count` | int | — | <=0.9.73 | — | — | — | — |
| `compliance.invalid_relationships[].name` | str | — | <=0.9.73 | — | — | — | — |
| `compliance.label_compliance` | str | — | <=0.9.73 | — | — | — | ok \| non-compliant |
| `compliance.predicate_distribution` | dict | — | <=0.9.73 | — | — | — | — |
| `compliance.predicate_distribution.*` | int | — | <=0.9.73 | — | — | — | a CENSUS of what is in the graph, not of what the ontology allows — a legacy predicate (ALIASES) shows here for as long as edges exist |
| `compliance.relationship_compliance` | str | — | <=0.9.73 | — | — | — | — |
| `entity_graph.entities_total` | int | — | <=0.9.73 | — | — | — | — |
| `entity_graph.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `entity_graph.genuinely_referenced_entities` | int | — | <=0.9.73 | — | — | — | — |
| `entity_graph.orphan_entities` | int | — | <=0.9.73 | — | — | — | — |
| `entity_graph.singleton_entities` | int | — | <=0.9.73 | — | — | — | — |
| `entity_graph.top_hubs[]` | list | — | <=0.9.73 | — | — | — | — |
| `entity_graph.top_hubs[].degree` | int | — | <=0.9.73 | — | — | — | — |
| `entity_graph.top_hubs[].name` | str | — | <=0.9.73 | — | — | — | — |
| `entity_graph.unmentioned_entities` | int | — | <=0.9.73 | — | — | — | — |
| `graph_integrity.by_label` | dict | — | <=0.9.73 | — | — | — | empty when clean |
| `graph_integrity.by_label.*` | int | — | <=0.9.73 | — | — | — | — |
| `graph_integrity.by_reason` | dict | — | <=0.9.73 | — | — | — | empty when clean |
| `graph_integrity.by_reason.*` | int | — | <=0.9.73 | — | — | — | — |
| `graph_integrity.clean` | bool | — | <=0.9.73 | — | — | — | — |
| `graph_integrity.error` | str | — | <=0.9.73 | — | — | — | present only when this section's own query failed |
| `graph_integrity.invalid_nodes` | int | — | <=0.9.73 | — | — | — | — |

