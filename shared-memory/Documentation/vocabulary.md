# The Record Vocabulary

How the framework's building blocks are described, connected, and read back.

This document is **derived from the code**, not from intent. Where the two disagree the code
is right and this file is a defect. Anything that changes a field, a relation, or how a fold
reads them updates this document in the same change.

Three record types share one store and one id sequence: **Fact**, **Decision**,
**Retrospective**. They differ in what they must carry, what they may connect to, and how
synthesis is allowed to weigh them.

> Status: the Fact section is written. Decision, Retrospective, the connection vocabulary,
> and the read paths are still to come.

---

## 1. Fact

A fact is a stored claim. It is the only record type that cannot ground on another record —
its grounding points *outward*, at where the knowledge came from, which is what makes every
provenance chain terminate.

### 1.1 Fields

Only one field is enforced. Everything that makes a fact *useful* is optional to the code and
therefore the job of whoever captures it.

| Field | Status | Default | What consumes it, and what breaks without it |
|---|---|---|---|
| `content` | **Required** — 400 without it | — | The record itself; embedded for semantic search. |
| `metadata.source` | **Required** — 400 without it | — | Provenance of the *record*. Overridden server-side by the authenticated identity, so a client value only has to be non-empty. |
| `metadata.entities` | Optional to the code | `[]` | **The consolidation gate.** Named concepts the fact is about; each becomes a `MENTIONS` edge. A fact saved without entities is stored and searchable but **never** reaches a thematic summary. This is the single most consequential silent omission in the system — nothing warns, and the exclusion is permanent until the fact is re-saved. |
| `metadata.project` | Optional | Derived client-side from the project folder | Scopes consolidation. Summaries are keyed on *(entity, domain)*, so facts sharing an entity but tagged differently never fuse. Derivation walks up to the nearest project marker, which is what keeps one project's tag identical across every agent and session. |
| `metadata.domain` | Optional | falls back to `general` | Sub-divides one project whose entities span unrelated topics. |
| `metadata.source_ref` | Optional | absent | **Sets the evidential kind** (§1.3) and supplies the citable origin a fold can quote. |
| `metadata.supersedes` | Optional | absent | Marks an earlier fact corrected by this one — soft, never a delete (§1.4). |
| `metadata.elicited` | Optional | absent | Records that a human had a say before the save. Feeds coverage telemetry; it is a claim about *process*, not content. |
| `metadata.subagent` | Optional | absent | Sub-role within one agent identity, since all sub-agents of a tool share its token. |

### 1.2 Fields you cannot set

Three values are stamped by the server and any client-supplied value is **stripped first**.
They are deterministic, so they are worth trusting in a way narrative fields are not.

| Field | Source | Why it is not yours to write |
|---|---|---|
| `source` | the authenticated token | An agent cannot claim to be another agent. |
| `principal` | the kernel-attested login of the connecting process | An agent told to "save as someone else" cannot move it. Absent — honestly unknown, never guessed — when the connection carries no kernel credential. |
| `content_hash` | SHA-256 of the content | Identity. Re-saving identical content updates rather than duplicates. |

A fact's custody is written into the graph as a **delegation**: the record is attributed to the
*agent* that produced it, and that agent acts on behalf of the *person*. It is deliberately not
attributed to the person directly, because a fact surfaced by a web search or a code review is
*committed by* an operator without being *authored by* them.

### 1.3 `source_ref` → the evidential kind

`fact_kind` is never chosen directly. It is **derived** from `source_ref`, so the weight a fact
carries in synthesis follows from a citation rather than from an assertion about itself.

**The floor is `discussion`.** Every fact is produced in a conversation; that is the base case,
not a degenerate one. A citation does not create provenance out of nothing — it records which
*external context entered that conversation* and raised the fact above it.

| `source_ref` | Kind | Meaning |
|---|---|---|
| *(absent)* | `discussion` | The floor. Unmarked means conversational. |
| `discussion_context` | `discussion` | The floor, stated explicitly. |
| `observation_context` | `observation` | A conclusion reasoned out *within* the discussion. A deliberate qualifier — never where an unmarked fact lands. |
| `live:…`, or a datastore URI | `tested` | An empirical reading off the **running system**: a graph census, a health check, the journal. |
| a path naming a test component | `tested` | Empirically verified by a test. |
| a source-code file | `measured` | Measured from code. |
| an `http(s)` URL | `researched` | An external source. |
| any other cited document | `researched` | A cited but non-code source. |

Resolution is ordered and first-match-wins; a sub-document locator (`#L1418`, `@00:04`) is
stripped before the path checks. A test path must match an actual **path component** — a mere
substring would promote `latest_run.py` to the highest evidential weight.

Two properties of this design are load-bearing:

- **It is downward-safe.** An unqualified fact grounds a decision as *soft input* rather than
  hard evidence, because the conversational kind maps to the softer grounding relation. An
  unqualified claim should not enter synthesis carrying the weight of evidence.
- **It must never inflate by accident.** Synthesis is told that tested and measured evidence
  outranks discussion, so a wrongly-promoted fact silently strengthens a claim nobody verified.
  Defaulting *down* is safe; promoting *up* is a judgement for a person, never a heuristic.

The same classification yields the **origin locus** — the citable phrase a thematic summary can
quote (`from="coordinator.py"`). The two conversational kinds produce no locus, deliberately, so
a fold never invents provenance a fact does not have.

### 1.4 Correction

A fact is never edited and never deleted. A correction **supersedes** it: the old fact is kept
for provenance and comparison, flagged, hidden from search, and excluded from consolidation.
Supersession is always explicit — similarity is not a correctness signal.

Propagation is **lazy**. Summaries already built from a superseded fact are not re-folded; they
are flagged at retrieval so the staleness is judged at the point of use, by whoever is using it.

### 1.5 What to elicit

The code enforces two fields; quality comes from the rest. For a fact, a *mention* is enough —
state what is about to be stored and the `source_ref` inferred, and let the operator correct it.
The full questionnaire belongs to decisions and retrospectives, not here.

Elicit in this order, because this is the order in which absence hurts:

1. **`entities`** — 1–4 named concepts. Without them the fact never reaches Tier 3 at all.
2. **`source_ref`** — because it sets the evidential weight. Propose the one you inferred and
   name the kind it implies, so the operator is confirming a *consequence*, not a string.
3. **`project`**, when the fact belongs to a different project than the working directory.

"No source" is a legitimate answer — it means `discussion`, which is a real kind and not a
failure. Recording that deliberately is different from never asking.
