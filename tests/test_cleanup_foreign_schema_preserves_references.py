"""Tests that cleanup_foreign_schema --apply preserves REFERENCES relationships.

Phase C deletes foreign relationship types, and Phase A DETACH DELETEs all-foreign
nodes. REFERENCES edges and nodes incident to them must be preserved until
ledgered by backfill_references.py.
"""
from collections import Counter
import os
import re
import sys
import pytest

_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import cleanup_foreign_schema
from cleanup_foreign_schema import apply, main

# Anchored query shapes supported by the fake session
_SURVEY_A_RE = re.compile(
    r"^MATCH \(n\) WHERE size\(labels\(n\)\) > 0 "
    r"AND none\(l IN labels\(n\) WHERE l IN \$kl\) "
    r"AND (?:(NOT) )?\(n\)\s*(<?)-\[:([A-Za-z_][A-Za-z0-9_]*(?:\|[A-Za-z_][A-Za-z0-9_]*)*)\]-(>?)\s*\(\) "
    r"RETURN labels\(n\) AS labels, coalesce\(n\.name, '<' \+ toString\(n\.pg_id\) \+ '>'\) AS name$"
)

_SURVEY_B_RE = re.compile(
    r"^MATCH \(n\) WHERE any\(l IN labels\(n\) WHERE l IN \$kl\) "
    r"AND any\(l IN labels\(n\) WHERE NOT l IN \$kl\) "
    r"RETURN labels\(n\) AS labels, coalesce\(n\.name, '<' \+ toString\(n\.pg_id\) \+ '>'\) AS name$"
)

_SURVEY_C_RE = re.compile(
    r"^MATCH \(a\)\s*(<?)-\[r\]-(>?)\s*\(b\) "
    r"WHERE (?:(NOT) )?type\(r\) IN \$kr "
    r"AND all\(l IN labels\(a\) WHERE l IN \$kl\) "
    r"AND all\(l IN labels\(b\) WHERE l IN \$kl\) "
    r"RETURN type\(r\) AS rel, count\(\*\) AS c ORDER BY c DESC$"
)

_APPLY_A_RE = re.compile(
    r"^MATCH \(n\) WHERE size\(labels\(n\)\) > 0 "
    r"AND none\(l IN labels\(n\) WHERE l IN \$kl\) "
    r"AND (?:(NOT) )?\(n\)\s*(<?)-\[:([A-Za-z_][A-Za-z0-9_]*(?:\|[A-Za-z_][A-Za-z0-9_]*)*)\]-(>?)\s*\(\) "
    r"DETACH DELETE n RETURN count\(\*\) AS n$"
)

_APPLY_B_RE = re.compile(
    r"^MATCH \(n:`([A-Za-z_][A-Za-z0-9_]*)`\) "
    r"WHERE any\(l IN labels\(n\) WHERE l IN \$kl\) "
    r"REMOVE n:`\1` RETURN count\(\*\) AS n$"
)

_APPLY_C_RE = re.compile(
    r"^MATCH \(\)\s*(<?)-\[r\]-(>?)\s*\(\) "
    r"WHERE (?:(NOT) )?type\(r\) IN \$types "
    r"DELETE r RETURN count\(\*\) AS n$"
)


class Node:
    def __init__(self, node_id: str, labels: list[str] | set[str], name: str = ""):
        self.id = node_id
        self.labels = set(labels)
        self.name = name or node_id


class Edge:
    def __init__(self, start: str, end: str, rel_type: str):
        self.start = start
        self.end = end
        self.type = rel_type


class InMemoryGraph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []

    def add_node(self, node_id: str, labels: list[str] | set[str], name: str = "") -> Node:
        node = Node(node_id, labels, name)
        self.nodes[node_id] = node
        return node

    def add_edge(self, start: str, end: str, rel_type: str) -> Edge:
        edge = Edge(start, end, rel_type)
        self.edges.append(edge)
        return edge

    def incident_edges(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.start == node_id or e.end == node_id]


def _node_matches_rel_pattern(
    graph: InMemoryGraph,
    node_id: str,
    left_arrow: str,
    types: list[str],
    right_arrow: str,
) -> bool:
    types_set = set(types)
    for e in graph.edges:
        if e.type not in types_set:
            continue
        if left_arrow == "<" and right_arrow == ">":
            if e.start == node_id or e.end == node_id:
                return True
        elif left_arrow == "<":
            if e.end == node_id:
                return True
        elif right_arrow == ">":
            if e.start == node_id:
                return True
        else:
            if e.start == node_id or e.end == node_id:
                return True
    return False


