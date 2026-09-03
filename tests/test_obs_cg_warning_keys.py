"""OBS CG — declare the warning-key vocabulary; fix the stale annotations.

THE DEFECT
----------
`warnings[].key` (`telemetry_contract.py`) was a free `str` — a renamed or
added warning key was invisible to every guard. Independently, FIVE existing
`log="health.warning.*"` annotations elsewhere in the same file named keys NO
producer ever emits (verified against `hive_mind_proxy.py`'s health-build
function, the only place `_warning(...)` is ever called):

  - `encoders.embed.p95_ms`   → `health.warning.encoder_p95_ms`   (no such
    producer — the real key is `encoder_embedder_projected_ms`)
  - `encoders.rerank.p95_ms`  → `health.warning.encoder_p95_ms`   (same wrong
    string, on the OTHER encoder — real key: `encoder_reranker_projected_ms`)
  - `gateway.shed_503_total`  → `health.warning.pool_shedding`    (real key:
    `gateway_shed_503_total`)
  - `outbox.oldest_pending_age_s` → `health.warning.outbox_age`   (real key:
    `outbox_oldest_pending_age_s`)
  - `credentials.token_verify_failed` → `health.warning.token_verify_failed`
    (missing `_per_min` — the real key is `token_verify_failed_per_min`)

(Five occurrences, four distinct wrong strings — more than the brief's
"three", since `encoder_p95_ms` is wrong on two different fields. Verified
exhaustively here rather than stopping at three; noted in HANDOFF as a
discrepancy from the brief's own count.)

THE FIX
-------
`telemetry_contract.WARNING_KEYS` — the enumerated, re-derived set of the six
real `_warning(...)` first-argument literals. All five stale `log=`
annotations renamed to the string their OWN field's producer actually emits.
`WARNING_KEYS` is rendered into the generated doc.

THIS FILE
---------
An AST/source walk of `hive_mind_proxy.py`, scoped to `_warning(...)` call
sites (never a flatten-the-whole-file string scan, which would also catch
unrelated string literals). Two directions, both required:

  1. Every literal first-arg `_warning(...)` can produce is a member of
     `WARNING_KEYS` (nothing emitted is undocumented).
  2. Every member of `WARNING_KEYS` has a producer (nothing documented is
     dead — the reverse of D5's registry.error lesson: a set with no
     structural link to its producers drifts the same way a duplicate dict
     key does).

The encoder pair's `_warning(f"encoder_{name}_projected_ms", ...)` is a
`JoinedStr`, not a plain string constant — this file's walker resolves it by
finding the nearest enclosing `for name in (...)` loop over a literal tuple
of strings and substituting each value, rather than special-casing the two
resulting strings by hand (which would silently stop tracking the real code
the moment the loop's tuple changed).

PROVE-FAILING-FIRST (recorded in the commit body): this file's own
`test_ast_walker_actually_resolves_the_encoder_pair` proves the walker
resolves the f-string at all (a walker that gave up on `JoinedStr` nodes
would silently report ZERO encoder keys and both directional tests would
pass vacuously — checked explicitly). The stale-annotation fix was proved by
running this file BEFORE the `log=` renames and confirming
`test_no_log_annotation_names_a_key_without_a_producer` failed, listing all
five (see commit body for the captured output).
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import telemetry_contract as tc  # noqa: E402
import hive_mind_proxy as g  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════
# The AST walker
# ═══════════════════════════════════════════════════════════════════════════

def _joinedstr_template(jstr: ast.JoinedStr):
    """[('lit', text) | ('var', name), ...] for a JoinedStr built ONLY from
    plain string pieces and single-Name FormattedValues (`f"a{x}b"`, never
    `f"a{x.y}b"` or `f"a{x!r}b"`) — anything else returns None, so an
    unsupported shape is a loud "no producer resolved", never a silent
    wrong answer."""
    parts = []
    for v in jstr.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(("lit", v.value))
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) \
                and v.format_spec is None and v.conversion == -1:
            parts.append(("var", v.value.id))
        else:
            return None
    return parts


def _literal_str_tuple_or_list(node) -> "list[str] | None":
    if isinstance(node, (ast.Tuple, ast.List)) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


def _warning_producer_keys(source: str) -> set[str]:
    """Every string `_warning(...)`'s first argument can actually take, by
    walking `hive_mind_proxy.py`'s SOURCE (never its already-executed
    module state, which cannot tell a literal from a value that happened to
    equal it at import time).

    Scoped to `_warning(` call sites ONLY — `ast.walk`ing the whole module
    for string constants would pick up unrelated text (log messages, other
    dict keys) that has nothing to do with this vocabulary."""
    tree = ast.parse(source)
    keys: set[str] = set()

    class _Visitor(ast.NodeVisitor):
        def __init__(self):
            self.for_stack: list[ast.For] = []

        def visit_For(self, node: ast.For):
            self.for_stack.append(node)
            self.generic_visit(node)
            self.for_stack.pop()

        def visit_Call(self, node: ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "_warning" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    keys.add(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    template = _joinedstr_template(arg)
                    if template is not None:
                        var_names = {p[1] for p in template if p[0] == "var"}
                        if len(var_names) == 1:
                            var_name = next(iter(var_names))
                            for for_node in reversed(self.for_stack):
                                if isinstance(for_node.target, ast.Name) \
                                        and for_node.target.id == var_name:
                                    values = _literal_str_tuple_or_list(for_node.iter)
                                    if values is not None:
                                        for val in values:
                                            keys.add("".join(
                                                val if p[0] == "var" else p[1]
                                                for p in template))
                                    break
            self.generic_visit(node)

    _Visitor().visit(tree)
    return keys


def _hive_mind_proxy_source() -> str:
    return inspect.getsource(g)


# ═══════════════════════════════════════════════════════════════════════════
# The walker itself actually works (guards against a silently-vacuous pin)
# ═══════════════════════════════════════════════════════════════════════════

def test_ast_walker_actually_resolves_the_encoder_pair():
    keys = _warning_producer_keys(_hive_mind_proxy_source())
    assert "encoder_embedder_projected_ms" in keys
    assert "encoder_reranker_projected_ms" in keys
    # And it did NOT invent a literal "encoder_{name}_projected_ms" or an
    # unresolved f-string artifact.
    assert not any("{" in k or "}" in k for k in keys)


def test_ast_walker_finds_all_six_producers():
    keys = _warning_producer_keys(_hive_mind_proxy_source())
    assert keys == {
        "encoder_embedder_projected_ms",
        "encoder_reranker_projected_ms",
        "outbox_oldest_pending_age_s",
        "rem_dead_lettered",
        "gateway_shed_503_total",
        "token_verify_failed_per_min",
    }


# ═══════════════════════════════════════════════════════════════════════════
# The two-directional pin: WARNING_KEYS ⊆ producers, producers ⊆ WARNING_KEYS
# ═══════════════════════════════════════════════════════════════════════════

def test_every_warning_key_has_a_producer():
    produced = _warning_producer_keys(_hive_mind_proxy_source())
    undocumented_or_dead = tc.WARNING_KEYS - produced
    assert not undocumented_or_dead, (
        f"WARNING_KEYS entries with no _warning(...) producer: {undocumented_or_dead}")


def test_every_producer_is_a_declared_warning_key():
    produced = _warning_producer_keys(_hive_mind_proxy_source())
    unregistered = produced - tc.WARNING_KEYS
    assert not unregistered, (
        f"_warning(...) call sites producing an undeclared key: {unregistered}")


# ═══════════════════════════════════════════════════════════════════════════
# The stale log= annotations are gone; every log="health.warning.X" names a
# real, declared warning key (the `<key>` template on warnings[].key itself
# is exempt — it is documentation syntax, not a literal key value).
# ═══════════════════════════════════════════════════════════════════════════

def _all_log_annotations() -> list[tuple[str, str]]:
    """(path, log) for every entry in HEALTH/TELEMETRY carrying a `log=`
    that starts with the warning-key namespace."""
    out = []
    for contract in (tc.HEALTH, tc.TELEMETRY):
        for path, spec in contract.items():
            log = spec.get("log")
            if log and log.startswith("health.warning."):
                out.append((path, log))
    return out


def test_no_log_annotation_names_a_key_without_a_producer():
    bad = []
    for path, log in _all_log_annotations():
        key = log[len("health.warning."):]
        if key == "<key>":
            continue  # the template on warnings[].key itself
        if key not in tc.WARNING_KEYS:
            bad.append((path, log))
    assert not bad, f"log= annotations naming a non-existent warning key: {bad}"


def test_the_five_previously_stale_annotations_are_now_correct():
    """Direct pin on the exact fields Opus flagged (plus the one Opus's
    count missed) — the four stale STRINGS Opus found map to correct
    per-field values now, not just "some" real key."""
    assert tc.HEALTH.get("encoders.embed.p95_ms") is None  # lives in TELEMETRY only
    assert tc.TELEMETRY["encoders.embed.p95_ms"]["log"] == \
        "health.warning.encoder_embedder_projected_ms"
    assert tc.TELEMETRY["encoders.rerank.p95_ms"]["log"] == \
        "health.warning.encoder_reranker_projected_ms"
    assert tc.TELEMETRY["gateway.shed_503_total"]["log"] == \
        "health.warning.gateway_shed_503_total"
    assert tc.TELEMETRY["outbox.oldest_pending_age_s"]["log"] == \
        "health.warning.outbox_oldest_pending_age_s"
    assert tc.TELEMETRY["credentials.token_verify_failed"]["log"] == \
        "health.warning.token_verify_failed_per_min"


# ═══════════════════════════════════════════════════════════════════════════
# No live-condition payload requirement (a healthy payload has empty
# warnings[] — a test requiring a live warning condition would skip forever
# green on a healthy install).
# ═══════════════════════════════════════════════════════════════════════════

def test_warning_keys_pin_does_not_require_a_live_warning_condition():
    """This whole file never builds a /health payload or drives a live
    warning condition — every assertion above is pure AST/dict inspection.
    This test exists only to document that property explicitly, per the
    brief's explicit prohibition."""
    assert tc.WARNING_KEYS  # the set itself is non-empty and always available


# ═══════════════════════════════════════════════════════════════════════════
# WARNING_KEYS renders into the generated doc
# ═══════════════════════════════════════════════════════════════════════════

def test_warning_keys_rendered_into_generated_doc():
    doc = tc.render_markdown()
    assert "## Warning keys" in doc
    for key in tc.WARNING_KEYS:
        assert f"`{key}`" in doc
