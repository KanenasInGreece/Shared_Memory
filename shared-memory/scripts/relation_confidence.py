"""
relation_confidence.py — shared conventions for machine-minted relation edges
(REM rebuild: decisions 718 / 726 / 727).

Single source of truth for:
  * the asserted_by taxonomy and the two machine calibration FAMILIES
    (entity_relation = typed Entity→Entity; evidential = record→record
    proposals — each family gets its OWN reliability curve, never shared),
  * deterministic confidence priors (fact_kind prior for grounding edges,
    the fixed neutral prior for legacy bare MENTIONS — no LLM backfill),
  * k-vote self-consistency confidence for NOVEL REM edges (operator-asserted
    edges are never re-scored — the delta principle applied to confidence),
  * the consumption thresholds and the born-below-threshold cap for
    evidential proposals (rung 1 of the 727 ladder),
  * calibration state computed FROM operator labels in the
    relation_adjudications ledger (migration 020): a family's thresholds act
    only once the family is calibrated; until then NREM must not consume its
    machine-asserted edges,
  * ledger upsert / review-sample / labeling helpers (psycopg2, shared by the
    evidence sweep, REM, the review-edges flow and NREM),
  * anti-gaming telemetry (0.71-band spike + novel-edge ratio monitors).

Pure functions carry no I/O; DB helpers take an open psycopg2 connection and
never open their own (caller owns transactions/commit policy — AUTOCOMMIT in
the daemons, explicit commit in CLI flows).
"""
import json
import os
from psycopg2.extras import Json, execute_values

# ── asserted_by taxonomy (who asserted; 'how evidenced' lives in fact_kind/support)
ASSERTED_OPERATOR       = "operator"        # first-write explicit operator role
ASSERTED_SYSTEM_DEFAULT = "system_default"  # first-write fact_kind default gate
ASSERTED_REM            = "rem"             # per-record REM enrichment
ASSERTED_REM_SWEEP      = "rem_sweep"       # periodic evidence sweep
MACHINE_ASSERTED = frozenset({ASSERTED_REM, ASSERTED_REM_SWEEP})

# ── calibration families (727: the evidential family gets its own curve —
# its oracle is the operator and its base rate is lower; sharing the
# entity-layer curve would flatter it)
FAMILY_ENTITY     = "entity_relation"
FAMILY_EVIDENTIAL = "evidential"
FAMILIES = (FAMILY_ENTITY, FAMILY_EVIDENTIAL)

# ── consumption thresholds (env-tunable; PROVISIONAL until the calibration
# run grounds them — see calibration_state: an uncalibrated family's machine
# edges are not consumed at all, so these values only act post-calibration)
CONSUME_THRESHOLD = {
    FAMILY_ENTITY:     float(os.environ.get("RELCONF_CONSUME_ENTITY", "0.60")),
    FAMILY_EVIDENTIAL: float(os.environ.get("RELCONF_CONSUME_EVIDENTIAL", "0.70")),
}
# Evidential proposals are BORN below the consumption threshold (727 rung 1):
# rung-1 confidence is capped here; only adjudication (rung 2) or operator
# promotion can lift an evidential edge into consumable range.
EVIDENTIAL_BORN_BELOW_CAP = float(os.environ.get(
    "RELCONF_EVIDENTIAL_CAP",
    str(CONSUME_THRESHOLD[FAMILY_EVIDENTIAL] - 0.05),
))

# Legacy bare MENTIONS (pre-provenance enrichment edges): fixed NEUTRAL prior,
# no LLM backfill (726 §4 — a reasoned divergence from the June backfill
# intent; era-gating makes old edges a legitimate distinct class).
LEGACY_MENTIONS_PRIOR = 0.5

# Minimum operator labels before a family counts as calibrated (per family).
CALIBRATION_MIN_LABELS = int(os.environ.get("RELCONF_MIN_LABELS", "20"))

# ── deterministic grounding-edge confidence v1 (726 §3): fact_kind prior only,
# tested highest → discussion lowest. Calibrated later; never LLM-scored.
GROUNDING_KIND_CONFIDENCE = {
    "tested":      0.90,
    "measured":    0.85,
    "researched":  0.75,
    "observation": 0.65,
    "discussion":  0.50,
}

# fact_kind prior SHIFT applied to k-vote confidence on edges minted from a
# record of that kind (the source record's evidential weight nudges its edges).
FACT_KIND_SHIFT = {
    "tested":      +0.10,
    "measured":    +0.07,
    "researched":  +0.03,
    "observation":  0.00,
    "discussion":  -0.10,
}

_CONF_FLOOR, _CONF_CEIL = 0.05, 0.95


def clamp(x: float, lo: float = _CONF_FLOOR, hi: float = _CONF_CEIL) -> float:
    return max(lo, min(hi, x))


def grounding_confidence(fact_kind: object) -> float:
    """Deterministic v1 confidence for a grounding edge, from the fact's kind."""
    return GROUNDING_KIND_CONFIDENCE.get(fact_kind, GROUNDING_KIND_CONFIDENCE["observation"])


