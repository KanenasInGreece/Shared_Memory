"""Built-in default literals only: no os.environ, frozen MappingProxyType, so
check_config Phase A can import this without booting the gateway.

Do not add env reads here — daemon tests reload coordinator/hive_mind_proxy and would
otherwise see a stale first import of this module.

Each row an env-reading consumer takes a fallback from carries an ``idiom`` that
check_config reads to render its verdict. ``"or"`` means an absent OR present-but-empty
variable takes the default (``os.environ.get(name) or default``). ``"get"`` means only
an ABSENT variable takes the default, so a present-but-empty value is used as the empty
string (``os.environ.get(name, default)``). Every row check_config.ENV_ROW_ORDER names
must carry ``idiom``; check_config._idiom_for raises KeyError otherwise.
"""
from types import MappingProxyType

FRAMEWORK_DEFAULTS = MappingProxyType({

    # ── env-default rows — EMBEDDER_URL, RERANKER_URL, LLM_DEFAULT_TARGET,
    # LLM_BACKENDS and the embed knobs take their fallback from this table;
    # EMBED_MAX_CHARS is derived instead (see its note) ──────────────────────

    "EMBEDDER_URL": MappingProxyType({
        "default": "http://localhost:8070",
        "kind": "env-default",
        "idiom": "or",
        "consumers": (
            "hive_mind_proxy.py (EMBEDDER_URL, the ROUTING_MAP base)",
            "coordinator.py (EMBED_URL, via _encoder_url)",
        ),
        "note": (
            "One setting moves BOTH the gateway's raw /v1/embeddings "
            "passthrough and the coordinator's own save/search embedding "
            "calls — see test_coordinator_encoder_urls.py's "
            "test_both_consumers_agree_on_an_empty_value."
        ),
    }),
    "RERANKER_URL": MappingProxyType({
        "default": "http://localhost:8071",
        "kind": "env-default",
        "idiom": "or",
        "consumers": (
            "hive_mind_proxy.py (RERANKER_URL, the ROUTING_MAP base)",
            "coordinator.py (RERANK_URL, via _encoder_url)",
        ),
        "note": "Same one-setting-moves-both-consumers shape as EMBEDDER_URL.",
    }),
    "LLM_DEFAULT_TARGET": MappingProxyType({
        "default": "http://localhost:5000",
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("hive_mind_proxy.py (DEFAULT_TARGET)",),
        "note": (
            "The consumer uses .get(name, default), so a present-but-empty value "
            "is the empty string, not this default. Because the LLM_BACKENDS "
            "fallback wraps it verbatim, the pool becomes [''] (pinned by a test; "
            "normalising the idiom would change what an empty value does)."
        ),
    }),
    "LLM_BACKENDS": MappingProxyType({
        "default": "",
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("hive_mind_proxy.py (_load_llm_backends fallback)",),
        "note": (
            "LLM_BACKENDS_JSON is tried first and wins when it yields any usable "
            "URL; when it is absent, invalid, empty or excludes every entry, the "
            "legacy LLM_BACKENDS CSV is read. Only when that CSV also yields no "
            "URL (absent, empty, or every entry rejected) is the single-entry pool "
            "[(LLM_DEFAULT_TARGET, 1.0)] used."
        ),
    }),
    "EMBED_MAX_CONTEXT_TOKENS": MappingProxyType({
        "default": 8192,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py (EMBED_MAX_CONTEXT_TOKENS)",),
        "note": "Maximum context window (tokens) the embedder/reranker supports.",
    }),
    "EMBED_CHARS_PER_TOKEN": MappingProxyType({
        "default": 3.0,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py (EMBED_CHARS_PER_TOKEN)",),
        "note": "Conservative characters-per-token ratio for text clamping.",
    }),
    "EMBED_SPECIAL_TOKEN_RESERVE": MappingProxyType({
        "default": 2,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py (_parse_special_token_reserve)",),
        "note": (
            "Special tokens reserved for BOS/EOS/framing. A value outside "
            "[0, EMBED_MAX_CONTEXT_TOKENS) or not an integer falls back to this "
            "default."
        ),
    }),
    "EMBED_MAX_CHARS": MappingProxyType({
        "default": 24570,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py (EMBED_MAX_CHARS)",),
        "note": (
            "dream_telemetry.py reads the variable, and when it is unset derives "
            "(EMBED_MAX_CONTEXT_TOKENS - EMBED_SPECIAL_TOKEN_RESERVE) * "
            "EMBED_CHARS_PER_TOKEN; 24570 is that formula at the shipped defaults. "
            "The module never reads this table entry."
        ),
    }),


    # ── hardcoded-literal rows — documented here; the consumers keep their own
    # literal ─────────────────────────────────────────────────────────────

    "NEO4J_URI": MappingProxyType({
        "default": "bolt://localhost:7687",
        "kind": "hardcoded-literal",
        "consumers": (
            "coordinator.py (NEO4J_URI)",
            "rem_loop.py (NEO4J_URI)",
            "consolidation_loop.py (NEO4J_URI)",
            "cleanup_entity_noise.py (NEO4J_URI)",
            "cleanup_foreign_schema.py (NEO4J_URI)",
            "backfill_project_of.py (NEO4J_URI env fallback)",
            "reconcile_project_edges.py (NEO4J_URI env fallback)",
            "reconcile_project_identity.py (NEO4J_URI env fallback)",
            "resolve_references.py (NEO4J_URI env fallback)",
            "entity_fragment_rate.py (NEO4J_URI env fallback)",
            "normalize_projects.py (NEO4J_URI env fallback)",
            "migrate_retro_edges.py (NEO4J_URI env fallback)",
            "migrations/verify_neo4j_init.py (NEO4J_URI env fallback)",
        ),
        "note": (
            "The gateway (coordinator.py) and both daemons (rem_loop.py, "
            "consolidation_loop.py) hardcode this literal and do NOT read the "
            "variable, as do cleanup_entity_noise.py and cleanup_foreign_schema.py. "
            "The maintenance scripts listed in consumers read NEO4J_URI with this "
            "same default."
        ),
    }),

    # ── documented-only rows — documented here; the consumers keep their own
    # literal ─────────────────────────────────────────────────────────────

    "PG_DSN_HOST_PORT": MappingProxyType({
        "default": "localhost:5432",
        "kind": "documented-only",
        "consumers": (
            "coordinator.py (PG_DSN default)",
            "rem_loop.py (PG_CONN default)",
            "consolidation_loop.py (PG_CONN default)",
            "migrations/apply.py (DSN default)",
            "migrations/generate_schema_init.py (DSN default)",
            "normalize_projects.py (PG_CONN default)",
            "migrate_retro_edges.py (PG_CONN default)",
        ),
        "note": (
            "The host:port segment of the constructed default Postgres DSN "
            "(postgresql://postgres:<pw>@localhost:5432/agent_data). The composers "
            "do not all spell the password the same way: some percent-encode it, "
            "others insert it raw. Secret-adjacent (the DSN carries the password); "
            "PG_CONN is the real override knob."
        ),
    }),
    "PROXY_BIND": MappingProxyType({
        "default": "127.0.0.1",
        "kind": "documented-only",
        "idiom": "or",
        "consumers": ("hive_mind_proxy.py (_resolve_proxy_bind_host)",),
        "note": (
            "Set PROXY_BIND=0.0.0.0 to opt into all-interfaces binding. "
            "hive_mind_proxy._resolve_proxy_bind_host maps an absent, empty or "
            "whitespace-only value to 127.0.0.1 before web.TCPSite(runner, "
            "bind_host, PORT), because aiohttp's host '' would bind every "
            "interface. The idiom is therefore 'or' although the kind is "
            "documented-only, and check_config renders the present-but-empty "
            "verdict from it."
        ),
    }),
    "PORT": MappingProxyType({
        "default": 8888,
        "kind": "documented-only",
        "consumers": ("hive_mind_proxy.py (main, argv[1])",),
        "note": (
            "Not an env var — a positional argv[1] "
            "(``python hive_mind_proxy.py [port]``), defaulting to 8888 "
            "when omitted."
        ),
    }),

    # ── not-a-knob rows — deliberately NOT configurable ─────────────────────

    "REASONER_URL": MappingProxyType({
        "default": "http://localhost:8888/v1/chat/completions",
        "kind": "not-a-knob",
        "consumers": (
            "rem_loop.py (REASONER_URL)",
            "consolidation_loop.py (REASONER_URL)",
        ),
        "note": (
            "The daemons' ONE way in is the hive-mind gateway itself — "
            "never a raw LLM. Pointing this directly at a backend would "
            "bypass pooling, cache-affinity, wedge detection and "
            "telemetry, so it is deliberately NOT an env knob: the "
            "shipped compose fixes the topology. LLM choice belongs to "
            "the gateway (LLM_BACKENDS), never to a client of it."
        ),
    }),
    "RETRIEVER_URL": MappingProxyType({
        "default": "http://localhost:8888/v1/embeddings",
        "kind": "not-a-knob",
        "consumers": ("consolidation_loop.py (RETRIEVER_URL)",),
        "note": "Same not-a-knob reasoning as REASONER_URL, for the embedding call.",
    }),

    # ── client-side exclusion — documented, never read by a client ─────────

    "COORDINATOR_URL": MappingProxyType({
        "default": None,
        "kind": "client-side",
        "consumers": (),
        "note": (
            "The gateway URL a CLIENT (memory_bridge.py, vector-skill.py) "
            "calls. Clients ship alone (ADR-014 thin-client split) and "
            "NEVER import this table — this row documents the exclusion, "
            "it names no value any server-side code reads. default is None as an "
            "exclusion marker: clients default to http://localhost:8888 themselves "
            "(memory_bridge.py, mcp/vector-skill.py), so the field is not the "
            "client default."
        ),
    }),
})
