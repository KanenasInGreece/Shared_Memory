-- 033 — the entity vocabulary: canonical + alias, seeded from entity_registry.
--
-- WHAT WAS WRONG. `entity_registry` (migration 030) is a flat set of exact
-- strings — a lookup gate, but with no notion that two spellings name the same
-- thing. "Kubernetes" and "kubernetes" register as distinct, unrelated names,
-- and a genuinely different spelling like "K8s" has nowhere to point AT at
-- all. That is the same defect the project and domain axes had before their
-- alias junctions (024, 028) — an unregistered value has nowhere to resolve
-- TO, and a retired or off-canon spelling stays a stranger forever.
--
-- WHAT THIS DOES. Builds the same canonical + alias shape those axes use,
-- for entities:
--   1. `entity_normalize(text)` — ONE normalization definition (lowercase,
--      strip everything that is not alphanumeric) that this migration's seed
--      and every future reader/writer must share. Declared IMMUTABLE so it
--      can back a trigger-maintained lookup column.
--   2. `entity_vocabulary` — the canonical identity. `id` is what a future
--      ingress gate and the graph point at; `name` is the verbatim canonical
--      spelling; `normalized_key` is unique, so two canonicals can never
--      collide once normalized.
--   3. `entity_vocab_aliases` — every OTHER spelling the operator asserts for
--      a canonical — SEMANTIC, not normalization-derived (decision:1380,
--      Option B): "K8s" is a legitimate alias of "Kubernetes" despite
--      normalizing to a completely different key. Mirrors `project_aliases`'
--      junction shape: the alias STRING is kept verbatim and is what must be
--      unique, and the FK points at the canonical's IDENTITY (`entity_id`),
--      never at its name. What the schema still guarantees is
--      LOOKUP-UNAMBIGUITY: a normalized value must never resolve to two
--      different identities — see the triggers below.
--   4. SEEDING, pure SQL, deriving the vocabulary from THIS install's own
--      `entity_registry` + `technical_docs` — no entity name is ever written
--      into this file, so it stays portable across every deployment. On a
--      fresh install `entity_registry` is empty and the seed inserts nothing.
--
-- ⛔ THIS MIGRATION ADDS NO WRITER AND CHANGES NO RUNTIME BEHAVIOR. Nothing in
-- the coordinator, the ingress gate, or REM consults these tables yet — that
-- is a LATER unit's work. This migration only creates the vocabulary and
-- seeds it from whatever `entity_registry` holds ON THIS INSTALL — a fresh
-- install has an empty registry and gets an empty, correctly-shaped
-- vocabulary; nothing here assumes any curation has already happened.
--
-- ⛔ THIS MIGRATION DOES NOT TOUCH `entity_registry`. It remains the ingress
-- log exactly as migration 030 left it; what happens to it once this
-- vocabulary exists is a separate, later question.
--
-- THE CANONICAL-PICK RULE (per normalized-key group in entity_registry — i.e.
-- spellings that normalize the SAME way, which is as far as this seed's
-- evidence reaches; a semantic alias like "K8s" is an operator judgement the
-- seed has no basis to make from `entity_registry` alone and does not
-- attempt):
--   1. the spelling with the most `technical_docs` FACT records whose
--      `metadata->'entities'` array contains that EXACT string, highest wins;
--   2. tie → earliest `entity_registry.created_at`;
--   3. tie → shortest `name`;
--   4. tie → lexicographically first `name`.
-- `entity_registry.name` is itself a PRIMARY KEY, so step 4 always resolves
-- the ordering — the pick is fully deterministic. Every OTHER spelling in the
-- group becomes an alias of the pick. A registry name that normalizes to the
-- EMPTY string (every character stripped) cannot be gated at all — the seed
-- FILTERS it out rather than letting it reach the vocabulary; see SEEDING.
--
-- Idempotent: `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` throughout. Safe to
-- re-run — a re-run seeds only names/aliases that were not already recorded.
-- (apply.py's ledger means this file only ever runs once in the ordinary
-- path; idempotency is the house style regardless, and is what makes a
-- manual re-run after a partial failure safe.)
--
-- ⚠ NAMES APPEAR VERBATIM IN THIS FILE'S RAISE EXCEPTION MESSAGES, BY DESIGN
-- (security review finding F-10, ruled ACCEPTED under decision:1380). Entity
-- names here are operator-curated data inside the trust boundary — the same
-- boundary `assert_alias_namespaces_disjoint` (migration 024) already puts
-- project/alias names in exception text across — never untrusted tenant
-- input, so there is nothing here for an exception message to leak.

