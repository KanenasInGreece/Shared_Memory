"""Reference resolution (Stage 1.2b) — turn textual record references into edges.

Agents routinely reference other records in free text ("refines decision 381",
"addendum to pg_id 257"). These are real cross-references that previously leaked
as noise entity nodes or stayed invisible in prose. This module extracts them
deterministically (context-gated regex + id validation) and assigns a
relationship type, materialising record→record edges.

Relationship-type judgment is deterministic — there is no LLM judge. The rule:
Decision→Decision = INFORMED_BY (activates the dormant "prior decision used as
input" edge); any other record pairing = REFERENCES. Neither ever yields
SUPERSEDES, which is explicit-only (`--supersedes`).
"""
import re

from ontology import ONT

# Context-gated: a bare number is ignored — it must follow a record-reference cue.
_REF_RE = re.compile(
    r"(?:pg[_ ]?id|decision|fact|retrospective|supersed\w*|refines|"
    r"follow-?up to|builds on|addendum to|companion to|related)\s*:?\s*#?\s*(\d{2,4})",
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


def deterministic_relation(src_label: str, tgt_label: str) -> str:
    """Label-based default: Decision→Decision = INFORMED_BY, else REFERENCES."""
    if src_label == ONT.decision and tgt_label == ONT.decision:
        return ONT.informed_by
    return ONT.references


def classify_relation(src_label, tgt_label, snippet) -> str:
    """Relationship type for a resolved reference — deterministic, no I/O.

    `snippet` is accepted (and ignored) so call sites keep passing the evidence
    they logged the edge with; the type follows from the two labels alone. It can
    never widen the relation set or return SUPERSEDES.
    """
    return deterministic_relation(src_label, tgt_label)