class FakeResult:
    def __init__(self, data: list[dict]):
        self._data = data

    def data(self) -> list[dict]:
        return self._data

    def single(self) -> dict:
        return self._data[0] if self._data else {"n": 0}


class FakeSession:
    def __init__(self, graph: InMemoryGraph):
        self.graph = graph
        self.recorded_queries: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def run(self, cypher: str, **params) -> FakeResult:
        self.recorded_queries.append((cypher, params))
        q = " ".join(cypher.split())
        kl = set(params.get("kl", []))
        kr = set(params.get("kr", []))

        # apply A: detach delete junk nodes
        m = _APPLY_A_RE.match(q)
        if m:
            has_not = bool(m.group(1))
            left_arr = m.group(2) or ""
            rel_types = m.group(3).split("|")
            right_arr = m.group(4) or ""

            deleted = []
            for n_id, n in list(self.graph.nodes.items()):
                if len(n.labels) > 0 and all(l not in kl for l in n.labels):
                    matched = _node_matches_rel_pattern(
                        self.graph, n_id, left_arr, rel_types, right_arr
                    )
                    cond = (not matched) if has_not else matched
                    if cond:
                        deleted.append(n_id)
            for n_id in deleted:
                del self.graph.nodes[n_id]
            self.graph.edges = [
                e for e in self.graph.edges
                if e.start not in deleted and e.end not in deleted
            ]
            return FakeResult([{"n": len(deleted)}])

        # apply B: remove foreign labels from mixed nodes
        m = _APPLY_B_RE.match(q)
        if m:
            lbl = m.group(1)
            modified = 0
            for n in self.graph.nodes.values():
                if lbl in n.labels and any(l in kl for l in n.labels):
                    n.labels.remove(lbl)
                    modified += 1
            return FakeResult([{"n": modified}])

        # apply C: delete foreign relationships
        m = _APPLY_C_RE.match(q)
        if m:
            has_not = bool(m.group(3))
            types = set(params.get("types", []))
            remaining = []
            deleted_count = 0
            for e in self.graph.edges:
                cond = (e.type not in types) if has_not else (e.type in types)
                if cond:
                    deleted_count += 1
                else:
                    remaining.append(e)
            self.graph.edges = remaining
            return FakeResult([{"n": deleted_count}])

        # survey A: junk nodes (delete candidates vs skipped)
        m = _SURVEY_A_RE.match(q)
        if m:
            has_not = bool(m.group(1))
            left_arr = m.group(2) or ""
            rel_types = m.group(3).split("|")
            right_arr = m.group(4) or ""

            rows = []
            for n in self.graph.nodes.values():
                if len(n.labels) > 0 and all(l not in kl for l in n.labels):
                    matched = _node_matches_rel_pattern(
                        self.graph, n.id, left_arr, rel_types, right_arr
                    )
                    cond = (not matched) if has_not else matched
                    if cond:
                        rows.append({"labels": list(n.labels), "name": n.name})
            return FakeResult(rows)

        # survey B: mixed nodes
        m = _SURVEY_B_RE.match(q)
        if m:
            rows = []
            for n in self.graph.nodes.values():
                if any(l in kl for l in n.labels) and any(l not in kl for l in n.labels):
                    rows.append({"labels": list(n.labels), "name": n.name})
            return FakeResult(rows)

        # survey C: foreign rels between valid nodes
        m = _SURVEY_C_RE.match(q)
        if m:
            has_not = bool(m.group(3))
            counts = Counter()
            for e in self.graph.edges:
                cond = (e.type not in kr) if has_not else (e.type in kr)
                if cond:
                    a = self.graph.nodes.get(e.start)
                    b = self.graph.nodes.get(e.end)
                    if a and b and len(a.labels) > 0 and len(b.labels) > 0:
                        if all(l in kl for l in a.labels) and all(l in kl for l in b.labels):
                            counts[e.type] += 1
            rows = [{"rel": rel, "c": c} for rel, c in counts.most_common()]
            return FakeResult(rows)

        raise AssertionError(f"unrecognised query: {cypher}")


class FakeDriver:
    def __init__(self, graph: InMemoryGraph):
        self.graph = graph

    def session(self):
        return FakeSession(self.graph)

    def close(self):
        pass


