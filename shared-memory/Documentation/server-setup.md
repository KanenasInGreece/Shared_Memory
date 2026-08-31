# Server Setup & Operations

This is the runbook for the **operations surface** of the Shared Memory Framework —
the gateway and its daemons. It is written for whoever (human or agent) stands up
and maintains the **one** gateway host per hive.

If you only want an agent to *use* memory, you do not need this document — install
the thin-client skill and point it at a running gateway. See [`../SKILL.md`](../SKILL.md).

---

## Two surfaces — know which one you are touching

| | **Usage** (skill / client) | **Operations** (this document) |
|---|---|---|
| What it is | `memory_bridge.py` + `SKILL.md` | gateway, daemons, `migrations/` |
| Runs on | every agent, every host (incl. remote) | the **one** gateway host |
| Talks to DB/GPU? | No — HTTP to `:8888` only | Yes — owns Postgres, Neo4j, GPU |
| Shipped by | `sync_skills.sh` | this repo, via `git` |
| Upgraded by | re-sync the skill | `git pull` → `migrations/apply.py` → restart gateway |

**Installing the skill is not installing the framework.** The skill is a thin
HTTP client; the daemons never run from a skill directory. A remote agent has no
database and cannot run or upgrade them.

---

## Prerequisites (gateway host only)

- Postgres with `pgvector`, Neo4j, and the BGE‑M3 embedder (`:8070`) + reranker (`:8071`).
  `docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml up -d` starts the database and
  inference layer.
- An LLM endpoint on `:5000` (LM Studio or equivalent) for consolidation.
- `nvtop` for GPU‑aware dreaming (optional — the daemons fall back to the time
  guard without it).
- `uv` for dependency-pinned runs.

Remote agent hosts need **none** of the above — only `python` + `httpx` and a token.

---

## First-time install

```bash
# 1. Clone the framework repo onto the gateway host.
git clone <repo-url> shared-memory-GitHub
cd shared-memory-GitHub

# 2. Configure credentials.
cp shared-memory/.env.example shared-memory/.env
#    Fill in: NEO4J_PASSWORD, PG_PASSWORD, TAVILY_API_KEY
#    Optional: MEMORY_LOG_LEVEL, AUDIT_LOG_PATH, PROXY_BIND, WRITE_QUIESCE_SEC

# 3. Start the database + inference layer.
docker compose -f shared-memory/ops/postgres_neo4j_limits.yaml --env-file shared-memory/.env up -d

# 4. Apply all schema migrations (idempotent — safe to re-run).
uv run --with psycopg2-binary python shared-memory/migrations/apply.py

# 5. Mint agent tokens (one-time auth setup). No secret value is ever
#    printed: the AGENT_TOKENS line printed below is DIGEST form
#    (name:sha256:<hex> — safe to paste), and each LOCAL agent's own
#    AGENT_TOKEN is written straight into its skill .env (mode 600) —
#    nothing to copy by hand. A REMOTE agent (no local skill install found
#    on this machine) needs an explicit, human-run reveal — pass --reveal
#    on THIS SAME invocation (running it again LATER, as a separate
#    command, mints a FRESH set of tokens for every agent — a full
#    rotation, not a free peek at the one you already have):
#      uv run python shared-memory/scripts/generate_tokens.py --reveal <name>
#    No remote agent to reveal? Just:
uv run python shared-memory/scripts/generate_tokens.py
#    → add the printed AGENT_TOKENS=... line to this host's .env

# 6. Start the gateway (also spawns the REM + NREM daemons).
uv run --with aiohttp --with asyncpg --with neo4j --with httpx --with json-repair \
  python shared-memory/scripts/hive_mind_proxy.py 8888

# 7. Verify. Anonymous callers get status/version/api_version only (v0.9.9,
#    S-10) — pass the token you just minted for the full operational detail.
curl http://localhost:8888/health
#    → {"status":"ok","api_version":1,"version":"0.4.6"}
curl -H "Authorization: Bearer $AGENT_TOKEN" http://localhost:8888/health
#    → {"status":"ok","api_version":1,"version":"0.4.6","daemon":"running",...}
```

The proxy binds to `127.0.0.1:8888` by default. Set `PROXY_BIND=0.0.0.0` only over
an encrypted overlay network (Tailscale/WireGuard) or behind TLS — bearer tokens
are plaintext over HTTP.

---

## The daemon roster (operations scripts)

All live in `shared-memory/scripts/` and run on the gateway host only.

