#!/usr/bin/env python3
"""
Generate AGENT_TOKENS for the gateway and per-agent AGENT_TOKEN values.

Run once from the repo root:
    uv run python shared-memory/scripts/generate_tokens.py

1. Add the AGENT_TOKENS line to the gateway .env
2. Copy the matching AGENT_TOKEN line to each agent's skill .env
   (or to ~/.config/shared-memory/client.env as a universal fallback)
"""
import secrets

AGENTS = ["claude", "gemini", "grok", "codex", "lm_studio", "antigravity"]
tokens = {a: f"tok_{secrets.token_urlsafe(24)}" for a in AGENTS}

print("=== Gateway .env — add this line ===")
print("AGENT_TOKENS=" + ",".join(f"{a}:{t}" for a, t in tokens.items()))
print()
print("=== Per-agent .env — copy the matching AGENT_TOKEN line ===")
for a, t in tokens.items():
    print(f"  {a:15}  AGENT_TOKEN={t}")
print()
print("=== Universal fallback (~/.config/shared-memory/client.env) ===")
print("# Create this file and add ONE line for the agent on this machine:")
print("# AGENT_TOKEN=<paste the matching value from above>")
