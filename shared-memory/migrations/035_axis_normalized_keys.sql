-- 035 — ONE normalization key for the project and domain axes, in the database.
--
-- WHAT WAS WRONG. Both axis registries answered on EXACT STRINGS. `projects`
-- has a UNIQUE on `name` and `project_domains` a UNIQUE on (project_id, name),
-- so `Orbit_Relay` and `orbit-relay` were two legal, unrelated rows — two
-- projects, as far as the schema was concerned, differing only in punctuation.
-- The gateway has always known better: `axis_key()` (in
-- `scripts/project_axis.py`) reduces both to one key and refuses the second as
-- a SPELLING of the first. But that guard lives entirely in Python and fires
-- only on the `new_project`/`new_domain` path, so the constraint the whole axis
-- design depends on — two registered names never share a key — was never
-- something the database could state, let alone enforce.
--
-- WHAT THIS DOES.
--
--   1. `axis_normalize(text)` — the key, as SQL, so the triggers below and any
--      reader in any language get the same answer as the gateway.
--   2. A TRIGGER-MAINTAINED `normalized_key` COLUMN on each registry, with a
--      plain UNIQUE constraint on it — `projects (normalized_key)` and
--      `project_domains (project_id, normalized_key)`. That is the new
--      invariant made structural: **two registered names never share a key.**
--   3. A BACKFILL, then a PRE-CHECK that names the colliding PAIR before the
--      constraint is added, so a collision produces an answerable message
--      instead of Postgres' key-only `DETAIL`.
--   4. An apply-time SELF-CHECK against the SAME fixture list the Python side
--      is asserted on, so the two definitions cannot drift apart silently.
--   5. CONTINUOUS enforcement of the alias rules on the KEY, by extending the
--      trigger functions migrations 024 and 028 already own.
--
-- ⛔ WHY A TRIGGER-MAINTAINED COLUMN AND NOT A UNIQUE FUNCTIONAL INDEX. The
-- functional index was the first shape of this migration and it was wrong twice
-- over, both reasons found in review:
--
--   * `generate_schema_init.py` emits indexes with their table and functions
--     AFTER every table, so a fresh install built from a regenerated
--     `schema_init.sql` would have hit `axis_normalize(text) does not exist` —
--     and the file is one transaction, so it would have created NOTHING. (The
--     generator's ordering is fixed in this same change; the schema still
--     should not depend on that fix to be installable.)
--   * An `IMMUTABLE` function over locale-dependent `[:alnum:]` backing a
--     UNIQUE INDEX is a latent trap: a collation, ICU or `pg_upgrade` change
--     silently splits old index entries from new ones, the invariant quietly
--     stops holding, and nothing re-checks. A stored column re-derives only
--     when a row is written, and a `REINDEX` cannot resurrect a stale key.
--
-- Migration 033 hit exactly this and chose the column; its own comment says why
-- a `GENERATED … STORED` column is not an option here either (the generator
-- introspects `information_schema.columns`, which does not report a generation
-- expression, so it would render as a bare unpopulated column). A BEFORE
-- INSERT/UPDATE trigger uses the function-and-trigger path the generator DOES
-- faithfully reproduce. This migration follows that precedent exactly.
--
-- ⚠ WHY `axis_normalize` REPEATS `entity_normalize`'s BODY (033) INSTEAD OF
-- CALLING IT. Same generator property: nothing orders one function before
-- another, so a wrapper could be emitted before its callee. The body is
-- repeated and the two are pinned together LOUDLY instead — the self-check
-- below asserts they agree on every fixture and fails the migration if a future
-- edit moves one of them. The separate name is right regardless: an axis key
-- and an entity key are different concepts that share a rule today, and this is
-- where a divergence would get written down rather than discovered.
--
-- ⚠ `[:alnum:]` IS LOCALE-DEPENDENT, exactly as 033 records: under a UTF-8
-- database with a non-`C` collation, accented and non-Latin letters survive
-- normalization; under `C` it behaves like `[a-zA-Z0-9]`. On a `C`-locale
-- deployment the SQL key and the Python key DISAGREE for any non-ASCII name —
-- which is why the self-check runs a fixture containing accented Latin and
-- Greek letters and RAISES rather than warns.
--
-- ⛔ AND WHY THERE IS NO KEY-UNIQUE CONSTRAINT ON `aliases`. It is a shared
-- string-intern table: migration 024 states, in the table's own comment, that
-- "the same spelling can legitimately alias on more than one axis — a word that
-- names a project here can name a section of a different project there". A
-- global key-unique constraint would forbid that by construction — not because
-- the data collides, but because the DESIGN allows what it would refuse. What
-- must actually hold is narrower: within one axis and one scope, a key must
-- never resolve to two different canonicals. That is a cross-table rule no
-- constraint can see, so it is enforced CONTINUOUSLY by the trigger functions
-- 024 and 028 already own, extended here to compare on the key.
--
-- MEASURED on the live deployment 2026-08-25, BEFORE this migration: 38
-- projects, 18 active project aliases, 0 key collisions; `project_domains` 0
-- collisions within any project; `domain_aliases` 0 rows. Every statement below
-- is expected to be a no-op on that data — and if it is not, that is the news,
-- which is why nothing here is written to skip quietly.
--
-- IDEMPOTENT: every statement is re-runnable. The backfill is an UPDATE
-- restricted to rows whose key is already wrong, so a re-run touches zero rows.

