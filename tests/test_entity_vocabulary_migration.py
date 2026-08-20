"""Migration 033 — the entity vocabulary (canonical + alias), seeded from
`entity_registry`.

FIX ROUND (decision:1380, Option B — semantic aliasing) after the security
review's live-fire on throwaway DBs found 1 Critical + 5 Required against the
first version. This file's tests were updated for every fix in that round:
lookup-unambiguity trigger semantics replacing the old normalize-to-parent
rule, `UNIQUE (alias)` replacing `UNIQUE (normalized_alias)`, TEXT replacing
VARCHAR(n), the `[:alnum:]` POSIX class replacing `[a-z0-9]`, empty-normalized
refusal, the seed's empty-key filter, and registry-sourced attribution
replacing the unreachable COALESCE fallback.

No live database is used or required — everything here is a STATIC check over
the migration's own SQL text, or a pure-Python re-implementation of the
canonical-pick rule run against a synthetic fixture. That is a real limit, not
a formality: these tests cannot prove the SQL parses, that the triggers fire,
that the seed's window-function ordering actually produces what the rule
below describes, or that a fresh `schema_init.sql` install ends up equivalent
to a migrated one. `migrations/verify_schema_init.py` against a throwaway
database is what proves those, and this file cannot substitute for it.

Fixture names are deliberately generic ("AlphaBeta") — never a real project or
entity name from this corpus (`fact:1195`).
"""
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_MIGRATIONS = os.path.join(_ROOT, "shared-memory", "migrations")
_MIGRATION_033 = os.path.join(_MIGRATIONS, "033_entity_vocabulary.sql")
_SCHEMA_INIT = os.path.join(_MIGRATIONS, "schema_init.sql")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _sql() -> str:
    return _read(_MIGRATION_033)


def _code_only(text: str) -> str:
    """Strip `--` line comments. The migration's own prose (canonical-pick
    rule explanations, house-style headers) freely uses backticked SQL
    fragments containing stray single quotes — e.g. `` `metadata->'entities'` ``
    — which would otherwise be mistaken for real string literals or corrupt a
    FROM/JOIN table scan. Scanning CODE rather than the whole file is what
    keeps those checks meaningful instead of just matching this file's own
    documentation back to itself."""
    return "\n".join(
        line.split("--", 1)[0] for line in text.splitlines()
    )


def _block(sql: str, start_marker: str, end_marker: str = "\n);") -> str:
    """Text from `start_marker` up to (not including) the next `end_marker`."""
    start = sql.index(start_marker)
    rest = sql[start:]
    return rest[:rest.index(end_marker)]


# ── The file exists, in sequence, with no gap ────────────────────────────────

def test_033_exists_in_sequence_with_no_gap():
    files = sorted(f for f in os.listdir(_MIGRATIONS) if re.match(r"^\d{3}_.*\.sql$", f))
    assert "033_entity_vocabulary.sql" in files
    numbers = sorted(int(f[:3]) for f in files)
    assert numbers == list(range(min(numbers), max(numbers) + 1)), (
        "the migration chain has a gap or duplicate around 033")
    # 032 is the last migration this unit was built against — if a later
    # migration lands with a lower number than 033 the ordering assumption
    # (033 runs after everything currently in the repo) is wrong and needs a
    # human look, not a silent renumber.
    assert max(numbers) >= 33


# ── entity_normalize() — the one normalization definition (R-3) ─────────────

def test_entity_normalize_is_defined_immutable_and_uses_posix_alnum_class():
    """R-3: `[a-z0-9]` was ASCII-only and would strip every non-Latin letter.
    `[:alnum:]` is the POSIX class — locale-dependent, but the fix the review
    required. MUTATION-CHECKED: reverting this line to `[^a-z0-9]` makes this
    test fail (see report)."""
    sql = _sql()
    assert "CREATE OR REPLACE FUNCTION entity_normalize(" in sql
    fn_block = sql[sql.index("CREATE OR REPLACE FUNCTION entity_normalize("):]
    fn_block = fn_block[:fn_block.index("$$;") + 3]
    assert "IMMUTABLE" in fn_block
    assert "lower(" in fn_block
    assert "[^[:alnum:]]" in fn_block, (
        "entity_normalize must strip via the POSIX [:alnum:] class (R-3), "
        "not the ASCII-only [a-z0-9] the review flagged")
    assert "[^a-z0-9]" not in fn_block, (
        "the old ASCII-only class must be gone, not merely supplemented")