def _setup_stubs(monkeypatch, graph: InMemoryGraph, argv: list[str]) -> FakeDriver:
    driver = FakeDriver(graph)
    monkeypatch.setattr(cleanup_foreign_schema.GraphDatabase, "driver", lambda *args, **kwargs: driver)
    monkeypatch.setattr(cleanup_foreign_schema.secure_env, "load_split_env", lambda: None)
    monkeypatch.setattr(
        cleanup_foreign_schema.secure_env,
        "get_secret",
        lambda k, default="": "fake_password" if k == "NEO4J_PASSWORD" else default,
    )
    monkeypatch.setattr(cleanup_foreign_schema, "KNOWN_LABELS", frozenset({"Fact", "Decision"}))
    monkeypatch.setattr(cleanup_foreign_schema, "KNOWN_RELATIONSHIPS", frozenset({"INFORMED_BY"}))
    monkeypatch.setattr(sys, "argv", argv)
    return driver


def test_case_1_phase_c_preserves_references_by_type(monkeypatch, capsys):
    # Failure caught: cleanup --apply deletes REFERENCES by type (phase C)
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "REFERENCES")
    graph.add_edge("a", "b", "FOREIGN_X")

    _setup_stubs(monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply"])

    ret = main()
    assert ret == 0

    rel_types = [e.type for e in graph.edges]
    assert "REFERENCES" in rel_types
    assert "FOREIGN_X" not in rel_types

    out = capsys.readouterr().out
    assert "PRESERVED (always): [('REFERENCES', 1)]" in out
    assert "PRESERVED (--preserve-rel)" not in out
    assert "backfill_references.py" in out


def test_case_2_phase_a_preserves_references_through_node(monkeypatch, capsys):
    # Failure caught: cleanup --apply deletes REFERENCES through a node (phase A)
    graph = InMemoryGraph()
    graph.add_node("a", ["Junk"], name="node_a")
    graph.add_node("b", ["Fact"], name="node_b")
    graph.add_node("c", ["Junk"], name="node_c")
    graph.add_edge("a", "b", "REFERENCES")

    _setup_stubs(monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply"])

    ret = main()
    assert ret == 0

    assert "a" in graph.nodes
    assert "b" in graph.nodes
    assert "c" not in graph.nodes

    rel_types = [e.type for e in graph.edges]
    assert "REFERENCES" in rel_types

    out = capsys.readouterr().out
    assert "SKIPPED (incident to REFERENCES): 1" in out
    assert "backfill_references.py" in out


def test_case_2_preserves_incoming_references_through_foreign_node(monkeypatch, capsys):
    # Failure caught: cleanup --apply deletes REFERENCES through a node (phase A), incoming edge
    graph = InMemoryGraph()
    graph.add_node("a", ["Junk"], name="node_a")
    graph.add_node("b", ["Fact"], name="node_b")
    graph.add_node("c", ["Junk"], name="node_c")
    graph.add_edge("b", "a", "REFERENCES")

    _setup_stubs(monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply"])

    ret = main()
    assert ret == 0

    assert "a" in graph.nodes
    assert "b" in graph.nodes
    assert "c" not in graph.nodes
    assert [e.type for e in graph.edges] == ["REFERENCES"]

    out = capsys.readouterr().out
    assert "SKIPPED (incident to REFERENCES): 1" in out
    assert "backfill_references.py" in out


def test_case_3_preserve_rel_argument_still_honored(monkeypatch, capsys):
    # Failure caught: cleanup --apply deletes user-preserved relationship type (--preserve-rel)
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "REFERENCES")
    graph.add_edge("a", "b", "FOREIGN_X")

    _setup_stubs(
        monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply", "--preserve-rel", "FOREIGN_X"]
    )

    ret = main()
    assert ret == 0

    rel_types = [e.type for e in graph.edges]
    assert "REFERENCES" in rel_types
    assert "FOREIGN_X" in rel_types

    out = capsys.readouterr().out
    assert "PRESERVED (always): [('REFERENCES', 1)]" in out
    assert "PRESERVED (--preserve-rel): [('FOREIGN_X', 1)]" in out
    assert "backfill_references.py" in out


def test_no_references_present_does_not_mention_backfill(monkeypatch, capsys):
    # Failure caught: cleanup falsely announces backfill_references.py when no REFERENCES exist
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "FOREIGN_X")

    _setup_stubs(monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply"])

    ret = main()
    assert ret == 0

    assert len(graph.edges) == 0
    out = capsys.readouterr().out
    assert "PRESERVED (always)" not in out
    assert "PRESERVED (--preserve-rel)" not in out
    assert "backfill_references.py" not in out


def test_apply_directly_preserves_references_even_if_passed_in_rel_types(monkeypatch):
    # Failure caught: cleanup apply() deletes REFERENCES by type (phase C)
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "REFERENCES")
    graph.add_edge("a", "b", "FOREIGN_X")

    monkeypatch.setattr(cleanup_foreign_schema, "KNOWN_LABELS", frozenset({"Fact", "Decision"}))
    session = FakeSession(graph)
    res = apply(session, [], ["REFERENCES", "FOREIGN_X"])

    rel_types = [e.type for e in graph.edges]
    assert "REFERENCES" in rel_types
    assert "FOREIGN_X" not in rel_types
    assert res["rels_deleted"] == 1


def test_survey_c_preserves_references_when_ontology_still_has_references(monkeypatch, capsys):
    # Failure caught: cleanup ignores REFERENCES when still present in KNOWN_RELATIONSHIPS
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "REFERENCES")

    _setup_stubs(monkeypatch, graph, ["cleanup_foreign_schema.py", "--apply"])
    monkeypatch.setattr(
        cleanup_foreign_schema,
        "KNOWN_RELATIONSHIPS",
        frozenset({"INFORMED_BY", "REFERENCES"}),
    )

    ret = main()
    assert ret == 0

    assert [e.type for e in graph.edges] == ["REFERENCES"]

    out = capsys.readouterr().out
    assert "PRESERVED (always): [('REFERENCES', 1)]" in out
    assert "backfill_references.py" in out


