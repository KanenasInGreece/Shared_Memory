#!/usr/bin/env python3
"""Stage 1.3 REM-typing evaluation harness (decision 475, refined).

Tunes the REM prompt for entity sub-typing + typed-relationship assignment
against a gold set, DATA-DRIVEN. Dry-run only — never writes Neo4j.

Reports, for entities and relationships:
  - macro-F1 (+ per-class P/R/F1, confusion), balanced + weighted accuracy
  - coverage (fraction typed, i.e. not OTHER / not NONE)
  - wrong-rate (confident-but-wrong: pred != gold and pred is a real type)
  - predicted-type ENTROPY (anti-gaming: collapse to a few labels = gate-gaming)
  - gate-rejection rate (rels the LLM proposes that the domain-range map rejects)
  - LIFT over a deterministic keyword/type-pair BASELINE (the real success signal
    for a local model — does the LLM beat cheap rules?)
  - --variance: N runs at temp 0.6 → per-run macro-F1 + std (brittleness check)

Usage:
  uv run --with httpx python eval/typing_eval.py            # temp=0 main run
  uv run --with httpx python eval/typing_eval.py --variance # temp=0.6 x5 study
  uv run --with httpx python eval/typing_eval.py --mock     # baseline-as-LLM smoke
"""
import argparse
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared-memory", "scripts"))
from ontology import is_allowed_relation  # noqa: E402

GOLD = Path(__file__).with_name("gold_set.json")
ENTITY_TYPES = ["Component", "System", "Model", "Concept", "Document", "OTHER"]
REL_TYPES = ["DEPENDS_ON", "PART_OF", "IMPLEMENTS", "PRODUCES", "CONSUMES",
             "RUNS_ON", "CONFIGURES", "DESCRIBES", "VALIDATES", "NONE"]

# ── Candidate prompts (the thing we iterate) ──────────────────────────────────
ENTITY_PROMPT = """Classify the ENTITY into EXACTLY ONE category. Reply with ONE word only.
Entity: "{name}"
Context: "{context}"
Categories:
- Component = a software unit we build (module, class, script, daemon)
- System = a service, datastore, framework, or infrastructure we run
- Model = an AI/ML model
- Concept = a pattern, technique, or principle
- Document = a spec, README, schema doc, or research artifact
- OTHER = a person, a project, or none of the above
Answer:"""

REL_PROMPT = """Pick the RELATIONSHIP from SOURCE to TARGET. Reply with ONE token only.
Source ({src_type}): "{src}"
Target ({tgt_type}): "{tgt}"
Context: "{context}"
Allowed: DEPENDS_ON, PART_OF, IMPLEMENTS, PRODUCES, CONSUMES, RUNS_ON, CONFIGURES, DESCRIBES, VALIDATES, NONE
Answer:"""


# ── Deterministic baseline (the lift floor) ───────────────────────────────────
def baseline_entity_type(name, context=""):
    n = name.lower()
    if re.search(r"\.py$|^(coordinator|rem_loop|consolidation_loop|hive_mind_proxy|"
                 r"memory_bridge|authmiddleware|vector-skill|reference_resolver|"
                 r"pruning_loop|remdaemon|consolidationdaemon)$", n):
        return "Component"
    if re.search(r"adr-?\d|readme|\.md$|changelog|design-doc|research-note|"
                 r"distilledresearch|server-setup|documentation", n):
        return "Document"
    if re.search(r"bge|gemma|qwen|llama|nomic|gpt-oss|embed|rerank", n):
        return "Model"
    if re.search(r"gateway|-api$|^postgres|^neo4j|lancedb|sharedmemory|monitor|"
                 r"\bvm$|store|service|framework|^retriever|^reranker", n):
        return "System"
    if re.search(r"pattern|consolidation|resolution|routing|supersession|"
                 r"equivalence|scoping|quality|provenance|^nrem$|^rem$", n):
        return "Concept"
    return "OTHER"


_BASELINE_REL = {
    ("Component", "System"): "DEPENDS_ON", ("Component", "Component"): "DEPENDS_ON",
    ("Component", "Concept"): "IMPLEMENTS", ("Component", "Model"): "CONSUMES",
    ("System", "Model"): "DEPENDS_ON", ("Model", "System"): "RUNS_ON",
    ("Document", "Component"): "DESCRIBES", ("Document", "System"): "DESCRIBES",
    ("Document", "Concept"): "DESCRIBES", ("Activity", "Component"): "VALIDATES",
}


def baseline_relation(src_type, tgt_type):
    rel = _BASELINE_REL.get((src_type, tgt_type), "NONE")
    if rel != "NONE" and not is_allowed_relation(rel, src_type, tgt_type):
        return "NONE"
    return rel


# ── Pure metrics ──────────────────────────────────────────────────────────────
def confusion(pairs):
    m = defaultdict(Counter)
    for gold, pred in pairs:
        m[gold][pred] += 1
    return m


