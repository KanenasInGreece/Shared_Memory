-- Migration 035: a stored normalized_key so two registered names cannot share
-- an axis key. The Python guard only ran on the new-name path. Not a unique
-- functional index: an IMMUTABLE index over locale-dependent [:alnum:] can
-- split when the collation changes, and a function used to be emitted after
-- the index that calls it. Not GENERATED STORED: the schema generator would
-- drop the expression (033). axis_normalize copies entity_normalize's body
-- because nothing orders one function before another; the fixture check fails
-- the migration if they disagree, including on non-ASCII where a C locale
-- would not match Python. aliases has no key-unique constraint: one spelling
-- may alias on more than one axis (024). Existing rows are pre-checked;
-- later writes use the widened triggers.
-- nobody counted registered names that normalize to the EMPTY string (fact:1338).

BEGIN;

-- IMMUTABLE so the stored key is stable. STRICT so a NULL name stays NULL
-- rather than becoming the empty string.
CREATE OR REPLACE FUNCTION axis_normalize(name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
    SELECT regexp_replace(lower(name), '[^[:alnum:]]', '', 'g');
$$;

-- Same list as AXIS_KEY_FIXTURES in project_axis.py, same order. Also pins
-- axis_normalize to entity_normalize so the two copies cannot drift quietly.
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
        -- The two copies of the rule. Editing one without the other fails here.
        IF got IS DISTINCT FROM entity_normalize(fixture[i][1]) THEN
            RAISE EXCEPTION
                'axis_normalize(%) and entity_normalize(%) disagree — one of the '
                'two copies of this schema''s normalization rule has been edited '
                'without the other.', fixture[i][1], fixture[i][1];
        END IF;
    END LOOP;
END;
$$;

-- Nullable until the backfill below; NOT NULL is added once every row has a key.
ALTER TABLE projects        ADD COLUMN IF NOT EXISTS normalized_key text;
ALTER TABLE project_domains ADD COLUMN IF NOT EXISTS normalized_key text;

-- The unnameable-row pre-check runs before the trigger, whose message is
-- written for a caller, not for an operator who already holds the row.
-- `---` is not blank, so the existing CHECKs do not catch it, and this block
-- renames nothing. fact:1338 — an unmeasured number is not a count this file
-- may claim.
DO $$
DECLARE
    bad_name text;
    bad_id   bigint;
BEGIN
    SELECT name, id INTO bad_name, bad_id
      FROM projects WHERE axis_normalize(name) = ''
     ORDER BY id LIMIT 1;
    IF bad_name IS NOT NULL THEN
        RAISE EXCEPTION
            'project % (id %) normalizes to the empty string — every character '
            'is punctuation, whitespace or similar, so it has no axis key and '
            'cannot be told apart from any other such name. Rename it to '
            'something with at least one letter or digit (a deliberate operation '
            'with its own tool and ledger), or retire it, then re-run. List them '
            'all with: SELECT id, name FROM projects WHERE '
            'axis_normalize(name) = '''' ORDER BY id;',
            bad_name, bad_id;
    END IF;

    SELECT name, id INTO bad_name, bad_id
      FROM project_domains WHERE axis_normalize(name) = ''
     ORDER BY id LIMIT 1;
    IF bad_name IS NOT NULL THEN
        RAISE EXCEPTION
            'section % (id %) normalizes to the empty string — every character '
            'is punctuation, whitespace or similar, so it has no axis key. '
            'Rename it to something with at least one letter or digit, or retire '
            'it, then re-run. List them all with: SELECT id, project_id, name '
            'FROM project_domains WHERE axis_normalize(name) = '''' ORDER BY id;',
            bad_name, bad_id;
    END IF;
END;
$$;

-- One function for both registries. It overwrites NEW.normalized_key from
-- NEW.name, so a caller cannot store a key the name does not normalize to.
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

-- A re-run updates nothing: only a key that is already wrong is rewritten.
UPDATE projects
   SET normalized_key = axis_normalize(name)
 WHERE normalized_key IS DISTINCT FROM axis_normalize(name);

UPDATE project_domains
   SET normalized_key = axis_normalize(name)
 WHERE normalized_key IS DISTINCT FROM axis_normalize(name);

-- Name the colliding pair. ADD CONSTRAINT would report only the key.
-- Which spelling wins is never something a migration may answer by picking.
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

-- Projects are global. A section key is unique per project, so two projects
-- may share a section name. Drop then add: a partial run may have left a
-- different definition, and re-adding validates the rows already there.
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

-- The alias rules over EXISTING rows. The triggers below only see later
-- writes, and there is no constraint to add: the rule crosses tables, so a
-- violating pair already stored would otherwise survive this file.
DO $$
DECLARE
    a_name text;
    b_name text;
    k      text;
BEGIN
    SELECT a1.name, a2.name, axis_normalize(a1.name)
      INTO a_name, b_name, k
      FROM project_aliases pa1
      JOIN aliases a1 ON a1.id = pa1.alias_id
      JOIN project_aliases pa2 ON pa2.active AND pa2.id > pa1.id
                              AND pa2.project_id <> pa1.project_id
      JOIN aliases a2 ON a2.id = pa2.alias_id
                     AND axis_normalize(a2.name) = axis_normalize(a1.name)
     WHERE pa1.active
     ORDER BY a1.name, a2.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'active project aliases % and % both normalize to the axis key % but '
            'point at DIFFERENT projects, so that key already resolves two ways '
            'and by-key resolution would answer by luck. Retire whichever mapping '
            'is wrong (set it inactive, with a reason), then re-run. '
            'List every such pair with: SELECT a1.name, a2.name, axis_normalize(a1.name) '
            'FROM project_aliases pa1 JOIN aliases a1 ON a1.id = pa1.alias_id '
            'JOIN project_aliases pa2 ON pa2.active AND pa2.id > pa1.id AND '
            'pa2.project_id <> pa1.project_id JOIN aliases a2 ON a2.id = '
            'pa2.alias_id AND axis_normalize(a2.name) = axis_normalize(a1.name) '
            'WHERE pa1.active ORDER BY 1,2;',
            a_name, b_name, k;
    END IF;

    SELECT a.name, p.name, axis_normalize(a.name)
      INTO a_name, b_name, k
      FROM project_aliases pa
      JOIN aliases a ON a.id = pa.alias_id
      JOIN projects p ON p.id <> pa.project_id
                     AND axis_normalize(p.name) = axis_normalize(a.name)
     WHERE pa.active
     ORDER BY a.name, p.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'active alias % normalizes to the axis key %, which is also the key '
            'of the registered project % that it does NOT point at — one key, '
            'two answers (024/A1, now on the key rather than only the exact '
            'string). Decide which the name means and retire the other mapping, '
            'then re-run. List every such pair with: SELECT a.name, p.name, '
            'axis_normalize(a.name) FROM project_aliases pa JOIN aliases a ON '
            'a.id = pa.alias_id JOIN projects p ON p.id <> pa.project_id AND '
            'axis_normalize(p.name) = axis_normalize(a.name) WHERE pa.active '
            'ORDER BY 1,2;',
            a_name, k, b_name;
    END IF;

    SELECT a1.name, a2.name, axis_normalize(a1.name)
      INTO a_name, b_name, k
      FROM domain_aliases da1
      JOIN aliases a1 ON a1.id = da1.alias_id
      JOIN domain_aliases da2 ON da2.active AND da2.id > da1.id
                             AND da2.project_id = da1.project_id
                             AND da2.domain_id <> da1.domain_id
      JOIN aliases a2 ON a2.id = da2.alias_id
                     AND axis_normalize(a2.name) = axis_normalize(a1.name)
     WHERE da1.active
     ORDER BY a1.name, a2.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'active domain aliases % and % of one project both normalize to the '
            'axis key % but point at DIFFERENT sections, so that key already '
            'resolves two ways. Retire whichever mapping is wrong, then re-run. '
            'List every such pair with: SELECT a1.name, a2.name, '
            'axis_normalize(a1.name) FROM domain_aliases da1 JOIN aliases a1 ON '
            'a1.id = da1.alias_id JOIN domain_aliases da2 ON da2.active AND '
            'da2.id > da1.id AND da2.project_id = da1.project_id AND '
            'da2.domain_id <> da1.domain_id JOIN aliases a2 ON a2.id = '
            'da2.alias_id AND axis_normalize(a2.name) = axis_normalize(a1.name) '
            'WHERE da1.active ORDER BY 1,2;',
            a_name, b_name, k;
    END IF;

    SELECT a.name, d.name, axis_normalize(a.name)
      INTO a_name, b_name, k
      FROM domain_aliases da
      JOIN aliases a ON a.id = da.alias_id
      JOIN project_domains d ON d.project_id = da.project_id
                            AND d.id <> da.domain_id
                            AND axis_normalize(d.name) = axis_normalize(a.name)
     WHERE da.active
     ORDER BY a.name, d.name
     LIMIT 1;
    IF a_name IS NOT NULL THEN
        RAISE EXCEPTION
            'active domain alias % normalizes to the axis key %, which is also '
            'the key of the registered section % of the same project that it '
            'does NOT point at — one key, two answers (028/A1, on the key). '
            'Decide which the name means and retire the other mapping, then '
            're-run. List every such pair with: SELECT a.name, d.name, '
            'axis_normalize(a.name) FROM domain_aliases da JOIN aliases a ON '
            'a.id = da.alias_id JOIN project_domains d ON d.project_id = '
            'da.project_id AND d.id <> da.domain_id AND axis_normalize(d.name) '
            '= axis_normalize(a.name) WHERE da.active ORDER BY 1,2;',
            a_name, k, b_name;
    END IF;
END;
$$;

-- Key comparisons exclude the alias's own target; the exact-string rule does
-- not. A rename demotes the old spelling to an alias of the new name, and
-- those two share a key — refusing that would block the rename. They call
-- axis_normalize rather than reading normalized_key so trigger order does
-- not matter.

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
$$ LANGUAGE plpgsql;

COMMIT;
