"""Per-alternative vectors — the write path converges, and pending is never lost.

A decision's alternatives become one row and one vector each so that decisions
can be grouped by WHAT THEY CONSIDERED rather than by how their headline reads.
Two properties carry that, and both are invariants rather than behaviours:

  * RECONCILE, NEVER APPEND. A save can rewrite a record in place, and
    alternatives do get rewritten — the repair that rejoined 46 shredded
    decisions changed text on rows that already existed. The write path
    converges on the decision's own array: unchanged entries keep their
    vectors, changed ones go back to pending, retracted ones are removed.

  * A NULL EMBEDDING IS A PENDING STATE, NEVER A TERMINAL ONE. The rows are
    written in the save's transaction and embedded afterwards, so the only
    thing making that safe is that the pending set is a QUERY over committed
    rows. Nothing may write a row off, and no failure may leave one
    unreachable — a restart between the write and the embed has to be a
    non-event.

⚠ The SQL here is stubbed, as all SQL in this repo is: these tests pin the
statement contract, not the query's behaviour against Postgres. The migration
and the live run are what prove the query — see the green-suite rule.

No DB, no Neo4j, no embedder.
"""
import asyncio
import importlib.util
import os
import sys

import pytest

_SCRIPTS = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts")
)


def load_coordinator():
    if _SCRIPTS not in sys.path:
        sys.path.insert(0, _SCRIPTS)
    path = os.path.join(_SCRIPTS, "coordinator.py")
    spec = importlib.util.spec_from_file_location("coordinator", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coordinator"] = mod
    spec.loader.exec_module(mod)
    return mod


coord = load_coordinator()
MemoryCoordinator = coord.MemoryCoordinator


class FakeConn:
    """asyncpg-shaped connection that records every statement it is given."""

    def __init__(self, fetch_rows=None):
        self.executed = []
        self.fetched = []
        self._fetch_rows = fetch_rows if fetch_rows is not None else []

    async def execute(self, sql, *args):
        self.executed.append((" ".join(sql.split()), args))
        return "DELETE 3"

    async def fetch(self, sql, *args):
        self.fetched.append((" ".join(sql.split()), args))
        return self._fetch_rows

    def statements(self):
        return [s for s, _ in self.executed] + [s for s, _ in self.fetched]


def _reconcile(metadata, pg_id=42, fetch_rows=None):
    conn = FakeConn(fetch_rows=fetch_rows)
    stats = asyncio.run(
        MemoryCoordinator()._reconcile_decision_alternatives(conn, pg_id, metadata)
    )
    return conn, stats


def _decision(*alts):
    return {"type": "decision", "decision": {"alternatives": list(alts)}}


# ── What the metadata asks for (pure) ─────────────────────────────────────────

def test_a_record_that_is_not_a_decision_wants_no_rows():
    """Facts and retrospectives do not consider alternatives.

    This is also what makes the reconciler safe to call on every save: a
    non-decision converges to the empty set, which is a no-op for a record that
    never had rows and a cleanup for one that stopped being a decision.
    """
    assert MemoryCoordinator._desired_alternatives({"type": "fact"}) == []
    assert MemoryCoordinator._desired_alternatives({}) == []


def test_a_decision_with_no_alternatives_wants_no_rows():
    assert MemoryCoordinator._desired_alternatives({"type": "decision"}) == []
    assert MemoryCoordinator._desired_alternatives(
        {"type": "decision", "decision": {}}) == []
    # A malformed value is an empty set, not an exception: metadata is
    # client-supplied and a wrong type must not turn a save into a 500.
    assert MemoryCoordinator._desired_alternatives(
        {"type": "decision", "decision": {"alternatives": "a, b"}}) == []


def test_the_ordinal_is_the_position_in_the_decisions_own_array():
    got = MemoryCoordinator._desired_alternatives(_decision("first", "second", "third"))
    assert got == [(0, "first"), (1, "second"), (2, "third")]


def test_a_blank_entry_is_dropped_WITHOUT_renumbering_what_follows():
    """The ordinal has to keep pointing at the same entry of the source array.

    Renumbering would be tidier and wrong: the row is the durable handle on
    'the third option this decision weighed', and a later reader joining the
    row back to `metadata.decision.alternatives` would land on a different one.
    """
    got = MemoryCoordinator._desired_alternatives(_decision("keep", "   ", "also keep"))
    assert got == [(0, "keep"), (2, "also keep")]


def test_non_string_entries_are_dropped():
    got = MemoryCoordinator._desired_alternatives(_decision("ok", None, 7, {"a": 1}))
    assert got == [(0, "ok")]


def test_alternatives_are_stored_verbatim_including_their_punctuation():
    """v0.8.38's rule reaches this table too: one entry is one alternative.

    A comma or a semicolon inside an alternative is prose, not a delimiter, and
    nothing on this path may re-split what capture took care to keep whole.
    """
    text = "use explicit transactions (APOC not available, and auto-commit is the pattern)"
    got = MemoryCoordinator._desired_alternatives(_decision(text))
    assert got == [(0, text)]


# ── Reconcile, never append ───────────────────────────────────────────────────

def test_rows_outside_the_desired_set_are_deleted_for_this_decision_only():
    conn, _ = _reconcile(_decision("a", "b"), pg_id=42)
    delete = next(s for s in conn.statements() if s.startswith("DELETE"))
    assert "decision_pg_id = $1" in delete
    assert "NOT (ordinal = ANY($2::int[]))" in delete
    assert conn.executed[0][1] == (42, [0, 1])


def test_a_decision_that_retracted_every_alternative_loses_every_row():
    """The empty set must DELETE, not skip. `NOT (ordinal = ANY('{}'))` is true
    for every row, so the same statement that spares kept ordinals clears them
    all when nothing is kept — and no INSERT runs at all."""
    conn, stats = _reconcile(_decision())
    assert conn.executed[0][1] == (42, [])
    assert not [s for s in conn.statements() if s.startswith("INSERT")]
    assert stats["desired"] == 0


def test_unchanged_text_is_NOT_rewritten_and_so_is_never_re_embedded():
    """The invariant, enforced by the statement rather than by care.

    Without the `IS DISTINCT FROM` guard an idempotent re-save would reset five
    perfectly good rows to pending and re-embed them, which turns saving the
    same decision twice into embedding work proportional to how often it is
    re-saved. Delete the guard and this test dies.
    """
    conn, _ = _reconcile(_decision("a", "b"))
    upsert = next(s for s in conn.statements() if s.startswith("INSERT INTO decision_alternatives"))
    assert "ON CONFLICT (decision_pg_id, ordinal) DO UPDATE" in upsert
    assert "WHERE decision_alternatives.text IS DISTINCT FROM EXCLUDED.text" in upsert


def test_changed_text_goes_back_to_PENDING_rather_than_keeping_its_vector():
    """A changed alternative is a different alternative: the old vector
    describes text nobody wrote any more, so keeping it would put a confident
    embedding of deleted prose into the similarity index."""
    conn, _ = _reconcile(_decision("a"))
    upsert = next(s for s in conn.statements() if s.startswith("INSERT INTO decision_alternatives"))
    assert "embedding = NULL" in upsert
    assert "embedded_at = NULL" in upsert
    assert "attempts = 0" in upsert
    assert "last_error = NULL" in upsert
    assert "next_attempt_at = NULL" in upsert


def test_the_upsert_never_writes_an_embedding_itself():
    """The save path stores TEXT. If it ever learned to write a vector it would
    have to embed on the request path, which is the cost this design exists to
    avoid."""
    conn, _ = _reconcile(_decision("a", "b"))
    upsert = next(s for s in conn.statements() if s.startswith("INSERT INTO decision_alternatives"))
    assert "embedding)" not in upsert.split("SELECT")[0]
    assert "(decision_pg_id, ordinal, text)" in upsert


def test_the_reported_counts_describe_what_actually_changed():
    conn, stats = _reconcile(_decision("a", "b", "c"), fetch_rows=[{"id": 1}])
    assert stats == {"desired": 3, "written": 1, "removed": 3}


# ── Pending is a query, not a queue ───────────────────────────────────────────

class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self, timeout=None):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class SweepConn(FakeConn):
    """Adds the transaction() context the vector write runs inside."""

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