def per_class_f1(pairs, labels):
    tp = Counter(); fp = Counter(); fn = Counter()
    for gold, pred in pairs:
        if gold == pred:
            tp[gold] += 1
        else:
            fp[pred] += 1
            fn[gold] += 1
    out = {}
    for lbl in labels:
        p = tp[lbl] / (tp[lbl] + fp[lbl]) if (tp[lbl] + fp[lbl]) else 0.0
        r = tp[lbl] / (tp[lbl] + fn[lbl]) if (tp[lbl] + fn[lbl]) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        out[lbl] = {"p": p, "r": r, "f1": f, "support": tp[lbl] + fn[lbl]}
    return out


def macro_f1(pairs, labels):
    pc = per_class_f1(pairs, labels)
    present = [lbl for lbl in labels if pc[lbl]["support"] > 0]
    return sum(pc[lbl]["f1"] for lbl in present) / len(present) if present else 0.0


def weighted_f1(pairs, labels):
    pc = per_class_f1(pairs, labels)
    tot = sum(pc[lbl]["support"] for lbl in labels)
    return sum(pc[lbl]["f1"] * pc[lbl]["support"] for lbl in labels) / tot if tot else 0.0


def balanced_accuracy(pairs, labels):
    pc = per_class_f1(pairs, labels)
    present = [lbl for lbl in labels if pc[lbl]["support"] > 0]
    return sum(pc[lbl]["r"] for lbl in present) / len(present) if present else 0.0


def accuracy(pairs):
    return sum(1 for g, p in pairs if g == p) / len(pairs) if pairs else 0.0


def entropy(preds):
    n = len(preds)
    if not n:
        return 0.0
    counts = Counter(preds)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def coverage(preds, fallback):
    return sum(1 for p in preds if p != fallback) / len(preds) if preds else 0.0


def wrong_rate(pairs, fallback):
    """Confident-but-wrong: predicted a real type/rel that disagrees with gold."""
    if not pairs:
        return 0.0
    return sum(1 for g, p in pairs if p != fallback and p != g) / len(pairs)


def consistency(runs):
    """Mean per-item agreement (modal fraction) across aligned runs."""
    if not runs or len(runs) < 2:
        return 1.0
    n = len(runs[0])
    total = 0.0
    for i in range(n):
        votes = Counter(run[i] for run in runs)
        total += votes.most_common(1)[0][1] / len(runs)
    return total / n if n else 0.0


# ── LLM ───────────────────────────────────────────────────────────────────────
def parse_label(text, vocab):
    up = (text or "").upper()
    hits = [v for v in vocab if v.upper() in up]
    return hits[0] if len(hits) == 1 else (vocab[0] if not hits else _longest(hits))


def _longest(hits):
    return max(hits, key=len)  # prefer the most specific token if several appear


