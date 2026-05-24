# Changelog

All notable changes to the Shared Memory Framework are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

- **Audit logging** — `_append_log()` helper in `memory_bridge.py` (both copies) and `vector-skill.py`. Controlled by two new env vars:
  - `MEMORY_LOG_LEVEL` — `0` (off, default) through `4` (full content copy with size warning)
  - `MEMORY_LOG_PATH` — log directory, defaults to `~/.shared-memory/logs`

- **Per-tool log files with write/rotate separation** — `memory_bridge.log` (Claude Code + Gemini CLI) and `vector_skill.log` (LM Studio MCP). The architectural decision is to make writing tools **append-only, never rotate**. Claude Code and Gemini CLI both invoke `memory_bridge.py` as separate short-lived OS processes; concurrent appends via `O_APPEND` are atomic on Linux for writes under `PIPE_BUF` (4096 bytes), so individual log lines are safe. Rotation across concurrent processes is not safe — a rename or truncate mid-write from one process corrupts the other's output. Separating the write responsibility (tools) from the rotate/merge responsibility (daemon) eliminates this class of race condition entirely.

- **Daily log merge in the consolidation daemon** — `merge_logs()` function in `consolidation_loop.py`. Triggered once per calendar day on the first 1-second poll of a new day. Uses the logrotate rename pattern: renames source files (writing tools create fresh files on next open), merges entries by timestamp, writes `shared_memory_YYYY-MM-DD.log.gz`. Appends to an existing archive for the same date if present. Handles entries spanning multiple days when the daemon was offline.

  **Why the consolidation daemon, not the proxy:** The Hive-Mind Gateway proxy was the initial candidate for triggering log merge, since it is the other long-running process in the stack. It was rejected because the proxy may be replaced by a lightweight LLM router — it is an infrastructure convenience, not an architectural constant. The consolidation daemon is the right owner: it is the one stable background process whose role is defined at the system level (not tied to any particular agent interface or transport), it already manages the sleep-cycle cleanup loop, and short-lived CLI invocations cannot perform time-based rotation reliably since they exit immediately after each save.

- **`test_logging.py`** — 30 new tests covering `_append_log` level filtering, per-tool routing, content size warnings, `save_artifact` logging integration at each event type, and all `merge_logs` paths.

### Changed

- **Metadata parsing in `save_artifact` — fail-fast replacing silent corruption** — the original code caught all exceptions with a bare `except:` (including `SystemExit` and `KeyboardInterrupt`) and fell back to `{"raw_metadata": metadata_json}`. This silent fallback was an architectural failure: the save would proceed and return `status: success`, but with no `entities` key in the stored metadata, the fact was permanently ineligible for Tier 3 consolidation — with no indication to the caller. The fix replaces the bare `except:` with `except (json.JSONDecodeError, ValueError)` and returns an explicit error instead of falling back. A second check validates that the parsed result is a `dict`: valid JSON that is not an object (e.g. `[1,2,3]`) would previously parse successfully but crash downstream on `m_data.get()`. Both error paths now log at level 2 and return `status: error` immediately.

- **`entities` extraction moved earlier** — `entities = m_data.get("entities", [])` now runs immediately after metadata parsing, before the Postgres insert. Previously it was defined inside the Neo4j `try` block; a Neo4j connection failure before that line left `entities` undefined, causing a `NameError` in the entities warning and success log calls. Moving it earlier ensures it is always in scope.

- **Entities warning in success response** — saves that succeed but include no `entities` now append `WARNING: No 'entities' in metadata — fact stored but ineligible for Tier 3 consolidation.` to the response message (both `memory_bridge.py` and `vector-skill.py`).

- **`.env.example`** — added `MEMORY_LOG_LEVEL` and `MEMORY_LOG_PATH` entries with inline documentation.

- **README** — added §14 Audit Logging; renumbered §14–18 → §15–19; added logging note to §12 (Save Path); updated §17 Testing table; updated §18 Observability open problem.
