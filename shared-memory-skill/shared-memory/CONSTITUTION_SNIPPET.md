<!--
Canonical constitution-file snippet for the Shared Memory Framework.

This is the single source of truth. An installer proposes inserting the
block below into the operator's constitution file (CLAUDE.md / GEMINI.md /
AGENTS.md, whichever the agent uses) — it must copy this text verbatim, never
regenerate or paraphrase it (determinism via template, not generation).

No install-specific substitution is needed: the block deliberately does not
name a skill path, since any agent can discover its own installed skills —
naming one would need per-agent substitution for no benefit.

Marker-delimited and versioned (v2 below) so a later install/upgrade pass can
find-and-replace this exact block instead of duplicating it, and can detect
drift by comparing the installed block's version marker against this file's.
(v1 -> v2: the search-trigger sentence was rewritten from a subjective "context
feels incomplete" gate to an explicit trigger category + store ranking — the
softer v1 phrasing let an agent rationalize skipping the search when a dense
local per-project memory index was already preloaded and felt sufficient,
even though it is a different store than the one this section is about.)

ALWAYS propose this block for the operator to confirm or adjust before
writing it into their personal constitution file. Never write it silently.
-->

<!-- shared-memory:constitution-snippet v2 -->
## Shared Memory
Use the shared memory skill proactively — to draw from it, not only save to
it. Locally preloaded per-project notes are supplementary scratch space, not
authoritative: for any question about project direction, a prior decision, or
a claim that may have been superseded, the shared memory store is the source
of truth, and searching it is a precondition before reasoning on that topic —
not a judgment call to make first. After a discussion that sets project
direction, propose recording the key facts, and confirm with the user before
saving any decision — never auto-decide (facts can later be superseded;
decision outcomes get recorded afterward as retrospectives).
<!-- /shared-memory:constitution-snippet -->
