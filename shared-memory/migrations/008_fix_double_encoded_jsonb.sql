-- 008_fix_double_encoded_jsonb.sql
-- Repair JSONB columns that were double-encoded as string scalars.
--
-- Root cause: coordinator.py called json.dumps() on metadata / cypher_params
-- before binding them to a $N::jsonb parameter, but the asyncpg pool already
-- registers a jsonb codec (encoder=json.dumps, see _init_connection). The value
-- was therefore serialised twice and stored as a JSON *string scalar*
-- (jsonb_typeof = 'string') instead of an object — so metadata->>'key' returned
-- NULL and any SQL audit of the column silently found nothing, while the read
-- path still worked because the codec decoder unwrapped one layer. Rows saved via
-- the psycopg2 MCP path (no codec) were stored correctly, which is why the
-- corruption was partial. The double json.dumps() is removed in code (v0.4.2);
-- this migration normalises the historical rows.
--
-- `jsonb #>> '{}'` extracts the root as text (unquoting a string scalar to its
-- inner JSON), and ::jsonb re-parses that into a proper object. The regex guard
-- only touches string scalars whose content is object/array-shaped, leaving any
-- genuine JSON string untouched. Idempotent: once a column is an object it no
-- longer matches jsonb_typeof = 'string'.

UPDATE technical_docs
SET metadata = (metadata #>> '{}')::jsonb
WHERE jsonb_typeof(metadata) = 'string'
  AND (metadata #>> '{}') ~ '^\s*[\{\[]';

UPDATE neo4j_outbox
SET cypher_params = (cypher_params #>> '{}')::jsonb
WHERE jsonb_typeof(cypher_params) = 'string'
  AND (cypher_params #>> '{}') ~ '^\s*[\{\[]';