# ── Both tables present, with TEXT columns (R-4) and empty-key CHECKs (R-3) ─

def test_both_tables_are_created():
    sql = _sql()
    assert re.search(r"CREATE TABLE IF NOT EXISTS entity_vocabulary\s*\(", sql)
    assert re.search(r"CREATE TABLE IF NOT EXISTS entity_vocab_aliases\s*\(", sql)


def test_entity_vocabulary_columns_types_and_constraints():
    block = _block(_sql(), "CREATE TABLE IF NOT EXISTS entity_vocabulary (")
    for col in ("id", "name", "normalized_key", "created_at", "registered_by"):
        assert col in block, f"entity_vocabulary missing column {col!r}"
    assert "GENERATED BY DEFAULT AS IDENTITY" in block
    assert "entity_vocabulary_normalized_key_unique UNIQUE (normalized_key)" in block
    assert "entity_vocabulary_normalized_key_not_empty CHECK (normalized_key <> '')" in block
    # R-4: TEXT, never a length-capped VARCHAR — a legal registry row must
    # never abort the migration. MUTATION-CHECKED: reverting `name`'s column
    # type to VARCHAR(255) makes this test fail (see report).
    assert "VARCHAR" not in block, (
        "entity_vocabulary must use TEXT throughout (R-4) — a VARCHAR(n) cap "
        "would make a legal, longer entity_registry row abort the migration")


def test_entity_vocab_aliases_columns_fk_and_constraints():
    block = _block(_sql(), "CREATE TABLE IF NOT EXISTS entity_vocab_aliases (")
    for col in ("id", "entity_id", "alias", "normalized_alias", "created_at", "created_by"):
        assert col in block, f"entity_vocab_aliases missing column {col!r}"
    assert "REFERENCES entity_vocabulary (id) ON DELETE CASCADE" in block
    assert "entity_vocab_aliases_normalized_alias_not_empty CHECK (normalized_alias <> '')" in block
    assert "VARCHAR" not in block, "entity_vocab_aliases must use TEXT throughout (R-4)"


def test_alias_not_normalized_alias_is_the_unique_column_r2():
    """R-2: the review's Option B makes an alias SEMANTIC, so two aliases of
    the SAME entity can legitimately share a normalized form ("K8s" /
    "k8s,"). The verbatim `alias` string is what must be unique;
    `normalized_alias` is a plain lookup index only.

    MUTATION-CHECKED: reverting `UNIQUE (alias)` back to
    `UNIQUE (normalized_alias)` makes this test fail (see report)."""
    block = _block(_sql(), "CREATE TABLE IF NOT EXISTS entity_vocab_aliases (")
    assert "entity_vocab_aliases_alias_unique UNIQUE (alias)" in block, (
        "the verbatim alias string must be the UNIQUE column (R-2)")
    assert "UNIQUE (normalized_alias)" not in block, (
        "normalized_alias must NOT be a unique constraint under Option B — "
        "two verbatim aliases of one entity may legitimately share a "
        "normalized form")
    sql = _sql()
    assert "CREATE INDEX IF NOT EXISTS idx_entity_vocab_aliases_normalized_alias" in sql, (
        "normalized_alias still needs a PLAIN index for lookup speed")


# ── Lookup-unambiguity triggers (Option B) ───────────────────────────────────

def test_the_cross_table_consistency_triggers_exist():
    """Eyes-only for correctness (a trigger firing correctly needs a live DB),
    but their PRESENCE and which function each calls is checkable statically."""
    sql = _sql()
    assert "CREATE TRIGGER trg_entity_vocabulary_before_write" in sql
    assert "EXECUTE FUNCTION entity_vocabulary_before_write" in sql
    assert "CREATE TRIGGER trg_entity_vocab_aliases_before_write" in sql
    assert "EXECUTE FUNCTION entity_vocab_aliases_before_write" in sql