BEGIN;

-- ─── entity_normalize() — THE one normalization definition ───────────────────
--
-- CONTRACT: lowercase, then strip every character that is not alphanumeric
-- per the POSIX `[:alnum:]` class. ⚠ `[:alnum:]` is LOCALE-DEPENDENT: under a
-- UTF8 database with a non-`C` collation it treats accented and non-Latin
-- letters as alphanumeric (so they survive normalization); under the `C`
-- locale it behaves exactly like `[a-zA-Z0-9]`. This install's actual locale
-- is a live-database question this migration cannot answer — verify it
-- against the running system, not by reading this comment. This is the ONLY
-- normalization this vocabulary recognises. The seed below calls it to build
-- `normalized_key`/`normalized_alias`; the ingress gate that consults this
-- vocabulary in a later unit MUST call this same function rather than
-- re-implementing the rule, or the two could disagree about whether two
-- spellings resolve to the same identity. IMMUTABLE because it backs
-- trigger-maintained lookup columns and must give the same answer for the
-- same input forever.
CREATE OR REPLACE FUNCTION entity_normalize(raw_name text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(lower(raw_name), '[^[:alnum:]]', '', 'g');
$$;

-- ─── entity_vocabulary — the canonical identity ───────────────────────────────
--
-- `normalized_key` is maintained by trigger rather than as a generated/stored
-- column: this repository's `generate_schema_init.py` introspects columns via
-- `information_schema.columns`, which does not report a generation
-- expression, so a `GENERATED ALWAYS AS (...) STORED` column would silently
-- render as a bare, unpopulated column in a regenerated `schema_init.sql` —
-- the same class of silent drop this schema's CHECK/FK/IDENTITY history
-- warns about. A BEFORE INSERT/UPDATE trigger uses the function-and-trigger
-- path the generator DOES faithfully introspect.
--
-- `name`/`registered_by` are TEXT, not VARCHAR(n): the sources this seeds
-- from (`entity_registry.name`, `entity_registry.registered_by`) are
-- themselves unbounded TEXT columns, and a length cap here would make a
-- LEGAL registry row abort the migration instead of registering it.
CREATE TABLE IF NOT EXISTS entity_vocabulary (
    id             bigint      GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name           TEXT        NOT NULL,
    normalized_key TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    registered_by  TEXT        NOT NULL DEFAULT 'system',

    CONSTRAINT entity_vocabulary_not_blank CHECK (btrim(name) <> ''),
    CONSTRAINT entity_vocabulary_normalized_key_not_empty CHECK (normalized_key <> ''),
    CONSTRAINT entity_vocabulary_normalized_key_unique UNIQUE (normalized_key)
);

CREATE INDEX IF NOT EXISTS idx_entity_vocabulary_created_at
    ON entity_vocabulary (created_at);

-- ─── entity_vocab_aliases — every spelling the operator asserts ─────────────
--
-- SEMANTIC aliasing (decision:1380, Option B): an alias is ANY spelling
-- asserted for a canonical, not merely a case/punctuation variant of it.
-- Mirrors `project_aliases`' junction shape: the alias STRING is kept
-- verbatim and is what must be unique (a verbatim spelling maps to exactly
-- one identity), and the FK points at the canonical's IDENTITY (`entity_id`),
-- never at its name.
--
-- `normalized_alias` carries a PLAIN index, not a unique one: under Option B
-- two distinct verbatim aliases of the SAME entity can legitimately
-- normalize to the same key (e.g. "K8s" and "k8s,"), and the schema no
-- longer forces every alias of an entity to share one normalized form. What
-- must still never happen — a normalized value resolving to two DIFFERENT
-- identities — is LOOKUP-UNAMBIGUITY, enforced by the triggers below; the
-- index here exists for lookup speed only.
CREATE TABLE IF NOT EXISTS entity_vocab_aliases (
    id               bigint      GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    entity_id        bigint      NOT NULL REFERENCES entity_vocabulary (id) ON DELETE CASCADE,
    alias            TEXT        NOT NULL,
    normalized_alias TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by       TEXT        NOT NULL DEFAULT 'system',

    CONSTRAINT entity_vocab_aliases_not_blank CHECK (btrim(alias) <> ''),
    CONSTRAINT entity_vocab_aliases_normalized_alias_not_empty CHECK (normalized_alias <> ''),
    CONSTRAINT entity_vocab_aliases_alias_unique UNIQUE (alias)
);

CREATE INDEX IF NOT EXISTS idx_entity_vocab_aliases_entity_id
    ON entity_vocab_aliases (entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_vocab_aliases_normalized_alias
    ON entity_vocab_aliases (normalized_alias);

-- ─── LOOKUP-UNAMBIGUITY — a normalized value resolves to ONE identity ───────
--
-- decision:1380 (Option B): aliasing is semantic, so `normalized_key` and
-- `normalized_alias` are no longer required to agree with each other — what
-- the schema still guarantees is that no normalized value resolves two
-- different ways. Two rules, enforced because a plain UNIQUE constraint
-- cannot see across these two tables:
--
--   A) Inserting/updating a CANONICAL is refused if its `normalized_key` is
--      already claimed by an EXISTING ALIAS whose own parent resolves to a
--      DIFFERENT `normalized_key`. If that alias's parent already resolves
--      to the SAME key (re-registering the same canonical, or a case variant
--      that happens to land on an existing alias's normalized form), the
--      insert is let through and `ON CONFLICT (normalized_key) DO NOTHING`
--      arbitrates — which is what makes a re-run a true no-op.
--   B) Inserting/updating an ALIAS is refused if its normalized value is
--      already claimed by a DIFFERENT canonical's `normalized_key`, or by
--      another alias row pointing at a DIFFERENT entity. Equal to its OWN
--      parent's key is fine — that is the ordinary case/punctuation variant,
--      not a collision.
--
-- ⚠ Both checks compare by KEY RESOLUTION (normalized values), never by row
-- id as the enforcement mechanism. An earlier version of this trigger
-- compared `alias.entity_id <> NEW.id` on the canonical side — dead code on
-- INSERT, because a fresh IDENTITY value is assigned before BEFORE ROW
-- triggers fire, so no pre-existing row could already reference an id that
-- did not exist a moment ago. Comparing normalized values is what actually
-- catches a collision. (The alias-side trigger below still compares
-- `other.id <> NEW.id`, but only to exclude the row's OWN prior state on
-- UPDATE — not as the ambiguity check itself, which is the entity_id
-- comparison; see the note there.)
--
-- ⚠ WHAT SQL CANNOT ENFORCE HERE: these are BEFORE ROW triggers reading a
-- sibling table under the default READ COMMITTED isolation the coordinator
-- runs at, not a single atomic constraint — two concurrent transactions can
-- each pass the check before either commits, landing a genuine collision.
-- Serializing writes to this vocabulary (an advisory lock, or SERIALIZABLE
-- isolation on the write path) is owed from the ingress gate that becomes the
-- vocabulary's write path in the later unit — recorded as a standing
-- condition of decision:1380, not resolved here.
CREATE OR REPLACE FUNCTION entity_vocabulary_before_write()
RETURNS trigger AS $$
BEGIN
    NEW.normalized_key := entity_normalize(NEW.name);

    IF NEW.normalized_key = '' THEN
        RAISE EXCEPTION
            'entity "%" normalizes to the empty string — every character was '
            'stripped, so it cannot be registered as a canonical spelling',
            NEW.name;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM entity_vocab_aliases a
          JOIN entity_vocabulary parent ON parent.id = a.entity_id
         WHERE a.normalized_alias = NEW.normalized_key
           AND parent.normalized_key <> NEW.normalized_key
    ) THEN
        RAISE EXCEPTION
            'entity "%" normalizes to "%", which is already registered as an '
            'alias resolving to a DIFFERENT identity — a normalized value '
            'must never resolve to two different identities',
            NEW.name, NEW.normalized_key;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entity_vocabulary_before_write ON entity_vocabulary;
