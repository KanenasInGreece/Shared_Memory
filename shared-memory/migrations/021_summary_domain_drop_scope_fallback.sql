-- Migration 021: stop keying summaries on scope, which is who may see a record,
-- not what it is about. A domain that is only a live scope value, and not a real
-- project or domain, is rewritten to 'general' unless that entity already has a
-- general summary — the unique index would reject the second row, so those are
-- counted and left in place. Scope names are discovered, not hardcoded.

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
