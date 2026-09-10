# W7 — README.md proposals (paste-ready; the operator applies these, per RULING 1)

⛔ No character of `README.md` was edited by this build. Every change below is quoted
verbatim (current text, exact replacement, file:line) for Xenofon to apply himself.

---

## Proposal 1 — S1 + S6(b): README.md:1223-1233 (opencode `shared-memory` MCP block)

Drops the removed-for-cause `python-dotenv` dependency (S1 — `SECURITY.md:134-146`
records why: the unbalanced-quote-dropping / silent-401 class) AND pins `fastmcp`/`httpx`
via the new lock (S6, RULING 4).

**Current (README.md:1223-1233):**
```jsonc
"mcp": {
  "shared-memory": {
    "type": "local",
    "command": ["uv", "run", "--with", "fastmcp", "--with", "httpx",
                "--with", "python-dotenv", "python", "/path/to/mcp/vector-skill.py"],
    "environment": {
      "COORDINATOR_URL": "http://localhost:8888",
      "VECTOR_SKILL_ENV": "/path/to/private/dir/.env"
    }
  }
}
```

**Replacement:**
```jsonc
"mcp": {
  "shared-memory": {
    "type": "local",
    "command": ["uv", "run", "--no-project", "--with-requirements",
                "requirements-mcp.lock", "python", "/path/to/mcp/vector-skill.py"],
    "environment": {
      "COORDINATOR_URL": "http://localhost:8888",
      "VECTOR_SKILL_ENV": "/path/to/private/dir/.env"
    }
  }
}
```

---

## Proposal 2 — S1 + S6(b): README.md:1248-1253 (`rag-orchestrator` / LM Studio block)

Same two fixes, second documented site.

**Current (README.md:1248-1253):**
```json
"rag-orchestrator": {
  "command": "uv",
  "args": ["run", "--with", "fastmcp", "--with", "httpx", "--with", "python-dotenv",
           "python", "/path/to/shared_mem/mcp/vector-skill.py"],
  "env": { "COORDINATOR_URL": "http://localhost:8888", "AGENT_TOKEN": "YOUR_LM_STUDIO_TOKEN" }
}
```

**Replacement:**
```json
"rag-orchestrator": {
  "command": "uv",
  "args": ["run", "--no-project", "--with-requirements", "requirements-mcp.lock",
           "python", "/path/to/shared_mem/mcp/vector-skill.py"],
  "env": { "COORDINATOR_URL": "http://localhost:8888", "AGENT_TOKEN": "YOUR_LM_STUDIO_TOKEN" }
}
```

---

## Proposal 3 — S3 + D1: README.md:453-459 (step 8, "Start the gateway")

Pins the gateway start line (D1) AND names `install_service.sh` (S3 — README never
names it today; `grep -c install_service.sh README.md` → 0; the unit's own
`ExecStart=/usr/bin/uv` placeholder crash-loops — `203/EXEC` — without it).
**One proposal covers both D1 and S3**, since they are the same README block.

**Current (README.md:453-459):**
```
8. **Start the gateway.**
   `uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair python shared-memory/scripts/hive_mind_proxy.py 8888`
   — this also launches the REM and NREM daemons ([§18](#18-the-gateway)). Verify:
   `curl http://localhost:8888/health` should report `"status":"ok"` before you save anything — once
   tokens exist (step 6) the anonymous reply carries only `status`, `version` and `api_version`; the
   fuller report needs a token, which step 9's postflight uses. For a gateway that survives logout and reboot,
   install the `systemd --user` unit in [`shared-memory/ops/`](shared-memory/ops/).
