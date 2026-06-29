"""Reference resolution (Stage 1.2b) — turn textual record references into edges.

Agents routinely reference other records in free text ("refines decision 381",
"addendum to pg_id 257"). These are real cross-references that previously leaked
as noise entity nodes or stayed invisible in prose. This module extracts them
deterministically (context-gated regex + id validation) and assigns a
relationship type, materialising record→record edges.

Relationship-type judgment is configurable (framework .env):
  REFERENCE_JUDGE_MODE  = deterministic (default) | llm
  REFERENCE_JUDGE_URL   = OpenAI-compatible base — remote (e.g. http://atlas:1234/v1)
                          or local (http://localhost:5000/v1). Used when mode=llm.
  REFERENCE_JUDGE_MODEL = model name (default "local-model")

Deterministic rule (also the llm fallback): Decision→Decision = INFORMED_BY
(activates the dormant "prior decision used as input" edge); any other record
pairing = REFERENCES. The judge only ever picks between those two — it never
returns SUPERSEDES, which is explicit-only (`--supersedes`).
"""
import os
import re
import logging

from ontology import ONT

logger = logging.getLogger("reference_resolver")

# Context-gated: a bare number is ignored — it must follow a record-reference cue.
_REF_RE = re.compile(
    r"(?:pg[_ ]?id|decision|fact|retrospective|supersed\w*|refines|"
    r"follow-?up to|builds on|addendum to|companion to|related)\s*:?\s*#?\s*(\d{2,4})",
    re.I,
)

_MODE = os.environ.get("REFERENCE_JUDGE_MODE", "deterministic").strip().lower()
_URL = os.environ.get("REFERENCE_JUDGE_URL", "http://localhost:5000/v1").rstrip("/")
_MODEL = os.environ.get("REFERENCE_JUDGE_MODEL", "local-model")


def judge_enabled() -> bool:
    return _MODE == "llm"


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


def classify_relation(src_label, tgt_label, snippet, client=None) -> str:
    """Relationship type for a resolved reference.

    The judge LLM is deliberately **gated** — it is the weaker model, so its scope
    is minimised three ways:
      1. It is consulted ONLY for the genuinely ambiguous `Decision→Decision`
         case. INFORMED_BY is ontology-valid only there; every other pairing is
         deterministically REFERENCES with no LLM call at all.
      2. It chooses between exactly two tokens; its output is **strictly
         validated** (exactly one allowed token present, else deterministic).
      3. Any error / wrong-mode → deterministic fallback. It can never widen the
         relation set or return SUPERSEDES.
    """
    # Non-ambiguous pairings are deterministic and never touch the judge.
    if not (src_label == ONT.decision and tgt_label == ONT.decision):
        return ONT.references
    default = ONT.informed_by  # the deterministic prior for Decision→Decision
    if not judge_enabled():
        return default
    # Tight, weak-model prompt: forced single word, no reasoning, explicit options.
    prompt = (
        "Pick the relationship from a SOURCE decision to a REFERENCED decision.\n"
        f"Snippet: {snippet!r}\n"
        "Reply with EXACTLY ONE word, nothing else:\n"
        f"  {ONT.informed_by} = the source builds on / was informed by the referenced decision\n"
        f"  {ONT.references} = a neutral mention, not a dependency\n"
        "Answer:"
    )
    payload = {"model": _MODEL, "temperature": 0.0, "max_tokens": 6,
               "messages": [{"role": "user", "content": prompt}]}
    try:
        import httpx
        owns = client is None
        c = client or httpx.Client(timeout=30.0)
        try:
            resp = c.post(f"{_URL}/chat/completions", json=payload)
            if resp.status_code != 200:
                return default
            ans = (resp.json()["choices"][0]["message"]["content"] or "").upper()
        finally:
            if owns:
                c.close()
        # Strict: accept only if EXACTLY ONE allowed token appears (guards against a
        # weak model echoing both options or waffling).
        hits = [rel for rel in (ONT.informed_by, ONT.references) if rel in ans]
        return hits[0] if len(hits) == 1 else default
    except Exception as exc:  # noqa: BLE001 — judge is best-effort, never fatal
        logger.warning("reference judge failed (%s) — deterministic fallback", exc)
        return default
