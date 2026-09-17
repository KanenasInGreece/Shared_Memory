-- Migration 041: whitelist neo4j_outbox cypher_params->>'type'.
--
-- Unknown outbox types used to fall through to Fact MERGE and SET f.content
-- from a missing content_snippet (blanking the node). The worker now
-- fail-closes those rows; this CHECK is the schema half so a direct INSERT
-- cannot enqueue one. NULL / missing / empty type is the historical Fact
-- default and stays allowed.
--
-- Do not UNIQUE neo4j_outbox.pg_id — a pg_id may have a dream-cycle row and a
-- later one-shot (project_of / domain_of / supersede) at once.
--
-- If this ADD CONSTRAINT fails, inspect rows whose type is not in the
-- whitelist; this migration does not rewrite or DELETE them.

BEGIN;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM neo4j_outbox
     WHERE cypher_params->>'type' IS NOT NULL
       AND cypher_params->>'type' <> ''
       AND cypher_params->>'type' NOT IN (
         'fact', 'decision', 'retrospective', 'supersede', 'project_of', 'domain_of'
       )
  ) THEN
    RAISE EXCEPTION
      'migration 041: neo4j_outbox has rows with unknown cypher_params.type; fail or delete them before adding neo4j_outbox_type_known';
  END IF;
END $$;

ALTER TABLE neo4j_outbox
    ADD CONSTRAINT neo4j_outbox_type_known
    CHECK (
      (cypher_params->>'type') IS NULL
      OR (cypher_params->>'type') = ''
      OR (cypher_params->>'type') IN (
        'fact', 'decision', 'retrospective', 'supersede', 'project_of', 'domain_of'
      )
    );

COMMIT;