def _coordinator_for_sweep(pending_rows):
    c = MemoryCoordinator()
    conn = SweepConn(fetch_rows=pending_rows)
    c._pool = FakePool(conn)
    return c, conn


def test_the_pending_set_is_defined_by_a_NULL_EMBEDDING():
    """This is the property the async write path rests on. Pending work lives in
    a committed row, so a crash or a reboot between the save and the embed
    leaves something the next sweep finds — there is no in-process queue to
    lose. Any other definition of pending would reintroduce that window."""
    c, conn = _coordinator_for_sweep([])
    asyncio.run(c._fill_pending_alternative_vectors())
    select = conn.fetched[0][0]
    assert "WHERE embedding IS NULL" in select
    assert "next_attempt_at IS NULL OR next_attempt_at <= now()" in select


def test_a_row_is_NEVER_excluded_from_the_sweep_by_its_attempt_count():
    """No give-up threshold, deliberately.

    An alternative that cannot be embedded is nearly always a statement about
    the embedder, not about the row — the text is already stored, non-blank and
    clamped. Writing rows off during an outage is the v0.7.2 defect (a batch
    503 charged to every record), and here it would strand them permanently
    because nothing ever revisits an abandoned row.
    """
    c, conn = _coordinator_for_sweep([])
    asyncio.run(c._fill_pending_alternative_vectors())
    select = conn.fetched[0][0]
    assert "attempts <" not in select
    assert "attempts >" not in select
    assert "ORDER BY attempts, id" in select   # fairest-first, not exclusion


