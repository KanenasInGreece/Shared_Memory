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
