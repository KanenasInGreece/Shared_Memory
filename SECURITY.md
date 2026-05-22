# Security Policy

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Report privately by emailing: **xsmotsenigos@googlemail.com**

Include:
- A description of the vulnerability and its potential impact
- Steps to reproduce
- Any suggested fix if you have one

You will receive a response within 72 hours. Once the issue is confirmed and a fix is available, a public disclosure will be coordinated with you.

## Known Security Considerations

### Gateway binds to all interfaces (`0.0.0.0`)

`hive_mind_proxy.py` listens on `0.0.0.0:8888` by default, which means **any machine on your local network** can reach the embedding, reranking, and LLM proxy endpoints — no authentication required.

This is intentional for workstation use where the LAN is trusted, but you should be aware of it:

- On an untrusted network (public Wi-Fi, shared office), restrict the bind address to `127.0.0.1` by editing line 245 of `hive_mind_proxy.py`:
  ```python
  site = web.TCPSite(runner, "127.0.0.1", PORT)
  ```
- Alternatively, firewall port 8888 at the OS level to limit access to localhost only.

### No authentication on MCP endpoints

The MCP server (`vector-skill.py`) and the CLI bridge (`memory_bridge.py`) perform no authentication. They are designed for local use only. Do not expose port 8888 or the MCP server to the public internet.

### Stored prompt injection (unmitigated)

**Status: known, no fix yet. Do not ingest untrusted external content at volume.**

Web-retrieved content enters the same ingestion pipeline as internally authored facts. A crafted document retrieved during a search session can embed geometrically close to a cluster of legitimate facts and — after consolidation — contaminate `community_summaries` as trusted context for all agents, persisting across all future sessions and across all tools sharing the backend.

This is a **stored injection**, not a reflected one. The attack surface is not the agent's context window — it is the shared brain itself. The geometry that makes the vector store useful (organising information by semantic proximity) is the same geometry that makes a well-crafted injection hard to distinguish from a legitimate fact. Once consolidated into Tier 3, the injected narrative is treated with the same weight as any internally authored summary.

**Two defences are planned but not yet implemented:**

- **Ingestion boundary sanitisation:** strip instructional patterns from web-retrieved content, enforce source provenance metadata, and quarantine external content in a separate trust tier before promoting it alongside internally authored facts.
- **Counterfactual simulation pass:** before committing a synthesised community narrative, verify that every claim in the output traces back to a source Fact node in the cluster. Narratives that introduce claims without a traceable source are rejected.

### Community staleness compounds injection persistence

Each consolidation cycle writes a **new** record to `community_summaries`. Superseded summaries are never deleted or marked inactive — they accumulate alongside newer ones. This means a successfully injected community summary, once written, is never automatically removed. It will continue to surface in Tier 3 retrieval results until manually identified and deleted from the database.

There is currently no tooling to audit, diff, or prune community summaries. Manual remediation requires direct Postgres access:

```sql
-- Inspect all community summaries for a given entity
SELECT id, content, metadata->>'entity', metadata->>'created_at'
FROM community_summaries
WHERE metadata->>'entity' = 'EntityName'
ORDER BY id DESC;

-- Delete a specific suspect summary
DELETE FROM community_summaries WHERE id = <suspect_id>;
```

And the corresponding Neo4j cleanup:
```cypher
MATCH (s:CommunitySummary {pg_id: <suspect_id>}) DETACH DELETE s;
```

## Supported Versions

This project is in active development. Security fixes are applied to the latest commit on `main` only.
