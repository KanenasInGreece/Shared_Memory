"""FRAMEWORK_DEFAULTS (W1, D1) — ONE authority for the framework's literal
built-in defaults, and the two edited consumer sites (coordinator.py's
EMBED_URL/RERANK_URL, hive_mind_proxy.py's EMBEDDER_URL/RERANKER_URL/
DEFAULT_TARGET/LLM_BACKENDS) that now read it instead of re-typing the
value.

⛔ Does NOT DRY tests/test_coordinator_encoder_urls.py:31-32/:44/:71-72 — those
stay byte-identical, independent pins of the exact same literals; this file
is deliberately a SECOND, separate set of assertions, not a refactor of the
first (an independent pin that references the table it is meant to check
against becomes a tautology).
"""
import ast
import importlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import framework_defaults  # noqa: E402


# ── The dependency-free / no-env-access invariant ───────────────────────────

def test_module_imports_only_mappingproxytype():
    """The module docstring's own NEW INVARIANT: the only import is
    ``from types import MappingProxyType`` — no os, no re, nothing that
    could itself raise or read the environment. Parsed from source (not
    sys.modules) so this catches an import added anywhere in the file, not
    only at the top."""
    src = inspect.getsource(framework_defaults)
    tree = ast.parse(src)
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(f"{node.module}.{','.join(a.name for a in node.names)}")
    assert imports == ["types.MappingProxyType"], (
        f"framework_defaults.py must import ONLY MappingProxyType from types, found: {imports}")


def test_module_never_reads_os_environ():
    """Zero os.environ access — the module docstring's own reasoning: a
    reload of coordinator/hive_mind_proxy does not reload this THIRD module
    (sys.modules caching), so any env-derived value here would go stale
    across every test that reloads a daemon under a different env. Checked
    structurally (AST attribute-access nodes), not a substring search of the
    raw source — the docstring above legitimately DISCUSSES os.environ in
    prose without the module ever touching it in code."""
    tree = ast.parse(inspect.getsource(framework_defaults))
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "environ"]
    assert not hits, "framework_defaults.py must never access os.environ (or any *.environ attribute)"
    hits2 = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in ("os", "getenv")]
    assert not hits2, "framework_defaults.py must never reference os/getenv as a name in code"


def test_table_and_every_row_are_frozen_mappingproxytype():
    assert type(framework_defaults.FRAMEWORK_DEFAULTS) is type(__import__("types").MappingProxyType({}))
    for name, row in framework_defaults.FRAMEWORK_DEFAULTS.items():
        assert type(row) is type(__import__("types").MappingProxyType({})), (
            f"{name}'s row must be a MappingProxyType too")


def test_table_cannot_be_mutated_through_the_exported_name():
    with pytest.raises(TypeError):
        framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"] = {}
    with pytest.raises(TypeError):
        framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"] = "x"


# ── Value pins — the literal, on one side, never table == table ────────────

def test_embedder_url_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"] == "http://localhost:8070"


def test_reranker_url_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"] == "http://localhost:8071"


def test_llm_default_target_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["default"] == "http://localhost:5000"


def test_llm_backends_default_value_is_empty_string():
    assert framework_defaults.FRAMEWORK_DEFAULTS["LLM_BACKENDS"]["default"] == ""


def test_neo4j_uri_documented_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["NEO4J_URI"]["default"] == "bolt://localhost:7687"
    assert framework_defaults.FRAMEWORK_DEFAULTS["NEO4J_URI"]["kind"] == "hardcoded-literal"


def test_pg_dsn_host_port_documented_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["PG_DSN_HOST_PORT"]["default"] == "localhost:5432"


def test_proxy_bind_documented_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["PROXY_BIND"]["default"] == "127.0.0.1"


def test_proxy_bind_carries_an_idiom_despite_being_documented_only():
    """Fold round item 4 (PR #347, QA Q1): PROXY_BIND's idiom now lives here
    — the ONE authority — even though its 'kind' stays documented-only (no
    W1 code change at that site); check_config.py used to duplicate this as
    a hand-written special case, deleted in the fold round.

    SEC H (R-3, RULED 2026-09-02): idiom flipped "get" -> "or" — measured
    on glxvm, TCPSite(runner, "", port) binds ALL interfaces, so a
    present-but-empty PROXY_BIND must fall back to the default exactly
    like an absent one, matching the gateway's actual (fixed) behaviour."""
    row = framework_defaults.FRAMEWORK_DEFAULTS["PROXY_BIND"]
    assert row["kind"] == "documented-only"
    assert row["idiom"] == "or"


