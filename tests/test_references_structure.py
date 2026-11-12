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


def references_occurrences(root):
    found = []
    roots = [root / 'shared-memory', root / 'mcp', root / 'shared-memory-skill']
    for directory in roots:
        for path in directory.rglob('*.py'):
            tree = ast.parse(path.read_text())
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.JoinedStr):
                    value = ''.join(part.value if isinstance(part, ast.Constant) else '{}'
                                    for part in node.values)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if isinstance(parents.get(node), ast.JoinedStr):
                        continue
                    value = node.value
                else:
                    continue
                if not re.search(r'\bREFERENCES\b', value):
                    continue
                context = type(parents.get(node)).__name__
                if value == 'REFERENCES':
                    parent = parents[node]
                    if isinstance(parent, ast.Tuple):
                        parent = parents[parent]
                    context = ast.unparse(parent)
                found.append((str(path.relative_to(root)), context, value))
    return sorted(found)


def test_shipped_references_occurrences():
    # Pin exact text deliberately: editors must re-pin reviewed occurrences, never widen exemptions.
    # Exact text and context also cover APOC's literal relationship-type argument.
    allowed = [('shared-memory/migrations/generate_schema_init.py',
      'Expr',
      'Table-level CHECK constraints, rendered as they were declared.\n'
      '\n'
      '    Without this the generator silently DROPPED them: the fresh-install fast path\n'
      '    rebuilt every table from columns and indexes alone, so a constraint that a\n'
      '    migration had added existed on upgraded deployments and on NO new one. That\n'
      '    is the worst shape a schema divergence can take — the guarantee holds\n'
      '    everywhere it was tested and nowhere it was not.\n'
      '\n'
      '    NOT NULL and PRIMARY KEY are excluded because render_column already emits\n'
      '    them. FOREIGN KEYS are excluded HERE but not from the file — see\n'
      '    fetch_foreign_keys, which emits them after every table exists. Inline was\n'
      '    never an option: tables are rendered in name order, and `project_promotions`\n'
      '    sorts before the `projects` it references, so an inline REFERENCES would\n'
      '    break the very install path this file serves.\n'
      '    '),
     ('shared-memory/scripts/backfill_references.py',
      'Assign',
      'MATCH (s)-[r]->(t)\n'
      "WHERE type(r) IN ['REFERENCES', 'INFORMED_BY']\n"
      'RETURN elementId(r) AS eid, type(r) AS type, labels(s) AS source_labels,\n'
      'labels(t) AS target_labels, s.pg_id AS source, t.pg_id AS target,\n'
      'properties(r) AS properties,\n'
      "[k IN keys(r) WHERE split(valueType(r[k]), ' NOT NULL')[0] IN\n"
      " ['ZONED DATETIME', 'LOCAL DATETIME', 'DATE', 'ZONED TIME', 'LOCAL TIME', 'DURATION'] |\n"
      ' {k:k, t:valueType(r[k]), v:toString(r[k])}] AS temporal'),
     ('shared-memory/scripts/backfill_references.py', "edge['type'] == 'REFERENCES'", 'REFERENCES'),
     ('shared-memory/scripts/cleanup_foreign_schema.py',
      'Expr',
      'One-time cleanup of foreign schema drift (Phase 2 compliance).\n'
      '\n'
      'The inbound gates (coordinator + REM) now keep new writes within the ontology,\n'
      'but pre-gate experiments left non-ontology labels and relationship types in the\n'
      'graph — surfaced by the `compliance` section of /memory/telemetry. This removes\n'
      'them, using the SAME vocabulary the telemetry checks against\n'
      '(ontology.KNOWN_LABELS / KNOWN_RELATIONSHIPS), so the cleanup can never disagree\n'
      'with what compliance reports.\n'
      '\n'
      'Three categories (all shown in dry-run; --apply executes):\n'
      '  A. Junk nodes      — every label is foreign  → DETACH DELETE (removes the node\n'
      '                       and all its edges; this clears most foreign rels, whose\n'
      '                       endpoints are these nodes). A node incident to a\n'
      '                       REFERENCES edge is kept.\n'
      '  B. Spurious labels — node has a valid label AND a foreign one (e.g.\n'
      '                       :Entity:Object) → REMOVE the foreign label, keep the node.\n'
      '  C. Foreign rels    — type is foreign and BOTH endpoints survive A → DELETE the\n'
      '                       relationship (REFERENCES is always preserved). Use\n'
      '                       --preserve-rel TYPE (repeatable) to keep a type pending\n'
      '                       a decision (e.g. a legacy-but-meaningful relationship\n'
      '                       between legitimate nodes).\n'
      '\n'
      'Dry-run is the DEFAULT. Nothing changes unless you pass --apply.\n'
      '\n'
      'Usage (on the gateway host):\n'
      '    uv run --with neo4j python shared-memory/scripts/cleanup_foreign_schema.py\n'
      '    uv run --with neo4j python shared-memory/scripts/cleanup_foreign_schema.py         --preserve-rel '
      'HAS_STEP --apply\n'),
     ('shared-memory/scripts/cleanup_foreign_schema.py', "PRESERVED_ALWAYS = ('REFERENCES',)", 'REFERENCES'),
     ('shared-memory/scripts/coordinator.py',
      'Call',
      'UNWIND $pg_ids AS pg_id CALL (pg_id) {   MATCH (n {pg_id: pg_id}) WHERE {}   OPTIONAL MATCH '
      "(n)-[r]-(related)     WHERE r IS NULL OR type(r) <> 'REFERENCES'   OPTIONAL MATCH "
      '(related)-[:{}]-(al:{})     WHERE EXISTS {       MATCH (related)<-[:{}]-(m)       WHERE m.pg_id IS NOT '
      'NULL AND coalesce(m.superseded,false) = false     }   WITH n, r, related, labels(related) AS '
      'labels,        collect(DISTINCT al.name) AS aliases   ORDER BY CASE WHEN type(r) IN '
      "['{}','{}',                                  '{}','{}'] THEN 0                 WHEN r.asserted_by IS NOT "
      "NULL THEN 1 ELSE 2 END,            CASE WHEN type(r) = '{}' THEN 1 ELSE 0 END   LIMIT $cap   RETURN "
      'labels, related.name AS name, related.pg_id AS rel_pg_id,          type(r) AS rel_type,          '
      'properties(r) AS rel_props,          related.rem_summary AS rem_summary,          related.rem_processed '
      'AS rem_processed,          related.rem_prompt_version AS rem_prompt_version,          startNode(r).pg_id '
      'AS start_pg_id,          labels(startNode(r)) AS start_labels,          startNode(r).name AS '
      'start_name,          endNode(r).pg_id AS end_pg_id,          labels(endNode(r)) AS end_labels,          '
      'endNode(r).name AS end_name,          aliases } RETURN pg_id AS anchor_pg_id, labels, name, rel_pg_id, '
      'rel_type,        rel_props, rem_summary, rem_processed, rem_prompt_version,        start_pg_id, '
      'start_labels, start_name,        end_pg_id, end_labels, end_name,        aliases'),
     ('shared-memory/scripts/coordinator.py',
      'Expr',
      "One Neo4j round-trip for every anchor. Rendering is the caller's hydration pass.\n"
      '\n'
      '        The per-anchor cap stays inside the correlated subquery. A query error\n'
      '        returns an empty list per anchor: graph context enriches a search and\n'
      '        must never fail one. REFERENCES stays excluded for installations awaiting\n'
      '        the backfill (decision:3482: citations live only in Postgres). Legacy\n'
      '        edges still appear in lineage and graph until that backfill runs.\n'
      '        ')]
    assert references_occurrences(SCRIPTS.parents[1]) == sorted(allowed)


def test_keyword_search_excludes_only_object_reference_key():
    # Section 5/r3: stored lists select hits; non-object metadata makes fallback raise.
    source = (SCRIPTS / 'coordinator.py').read_text()
    assert "CASE WHEN jsonb_typeof(metadata) = 'object' THEN (metadata - 'references')::text ELSE metadata::text END" in source
