<!--
Canonical constitution-file snippet for the Shared Memory Framework.

This is the single source of truth. An installer proposes inserting the
block below into the operator's constitution file (CLAUDE.md / GEMINI.md /
AGENTS.md, whichever the agent uses) — it must copy this text verbatim, never
regenerate or paraphrase it (determinism via template, not generation).

No install-specific substitution is needed: the block deliberately does not
name a skill path, since any agent can discover its own installed skills —
naming one would need per-agent substitution for no benefit.

Marker-delimited and versioned (v5 below) so a later install/upgrade pass can
find-and-replace this exact block instead of duplicating it, and can detect
drift by comparing the installed block's version marker against this file's.
(v1 -> v2: the search-trigger sentence was rewritten from a subjective "context
feels incomplete" gate to an explicit trigger category + store ranking — the
softer v1 phrasing let an agent rationalize skipping the search when a dense
local per-project memory index was already preloaded and felt sufficient,
even though it is a different store than the one this section is about.)
(v2 -> v3: added an explicit trigger for history questions — "was X tested,
tried, rejected or done" — because the v2 categories did not cover them and an
agent answered one from a state instrument instead of the store, and that
answer reached the public README.)
(v3 -> v4: added the index-pointer rule — an agent cited a superseded fact id
from a preloaded local index without searching, although the store had the
current version and filters superseded records from search; index files go
stale silently, the store does not.)
(v4 -> v5: 'check' was not enough — the rule now prescribes the REPAIR: follow
superseded_by to the current record and rewrite the index line, because an
unrepaired index reproduces the same wrong answer at the next invocation.)

ALWAYS propose this block for the operator to confirm or adjust before
writing it into their personal constitution file. Never write it silently.
-->

<!-- shared-memory:constitution-snippet v5 -->
## Shared Memory
Use the shared memory skill proactively — to draw from it, not only save to
it. Locally preloaded per-project notes are supplementary scratch space, not
authoritative: for any question about project direction, a prior decision, a
claim that may have been superseded — or whether something was ever tested,
tried, rejected or done: those are questions about history, and the current
state of files can only confirm an answer, never give one — the shared memory
store is the source of truth, and searching it is a precondition before
reasoning on that topic — not a judgment call to make first. After a
discussion that sets project direction, propose recording the key facts, and
confirm with the user before saving any decision — never auto-decide (facts
can later be superseded; decision outcomes get recorded afterward as
retrospectives).
An id or claim hard-coded in a constitution file, a memory index, a resume or
a handoff (`fact:N`) is a pointer, not the record. Before citing or acting on
it, resolve it: `lineage <id>` says whether it is superseded and by what —
follow `superseded_by` until a current record, or search the subject. If the
pointer was stale, do not delete it and do not stop at checking: rewrite the
index line to the current id and its corrected hook, so the next invocation
starts from the right record — an unrepaired index reproduces the same wrong
answer every session. The store retires superseded records from search; only
the index decays.
<!-- /shared-memory:constitution-snippet -->
