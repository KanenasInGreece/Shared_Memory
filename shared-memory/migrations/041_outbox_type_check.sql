-- Migration 041: reject an unknown neo4j_outbox type. Those rows used to fall
-- through to Fact MERGE and blank content from a missing snippet. NULL or
-- empty type stays the historical Fact default. Do not unique pg_id: a
-- dream-cycle row and a later one-shot may coexist. A failing check names
-- the bad type; this file does not rewrite or delete those rows.

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
