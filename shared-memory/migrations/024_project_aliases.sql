-- 024 — aliases: a rename is REMEMBERED, not merely applied.
--
-- Renaming a project used to be a one-shot rewrite. The tool moved every record
-- onto the new name and then forgot the old one, which produced two lasting
-- problems:
--
--   * THE OLD NAME CAME BACK. A folder on another machine still carries it, so
--     the next save from that machine recreates the variant the merge just
--     removed. The rewrite fixed history and did nothing about the source.
--   * THE DECISION WAS RE-ASKED FOREVER. With nothing recorded, the old name
--     reads as an unregistered stranger to every later review, so a judgement
--     made once returns to the operator's queue every time anybody looks.
--
-- An alias is the durable form of that judgement: it resolves the old name to
-- the current one at ingress, so a machine that cannot be reached — or a folder
-- nobody wants to rename — stops mattering.
--
-- WHY A JUNCTION AND NOT A FLAT COLUMN. Domains are PROJECT-LOCAL: a domain is
-- identified by (project, domain), not by a name on its own. A single flat
-- alias table cannot key both axes without one of them carrying nulls that mean
-- something. So the alias STRING is stored once, and a junction per axis carries
-- the mapping. `domain_aliases` lands with the domain registry — it needs a
-- table to reference — but the shape is fixed here so both axes resolve the same
-- way rather than growing two different answers.

CREATE TABLE IF NOT EXISTS aliases (
    id         bigserial PRIMARY KEY,
    -- The string itself, stored ONCE. The same spelling can legitimately alias
    -- on more than one axis — a word that names a project here can name a
    -- section of a different project there — so the string is not owned by
    -- either junction.
    name       text        NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),

    -- The sentinel is reserved everywhere, including here: aliasing it would
    -- give the parked-project marker a second spelling and quietly reintroduce
    -- the shared bucket the axis work exists to remove.
    CONSTRAINT aliases_sentinel_reserved CHECK (name <> 'general_discussion'),
    CONSTRAINT aliases_not_blank CHECK (btrim(name) <> '')
);

CREATE TABLE IF NOT EXISTS project_aliases (
    id           bigserial PRIMARY KEY,
    alias_id     bigint      NOT NULL REFERENCES aliases (id),
    project      text        NOT NULL REFERENCES projects (name),
    -- A4 — mappings are SUPERSEDED, never deleted. The rename history is the
    -- ledger: a later session asks the database why a name resolves the way it
    -- does instead of re-deriving it from the shape of the data.
    active       boolean     NOT NULL DEFAULT true,
    reason       text,
    created_by   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,

    -- An inactive row must say when it stopped applying, and an active one must
    -- not claim to have stopped. Without this the history is unreadable.
    CONSTRAINT project_aliases_superseded_consistent
        CHECK ((active AND superseded_at IS NULL)
               OR (NOT active AND superseded_at IS NOT NULL))
);

-- A2 — at most ONE ACTIVE mapping per alias, enforced by the database rather
-- than by discipline. Two active rows for one alias is not a conflict to
-- resolve at read time; it is a state that must never exist, because whichever
-- row a query happened to return would become the answer.
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_aliases_one_active
    ON project_aliases (alias_id) WHERE active;

CREATE INDEX IF NOT EXISTS idx_project_aliases_project
    ON project_aliases (project);

-- A1 — THE ALIAS AND CANONICAL NAMESPACES ARE DISJOINT. A string that is both a
-- registered project and an alias for a different one has two correct answers,
-- and resolution would pick by luck. Enforced as a trigger because it is a rule
-- ACROSS two tables, which no single CHECK can see.
CREATE OR REPLACE FUNCTION assert_alias_namespaces_disjoint()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'project_aliases' THEN
        IF NEW.active AND EXISTS (
            SELECT 1 FROM projects p
              JOIN aliases a ON a.id = NEW.alias_id
             WHERE p.name = a.name
        ) THEN
            RAISE EXCEPTION
                'alias % is also a registered project — an alias and a canonical '
                'name must never be the same string (A1)',
                (SELECT name FROM aliases WHERE id = NEW.alias_id);
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
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_project_aliases_disjoint ON project_aliases;
CREATE TRIGGER trg_project_aliases_disjoint
    BEFORE INSERT OR UPDATE ON project_aliases
    FOR EACH ROW EXECUTE FUNCTION assert_alias_namespaces_disjoint();

DROP TRIGGER IF EXISTS trg_projects_disjoint ON projects;
CREATE TRIGGER trg_projects_disjoint
    BEFORE INSERT OR UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION assert_alias_namespaces_disjoint();
