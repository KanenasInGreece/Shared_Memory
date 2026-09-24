-- 042_function_comment_parity.sql
--
-- Re-create three already-existing function bodies so they match the definitions
-- a fresh install produces, making verify_schema_init.py's live-versus-fresh
-- function comparison empty again (postflight A3).
--
-- WHY THIS EXISTS, AND WHY IT IS A NEW FILE RATHER THAN AN EDIT. The v0.9.113
-- comment cut shortened comments INSIDE the bodies of these three functions in
-- their original migrations. Those migrations were long since recorded in
-- schema_migrations on every existing install, and apply.py never re-runs a
-- recorded migration, so the live bodies stayed frozen with the old comments
-- while the migration files and schema_init.sql moved on. A fresh install was
-- never affected -- it builds from the current files and matches them. Every
-- install older than that cut diverged, and A3 failed on all three of ours for
-- exactly this reason.
--
-- A MIGRATION IS IMMUTABLE ONCE APPLIED. Editing the original files again would
-- change nothing on any existing database, so the repair has to be a new file
-- that every install applies once.
--
-- WHAT THE DIFFERENCE WAS, AND HOW THAT IS KNOWN. By inspection the three
-- bodies differed only in comment text. That characterisation was first
-- "proved" by stripping SQL comments line-wise and comparing, and THAT METHOD IS
-- UNSOUND -- a review caught it: a line-wise strip truncates any string literal
-- containing a -- sequence, and a second attempt to bound the risk by extracting
-- string literals with a regex failed the same way, because an apostrophe inside
-- a comment (canonical's, row's) opens a spurious literal. Do not reuse either
-- script.
--
-- The claim this migration actually rests on needs no comment parsing. After
-- applying it, pg_get_functiondef for each of the three functions is
-- BYTE-IDENTICAL to the definition schema_init.sql installs, modulo the trailing
-- semicolon pg_get_functiondef does not emit. Independently, verify_schema_init.py
-- -- which builds a throwaway database from schema_init.sql and diffs it against
-- live using Postgres's own introspection, never this repair's reasoning -- went
-- from exit 1 to exit 0, reporting functions 132/132.
--
-- Worth applying regardless of how small the difference is: a verifier that
-- cannot explain a difference is a verifier nobody can use as a gate, and A3
-- blocks postflight on every install older than the cut.
--
-- ORDERING HAZARD, CLEARED BY MEASUREMENT BEFORE THIS LANDED. apply.py resumes
-- from SELECT max(filename), not from the set of applied filenames (see its own
-- latest_applied docstring), so a database already past this number would skip
-- this file for ever. Checked on every host first: d9400 and glxvm were at
-- 041_outbox_type_check.sql and 041 was the highest file in main, so none could
-- be beyond 042. Anyone adding a repair migration inherits this check.
--
-- Idempotent by construction: CREATE OR REPLACE FUNCTION only. No trigger is
-- dropped or recreated, and every trigger already bound to these functions
-- keeps pointing at the same name, so there is no window in which a write
-- escapes its guard.

CREATE OR REPLACE FUNCTION public.notify_new_artifact()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN
  PERFORM pg_notify('new_artifact', json_build_object('pg_id', NEW.id)::text);
  RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.entity_vocab_aliases_before_write()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_parent_exists boolean;