def test_entity_vocabulary_trigger_refuses_empty_normalized_key():
    """R-3: a name that normalizes to '' has nothing left to look up by."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocabulary_before_write()", "$$ LANGUAGE plpgsql;")
    assert "NEW.normalized_key = ''" in fn
    assert "RAISE EXCEPTION" in fn


def test_entity_vocabulary_trigger_compares_by_key_resolution_not_row_id():
    """C-1 root fix: the canonical-side ambiguity check must compare
    `parent.normalized_key <> NEW.normalized_key` (KEY resolution) — never
    `<> NEW.id`, which the review proved is dead on INSERT (a fresh IDENTITY
    value cannot already be referenced by any existing row).

    MUTATION-CHECKED: reverting the WHERE clause back to
    `a.entity_id <> NEW.id` makes this test fail (see report)."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocabulary_before_write()", "$$ LANGUAGE plpgsql;")
    assert "parent.normalized_key <> NEW.normalized_key" in fn, (
        "Option B: the canonical trigger must refuse by comparing the "
        "alias's PARENT's normalized_key against NEW.normalized_key")
    assert "a.entity_id <> NEW.id" not in fn, (
        "the old, dead row-id comparison must be gone — the review proved a "
        "fresh IDENTITY value can never already match an existing row's id")
    # The join that reaches "the alias's own parent" must exist — without it
    # there is nothing to compare "by key resolution" against.
    assert re.search(r"JOIN\s+entity_vocabulary\s+parent\s+ON\s+parent\.id\s*=\s*a\.entity_id", fn)


def test_entity_vocabulary_trigger_allows_same_key_parent_through_for_on_conflict():
    """The 'same-key parent → allow through' half of Option B: the refusal
    condition must require the parent's key to DIFFER, not merely exist,
    from NEW.normalized_key — otherwise a legitimate re-run (which relies on
    ON CONFLICT (normalized_key) DO NOTHING to arbitrate) would be refused
    outright instead of silently no-op'ing."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocabulary_before_write()", "$$ LANGUAGE plpgsql;")
    # The refusal's WHERE clause must include the inequality, not a bare
    # existence check against normalized_key alone.
    where = fn[fn.index("WHERE a.normalized_alias"):fn.index(") THEN")]
    assert "<>" in where


def test_entity_vocab_aliases_trigger_refuses_empty_normalized_alias():
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()", "$$ LANGUAGE plpgsql;")
    assert "NEW.normalized_alias = ''" in fn
    assert "RAISE EXCEPTION" in fn


def test_entity_vocab_aliases_trigger_no_longer_requires_matching_its_parent():
    """C-1 root fix + Option B: the old rule — an alias MUST normalize to
    exactly its parent's key — is gone. "K8s" (parent "Kubernetes") must be
    representable, and the old trigger would have refused it outright."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()", "$$ LANGUAGE plpgsql;")
    assert "v_parent_key" not in fn, (
        "the Option-A 'must equal parent's key' variable must be gone")
    assert "NEW.normalized_alias <> v_parent_key" not in fn, (
        "the old normalize-to-parent refusal must be gone — Option B makes "
        "aliasing semantic")


def test_entity_vocab_aliases_trigger_refuses_a_different_canonicals_identity():
    """R-1 item 1, alias side, rule one: refused if normalized_alias already
    IS a different canonical's normalized_key."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()", "$$ LANGUAGE plpgsql;")
    assert "v.normalized_key = NEW.normalized_alias" in fn
    assert "v.id <> NEW.entity_id" in fn


def test_entity_vocab_aliases_trigger_refuses_a_different_entitys_alias():
    """R-1 item 1, alias side, rule two: refused if another alias ROW already
    claims this normalized value for a DIFFERENT entity_id (never by id)."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()", "$$ LANGUAGE plpgsql;")
    assert "other.normalized_alias = NEW.normalized_alias" in fn
    assert "other.entity_id <> NEW.entity_id" in fn


def test_entity_vocab_aliases_trigger_still_checks_parent_exists():
    """'Parent-existence check stays' — R-1 item 1's explicit carry-over."""
    fn = _block(_sql(), "CREATE OR REPLACE FUNCTION entity_vocab_aliases_before_write()", "$$ LANGUAGE plpgsql;")
    assert "entity_vocab_aliases.entity_id % does not reference a known" in fn


# ── F-10 (ruled ACCEPTED): names in RAISE messages are intentional ──────────

