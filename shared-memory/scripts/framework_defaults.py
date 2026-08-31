"""FRAMEWORK_DEFAULTS — the one table of the framework's built-in defaults.

W1 (Local_Documentation plan, anchor v0.9.77): before this module, the same
literal default lived in more than one file with no cross-check between
them — e.g. EMBEDDER_URL's "http://localhost:8070" was typed once in
coordinator.py and once in hive_mind_proxy.py, and nothing would notice the
day the two drifted apart. This module is the single place that literal is
now written; the two consumer sites read it instead of re-typing it. That is
the ENTIRE scope of this module — see check_config.py (a separate,
standalone script) for rendering what the running gateway will actually do
with these values, and the D1 section of the W1 plan for why the two are
split.

⛔ NEW INVARIANT — read this before adding anything to this module:

    This module holds IMMUTABLE LITERALS ONLY. Its only import is
    ``from types import MappingProxyType``. It performs ZERO os.environ
    access, and the table it exports is a MappingProxyType (frozen).

Why that invariant is load-bearing, not just tidy:

  1. coordinator.py's own module-level ``_encoder_url()`` call RAISES
     ValueError at import time on a malformed EMBEDDER_URL/RERANKER_URL —
     deliberate (pinned by tests/test_coordinator_encoder_urls.py:119/:127)
     — and hive_mind_proxy.py:60 imports coordinator before anything else in
     the gateway boots. A standalone script that wants to render the
     defaults table WITHOUT risking that raise (check_config.py's Phase A,
     which must run stdlib-only and crash-proof) needs a module it can
     import that can never itself raise or block on a live daemon's
     startup checks. This module is that: reachable even when the daemons
     are not.

  2. ``importlib.reload()`` of coordinator.py or hive_mind_proxy.py is a
     working pattern roughly twenty test files rely on (see
     tests/test_coordinator_encoder_urls.py's ``_reloaded()`` helper) to
     re-evaluate module-level env-derived constants under a different
     monkeypatched environment. Python's ``sys.modules`` caching means a
     reload of coordinator/hive_mind_proxy does NOT reload a THIRD module
     they import (this one) — a second, unrelated ``import
     framework_defaults`` elsewhere in the same process reuses the exact
     object already in ``sys.modules``. If this module read os.environ at
     import time, every test that reloads a daemon to see a new env value
     would keep seeing this module's FIRST-EVER-imported values instead —
     a stale copy that is silently indistinguishable from a fresh one,
     because nothing about a plain dict access reveals its own staleness.
     With NO env read at all, "stale" and "fresh" are the same value by
     construction, which is what makes the invariant sufficient rather
     than merely convenient.

Precedent for a small, dependency-free shared module in this scripts/
directory: log_hygiene.py, agent_roles.py, secure_env.py.

Table shape. Every row is keyed by the framework's own name for the setting
(the env var name where one exists) and maps to a frozen dict of:

  default    the literal default value, or None for a row that documents a
             site with no single literal default (an argv positional, or a
             deliberately-absent knob).
  kind       one of:
               "env-default"        — os.environ-overridable; ONE OR MORE
                                       consumer sites in this wave were
                                       switched to read this table instead
                                       of re-typing the literal.
               "hardcoded-literal"  — a real literal, duplicated across more
                                       than one file today, but NOT wired to
                                       an env var and NOT changed by W1
                                       (becoming a knob needs an operator
                                       ruling — recorded here as a
                                       documented row only).
               "documented-only"    — a real default, single call site,
                                       recorded here for visibility; W1
                                       changes no code for it.
               "not-a-knob"         — deliberately NOT configurable; the
                                       row exists so a later wave does not
                                       "fix" this by adding an env knob
                                       without first re-reading why it
                                       isn't one.
               "client-side"        — belongs to the CLIENT side of the
                                       thin-client split (ADR-014); clients
                                       never import this table, so this row
                                       is a documented exclusion, not a
                                       value any server-side code reads.
  idiom      present on every row with a genuine os.environ-reading
             consumer site (every "env-default" row, PLUS PROXY_BIND —
             a "documented-only" row with no W1 CODE change, but check_
             config.py still needs its idiom to render an honest verdict,
             so it lives here rather than as a second, hand-written
             authority in that script — fold-round item 4, PR #347):
             which idiom the *consumer site* uses to fall back to the
             default —
               "or"   — ``os.environ.get(NAME) or default`` — an EMPTY env
                        value (``NAME=`` in .env) is treated the SAME as an
                        unset one and falls back to the default.
               "get"  — ``os.environ.get(NAME, default)`` — an EMPTY env
                        value is honoured AS EMPTY; the default only
                        applies when the key is absent entirely.
             W1 never normalises which idiom a site uses (that would be an
             invisible behaviour change on an empty-string value) — this
             field is DOCUMENTATION of today's behaviour, pinned by an
             empty-string test per edited site. See the row's own "note"
             for the known latent this creates.
  consumers  a tuple of "file.py:line (context)" strings, approximate
             (marked with the file/line the code lived at when this row was
             written) — informational only, never asserted against by a
             test; code moves, this table is not re-synced line-for-line
             on every unrelated edit.
  note       free text: why a "hardcoded-literal"/"not-a-knob" row is what
             it is, or a caveat about the row's own behaviour.
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
        "consumers": ("hive_mind_proxy.py:502-504 (_load_llm_backends fallback)",),
        "note": (
            "An absent or empty LLM_BACKENDS falls back to a single-entry "
            "pool: [(LLM_DEFAULT_TARGET, 1.0)]. See the LLM_DEFAULT_TARGET "
            "row's note for the known latent this composes with when "
            "LLM_DEFAULT_TARGET is ALSO present-but-empty."
        ),
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
        "idiom": "get",
        "consumers": ("hive_mind_proxy.py:5253 (bind_host)",),
        "note": (
            "Set PROXY_BIND=0.0.0.0 to opt into all-interfaces binding. "
            "Carries an 'idiom' despite 'kind' being documented-only (no W1 "
            "code change at this site) — see the module docstring's 'idiom' "
            "field entry for why: check_config.py needs it to render an "
            "honest present-but-empty verdict, and a second, hand-written "
            "idiom table in that script would be exactly the duplicate "
            "authority this module exists to prevent (fold-round item 4)."
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
