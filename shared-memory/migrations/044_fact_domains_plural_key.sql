-- Migration 044: a fact's sections live under the plural key `domains` only.
-- The search filter reads `domains`; a fact saved with the singular `domain` was filed in the graph
-- but not returned by `search --domain`. The gateway now stores `domains` on every record that names a
-- section; this repairs the records saved before that, on technical_docs only (never community_summaries).
-- Facts only: a decision or retrospective, and any record whose `decision` blob names a section, is left alone.
--   1. singular only (or a null plural): the singular value moves to `domains`.
--   2. both keys present and every singular value already in `domains`: the singular copy is dropped.
--   3. the notice counts what still carries `domain`: keys that disagree, or a singular that names nothing.
--      Those rows are never changed here; the operator judges them.
-- No BEGIN/COMMIT: apply.py writes this file and its ledger row in one transaction.

DO $mig$
DECLARE n1 bigint; n2 bigint; n3 bigint;
BEGIN
UPDATE technical_docs t
   SET metadata = (t.metadata - 'domain') || jsonb_build_object('domains',
       (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domain') WHEN 'array' THEN t.metadata->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g))
 WHERE jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\s+|\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND t.metadata->'decision' ?| array['domain', 'domains'], false)
   AND t.metadata ? 'domain'
   AND (NOT t.metadata ? 'domains' OR jsonb_typeof(t.metadata->'domains') = 'null')
   AND (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domain') WHEN 'array' THEN t.metadata->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL;
GET DIAGNOSTICS n1 = ROW_COUNT;

UPDATE technical_docs t SET metadata = t.metadata - 'domain'
 WHERE jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\s+|\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND t.metadata->'decision' ?| array['domain', 'domains'], false)
   AND t.metadata ? 'domain'
   AND jsonb_typeof(t.metadata->'domains') IN ('array', 'string')
   AND NOT EXISTS (
     SELECT 1 FROM jsonb_array_elements(coalesce((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domain') WHEN 'array' THEN t.metadata->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g), '[]'::jsonb)) s(e)
      WHERE NOT coalesce((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domains') WHEN 'array' THEN t.metadata->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g), '[]'::jsonb) @> jsonb_build_array(s.e));
GET DIAGNOSTICS n2 = ROW_COUNT;

SELECT count(*) INTO n3 FROM technical_docs t
 WHERE jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\s+|\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND t.metadata->'decision' ?| array['domain', 'domains'], false)
   AND t.metadata ? 'domain';

RAISE NOTICE '044: % moved, % agreeing copies dropped, % left for the operator', n1, n2, n3;
END $mig$;
