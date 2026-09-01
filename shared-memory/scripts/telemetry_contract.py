"""THE TELEMETRY CONTRACT — one Python source of truth for every key the
gateway emits on ``GET /health`` and ``GET /memory/telemetry``. Introduced at
v0.9.74; ``VERSION`` below tracks the release this file was last meaningfully
edited in (W2, decision:1832, added it as a fifth version pin — see
``VERSION``'s own comment).

⛔ THIS DICT IS THE CONTRACT. ``Documentation/telemetry-contract.md`` is
GENERATED from it (``render_markdown``), and ``tests/test_telemetry_contract.py``
asserts BOTH directions against it — every documented key must be emitted, and
every emitted key must be documented. So the doc cannot drift from the code the
way a hand-written table does, and a new key that nobody documents fails the
suite rather than arriving unannounced in a consumer's payload.

THE ROLES (decision:1785, the operator's rule of thumb, verbatim):

    up/down → health · a number → telemetry · number > limit → telemetry keeps
    the number, health raises the warning, the log records the crossing.

``/health`` answers "can I use it now, and what should I expect": one status
enum, one enum per DEPENDENCY, the WARNINGS a limit crossing raised, identity/
version, and the sizing a client needs to set its own timeouts. It is cheap —
served from a 3 s cache over a 60 s refresher, and it makes NO database call at
request time.

``/memory/telemetry`` is THE NUMBERS: counters, gauges, percentiles and
censuses, with the limit stated next to the number it bounds. Bounded cost per
request (a 15 s whole-payload cache; the unbounded graph walk moved into the
refresher — see ``nrem``).

The logs are the final word on "what happened at 03:12": every dependency state
TRANSITION and every warning raised/cleared writes ONE line named after the key
that changed (``log`` below is that line's name). Never a per-poll line.

── PATH GRAMMAR ────────────────────────────────────────────────────────────────
A key path is dot-separated. Two non-literal segments:

  ``*``   — a DYNAMIC map key (a backend URL, a model name, a client version).
            ⚠ A dynamic key may itself contain dots (backend URLs do), which is
            exactly why ``walk_payload`` descends the trie rather than splitting
            a joined string.
  ``x[]`` — ``x`` is a LIST; the children documented under ``x[]`` are the shape
            of ONE element. An empty list is emitted as the ``x[]`` path itself.

An EMPTY dict is emitted as its own container path (it has no leaves to emit),
so a container that can be empty is documented as well as its ``*`` children.

── LIFECYCLE FIELDS ────────────────────────────────────────────────────────────
``moved_to``   — this key is emitted from its NEW home as well as here; read the
                 new path, not this one.
``removed_in`` — the release that stops emitting this key here.
``since``      — the release the key first appeared in. ``BASELINE`` means
                 "present at the v0.9.73 baseline, earlier introduction not
                 established" — stated rather than guessed (fact:1338: a number
                 you did not measure is not a number you may assert).
"""

from __future__ import annotations

__all__ = [
    "BASELINE",
    "VERSION",
    "INTRODUCED_0_9_74",
    "INTRODUCED_0_9_79",
    "INTRODUCED_0_9_81",
    "DUAL_EMIT_DROP_TARGET",
    "CATEGORIES",
    "HEALTH",
    "TELEMETRY",
    "MEANING_CHANGES",
    "ANONYMOUS_HEALTH_KEYS",
    "walk_payload",
    "canonical_paths",
    "render_markdown",
    "type_matches",
]

#: The release this contract document was LAST MEANINGFULLY EDITED in — not a
#: free-running "today". D4 (decision:1832) makes this the FIFTH entry in
#: test_change_group_contracts.py's `_VERSION_PINS`, proven against the known-
#: broken state first (this constant sat at "0.9.74" through three releases —
#: 0.9.75/76/77 — of the OTHER four pins moving without it, unnoticed because
#: nothing compared them). ⚠ This value now leads the other four rather than
#: following them: W2 ships as the next +0.0.1 after the v0.9.78 anchor, so it
#: is bumped here to "0.9.79", and the fifth pin will not agree with
#: coordinator.py's FRAMEWORK_VERSION et al. until the merger's own version-
#: bump step (which those four files stay reserved for) catches up to it at
#: release time — that gap is the check doing its job, not a build defect.
VERSION = "0.9.86"


def _version_tuple(v: str) -> tuple:
    """"0.9.79" -> (0, 9, 79) — the same idiom `backfill_domain_of.py`,
    `backfill_project_of.py`, `backfill_promote_grounded.py` and
    `reconcile_project_edges.py` already use, so a release string compares
    numerically (fix round item 1/8, decision:1832): STRING comparison would
    rank "0.9.9" ahead of "0.9.10", which is exactly the kind of silent wrong
    answer a version gate must never give."""
    return tuple(int(p) for p in v.split(".")[:3])


#: "present at the v0.9.73 baseline; the release it first appeared in was not
#: established". Used rather than a guessed version number.
BASELINE = "<=0.9.73"

#: The categories the contract organises keys into (decision:1785).
CATEGORIES = (
    "liveness",
    "dependencies",
    "warnings",
    "capacity",
    "encoders",
    "gateway",
    "outbox",
    "postgres",
    "neo4j",
    "llm",
    "rem",
    "nrem/consolidation",
    "insight",
    "axes/registry",
    "credentials",
    "versions",
    "spine",
    "graph",
)

#: The ONLY keys an anonymous caller sees on an auth-configured install
#: (decision:1333 — unchanged by this release, and pinned by
#: test_health_anonymous_slimming.py).
ANONYMOUS_HEALTH_KEYS = ("status", "version", "api_version")


def _k(types: str, category: str, *, unit: str | None = None,
       since: str = BASELINE, moved_to: str | None = None,
       removed_in: str | None = None, log: str | None = None,
       note: str | None = None) -> dict:
    """One contract entry. ``types`` is a ``|``-separated set of JSON type
    names (``str int float bool list dict null``) — a nullable int is
    ``"int|null"``, and a key that legitimately reports either is documented as
    both rather than as whichever one today's corpus happens to produce."""
    assert category in CATEGORIES, f"unknown category {category!r}"
    return {
        "types": tuple(types.split("|")),
        "category": category,
        "unit": unit,
        "since": since,
        "moved_to": moved_to,
        "removed_in": removed_in,
        "log": log,
        "note": note,
    }


#: ⛔ NOT "now" — the INTRODUCTION stamp `since=INTRODUCED_0_9_74` puts on 228
#: keys and `in_version` puts on the four original MEANING_CHANGES entries.
#: FROZEN at 0.9.74 and named for what it is (D4, decision:1832): bumping it
#: with VERSION would re-date all 228 keys as arriving in whatever release
#: touches this file next, in the regenerated public doc — and nothing in the
#: suite would have caught it, because `since` was never asserted against
#: anything but itself. A key genuinely NEW in a later release gets that
#: release's literal version string, same as `dependencies.postgres.state`
#: below does with `INTRODUCED_0_9_74` itself.
INTRODUCED_0_9_74 = "0.9.74"
#: Same pattern, one release later — FROZEN at 0.9.79 (handback H1): the three
#: W2 MEANING_CHANGES entries below were pinned to the bare `VERSION` constant,
#: but `VERSION` is now the fifth version pin and moves EVERY release
#: (test_change_group_contracts.py's `_VERSION_PINS`). Pinning a historical
#: entry to `VERSION` directly means the very next bump falsifies it — the
#: cheapest "fix" in that moment is to re-date the entry to the new VERSION,
#: which is exactly the meaning-change falsification the whole point of this
#: file exists to prevent (fact:1626). Entries authored under THIS stamp keep
#: it forever, the same way the four originals keep `INTRODUCED_0_9_74`; the
#: general `in_version <= VERSION` bound (item 8) stays the durable rule that
#: catches anything genuinely wrong regardless of which release is current.
INTRODUCED_0_9_79 = "0.9.79"

#: W4's four MEANING_CHANGES entries froze here at release time (QA MED-8 of
#: the v0.9.81 cycle): they were authored against bare ``VERSION`` while the
#: wave was in flight and pinned to this constant at the version bump, the
#: same carve-out INTRODUCED_0_9_79 got — so no later release can silently
#: re-date them.
INTRODUCED_0_9_81 = "0.9.81"
#: The release the dual-emitted /health copies TARGET being dropped in — a
#: TARGET, never a commitment (fix round item 1 on decision:1832): the drop
#: is GATED on the monitor-contract step (Group 3 — the monitor must consume
#: the replacement keys before the originals can go), which has not landed.
#: See coverage ledger row 11 (dual-emit drop, gated on the monitor-contract
#: step) — NOT the CHANGELOG, which narrates releases after the fact and is
#: not where a still-pending obligation is tracked. ⚠ CORRECTED (D4,
#: decision:1832): the code constant sat at "0.9.75" — its ORIGINAL,
#: never-updated value — through three releases of CHANGELOG prose saying
#: otherwise (0.9.75 moved the drop to 0.9.76; 0.9.76 moved it again, undated,
#: because that release became the A1 security fix instead). This names the
#: EARLIEST release it can still land in, mutation-checked against VERSION
#: (test_dual_emit_drop_target_is_strictly_after_this_release) so the target
#: can never silently fall behind the release that is naming it. Whoever ships
#: the removal — gated on the monitor-contract step actually landing —
#: updates this alongside it.
DUAL_EMIT_DROP_TARGET = "0.9.87"