def vote_confidence(votes: int, k: int, fact_kind: object = None,
                    family: str = FAMILY_ENTITY) -> float:
    """k-vote self-consistency confidence (726 §3): vote share prior-shifted by
    the source record's fact_kind, clamped to [0.05, 0.95]. For the evidential
    family the result is additionally capped BELOW the consumption threshold
    (born-below rule, 727 rung 1). k <= 0 or votes outside [0, k] raise."""
    if k <= 0 or not (0 <= votes <= k):
        raise ValueError(f"invalid vote count {votes}/{k}")
    conf = clamp(votes / k + FACT_KIND_SHIFT.get(fact_kind, 0.0))
    if family == FAMILY_EVIDENTIAL:
        conf = min(conf, EVIDENTIAL_BORN_BELOW_CAP)
    return round(conf, 4)


def consumable(family: str, asserted_by: object, confidence: object,
               calibrated: bool) -> bool:
    """May NREM/synthesis consume this edge? Operator-asserted always; machine-
    asserted only when the family is CALIBRATED and confidence clears the
    family threshold. A machine edge with no confidence is never consumable.
    Legacy edges (asserted_by None/absent — the pre-provenance era) are ALWAYS
    consumable at the fixed neutral prior: era-gating makes them a legitimate
    distinct class (726 §4), and the confidence threshold is a gate on MACHINE
    assertions, not a retroactive purge of the existing graph — excluding them
    would silently sever every pre-rebuild cluster."""
    if asserted_by in (ASSERTED_OPERATOR, ASSERTED_SYSTEM_DEFAULT):
        return True
    if asserted_by in MACHINE_ASSERTED:
        if not calibrated or not isinstance(confidence, (int, float)):
            return False
        return float(confidence) >= CONSUME_THRESHOLD[family]
    # legacy / unstamped edge → visible at neutral weight (LEGACY_MENTIONS_PRIOR)
    return True


# ── ledger helpers (relation_adjudications, migration 020) ────────────────────

def upsert_adjudication(conn, *, family: str, rel_type: str, verdict: str,
                        method: str, confidence: float | None,
                        src_name: str | None = None, tgt_name: str | None = None,
                        src_pg_id: int | None = None, tgt_pg_id: int | None = None,
                        support: str | None = None, signals: dict | None = None,
                        rationale: str | None = None, model: str | None = None,
                        run_id: str | None = None) -> int:
    """Insert or re-score the CURRENT row for a directed edge. On conflict the
    row is updated in place (the evidential ladder re-scores rem_k3 → llm_sweep)
    and the previous method/confidence are preserved inside signals.prior_rungs
    so the rung history stays auditable. Returns the row id."""
    sig = dict(signals or {})
    with conn.cursor() as cur:
        conflict_target = (
            "(family, src_name, tgt_name, rel_type) WHERE family = 'entity_relation'"
            if family == FAMILY_ENTITY else
            "(family, src_pg_id, tgt_pg_id, rel_type) WHERE family = 'evidential'"
        )
        cur.execute(
            f"""
            INSERT INTO relation_adjudications
                (family, src_name, tgt_name, src_pg_id, tgt_pg_id, rel_type,
                 verdict, method, confidence, support, signals, rationale, model, run_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT {conflict_target}
            DO UPDATE SET
                verdict    = EXCLUDED.verdict,
                method     = EXCLUDED.method,
                confidence = EXCLUDED.confidence,
                support    = COALESCE(EXCLUDED.support, relation_adjudications.support),
                rationale  = COALESCE(EXCLUDED.rationale, relation_adjudications.rationale),
                model      = COALESCE(EXCLUDED.model, relation_adjudications.model),
                run_id     = COALESCE(EXCLUDED.run_id, relation_adjudications.run_id),
                signals    = COALESCE(EXCLUDED.signals, '{{}}'::jsonb)
                             || jsonb_build_object('prior_rungs',
                                  COALESCE(relation_adjudications.signals->'prior_rungs', '[]'::jsonb)
                                  || jsonb_build_array(jsonb_build_object(
                                       'method', relation_adjudications.method,
                                       'verdict', relation_adjudications.verdict,
                                       'confidence', relation_adjudications.confidence,
                                       'at', relation_adjudications.updated_at))),
                updated_at = now()
            RETURNING id
            """,
            (family, src_name, tgt_name, src_pg_id, tgt_pg_id, rel_type,
             verdict, method, confidence, support, Json(sig), rationale, model, run_id),
        )
        return cur.fetchone()[0]


def already_adjudicated_entity_pairs(conn) -> set[tuple[str, str, str]]:
    """Directed (src, tgt, rel_type) triples already in the entity ledger —
    the sweep's don't-re-ask cache."""
    with conn.cursor() as cur:
        cur.execute("SELECT src_name, tgt_name, rel_type FROM relation_adjudications"
                    " WHERE family = %s", (FAMILY_ENTITY,))
        return {(a, b, r) for a, b, r in cur.fetchall()}


