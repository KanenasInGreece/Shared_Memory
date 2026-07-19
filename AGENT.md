# AGENT.md

**→ Read [`AGENTS.md`](AGENTS.md) — the canonical agent file for this repository.**

It has one mission: **operate the framework** on the user's machine — interview-driven first install (writes `shared-memory/.env` from the template), start/stop/status, token issuance, installing the skill into each agent, upgrade, and backup. Quick start, maintenance, and updates.

For architecture, internals, or changing the framework's own code, use [`README.md`](README.md) — the authoritative deep reference.

This file exists only so agents that look for `AGENT.md` find their way; the two files previously carried duplicate guidance, which drifted. All operating content now lives in `AGENTS.md`.