def test_port_documented_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["PORT"]["default"] == 8888


def test_reasoner_url_not_a_knob_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["REASONER_URL"]["default"] == "http://localhost:8888/v1/chat/completions"
    assert framework_defaults.FRAMEWORK_DEFAULTS["REASONER_URL"]["kind"] == "not-a-knob"


def test_retriever_url_not_a_knob_default_value():
    assert framework_defaults.FRAMEWORK_DEFAULTS["RETRIEVER_URL"]["default"] == "http://localhost:8888/v1/embeddings"
    assert framework_defaults.FRAMEWORK_DEFAULTS["RETRIEVER_URL"]["kind"] == "not-a-knob"


def test_coordinator_url_is_a_client_side_exclusion_row():
    row = framework_defaults.FRAMEWORK_DEFAULTS["COORDINATOR_URL"]
    assert row["kind"] == "client-side"
    assert row["default"] is None


# ── Idiom field pins (env-default rows only) ────────────────────────────────

def test_embedder_and_reranker_use_the_or_idiom():
    assert framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["idiom"] == "or"
    assert framework_defaults.FRAMEWORK_DEFAULTS["RERANKER_URL"]["idiom"] == "or"


def test_llm_default_target_and_llm_backends_use_the_get_idiom():
    assert framework_defaults.FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["idiom"] == "get"
    assert framework_defaults.FRAMEWORK_DEFAULTS["LLM_BACKENDS"]["idiom"] == "get"


# ── Per-edited-consumer: effective default == table value, env unset ───────
# (reload precedent: tests/test_coordinator_encoder_urls.py's own _reloaded())