```

**Replacement:**
```
8. **Start the gateway.**
   `uv run --no-project --with-requirements requirements-gateway.lock python shared-memory/scripts/hive_mind_proxy.py 8888`
   — this also launches the REM and NREM daemons ([§18](#18-the-gateway)). Verify:
   `curl http://localhost:8888/health` should report `"status":"ok"` before you save anything — once
   tokens exist (step 6) the anonymous reply carries only `status`, `version` and `api_version`; the
   fuller report needs a token, which step 9's postflight uses. For a gateway that survives logout and
   reboot, install it as a service instead:
   `bash shared-memory/ops/install_service.sh` — substitutes this host's own resolved `uv` path into
   the shipped `systemd --user` unit in [`shared-memory/ops/`](shared-memory/ops/); the unit's own
   `ExecStart=/usr/bin/uv` is a PLACEHOLDER (a `203/EXEC` crash-loop, measured, if started as shipped)
   and only this script fixes it.
```

---

## Proposal 4 — D1: README.md:1106-1109 (§18 "The gateway")

Second documented gateway-start site — the one a line-only matcher misses (backslash
continuation spanning two physical lines).

**Current (README.md:1106-1109):**
````
```bash
uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair \
  python shared-memory/scripts/hive_mind_proxy.py 8888
curl -H "Authorization: Bearer $AGENT_TOKEN" http://localhost:8888/health
````

**Replacement:**
````
```bash
uv run --no-project --with-requirements requirements-gateway.lock python shared-memory/scripts/hive_mind_proxy.py 8888
curl -H "Authorization: Bearer $AGENT_TOKEN" http://localhost:8888/health
````

---

## Proposal 5 — S2: README.md:434-437 (step 5, "Install the skill into your agent")

README names only 2 of the 7 files `MANIFEST.txt` (the authority) actually lists —
missing `update_skill.sh` (so `doctor`'s "compat: incompatible" self-update path does
not exist on a stranger's machine) and `.env.example` (so no client knob is ever
visible).

**Current (README.md:434-437):**
```
5. **Install the skill into your agent.** The skill is a thin client — only `memory_bridge.py`
   ships with it. Copy `SKILL.md` + `memory_bridge.py` into the agent's skills directory
   ([§19](#19-tokens-and-agents); remote clients → [§20](#20-remote-clients)). Shortcut: tell
   your agent — *"clone this repo and install the shared-memory skill per README §19."*
```

**Replacement:**
```
5. **Install the skill into your agent.** The skill is a thin client — copy every file
   `shared-memory-skill/shared-memory/MANIFEST.txt` lists (the authority on what ships;
   currently `MANIFEST.txt`, `SKILL.md`, `CONSTITUTION_SNIPPET.md`, `.env.example`,
   `scripts/memory_bridge.py`, `scripts/update_skill.sh`, `Documentation/schema.md`) into the
   agent's skills directory ([§19](#19-tokens-and-agents); remote clients →
   [§20](#20-remote-clients)). Shortcut: tell your agent — *"clone this repo and install the
   shared-memory skill per README §19."*
```

---

## Proposal 6 — D3: after README.md:446 (end of step 6, "Generate agent tokens")

README presents token bootstrap as uniformly successful; it names neither `REFUSED`
(printed when a local agent's skill directory doesn't exist yet) nor `UNDELIVERABLE`
(a remote agent minted without `--reveal`). `AGENTS.md:430`/`:748` already explain
both — this inserts a pointer + the two distinct recoveries after step 6's existing
paragraph (which ends "...One distinct token per agent — never shared." at line 446).

**Current (README.md, immediately after line 446, i.e. before blank line + "7. **Start the reasoning LLM**"):**
```
   printed here to save. A REMOTE agent's token needs `--reveal <name>` on this same invocation
   (a later, separate run is a full rotation) — **and `--reveal` is yours to run, in your own
   terminal, never through an agent: a token that passes through an agent's transcript is
   stored forever** ([§19](#19-tokens-and-agents)). One distinct token per
   agent — never shared.

7. **Start the reasoning LLM** on `:5000` ...
```

**Replacement (insert this paragraph between the existing step-6 text and step 7):**
```
   printed here to save. A REMOTE agent's token needs `--reveal <name>` on this same invocation
   (a later, separate run is a full rotation) — **and `--reveal` is yours to run, in your own
   terminal, never through an agent: a token that passes through an agent's transcript is
   stored forever** ([§19](#19-tokens-and-agents)). One distinct token per
   agent — never shared.

   **Two things the mint can report instead of success, and their recoveries** (`AGENTS.md`
   Phase 6/8 has the full detail): **`REFUSED`** — the named agent's skill directory does not
   exist yet on this machine (nothing to fix here; Phase 8 mints it right after installing
   that agent's package, or create the directory yourself and re-run with
   `--add <name> --install-path <dir>`). **`UNDELIVERABLE`** — a remote agent was minted with
   no local skill install found and `--reveal` was not passed on that same invocation; recover
   with `generate_tokens.py --remint <name> --reveal <name>` **on one invocation, run by you in
   your own terminal, never through an agent** (`--reveal` prints a live token).

7. **Start the reasoning LLM** on `:5000` ...
```

---

## Proposal 7 — S5: README.md:845 (update one-liner)

The documented update line never exports `AGENT_TOKEN`; `update_framework.sh:903-924`
then warns A1/A5/A8 will SKIP and **postflight exits 1** — the documented line ends
unverified, and `AGENTS.md`'s own prelude (which exports the token) hides this.

**Current (README.md:845):**
```
bash shared-memory/scripts/update_framework.sh          # --dry-run prints every step, runs nothing
```

**Replacement:**
```
export AGENT_TOKEN=$(sed -n 's/^AGENT_TOKEN=//p' ~/.claude/skills/shared-memory/.env)  # or any write-capable agent's skill .env
bash shared-memory/scripts/update_framework.sh          # --dry-run prints every step, runs nothing
```
*(Without `AGENT_TOKEN` exported, postflight's A1/A5/A8 assertions SKIP and postflight
exits 1 at the end of an otherwise-successful update — this is documented behavior, not
a bug, but the one-liner alone does not warn a reader it is about to happen.)*

---

## Summary — every proposal, one line each

| # | Site | Fixes |
|---|---|---|
| 1 | README.md:1223-1233 | S1 (drop python-dotenv) + S6 (pin lock) |
| 2 | README.md:1248-1253 | S1 (drop python-dotenv) + S6 (pin lock) |
| 3 | README.md:453-459 | D1 (pin gateway line) + S3 (name install_service.sh) |
| 4 | README.md:1106-1109 | D1 (pin gateway line, 2nd site — the continuation-joined one) |
| 5 | README.md:434-437 | S2 (name MANIFEST.txt as the real file list) |
| 6 | README.md, after :446 | D3 (REFUSED / UNDELIVERABLE recoveries) |
| 7 | README.md:845 | S5 (AGENT_TOKEN export before the update one-liner) |

None of these will apply themselves — `tests/test_readme_gateway_start_line_is_pinned.py`
and half of `tests/test_mcp_spawn_lines_pinned.py` stay RED until proposals 1-4 are
applied (RULING at the top of the brief: this is the gate working, not a defect).
