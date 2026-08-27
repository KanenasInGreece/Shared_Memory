<!--
Canonical constitution-file snippet for the Shared Memory Framework.

This is the single source of truth. An installer proposes inserting the
block below into the operator's constitution file (CLAUDE.md / GEMINI.md /
AGENTS.md, whichever the agent uses) — it must copy this text verbatim, never
regenerate or paraphrase it (determinism via template, not generation).

No install-specific substitution is needed: the block deliberately does not
name a skill path, since any agent can discover its own installed skills —
naming one would need per-agent substitution for no benefit.

Marker-delimited and versioned (v4 below) so a later install/upgrade pass can
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

ALWAYS propose this block for the operator to confirm or adjust before
writing it into their personal constitution file. Never write it silently.
-->

<!-- shared-memory:constitution-snippet v4 -->
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
An id or claim found in a local index — a CLAUDE.md rule table, a MEMORY.md
line, a resume or handoff that cites `fact:N` — is a pointer, not the record.
Resolve it by searching before you cite or act on it: the store retires
superseded records from search, so a search returns the current version while
an index line goes stale silently. Answering from an index line without the
search is the failure this rule names.
<!-- /shared-memory:constitution-snippet -->
