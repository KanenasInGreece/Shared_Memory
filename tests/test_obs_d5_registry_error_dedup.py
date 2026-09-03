"""OBS D5 — the duplicate `"registry.error"` key, and a guard that can see one.

THE DEFECT
----------
`telemetry_contract.py`'s `TELEMETRY` dict literal declared `"registry.error"`
TWICE: once (originally around line 853) documenting the CENSUS producer
(`_registry_telemetry()` in `coordinator.py`, which sets `out["error"]` while
the last background census attempt failed), and again (originally around line
881) documenting the SECTION-QUERY producer (the `try/except` wrapping the
CALL to `_registry_telemetry()`, which replaces the whole `registry` section
with `{"error": str(exc)}` when that call itself raises). A Python dict
literal silently keeps only the LAST of two duplicate keys — the first entry's
documentation was dead on arrival, shadowed with no error, no warning, and no
existing test noticing (the suite only ever iterates the resulting `dict`,
which has already lost the duplicate by the time any test sees it).

THE FIX
-------
One merged entry naming BOTH producers and stating when each fires; the
duplicate declaration is deleted.

THE GUARD
---------
A key colliding INSIDE one dict literal is invisible to any check that walks
the resulting `dict` object (`TELEMETRY.items()`) — by the time Python has
finished constructing it, the collision is already resolved. The only way to
see it is to inspect the SOURCE: an AST walk of `telemetry_contract.py`,
scoped to the two `ast.Dict` nodes that are the actual RIGHT-HAND SIDE values
of the `HEALTH: dict[str, dict] = {...}` and `TELEMETRY: dict[str, dict] =
{...}` module-level annotated assignments — and ONLY those two nodes.

⚠ Scoping matters: `_k(...)` itself is a function whose OWN call sites repeat
field names like `since=`/`note=`/`log=` constantly across the file (they are
keyword arguments, not dict keys, but `ast.walk` does not know the
difference structurally the naive way) — flattening the whole file into one
`ast.walk` and looking for repeated string `ast.Constant` nodes would
false-positive on every one of those. This guard walks `dict_node.keys`
(top-level literal keys of the Dict node only, not `ast.walk(dict_node)`),
so it sees only the actual dict keys `HEALTH`/`TELEMETRY` declare, never a
`_k(...)` call's keyword names or nested string constants inside a `note=`
value.

PROVE-FAILING-FIRST (recorded in the commit body): the guard test was run
against the file WITH the duplicate still present (before deleting the second
`"registry.error"` entry) and failed, reporting `{"registry.error"}` as the
duplicate set — matching Opus's repo-wide finding that this is the ONLY
duplicate in either dict. After the fix, it passes.
"""
import ast
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))

import telemetry_contract as tc  # noqa: E402


def _dict_literal_for(tree: ast.Module, name: str) -> ast.Dict:
    """The `ast.Dict` node that is the RHS of `NAME: dict[...] = {...}` (an
    `AnnAssign`) or `NAME = {...}` (a plain `Assign`) at module level — never
    a nested dict reached by flattening the whole tree."""
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == name \
                    and isinstance(node.value, ast.Dict):
                return node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name \
                        and isinstance(node.value, ast.Dict):
                    return node.value
    raise AssertionError(f"no dict-literal assignment named {name!r} found")


def _duplicate_string_keys(dict_node: ast.Dict) -> set:
    """String keys appearing more than once among `dict_node.keys` — the
    TOP-LEVEL keys of this one literal only (`dict_node.keys`, never
    `ast.walk(dict_node)`, which would descend into every `_k(...)` call's
    keyword arguments and any nested string constant in a `note=`)."""
    seen: set = set()
    dupes: set = set()
    for key_node in dict_node.keys:
        if key_node is None:
            continue  # a `**other_dict` unpacking inside the literal
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            if key_node.value in seen:
                dupes.add(key_node.value)
            seen.add(key_node.value)
    return dupes


def _module_ast() -> ast.Module:
    source = inspect.getsource(tc)
    return ast.parse(source)


def test_no_duplicate_keys_in_health_dict_literal():
    tree = _module_ast()
    node = _dict_literal_for(tree, "HEALTH")
    assert _duplicate_string_keys(node) == set()


def test_no_duplicate_keys_in_telemetry_dict_literal():
    tree = _module_ast()
    node = _dict_literal_for(tree, "TELEMETRY")
    assert _duplicate_string_keys(node) == set()


def test_guard_scoping_ignores_k_helper_keyword_repeats():
    """`_k(...)` is called hundreds of times across both dicts, each call
    repeating keyword names (`since=`, `note=`, `log=`, `unit=`) that are NOT
    dict keys. If the guard mistakenly walked the whole subtree instead of
    just `dict_node.keys`, it would report those as "duplicate keys" and fail
    on every healthy file. This proves the scoping: the real `TELEMETRY`
    literal has dozens of `_k(...)` calls sharing `since=` and passes clean."""
    tree = _module_ast()
    node = _dict_literal_for(tree, "TELEMETRY")
    k_calls = sum(
        1 for value_node in node.values
        if isinstance(value_node, ast.Call)
        and any(kw.arg == "since" for kw in value_node.keywords)
    )
    assert k_calls > 50  # sanity: this file really does repeat `since=` a lot
    assert _duplicate_string_keys(node) == set()


def test_registry_error_documents_both_producers_once():
    """The merged entry is the SUITE's only declaration of `registry.error`
    — an AST scan of the actual dict-literal keys (QA finding 4: the prior
    `[k for k in tc.TELEMETRY if k == "registry.error"] == ["registry.error"]`
    shape is a tautology a Python dict cannot violate — it was ALREADY
    deduplicated by the time this test ever saw it, so it survived QA's
    planted-duplicate mutation while the real AST guards above died. This
    reuses `_duplicate_string_keys`, the same instrument those guards use,
    and asserts the VALUE `"registry.error" not in dupes` rather than an
    equality no input could ever falsify) and its note names both producers
    by their real phrase, not via an `or` chain that any sufficiently long
    note satisfies."""
    tree = _module_ast()
    node = _dict_literal_for(tree, "TELEMETRY")
    dupes = _duplicate_string_keys(node)
    assert "registry.error" not in dupes, dupes
    note = tc.TELEMETRY["registry.error"].get("note", "")
    assert "census" in note.lower()
    assert "section-query" in note.lower()