def test_names_in_raise_messages_are_documented_as_intentional_f10():
    sql = _sql()
    assert "trust boundary" in sql, (
        "F-10 (ruled ACCEPTED) requires a comment stating that names in "
        "exception messages are intentional — operator-curated data inside "
        "the trust boundary, not untrusted tenant input")


# ── Idempotency markers ───────────────────────────────────────────────────────

def test_idempotency_markers_present():
    sql = _sql()
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "DROP TRIGGER IF EXISTS" in sql
    assert sql.strip().startswith("--")
    assert "BEGIN;" in sql and sql.rstrip().endswith("COMMIT;")


# ── entity_registry is read, never written ───────────────────────────────────

def test_entity_registry_is_never_altered_or_dropped():
    sql = _sql()
    assert not re.search(r"ALTER TABLE\s+entity_registry", sql, re.I)
    assert not re.search(r"DROP TABLE\s+(?:IF EXISTS\s+)?entity_registry", sql, re.I)
    assert not re.search(r"(?:INSERT INTO|UPDATE|DELETE FROM)\s+entity_registry\b", sql, re.I)
    assert "FROM entity_registry" in sql, "seeding must read entity_registry"


# ── The seed's shape: INSERT...SELECT, sourced only from entity_registry /
#    technical_docs — never a literal row of names ──────────────────────────

def _seed_code() -> str:
    seed = _code_only(_sql())
    return seed[seed.index("CREATE TEMP TABLE entity_vocab_seed_ranked"):]


def test_seeding_is_insert_select_never_insert_values():
    seed = _seed_code()
    inserts = re.findall(r"INSERT INTO\s+entity_vocab(?:ulary|_aliases)?\s*\([^)]*\)\s*(SELECT|VALUES)",
                          seed, re.I)
    assert inserts, "expected at least one INSERT INTO entity_vocabulary/entity_vocab_aliases in the seed"
    assert all(kw.upper() == "SELECT" for kw in inserts), (
        "seeding must be INSERT ... SELECT (derived from live data), never "
        "INSERT ... VALUES (a literal list)")


def test_seed_source_is_only_entity_registry_and_technical_docs():
    seed = _seed_code()
    from_tables = set(re.findall(r"FROM\s+(\w+)", seed, re.I))
    join_tables = set(re.findall(r"JOIN\s+(\w+)", seed, re.I))
    referenced = from_tables | join_tables
    # entity_vocab_seed_ranked and entity_vocabulary/entity_vocab_aliases are
    # this migration's own temp/target tables, not external name sources.
    allowed = {"entity_registry", "technical_docs", "entity_vocab_seed_ranked",
               "entity_vocabulary", "occurrence_counts", "registry_norm"}
    assert referenced <= allowed, (
        f"seed reads from an unexpected source: {referenced - allowed} — the "
        f"vocabulary must be derived only from entity_registry/technical_docs")


def test_seed_filters_out_names_that_normalize_to_empty_r3():
    """R-3: the seed must SKIP an ungateable name, never abort, never delete
    from entity_registry. MUTATION-CHECKED: removing the WHERE filter makes
    this test fail (see report)."""
    seed = _seed_code()
    registry_norm = seed[seed.index("registry_norm AS ("):seed.index("occurrence_counts AS (")]
    assert "entity_normalize(r.name) <> ''" in registry_norm, (
        "registry_norm must filter WHERE entity_normalize(r.name) <> '' — "
        "an ungateable name must be skipped, not passed through to the "
        "triggers' RAISE")


def test_seed_attribution_comes_from_the_registry_row_not_a_literal_r7():
    """R-7: both canonical AND alias inserts must carry entity_registry's own
    registered_by — the unreachable COALESCE(..., 'migration_033_seed')
    fallback must be gone entirely."""
    seed = _seed_code()
    assert "COALESCE" not in seed, (
        "R-7: drop the unreachable COALESCE fallback — entity_registry."
        "registered_by is NOT NULL, so there is nothing to fall back to")
    assert "migration_033_seed" not in seed, (
        "R-7: the seed-invented attribution literal must be gone — both "
        "inserts carry the registry row's OWN registered_by")
    assert re.search(r"SELECT\s+sr\.name,\s*sr\.registered_by", seed), (
        "the canonical insert must SELECT sr.registered_by directly"
    )
    assert re.search(r"SELECT\s+v\.id,\s*sr\.name,\s*sr\.registered_by", seed), (
        "the alias insert must SELECT sr.registered_by directly"
    )


