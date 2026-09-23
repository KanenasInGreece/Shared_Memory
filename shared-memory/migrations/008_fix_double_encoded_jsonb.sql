-- Migration 008: a second json.dumps against the pool's jsonb codec stored
-- metadata and cypher_params as string scalars, so ->> returned NULL.
-- #>> '{}' re-parses only object- or array-shaped scalars; a real JSON string
-- stays, and once the value is an object the predicate no longer matches.

UPDATE technical_docs
SET metadata = (metadata #>> '{}')::jsonb
WHERE jsonb_typeof(metadata) = 'string'
  AND (metadata #>> '{}') ~ '^\s*[\{\[]';

UPDATE neo4j_outbox
SET cypher_params = (cypher_params #>> '{}')::jsonb
WHERE jsonb_typeof(cypher_params) = 'string'
  AND (cypher_params #>> '{}') ~ '^\s*[\{\[]';
