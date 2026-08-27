<!--
Canonical constitution-file snippet for an AGENT HOST wired to the shared
memory through the MCP connector (`mcp/vector-skill.py`).

This is the MCP twin of `shared-memory/CONSTITUTION_SNIPPET.md`, and the two are
NOT interchangeable. The CLI snippet speaks of "the shared memory skill" — an
agent whose only interface is a set of MCP tools has no skill, cannot run
`memory_bridge.py`, and is left to translate the instruction itself. This file
says the same standing behaviour in the vocabulary that agent actually has:
tool names, a role that decides which writes succeed, and the one thing it must
never register alongside.

WHICH FILE GOES WHERE
  - An agent host that mounts the connector as MCP tools -> this file.
  - A CLI agent running the thin-client skill              -> CONSTITUTION_SNIPPET.md.
  - The MCP host that is an LLM SERVER with a system-prompt field rather than a
    constitution file (LM Studio is the exercised example) -> `system-prompt.md`,
    pasted into the model's system prompt. Same rules, wrapped for that surface;
    ⛔ no rule may live in only one of the two.

⛔ NO RULE HERE IS ABOUT THIS INSTALL. The block names no path, no host, no
agent and no token — an install-specific substitution would have to be
regenerated per agent, and a regenerated block is one nothing can later
find-and-replace. Everything install-specific lives in the MCP host's own
config, never here.

Marker-delimited and versioned (v2 below) so a later install/upgrade pass can
find-and-replace this exact block instead of duplicating it, and can detect
drift by comparing the installed block's version marker against this file's --
exactly as AGENTS.md Phase 8b/8c already do for the CLI snippet.
(v1 -> v2: added an explicit trigger for history questions — "was X tested,
tried, rejected or done" — because the v1 wording did not cover them and an
agent answered one from a state instrument instead of the store, and that
answer reached the public README.)

ALWAYS propose this block for the operator to confirm or adjust before writing
it into their agent's constitution file. Never write it silently, and never
paraphrase it: copying it verbatim is what keeps the marker intact.
-->

<!-- shared-memory:mcp-constitution-snippet v3 -->
## Shared Memory — through your MCP tools
The shared memory is a three-tier store other agents write to as well, reached
through the `shared-memory` MCP server. It is the source of truth for project
direction, prior decisions, any claim that may since have been superseded — or
whether something was ever tested, tried, rejected or done: those are
questions about history, and the current state of files can only confirm an
answer, never give one; locally preloaded notes are supplementary scratch
space, not authoritative.

- **Search first, always.** Before reasoning about this workstation, its
  projects, a prior decision, or whether something was ever tested, tried,
  rejected or done, call `hybrid_search_and_rerank`. This is a precondition,
  not a judgement call to make first. If the results need more graph depth
  than the automatic expansion returned, follow with `graph_query`
  (read-only Cypher) — depth is the reason to reach for it, not a second guess
  at the same question.
- **Quote the `ref`, never a bare number.** A record id is unique only WITHIN
  its table, so `fact:1234` and `summary:1234` are different records. Every
  result carries a qualified `ref` — pass that. A bare integer still resolves,
  against the facts table, which is exactly why one lifted off a summary result
  returns a confident, unrelated record instead of an error.
- **Your ROLE decides which writes succeed, and a refusal is an answer.** Every
  identity is registered with a role: a read-only one reaches retrieval and
  telemetry, and `save_artifact`, `save_decision`, `save_retrospective` and
  `supersede` answer with an honest 403. That 403 is the system working — do
  not retry it, do not route around it, and say plainly that the record was not
  saved rather than reporting a save that did not happen. Where writes ARE
  permitted, the same discipline as everywhere: propose the record and confirm
  with the operator before saving a decision, never auto-decide.
- **Never register a database MCP alongside this one** — not Postgres over SQL,
  not Neo4j over Bolt. Both connect PAST the gateway, and the gateway is what
  applies read authorization and keeps the two stores consistent; a direct
  connection returns records it should have filtered and writes ones nothing
  else can see. The tools here already cover retrieval and graph expansion, so
  such a server adds no capability, only an unguarded path to the same data. If
  one is already registered, say so rather than using it.
An id or claim found in a local index — a CLAUDE.md rule table, a MEMORY.md
line, a resume or handoff that cites `fact:N` — is a pointer, not the record.
Resolve it by searching (search_memory) before you cite or act on it: the store retires
superseded records from search, so a search returns the current version while
an index line goes stale silently. Answering from an index line without the
search is the failure this rule names.
<!-- /shared-memory:mcp-constitution-snippet -->