def test_seed_alias_insert_conflict_target_is_alias_not_normalized_alias_r2():
    """R-2: the seed's alias INSERT must arbitrate on the new UNIQUE column."""
    seed = _seed_code()
    alias_insert = seed[seed.index("INSERT INTO entity_vocab_aliases"):]
    assert "ON CONFLICT (alias) DO NOTHING" in alias_insert
    assert "ON CONFLICT (normalized_alias)" not in alias_insert


_ALLOWED_SEED_STRING_LITERALS = {
    "fact",                # metadata->>'kind' value it filters FOR
    "kind",                # JSONB key
    "entities",             # JSONB key
    "array",                # jsonb_typeof(...) = 'array' — a JSON type name
    "",                     # entity_normalize(r.name) <> '' — the R-3 empty-key filter
}


def test_no_string_literal_in_the_seed_looks_like_a_seeded_entity_name():
    """Scoped to CODE in the SEEDING block (comments stripped): RAISE
    EXCEPTION messages elsewhere, and this migration's own prose — which
    freely backtick-quotes SQL fragments containing stray single quotes, e.g.
    `` `metadata->'entities'` `` in the canonical-pick-rule comment — are not
    candidate entity names and would make a raw-text scan fail on the
    migration's own documentation. Every single-quoted string literal
    actually reachable by the seed's INSERTs is one of a small, known,
    structural set (JSONB keys) — never a name that could only have come
    from this install's own corpus, and never (post R-7) a seed-invented
    attribution literal either. Portability (`fact:1195`) depends on this
    staying true for every future edit to this file, not just the first
    version."""
    seed = _seed_code()
    literals = re.findall(r"'((?:[^'\\]|'')*)'", seed)
    assert literals, "expected to find string literals in the seed block"
    unexpected = [lit for lit in literals if lit not in _ALLOWED_SEED_STRING_LITERALS]
    assert not unexpected, (
        f"unexpected string literal(s) in migration 033's seed — a seed must "
        f"never hardcode a name: {unexpected}")


# ── The canonical-pick rule, reference-implemented in Python ────────────────
#
# This is the one invariant that needs SEMANTICS, not just text-matching. We
# cannot run the SQL, so we re-implement the documented rule in pure Python
# against a synthetic fixture and assert it produces the outcome the rule's
# own English description promises. This does NOT prove the SQL implements
# the same rule correctly — only that the rule, AS DOCUMENTED, is internally
# coherent and deterministic. Only `verify_schema_init.py` / a live apply can
# prove the SQL itself. (This rule is UNCHANGED by the fix round — it governs
# which spelling within a normalized-key GROUP becomes canonical, which R-1's
# semantic-alias fix does not touch.)

def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _pick_canonical(rows):
    """rows: list of dicts with name, created_at (int, lower=earlier),
    occurrence_count. Mirrors migration 033's ORDER BY:
    occurrence_count DESC, created_at ASC, length(name) ASC, name ASC.
    """
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_normalize(r["name"]), []).append(r)
    picks = {}
    for key, members in groups.items():
        ranked = sorted(
            members,
            key=lambda r: (-r["occurrence_count"], r["created_at"], len(r["name"]), r["name"]),
        )
        picks[key] = ranked[0]["name"]
    return picks


def test_canonical_pick_prefers_highest_occurrence_count():
    rows = [
        {"name": "AlphaBeta", "created_at": 3, "occurrence_count": 1},
        {"name": "alpha-beta", "created_at": 1, "occurrence_count": 5},
        {"name": "alpha_beta", "created_at": 2, "occurrence_count": 2},
    ]
    picks = _pick_canonical(rows)
    assert picks[_normalize("AlphaBeta")] == "alpha-beta"


def test_canonical_pick_tie_breaks_on_earliest_created_at():
    rows = [
        {"name": "GammaDelta", "created_at": 5, "occurrence_count": 3},
        {"name": "gamma-delta", "created_at": 1, "occurrence_count": 3},
    ]
    picks = _pick_canonical(rows)
    assert picks[_normalize("GammaDelta")] == "gamma-delta"