| Script | Role |
|---|---|
| `hive_mind_proxy.py` | The gateway. aiohttp server on `:8888`; routes memory ops to the coordinator and embeds/reranks to `:8070`/`:8071`. Spawns and watchdogs the daemons. |
| `coordinator.py` | Owns all Postgres + Neo4j I/O — per-entity locks, outbox worker, auth middleware. Embedded in the gateway. |
| `rem_loop.py` | REM daemon — idle summarisation: an LLM summary for a long record; writes no edges and no labels. |
| `consolidation_loop.py` | NREM daemon — synthesises Tier‑3 community summaries: grounded facts fold on the **(project, domain)** spine at density ≥ 3 (fact 1215), never on an entity hub. |
| `gpu_load.py` | GPU‑busy probe (`nvtop --snapshot`), polled by the coordinator's health refresher to feed the `inference_busy` signal on `GET /health`; the dream-cycle daemons no longer gate on it directly. |
| `ontology.py` | Loads `shared-memory/ontology.yaml` (repo-root fallback for older checkouts; `SMEM_ONTOLOGY_PATH` overrides) for the configurable entity sub-labels; relationship types are spine, pinned in code, not configurable. |
| `generate_tokens.py` | Token minting helper (write-through mint flow, `--reveal`, `--convert-digests` — see below). |

None of these ship with the skill. See [`sync_skills.sh`](../scripts/sync_skills.sh).

---

## Credential delivery — the default tier, and the hardened tier

**The default tier — `shared-memory/.env`, mode 600 — is fine for a
single-user dev box**, and is what `install_framework.sh` sets up. It is
**hygiene and detection, not isolation**: every secret in that file is
readable by anything running as the same OS user, and SEC-06 (ii) below only
*advises*, it does not block.

**⚠ Corrected (fix round 1, Opus review, verified against `man
systemd.exec`): for the `systemd --user` unit this project ships
(`hive-mind-gateway.service`), the file-based tiers below do NOT isolate the
credential from a same-uid compromise.** `man systemd.exec`, `LoadCredential=`:
*"The data is only accessible to **the user associated with the unit**, via
the `User=`/`DynamicUser=` settings (as well as the superuser)."* For a
`--user` unit, the user associated with it **is your own login uid** — the
exact identity this threat model treats as hostile. Verified directly on the
reference host: `systemd-path user-credential-store` →
`~/.config/credstore` — owned by, and readable by, the same account that can
already read a 600-mode `.env`. `<KEY>_FILE` has the identical property:
whatever file the pointer names is just as readable by that uid as the
`.env` it replaces.

**What the tiers below genuinely buy, even for a `--user` unit:** no
plaintext credential committed to the repo checkout or sitting in
`shared-memory/.env`; no value in `/proc/<pid>/environ` (SEC-06 (i)'s own
invariant — neither tier is ever copied into `os.environ`); no inheritance
by another user unit (unlike `import-environment`/`EnvironmentFile=`); a
non-swappable backing for `LoadCredential=` specifically; and a credential
that survives a headless boot. **What they do NOT buy on a `--user` unit is
isolation from the operator's own account** — a same-uid attacker who could
already read your `.env` can read `~/.config/credstore/pg_password` just as
easily. Genuine same-uid isolation needs a **system** unit with a dedicated
`User=` (a service account distinct from the operator's login) — a
different, more involved deployment shape this project does not set up for
you. The `kernel.yama.ptrace_scope=1` mitigation some hosts run has the same
caveat: it raises the bar on reading another uid's process memory, which
buys nothing here since the credential is readable at rest by the SAME uid
regardless.

If your threat model is "protect the credential from a process running as a
**different** account" (a multi-tenant box, a compromised unrelated
service), the tiers below are real hardening. If it is "protect the
credential from a compromise of my own account," none of the three tiers
changes that — only a dedicated system-unit service account does, and that
is out of scope for what this document walks you through:

