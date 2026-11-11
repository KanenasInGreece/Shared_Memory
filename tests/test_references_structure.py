"""Structural guards for release-3 graph and lexical failures."""
import ast
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'shared-memory/scripts'
sys.path.insert(0, str(SCRIPTS))
import ontology


def test_spine_relationships_exact():
    # Section 5/r3: a new shipped citation writer or REFERENCES ontology entry.
    expected_spine = {
        'MENTIONS', 'REPORTS_ON', 'ALIASES', 'SUMMARIZED_BY', 'NEXT_STEP',
        'WAS_ATTRIBUTED_TO', 'WAS_ASSISTED_BY', 'WAS_GENERATED_BY', 'PROJECT_OF',
        'DOMAIN_OF', 'ACTED_ON_BEHALF_OF', 'SUPERSEDES', 'INFORMED_BY', 'HAD_OUTCOME',
        'PRODUCES_INSIGHT', 'UNDER_CONDITIONS', 'CONSIDERED', 'REJECTED', 'GROUNDED_IN'}
    assert ontology.SPINE_RELATIONSHIPS == expected_spine
    assert ontology.KNOWN_RELATIONSHIPS == expected_spine | {
        'DEPENDS_ON', 'PART_OF', 'IMPLEMENTS', 'PRODUCES', 'CONSUMES',
        'RUNS_ON', 'CONFIGURES', 'DESCRIBES', 'VALIDATES'}


def test_shipped_references_occurrences():
    # Inspect every string shape, including docstrings and f-string fragments.
    # Exact contexts prevent a new writer hiding in an otherwise allowed file.
    allowed = {
        ('shared-memory/scripts/backfill_references.py', "edge['type'] == 'REFERENCES'"),
        ('shared-memory/scripts/backfill_references.py', """MATCH (s)-[r]->(t)
WHERE type(r) IN ['REFERENCES', 'INFORMED_BY']
RETURN elementId(r) AS eid, type(r) AS type, labels(s) AS source_labels,
labels(t) AS target_labels, s.pg_id AS source, t.pg_id AS target,
properties(r) AS properties"""),
    }
    roots = [SCRIPTS.parent, SCRIPTS.parents[1] / 'mcp',
             SCRIPTS.parents[1] / 'shared-memory-skill']
    found = set()
    pattern = re.compile(r"\[[^\]]*\bREFERENCES\b|:\s*REFERENCES\s*\]|type\(r\).*REFERENCES", re.S)
    for root in roots:
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    value = ''.join(part.value if isinstance(part, ast.Constant) else '{}'
                                    for part in node.values)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value
                else:
                    continue
                if path == SCRIPTS / 'coordinator.py':
                    value = value.replace("WHERE r IS NULL OR type(r) <> 'REFERENCES'", '')
                if not (pattern.search(value) or value == 'REFERENCES'):
                    continue
                if value == 'REFERENCES' and isinstance(parents.get(node), ast.Compare):
                    value = ast.unparse(parents[node])
                found.add((str(path.relative_to(SCRIPTS.parents[1])), value))
    assert found == allowed


def test_keyword_search_excludes_only_object_reference_key():
    # Section 5/r3: stored lists select hits; non-object metadata makes fallback raise.
    source = (SCRIPTS / 'coordinator.py').read_text()
    assert "CASE WHEN jsonb_typeof(metadata) = 'object' THEN (metadata - 'references')::text ELSE metadata::text END" in source
