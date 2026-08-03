"""The entity vote — proposing a parked record's project from its neighbours.

A fact written before the project axis was required carries no project, but it
does carry ENTITIES, and the other facts mentioning those entities carry
projects. Where they overwhelmingly agree, that agreement is evidence about this
fact. Where they merely lean, it is evidence about the CORPUS.

VALIDATED BEFORE IT WAS TRUSTED, by holding out every fact whose project is
already known and voting on it with the others (390 facts):

    always guessing the largest project ......... 66.2%
    share >= 70%, support >= 3 .................. 96.9%  (n=193)
    share >= 80%, support >= 3 .................. 98.7%  (n=149)
    share 100%, support >= 3 .................... 100%   (n=57)

So the vote carries roughly thirty points of real signal. That is why it is used
at all — and the shape of its FAILURES is why it is not simply thresholded.

⚠ EVERY MEASURED ERROR WAS A SISTER PROJECT ABSORBED INTO THE LARGER ONE:
`shared-memory-monitor` read as `shared-memory-GitHub` three times, plus
`cloe-consult`, `tier3-cloe` and `cloe-multichannel-hub`. And they were not thin
votes — one was 95% agreement across 20 supporting facts and still wrong. A
sister project shares most of its vocabulary with the project beside it, so a
CONFIDENT vote is exactly what a misfiled sister-project fact looks like.
**Raising the threshold does not fix this; only unanimity does.**

⚠⚠ AND THE ABSORPTION IS A REAL LOSS, NOT A ROUNDING ERROR. `shared-memory-
monitor` is a SISTER PROJECT of the framework and MUST STAY DISTINCT (Xenofon,
2026-08-04) — it is not a spelling of it and must never be merged into it. So
this failure mode does not degrade gracefully: each occurrence silently deletes
the distinction the operator is relying on, in the direction of the project that
already dominates the corpus. That is the whole reason the vote proposes rather
than writes below unanimity, and why the review output always shows the
COMPETING projects rather than only the winner — a monitor fact leaning towards
the framework must read as a contest, not as a result.

Hence the bands: unanimity is auto-accepted because it measured 100%, and
everything below it is proposed to an operator rather than written. The band
boundaries are the honest expression of what the holdout showed.

SERVER-SIDE ONLY — never shipped in a skill.
"""
import os

# Minimum number of supporting facts before a share means anything at all. A
# 100% share drawn from ONE neighbouring fact is not unanimity, it is a
# coincidence of sharing a single entity. The holdout showed support barely
# moves accuracy above this floor, so it is a floor and not a lever.
MIN_SUPPORT = int(os.environ.get("ENTITY_VOTE_MIN_SUPPORT", "3"))

# Nothing below this is proposed at all — it goes to the sentinel instead.
# At 70% the holdout still measured 96.9%, and below it accuracy falls off a
# cliff (92% at 60%, 85% at 50%).
REVIEW_FLOOR = float(os.environ.get("ENTITY_VOTE_REVIEW_FLOOR", "0.70"))

# The boundary between "review this closely" and "review this quickly". Every
# observed error but one fell below it.
CLOSE_REVIEW_CEILING = float(os.environ.get("ENTITY_VOTE_CLOSE_CEILING", "0.90"))

BAND_AUTO = "auto"        # unanimous — written without asking
BAND_HIGH = "high"        # 90-99% — confirm, one group
BAND_REVIEW = "review"    # 70-90% — confirm, surfaced separately
BAND_NONE = "none"        # below the floor, or too little support — sentinel


def tally(rows) -> tuple[str | None, float, int]:
    """(winning project, its share, total supporting facts) for one record.

    ``rows`` is [{"proj": name, "n": count}, …] — the projects of the facts
    sharing an entity with this one. Ties resolve to no winner rather than to
    whichever the database returned first: a tie is the definition of an
    unresolved vote, and letting row order break it would make the outcome
    depend on something nobody chose.
    """
    counts = [(r["proj"], r["n"]) for r in (rows or []) if r.get("proj")]
    if not counts:
        return None, 0.0, 0
    total = sum(n for _, n in counts)
    top = max(n for _, n in counts)
    leaders = [p for p, n in counts if n == top]
    if len(leaders) != 1:
        return None, top / total, total
    return leaders[0], top / total, total


def vote_band(project: str | None, share: float, support: int) -> str:
    """Which band a vote falls in — the ONE place the policy lives.

    Extracted as a pure function on purpose: a rule expressed only as branches
    inside a loop can be tested solely through the loop's side effects, and a
    guard disabled in place leaves its own text behind for any test that reads
    the source. This can be asserted directly.
    """
    if project is None or support < MIN_SUPPORT or share < REVIEW_FLOOR:
        return BAND_NONE
    if share >= 1.0:
        return BAND_AUTO
    if share >= CLOSE_REVIEW_CEILING:
        return BAND_HIGH
    return BAND_REVIEW


def is_auto(band: str) -> bool:
    """Only unanimity is written without an operator. Kept as its own predicate
    so that widening what gets auto-written is a deliberate edit to a named
    rule, not a comparison quietly changed somewhere in a loop."""
    return band == BAND_AUTO
