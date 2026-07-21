<!--
Canonical constitution-file snippet for the Shared Memory Framework.

This is the single source of truth. An installer proposes inserting the
block below into the operator's constitution file (CLAUDE.md / GEMINI.md /
AGENTS.md, whichever the agent uses) — it must copy this text verbatim, never
regenerate or paraphrase it (determinism via template, not generation).

No install-specific substitution is needed: the block deliberately does not
name a skill path, since any agent can discover its own installed skills —
naming one would need per-agent substitution for no benefit.

Marker-delimited and versioned (v1 below) so a later install/upgrade pass can
find-and-replace this exact block instead of duplicating it, and can detect
drift by comparing the installed block's version marker against this file's.

ALWAYS propose this block for the operator to confirm or adjust before
writing it into their personal constitution file. Never write it silently.
-->

<!-- shared-memory:constitution-snippet v1 -->
## Shared Memory
Use the shared memory skill proactively, not just when asked. After a
discussion that sets project direction, propose recording the key facts, and
confirm with the user before saving any decision — never auto-decide (facts
can later be superseded; decision outcomes get recorded afterward as
retrospectives). Before pursuing an approach, or whenever context feels
incomplete, search prior decisions and their outcomes across projects and
sessions — that cross-project continuity is exactly what local, single-project
memory can't give you.
<!-- /shared-memory:constitution-snippet -->