def test_fake_session_raises_on_unrecognised_query():
    # Failure caught: fake session silently absorbs unrecognised cypher queries
    graph = InMemoryGraph()
    session = FakeSession(graph)
    with pytest.raises(AssertionError, match="unrecognised query"):
        session.run("MATCH (x) RETURN x")


def test_fake_session_raises_on_malformed_query_with_keyword():
    # Failure caught: fake session loosely matches keywords instead of complete query shape
    graph = InMemoryGraph()
    session = FakeSession(graph)
    malformed_queries = [
        "invalid Cypher DELETE r",
        "MATCH ()-[r]->() DELETE r",
        "MATCH (n) DETACH DELETE n",
        "MATCH (n:`Foo`) REMOVE n:`Foo`",
        "MATCH (n) WHERE none(l IN labels(n) WHERE l IN $kl) RETURN labels(n) AS labels",
        "MATCH (a)-[r]->(b) RETURN type(r) AS rel",
    ]
    for query in malformed_queries:
        with pytest.raises(AssertionError, match="unrecognised query"):
            session.run(query, types=["FOREIGN_X"], kl=["Fact"], kr=["INFORMED_BY"])


def test_phase_c_query_with_negation_inverts_deletion():
    # Failure caught: fake session ignores NOT predicate in phase C relationship deletion
    graph = InMemoryGraph()
    graph.add_node("a", ["Fact"], name="node_a")
    graph.add_node("b", ["Decision"], name="node_b")
    graph.add_edge("a", "b", "REFERENCES")
    graph.add_edge("a", "b", "FOREIGN_X")

    session = FakeSession(graph)
    res = session.run(
        "MATCH ()-[r]->() WHERE NOT type(r) IN $types DELETE r RETURN count(*) AS n",
        types=["FOREIGN_X"],
    )

    assert res.single()["n"] == 1
    rel_types = [e.type for e in graph.edges]
    # Inverted predicate deletes REFERENCES (not in types) and keeps FOREIGN_X (in types)
    assert "REFERENCES" not in rel_types
    assert "FOREIGN_X" in rel_types


def test_fake_session_raises_on_malformed_relationship_type_list():
    # Failure caught: fake session accepts malformed relationship type list (leading, trailing, or doubled separator)
    graph = InMemoryGraph()
    session = FakeSession(graph)
    malformed_queries = [
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:|REFERENCES]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:REFERENCES|]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:REFERENCES||X]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:|REFERENCES]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:REFERENCES|]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:REFERENCES||X]-() DETACH DELETE n RETURN count(*) AS n",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:|REFERENCES]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:REFERENCES|]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND (n)-[:REFERENCES||X]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:|REFERENCES]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:REFERENCES|]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
        "MATCH (n) WHERE size(labels(n)) > 0 AND none(l IN labels(n) WHERE l IN $kl) AND NOT (n)-[:REFERENCES||X]-() RETURN labels(n) AS labels, coalesce(n.name, '<' + toString(n.pg_id) + '>') AS name",
    ]
    for query in malformed_queries:
        with pytest.raises(AssertionError, match="unrecognised query"):
            session.run(query, kl=["Fact"])


