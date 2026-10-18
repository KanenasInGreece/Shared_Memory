"""Qualified record references, shared by coordinator.py (the API surface that emits and
accepts them) and consolidation_loop.py (decision:882: NREM dead-letter keys are member refs).

A record id is unique only within its table: `technical_docs` and `community_summaries`
number independently, so every reference carries its record type (`fact:816`,
`summary:87`) rather than both tables being renumbered (decision:822). A bare integer is
still accepted and still means technical_docs.
"""

REF_TYPES_DOCS      = ("fact", "decision", "retrospective")
REF_TYPES_SUMMARIES = ("summary", "insight")
REF_SEPARATOR       = ":"
# Ids are Postgres bigint identity values: 1 up to this bound.
ID_MAX              = 2**63 - 1


def make_ref(record_type: str, pg_id: int) -> str:
    """The qualified, unambiguous form of a record reference."""
    return f"{record_type}{REF_SEPARATOR}{pg_id}"


def _parse_id(raw: str) -> int:
    """A record id is ASCII digits only, within 1..ID_MAX; ``int()`` alone would also take
    ``+5``, ``1_6`` and ``-1`` and resolve them to a real row."""
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        raise ValueError(f"record id must be a positive integer, got {raw!r}")
    pid = int(text)
    if not 0 < pid <= ID_MAX:
        raise ValueError(f"record id {pid} is outside 1..{ID_MAX}")
    return pid


def parse_ref(raw: str) -> tuple[str | None, int]:
    """Parse a record reference into (record_type, pg_id).

    A qualified ref returns its type; a bare integer returns ``None``, meaning
    technical_docs, the one place the old ambiguity survives. Anything else raises
    ValueError, so a malformed ref fails loudly instead of resolving to some row.
    """
    text = str(raw).strip()
    if REF_SEPARATOR not in text:
        return None, _parse_id(text)
    rtype, _, num = text.partition(REF_SEPARATOR)
    rtype = rtype.strip().lower()
    if rtype not in REF_TYPES_DOCS + REF_TYPES_SUMMARIES:
        raise ValueError(
            f"unknown record type {rtype!r} — expected one of "
            f"{', '.join(REF_TYPES_DOCS + REF_TYPES_SUMMARIES)}"
        )
    return rtype, _parse_id(num)


def _normalised(value) -> str:
    """Stored type words compared the way the graph label does: trimmed, case-folded."""
    return str(value or "").strip().casefold()


def summary_record_type(metadata: dict | None) -> str:
    """The record type of a community_summaries row: an insight is its own type, not a kind
    of thematic summary, because the two rank and read differently."""
    return "insight" if _normalised((metadata or {}).get("kind")) == "insight" else "summary"


def doc_record_type(metadata: dict | None) -> str:
    """The record type of a technical_docs row: anything not a decision or retrospective
    is a fact. rem_loop uses this same function, so the ref and the enrichment agree."""
    t = _normalised((metadata or {}).get("type"))
    return t if t in REF_TYPES_DOCS else "fact"
