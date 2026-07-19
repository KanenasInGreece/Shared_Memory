-- 021 — re-key community summaries off the scope-derived domain fallback.
--
-- The consolidation domain key was COALESCE(project, domain, scope, 'general').
-- `scope` is an ACCESS-CONTROL axis (it pairs with visibility='scope' on the read
-- path), not a topical one, so a record with no project and no domain was keyed by
-- who may SEE it rather than what it is ABOUT. The chain is now
-- COALESCE(project, domain, 'general').
--
-- Consequence for existing rows: summaries whose domain key came from that
-- fallback carry a SCOPE value, and would otherwise be orphaned — the daemon would
-- look for 'general', not find them, and fold a duplicate alongside.
--
-- Portability: the affected values are discovered from the live `scope` column, not
-- hardcoded to any one deployment's scope names. A summary is re-keyed only when its
-- domain is a value that appears in `scope` AND that no record actually uses as a
-- project or domain — so a deployment that legitimately names a project after a
-- scope keeps its summary untouched.
--
-- Idempotent and guarded: re-keying is skipped where a 'general' summary already
-- exists for that entity (the (entity, domain) unique index would reject it); those
-- rows are left in place and reported rather than silently dropped.

DO $$
DECLARE
    rekeyed    int;
    conflicted int;
BEGIN
    CREATE TEMP TABLE _scope_only_domains ON COMMIT DROP AS
        SELECT DISTINCT scope AS val
        FROM technical_docs
        WHERE NULLIF(scope, '') IS NOT NULL
    EXCEPT
        SELECT DISTINCT COALESCE(NULLIF(metadata->>'project', ''),
                                 NULLIF(metadata->>'domain', ''))
        FROM technical_docs
        WHERE COALESCE(NULLIF(metadata->>'project', ''),
                       NULLIF(metadata->>'domain', '')) IS NOT NULL;

    SELECT count(*) INTO conflicted
    FROM community_summaries a
    WHERE a.metadata->>'domain' IN (SELECT val FROM _scope_only_domains)
      AND EXISTS (SELECT 1 FROM community_summaries b
                  WHERE b.metadata->>'entity' = a.metadata->>'entity'
                    AND b.metadata->>'domain' = 'general');

    UPDATE community_summaries a
       SET metadata = jsonb_set(a.metadata, '{domain}', '"general"')
     WHERE a.metadata->>'domain' IN (SELECT val FROM _scope_only_domains)
       AND NOT EXISTS (SELECT 1 FROM community_summaries b
                       WHERE b.metadata->>'entity' = a.metadata->>'entity'
                         AND b.metadata->>'domain' = 'general');
    GET DIAGNOSTICS rekeyed = ROW_COUNT;

    RAISE NOTICE 'migration 021: re-keyed % summary/summaries from a scope-derived domain to general', rekeyed;
    IF conflicted > 0 THEN
        RAISE NOTICE 'migration 021: % left on the old key (a general summary already exists for that entity) — resolve by hand', conflicted;
    END IF;
END $$;
