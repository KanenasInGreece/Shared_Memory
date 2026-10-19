"""Extract textual record references ("refines decision 381", "addendum to pg_id 257") and
label them; resolve_references.py, the operator-run backfill, writes the edges (decision:474
as refined by retrospective:1697: deterministic, no LLM judge, REM writes no edges).

A number counts only after a whole-word reference cue and a separator, and only when it is an
existing technical_docs id. The backfill writes every citation as REFERENCES (decision:3135:
a citation is not grounding); SUPERSEDES is never produced here, it is explicit-only (`--supersedes`).
"""
import re

# A whole-word cue, then whitespace, ':' or '#', then the whole number; the valid-id check in
# extract_references decides whether it is a record. ASCII digits only, as record_ref.parse_ref,
# and a number followed by any digit is never cut to a valid prefix.
_REF_RE = re.compile(
    r"\b(?:pg[_ ]?id|decision|fact|retrospective|supersed\w*|refines|"
    r"follow-?up to|builds on|addendum to|companion to|related)"
    r"[\s:#]+([0-9]+)(?!\d)",
    re.I,
)


def extract_references(content, source_pg_id, valid_ids):
    """Extract (referenced_pg_id, cue, snippet) tuples from content. Pure — no I/O.

    A number is returned only when it (a) follows a record-reference cue and
    (b) matches an existing technical_docs id. Self-references are skipped;
    results are de-duplicated by referenced id (first occurrence kept).
    """
    if not isinstance(content, str):
        return []
    seen: set[int] = set()
    out: list[tuple[int, str, str]] = []
    for m in _REF_RE.finditer(content):
        ref = int(m.group(1))
        if ref == source_pg_id or ref in seen or ref not in valid_ids:
            continue
        seen.add(ref)
        s = m.start()
        snippet = content[max(0, s - 30):s + 25].replace("\n", " ").strip()
        out.append((ref, m.group(0).strip(), snippet))
    return out