CREATE TRIGGER trg_entity_vocabulary_before_write
    BEFORE INSERT OR UPDATE ON entity_vocabulary
    FOR EACH ROW EXECUTE FUNCTION entity_vocabulary_before_write();

CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()
RETURNS trigger AS $$
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

    -- Refused if this normalized value is already a DIFFERENT canonical's
    -- identity. Equal to its OWN parent's key is fine — the ordinary case.
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

    -- Refused if another alias ROW already claims this normalized value for
    -- a DIFFERENT entity. `other.id <> NEW.id` excludes this row's own prior
    -- state on UPDATE (e.g. re-pointing entity_id): on INSERT it is a no-op,
    -- since no existing row can yet carry the id a fresh IDENTITY just
    -- assigned — it is NOT the ambiguity check itself, which is the
    -- `entity_id` comparison.
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
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_entity_vocab_aliases_before_write ON entity_vocab_aliases;
CREATE TRIGGER trg_entity_vocab_aliases_before_write
    BEFORE INSERT OR UPDATE ON entity_vocab_aliases
    FOR EACH ROW EXECUTE FUNCTION entity_vocab_aliases_before_write();

-- ─── SEEDING — pure SQL, no entity name ever written into this file ─────────
--
-- On a fresh install `entity_registry` is empty, `entity_vocab_seed_ranked`
-- computes zero rows, and both INSERTs below affect zero rows: a genuine
-- no-op, not a special case.
--
-- Per normalized-key group: the CANONICAL row is the one with the highest
-- exact-spelling occurrence count across `technical_docs` FACT records'
-- `metadata->'entities'` arrays, tied first by earliest
-- `entity_registry.created_at`, then shortest `name`, then lexicographic
-- `name` — `entity_registry.name` is a PRIMARY KEY, so the final tiebreak
-- always resolves and ROW_NUMBER()'s ordering is fully deterministic.
--
-- A registry name whose normalized key is the EMPTY string cannot be gated
-- at all — there is nothing left to look up by once every character is
-- stripped. The seed FILTERS such names out (`WHERE entity_normalize(r.name)
-- <> ''`) rather than letting them reach the triggers' RAISE. The row stays
-- exactly where it already was, untouched, in `entity_registry` — the seed
-- SKIPS an ungateable name, it never aborts the migration and never deletes
-- from the registry log.
CREATE TEMP TABLE entity_vocab_seed_ranked ON COMMIT DROP AS
WITH registry_norm AS (
    SELECT
        r.name,
        r.created_at,
        r.registered_by,
        entity_normalize(r.name) AS norm_key
    FROM entity_registry r
    WHERE entity_normalize(r.name) <> ''
),
occurrence_counts AS (
    SELECT
        rn.name,
        rn.norm_key,
        rn.created_at,
        rn.registered_by,
        (
            SELECT count(*)
              FROM technical_docs td
             WHERE (td.metadata->>'kind' IS NULL OR td.metadata->>'kind' = 'fact')
               AND td.metadata->'entities' IS NOT NULL
               AND jsonb_typeof(td.metadata->'entities') = 'array'
               AND td.metadata->'entities' ? rn.name
        ) AS occurrence_count
      FROM registry_norm rn
)
SELECT
    oc.name,
    oc.norm_key,
    oc.created_at,
    oc.registered_by,
    oc.occurrence_count,
    ROW_NUMBER() OVER (
        PARTITION BY oc.norm_key
        ORDER BY oc.occurrence_count DESC,
                 oc.created_at ASC,
                 length(oc.name) ASC,
                 oc.name ASC
    ) AS rnk
FROM occurrence_counts oc;

-- The canonical of each group: rnk = 1. Attribution is carried straight from
-- the entity_registry row's own `registered_by` — that column is NOT NULL on
-- entity_registry (migration 030), so there is no unreachable fallback
-- literal to reach for here.
INSERT INTO entity_vocabulary (name, registered_by)
SELECT sr.name, sr.registered_by
  FROM entity_vocab_seed_ranked sr
 WHERE sr.rnk = 1
ON CONFLICT (normalized_key) DO NOTHING;

-- Every other spelling in the group: an alias of the canonical that shares
-- its normalized key. Attribution likewise carried from entity_registry's
-- own `registered_by`. ON CONFLICT targets `alias` (R-2, decision:1380) —
-- the verbatim spelling is the unique key now, not its normalized form.
INSERT INTO entity_vocab_aliases (entity_id, alias, created_by)
SELECT v.id, sr.name, sr.registered_by
  FROM entity_vocab_seed_ranked sr
  JOIN entity_vocabulary v ON v.normalized_key = sr.norm_key
 WHERE sr.rnk > 1
ON CONFLICT (alias) DO NOTHING;

COMMIT;