BEGIN
    NEW.normalized_alias := entity_normalize(NEW.alias);

    IF NEW.normalized_alias = '' THEN
        RAISE EXCEPTION
            'alias "%" normalizes to the empty string — every character was '
            'stripped, so it cannot be registered', NEW.alias;
    END IF;

    SELECT EXISTS (SELECT 1 FROM entity_vocabulary WHERE id = NEW.entity_id)
      INTO v_parent_exists;
    IF NOT v_parent_exists THEN
        RAISE EXCEPTION
            'entity_vocab_aliases.entity_id % does not reference a known '
            'entity_vocabulary row', NEW.entity_id;
    END IF;

    -- A different canonical's key is a collision. The parent's own key is not.
    IF EXISTS (
        SELECT 1 FROM entity_vocabulary v
         WHERE v.normalized_key = NEW.normalized_alias
           AND v.id <> NEW.entity_id
    ) THEN
        RAISE EXCEPTION
            'alias "%" normalizes to "%", which is already a DIFFERENT '
            'canonical entity''s identity — a normalized value must never '
            'resolve to two different identities',
            NEW.alias, NEW.normalized_alias;
    END IF;

    -- other.id excludes this row's previous state on UPDATE. The ambiguity
    -- check is the entity_id comparison, not that id.
    IF EXISTS (
        SELECT 1 FROM entity_vocab_aliases other
         WHERE other.normalized_alias = NEW.normalized_alias
           AND other.entity_id <> NEW.entity_id
           AND other.id <> NEW.id
    ) THEN
        RAISE EXCEPTION
            'alias "%" normalizes to "%", which is already an alias of a '
            'DIFFERENT entity — a normalized value must never resolve to '
            'two different identities',
            NEW.alias, NEW.normalized_alias;
    END IF;

    RETURN NEW;
END;
$function$
;

CREATE OR REPLACE FUNCTION public.assert_domain_alias_namespaces_disjoint()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
DECLARE
    v_alias text;
BEGIN
    IF TG_TABLE_NAME = 'domain_aliases' THEN
        IF NOT NEW.active THEN
            RETURN NEW;
        END IF;
        -- NEW.project_id, not a lookup: the composite FK already makes them agree.
        SELECT name INTO v_alias FROM aliases WHERE id = NEW.alias_id;
        -- 028's original rule, on the exact string, unchanged.
        IF EXISTS (
            SELECT 1 FROM project_domains d
             WHERE d.project_id = NEW.project_id
               AND d.name = v_alias
        ) THEN
            RAISE EXCEPTION
                'alias % is also a registered domain of the same project — an '
                'alias and a canonical name must never be the same string '
                'within one project (A1)', v_alias;
        END IF;
        -- Same rule on the key, excluding this alias's own section, so a rename stays possible.
        IF EXISTS (
            SELECT 1 FROM project_domains d
             WHERE d.project_id = NEW.project_id
               AND d.id <> NEW.domain_id
               AND axis_normalize(d.name) = axis_normalize(v_alias)
        ) THEN
            RAISE EXCEPTION
                'alias % normalizes to the same axis key as a DIFFERENT '
                'registered section of this project — one key would resolve two '
                'ways (A1)', v_alias;
        END IF;
        IF EXISTS (
            SELECT 1 FROM domain_aliases da
              JOIN aliases a ON a.id = da.alias_id
             WHERE da.active
               AND da.id <> NEW.id
               AND da.project_id = NEW.project_id
               AND da.domain_id <> NEW.domain_id
               AND axis_normalize(a.name) = axis_normalize(v_alias)
        ) THEN
            RAISE EXCEPTION
                'alias % normalizes to the same axis key as an active alias of a '
                'DIFFERENT section of this project — one key would resolve two '
                'ways (A1)', v_alias;
        END IF;
    ELSE  -- project_domains
        -- 028's original rule, on the exact string, unchanged.
        IF EXISTS (
            SELECT 1 FROM domain_aliases da
              JOIN aliases a ON a.id = da.alias_id
              JOIN project_domains d ON d.id = da.domain_id
             WHERE da.active
               AND d.project_id = NEW.project_id
               AND a.name = NEW.name
        ) THEN
            RAISE EXCEPTION
                'domain % is already an active alias for another domain of this '
                'project — register the canonical name instead (A1)', NEW.name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM domain_aliases da
              JOIN aliases a ON a.id = da.alias_id
              JOIN project_domains d ON d.id = da.domain_id
             WHERE da.active
               AND d.project_id = NEW.project_id
               AND d.id <> NEW.id
               AND axis_normalize(a.name) = axis_normalize(NEW.name)
        ) THEN
            RAISE EXCEPTION
                'domain % normalizes to the same axis key as an active alias '
                'for another domain of this project — register the canonical '
                'name instead (A1)', NEW.name;
        END IF;
    END IF;
    RETURN NEW;
END;
$function$
;