def call_llm(prompt, url, model, temp, timeout=300.0, retries=2):
    """Call an OpenAI-compatible endpoint. Long timeout + retry because the local
    model is shared with REM/NREM — a call can queue behind a dream cycle for
    minutes during backlog (observed 90s+ under load)."""
    import httpx
    last = None
    for attempt in range(retries + 1):
        try:
            resp = httpx.post(
                f"{url.rstrip('/')}/chat/completions",
                json={"model": model, "temperature": temp, "max_tokens": 12,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 — retry transient GPU-contention timeouts
            last = exc
    raise last


def predict_entity(e, url, model, temp, mock):
    if mock:
        return baseline_entity_type(e["name"], e.get("context", ""))
    raw = call_llm(ENTITY_PROMPT.format(name=e["name"], context=e.get("context", "")),
                   url, model, temp)
    pred = parse_label(raw, ENTITY_TYPES)
    return pred if pred in ENTITY_TYPES else "OTHER"


def predict_rel(r, url, model, temp, mock):
    if mock:
        pred = baseline_relation(r["src_type"], r["tgt_type"])
    else:
        raw = call_llm(REL_PROMPT.format(**r), url, model, temp)
        pred = parse_label(raw, REL_TYPES)
    # domain-range gate: a proposed typed rel that the map rejects → NONE (with flag)
    gated = False
    if pred != "NONE" and not is_allowed_relation(pred, r["src_type"], r["tgt_type"]):
        pred, gated = "NONE", True
    return pred, gated


# ── Eval ────────────────────────────────────────────────────────────────────
def eval_once(gold, url, model, temp, mock):
    ent_pairs, ent_preds = [], []
    for e in gold["entities"]:
        p = predict_entity(e, url, model, temp, mock)
        ent_pairs.append((e["type"], p)); ent_preds.append(p)
    rel_pairs, rel_preds, gate_rej = [], [], 0
    for r in gold["relationships"]:
        p, gated = predict_rel(r, url, model, temp, mock)
        rel_pairs.append((r["rel"], p)); rel_preds.append(p)
        gate_rej += int(gated)
    return ent_pairs, ent_preds, rel_pairs, rel_preds, gate_rej


def baseline_pairs(gold):
    ep = [(e["type"], baseline_entity_type(e["name"], e.get("context", "")))
          for e in gold["entities"]]
    rp = [(r["rel"], baseline_relation(r["src_type"], r["tgt_type"]))
          for r in gold["relationships"]]
    return ep, rp


def report(gold, url, model, mock):
    bep, brp = baseline_pairs(gold)
    base_ent_f1 = macro_f1(bep, ENTITY_TYPES)
    base_rel_f1 = macro_f1(brp, REL_TYPES)
    ep, epreds, rp, rpreds, gate_rej = eval_once(gold, url, model, 0.0, mock)

    print(f"\n{'='*64}\nSTAGE 1.3 TYPING EVAL  (model={'MOCK=baseline' if mock else model}, temp=0)\n{'='*64}")
    print(f"\nENTITIES (n={len(ep)})")
    print(f"  macro-F1      : {macro_f1(ep, ENTITY_TYPES):.3f}   (baseline {base_ent_f1:.3f}"
          f"  → LIFT {macro_f1(ep, ENTITY_TYPES)-base_ent_f1:+.3f})")
    print(f"  weighted-F1   : {weighted_f1(ep, ENTITY_TYPES):.3f}")
    print(f"  balanced-acc  : {balanced_accuracy(ep, ENTITY_TYPES):.3f}")
    print(f"  accuracy      : {accuracy(ep):.3f}")
    print(f"  coverage      : {coverage(epreds, 'OTHER'):.3f}   (typed vs OTHER fallback)")
    print(f"  wrong-rate    : {wrong_rate(ep, 'OTHER'):.3f}   (confident-but-wrong; target ≤0.10)")
    print(f"  pred-entropy  : {entropy(epreds):.3f}   (max {math.log2(len(ENTITY_TYPES)):.3f}; low ⇒ gaming)")
    pc = per_class_f1(ep, ENTITY_TYPES)
    for lbl in ENTITY_TYPES:
        if pc[lbl]["support"]:
            print(f"     {lbl:<10} P={pc[lbl]['p']:.2f} R={pc[lbl]['r']:.2f} "
                  f"F1={pc[lbl]['f1']:.2f} n={pc[lbl]['support']}")
    print(f"\nRELATIONSHIPS (n={len(rp)})")
    print(f"  macro-F1      : {macro_f1(rp, REL_TYPES):.3f}   (baseline {base_rel_f1:.3f}"
          f"  → LIFT {macro_f1(rp, REL_TYPES)-base_rel_f1:+.3f})")
    print(f"  accuracy      : {accuracy(rp):.3f}")
    print(f"  coverage      : {coverage(rpreds, 'NONE'):.3f}")
    print(f"  wrong-rate    : {wrong_rate(rp, 'NONE'):.3f}")
    print(f"  gate-rejected : {gate_rej}/{len(rp)}   (LLM proposed an edge the map rejects)")

    out = Path(__file__).with_name("typing_eval_predictions.jsonl")
    with out.open("w") as f:
        for (g, p), e in zip(ep, gold["entities"]):
            f.write(json.dumps({"kind": "entity", "name": e["name"], "gold": g, "pred": p}) + "\n")
        for (g, p), r in zip(rp, gold["relationships"]):
            f.write(json.dumps({"kind": "rel", "src": r["src"], "tgt": r["tgt"],
                                "gold": g, "pred": p}) + "\n")
    print(f"\n  predictions → {out}")


def variance_study(gold, url, model, n=5):
    print(f"\n{'='*64}\nVARIANCE STUDY  (temp=0.6, N={n})\n{'='*64}")
    ent_runs, f1s = [], []
    for i in range(n):
        ep, epreds, _, _, _ = eval_once(gold, url, model, 0.6, mock=False)
        ent_runs.append(epreds); f1s.append(macro_f1(ep, ENTITY_TYPES))
        print(f"  run {i+1}: entity macro-F1 = {f1s[-1]:.3f}")
    mean = sum(f1s) / len(f1s)
    std = (sum((x - mean) ** 2 for x in f1s) / len(f1s)) ** 0.5
    print(f"  mean={mean:.3f}  std={std:.3f}  (brittle if std > ~0.07)")
    print(f"  entity consistency (modal agreement) = {consistency(ent_runs):.3f}  (target ≥0.85)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.environ.get("REASONER_EVAL_URL", "http://localhost:5000/v1"))
    ap.add_argument("--model", default=os.environ.get("REASONER_EVAL_MODEL", "local-model"))
    ap.add_argument("--variance", action="store_true", help="temp=0.6 N=5 brittleness study")
    ap.add_argument("--mock", action="store_true", help="use baseline as the LLM (no GPU) — smoke test")
    args = ap.parse_args()
    gold = json.loads(GOLD.read_text())
    report(gold, args.url, args.model, args.mock)
    if args.variance and not args.mock:
        variance_study(gold, args.url, args.model)


if __name__ == "__main__":
    main()
