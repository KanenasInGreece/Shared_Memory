# Stage 1.3 REM-typing prompt — overnight iteration log

Autonomous self-paced loop (decision 475). Optimise the prompt in `typing_eval.py`
against `gold_set.json` until the live LLM (Gemma-4-12B, local `:5000`) beats the
saved baseline with wrong-rate ≤ 0.10. Prompt-only edits; REM wiring is done with
Xenofon afterward. Each iteration appends below.

**Saved baselines (deterministic, the lift floor):** entity macro-F1 = 0.956,
relationship macro-F1 = 0.605.

**Goal:** relationship macro-F1 > 0.605 AND rel wrong-rate ≤ 0.10; entity macro-F1
≥ 0.93 (keyword baseline is near-ceiling on canonical gold — matching it is success;
the LLM's real entity value is on live/novel data) AND entity wrong-rate ≤ 0.10.

| iter | ts | ent macroF1 (lift) | ent wrong | rel macroF1 (lift) | rel wrong | change made |
|---|---|---|---|---|---|---|
| baseline | — | 0.956 (—) | 0.000 | 0.605 (—) | 0.240 | deterministic keyword/type-pair rules |
| 1 | 06-30 10:46 | 0.939 (-0.017) | 0.075 | 0.857 (+0.252) | 0.120 | first live pass, prompt v1 |
| 2 | 06-30 10:50 | 0.939 (-0.017) | 0.075 | 0.760 (+0.156) | 0.160 | REGRESSED — sharpened rel defs over-instructed the model; REVERTED to v1 |

## Learnings (recorded to shared memory: retro on decision 475, facts 477/478)
- **Lean > over-instructed:** v1 (terse) rel macro-F1 0.857; iter2 sharpened rel definitions → REGRESSED to 0.760. Over-instructing the 12B model shifts predictions net-negative. Verify EVERY prompt change against the harness; never ship on intuition. (fact 477)
- **Single-GPU contention:** the overnight loop made no progress — eval calls queue behind REM/NREM dream generations and time out (300s). Gate measurement work on backlog==0 (not a timer), or quiesce REM, or offload to a separate node. The night still drained the backlog 85→0. (fact 478)
- **Debatable gold labels are real:** of v1's 3 rel errors, `consolidation_loop→Neo4j` (DEPENDS_ON vs CONSUMES) is genuinely ambiguous — confirms ensemble=filter, human-judgment on boundaries. If re-labelled CONSUMES, rel wrong-rate → 0.08 and the goal is met as-is.
- **Failure modes (Cloe-predicted):** entity Component-overreach (Systems/Concepts typed Component); rel over-attribution (spurious PRODUCES / IMPLEMENTS).