BEGIN;

-- ─── axis_normalize() — THE axis key, as SQL ─────────────────────────────────
--
-- IMMUTABLE because the trigger and the constraint machinery both want a stable
-- answer for a stable input. STRICT so a NULL name keys to NULL rather than to
-- the empty string.
CREATE OR REPLACE FUNCTION axis_normalize(name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT regexp_replace(lower(name), '[^[:alnum:]]', '', 'g');
$$;

-- ─── The self-check: this file and project_axis.py agree, or nothing applies ──
--
-- The pairs below are `AXIS_KEY_FIXTURES` in `scripts/project_axis.py`, verbatim
-- and in the same order. The suite asserts the Python side against them; this
-- block asserts the SQL side against them at apply time. Neither implementation
-- can move without the other unless someone edits BOTH lists — which is the
-- point, because that edit is a deliberate act and a silent drift is not.
DO $$
DECLARE
    fixture   text[][] := ARRAY[
        ['orbit-relay',    'orbitrelay'],
        ['Orbit_Relay',    'orbitrelay'],
        ['orbit relay',    'orbitrelay'],
        ['ORBIT-RELAY',    'orbitrelay'],
        ['  orbit-relay  ','orbitrelay'],
        ['orbit.relay',    'orbitrelay'],
        ['orbit/relay',    'orbitrelay'],
        ['alpha-service-2',  'alphaservice2'],
        ['Ops2026',          'ops2026'],
        ['Ãgua-Viva',        'ãguaviva'],
        ['Ωmega_Project',    'ωmegaproject'],
        ['Über-Tooling',     'übertooling'],
        ['---',              ''],
        ['',                 '']
    ];
    i         int;
    got       text;
BEGIN
    FOR i IN 1 .. array_length(fixture, 1) LOOP
        got := axis_normalize(fixture[i][1]);
        IF got IS DISTINCT FROM fixture[i][2] THEN
            RAISE EXCEPTION
                'axis_normalize(%) = % but the gateway''s axis_key() says % — '
                'the SQL and Python definitions of the axis key have diverged, '
                'or this database''s locale does not treat non-ASCII letters as '
                'alphanumeric. Do NOT key a registry on a rule the gateway does '
                'not share.',
                fixture[i][1], got, fixture[i][2];
        END IF;
        -- The second pin: this schema now states one normalization rule under
        -- two names, and the ONLY thing keeping them one rule is this line.
        IF got IS DISTINCT FROM entity_normalize(fixture[i][1]) THEN
            RAISE EXCEPTION
                'axis_normalize(%) and entity_normalize(%) disagree — one of the '
                'two copies of this schema''s normalization rule has been edited '
                'without the other.', fixture[i][1], fixture[i][1];
        END IF;
    END LOOP;
END;
$$;

-- ─── The column, and the trigger that maintains it ───────────────────────────
--
-- Nullable at first: the column has to exist before the backfill can fill it,
-- and NOT NULL is added below once every row has a value.
ALTER TABLE projects        ADD COLUMN IF NOT EXISTS normalized_key text;
ALTER TABLE project_domains ADD COLUMN IF NOT EXISTS normalized_key text;

-- ONE function for both registries. They enforce the same rule on the same
-- column, and two functions would be two rules the day one of them is edited —
-- the same reasoning `spelling_variant_of` follows on the Python side.
--
-- It re-derives from `NEW.name` on every write, so a rename can never leave a
-- stale key behind, and a caller cannot set the column to something the name
-- does not normalize to: whatever it sends is overwritten.
CREATE OR REPLACE FUNCTION axis_registry_before_write()
RETURNS trigger AS $$
BEGIN
    NEW.normalized_key := axis_normalize(NEW.name);

    IF NEW.normalized_key = '' THEN
        RAISE EXCEPTION
            '% "%" normalizes to the empty string — every character is '
            'punctuation, whitespace or similar, so there is no spelling left '
            'to register. Name it with at least one letter or digit.',
            TG_TABLE_NAME, NEW.name;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_projects_axis_key ON projects;
CREATE TRIGGER trg_projects_axis_key
    BEFORE INSERT OR UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION axis_registry_before_write();

DROP TRIGGER IF EXISTS trg_project_domains_axis_key ON project_domains;
CREATE TRIGGER trg_project_domains_axis_key
    BEFORE INSERT OR UPDATE ON project_domains
    FOR EACH ROW EXECUTE FUNCTION axis_registry_before_write();

-- ─── Backfill ────────────────────────────────────────────────────────────────
--
-- Restricted to rows whose stored key is not already the right one, so a re-run
-- updates nothing at all rather than rewriting every row to itself.
UPDATE projects
   SET normalized_key = axis_normalize(name)
 WHERE normalized_key IS DISTINCT FROM axis_normalize(name);

UPDATE project_domains
   SET normalized_key = axis_normalize(name)
 WHERE normalized_key IS DISTINCT FROM axis_normalize(name);

-- ─── The collision pre-check — name the PAIR, and the query ──────────────────
--
-- Adding the UNIQUE constraint below would already fail on a collision, and
-- would roll the whole migration back, which is the correct shape. What it
-- would NOT do is say WHICH TWO NAMES collided: Postgres reports the duplicated
-- key and leaves the operator to write the join, at exactly the moment they are
-- mid-migration and the data question is urgent. So the pair is found first,
-- and the message carries both names and the query that lists the rest.
--
-- ⛔ IT DOES NOT REPAIR ANYTHING. Which of two spellings is the project, and
-- what happens to the records filed under the other, is a data judgement with
-- history hanging off it — never something a migration may answer by picking.
DO $$
DECLARE
    a_name text;
    b_name text;
    k      text;
BEGIN
    SELECT p1.name, p2.name, p1.normalized_key
      INTO a_name, b_name, k
      FROM projects p1
      JOIN projects p2
        ON p2.normalized_key = p1.normalized_key AND p2.id > p1.id
     ORDER BY p1.name, p2.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'projects % and % are both registered and normalize to the same '
            'axis key %, so they are two spellings of one project. Decide which '
            'is the project, move the other''s records onto it and retire the '
            'name as an alias, then re-run. List every such pair with: '
            'SELECT p1.name, p2.name, p1.normalized_key FROM projects p1 JOIN '
            'projects p2 ON p2.normalized_key = p1.normalized_key AND p2.id > '
            'p1.id ORDER BY 1,2;',
            a_name, b_name, k;
    END IF;

    SELECT d1.name, d2.name, d1.normalized_key
      INTO a_name, b_name, k
      FROM project_domains d1
      JOIN project_domains d2
        ON d2.project_id = d1.project_id
       AND d2.normalized_key = d1.normalized_key
       AND d2.id > d1.id
     ORDER BY d1.name, d2.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'sections % and % of the same project both normalize to the axis '
            'key %, so they are two spellings of one section. Decide which is '
            'the section, move the other''s records onto it and retire the name '
            'as an alias, then re-run. List every such pair with: SELECT '
            'd1.project_id, d1.name, d2.name, d1.normalized_key FROM '
            'project_domains d1 JOIN project_domains d2 ON d2.project_id = '
            'd1.project_id AND d2.normalized_key = d1.normalized_key AND d2.id '
            '> d1.id ORDER BY 1,2,3;',
            a_name, b_name, k;
    END IF;
END;
$$;

-- ─── The invariant, structurally ─────────────────────────────────────────────
--
-- Projects are global; a section is identified WITHIN its project, so its key
-- is unique per (project_id, key) and nowhere wider — two projects may both
-- have a `graph-quality` section and they are different sections.
--
-- DROP-then-ADD rather than a guarded ADD: a constraint may exist from an
-- earlier partial run under a different definition, and re-adding validates the
-- existing rows, which is the point.
ALTER TABLE projects ALTER COLUMN normalized_key SET NOT NULL;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_normalized_key_unique;
ALTER TABLE projects
    ADD CONSTRAINT projects_normalized_key_unique UNIQUE (normalized_key);

ALTER TABLE project_domains ALTER COLUMN normalized_key SET NOT NULL;
ALTER TABLE project_domains
    DROP CONSTRAINT IF EXISTS project_domains_normalized_key_unique;
ALTER TABLE project_domains
    ADD CONSTRAINT project_domains_normalized_key_unique
    UNIQUE (project_id, normalized_key);

-- ─── The alias rules, on the KEY, enforced CONTINUOUSLY ──────────────────────
--
-- ⛔ AN APPLY-TIME CHECK IS NOT AN INVARIANT. The first shape of this migration
-- asserted the alias rules once, in a DO block, and stopped: after it applied,
-- nothing prevented a colliding alias being written the next day. The schema
-- already owns the continuous mechanism for the EXACT-STRING form of these very
-- rules — 024's `assert_alias_namespaces_disjoint()` and 028's
-- `assert_domain_alias_namespaces_disjoint()`, on four triggers — so the fix is
-- to widen those from the string to the key, not to add a second mechanism.
--
-- Each keeps its original exact-string rule UNCHANGED and gains a key rule
-- beside it. The two are separate statements rather than one widened comparison
-- for a reason found while writing them:
--
-- ⛔ THE KEY RULE MUST EXCLUDE THE ALIAS'S OWN TARGET, AND THE EXACT RULE MUST
-- NOT. Retiring a spelling is exactly the operation that produces an alias
-- keying like a live project — renaming `Orbit_Relay` to `orbit-relay` demotes
-- the old name to an alias of the new one, and those two ARE one key. Widening
-- the original comparison in place would have refused every such rename, which
-- is the one operation this whole alias mechanism exists to support. The
-- ambiguity being guarded is "one key, two DIFFERENT answers"; an alias keying
-- like the project it already points at has one answer and is merely redundant.
--
-- ⚠ They call `axis_normalize(...)` rather than reading `normalized_key`, so
-- they carry NO dependency on which BEFORE trigger fires first. Firing order is
-- alphabetical by trigger name and is not something a rule this important
-- should rest on.

CREATE OR REPLACE FUNCTION assert_alias_namespaces_disjoint()
RETURNS trigger AS $$
DECLARE
    v_alias text;
BEGIN
    IF TG_TABLE_NAME = 'project_aliases' THEN
        SELECT name INTO v_alias FROM aliases WHERE id = NEW.alias_id;
        -- 024's original rule, on the exact string, unchanged.
        IF NEW.active AND EXISTS (
            SELECT 1 FROM projects p WHERE p.name = v_alias
        ) THEN
            RAISE EXCEPTION
                'alias % is also a registered project — an alias and a canonical '
                'name must never be the same string (A1)', v_alias;
        END IF;
        -- The same rule on the KEY, excluding the project this alias points at.
        IF NEW.active AND EXISTS (
            SELECT 1 FROM projects p
             WHERE p.id <> NEW.project_id
               AND axis_normalize(p.name) = axis_normalize(v_alias)
        ) THEN
            RAISE EXCEPTION
                'alias % normalizes to the same axis key as a DIFFERENT '
                'registered project — one key would resolve two ways, and the '
                'gateway''s by-key resolution would answer by luck (A1)', v_alias;
        END IF;
        -- And two active aliases keying alike must not point at two projects.
        IF NEW.active AND EXISTS (
            SELECT 1 FROM project_aliases pa
              JOIN aliases a ON a.id = pa.alias_id
             WHERE pa.active
               AND pa.id <> NEW.id
               AND pa.project_id <> NEW.project_id
               AND axis_normalize(a.name) = axis_normalize(v_alias)
        ) THEN
            RAISE EXCEPTION
                'alias % normalizes to the same axis key as an active alias of '
                'a DIFFERENT project — one key would resolve two ways (A1)',
                v_alias;
        END IF;
    ELSE  -- projects
        IF EXISTS (
            SELECT 1 FROM project_aliases pa
              JOIN aliases a ON a.id = pa.alias_id
             WHERE pa.active AND a.name = NEW.name
        ) THEN
            RAISE EXCEPTION
                'project % is already an active alias for another project — '
                'register the canonical name instead (A1)', NEW.name;
        END IF;
        IF EXISTS (
            SELECT 1 FROM project_aliases pa
              JOIN aliases a ON a.id = pa.alias_id
             WHERE pa.active
               AND pa.project_id <> NEW.id
               AND axis_normalize(a.name) = axis_normalize(NEW.name)
        ) THEN
            RAISE EXCEPTION
                'project % normalizes to the same axis key as an active alias '
                'for another project — register the canonical name instead (A1)',
                NEW.name;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION assert_domain_alias_namespaces_disjoint()
RETURNS trigger AS $$
DECLARE
    v_alias text;
BEGIN
    IF TG_TABLE_NAME = 'domain_aliases' THEN
        IF NOT NEW.active THEN
            RETURN NEW;
        END IF;
        -- NEW.project_id, not a lookup through domain_id: the composite foreign
        -- key already guarantees the two agree, so re-deriving it here would
        -- only add a way for this rule and that key to disagree.
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
        -- The same rule on the KEY, excluding the section this alias points at
        -- — retiring a spelling is exactly what produces an alias keying like a
        -- live section, and that rename must stay possible.
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
$$ LANGUAGE plpgsql;

COMMIT;
