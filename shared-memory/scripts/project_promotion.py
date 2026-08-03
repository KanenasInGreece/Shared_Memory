"""One writer for the parked → real project transition (invariant P6).

A record whose project could not be established at first write is PARKED: it
saves, searches and enriches normally, and is simply never folded as a subject
(P5). Establishing that project later is a state transition, and this module is
the ONLY thing allowed to perform it.

WHY ONE WRITER. Two callers need the same transition — inheritance from a
grounding judgement, and an operator-confirmed repair — and a second writer on a
gating property is the exact shape of the laundering defect this framework has
already shipped once: a rule keyed on a property whose second writer nobody
enumerated. So the transition is defined here, asserted here, and recorded here,
and the callers supply only *which* record and *on what basis*.

THE TRANSITION IS ONE-WAY. ``parked → real`` succeeds; ``real → real`` is
refused. A record that already names a project has an answer, and silently
replacing it is how a value changes meaning without anyone deciding that it
should. The deliberate consequence is that a WRONG promotion cannot be undone
through this path — which is why every promotion writes a ledger row carrying
its basis, and why the callers must be conservative about what they claim to
know. Repairing a wrong *real* project is a different act at a different
altitude: the registry plus ``normalize_projects.py``.

BOTH STORES, THROUGH THE OUTBOX. The Postgres metadata and the ``PROJECT_OF``
edge are written in one transaction — the metadata directly, the edge as an
outbox row. Never a direct Neo4j write: outbox atomicity is what makes a partial
run leave durable work rather than half a graph.

⚠ ONE THING THIS WRITER DELIBERATELY DOES NOT DO YET. Whether a retrospective's
project must EQUAL its decision's is still open. If it is ratified, a
retrospective's project becomes a DERIVED value, and this writer immediately
becomes a second writer of it — promoting a judgement would have to re-derive
every retrospective targeting it, or the pair silently disagrees. That cascade is
left unwritten rather than guessed at, because a rule the code starts enforcing
before anyone agreed it is exactly the kind of rule the next change breaks. It
costs nothing to defer: no live record needs it — every decision in the corpus
already resolves, so there is no parked judgement for a retrospective to trail.

SERVER-SIDE ONLY. Never added to ``sync_skills.sh`` or ``shared-memory-skill/``
— the skill is a thin HTTP client and the transition happens on the gateway.
"""
import json
import logging

from project_axis import SENTINEL, PROJECT_SQL

log = logging.getLogger(__name__)

# The method strings the shipped callers record in the ledger. Free text in the
# schema on purpose — a new basis for a promotion should not need a migration —
# but the ones we write ourselves are named here so they stay spelled the same
# in the writer, the tests and any later query over the ledger.
METHOD_GROUNDING = "grounding_inheritance"
METHOD_OPERATOR = "operator_confirmed"


def is_parked(project) -> bool:
    """Does this value fail to name a real project?

    The transition's SOURCE condition, and deliberately broader than "equals the
    sentinel". Nothing in this corpus carried the sentinel when the writer was
    built — every parked record was parked by ABSENCE — so a strictly
    sentinel-only test would have been correct on paper and reached nothing at
    all. Both absences mean the same thing to every reader: no project has been
    established. Whitespace counts as absent for the same reason ``fold_eligible``
    treats it so — a key that renders as nothing is an absence wearing a value.
    """
    if not isinstance(project, str):
        return True
    stripped = project.strip()
    return stripped == "" or stripped == SENTINEL


def promotion_refusal(current, target) -> str | None:
    """Why this transition must not happen — or ``None`` when it may.

    A single predicate rather than conditions scattered through the writer, so
    the rule can be asserted directly instead of through the behaviour of a
    database call. Both halves are checked: the source must be parked, and the
    target must be a real project. Promoting *to* the sentinel is parking, not
    promotion, and would quietly make the ledger claim a transition that did not
    occur.
    """
    if not is_parked(current):
        return (
            f"record already carries the project {current!r}; promotion is "
            f"parked → real only, and a real project is repaired through the "
            f"registry, not overwritten here"
        )
    if not isinstance(target, str) or not target.strip():
        return "target project is empty — there is nothing to promote to"
    if target.strip() == SENTINEL:
        return (
            f"target is the sentinel {SENTINEL!r} — that is parking, not "
            f"promotion, and the record is already parked"
        )
    return None


def sole_project(projects) -> str | None:
    """The one real project a set of judgements agrees on, or ``None``.

    Caller 1's ambiguity guard. Two judgements naming two projects leave the
    record parked rather than picking one: parked is visible and repairable,
    wrong is neither. Parked values among the inputs are IGNORED rather than
    counted as disagreement — a judgement that names no project has not
    dissented, it has abstained.
    """
    real = {p.strip() for p in (projects or []) if not is_parked(p)}
    return real.pop() if len(real) == 1 else None


async def promote_record(
    conn,
    pg_id: int,
    target: str,
    *,
    method: str,
    actor: str,
    note: str | None = None,
) -> dict:
    """Perform the transition for one record. Caller supplies the transaction.

    Returns ``{"promoted": bool, "reason": str|None, "from": str|None}``.
    A refusal is a RESULT, not an exception: both callers act on many records and
    one ineligible record must not abort a run.

    The caller owns the transaction so that the metadata write, the outbox row
    and the ledger row commit together. Nothing here commits or rolls back.
    """
    row = await conn.fetchrow(
        f"SELECT {PROJECT_SQL} AS project"
        f" FROM technical_docs WHERE id = $1 FOR UPDATE",
        pg_id,
    )
    if row is None:
        return {"promoted": False, "reason": f"no record with pg_id={pg_id}",
                "from": None}

    current = row["project"]
    refusal = promotion_refusal(current, target)
    if refusal is not None:
        log.info("promotion refused pg_id=%s → %r: %s", pg_id, target, refusal)
        return {"promoted": False, "reason": refusal, "from": current}

    target = target.strip()

    # The value is written to the TOP-LEVEL `project` field, always. The
    # resolution reads the decision blob first and falls back to this one, so a
    # top-level write resolves correctly for every record type — and a parked
    # record by definition has neither field set, so there is no shadowing to
    # worry about. One rule beats a per-type branch that has to stay in step
    # with the resolution.
    await conn.execute(
        "UPDATE technical_docs"
        "   SET metadata = jsonb_set(COALESCE(metadata, '{}'::jsonb),"
        "                            '{project}', to_jsonb($2::text))"
        " WHERE id = $1",
        pg_id, target,
    )

    # The graph half, through the outbox. The row REPLACES the record's project
    # edge rather than adding to it (P19) — see the worker's handler.
    await conn.execute(
        "INSERT INTO neo4j_outbox (pg_id, cypher_params) VALUES ($1, $2::jsonb)",
        pg_id, json.dumps({"type": "project_of", "project": target}),
    )

    # The durable ledger row. Its CHECK constraints re-assert both halves of the
    # transition, so a caller that somehow bypassed `promotion_refusal` is
    # refused by the database rather than silently recorded.
    await conn.execute(
        "INSERT INTO project_promotions"
        " (pg_id, from_project, to_project, method, actor, note)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        pg_id, current, target, method, actor, note,
    )
    # A durable ledger row always leaves a log line — the ledger is queried when
    # someone already suspects something, the log is what makes it noticeable.
    log.info(
        "promotion: pg_id=%s %s → %r via %s by %s%s",
        pg_id, "(unresolvable)" if current is None else repr(current),
        target, method, actor, f" [{note}]" if note else "",
    )

    return {"promoted": True, "reason": None, "from": current}