def fetch_review_sample(conn, family: str, limit: int = 20) -> list[dict]:
    """Unlabeled ledger rows for the review-edges elicitation flow, stratified
    across the confidence range (weekly label passes, 726 §5): rows are bucketed
    into confidence deciles and drawn round-robin so labels cover the whole
    curve instead of clustering at one band."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, family, src_name, tgt_name, src_pg_id, tgt_pg_id, rel_type,
                   verdict, method, confidence, support, signals, rationale, created_at
            FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY width_bucket(COALESCE(confidence, 0.5), 0, 1, 10)
                    ORDER BY created_at DESC) AS rn
                FROM relation_adjudications
                WHERE family = %s AND operator_label IS NULL
            ) t
            ORDER BY rn, confidence DESC NULLS LAST
            LIMIT %s
            """,
            (family, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def apply_operator_labels(conn, labels: dict[int, str]) -> int:
    """Record operator verdicts {row_id: 'correct'|'incorrect'}. Returns rows
    updated. Invalid label values raise before any write."""
    bad = {v for v in labels.values() if v not in ("correct", "incorrect")}
    if bad:
        raise ValueError(f"invalid operator label(s): {bad}")
    rows = [(rid, lab) for rid, lab in labels.items()]
    if not rows:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "UPDATE relation_adjudications AS ra"
            " SET operator_label = v.lab, operator_labeled_at = now(), updated_at = now()"
            " FROM (VALUES %s) AS v(id, lab) WHERE ra.id = v.id::bigint",
            rows,
        )
        return cur.rowcount


def calibration_state(conn, family: str) -> dict:
    """Per-family calibration from operator labels: label count, calibrated
    flag (>= CALIBRATION_MIN_LABELS), and measured precision per 0.1 confidence
    band (accept-verdict rows only — the precision that matters is 'of the
    edges the machine accepted at this band, how many were right')."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT width_bucket(COALESCE(confidence, 0.5), 0, 1, 10) AS band,
                   count(*) FILTER (WHERE operator_label IS NOT NULL)     AS labeled,
                   count(*) FILTER (WHERE operator_label = 'correct')     AS correct
            FROM relation_adjudications
            WHERE family = %s AND verdict = 'accept'
            GROUP BY band ORDER BY band
            """,
            (family,),
        )
        bands = []
        total_labeled = 0
        for band, labeled, correct in cur.fetchall():
            total_labeled += labeled
            bands.append({
                "band": f"{(band - 1) / 10:.1f}-{band / 10:.1f}",
                "labeled": labeled,
                "precision": round(correct / labeled, 3) if labeled else None,
            })
    return {
        "family": family,
        "labels": total_labeled,
        "calibrated": total_labeled >= CALIBRATION_MIN_LABELS,
        "min_labels": CALIBRATION_MIN_LABELS,
        "threshold": CONSUME_THRESHOLD[family],
        "bands": bands,
    }


def ledger_stats(conn) -> dict:
    """Telemetry + anti-gaming monitors (726 §5): per-family verdict/method
    breakdown, the 0.71-band share (a spike there means the model is gaming the
    threshold), and the novel-edge ratio over the last 7 days."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family, method, verdict, count(*),"
            "       round(avg(confidence)::numeric, 3)"
            " FROM relation_adjudications GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
        breakdown = [
            {"family": f, "method": m, "verdict": v, "count": c,
             "mean_confidence": float(a) if a is not None else None}
            for f, m, v, c, a in cur.fetchall()
        ]
        cur.execute(
            "SELECT family,"
            "       count(*) FILTER (WHERE confidence >= 0.70 AND confidence < 0.75"
            "                          AND verdict = 'accept'),"
            "       count(*) FILTER (WHERE verdict = 'accept')"
            " FROM relation_adjudications GROUP BY family")
        band_071 = {
            f: round(n / tot, 3) if tot else 0.0
            for f, n, tot in cur.fetchall()
        }
        cur.execute(
            "SELECT count(*) FILTER (WHERE created_at > now() - interval '7 days'),"
            "       count(*) FROM relation_adjudications")
        recent, total = cur.fetchone()
    return {
        "total": total,
        "recent_7d": recent,
        "novel_ratio_7d": round(recent / total, 3) if total else 0.0,
        "band_0.70_0.75_accept_share": band_071,
        "breakdown": breakdown,
    }


def edge_properties(*, asserted_by: str, confidence: float | None, model: str,
                    run_id: str, support: str | None = None) -> dict:
    """The universal provenance property map every machine-minted edge carries
    (726 §2 — the proven ALIASES template): who asserted × how scored × which
    run. created_at is set Neo4j-side (datetime()) by the writer."""
    props = {
        "asserted_by": asserted_by,
        "confidence": confidence,
        "model": model,
        "run_id": run_id,
    }
    if support is not None:
        props["support"] = support
    return props
