-- Migration 024: an alias remembers a project rename so the old spelling keeps
-- resolving instead of coming back as an unknown name. The string is stored
-- once; project_aliases is the mapping. domain_aliases (028) uses the same
-- shape because a domain is (project, domain), not a name on its own.
-- Aliasing general_discussion would give the parked sentinel a second spelling.

CREATE TABLE IF NOT EXISTS aliases (
    id         bigserial PRIMARY KEY,
    -- One spelling may alias on more than one axis, so neither junction owns the string.
    name       text        NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT aliases_sentinel_reserved CHECK (name <> 'general_discussion'),
    CONSTRAINT aliases_not_blank CHECK (btrim(name) <> '')
);

CREATE TABLE IF NOT EXISTS project_aliases (
    id           bigserial PRIMARY KEY,
    alias_id     bigint      NOT NULL REFERENCES aliases (id),
    project      text        NOT NULL REFERENCES projects (name),
    -- Superseded, never deleted: the inactive row is why the name resolves.
    active       boolean     NOT NULL DEFAULT true,
    reason       text,
    created_by   text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,

    -- An inactive row with no end time makes the history unreadable.
    CONSTRAINT project_aliases_superseded_consistent
        CHECK ((active AND superseded_at IS NULL)
               OR (NOT active AND superseded_at IS NOT NULL))
);

-- Two active rows for one alias would make resolution whichever row came back.
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_aliases_one_active
    ON project_aliases (alias_id) WHERE active;

CREATE INDEX IF NOT EXISTS idx_project_aliases_project
    ON project_aliases (project);

-- A string that is both a project and an alias has two answers. A CHECK cannot
-- see both tables, so the trigger refuses either write.
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