# ═══════════════════════════════════════════════════════════════════════════════
# GET /health — the authenticated payload (an auth-off install serves the same
# shape to everyone; an anonymous caller on an auth-configured install sees
# ANONYMOUS_HEALTH_KEYS and nothing else).
# ═══════════════════════════════════════════════════════════════════════════════
HEALTH: dict[str, dict] = {
    # ── liveness ────────────────────────────────────────────────────────────
    "status": _k("str", "liveness", note=(
        "ok | degraded | down. down if any dependency is down; degraded if any "
        "dependency is degraded OR warnings is non-empty; else ok. ⛔ THE HTTP "
        "CODE IS A DIFFERENT QUESTION: 503 iff embedder or reranker is down "
        "(the save mandate) — every other verdict is served 200 with the enum.")),
    "version": _k("str", "versions"),
    "api_version": _k("int", "versions"),
    "agent": _k("str", "liveness", note="authenticated callers only"),
    "role": _k("str", "liveness", note=(
        "read | write | admin. `admin` since 0.9.74 — an admin token is confined "
        "to /admin/* and cannot save either, so reporting it as `write` "
        "overstated it.")),
    "auth_required": _k("bool", "liveness"),
    "auth_scheme": _k("str", "liveness"),
    "backup_in_progress": _k("bool", "liveness"),
    "inference_busy": _k("str", "llm", note="busy | idle | unknown"),

    # ── dependencies (NEW, 0.9.74; enum vocabulary documented W2/decision:1832) ─
    "dependencies.postgres.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                      log="health.postgres", note=(
        "ok | down | unknown — unknown before the background refresher's "
        "first Postgres probe completes")),
    "dependencies.postgres.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.neo4j.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                   log="health.neo4j", note=(
        "ok | down | unknown — unknown before the background refresher's "
        "first Neo4j probe completes")),
    "dependencies.neo4j.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.embedder.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                      log="health.embedder", note=(
        "ok | degraded | down — down iff the liveness probe itself failed; "
        "degraded iff live but the capability probe reads too_slow/failing")),
    "dependencies.embedder.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.reranker.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                      log="health.reranker", note=(
        "ok | degraded | down — same rule as dependencies.embedder.state")),
    "dependencies.reranker.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.llm_pool.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                      log="health.llm_pool", note=(
        "ok | degraded | down | unknown. unknown iff no backend has been "
        "probed at all. down iff every probed backend is down — liveness is "
        "never softened by configuration. degraded: some (not all) backends "
        "down, OR a declared fleet was entirely excluded (the legacy "
        "fallback is serving instead), OR nothing was declared at all (W2, "
        "decision:1832 — the built-in fallback IS serving), OR every probed "
        "backend answers but none is ELIGIBLE for any traffic class (W2, "
        "fleet-wide only — see MEANING_CHANGES)")),
    "dependencies.llm_pool.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.rem_daemon.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                        log="health.rem_daemon", note=(
        "ok | degraded | down. down iff the REM process is not running. "
        "degraded: dead-letters > 0, OR no backend counts toward dream slots "
        "(W2, decision:1832 — REM structurally cannot run against this "
        "fleet) — both reasons appear together when both apply")),
    "dependencies.rem_daemon.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.nrem_daemon.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                         log="health.nrem_daemon", note=(
        "ok | degraded | down | unknown. down iff the NREM process is not "
        "running. unknown iff dream slots ARE possible but the consolidation "
        "snapshot has not been probed yet. degraded: stalled, OR folds "
        "attempted with none succeeded, OR no backend counts toward dream "
        "slots (W2, decision:1832 — this last one WINS OVER unknown, since "
        "it is a config fact knowable before any probe)")),
    "dependencies.nrem_daemon.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.outbox.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                    log="health.outbox", note=(
        "ok | degraded | unknown — unknown before the first outbox census; "
        "degraded iff failed rows > 0 or the oldest pending row exceeds the "
        "age limit. Never down: an outbox backlog is not a liveness fact")),
    "dependencies.outbox.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),
    "dependencies.registry.state": _k("str", "dependencies", since=INTRODUCED_0_9_74,
                                      log="health.registry", note=(
        "ok | degraded | unknown — unknown before the first registry census; "
        "degraded iff a read failure or a census failure has been counted. "
        "Never down: an unreadable registry degrades axis resolution, it "
        "does not take the gateway down")),
    "dependencies.registry.reason": _k("str|null", "dependencies", since=INTRODUCED_0_9_74),

    # ── warnings (NEW, 0.9.74) ──────────────────────────────────────────────
    "warnings[]": _k("list", "warnings", since=INTRODUCED_0_9_74, note=(
        "One entry per limit crossing. The THRESHOLD lives server-side so every "
        "consumer sees the same verdict — the monitor stops deriving health from "
        "telemetry numbers client-side.")),
    "warnings[].key": _k("str", "warnings", since=INTRODUCED_0_9_74,
                         log="health.warning.<key>"),
    "warnings[].limit": _k("int|float", "warnings", since=INTRODUCED_0_9_74),
    "warnings[].observed": _k("int|float", "warnings", since=INTRODUCED_0_9_74),
    "warnings[].unit": _k("str", "warnings", since=INTRODUCED_0_9_74),

    # ── encoders (KEPT: a client derives its own timeouts from these) ───────
    "embedder": _k("str", "encoders", note="ok | timeout | down | http_<code>"),
    "reranker": _k("str", "encoders", note="ok | timeout | down | http_<code>"),
    "backend_capability.probed_at": _k("str|null", "encoders"),
    "backend_capability.gateway_host_load1": _k("float|null", "encoders"),
    "backend_capability.status": _k("str|null", "encoders"),
    "backend_capability.*.probe_chars": _k("int", "encoders", unit="_chars"),
    "backend_capability.*.latency_s": _k("float", "encoders", unit="_s"),
    "backend_capability.*.throughput_chars_s": _k("int|float", "encoders"),
    "backend_capability.*.projected_full_payload_s": _k("float", "encoders", unit="_s"),
    "backend_capability.*.ceiling_s": _k("float", "encoders", unit="_s"),
    "backend_capability.*.serves_full_payload": _k("bool", "encoders"),
    "backend_capability.*.status": _k("str", "encoders"),
    "backend_capability.*.projection_stale": _k("bool", "encoders"),
    "backend_capability.*.last_ok_at": _k("str|null", "encoders"),
    "backend_capability.*.projection_age_s": _k("float|null", "encoders", unit="_s"),

    # ── capacity: FIVE keys a client reads stay; the rest move ─────────────
    "capacity.timestamp": _k("str", "capacity"),
    "capacity.derived.s_mean_s": _k("float|null", "capacity", unit="_s"),
    "capacity.derived.s_max_measured_s": _k("float|null", "capacity", unit="_s"),
    "capacity.derived.client_ceiling_s": _k("float|null", "capacity", unit="_s"),
    "capacity.probe.probe_stale": _k("bool", "capacity"),
    # …and the other 23, dual-emitted this release.
    "capacity.trigger": _k("str", "capacity",
                           moved_to="telemetry:capacity.trigger", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.hardware.nproc": _k(
        "int", "capacity", moved_to="telemetry:capacity.fingerprint.hardware.nproc",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.hardware.mem_total_bytes": _k(
        "int", "capacity", unit="_bytes",
        moved_to="telemetry:capacity.fingerprint.hardware.mem_total_bytes",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.hardware.gpu_present": _k(
        "bool", "capacity",
        moved_to="telemetry:capacity.fingerprint.hardware.gpu_present",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.rerank_max_doc_chars": _k(
        "int", "capacity", unit="_chars",
        moved_to="telemetry:capacity.fingerprint.encoder_config.rerank_max_doc_chars",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.search_candidate_floor": _k(
        "int", "capacity",
        moved_to="telemetry:capacity.fingerprint.encoder_config.search_candidate_floor",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.embedder_url": _k(
        "str", "capacity",
        moved_to="telemetry:capacity.fingerprint.encoder_config.embedder_url",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.reranker_url": _k(
        "str", "capacity",
        moved_to="telemetry:capacity.fingerprint.encoder_config.reranker_url",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.cpu_encoder_replicas": _k(
        "str|int|null", "capacity",
        moved_to="telemetry:capacity.fingerprint.encoder_config.cpu_encoder_replicas",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.fingerprint.encoder_config.gpu_encoder_replicas": _k(
        "str|int|null", "capacity",
        moved_to="telemetry:capacity.fingerprint.encoder_config.gpu_encoder_replicas",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.reranker_chars_per_s": _k(
        "int|float|null", "capacity",
        moved_to="telemetry:capacity.probe.reranker_chars_per_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.reranker_status": _k(
        "str|null", "capacity",
        moved_to="telemetry:capacity.probe.reranker_status", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.embedder_chars_per_s": _k(
        "int|float|null", "capacity",
        moved_to="telemetry:capacity.probe.embedder_chars_per_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.probed_at": _k(
        "str|null", "capacity",
        moved_to="telemetry:capacity.probe.probed_at", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.reranker_measured_at": _k(
        "str|null", "capacity",
        moved_to="telemetry:capacity.probe.reranker_measured_at", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.probe.embedder_measured_at": _k(
        "str|null", "capacity",
        moved_to="telemetry:capacity.probe.embedder_measured_at", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.s_mean_measured_s": _k(
        "float|null", "capacity", unit="_s",
        moved_to="telemetry:capacity.derived.s_mean_measured_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.payload_basis": _k(
        "str", "capacity",
        moved_to="telemetry:capacity.derived.payload_basis", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.payload_basis_sample_count": _k(
        "int", "capacity",
        moved_to="telemetry:capacity.derived.payload_basis_sample_count",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.payload_mean_chars_measured": _k(
        "int|float|null", "capacity", unit="_chars",
        moved_to="telemetry:capacity.derived.payload_mean_chars_measured",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.payload_max_chars_measured": _k(
        "int|null", "capacity", unit="_chars",
        moved_to="telemetry:capacity.derived.payload_max_chars_measured",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.queue_bound": _k(
        "int|null", "capacity",
        moved_to="telemetry:capacity.derived.queue_bound", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.tolerable_wait_s": _k(
        "float|null", "capacity", unit="_s",
        moved_to="telemetry:capacity.derived.tolerable_wait_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.single_search_exceeds_wait": _k(
        "bool", "capacity",
        moved_to="telemetry:capacity.derived.single_search_exceeds_wait",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "capacity.derived.recommended_reranker_mem_limit_bytes": _k(
        "int|null", "capacity", unit="_bytes",
        moved_to="telemetry:capacity.derived.recommended_reranker_mem_limit_bytes",
        removed_in=DUAL_EMIT_DROP_TARGET),

    # ── llm: the enum map stays (the monitor's tiles read it); detail moves ──
    "llm": _k("str", "llm", note="ok | down — ok iff ANY backend answered"),
    "llm_backends.*": _k("str", "llm",
                         note="per-backend enum: ok | timeout | down | http_<code>"),
    "llm_reserved[]": _k("list", "llm", moved_to="telemetry:llm.reserved",
                         removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_oldest_inflight_age_s": _k("float", "llm", unit="_s",
                                    moved_to="telemetry:llm.oldest_inflight_age_s",
                                    removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_suspect_wedged[]": _k("list", "llm",
                               moved_to="telemetry:llm.suspect_wedged",
                               removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.weight": _k("float", "llm", moved_to="telemetry:llm.pool.*.weight",
                            removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.inflight": _k("int", "llm", moved_to="telemetry:llm.pool.*.inflight",
                              removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.routed": _k("int", "llm", moved_to="telemetry:llm.pool.*.routed",
                            removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.routed_pct": _k("float", "llm", unit="_pct",
                                moved_to="telemetry:llm.pool.*.routed_pct",
                                removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.fails": _k("int", "llm", moved_to="telemetry:llm.pool.*.fails",
                           removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.cooldown": _k("float", "llm", unit="_s",
                              moved_to="telemetry:llm.pool.*.cooldown",
                              removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_pool.*.reserved": _k("bool", "llm", moved_to="telemetry:llm.pool.*.reserved",
                              removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_affinity.hits": _k("int", "llm", moved_to="telemetry:llm.affinity.hits",
                            removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_affinity.misses": _k("int", "llm", moved_to="telemetry:llm.affinity.misses",
                              removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_affinity.hit_rate": _k("float|null", "llm",
                                moved_to="telemetry:llm.affinity.hit_rate",
                                removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_affinity.hot_prefixes": _k(
        "dict", "llm", moved_to="telemetry:llm.affinity.hot_prefixes",
        removed_in=DUAL_EMIT_DROP_TARGET, note="empty when no prefix is hot"),
    "llm_affinity.hot_prefixes.*.backend": _k(
        "str", "llm", moved_to="telemetry:llm.affinity.hot_prefixes.*.backend",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_affinity.hot_prefixes.*.hits": _k(
        "int", "llm", moved_to="telemetry:llm.affinity.hot_prefixes.*.hits",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routed_role_extract": _k(
        "int", "llm", moved_to="telemetry:llm.routing.routed_role_extract",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routed_role_extract_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.routing.routed_role_extract_last_ts",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routed_role_judge": _k(
        "int", "llm", moved_to="telemetry:llm.routing.routed_role_judge",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routed_role_judge_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.routing.routed_role_judge_last_ts", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_no_eligible_backend": _k(
        "int", "llm", moved_to="telemetry:llm.routing.routing_no_eligible_backend",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_no_eligible_backend_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.routing.routing_no_eligible_backend_last_ts",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_fit_rejected": _k(
        "int", "llm", moved_to="telemetry:llm.routing.routing_fit_rejected",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_fit_rejected_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.routing.routing_fit_rejected_last_ts",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_backend_at_capacity": _k(
        "int", "llm", moved_to="telemetry:llm.routing.routing_backend_at_capacity",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_routing.routing_backend_at_capacity_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.routing.routing_backend_at_capacity_last_ts",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_token_usage.*.tokens_prompt_total": _k(
        "int", "llm", unit="_total",
        moved_to="telemetry:llm.token_usage.*.tokens_prompt_total", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_token_usage.*.tokens_completion_total": _k(
        "int", "llm", unit="_total",
        moved_to="telemetry:llm.token_usage.*.tokens_completion_total",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_token_usage.*.tokens_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.token_usage.*.tokens_last_ts", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_latency.*.requests_total": _k(
        "int", "llm", unit="_total",
        moved_to="telemetry:llm.latency.*.requests_total", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_latency.*.requests_failed_total": _k(
        "int", "llm", unit="_total",
        moved_to="telemetry:llm.latency.*.requests_failed_total", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_latency.*.latency_sum_s": _k(
        "float", "llm", unit="_s",
        moved_to="telemetry:llm.latency.*.latency_sum_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_latency.*.latency_max_s": _k(
        "float", "llm", unit="_s",
        moved_to="telemetry:llm.latency.*.latency_max_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_latency.*.latency_last_ts": _k(
        "str|null", "llm",
        moved_to="telemetry:llm.latency.*.latency_last_ts", removed_in=DUAL_EMIT_DROP_TARGET),

    # ── daemon liveness: PID enums, renamed; old spellings kept this release ─
    "rem_daemon_process": _k("str", "rem", since=INTRODUCED_0_9_74,
                             note="running | stopped — a PID check, nothing more"),
    "nrem_daemon_process": _k("str", "nrem/consolidation", since=INTRODUCED_0_9_74,
                              note="running | stopped — a PID check, nothing more"),
    "daemon": _k("str", "nrem/consolidation",
                 moved_to="health:nrem_daemon_process", removed_in=DUAL_EMIT_DROP_TARGET,
                 note="the NREM daemon's PID check under its pre-0.9.74 name"),
    "rem_daemon": _k("str", "rem", moved_to="health:rem_daemon_process",
                     removed_in=DUAL_EMIT_DROP_TARGET,
                     note="the REM daemon's PID check under its pre-0.9.74 name"),

    # ── config: whole family moves ──────────────────────────────────────────
    "config.llm_backends[]": _k("list", "llm",
                                moved_to="telemetry:config.llm_backends", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].url": _k("str", "llm",
                                    moved_to="telemetry:config.llm_backends[].url",
                                    removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].weight": _k("float", "llm",
                                       moved_to="telemetry:config.llm_backends[].weight",
                                       removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].has_credential": _k(
        "bool", "credentials",
        moved_to="telemetry:config.llm_backends[].has_credential", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].model": _k("str|null", "llm",
                                      moved_to="telemetry:config.llm_backends[].model",
                                      removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].roles": _k("list|null", "llm",
                                      moved_to="telemetry:config.llm_backends[].roles",
                                      removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].n_ctx": _k("int|null", "llm",
                                      moved_to="telemetry:config.llm_backends[].n_ctx",
                                      removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].private_ok": _k(
        "bool", "llm", moved_to="telemetry:config.llm_backends[].private_ok",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].max_inflight": _k(
        "int|null", "llm", moved_to="telemetry:config.llm_backends[].max_inflight",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].price_per_mtok_in": _k(
        "float|null", "llm",
        moved_to="telemetry:config.llm_backends[].price_per_mtok_in", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_backends[].price_per_mtok_out": _k(
        "float|null", "llm",
        moved_to="telemetry:config.llm_backends[].price_per_mtok_out", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_pool_tuning.fail_threshold": _k(
        "int", "llm", moved_to="telemetry:config.llm_pool_tuning.fail_threshold",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_pool_tuning.fail_window_s": _k(
        "float|int", "llm", unit="_s",
        moved_to="telemetry:config.llm_pool_tuning.fail_window_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_pool_tuning.cooldown_s": _k(
        "float|int", "llm", unit="_s",
        moved_to="telemetry:config.llm_pool_tuning.cooldown_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_pool_tuning.max_tries": _k(
        "int", "llm", moved_to="telemetry:config.llm_pool_tuning.max_tries",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_affinity.prefix_chars": _k(
        "int", "llm", unit="_chars",
        moved_to="telemetry:config.llm_affinity.prefix_chars", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_affinity.ttl_s": _k(
        "float|int", "llm", unit="_s",
        moved_to="telemetry:config.llm_affinity.ttl_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.llm_affinity.max_inflight": _k(
        "int", "llm", moved_to="telemetry:config.llm_affinity.max_inflight",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "config.embed_max_chars": _k(
        "int", "encoders", unit="_chars",
        moved_to="telemetry:config.embed_max_chars", removed_in=DUAL_EMIT_DROP_TARGET),
    "config.allow_unauthenticated_provider_keys": _k(
        "bool", "credentials",
        moved_to="telemetry:config.allow_unauthenticated_provider_keys",
        removed_in=DUAL_EMIT_DROP_TARGET,
        note="present ONLY while the S-05 override is actually in effect"),

    # ── consolidation: the ADR-018 subset stays on /health ──────────────────
    "consolidation.stalled": _k("bool", "nrem/consolidation"),
    "consolidation.last_outcome": _k("str|null", "nrem/consolidation"),
    "consolidation.last_success_age_seconds": _k(
        "int|null", "nrem/consolidation", unit="_seconds"),
    "consolidation.last_success_cycle_type": _k("str|null", "nrem/consolidation"),
    "consolidation.stalled_types[]": _k("list", "nrem/consolidation"),
    "consolidation.fresh": _k("bool", "nrem/consolidation", note=(
        "false means the 60 s refresher's last pass FAILED — the snapshot is "
        "stale, not a verdict about the system")),
    "consolidation.inference_busy": _k("str", "llm",
                                       note="the top-level inference_busy, inside the cached snapshot"),
    "consolidation.graph_invalid_nodes": _k("int|null", "graph"),
    "consolidation.gpu_probe.state": _k("str", "llm"),
    "consolidation.gpu_probe.consecutive_hangs": _k("int", "llm"),
    "consolidation.gpu_probe.leaked_children": _k("int", "llm"),
    "consolidation.project_identity.nodes": _k("int", "axes/registry"),
    "consolidation.project_identity.unidentified": _k("int", "axes/registry"),
    "consolidation.project_identity.mismatched": _k("int", "axes/registry"),
    "consolidation.project_identity.unregistered": _k("int", "axes/registry"),
    "consolidation.project_identity.complete": _k("bool", "axes/registry"),
    "consolidation.domain_identity.nodes": _k("int", "axes/registry"),
    "consolidation.domain_identity.registry_rows": _k("int", "axes/registry"),
    "consolidation.domain_identity.unregistered": _k("int", "axes/registry"),
    "consolidation.domain_identity.mismatched": _k("int", "axes/registry"),
    "consolidation.domain_identity.unattached": _k("int", "axes/registry"),
    "consolidation.domain_identity.complete": _k("bool", "axes/registry"),

    # ── the top-level duplicates of coordinator snapshot keys: all move ─────
    "graph_invalid_nodes": _k("int|null", "graph",
                              moved_to="telemetry:graph_integrity.invalid_nodes",
                              removed_in=DUAL_EMIT_DROP_TARGET),
    "project_identity.nodes": _k("int", "axes/registry",
                                 moved_to="telemetry:axes.project_identity.nodes",
                                 removed_in=DUAL_EMIT_DROP_TARGET),
    "project_identity.unidentified": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.project_identity.unidentified", removed_in=DUAL_EMIT_DROP_TARGET),
    "project_identity.mismatched": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.project_identity.mismatched", removed_in=DUAL_EMIT_DROP_TARGET),
    "project_identity.unregistered": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.project_identity.unregistered", removed_in=DUAL_EMIT_DROP_TARGET),
    "project_identity.complete": _k(
        "bool", "axes/registry",
        moved_to="telemetry:axes.project_identity.complete", removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.nodes": _k("int", "axes/registry",
                                moved_to="telemetry:axes.domain_identity.nodes",
                                removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.registry_rows": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.domain_identity.registry_rows", removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.unregistered": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.domain_identity.unregistered", removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.mismatched": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.domain_identity.mismatched", removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.unattached": _k(
        "int", "axes/registry",
        moved_to="telemetry:axes.domain_identity.unattached", removed_in=DUAL_EMIT_DROP_TARGET),
    "domain_identity.complete": _k(
        "bool", "axes/registry",
        moved_to="telemetry:axes.domain_identity.complete", removed_in=DUAL_EMIT_DROP_TARGET),
    "gpu_probe.state": _k("str", "llm", moved_to="telemetry:gpu_probe.state",
                          removed_in=DUAL_EMIT_DROP_TARGET),
    "gpu_probe.consecutive_hangs": _k(
        "int", "llm", moved_to="telemetry:gpu_probe.consecutive_hangs",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "gpu_probe.leaked_children": _k(
        "int", "llm", moved_to="telemetry:gpu_probe.leaked_children",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "pgvector.version": _k("str|null", "postgres",
                           moved_to="telemetry:postgres.pgvector.version",
                           removed_in=DUAL_EMIT_DROP_TARGET),
    "pgvector.iterative_scan": _k("bool", "postgres",
                                  moved_to="telemetry:postgres.pgvector.iterative_scan",
                                  removed_in=DUAL_EMIT_DROP_TARGET),
}


# ═══════════════════════════════════════════════════════════════════════════════
# GET /memory/telemetry — paths are relative to the ``telemetry`` wrapper key.
# The envelope is ``{"status": "success", "telemetry": {...}}``; ``status`` there
# is the REPLY envelope, not a health verdict, and is not part of this table.
# Every section is computed independently and degrades to ``{"error": "..."}`` on
# its own failure, so one dead backend never blanks the payload — hence the
# ``<section>.error`` entries below.
# ═══════════════════════════════════════════════════════════════════════════════
TELEMETRY: dict[str, dict] = {
    # ── liveness / cache ────────────────────────────────────────────────────
    "timestamp": _k("str", "liveness",
                    note="when this payload was SERVED"),
    "generated_at": _k("str", "liveness", since=INTRODUCED_0_9_74, note=(
        "when this payload was BUILT. Differs from `timestamp` by up to "
        "TELEMETRY_CACHE_S — a cached payload is served stale on purpose, and a "
        "reader must be able to tell how stale.")),
    "inference_busy": _k("str", "llm", note="busy | idle | unknown"),

    # ── encoders (NEW, 0.9.74) ──────────────────────────────────────────────
    "encoders.embed.calls": _k("int", "encoders", since=INTRODUCED_0_9_74),
    "encoders.embed.errors": _k("int", "encoders", since=INTRODUCED_0_9_74),
    "encoders.embed.p50_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.embed.p95_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74,
                                log="health.warning.encoder_p95_ms"),
    "encoders.embed.max_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.embed.last_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.embed.last_payload_chars": _k("int|null", "encoders", unit="_chars",
                                            since=INTRODUCED_0_9_74),
    "encoders.embed.window": _k("int", "encoders", since=INTRODUCED_0_9_74, note=(
        "observations the percentiles were computed over — NOT the ring's "
        "capacity. p95 over 3 calls is not a p95.")),
    "encoders.rerank.calls": _k("int", "encoders", since=INTRODUCED_0_9_74),
    "encoders.rerank.errors": _k("int", "encoders", since=INTRODUCED_0_9_74),
    "encoders.rerank.p50_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.rerank.p95_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74,
                                 log="health.warning.encoder_p95_ms"),
    "encoders.rerank.max_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.rerank.last_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74),
    "encoders.rerank.last_payload_chars": _k("int|null", "encoders", unit="_chars",
                                             since=INTRODUCED_0_9_74),
    "encoders.rerank.window": _k("int", "encoders", since=INTRODUCED_0_9_74),
    "encoders.limit_ms": _k("float|null", "encoders", unit="_ms", since=INTRODUCED_0_9_74, note=(
        "ENCODER_LATENCY_WARN_MS — the limit the p95s above are compared against; "
        "null means it is derived per-encoder from backend_capability.ceiling_s "
        "rather than pinned by env.")),

    # ── gateway (NEW, 0.9.74) ───────────────────────────────────────────────
    "gateway.requests_total": _k("int", "gateway", unit="_total", since=INTRODUCED_0_9_74),
    "gateway.by_status.2xx": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.4xx": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.5xx": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.401": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.403": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.409": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.by_status.503": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.latency_p50_ms": _k("float|null", "gateway", unit="_ms", since=INTRODUCED_0_9_74),
    "gateway.latency_p95_ms": _k("float|null", "gateway", unit="_ms", since=INTRODUCED_0_9_74),
    "gateway.latency_window": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.inflight": _k("int", "gateway", since=INTRODUCED_0_9_74),
    "gateway.inflight_max": _k("int", "gateway", since=INTRODUCED_0_9_74,
                               note="GATEWAY_INFLIGHT_MAX; 0 = valve disabled"),
    "gateway.shed_503_total": _k("int", "gateway", unit="_total", since=INTRODUCED_0_9_74,
                                 log="health.warning.pool_shedding"),

    # ── outbox (NEW section, 0.9.74) ────────────────────────────────────────
    "outbox.pending": _k("int", "outbox", since=INTRODUCED_0_9_74),
    "outbox.applied": _k("int", "outbox", since=INTRODUCED_0_9_74),
    "outbox.failed": _k("int", "outbox", since=INTRODUCED_0_9_74, log="health.outbox", note=(
        "⛔ ALWAYS PRESENT, 0 when zero. The pre-0.9.74 `postgres.outbox` census "
        "omitted the key entirely at zero, so absence and zero were "
        "indistinguishable to every consumer.")),
    "outbox.rem_reviewed": _k("int", "outbox", since=INTRODUCED_0_9_74),
    "outbox.oldest_failed_age_s": _k("int|null", "outbox", unit="_s", since=INTRODUCED_0_9_74),
    "outbox.oldest_pending_age_s": _k("int|null", "outbox", unit="_s", since=INTRODUCED_0_9_74,
                                      log="health.warning.outbox_age"),
    "outbox.apply_latency_p50_s": _k("float|null", "outbox", unit="_s", since=INTRODUCED_0_9_74),
    "outbox.apply_latency_p95_s": _k("float|null", "outbox", unit="_s", since=INTRODUCED_0_9_74),
    "outbox.apply_latency_window": _k("int", "outbox", since=INTRODUCED_0_9_74),
    "outbox.drain_rate_per_min": _k("float|null", "outbox", since=INTRODUCED_0_9_74),
    "outbox.age_limit_s": _k("int", "outbox", unit="_s", since=INTRODUCED_0_9_74,
                             note="OUTBOX_AGE_WARN_S — the limit oldest_pending_age_s is compared against"),
    "outbox.error": _k("str", "outbox", since=INTRODUCED_0_9_74,
                       note="present only when this section's own query failed"),

    # ── postgres ────────────────────────────────────────────────────────────
    "postgres.technical_docs": _k("int", "postgres"),
    "postgres.technical_docs_superseded": _k("int", "postgres"),
    "postgres.outbox.*": _k("int", "outbox", moved_to="telemetry:outbox",
                            removed_in=DUAL_EMIT_DROP_TARGET, note=(
                                "the status census; a status with zero rows was "
                                "OMITTED, which is why it moved")),
    "postgres.outbox": _k("dict", "outbox", moved_to="telemetry:outbox",
                          removed_in=DUAL_EMIT_DROP_TARGET,
                          note="emitted as an empty dict when the outbox is empty"),
    "postgres.outbox_failed_oldest_age_seconds": _k(
        "int|null", "outbox", unit="_seconds",
        moved_to="telemetry:outbox.oldest_failed_age_s", removed_in=DUAL_EMIT_DROP_TARGET),
    "postgres.community_summaries.total": _k("int", "postgres"),
    "postgres.community_summaries.superseded": _k("int", "postgres"),
    "postgres.community_summaries.insight": _k("int", "insight"),
    "postgres.pool_in_use": _k("int", "postgres", since=INTRODUCED_0_9_74),
    "postgres.pool_free": _k("int", "postgres", since=INTRODUCED_0_9_74),
    "postgres.pool_size": _k("int", "postgres", since=INTRODUCED_0_9_74),
    "postgres.pool_wait_p50_ms": _k("float|null", "postgres", unit="_ms", since=INTRODUCED_0_9_74),
    "postgres.pool_wait_p95_ms": _k("float|null", "postgres", unit="_ms", since=INTRODUCED_0_9_74),
    "postgres.pool_wait_window": _k("int", "postgres", since=INTRODUCED_0_9_74),
    "postgres.pgvector.version": _k("str|null", "postgres", since=INTRODUCED_0_9_74),
    "postgres.pgvector.iterative_scan": _k("bool", "postgres", since=INTRODUCED_0_9_74),
    "postgres.error": _k("str", "postgres",
                         note="present only when this section's own query failed"),

    # ── neo4j ───────────────────────────────────────────────────────────────
    "neo4j.facts_total": _k("int", "neo4j"),
    "neo4j.facts_rem_pending": _k("int", "rem"),
    "neo4j.facts_unconsolidated": _k("int", "nrem/consolidation"),
    "neo4j.decisions_total": _k("int", "neo4j"),
    "neo4j.decisions_rem_pending": _k("int", "rem"),
    "neo4j.rem_dead_lettered": _k("int", "rem", moved_to="telemetry:rem.dead_lettered",
                                  removed_in=DUAL_EMIT_DROP_TARGET),
    "neo4j.rem_failing": _k("int", "rem", moved_to="telemetry:rem.failing",
                            removed_in=DUAL_EMIT_DROP_TARGET),
    "neo4j.rem_max_attempts": _k("int", "rem", moved_to="telemetry:rem.max_attempts",
                                 removed_in=DUAL_EMIT_DROP_TARGET),
    "neo4j.rem_passed_over_total": _k("int", "rem", unit="_total",
                                      moved_to="telemetry:rem.passed_over",
                                      removed_in=DUAL_EMIT_DROP_TARGET),
    "neo4j.rem_starved_pending": _k("int", "rem",
                                    moved_to="telemetry:rem.starved_pending",
                                    removed_in=DUAL_EMIT_DROP_TARGET),
    "neo4j.query_p50_ms": _k("float|null", "neo4j", unit="_ms", since=INTRODUCED_0_9_74, note=(
        "over BOTH Neo4j callers — the /memory/graph route and the outbox "
        "apply — so the write path that actually blocks the pipeline is in "
        "scope, not only ad-hoc read Cypher")),
    "neo4j.query_p95_ms": _k("float|null", "neo4j", unit="_ms", since=INTRODUCED_0_9_74),
    "neo4j.query_window": _k("int", "neo4j", since=INTRODUCED_0_9_74),
    "neo4j.cypher_rejected_total": _k("int", "neo4j", unit="_total", since=INTRODUCED_0_9_74,
                                      note=(
        "queries the DATABASE refused because the CALLER wrote them wrong "
        "(/memory/graph only — the outbox apply has no caller to blame). "
        "Counted apart from tx_failures_total so a user's typo cannot read as "
        "an outage")),
    "neo4j.tx_failures_total": _k("int", "neo4j", unit="_total", since=INTRODUCED_0_9_74,
                                  note=(
        "OUR failures, from both callers: a failed /memory/graph query and a "
        "failed outbox apply. Non-zero with cypher_rejected_total flat means "
        "Neo4j, not the caller")),
    "neo4j.error": _k("str", "neo4j",
                      note="present only when this section's own query failed"),

    # ── rem (NEW section, 0.9.74) ───────────────────────────────────────────
    "rem.dead_lettered": _k("int", "rem", since=INTRODUCED_0_9_74, log="health.rem_daemon"),
    "rem.failing": _k("int", "rem", since=INTRODUCED_0_9_74),
    "rem.passed_over": _k("int", "rem", since=INTRODUCED_0_9_74),
    "rem.starved_pending": _k("int", "rem", since=INTRODUCED_0_9_74),
    "rem.max_attempts": _k("int", "rem", since=INTRODUCED_0_9_74,
                           note="REM_MAX_ATTEMPTS — the limit dead_lettered counts arrivals at"),
    "rem.throughput_per_hour": _k("float|null", "rem", since=INTRODUCED_0_9_74, note=(
        "records REM stamped in the last hour, from the durable "
        "technical_docs.rem_timing clock")),
    "rem.degeneration_firings": _k("int|null", "rem", since=INTRODUCED_0_9_74, note=(
        "⚠ ALWAYS NULL AT 0.9.74, and null is the honest value. REM runs in a "
        "SEPARATE PROCESS (rem_loop.py); its anti-degeneration detector writes a "
        "log line and nothing durable, so the gateway cannot see it. Reporting 0 "
        "would claim it never fired. A durable counter is owed.")),
    "rem.error": _k("str", "rem", since=INTRODUCED_0_9_74,
                    note="present only when this section's own query failed"),

    # ── registry (NEW section, 0.9.74) ──────────────────────────────────────
    "registry.projects": _k("int", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "rows in `projects`. ⛔ NEVER NULL: on a failed census the LAST GOOD "
        "value is served with `as_of` and `error` beside it, because a null "
        "would make a failed query look like a deployment with no projects")),
    "registry.domains": _k("int", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "rows in `project_domains`. A domain is (project_id, name), so the "
        "same NAME under two projects is two rows — they are different "
        "sections")),
    "registry.aliases": _k("int", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "ACTIVE alias BINDINGS — `project_aliases` + `domain_aliases` — not "
        "rows in `aliases`, which is the shared NAME POOL. A pooled name no "
        "active binding points at resolves nothing")),
    "registry.as_of": _k("str|null", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "when the census last SUCCEEDED. null before the first success")),
    "registry.error": _k("str", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "present only while the last census attempt failed; the counts beside "
        "it are the last good ones")),
    "registry.census_failures_total": _k(
        "int", "axes/registry", unit="_total", since=INTRODUCED_0_9_74, log="health.registry",
        note=("failures of the row-count query behind registry.*. Deliberately "
              "SEPARATE from read_failures_total: a failed census means these "
              "numbers are stale, a failed axis read means a SEARCH silently "
              "answered from the literal string — same subsystem, different "
              "incidents")),
    "registry.read_failures_total": _k("int", "axes/registry", unit="_total",
                                       since=INTRODUCED_0_9_74, log="health.registry",
                                       note="the SEARCH path: a filter that could not be resolved"),
    "registry.refusals.entity_reserved": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "registry.refusals.entity_confusable": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "registry.refusals.entity_unknown": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "registry.refusals.axis_conflict": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "registry.refusals.entities_not_allowed_on_judgement": _k(
        "int", "axes/registry", since=INTRODUCED_0_9_74),
    "registry.refusals.new_project_refused": _k("int", "axes/registry", since=INTRODUCED_0_9_74,
                                                note=(
        "aggregates the project-naming refusals: project_unnameable, "
        "project_spelling_variant, project_confusable")),
    "registry.refusals.new_domain_refused": _k("int", "axes/registry", since=INTRODUCED_0_9_74,
                                               note=(
        "aggregates the domain-naming refusals: domain_unnameable, "
        "domain_spelling_variant, domain_confusable, domain_unknown, "
        "domain_without_project, domain_not_allowed_on_judgement")),
    "registry.error": _k("str", "axes/registry", since=INTRODUCED_0_9_74,
                         note="present only when this section's own query failed"),

    # ── clients (NEW, 0.9.74) ───────────────────────────────────────────────
    "clients.versions_seen": _k("dict", "versions", since=INTRODUCED_0_9_74,
                                note="empty until a 0.9.74+ client calls"),
    "clients.versions_seen.*": _k("int", "versions", since=INTRODUCED_0_9_74, note=(
        "{client VERSION string: requests seen this process}. Fed by the "
        "X-Shared-Memory-Client header both front doors now send.")),

    # ── llm (the family moved off /health) ──────────────────────────────────
    "llm.status": _k("str|null", "llm", since=INTRODUCED_0_9_74, note=(
        "ok | down — ok iff ANY backend answered. Read off the /health probe "
        "cache, so it is null until the first /health build of this process: a "
        "telemetry request must never fire the backend fan-out itself.")),
    "llm.backends": _k("dict", "llm", since=INTRODUCED_0_9_74,
                       note="empty when no backend is configured"),
    "llm.backends.*": _k("str", "llm", since=INTRODUCED_0_9_74),
    "llm.reserved[]": _k("list", "llm", since=INTRODUCED_0_9_74),
    "llm.oldest_inflight_age_s": _k("float|null", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "llm.suspect_wedged[]": _k("list", "llm", since=INTRODUCED_0_9_74),
    "llm.pool.*.weight": _k("float", "llm", since=INTRODUCED_0_9_74),
    "llm.pool.*.inflight": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.pool.*.routed": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.pool.*.routed_pct": _k("float", "llm", unit="_pct", since=INTRODUCED_0_9_74),
    "llm.pool.*.fails": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.pool.*.cooldown": _k("float", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "llm.pool.*.reserved": _k("bool", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.hits": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.misses": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.hit_rate": _k("float|null", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.hot_prefixes.*.backend": _k("str", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.hot_prefixes.*.hits": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.affinity.hot_prefixes": _k("dict", "llm", since=INTRODUCED_0_9_74,
                                    note="empty when no prefix is hot"),
    "llm.routing.routed_role_extract": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routed_role_extract_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routed_role_judge": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routed_role_judge_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_no_eligible_backend": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_no_eligible_backend_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_fit_rejected": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_fit_rejected_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_backend_at_capacity": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.routing.routing_backend_at_capacity_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.token_usage.*.tokens_prompt_total": _k("int", "llm", unit="_total", since=INTRODUCED_0_9_74),
    "llm.token_usage.*.tokens_completion_total": _k("int", "llm", unit="_total", since=INTRODUCED_0_9_74),
    "llm.token_usage.*.tokens_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.token_usage": _k("dict", "llm", since=INTRODUCED_0_9_74, note="empty when no backend is configured"),
    "llm.latency.*.requests_total": _k("int", "llm", unit="_total", since=INTRODUCED_0_9_74),
    "llm.latency.*.requests_failed_total": _k("int", "llm", unit="_total", since=INTRODUCED_0_9_74),
    "llm.latency.*.latency_sum_s": _k("float", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "llm.latency.*.latency_max_s": _k("float", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "llm.latency.*.latency_last_ts": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.latency": _k("dict", "llm", since=INTRODUCED_0_9_74, note="empty when no backend is configured"),
    "llm.faults": _k("dict", "llm", since=INTRODUCED_0_9_74, note="empty until a fault occurs"),
    "llm.faults.*.gateway.count": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.faults.*.gateway.last": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm.faults.*.llm.credential.count": _k("int", "credentials", since=INTRODUCED_0_9_74),
    "llm.faults.*.llm.credential.last": _k("str|null", "credentials", since=INTRODUCED_0_9_74),
    "llm.faults.*.llm.transient.count": _k("int", "llm", since=INTRODUCED_0_9_74),
    "llm.faults.*.llm.transient.last": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "llm_faults": _k("dict", "llm", moved_to="telemetry:llm.faults", removed_in=DUAL_EMIT_DROP_TARGET,
                     note="empty until a fault occurs"),
    "llm_faults.*.gateway.count": _k("int", "llm",
                                     moved_to="telemetry:llm.faults.*.gateway.count",
                                     removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_faults.*.gateway.last": _k("str|null", "llm",
                                    moved_to="telemetry:llm.faults.*.gateway.last",
                                    removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_faults.*.llm.credential.count": _k(
        "int", "credentials",
        moved_to="telemetry:llm.faults.*.llm.credential.count", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_faults.*.llm.credential.last": _k(
        "str|null", "credentials",
        moved_to="telemetry:llm.faults.*.llm.credential.last", removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_faults.*.llm.transient.count": _k(
        "int", "llm", moved_to="telemetry:llm.faults.*.llm.transient.count",
        removed_in=DUAL_EMIT_DROP_TARGET),
    "llm_faults.*.llm.transient.last": _k(
        "str|null", "llm", moved_to="telemetry:llm.faults.*.llm.transient.last",
        removed_in=DUAL_EMIT_DROP_TARGET),

    # ── gpu_probe / capacity / config / axes (moved off /health) ────────────
    "gpu_probe.state": _k("str", "llm", since=INTRODUCED_0_9_74),
    "gpu_probe.consecutive_hangs": _k("int", "llm", since=INTRODUCED_0_9_74),
    "gpu_probe.leaked_children": _k("int", "llm", since=INTRODUCED_0_9_74),
    "gpu_probe": _k("null", "llm", since=INTRODUCED_0_9_74, note="null until the first probe"),
    "capacity": _k("null", "capacity", since=INTRODUCED_0_9_74,
                   note="null until the first derivation of this deployment's lifetime"),
    "capacity.timestamp": _k("str", "capacity", since=INTRODUCED_0_9_74),
    "capacity.trigger": _k("str", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.hardware.nproc": _k("int", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.hardware.mem_total_bytes": _k("int", "capacity",
                                                        unit="_bytes", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.hardware.gpu_present": _k("bool", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.rerank_max_doc_chars": _k(
        "int", "capacity", unit="_chars", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.search_candidate_floor": _k(
        "int", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.embedder_url": _k("str", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.reranker_url": _k("str", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.cpu_encoder_replicas": _k(
        "str|int|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.fingerprint.encoder_config.gpu_encoder_replicas": _k(
        "str|int|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.reranker_chars_per_s": _k("int|float|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.reranker_status": _k("str|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.embedder_chars_per_s": _k("int|float|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.probed_at": _k("str|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.reranker_measured_at": _k("str|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.embedder_measured_at": _k("str|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.probe.probe_stale": _k("bool", "capacity", since=INTRODUCED_0_9_74),
    "capacity.derived.s_mean_s": _k("float|null", "capacity", unit="_s", since=INTRODUCED_0_9_74),
    "capacity.derived.s_max_measured_s": _k("float|null", "capacity", unit="_s", since=INTRODUCED_0_9_74),
    "capacity.derived.s_mean_measured_s": _k("float|null", "capacity", unit="_s", since=INTRODUCED_0_9_74),
    "capacity.derived.payload_basis": _k("str", "capacity", since=INTRODUCED_0_9_74),
    "capacity.derived.payload_basis_sample_count": _k("int", "capacity", since=INTRODUCED_0_9_74),
    "capacity.derived.payload_mean_chars_measured": _k(
        "int|float|null", "capacity", unit="_chars", since=INTRODUCED_0_9_74),
    "capacity.derived.payload_max_chars_measured": _k(
        "int|null", "capacity", unit="_chars", since=INTRODUCED_0_9_74),
    "capacity.derived.client_ceiling_s": _k("float|null", "capacity", unit="_s", since=INTRODUCED_0_9_74),
    "capacity.derived.queue_bound": _k("int|null", "capacity", since=INTRODUCED_0_9_74),
    "capacity.derived.tolerable_wait_s": _k("float|null", "capacity", unit="_s", since=INTRODUCED_0_9_74),
    "capacity.derived.single_search_exceeds_wait": _k("bool", "capacity", since=INTRODUCED_0_9_74),
    "capacity.derived.recommended_reranker_mem_limit_bytes": _k(
        "int|null", "capacity", unit="_bytes", since=INTRODUCED_0_9_74),
    "config.llm_backends[]": _k("list", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].url": _k("str", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].weight": _k("float", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].has_credential": _k("bool", "credentials", since=INTRODUCED_0_9_74),
    "config.llm_backends[].model": _k("str|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].roles": _k("list|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].n_ctx": _k("int|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].private_ok": _k("bool", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].max_inflight": _k("int|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].price_per_mtok_in": _k("float|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_backends[].price_per_mtok_out": _k("float|null", "llm", since=INTRODUCED_0_9_74),
    "config.llm_pool_tuning.fail_threshold": _k("int", "llm", since=INTRODUCED_0_9_74),
    "config.llm_pool_tuning.fail_window_s": _k("float|int", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "config.llm_pool_tuning.cooldown_s": _k("float|int", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "config.llm_pool_tuning.max_tries": _k("int", "llm", since=INTRODUCED_0_9_74),
    "config.llm_affinity.prefix_chars": _k("int", "llm", unit="_chars", since=INTRODUCED_0_9_74),
    "config.llm_affinity.ttl_s": _k("float|int", "llm", unit="_s", since=INTRODUCED_0_9_74),
    "config.llm_affinity.max_inflight": _k("int", "llm", since=INTRODUCED_0_9_74),
    "config.embed_max_chars": _k("int", "encoders", unit="_chars", since=INTRODUCED_0_9_74),
    "config.allow_unauthenticated_provider_keys": _k("bool", "credentials", since=INTRODUCED_0_9_74),
    "axes.project_identity.nodes": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.project_identity.unidentified": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.project_identity.mismatched": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.project_identity.unregistered": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.project_identity.complete": _k("bool", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.project_identity": _k("null", "axes/registry", since=INTRODUCED_0_9_74,
                                note="null until the first refresher pass"),
    "axes.domain_identity.nodes": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity.registry_rows": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity.unregistered": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity.mismatched": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity.unattached": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity.complete": _k("bool", "axes/registry", since=INTRODUCED_0_9_74),
    "axes.domain_identity": _k("null", "axes/registry", since=INTRODUCED_0_9_74,
                               note="null until the first refresher pass"),

    # ── nrem / consolidation ────────────────────────────────────────────────
    "nrem.fact_cycles": _k("int", "nrem/consolidation"),
    "nrem.decision_cycles": _k("int", "insight"),
    "nrem.total_cycles": _k("int", "nrem/consolidation"),
    "nrem.fact_threshold": _k("int", "nrem/consolidation",
                              note="ONT.density_threshold"),
    "nrem.as_of": _k("str|null", "nrem/consolidation", since=INTRODUCED_0_9_74, note=(
        "when the 60 s refresher last computed this section. MEASURED 2026-08-28 "
        "on this corpus: the insight walk is 149 SEQUENTIAL Neo4j round-trips "
        "(8 gating groups x 9-26 BFS layers each, unbounded by construction — "
        "the walk has no hop cap), so it moved out of the request path.")),
    "nrem.error": _k("str", "nrem/consolidation",
                     note="present only when the refresher's last pass failed"),

    # ── breakdown ───────────────────────────────────────────────────────────
    "breakdown.record_types[]": _k("list", "spine"),
    "breakdown.record_types[].key": _k("str", "spine"),
    "breakdown.record_types[].count": _k("int", "spine"),
    "breakdown.agents[]": _k("list", "spine"),
    "breakdown.agents[].key": _k("str", "spine"),
    "breakdown.agents[].count": _k("int", "spine"),
    "breakdown.sources[]": _k("list", "spine"),
    "breakdown.sources[].key": _k("str", "spine"),
    "breakdown.sources[].count": _k("int", "spine"),
    "breakdown.projects[]": _k("list", "axes/registry", since=INTRODUCED_0_9_74,
                               note="the PROJECT distribution, under its true name"),
    "breakdown.projects[].key": _k("str", "axes/registry", since=INTRODUCED_0_9_74),
    "breakdown.projects[].count": _k("int", "axes/registry", since=INTRODUCED_0_9_74),
    "breakdown.domains[]": _k("list", "axes/registry", note=(
        "⚠ MEANING CHANGED IN 0.9.74 — see _meaning_changes. Before 0.9.74 this "
        "carried the PROJECT distribution; it now carries the DOMAIN "
        "distribution from metadata->'domains'. The project distribution is "
        "breakdown.projects.")),
    "breakdown.domains[].key": _k("str", "axes/registry"),
    "breakdown.domains[].count": _k("int", "axes/registry"),
    "breakdown.records_with_domains": _k("int", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "how many records carry a non-empty `domains` array — the DENOMINATOR "
        "for breakdown.domains. Live 2026-08-28: 629 of 1691, so 62.8% of the "
        "corpus carries none and the distribution describes a 37% subset")),
    "breakdown.records_total": _k("int", "axes/registry", since=INTRODUCED_0_9_74, note=(
        "records in technical_docs, so the coverage above can be read as a "
        "fraction without a second query")),
    "breakdown.summaries[]": _k("list", "nrem/consolidation"),
    "breakdown.summaries[].kind": _k("str", "nrem/consolidation"),
    "breakdown.summaries[].superseded": _k("int", "nrem/consolidation"),
    "breakdown.summaries[].active": _k("int", "nrem/consolidation"),
    "breakdown.error": _k("str", "spine",
                          note="present only when this section's own query failed"),

    # ── entity graph ────────────────────────────────────────────────────────
    "entity_graph.entities_total": _k("int", "graph"),
    "entity_graph.orphan_entities": _k("int", "graph"),
    "entity_graph.unmentioned_entities": _k("int", "graph"),
    "entity_graph.singleton_entities": _k("int", "graph"),
    "entity_graph.genuinely_referenced_entities": _k("int", "graph"),
    "entity_graph.top_hubs[]": _k("list", "graph"),
    "entity_graph.top_hubs[].name": _k("str", "graph"),
    "entity_graph.top_hubs[].degree": _k("int", "graph"),
    "entity_graph.error": _k("str", "graph",
                             note="present only when this section's own query failed"),

    # ── compliance / integrity ──────────────────────────────────────────────
    "compliance.predicate_distribution": _k("dict", "graph"),
    "compliance.predicate_distribution.*": _k("int", "graph", note=(
        "a CENSUS of what is in the graph, not of what the ontology allows — a "
        "legacy predicate (ALIASES) shows here for as long as edges exist")),
    "compliance.label_compliance": _k("str", "graph", note="ok | non-compliant"),
    "compliance.invalid_labels[]": _k("list", "graph"),
    "compliance.invalid_labels[].name": _k("str", "graph"),
    "compliance.invalid_labels[].count": _k("int", "graph"),
    "compliance.relationship_compliance": _k("str", "graph"),
    "compliance.invalid_relationships[]": _k("list", "graph"),
    "compliance.invalid_relationships[].name": _k("str", "graph"),
    "compliance.invalid_relationships[].count": _k("int", "graph"),
    "compliance.error": _k("str", "graph",
                           note="present only when this section's own query failed"),
    "graph_integrity.invalid_nodes": _k("int", "graph"),
    "graph_integrity.by_reason": _k("dict", "graph", note="empty when clean"),
    "graph_integrity.by_reason.*": _k("int", "graph"),
    "graph_integrity.by_label": _k("dict", "graph", note="empty when clean"),
    "graph_integrity.by_label.*": _k("int", "graph"),
    "graph_integrity.clean": _k("bool", "graph"),
    "graph_integrity.error": _k("str", "graph",
                                note="present only when this section's own query failed"),

    # ── consolidation rollup ────────────────────────────────────────────────
    "consolidation.stall_threshold_seconds": _k("int", "nrem/consolidation",
                                                unit="_seconds"),
    "consolidation.stalled": _k("bool", "nrem/consolidation"),
    "consolidation.stalled_types[]": _k("list", "nrem/consolidation"),
    "consolidation.last_success_age_seconds": _k("int|null", "nrem/consolidation",
                                                 unit="_seconds"),
    "consolidation.last_success_cycle_type": _k("str|null", "nrem/consolidation"),
    "consolidation.last_outcome": _k("str|null", "nrem/consolidation"),
    "consolidation.last_deferred_reason": _k("str|null", "nrem/consolidation"),
    "consolidation.last_active_cycle_type": _k("str|null", "nrem/consolidation"),
    "consolidation.error": _k("str", "nrem/consolidation",
                              note="present only when this section's own query failed"),
    "consolidation.*.last_outcome": _k("str|null", "nrem/consolidation", note=(
        "per cycle type — `insight` and `fact_consolidation` today")),
    "consolidation.*.last_success_age_seconds": _k("int|null", "nrem/consolidation",
                                                   unit="_seconds"),
    "consolidation.*.in_flight": _k("bool", "nrem/consolidation"),
    "consolidation.*.consecutive_failures": _k("int", "nrem/consolidation"),
    "consolidation.*.backlog": _k("int", "nrem/consolidation"),
    "consolidation.*.stalled": _k("bool", "nrem/consolidation"),
    "consolidation.*.last_error.class": _k("str|null", "nrem/consolidation"),
    "consolidation.*.last_error.msg": _k("str|null", "nrem/consolidation"),
    "consolidation.*.last_error.age_seconds": _k("int|null", "nrem/consolidation",
                                                 unit="_seconds"),
    "consolidation.*.last_error.superseded": _k("bool", "nrem/consolidation"),
    "consolidation.*.last_error": _k("null", "nrem/consolidation",
                                     note="null when no error is on record"),
    "consolidation.*.eligible_clusters": _k("int", "nrem/consolidation"),
    "consolidation.*.eligible_oldest_age_seconds": _k("int|null",
                                                      "nrem/consolidation",
                                                      unit="_seconds"),
    "consolidation.*.dead_lettered_clusters": _k("int", "nrem/consolidation"),
    "consolidation.*.unchanged_clusters": _k("int", "nrem/consolidation"),
    "consolidation.*.singleton_clusters": _k("int", "nrem/consolidation"),
    "consolidation.*.truncation_failures": _k("int", "nrem/consolidation"),
    "consolidation.*.slot_failures": _k("int", "nrem/consolidation"),
    "consolidation.*.last_deferred_reason": _k("str|null", "nrem/consolidation"),
    "consolidation.*.cycle_seconds_avg": _k("float|null", "nrem/consolidation",
                                            unit="_seconds"),
    "consolidation.*.runs_24h": _k("int", "nrem/consolidation"),
    "consolidation.*.deferred_24h": _k("int", "nrem/consolidation"),
    "consolidation.*.idle_24h": _k("int", "nrem/consolidation"),
    "consolidation.*.folds_succeeded_24h": _k("int", "nrem/consolidation",
                                              log="health.nrem_daemon"),
    "consolidation.*.folds_attempted_24h": _k("int", "nrem/consolidation",
                                              log="health.nrem_daemon"),
    "consolidation.*.truncation_failures_24h": _k("int", "nrem/consolidation"),
    "consolidation.*.slot_failures_24h": _k("int", "nrem/consolidation"),
    "consolidation.*.last_started": _k("str|null", "nrem/consolidation"),

    # ── refold ledger ───────────────────────────────────────────────────────
    "refold_ledger.by_status_reason[]": _k("list", "insight"),
    "refold_ledger.by_status_reason[].status": _k("str", "insight"),
    "refold_ledger.by_status_reason[].closed_reason": _k("str|null", "insight"),
    "refold_ledger.by_status_reason[].count": _k("int", "insight"),
    "refold_ledger.by_trigger_kind": _k("dict", "insight"),
    "refold_ledger.by_trigger_kind.*": _k("int", "insight"),
    "refold_ledger.insight_reconciliation_stuck": _k("int", "insight"),
    "refold_ledger.error": _k("str", "insight",
                              note="present only when this section's own query failed"),

    # ── spine ───────────────────────────────────────────────────────────────
    "spine.decisions.total": _k("int", "spine"),
    "spine.decisions.grounded_in_pct": _k("float", "spine", unit="_pct"),
    "spine.decisions.alternatives_pct": _k("float", "spine", unit="_pct"),
    "spine.decisions.confidence_pct": _k("float", "spine", unit="_pct"),
    "spine.decisions.elicited_pct": _k("float", "spine", unit="_pct"),
    "spine.alternative_vectors.entries": _k("int", "spine"),
    "spine.alternative_vectors.decisions": _k("int", "spine"),
    "spine.alternative_vectors.embedded": _k("int", "spine"),
    "spine.alternative_vectors.pending": _k("int", "spine"),
    "spine.alternative_vectors.failing": _k("int", "spine"),
    "spine.alternative_vectors.embedded_pct": _k("float", "spine", unit="_pct"),
    "spine.alternative_vectors.oldest_pending_age_s": _k("int|float|null", "spine",
                                                         unit="_s"),
    "spine.facts.total": _k("int", "spine"),
    "spine.facts.source_ref_pct": _k("float", "spine", unit="_pct"),
    "spine.facts.elicited_pct": _k("float", "spine", unit="_pct"),
    "spine.retrospectives.total": _k("int", "spine"),
    "spine.retrospectives.rating_pct": _k("float", "spine", unit="_pct"),
    "spine.retrospectives.target_pg_id_pct": _k("float", "spine", unit="_pct"),
    "spine.retrospectives.grounded_in_pct": _k("float", "spine", unit="_pct"),
    "spine.retrospectives.elicited_pct": _k("float", "spine", unit="_pct"),
    "spine.emergent_unprojected_fields[]": _k("list", "spine"),
    "spine.emergent_unprojected_fields[].key": _k("str", "spine"),
    "spine.emergent_unprojected_fields[].n": _k("int", "spine"),
    "spine.error": _k("str", "spine",
                      note="present only when this section's own query failed"),

    # ── latency ─────────────────────────────────────────────────────────────
    "latency.rem_ms.note": _k("str", "rem"),
    "latency.rem_ms.by_model[]": _k("list", "rem"),
    "latency.rem_ms.by_model[].model": _k("str|null", "rem"),
    "latency.rem_ms.by_model[].n": _k("int", "rem"),
    "latency.rem_ms.by_model[].n_service": _k("int", "rem"),
    "latency.rem_ms.by_model[].max_batch_size": _k("int|null", "rem"),
    "latency.rem_ms.by_model[].backend": _k("str|null", "rem"),
    "latency.rem_ms.by_model[].timing_source": _k("str", "rem",
                                                  note="server | wall | mixed"),
    "latency.rem_ms.by_model[].service_ms.p50": _k("float|null", "rem", unit="_ms"),
    "latency.rem_ms.by_model[].service_ms.p95": _k("float|null", "rem", unit="_ms"),
    "latency.rem_ms.by_model[].contention_ms.p50": _k("float|null", "rem", unit="_ms"),
    "latency.rem_ms.by_model[].contention_ms.p95": _k("float|null", "rem", unit="_ms"),
    "latency.rem_ms.by_model[].wall_ms.p50": _k("float|null", "rem", unit="_ms"),
    "latency.rem_ms.by_model[].wall_ms.p95": _k("float|null", "rem", unit="_ms"),
    "latency.nrem_cycle_seconds.window_days": _k("int", "nrem/consolidation"),
    "latency.nrem_cycle_seconds.n": _k("int", "nrem/consolidation"),
    "latency.nrem_cycle_seconds.p50": _k("float|null", "nrem/consolidation", unit="_seconds"),
    "latency.nrem_cycle_seconds.p95": _k("float|null", "nrem/consolidation", unit="_seconds"),
    "latency.nrem_cycle_seconds.note": _k("str", "nrem/consolidation"),
    "latency.nrem_cycle_seconds": _k("dict", "nrem/consolidation",
                                     note="empty dict when no cycle has finished"),
    "latency.error": _k("str", "rem",
                        note="present only when this section's own query failed"),

    # ── rerank counters (flat, fact:1314 shape) ─────────────────────────────
    "rerank_successes_total": _k("int", "encoders", unit="_total"),
    "rerank_fallbacks_total": _k("int", "encoders", unit="_total"),
    "rerank_fallbacks_last_ts": _k("str|null", "encoders"),
    "rerank_payload_chars_total": _k("int", "encoders", unit="_total"),
    "rerank_payload_docs_total": _k("int", "encoders", unit="_total"),
    "rerank_payload_chars_max": _k("int", "encoders", unit="_chars"),

    # ── axes registry read failures (flat) ──────────────────────────────────
    "axis_registry_read_failures_total": _k(
        "int", "axes/registry", unit="_total",
        moved_to="telemetry:registry.read_failures_total", removed_in=DUAL_EMIT_DROP_TARGET),
    "axis_registry_read_failures_last_ts": _k("str|null", "axes/registry"),

    # ── credentials ─────────────────────────────────────────────────────────
    "credentials.token_verify_failed": _k("int", "credentials",
                                          log="health.warning.token_verify_failed"),
    "credentials.token_verify_failed_last_ts": _k("str|null", "credentials"),
    "credentials.daemon_tokens_issued": _k("int", "credentials"),
    "credentials.daemon_tokens_issued_last_ts": _k("str|null", "credentials"),
    "credentials.credentialed_route_denied": _k("int", "credentials"),
    "credentials.credentialed_route_denied_last_ts": _k("str|null", "credentials"),
    "credentials.audit_log_dropped": _k("int", "credentials"),
    "credentials.audit_log_dropped_last_ts": _k("str|null", "credentials"),
    "credentials.token_verify_warn_per_min": _k(
        "int|float", "credentials", since=INTRODUCED_0_9_74,
        note="TOKEN_VERIFY_WARN_PER_MIN — the limit the warning is raised at"),
}


# ═══════════════════════════════════════════════════════════════════════════════
# MEANING CHANGES — fact:1626: a key whose VALUE changes meaning while its NAME
# stays is ENUMERATED, never left to be discovered. A consumer reading only the
# key list would see nothing wrong here; that is exactly why this list exists.
# ═══════════════════════════════════════════════════════════════════════════════
MEANING_CHANGES: tuple[dict, ...] = (
    {
        "endpoint": "telemetry",
        "path": "breakdown.domains",
        "in_version": INTRODUCED_0_9_74,
        "was": "the PROJECT distribution (it was built from PROJECT_SQL)",
        "now": "the DOMAIN distribution, from metadata->'domains'",
        "action": "read breakdown.projects for the old value",
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "role",
        "in_version": INTRODUCED_0_9_74,
        "was": "two-valued: read | write — an admin token reported `write`",
        "now": "three-valued: read | write | admin",
        "action": ("a consumer testing `role == 'write'` for may-I-save now "
                   "correctly excludes an admin token, which cannot save"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "llm_backends.*",
        "in_version": INTRODUCED_0_9_74,
        "was": ("a 401/403 from a CREDENTIALED backend was reported `ok` — the "
                "bare probe carries no key, so the rejection was read as "
                "'the server answered'"),
        "now": "http_401 / http_403 for that backend, and it counts as down",
        "action": ("expect a credentialed backend that rejects the liveness "
                   "probe to read down rather than ok"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "status",
        "in_version": INTRODUCED_0_9_74,
        "was": "ok | degraded — degraded iff embedder or reranker was not ok",
        "now": ("ok | degraded | down, derived from `dependencies` and "
                "`warnings`; a degraded encoder, a failed outbox row, a REM "
                "dead-letter or an unreadable registry all reach it"),
        "action": ("⛔ THE HTTP CODE IS UNCHANGED — 503 still means exactly "
                   "'embedder or reranker is down'. A consumer that inferred "
                   "the code from the enum must now read the code itself."),
        "shape_changed": False,
    },
    # ── W2 (decision:1832) — visibility before behaviour. FROZEN at
    # INTRODUCED_0_9_79 (handback H1) — VERSION is the fifth version pin and
    # moves every release; pinning these three historical entries to the
    # bare VERSION constant would falsify them at the very next bump.
    {
        "endpoint": "health",
        "path": "dependencies.llm_pool.state",
        "in_version": INTRODUCED_0_9_79,
        "was": ("`ok` whenever a probed backend answered — including a "
                "zero-config install where NOTHING was declared and the "
                "built-in fallback (LLM_DEFAULT_TARGET) happened to be "
                "serving, and including a fleet where every backend answers "
                "but NONE is eligible for any traffic class (role+privacy "
                "empty for role-less traffic and every ROUTING_ROLE_NAMES "
                "role — fit is NOT evaluated here, the check runs at 0/0 "
                "tokens, so it is vacuous; this is visibility, not a fit "
                "gate)"),
        "now": ("`degraded` in both of those cases, each with its own "
                "reason. Liveness is also now checked BEFORE configuration: "
                "every probed backend down reads `down` unconditionally — a "
                "config-empty or fallback-exclusion reason no longer softens "
                "it to `degraded` the way it could before"),
        "action": ("a consumer treating `ok` as 'nothing to look at' on "
                   "llm_pool must now read `reason` — an undeclared fleet "
                   "and a fleet-wide eligibility hole both surface here for "
                   "the first time"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "dependencies.rem_daemon.state",
        "in_version": INTRODUCED_0_9_79,
        "was": ("`ok` whenever the REM process was running and dead-letters "
                "were zero — a fleet where NO backend counts toward "
                "/pool/status free_slots (warn_if_dream_slots_impossible's "
                "own condition) read `ok` while REM structurally never ran "
                "a single job"),
        "now": ("`degraded`, naming the same fact the startup warning "
                "already logs ('no backend counts toward dream slots...'); "
                "appended after a dead-letter reason when both apply"),
        "action": ("a consumer alerting on rem_daemon != ok for the first "
                   "time will see this reason on any fleet with a "
                   "partial-role or private_ok=false-only configuration — "
                   "the daemon's own PID was never the problem"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "dependencies.nrem_daemon.state",
        "in_version": INTRODUCED_0_9_79,
        "was": ("the same dream-slots-impossible fleet read `ok` (or "
                "`unknown` before the first consolidation snapshot) with no "
                "indication NREM could never run either"),
        "now": ("`degraded` with the same reason — and because the "
                "condition is a config fact knowable before any probe, it "
                "now WINS OVER the `unknown` 'not yet probed' state rather "
                "than waiting behind it: not-yet-probed AND slots-impossible "
                "together read `degraded`"),
        "action": ("a consumer that treated nrem_daemon:unknown as merely "
                   "'still starting up' must check whether a `reason` is "
                   "already present even during that window"),
        "shape_changed": False,
    },
    # ── W4 default-deny (decision:1824) — pinned to the bare `VERSION`
    # constant for now, same as the W2 entries above were AT THE TIME they
    # were authored (see INTRODUCED_0_9_79's own comment): this build has
    # not been released yet, so there is no frozen stamp to name — the
    # merger's version-bump lands VERSION at the assigned release number in
    # the SAME commit this file ships in, which is exactly when these four
    # should be frozen into a new INTRODUCED_0_9_8x constant (mirroring how
    # INTRODUCED_0_9_79 was carved out) so a LATER bump cannot re-date them.
    {
        "endpoint": "health",
        "path": "config.llm_backends[].private_ok",
        "in_version": INTRODUCED_0_9_81,
        "was": ("default TRUE for an uncredentialed backend, FALSE for a "
                "credentialed one (the absent-key case) — `false` on this "
                "field meant the operator had EITHER explicitly scoped a "
                "backend away from role-less traffic, or simply attached a "
                "provider token and never answered the M-5 access choice"),
        "now": ("default FALSE unconditionally, credentialed or not — "
                "`false` now means UNDECLARED (no explicit private_ok/roles "
                "was ever stated for this backend), not 'operator scoped "
                "away'; an explicit `true` or `false` still always wins over "
                "the default either way"),
        "action": ("a consumer reading `private_ok: false` as 'the operator "
                   "chose to restrict this backend' must now also read "
                   "`private_ok_explicit`-adjacent context (check_config.py's "
                   "per-backend census, or the absence of `roles`) to tell "
                   "an undeclared backend apart from a deliberately-scoped "
                   "one — the bare bool no longer carries that distinction"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        # W4 default-deny (decision:1824): a SECOND, distinct meaning change
        # on the SAME field this wave — the "(legacy-CSV population)" suffix
        # keeps this entry's key distinct from the immutable, frozen W2
        # entry above (test_the_meaning_change_list_covers_every_re_pointed_
        # key pins THAT one to INTRODUCED_0_9_79 forever) rather than
        # colliding with it in the by_path lookup and silently clobbering it.
        "path": "dependencies.llm_pool.state (legacy-CSV population)",
        "in_version": INTRODUCED_0_9_81,
        "was": ("`ok` for a live legacy `LLM_BACKENDS` CSV (or the bare "
                "`LLM_DEFAULT_TARGET` fallback) serving role-less traffic — "
                "a descriptor-less fleet was eligible for everything (I-5a)"),
        "now": ("`degraded` for the same population — a descriptor-less "
                "fleet is now eligible for NOTHING under default-deny, "
                "surfaced by the widened `_all_roles_ineligible` trigger; "
                "measured: `test_declared_fleet_healthy_still_reads_ok_"
                "unchanged` flips ok→degraded for exactly this shape"),
        "action": ("a consumer that treated a live legacy-CSV install as "
                   "healthy-by-construction must now read `reason` — it "
                   "names the fix (declare `LLM_BACKENDS_JSON` explicitly) "
                   "rather than the install being silently fine"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "dependencies.rem_daemon.state / dependencies.nrem_daemon.state",
        "in_version": INTRODUCED_0_9_81,
        "was": ("the 0.9.79 `degraded` reason (no backend counts toward "
                "/pool/status free_slots) fired only for a genuinely "
                "partial-role or private_ok=false-only configuration — a "
                "deliberate, already-narrow population"),
        "now": ("the SAME reason string now also fires for every UNDECLARED "
                "fleet (legacy CSV, bare `LLM_DEFAULT_TARGET`, or an "
                "explicitly-declared-but-never-opted-in JSON fleet) — W4's "
                "default-deny flip means none of them count a free dream "
                "slot any more either, widening the population this reason "
                "composes for without changing the reason's own wording"),
        "action": ("a consumer alerting on rem_daemon/nrem_daemon `degraded` "
                   "will now see this reason far more often post-upgrade — "
                   "it is the expected, honest surfacing of a fleet that "
                   "was silently never dreaming before, not a regression"),
        "shape_changed": False,
    },
    {
        "endpoint": "health",
        "path": "dependencies.llm_pool.reason",
        "in_version": INTRODUCED_0_9_81,
        "was": ("the config-empty reason and `_all_roles_ineligible`'s "
                "reason each named only the bare fact (no backend declared / "
                "configured but ineligible), with no remedy"),
        "now": ("both reason strings, when a legacy declaration key "
                "(`LLM_BACKENDS` CSV or bare `LLM_DEFAULT_TARGET`) is "
                "present, APPEND a remedy clause naming `check_config.py` "
                "and \"declare LLM_BACKENDS_JSON explicitly\" (§6.5, "
                "decision:1824) — deliberately NOT `migrate_env.py`, whose "
                "same-generation planning gate (§6.4) means it would "
                "correctly no-op for this population at runtime"),
        "action": ("a consumer doing exact-string matching on either reason "
                   "must now match a PREFIX, not the whole string — the "
                   "remedy clause is additive text, appended, never a "
                   "replacement of the original fact"),
        "shape_changed": False,
    },
)

#: Keys REMOVED outright in 0.9.74 (not moved) — each had no writer and had
#: therefore read 0 since it shipped. Listed so a consumer knows the difference
#: between "gone" and "moved", and so the removal is auditable.
REMOVED_IN_0_9_74: tuple[dict, ...] = (
    {"endpoint": "telemetry", "path": "entity_graph.alias_edges",
     "reason": "no writer — the ALIASES relationship was never emitted by any "
               "code path, and the name collides with the live alias TABLES"},
    {"endpoint": "telemetry", "path": "entity_graph.alias_covered_entities",
     "reason": "same writer-less ALIASES relationship"},
    {"endpoint": "telemetry", "path": "entity_graph.alias_components",
     "reason": "read Entity.alias_component, which the retired gds.wcc caller "
               "was the only writer of"},
    {"endpoint": "telemetry", "path": "entity_graph.largest_alias_component",
     "reason": "same retired gds.wcc stamp"},
)


# ═══════════════════════════════════════════════════════════════════════════════
# CONDITIONAL KEYS — the exemption list for the "every documented key is
# emitted" direction, in ONE visible place rather than a flag scattered through
# 550 entries, so the list can be read as a whole and argued with.
#
# ⚠ THIS LIST IS THE ONLY WAY A KEY ESCAPES THE COMPLETENESS CHECK, so adding to
# it is the cheap way to make a failing contract test pass — and that is exactly
# what it must not become. Every entry below is one of three kinds:
#
#   (a) IDENTITY — present only for an authenticated caller.
#   (b) OVERRIDE / CONDITION — present only while a specific condition holds
#       (an S-05 override in effect, a wedged backend, an error branch).
#   (c) SHAPE ALTERNATIVE — a container that may be EMPTY. An empty dict emits
#       the container path; a populated one emits its `*` children instead. One
#       payload can only ever show one of the two, so both are exempt; the
#       OTHER direction (nothing undocumented is emitted) still pins both.
# ═══════════════════════════════════════════════════════════════════════════════
CONDITIONAL: frozenset = frozenset({
    # (a) identity — authenticated callers only
    "health:agent",
    "health:role",
    # (b) condition-gated
    "health:llm_reserved[]",
    "health:llm_oldest_inflight_age_s",
    "health:llm_suspect_wedged[]",
    "health:config.allow_unauthenticated_provider_keys",
    "telemetry:config.allow_unauthenticated_provider_keys",
    "telemetry:llm.reserved[]",
    "telemetry:llm.suspect_wedged[]",
    "telemetry:llm.oldest_inflight_age_s",
    # (b) per-section error branches — present ONLY when that section's own
    # query failed, which is the whole point of computing sections independently
    "telemetry:postgres.error",
    "telemetry:neo4j.error",
    "telemetry:outbox.error",
    "telemetry:rem.error",
    "telemetry:nrem.error",
    "telemetry:registry.error",
    "telemetry:breakdown.error",
    "telemetry:entity_graph.error",
    "telemetry:compliance.error",
    "telemetry:graph_integrity.error",
    "telemetry:consolidation.error",
    "telemetry:refold_ledger.error",
    "telemetry:spine.error",
    "telemetry:latency.error",
    # (c) shape alternatives — container-vs-children
    "telemetry:gpu_probe",
    "telemetry:gpu_probe.state",
    "telemetry:gpu_probe.consecutive_hangs",
    "telemetry:gpu_probe.leaked_children",
    "telemetry:capacity",
    "telemetry:axes.project_identity",
    "telemetry:axes.domain_identity",
    "telemetry:postgres.outbox",
    "telemetry:postgres.outbox.*",
    "telemetry:graph_integrity.by_reason",
    "telemetry:graph_integrity.by_reason.*",
    "telemetry:graph_integrity.by_label",
    "telemetry:graph_integrity.by_label.*",
    "telemetry:llm.faults",
    "telemetry:llm.faults.*.gateway.count",
    "telemetry:llm.faults.*.gateway.last",
    "telemetry:llm.faults.*.llm.credential.count",
    "telemetry:llm.faults.*.llm.credential.last",
    "telemetry:llm.faults.*.llm.transient.count",
    "telemetry:llm.faults.*.llm.transient.last",
    "telemetry:llm_faults",
    "telemetry:llm_faults.*.gateway.count",
    "telemetry:llm_faults.*.gateway.last",
    "telemetry:llm_faults.*.llm.credential.count",
    "telemetry:llm_faults.*.llm.credential.last",
    "telemetry:llm_faults.*.llm.transient.count",
    "telemetry:llm_faults.*.llm.transient.last",
    "telemetry:llm.affinity.hot_prefixes",
    "telemetry:llm.affinity.hot_prefixes.*.backend",
    "telemetry:llm.affinity.hot_prefixes.*.hits",
    "telemetry:llm.token_usage",
    "telemetry:llm.latency",
    "telemetry:clients.versions_seen",
    "telemetry:clients.versions_seen.*",
    "telemetry:latency.nrem_cycle_seconds",
    "telemetry:consolidation.*.last_error",
    "telemetry:refold_ledger.by_trigger_kind",
    "telemetry:refold_ledger.by_trigger_kind.*",
    "telemetry:compliance.predicate_distribution",
    "telemetry:compliance.predicate_distribution.*",
    "health:llm_affinity.hot_prefixes",
    "telemetry:llm.backends",
    "telemetry:llm.backends.*",
    "health:llm_affinity.hot_prefixes.*.backend",
    "health:llm_affinity.hot_prefixes.*.hits",
    "health:consolidation.stalled_types[]",
    "telemetry:consolidation.stalled_types[]",
    "telemetry:breakdown.summaries[].kind",
    "telemetry:breakdown.summaries[].superseded",
    "telemetry:breakdown.summaries[].active",
    "telemetry:compliance.invalid_labels[].name",
    "telemetry:compliance.invalid_labels[].count",
    "telemetry:compliance.invalid_relationships[].name",
    "telemetry:compliance.invalid_relationships[].count",
    "telemetry:entity_graph.top_hubs[].name",
    "telemetry:entity_graph.top_hubs[].degree",
    "telemetry:spine.emergent_unprojected_fields[].key",
    "telemetry:spine.emergent_unprojected_fields[].n",
    "telemetry:refold_ledger.by_status_reason[].status",
    "telemetry:refold_ledger.by_status_reason[].closed_reason",
    "telemetry:refold_ledger.by_status_reason[].count",
    "telemetry:breakdown.record_types[].key",
    "telemetry:breakdown.record_types[].count",
    "telemetry:breakdown.agents[].key",
    "telemetry:breakdown.agents[].count",
    "telemetry:breakdown.sources[].key",
    "telemetry:breakdown.sources[].count",
    "telemetry:breakdown.projects[].key",
    "telemetry:breakdown.projects[].count",
    "telemetry:breakdown.domains[].key",
    "telemetry:breakdown.domains[].count",
    "telemetry:latency.rem_ms.by_model[].model",
    "telemetry:latency.rem_ms.by_model[].n",
    "telemetry:latency.rem_ms.by_model[].n_service",
    "telemetry:latency.rem_ms.by_model[].max_batch_size",
    "telemetry:latency.rem_ms.by_model[].backend",
    "telemetry:latency.rem_ms.by_model[].timing_source",
    "telemetry:latency.rem_ms.by_model[].service_ms.p50",
    "telemetry:latency.rem_ms.by_model[].service_ms.p95",
    "telemetry:latency.rem_ms.by_model[].contention_ms.p50",
    "telemetry:latency.rem_ms.by_model[].contention_ms.p95",
    "telemetry:latency.rem_ms.by_model[].wall_ms.p50",
    "telemetry:latency.rem_ms.by_model[].wall_ms.p95",
    "telemetry:latency.nrem_cycle_seconds.window_days",
    "telemetry:latency.nrem_cycle_seconds.n",
    "telemetry:latency.nrem_cycle_seconds.p50",
    "telemetry:latency.nrem_cycle_seconds.p95",
    "telemetry:latency.nrem_cycle_seconds.note",
    "health:config.llm_backends[].url",
    "health:config.llm_backends[].weight",
    "health:config.llm_backends[].has_credential",
    "health:config.llm_backends[].model",
    "health:config.llm_backends[].roles",
    "health:config.llm_backends[].n_ctx",
    "health:config.llm_backends[].private_ok",
    "health:config.llm_backends[].max_inflight",
    "health:config.llm_backends[].price_per_mtok_in",
    "health:config.llm_backends[].price_per_mtok_out",
    "telemetry:config.llm_backends[].url",
    "telemetry:config.llm_backends[].weight",
    "telemetry:config.llm_backends[].has_credential",
    "telemetry:config.llm_backends[].model",
    "telemetry:config.llm_backends[].roles",
    "telemetry:config.llm_backends[].n_ctx",
    "telemetry:config.llm_backends[].private_ok",
    "telemetry:config.llm_backends[].max_inflight",
    "telemetry:config.llm_backends[].price_per_mtok_in",
    "telemetry:config.llm_backends[].price_per_mtok_out",
    "health:warnings[].key",
    "health:warnings[].limit",
    "health:warnings[].observed",
    "health:warnings[].unit",
})


def required_paths(contract: dict, endpoint: str) -> set:
    """Documented paths that a fully-populated payload MUST emit."""
    return {p for p in contract if f"{endpoint}:{p}" not in CONDITIONAL}


# ═══════════════════════════════════════════════════════════════════════════════
# Walking a payload against the contract
# ═══════════════════════════════════════════════════════════════════════════════
_SCALAR_TYPE_NAMES = {
    str: "str", bool: "bool", int: "int", float: "float", type(None): "null",
}


def _json_type(value) -> str:
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    # bool BEFORE int — bool is a subclass of int and would otherwise be
    # reported as int, which would let a bool/int swap pass the type check.
    if isinstance(value, bool):
        return "bool"
    return _SCALAR_TYPE_NAMES.get(type(value), type(value).__name__)


def type_matches(value, spec: dict) -> bool:
    """True iff ``value``'s JSON type is one the spec allows.

    ``int`` satisfies a ``float`` declaration (JSON has one number type and a
    percentile that lands exactly on an integer serialises as one), but never
    the other way round — a float where an int is declared is a real defect.
    """
    actual = _json_type(value)
    allowed = spec["types"]
    if actual in allowed:
        return True
    return actual == "int" and "float" in allowed


def _trie(paths) -> dict:
    """Build a nested lookup from the contract's dotted paths.

    ⚠ Built from the CONTRACT's paths, never from a payload's — a dynamic map
    key can itself contain dots (an LLM backend key is a URL), so a payload path
    can never be recovered by splitting a joined string. ``walk_payload``
    descends this trie alongside the payload instead.
    """
    root: dict = {}
    for path in paths:
        node = root
        for seg in path.split("."):
            node = node.setdefault(seg, {})
    return root


def walk_payload(payload: dict, contract: dict) -> list[tuple[str, object]]:
    """Every (canonical path, value) pair a payload actually emits.

    A dict key is canonicalised to itself when the contract documents it
    literally, and to ``*`` when the contract documents a dynamic map at that
    position. An unknown key keeps its literal spelling so the caller can name
    it in a failure message.

    LEAVES ONLY, with one exception that is not really one: an EMPTY dict or
    list has no leaves, so the container itself is emitted — otherwise a section
    that legitimately reports "nothing here" would be invisible to the contract
    in exactly the state a reader most needs it documented for.
    """
    trie = _trie(contract)
    out: list[tuple[str, object]] = []

    def descend(value, node: dict, prefix: str) -> None:
        if isinstance(value, dict):
            if not value:
                out.append((prefix, value))
                return
            for k, v in value.items():
                if isinstance(v, list):
                    seg = k + "[]" if (k + "[]") in node else (
                        "*" if "*" in node and (k + "[]") not in node else k + "[]")
                else:
                    seg = k if k in node else ("*" if "*" in node else k)
                child = node.get(seg, {})
                descend(v, child, f"{prefix}.{seg}" if prefix else seg)
            return
        if isinstance(value, list):
            out.append((prefix, value))
            if value:
                descend(value[0], node, prefix)
            return
        out.append((prefix, value))

    descend(payload, trie, "")
    return out


def canonical_paths(payload: dict, contract: dict) -> set[str]:
    """Just the paths ``walk_payload`` produced."""
    return {p for p, _ in walk_payload(payload, contract)}


# ═══════════════════════════════════════════════════════════════════════════════
# Rendering the document
# ═══════════════════════════════════════════════════════════════════════════════
_RULE_OF_THUMB = (
    "up/down → health · a number → telemetry · number > limit → telemetry keeps "
    "the number, health raises the warning, the log records the crossing."
)


def _rows(contract: dict, endpoint: str) -> list[str]:
    lines = []
    for cat in CATEGORIES:
        entries = [(p, s) for p, s in sorted(contract.items())
                   if s["category"] == cat]
        if not entries:
            continue
        lines.append("")
        lines.append(f"### {cat}")
        lines.append("")
        lines.append("| key | type | unit | since | moved to | removed in | log twin | notes |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for path, spec in entries:
            note = (spec["note"] or "").replace("|", "\\|").replace("\n", " ")
            removed_in = spec["removed_in"]
            # Fix round item 1b (decision:1832): DUAL_EMIT_DROP_TARGET is a
            # TARGET, never a commitment — render it distinctly so a reader
            # does not mistake "removed in" for a scheduled date the way the
            # bare version string invited before.
            if removed_in == DUAL_EMIT_DROP_TARGET:
                removed_in_cell = f"{removed_in} (targeted)"
            else:
                removed_in_cell = removed_in or "—"
            lines.append(
                f"| `{path}` | {'/'.join(spec['types'])} | {spec['unit'] or '—'} "
                f"| {spec['since']} | {'`' + spec['moved_to'] + '`' if spec['moved_to'] else '—'} "
                f"| {removed_in_cell} | {'`' + spec['log'] + '`' if spec['log'] else '—'} "
                f"| {note or '—'} |"
            )
    return lines


def render_markdown() -> str:
    """The whole of ``Documentation/telemetry-contract.md``, from this module.

    ⛔ Nothing else may write that file. It is regenerated by
    ``tests/test_telemetry_contract.py`` (which fails when the checked-in copy
    differs), so a key added to the dict and not to the doc cannot be merged.
    """
    out: list[str] = []
    a = out.append
    a("<!-- GENERATED FROM shared-memory/scripts/telemetry_contract.py — DO NOT EDIT BY HAND. -->")
    a("<!-- Regenerate: uv run python shared-memory/scripts/telemetry_contract.py > shared-memory/Documentation/telemetry-contract.md -->")
    a("")
    a("# The Telemetry Contract")
    a("")
    a(f"Contract version **{VERSION}**. Every key the gateway emits on `GET /health` "
      "and `GET /memory/telemetry`, what it means, what it is measured in, when it "
      "arrived, and where it is going.")
    a("")
    a("## The roles")
    a("")
    a("**Rule of thumb:** " + _RULE_OF_THUMB)
    a("")
    a("`GET /health` answers *can I use it now, and what should I expect*: one status "
      "enum, one enum per dependency, the warnings a limit crossing raised, "
      "identity/version, and the sizing a client needs to set its own timeouts. It is "
      "served from a short TTL cache over a 60-second refresher and **makes no "
      "database call at request time**.")
    a("")
    a("`GET /memory/telemetry` is **the numbers**: counters, gauges, percentiles and "
      "censuses, with the limit stated next to the number it bounds. Bounded cost per "
      "request — the whole payload is cached for `TELEMETRY_CACHE_S`, and the "
      "unbounded insight walk is computed by the refresher, not per request.")
    a("")
    a("The logs are the final word on *what happened at 03:12*: every dependency "
      "state transition and every warning raised or cleared writes one line named "
      "after the key that changed (the **log twin** column). Never a line per poll.")
    a("")
    a("## HTTP status codes — unchanged by this release")
    a("")
    a("`/health` returns **503 if and only if the embedder or the reranker is down**. "
      "That is the save mandate: without an encoder a save cannot produce a vector, "
      "and a row with no vector is invisible to semantic search. **Every other "
      "verdict — a degraded encoder, a dead Postgres, a failing outbox, a stalled "
      "daemon — is served 200 with the enum in the body.** A consumer that inferred "
      "the verdict from the status code must read `status` and `dependencies` "
      "instead.")
    a("")
    a(f"An anonymous caller on an auth-configured install receives exactly "
      f"`{{{', '.join(ANONYMOUS_HEALTH_KEYS)}}}` and nothing else.")
    a("")
    a("## Meaning changes")
    a("")
    a("A key whose **value** changed meaning while its **name** stayed. Enumerated "
      "because a consumer reading only the key list would see nothing wrong.")
    a("")
    a("| endpoint | key | version | was | now | what to do |")
    a("|---|---|---|---|---|---|")
    for mc in MEANING_CHANGES:
        a(f"| {mc['endpoint']} | `{mc['path']}` | {mc['in_version']} | {mc['was']} "
          f"| {mc['now']} | {mc['action']} |")
    a("")
    a("## Removed outright in 0.9.74")
    a("")
    a("Not moved — **removed**. Each had no writer and had therefore read `0` since "
      "it shipped.")
    a("")
    a("| endpoint | key | why |")
    a("|---|---|---|")
    for rm in REMOVED_IN_0_9_74:
        a(f"| {rm['endpoint']} | `{rm['path']}` | {rm['reason']} |")
    a("")
    a("## Dual-emit drop target")
    a("")
    a(f"`removed_in: {DUAL_EMIT_DROP_TARGET} (targeted)` marks a key moved off `/health` "
      "and dual-emitted since v0.9.74. The drop is **gated on the monitor-contract step "
      "landing first** (Group 3 — the monitor must consume the replacement keys before "
      f"the originals can go); `{DUAL_EMIT_DROP_TARGET}` names only the earliest release "
      "it could still happen in, **not a commitment**.")
    a("")
    a("## `GET /health`")
    a("")
    a("Paths are relative to the response object.")
    out.extend(_rows(HEALTH, "health"))
    a("")
    a("## `GET /memory/telemetry`")
    a("")
    a("The envelope is `{\"status\": \"success\", \"telemetry\": {…}}`; paths below are "
      "relative to the `telemetry` wrapper. Every section is computed independently "
      "and degrades to `{\"error\": \"…\"}` on its own failure, so one dead backend "
      "never blanks the payload.")
    out.extend(_rows(TELEMETRY, "telemetry"))
    a("")
    return "\n".join(out) + "\n"


if __name__ == "__main__":   # pragma: no cover
    print(render_markdown(), end="")