def test_nothing_pending_means_no_embedder_call_at_all():
    c, conn = _coordinator_for_sweep([])
    called = {"n": 0}

    async def _never(texts, client):
        called["n"] += 1
        return []

    c._embed_many = _never
    assert asyncio.run(c._fill_pending_alternative_vectors()) == 0
    assert called["n"] == 0


def test_a_filled_row_is_written_only_while_it_still_holds_the_text_embedded():
    """A re-save can reset the row while its batch is in flight. Writing the
    vector back unconditionally would then attach an embedding to text it does
    not describe — undetectable in the data, and fatal to the grouping the
    table exists for."""
    rows = [{"id": 7, "text": "an option"}]
    c, conn = _coordinator_for_sweep(rows)

    async def _fake_embed(texts, client):
        return [[0.5] * 3 for _ in texts]

    c._embed_many = _fake_embed
    filled = asyncio.run(c._fill_pending_alternative_vectors())
    assert filled == 1
    update = next(s for s in conn.statements() if s.startswith("UPDATE decision_alternatives"))
    assert "WHERE id = $1 AND text = $3 AND embedding IS NULL" in update
    assert "embedded_at = now()" in update
    assert "attempts = 0" in update      # success clears the failure history
    assert "last_error = NULL" in update


def test_a_failed_batch_is_deferred_and_charged_but_never_written_off():
    """Backoff, not abandonment. `attempts` grows so the retry interval grows
    and telemetry can flag the row as failing — and the row stays in the
    pending set, so it fills the moment the embedder returns."""
    rows = [{"id": 7, "text": "a"}, {"id": 9, "text": "b"}]
    c, conn = _coordinator_for_sweep(rows)

    async def _boom(texts, client):
        raise RuntimeError("embedder down")

    c._embed_many = _boom
    assert asyncio.run(c._fill_pending_alternative_vectors()) == 0
    deferred = next(s for s in conn.statements() if "next_attempt_at = now()" in s)
    assert "attempts = attempts + 1" in deferred
    assert "last_error = $2" in deferred
    assert "power(2, attempts)" in deferred          # exponential
    assert "least($3," in deferred                   # capped
    # Nothing that removes the row from the pending set.
    assert "embedding =" not in deferred
    assert "status" not in deferred
    assert conn.executed[-1][1][0] == [7, 9]


def test_a_sweep_that_raises_does_not_kill_the_worker_loop():
    """FAILURE ≠ IDLE. A worker that dies on one bad sweep leaves a table full
    of pending rows and a process that looks like it has nothing to do."""
    c, _ = _coordinator_for_sweep([])
    calls = {"n": 0}

    async def _explode():
        calls["n"] += 1
        if calls["n"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("sweep failed")

    c._fill_pending_alternative_vectors = _explode
    coord.ALT_VECTOR_POLL_INTERVAL = 0.0
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(c._alternative_vector_worker())
    assert calls["n"] == 3   # survived two failures before the cancel


# ── Batch embedding ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_a_batch_is_ONE_request_and_keeps_input_order():
    captured = {}

    class Client:
        async def post(self, url, json=None, timeout=None):
            captured["input"] = json["input"]
            captured["calls"] = captured.get("calls", 0) + 1
            return _Resp({"data": [
                {"index": 2, "embedding": [3.0]},
                {"index": 0, "embedding": [1.0]},
                {"index": 1, "embedding": [2.0]},
            ]})

    out = asyncio.run(MemoryCoordinator._embed_many(None, ["a", "b", "c"], Client()))
    assert captured["calls"] == 1
    assert captured["input"] == ["a", "b", "c"]
    # Re-ordered by the response's own index, never trusted to arrive in order:
    # a shuffled response would otherwise attach every vector to the wrong
    # alternative — invisible in the data, fatal to the similarity.
    assert out == [[1.0], [2.0], [3.0]]


def test_a_short_response_is_an_error_not_a_silent_misalignment():
    class Client:
        async def post(self, url, json=None, timeout=None):
            return _Resp({"data": [{"index": 0, "embedding": [1.0]}]})

    with pytest.raises(RuntimeError):
        asyncio.run(MemoryCoordinator._embed_many(None, ["a", "b"], Client()))


def test_each_item_is_clamped_and_the_timeout_covers_the_whole_payload():
    captured = {}

    class Client:
        async def post(self, url, json=None, timeout=None):
            captured["lens"] = [len(t) for t in json["input"]]
            captured["timeout"] = timeout
            return _Resp({"data": [{"index": i, "embedding": [0.0]} for i in range(2)]})

    long = "x" * (coord.EMBED_MAX_CHARS + 500)
    asyncio.run(MemoryCoordinator._embed_many(None, [long, "short"], Client()))
    assert captured["lens"] == [coord.EMBED_MAX_CHARS, len("short")]
    assert captured["timeout"] == coord.embed_ceiling(
        coord.EMBED_MAX_CHARS + len("short"))


def test_an_empty_batch_never_reaches_the_network():
    class Client:
        async def post(self, *a, **k):
            raise AssertionError("must not post")

    assert asyncio.run(MemoryCoordinator._embed_many(None, [], Client())) == []