def _reload_coordinator(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("coordinator")
    return importlib.reload(mod)


def _reload_proxy(monkeypatch, **env):
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_DEFAULT_TARGET", "LLM_BACKENDS", "LLM_BACKENDS_JSON"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("hive_mind_proxy")
    return importlib.reload(mod)


@pytest.fixture(autouse=True)
def _restore_daemon_modules(monkeypatch):
    """Same discipline as test_coordinator_encoder_urls.py's own
    _restore_coordinator_module fixture: reloading coordinator/
    hive_mind_proxy rebinds the module object process-wide, so restore a
    clean-env reload after every test in this file too — belt-and-braces
    delenv (monkeypatch's own teardown already reverts these, since it runs
    before this fixture's post-yield code, LIFO) plus the reload itself."""
    yield
    for k in ("EMBEDDER_URL", "RERANKER_URL", "LLM_DEFAULT_TARGET", "LLM_BACKENDS", "LLM_BACKENDS_JSON"):
        monkeypatch.delenv(k, raising=False)
    importlib.reload(importlib.import_module("coordinator"))
    importlib.reload(importlib.import_module("hive_mind_proxy"))


def test_coordinator_embed_rerank_defaults_match_the_table(monkeypatch):
    mod = _reload_coordinator(monkeypatch)
    assert mod.EMBED_URL == framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"] + "/v1/embeddings"
    assert mod.RERANK_URL == framework_defaults.FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"] + "/v1/reranking"


def test_proxy_embedder_reranker_defaults_match_the_table(monkeypatch):
    mod = _reload_proxy(monkeypatch)
    assert mod.EMBEDDER_URL == framework_defaults.FRAMEWORK_DEFAULTS["EMBEDDER_URL"]["default"]
    assert mod.RERANKER_URL == framework_defaults.FRAMEWORK_DEFAULTS["RERANKER_URL"]["default"]


def test_proxy_default_target_matches_the_table_when_llm_default_target_unset(monkeypatch):
    mod = _reload_proxy(monkeypatch)
    assert mod.DEFAULT_TARGET == framework_defaults.FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["default"]


def test_proxy_llm_backends_falls_back_to_default_target_when_llm_backends_unset(monkeypatch):
    mod = _reload_proxy(monkeypatch)
    assert mod.LLM_BACKENDS == [framework_defaults.FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["default"]]


# ── Empty-string idiom pins — one per NEWLY-edited site ─────────────────────
# (EMBEDDER_URL/RERANKER_URL's empty-value behaviour is already pinned,
# byte-identical, by test_coordinator_encoder_urls.py:44/:71-72 — not
# duplicated here.)

def test_llm_default_target_empty_value_is_honoured_as_empty_not_the_default(monkeypatch):
    """'get' idiom: LLM_DEFAULT_TARGET= (present but EMPTY) resolves to the
    empty string, NOT the table default — pinned as today's exact, if
    latent, behaviour (see framework_defaults.py's own row note); W1 never
    normalises idiom on an existing site."""
    mod = _reload_proxy(monkeypatch, LLM_DEFAULT_TARGET="")
    assert mod.DEFAULT_TARGET == ""


def test_llm_backends_empty_value_produces_an_empty_pool_list(monkeypatch):
    """'get' idiom + the empty-string split/filter: LLM_BACKENDS= (present
    but EMPTY) parses to no entries, so the empty-fallback branch fires and
    the pool becomes [DEFAULT_TARGET] — same shape as LLM_BACKENDS unset."""
    mod = _reload_proxy(monkeypatch, LLM_BACKENDS="")
    assert mod.LLM_BACKENDS == [framework_defaults.FRAMEWORK_DEFAULTS["LLM_DEFAULT_TARGET"]["default"]]


def test_known_latent_both_llm_default_target_and_llm_backends_empty_yields_backends_of_empty_string(monkeypatch):
    """The composed KNOWN LATENT documented in framework_defaults.py's
    LLM_DEFAULT_TARGET row: LLM_DEFAULT_TARGET= (present-but-empty, 'get'
    idiom keeps it "") AND LLM_BACKENDS unset/empty (falls back to
    [DEFAULT_TARGET] verbatim) composes into LLM_BACKENDS == [""] — a
    latent the code does not fix today; this pins it AS-IS, per the brief,
    rather than silently changing behaviour."""
    mod = _reload_proxy(monkeypatch, LLM_DEFAULT_TARGET="")
    assert mod.DEFAULT_TARGET == ""
    assert mod.LLM_BACKENDS == [""]


# ── Binding-time guard — reads stay MODULE-LEVEL, never a runtime lookup ───
# A rewrite to a function/property call at USE time (rather than binding a
# plain module-level constant once at import/reload) would change the
# monkeypatch+reload semantics every test in this family (and
# test_coordinator_encoder_urls.py) depends on — see that file's own
# _restore_coordinator_module fixture docstring for why the pattern matters.

def _assigned_at_module_level(source: str, name: str) -> bool:
    """True if `name = ...` (or as part of a tuple-unpack target list)
    appears as a top-level (zero-indentation) statement in `source`."""
    tree = ast.parse(source)
    for node in tree.body:  # tree.body is ALREADY module-top-level only
        if isinstance(node, ast.Assign):
            targets = node.targets
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return True
                if isinstance(t, ast.Tuple):
                    if any(isinstance(e, ast.Name) and e.id == name for e in t.elts):
                        return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            return True
    return False


def test_coordinator_encoder_reads_are_module_level_not_a_runtime_lookup():
    import coordinator
    src = inspect.getsource(coordinator)
    assert _assigned_at_module_level(src, "EMBED_URL")
    assert _assigned_at_module_level(src, "RERANK_URL")


def test_proxy_encoder_and_default_target_reads_are_module_level():
    import hive_mind_proxy
    src = inspect.getsource(hive_mind_proxy)
    assert _assigned_at_module_level(src, "EMBEDDER_URL")
    assert _assigned_at_module_level(src, "RERANKER_URL")
    assert _assigned_at_module_level(src, "DEFAULT_TARGET")


def test_proxy_llm_backends_is_bound_once_at_module_level_via_tuple_unpack():
    """LLM_BACKENDS is produced by ONE call to _load_llm_backends(), unpacked
    into a module-level tuple assignment — not re-derived per request."""
    import hive_mind_proxy
    src = inspect.getsource(hive_mind_proxy)
    assert _assigned_at_module_level(src, "LLM_BACKENDS")
