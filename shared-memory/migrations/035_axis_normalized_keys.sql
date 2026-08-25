-- 035 — ONE normalization key for the project and domain axes, in the database.
--
-- WHAT WAS WRONG. Both axis registries answered on EXACT STRINGS. `projects`
-- has a UNIQUE on `name` and `project_domains` a UNIQUE on (project_id, name),
-- so `Orbit_Relay` and `orbit-relay` were two legal, unrelated rows — two
-- projects, as far as the schema was concerned, differing only in punctuation.
-- The gateway has always known better: `spelling_key()` (now `axis_key()`, in
-- `scripts/project_axis.py`) reduces both to one key and refuses the second as
-- a SPELLING of the first. But that guard lives entirely in Python and fires
-- only on the `new_project`/`new_domain` path, so the constraint the whole axis
-- design depends on — two registered names never share a key — was never
-- something the database could state, let alone enforce.
--
-- WHAT THIS DOES.
--
--   1. `axis_normalize(text)` — the key, as SQL, so an index can be built on it
--      and so a reader in any language gets the same answer as the gateway.
--   2. A UNIQUE FUNCTIONAL INDEX per registry, which is the new invariant made
--      structural: **two registered names never share a key**, on projects
--      globally and on sections within their project.
--   3. An apply-time SELF-CHECK against the SAME fixture list the Python side
--      is asserted on, so the two definitions cannot drift apart silently.
--   4. An apply-time AMBIGUITY CHECK over the ALIAS tables, which the indexes
--      cannot cover (see below), failing loudly rather than skipping.
--
-- ⚠ WHY `axis_normalize` REPEATS `entity_normalize`'s BODY (migration 033)
-- INSTEAD OF CALLING IT. 033 declared exactly this rule — lowercase, strip
-- everything that is not `[:alnum:]` — for the entity vocabulary, and calling it
-- would be the obvious way to keep one definition. It is not available:
-- `schema_init.sql` is REGENERATED from a live database by introspection, and
-- nothing in that generator orders one function before another, so on a fresh
-- install a wrapper could be created before the function it calls and fail at
-- parse time — a defect visible only on the one path nobody re-inspects. So the
-- body is repeated and the two are pinned together LOUDLY instead: the second
-- self-check below asserts `axis_normalize` and `entity_normalize` agree on
-- every fixture, and fails the migration if a future edit moves one of them.
-- The separate name is right regardless — an axis key and an entity key are
-- different concepts that share a rule today, and this is where a divergence
-- would get written down rather than discovered.
--
-- ⚠ `[:alnum:]` IS LOCALE-DEPENDENT, exactly as 033 records: under a UTF-8
-- database with a non-`C` collation, accented and non-Latin letters survive
-- normalization; under `C` it behaves like `[a-zA-Z0-9]`. On a `C`-locale
-- deployment the SQL key and the Python key DISAGREE for any non-ASCII name —
-- which is why the self-check below runs a fixture containing accented Latin
-- and Greek letters and RAISES rather than warns. A deployment that cannot pass
-- it must not build indexes on a key its gateway does not share.
--
-- ⛔ AND WHY THERE IS NO UNIQUE INDEX ON `aliases (axis_normalize(name))`.
-- `aliases` is a shared string-intern table: migration 024 states, in the
-- table's own comment, that "the same spelling can legitimately alias on more
-- than one axis — a word that names a project here can name a section of a
-- different project there". A global key-unique index would forbid that by
-- construction — not because the data collides, but because the DESIGN allows
-- what the index would refuse. What actually has to be true is narrower, and it
-- is what the by-key resolution added in this release depends on: within one
-- axis and one scope, a key must never resolve to two different canonicals.
-- That is a cross-table rule no functional index can see, so it is asserted
-- here at apply time and reported loudly if this deployment violates it.
--
-- MEASURED on the live deployment 2026-08-25, BEFORE this migration: 38
-- projects, 18 active project aliases, 0 key collisions; `project_domains` 0
-- collisions within any project; `domain_aliases` 0 rows. So every statement
-- below is expected to be a no-op on that data — and if it is not, that is the
-- news, which is why nothing here is written to skip quietly.
--
-- ⚠ THE INDEX BUILDS ARE THE MEASUREMENT ON ANY OTHER DEPLOYMENT. A collision
-- makes `CREATE UNIQUE INDEX` fail and the whole migration roll back, naming
-- the duplicated key. That is the intended outcome: two registered names
-- sharing a key is a data question for the operator (which one is the project?
-- what happens to the records under the other?), never something a migration
-- may answer by picking one.

