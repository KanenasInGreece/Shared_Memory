"""Qualified record references (decision 822) — shared by coordinator.py (the
API surface that emits/accepts them) and consolidation_loop.py (which uses the
same scheme internally to key the NREM fold dead-letter ledger; decision 882).

A record id is only unique WITHIN ITS TABLE. `technical_docs` and
`community_summaries` run INDEPENDENT id sequences, so the same integer names
two unrelated real records. The fix is to make the record TYPE explicit on
every reference (`fact:816`, `summary:87`) rather than to renumber both tables
onto one global sequence — an irreversible migration to close something this
closes additively. A bare integer is still accepted, and still means
technical_docs, for compatibility.
"""

REF_TYPES_DOCS      = ("fact", "decision", "retrospective")
REF_TYPES_SUMMARIES = ("summary", "insight")
REF_SEPARATOR       = ":"


def make_ref(record_type: str, pg_id: int) -> str:
    """The qualified, unambiguous form of a record reference."""
    return f"{record_type}{REF_SEPARATOR}{pg_id}"


def parse_ref(raw: str) -> tuple[str | None, int]:
    """Parse a record reference into (record_type, pg_id).

    A qualified ref (`fact:816`) returns its type; a bare integer returns
    ``None``, meaning "unqualified — assume technical_docs", which is the
    documented compatibility behaviour and the ONE place the old ambiguity
    survives. Raises ValueError on anything else, so a malformed ref fails
    loudly instead of being silently coerced to a number.
    """
    text = str(raw).strip()
    if REF_SEPARATOR not in text:
        return None, int(text)          # ValueError on non-numeric — deliberate
    rtype, _, num = text.partition(REF_SEPARATOR)
    rtype = rtype.strip().lower()
    if rtype not in REF_TYPES_DOCS + REF_TYPES_SUMMARIES:
        raise ValueError(
            f"unknown record type {rtype!r} — expected one of "
            f"{', '.join(REF_TYPES_DOCS + REF_TYPES_SUMMARIES)}"
        )
    return rtype, int(num)


def summary_record_type(metadata: dict | None) -> str:
    """The record type of a community_summaries row: insights are their own
    namespace-mate, not a kind of thematic summary, and the two rank and read
    differently — so they are distinguishable in a reference."""
    kind = (metadata or {}).get("kind")
    return "insight" if kind == "insight" else "summary"


def doc_record_type(metadata: dict | None) -> str:
    """The record type of a technical_docs row. Everything that is not an
    explicit decision or retrospective is a fact — the same collapse the
    enrichment daemon applies, so the two agree on what a record IS."""
    t = (metadata or {}).get("type")
    return t if t in REF_TYPES_DOCS else "fact"
