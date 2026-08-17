"""SEC-A5-04 (PR A5 fix round): `credentialed_route_denied` is entirely
attacker-controlled in volume — any holder of a valid (possibly leaked)
agent token can emit one per request in a loop, and on an auth-off +
override install so can any anonymous caller — but it was written with the
bounded writer's DEFAULT drop-OLDEST eviction. A flood of denials could
therefore evict the genuine `token_verify_failed`/`daemon_token_issued`
lines recording an actual compromise, which is exactly the audit trail
A3/PR A3 exists to provide. Fix: register the event in
coordinator._ATTACKER_TRIGGERABLE_EVENTS so a flood can only ever evict
itself (drop-NEWEST), same as its sibling `token_verify_failed`."""
import asyncio
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))


def test_credentialed_route_denied_registered_as_attacker_triggerable():
    import coordinator
    assert "credentialed_route_denied" in coordinator._ATTACKER_TRIGGERABLE_EVENTS


def test_credentialed_route_denied_write_requests_drop_newest_policy(monkeypatch, tmp_path):
    """MUTATION TARGET: proves the actual WRITE CALL this event triggers
    requests drop-newest, not just that the event name sits in the set —
    a completeness check on the wiring between the registry and the write
    site, not only the registry itself."""
    log_path = tmp_path / "credential-audit.jsonl"
    monkeypatch.setenv("CREDENTIAL_AUDIT_LOG_PATH", str(log_path))
    import coordinator
    importlib.reload(coordinator)

    captured = {}
    real_write = coordinator._credential_audit_writer.write

    def _spy(line, *, drop_newest_when_full=False):
        captured["drop_newest_when_full"] = drop_newest_when_full
        return real_write(line, drop_newest_when_full=drop_newest_when_full)

    monkeypatch.setattr(coordinator._credential_audit_writer, "write", _spy)
    coordinator.record_credentialed_route_denied("http://x", "GET", "/v1/models")
    assert captured["drop_newest_when_full"] is True
    coordinator._credential_audit_writer = None  # avoid leaking the reload into other tests


@pytest.mark.asyncio
async def test_denial_flood_cannot_evict_queued_compromise_evidence(monkeypatch, tmp_path):
    """End-to-end proof of the actual failure scenario: two genuine
    lifecycle events are queued first (behind a deliberately tiny writer so
    the queue is provably full without needing thousands of real writes),
    then a flood of denials tries to push past them. None of the flood may
    reach disk at the genuine lines' expense.

    Calling AsyncLineWriter.write() synchronously, back to back, with no
    `await` in between and no prior yield to the event loop, is what makes
    this deterministic: write() itself never awaits (it only enqueues +
    schedules the drain task), so the drain task gets no chance to run
    until this coroutine itself awaits — by which point every write below
    has already landed (or been dropped) in the exact order issued."""
    from log_hygiene import AsyncLineWriter
    import coordinator

    log_path = tmp_path / "credential-audit.jsonl"
    writer = AsyncLineWriter(str(log_path), maxsize=2)
    monkeypatch.setattr(coordinator, "_credential_audit_writer", writer)

    # Genuine compromise evidence, queued FIRST -- fills the tiny queue.
    coordinator._write_credential_audit_line(
        "token_verify_failed", origin="gateway", digest_prefix="deadbeef")
    coordinator._write_credential_audit_line(
        "daemon_token_issued", origin="gateway", daemon="consolidation")

    # Flood: an attacker (or a leaked token's holder) hammering a
    # non-allowlisted route at a credentialed backend. Queue is full (2/2)
    # before any of these -- every one must evict ITSELF, never the two
    # genuine lines above.
    for _ in range(5):
        coordinator.record_credentialed_route_denied("http://x", "POST", "/v1/models")

    # Bounded, not bare: AsyncLineWriter's DEFAULT drop-oldest path evicts
    # via get_nowait() without a matching task_done() call, which leaks
    # Queue.unfinished_tasks and makes .flush()/.join() hang FOREVER once
    # any drop-oldest eviction has ever happened (found while mutation-
    # checking this exact test — a genuine pre-existing defect in the
    # shared writer, out of A5's scope, recorded in the handoff, not
    # fixed here). A bare `await writer.flush()` would make a reverted fix
    # hang the whole suite instead of failing this one test.
    try:
        await asyncio.wait_for(writer.flush(), timeout=2.0)
    except asyncio.TimeoutError:
        pytest.fail(
            "writer.flush() hung -- the flood took the drop-OLDEST path "
            "(AsyncLineWriter's unfinished_tasks leak on manual eviction), "
            "meaning credentialed_route_denied is NOT registered as "
            "attacker-triggerable"
        )
    content = log_path.read_text()
    assert '"event":"token_verify_failed"' in content, (
        "the flood evicted genuine compromise evidence -- exactly the "
        "defect SEC-A5-04 exists to close"
    )
    assert '"event":"daemon_token_issued"' in content
    assert content.count('"event":"credentialed_route_denied"') == 0, (
        "a drop-newest event must never itself reach disk once the queue "
        "is full -- it evicts itself, not the entries ahead of it"
    )
    assert writer.dropped == 5


# ── Mutation check target ────────────────────────────────────────────────────
# See A5_HANDOFF.md's mutation-check table: removing "credentialed_route_
# denied" from coordinator._ATTACKER_TRIGGERABLE_EVENTS makes BOTH
# test_credentialed_route_denied_registered_as_attacker_triggerable and
# test_denial_flood_cannot_evict_queued_compromise_evidence fail (the
# latter because the write call reverts to drop-OLDEST, and the flood
# evicts the genuine token_verify_failed line the test asserts survives).