BEGIN;

-- ─── axis_normalize() — THE axis key, as SQL ─────────────────────────────────
--
-- IMMUTABLE because a functional index is built on it and must give the same
-- answer for the same input forever. STRICT so a NULL name keys to NULL and is
-- simply not indexed, rather than colliding with every other NULL.
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
                'alphanumeric. Do NOT index a key the gateway does not share.',
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

-- ─── The new invariant, structurally: two registered names never share a key ──
--
-- Projects are global; a section is identified WITHIN its project, so its key is
-- unique per (project_id, key) and nowhere wider — two projects may both have a
-- `graph-quality` section and they are different sections.
CREATE UNIQUE INDEX IF NOT EXISTS projects_axis_key_uniq
    ON projects (axis_normalize(name));

CREATE UNIQUE INDEX IF NOT EXISTS project_domains_axis_key_uniq
    ON project_domains (project_id, axis_normalize(name));

-- ─── The alias ambiguity check (no index can express it) ─────────────────────
--
-- The by-key resolution step answers "what does this spelling mean?" from the
-- registry first and the ACTIVE aliases second. That is only well-defined while
-- a key has ONE answer per axis and scope. Three ways it could not:
--
--   * two active project aliases sharing a key but pointing at different
--     projects;
--   * an active project alias keying the same as a REGISTERED project it does
--     not point at (the key-level form of 024's A1 disjointness rule, which the
--     existing trigger enforces only on exact strings);
--   * the same two shapes among one project's active domain aliases.
--
-- Each is reported with the key and the count, never with a repair: which
-- spelling wins is a data judgement with records hanging off it.
DO $$
DECLARE
    offender text;
    n        bigint;
BEGIN
    SELECT axis_normalize(a.name), count(DISTINCT pa.project_id)
      INTO offender, n
      FROM project_aliases pa
      JOIN aliases a ON a.id = pa.alias_id
     WHERE pa.active
     GROUP BY axis_normalize(a.name)
    HAVING count(DISTINCT pa.project_id) > 1
     LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'active project aliases normalizing to % point at % different '
            'projects — by-key resolution would answer by luck. Retire all but '
            'one before applying this migration.', offender, n;
    END IF;

    SELECT axis_normalize(a.name)
      INTO offender
      FROM project_aliases pa
      JOIN aliases a  ON a.id = pa.alias_id
      JOIN projects p  ON axis_normalize(p.name) = axis_normalize(a.name)
     WHERE pa.active AND p.id <> pa.project_id
     LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'an active project alias normalizing to % keys the same as a '
            'REGISTERED project it does not point at — an alias and a canonical '
            'name must never be the same name (024/A1), and that rule holds on '
            'the key, not only on the exact string.', offender;
    END IF;

    SELECT axis_normalize(a.name), count(DISTINCT da.domain_id)
      INTO offender, n
      FROM domain_aliases da
      JOIN aliases a ON a.id = da.alias_id
     WHERE da.active
     GROUP BY da.project_id, axis_normalize(a.name)
    HAVING count(DISTINCT da.domain_id) > 1
     LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'active domain aliases normalizing to % point at % different '
            'sections of one project — by-key resolution would answer by luck.',
            offender, n;
    END IF;

    SELECT axis_normalize(a.name)
      INTO offender
      FROM domain_aliases da
      JOIN aliases a ON a.id = da.alias_id
      JOIN project_domains d
        ON d.project_id = da.project_id
       AND axis_normalize(d.name) = axis_normalize(a.name)
     WHERE da.active AND d.id <> da.domain_id
     LIMIT 1;
    IF offender IS NOT NULL THEN
        RAISE EXCEPTION
            'an active domain alias normalizing to % keys the same as a '
            'REGISTERED section of the same project that it does not point at.',
            offender;
    END IF;
END;
$$;

COMMIT;
