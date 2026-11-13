"""Canonical citations stored in Postgres, extracted from stored content without I/O."""
import re

from record_ref import ID_MAX, doc_record_type, make_ref

_CITATION_RE = re.compile(r"\b(fact|decision|retrospective):([0-9]+)(?!\d)")


def extract_citations(content) -> list[tuple[str, int]]:
    """Return distinct typed citations in first-occurrence order."""
    if not isinstance(content, str):
        return []
    seen = set()
    out = []
    for match in _CITATION_RE.finditer(content):
        digits = match.group(2).lstrip('0')
        if not digits or len(digits) > len(str(ID_MAX)):
            continue
        pid = int(digits)
        citation = (match.group(1), pid)
        if pid <= ID_MAX and citation not in seen:
            seen.add(citation)
            out.append(citation)
    return out


def references_from(citations, kind_by_id, self_id) -> list[str]:
    """Keep existing records of the cited kind, excluding the citing record."""
    return [make_ref(kind, pid) for kind, pid in citations
            if pid != self_id and pid in kind_by_id
            and doc_record_type(kind_by_id[pid]) == kind]
