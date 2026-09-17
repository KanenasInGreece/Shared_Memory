"""Built-in default literals only: no os.environ, frozen MappingProxyType, so check_config Phase A can import this without booting the gateway.

Do not add env reads here — daemon tests reload coordinator/hive_mind_proxy and would otherwise see a stale first import of this module.
"""
from types import MappingProxyType

FRAMEWORK_DEFAULTS = MappingProxyType({

    # ── env-default rows — code changed in W1 to read this table ───────────

    "EMBEDDER_URL": MappingProxyType({
        "default": "http://localhost:8070",
        "kind": "env-default",
        "idiom": "or",
        "consumers": (
            "hive_mind_proxy.py:134 (ROUTING_MAP base)",
            "coordinator.py:2187 (_encoder_url base)",
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
            "hive_mind_proxy.py:135 (ROUTING_MAP base)",
            "coordinator.py:2188 (_encoder_url base)",
        ),
        "note": "Same one-setting-moves-both-consumers shape as EMBEDDER_URL.",
    }),
    "LLM_DEFAULT_TARGET": MappingProxyType({
        "default": "http://localhost:5000",
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("hive_mind_proxy.py:176 (DEFAULT_TARGET)",),
        "note": (
            "KNOWN LATENT (documented, not fixed by W1 — a later wave's "
            "ruling): this site uses the .get(name, default) idiom, so "
            "LLM_DEFAULT_TARGET= (present but EMPTY) resolves to the empty "
            "string, not this default — and because LLM_BACKENDS' own "
            "empty-fallback (below) wraps DEFAULT_TARGET verbatim, that "
            "empty string then becomes LLM_BACKENDS == ['']. Pinned "
            "as-is by an empty-string test; normalising the idiom here "
            "would silently change what an empty LLM_DEFAULT_TARGET does "
            "today."
        ),
    }),
    "LLM_BACKENDS": MappingProxyType({
        "default": "",
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("hive_mind_proxy.py:693-731 (_load_llm_backends fallback)",),
        "note": (
            "An absent or empty LLM_BACKENDS falls back to a single-entry "
            "pool: [(LLM_DEFAULT_TARGET, 1.0)]. See the LLM_DEFAULT_TARGET "
            "row's note for the known latent this composes with when "
            "LLM_DEFAULT_TARGET is ALSO present-but-empty."
        ),
    }),
    "EMBED_MAX_CONTEXT_TOKENS": MappingProxyType({
        "default": 8192,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py:88",),
        "note": "Maximum context window (tokens) the embedder/reranker supports.",
    }),
    "EMBED_CHARS_PER_TOKEN": MappingProxyType({
        "default": 3.0,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py:94",),
        "note": "Conservative characters-per-token ratio for text clamping.",
    }),
    "EMBED_SPECIAL_TOKEN_RESERVE": MappingProxyType({
        "default": 2,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py:108",),
        "note": "Special tokens reserved for BOS/EOS/framing.",
    }),
    "EMBED_MAX_CHARS": MappingProxyType({
        "default": 24570,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("dream_telemetry.py:114",),
        "note": "Maximum character length sent to embedder before vector clamping.",
    }),
    "OVERFLOW_TOKEN_SLACK": MappingProxyType({
        "default": 16,
        "kind": "env-default",
        "idiom": "get",
        "consumers": ("encoder_window.py:30",),
        "note": "Token slack allowed above context window during overflow snap loop.",
    }),


    # ── hardcoded-literal rows — documented only, no code change in W1 ─────

    "NEO4J_URI": MappingProxyType({
        "default": "bolt://localhost:7687",
        "kind": "hardcoded-literal",
        "consumers": (
            "coordinator.py:2130",
            "rem_loop.py:89",
            "consolidation_loop.py:115",
        ),
        "note": (
            "Duplicated verbatim across three files, not wired to an env "
            "var anywhere. Making it an env-overridable knob is a "
            "deliberate ruling for a LATER wave, not W1 — recorded here so "
            "the duplication is visible and no wave 'fixes' it silently."
        ),
    }),

    # ── documented-only rows — one real call site each, no code change ─────

    "PG_DSN_HOST_PORT": MappingProxyType({
        "default": "localhost:5432",
        "kind": "documented-only",
        "consumers": ("coordinator.py:2127-2129 (PG_DSN constructed default)",),
        "note": (
            "The host:port segment of the constructed default Postgres DSN "
            "(postgresql://postgres:<pw>@localhost:5432/agent_data). "
            "Secret-adjacent (the DSN carries the password) — W1 makes no "
            "code change here; PG_CONN is the real override knob."
        ),
    }),
    "PROXY_BIND": MappingProxyType({
        "default": "127.0.0.1",
        "kind": "documented-only",
        "idiom": "or",
        "consumers": ("hive_mind_proxy.py:5709 (_resolve_proxy_bind_host)",),
        "note": (
            "Set PROXY_BIND=0.0.0.0 to opt into all-interfaces binding. "
            "Carries an 'idiom' despite 'kind' being documented-only (no W1 "
            "code change at this site) — see the module docstring's 'idiom' "
            "field entry for why: check_config.py needs it to render an "
            "honest present-but-empty verdict, and a second, hand-written "
            "idiom table in that script would be exactly the duplicate "
            "authority this module exists to prevent (fold-round item 4). "
            "SEC H (R-3, RULED — Xenofon 2026-09-02, measured on glxvm): "
            "flipped 'get' -> 'or'. TCPSite(runner, '', port) binds "
            "ALL interfaces (0.0.0.0 + [::]), never loopback — so a "
            "present-but-empty PROXY_BIND ('PROXY_BIND=' in the env file) "
            "must fall back to the default exactly like an absent one, or "
            "check_config would tell the operator empty means "
            "all-interfaces while the gateway actually binds loopback. A "
            "deliberate, recorded reversal of the W1 no-normalise "
            "position, for this one site."
        ),
    }),
    "PORT": MappingProxyType({
        "default": 8888,
        "kind": "documented-only",
        "consumers": ("hive_mind_proxy.py:5209",),
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
        "consumers": ("rem_loop.py:109", "consolidation_loop.py:135"),
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
        "consumers": ("consolidation_loop.py:130",),
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
            "it names no value any server-side code reads."
        ),
    }),
})
