-- Migration 044: a fact's sections live under the plural key `domains` only.
-- The search filter reads `domains`; a fact saved with the singular `domain` was filed in the graph
-- but not returned by `search --domain`. The gateway now stores `domains` on every record that names a
-- section; this repairs the records saved before that, on technical_docs only (never community_summaries).
-- Only records that are not a decision or retrospective, and whose `decision` blob (if any) resolves to no
-- section, are looked at. A name is a string, trimmed; blanks and non-strings are not names.
--   1. top-level `domain` names at least one section and `domains` is absent, null, or an array or string
--      that names nothing: `domains` becomes the singular's names (an array) and `domain` is removed.
--   2. both keys present and every singular name is already in `domains`, or the singular names nothing:
--      `domain` is removed. A string-valued `domains` that names something is also rewritten as an array
--      of its names, because the search filter matches arrays only.
--   3. the notice counts what still has a top-level `domain`: rows whose keys disagree, and rows whose
--      `domains` is an object, number or boolean. Those are never changed here; the operator judges them.
-- A row matches at most one step. No BEGIN/COMMIT: apply.py writes this file and its ledger row in one
-- transaction.

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
                    AND ((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domain') WHEN 'array' THEN t.metadata->'decision'->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
                         OR (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domains') WHEN 'array' THEN t.metadata->'decision'->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL), false)
   AND t.metadata ? 'domain'
   AND (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domain') WHEN 'array' THEN t.metadata->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
   AND (NOT t.metadata ? 'domains'
        OR jsonb_typeof(t.metadata->'domains') = 'null'
        OR (jsonb_typeof(t.metadata->'domains') IN ('array', 'string')
            AND (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domains') WHEN 'array' THEN t.metadata->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NULL));
GET DIAGNOSTICS n1 = ROW_COUNT;

UPDATE technical_docs t
   SET metadata = CASE
       WHEN jsonb_typeof(t.metadata->'domains') = 'string'
            AND (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domains') WHEN 'array' THEN t.metadata->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
       THEN (t.metadata - 'domain') || jsonb_build_object('domains',
            (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'domains') WHEN 'array' THEN t.metadata->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g))
       ELSE t.metadata - 'domain' END
 WHERE jsonb_typeof(t.metadata) = 'object'
   AND lower(regexp_replace(coalesce(t.metadata->>'type', ''), '^\s+|\s+$', '', 'g'))
       NOT IN ('decision', 'retrospective')
   AND NOT coalesce(jsonb_typeof(t.metadata->'decision') = 'object'
                    AND ((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domain') WHEN 'array' THEN t.metadata->'decision'->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
                         OR (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domains') WHEN 'array' THEN t.metadata->'decision'->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL), false)
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
                    AND ((SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domain') WHEN 'array' THEN t.metadata->'decision'->'domain'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domain') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL
                         OR (SELECT jsonb_agg(v ORDER BY o) FROM (SELECT v, min(ord) o FROM (
          SELECT regexp_replace(e #>> '{}', '^\s+|\s+$', '', 'g') v, ord
            FROM jsonb_array_elements(CASE jsonb_typeof(t.metadata->'decision'->'domains') WHEN 'array' THEN t.metadata->'decision'->'domains'
                 WHEN 'string' THEN jsonb_build_array(t.metadata->'decision'->'domains') ELSE '[]'::jsonb END)
                 WITH ORDINALITY a(e, ord)
           WHERE jsonb_typeof(e) = 'string') q WHERE v <> '' GROUP BY v) g) IS NOT NULL), false)
   AND t.metadata ? 'domain';

RAISE NOTICE '044: % moved, % repaired in place, % left for the operator', n1, n2, n3;
END $mig$;