def test_canonical_pick_tie_breaks_on_shortest_name_then_lexicographic():
    # All three normalize to the same key ("epsilonzeta") — they differ only
    # by punctuation the normalizer strips, so occurrence_count and
    # created_at tie and the SHORTEST verbatim spelling must win.
    rows = [
        {"name": "Epsilon--Zeta", "created_at": 1, "occurrence_count": 0},
        {"name": "Epsilon-Zeta", "created_at": 1, "occurrence_count": 0},
        {"name": "EpsilonZeta", "created_at": 1, "occurrence_count": 0},
    ]
    assert {_normalize(r["name"]) for r in rows} == {"epsilonzeta"}, (
        "fixture bug: all three rows must land in ONE normalized group")
    picks = _pick_canonical(rows)
    assert picks["epsilonzeta"] == "EpsilonZeta"


def test_canonical_pick_is_fully_deterministic_final_tiebreak():
    # Same normalized key ("thetaiota"), same verbatim LENGTH, same count and
    # created_at — only case differs, so the final lexicographic tiebreak
    # must decide (uppercase sorts before lowercase in ASCII).
    rows = [
        {"name": "thetaiota", "created_at": 1, "occurrence_count": 0},
        {"name": "ThetaIota", "created_at": 1, "occurrence_count": 0},
    ]
    assert {_normalize(r["name"]) for r in rows} == {"thetaiota"}, (
        "fixture bug: both rows must land in ONE normalized group")
    picks = _pick_canonical(rows)
    assert picks["thetaiota"] == "ThetaIota", (
        "lexicographic tiebreak must be deterministic: 'ThetaIota' < "
        "'thetaiota' by ASCII ordering")


def test_migration_sql_orders_the_seed_by_the_documented_rule():
    """Confirms the SQL's ORDER BY clause names the same four keys, in the
    same order, that the reference implementation above uses — a textual
    check, not a semantic one, but it is what static analysis can offer here."""
    sql = _sql()
    order_by = sql[sql.index("ORDER BY oc.occurrence_count"):]
    order_by = order_by[:order_by.index(") AS rnk")]
    assert "occurrence_count DESC" in order_by
    assert "created_at ASC" in order_by
    assert "length(oc.name) ASC" in order_by
    assert "oc.name ASC" in order_by
    # DESC must come before the ASC tiebreaks in the clause, matching the
    # documented priority order.
    assert order_by.index("occurrence_count DESC") < order_by.index("created_at ASC") \
        < order_by.index("length(oc.name) ASC") < order_by.index("oc.name ASC")


# ── Empty-registry no-op (reasoned, not run) ─────────────────────────────────

def test_seed_is_reasoned_as_a_no_op_on_an_empty_registry():
    """No live DB is used here — this documents WHY the seed is a no-op on a
    fresh install, checked against the SQL's own structure, rather than
    proving it by running it (that needs `verify_schema_init.py`).

    `entity_vocab_seed_ranked` is built entirely FROM `entity_registry`
    (`registry_norm` selects `FROM entity_registry r` with no UNION/VALUES
    supplementing it — the R-3 empty-key filter only REMOVES rows, it cannot
    add any), so zero rows in `entity_registry` means zero rows in
    `registry_norm`, zero in `occurrence_counts` (built FROM `registry_norm`),
    and zero in the ranked temp table. Both seed INSERTs SELECT FROM that temp
    table, so both affect zero rows: a fresh install's `entity_vocabulary` and
    `entity_vocab_aliases` are created but left empty, not specially handled.
    """
    sql = _sql()
    registry_norm = sql[sql.index("registry_norm AS ("):sql.index("occurrence_counts AS (")]
    assert "FROM entity_registry r" in registry_norm
    assert "UNION" not in registry_norm and "VALUES" not in registry_norm.upper()


# ── verify_schema_init.py — R-5 instrument extension is present (not run) ──

def test_verify_schema_init_gained_a_column_type_comparison_r5():
    """R-5: the merger proves this against a live/throwaway DB at deploy —
    this only checks the instrument was actually extended, statically."""
    vsi = _read(os.path.join(_MIGRATIONS, "verify_schema_init.py"))
    assert "_column_types" in vsi
    assert "character_maximum_length" in vsi
    assert "COLUMN TYPE" in vsi
