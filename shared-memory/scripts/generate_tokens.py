#!/usr/bin/env python3
"""
Generate AGENT_TOKENS for the gateway and per-agent AGENT_TOKEN values.

Run once from the repo root:
    uv run python shared-memory/scripts/generate_tokens.py

1. Add the AGENT_TOKENS line to the gateway .env
2. Copy each AGENT_TOKEN line to the matching agent's skill .env
   (e.g. ~/.gemini/skills/shared-memory/.env for Gemini CLI)
   Each agent must use its own distinct token — never share tokens across agents.
"""
import secrets

AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity", "monitor"]

# Read-only identities: registered like any agent, but confined by AGENT_ROLES
# to GET /health, GET /memory/telemetry, and POST /memory/graph (read-only
# Cypher). "monitor" is the shared-memory-monitor dashboard — a read-only ops
# client that must not borrow a write-capable agent token.
READ_ONLY_AGENTS = ["monitor"]

tokens = {a: f"tok_{secrets.token_urlsafe(24)}" for a in AGENTS}

print("=== Gateway .env — add this line ===")
print("AGENT_TOKENS=" + ",".join(f"{a}:{t}" for a, t in tokens.items()))
print()
print("=== Gateway .env — optional read-only roles ===")
print("AGENT_ROLES=" + ",".join(f"{a}:read" for a in READ_ONLY_AGENTS))
print("# read-role agents may reach only GET /health, GET /memory/telemetry,")
print("# and POST /memory/graph (read-only Cypher). All other routes → 403.")
print()
print("=== Per-agent .env — copy the matching AGENT_TOKEN line ===")
for a, t in tokens.items():
    suffix = "  (read-only)" if a in READ_ONLY_AGENTS else ""
    print(f"  {a:15}  AGENT_TOKEN={t}{suffix}")
print()
print("Each agent must use its own distinct token — never share tokens across agents.")