1. **systemd `LoadCredential=`** — uncomment and adapt the commented example
   in [`ops/hive-mind-gateway.service`](../ops/hive-mind-gateway.service),
   using the **bare-ID form** (`LoadCredential=pg_password`, no `:PATH`) —
   see that file's own comment for why an absolute path fails a `--user`
   unit outright. `secure_env.py` (this process's credential accessor) reads
   it from `$CREDENTIALS_DIRECTORY/<key, lowercased>` automatically; nothing
   else to configure. Next tier up: `LoadCredentialEncrypted=` /
   `systemd-creds` (`man systemd-creds`) encrypts the file at rest, bound to
   TPM2 and/or a machine-local secret — worth it if the credential store
   itself might be copied off the host.
2. **`<KEY>_FILE`** (Docker official-images convention) — set e.g.
   `PG_PASSWORD_FILE=/run/secrets/pg_password` in `shared-memory/.env`, or
   export it directly. Works with any secret store that can hand you a file
   (`pass`, a mounted Docker/Kubernetes secret, a vault-agent template).
3. **The plaintext `.env` value** — the default tier above, always the
   fallback.

Full precedence statement (and why): the module docstring in
[`shared-memory/scripts/secure_env.py`](../scripts/secure_env.py). None of
these three tiers, nor an operator's own direct `os.environ` export, is ever
copied into a daemon's child process environment — `hive_mind_proxy.py`'s
`_daemon_env()` builds each spawned daemon's environment by exclusion, not
by copying this process's own.

**`systemctl --user import-environment` and unit-level `EnvironmentFile=`
for a secret key are both deprecated anti-patterns**, not because they fail
to work, but because they land the value in this process's OWN exec
environment — readable via `systemctl --user show-environment` by any
same-uid process, and inherited by every user unit started afterward, not
just this one. `ops/README.md`, "Reasoning-LLM backends" has the worked
example and the reasoning in full.

---

## Upgrading the gateway

Daemon and schema changes reach a hive through **git**, not through a skill download:

```bash
cd shared-memory-GitHub
git pull
uv run --with psycopg2-binary python shared-memory/migrations/apply.py   # apply any new migrations (idempotent)
# restart the gateway (Ctrl+C the running process, then re-run step 6 above)
```

Migrations are idempotent and run "all pending" when invoked with no argument, so
re-running after a pull is always safe. Updating an agent's **skill** never runs a
migration — the client does not own the schema.

**Upgrading through v0.9.3 (PR A2 — digest registry):** if your gateway `.env`
predates this release, run `generate_tokens.py --convert-digests` once to rewrite
`AGENT_TOKENS` to digest form — a plaintext entry now refuses gateway startup
outright. While you're editing that line: a pre-A2 registry may still carry entries
named `consolidation` or `rem_daemon` — those were how the two framework daemons
authenticated before A2. They are now dead weight; the daemons mint their own token
fresh, in-memory, on every boot instead, so delete any such entries rather than
converting them.

Neither path above recreates the store containers when a release moves a compose image pin
(pgvector, neo4j). `bash shared-memory/scripts/reconcile_stack.sh --dry-run` shows the drift, and
`bash shared-memory/scripts/reconcile_stack.sh` closes it, only once you confirm — see AGENTS.md's
*Reconcile the stack to the shipped pins* runbook.

---

## Version contract (client ↔ gateway)

The thin client and the gateway are decoupled, so they can drift. Compatibility is
enforced by an **API version**, not by file-copy parity:

- The gateway reports `api_version` (and informational `version`) on `GET /health`.
- The client sends its API version on every request via the `X-SM-Api-Version` header.
- `API_VERSION` is defined in `coordinator.py` (server) and `memory_bridge.py` (client).
  It bumps **only** on a breaking change to request/response shape, auth, or routes —
  not on every release.

Two ways skew surfaces:

1. **Caller-facing** — any agent can run the doctor command:
   ```bash
   uv run --with httpx python ~/.claude/skills/shared-memory/scripts/memory_bridge.py doctor
   ```
   It prints `compat: ok | incompatible | unknown` and, on skew, names which side
   to upgrade. The same warning is appended to error output when a request fails.

2. **Gateway-log** — when a client sends a mismatched `X-SM-Api-Version`, the
   coordinator logs a one-time warning naming the agent and the version gap.

When you bump `API_VERSION`, bump it in **both** `coordinator.py` and
`memory_bridge.py`, then `git pull` + restart the gateway and re-sync the skills.

---

## Health and observability

```bash
curl http://localhost:8888/health
```

An anonymous caller sees `status`/`version`/`api_version` only (S-10, v0.9.9)
— every other field requires a valid agent bearer token
(`-H "Authorization: Bearer <token>"`; any agent token works, this is not
role-gated). The backend roster and per-backend pool state are operational
detail about this deployment, not something every unauthenticated caller on
the network should learn.

| Field | Meaning | Anonymous? |
|---|---|---|
| `status` | `ok` \| `degraded` \| `down`, derived from `dependencies` and `warnings` (decision:1785) — the HTTP code is a separate question: **503 iff the embedder or reranker is down**, every other verdict is served 200 with the enum | yes |
| `version` / `api_version` | build version / wire contract | yes |
| `dependencies.{postgres,neo4j,embedder,reranker,llm_pool,rem_daemon,nrem_daemon,outbox,registry}` | one `{state, reason}` per dependency — see `Documentation/telemetry-contract.md` for the full enum vocabulary per key | authenticated only |
| `embedder` / `reranker` / `llm` | upstream backend reachability | authenticated only |
| `daemon` / `rem_daemon` | NREM / REM liveness (PID check only — see `dependencies.{rem,nrem}_daemon` above for the actual health verdict) | authenticated only |
| `auth_required` | whether `AGENT_TOKENS` is set | authenticated only |
| `config` | resolved LLM backend roster, pool tuning, affinity config | authenticated only |

⚠ **A fresh, undeclared install now reads `degraded` on `llm_pool`, not `ok`**
(W2, decision:1832). Before this, a zero-config gateway with something merely
*serving* the implicit fallback (`LLM_DEFAULT_TARGET`) read
`ok` — indistinguishable from a deliberately configured fleet. It now reads
`degraded` with a reason naming exactly that: nothing was declared, and the
built-in fallback is what answered. This is a deliberate visibility change
ahead of the fallback's eventual retirement — declare backends explicitly
(`bash shared-memory/ops/install_llm_backends.sh`) to clear it. The same
principle applies to `rem_daemon`/`nrem_daemon`: a fleet where no backend
counts toward a dream slot now reads `degraded` there instead of silently
never running REM/NREM while both read `ok`.

The gateway auto-restarts a crashed daemon with exponential backoff; a circuit
breaker stops after 5 crashes in 10 minutes (restart the gateway to reset). Set
`AUDIT_LOG_PATH` to capture a JSON-lines log of outbox rows.
