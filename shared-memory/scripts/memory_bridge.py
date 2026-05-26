"""
Memory Bridge — thin CLI client for the Memory Coordinator.

Delegates all storage I/O to the coordinator running inside hive_mind_proxy
on port 8888. Direct Postgres and Neo4j access has been removed; the
coordinator owns those connections.

CLI usage (unchanged from previous versions):
    python memory_bridge.py save   "<content>" '<metadata_json>'
    python memory_bridge.py search "<query>" [limit]
    python memory_bridge.py graph  "<cypher>"
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime

import httpx

COORDINATOR_BASE = os.environ.get("COORDINATOR_URL", "http://localhost:8888")
AGENT_ID         = os.environ.get("AGENT_ID", "memory_bridge")

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

# ── Audit logging ─────────────────────────────────────────────────────────────

_CONTENT_SIZE_WARN_BYTES = 10 * 1024


def _append_log(tool: str, min_level: int, event: str, data: dict, content: str = None) -> None:
    log_level = int(os.environ.get("MEMORY_LOG_LEVEL", "0"))
    if log_level < min_level:
        return
    log_dir = os.path.expanduser(os.environ.get("MEMORY_LOG_PATH", "~/.shared-memory/logs"))
    try:
        os.makedirs(log_dir, exist_ok=True)
        entry = {"ts": datetime.now().isoformat(), "tool": tool, "event": event, **data}
        if log_level >= 4 and content is not None:
            entry["content"] = content
            if len(content.encode()) > _CONTENT_SIZE_WARN_BYTES:
                entry["content_size_warn"] = (
                    f"content is {len(content.encode())} bytes"
                    " — reduce log level to avoid large logs"
                )
        with open(os.path.join(log_dir, f"{tool}.log"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break the save path


# ── Coordinator HTTP helpers ──────────────────────────────────────────────────

def _coordinator_unavailable(exc: Exception) -> dict:
    return {
        "status": "error",
        "message": (
            f"Memory coordinator unreachable at {COORDINATOR_BASE} — "
            f"is hive_mind_proxy.py running? ({exc})"
        ),
    }


async def save_artifact(content: str, metadata_json: str = "{}") -> dict:
    if isinstance(metadata_json, str):
        try:
            metadata = json.loads(metadata_json)
        except (json.JSONDecodeError, ValueError) as e:
            _append_log("memory_bridge", 2, "bad_metadata", {"error": str(e), "content_preview": content[:100]}, content)
            return {"status": "error", "message": f"Invalid metadata JSON: {e}"}
    else:
        metadata = metadata_json

    if not isinstance(metadata, dict):
        _append_log("memory_bridge", 2, "bad_metadata_type", {"got": type(metadata).__name__, "content_preview": content[:100]}, content)
        return {"status": "error", "message": f"Metadata must be a JSON object, got {type(metadata).__name__}"}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/save",
                json={"content": content, "metadata": metadata, "agent_id": AGENT_ID},
            )
            result = r.json()
    except Exception as exc:
        _append_log("memory_bridge", 2, "coordinator_down", {"content_preview": content[:100]}, content)
        return _coordinator_unavailable(exc)

    if result.get("status") == "success":
        pg_id    = result.get("pg_id")
        entities = metadata.get("entities", [])
        _append_log("memory_bridge", 3, "save_success",
                    {"pg_id": pg_id, "source": metadata.get("source"), "entity_count": len(entities)},
                    content)
        if not entities:
            _append_log("memory_bridge", 1, "no_entities", {"pg_id": pg_id, "source": metadata.get("source")}, content)
    else:
        _append_log("memory_bridge", 2, "save_failed", {"response": result, "content_preview": content[:100]}, content)

    return result


async def search_and_rerank(query: str, limit: int = 5) -> list | dict:
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{COORDINATOR_BASE}/memory/search",
                json={"query": query, "limit": limit, "agent_id": AGENT_ID},
            )
            result = r.json()
    except Exception as exc:
        return _coordinator_unavailable(exc)

    return result.get("results", result)


def query_graph(cypher: str, params: dict = None) -> list | dict:
    try:
        r = httpx.post(
            f"{COORDINATOR_BASE}/memory/graph",
            json={"cypher": cypher, "params": params or {}},
            timeout=30.0,
        )
        result = r.json()
    except Exception as exc:
        return _coordinator_unavailable(exc)

    return result.get("records", result)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({
            "error": "Usage: python memory_bridge.py [graph|search|save] <query/content> [metadata/limit]"
        }))
        sys.exit(1)

    action = sys.argv[1]

    if action == "graph":
        print(json.dumps(query_graph(sys.argv[2]), indent=2))
    elif action == "search":
        limit = int(sys.argv[3]) if len(sys.argv) > 3 else 5
        print(json.dumps(await search_and_rerank(sys.argv[2], limit), indent=2))
    elif action == "save":
        metadata = sys.argv[3] if len(sys.argv) > 3 else "{}"
        print(json.dumps(await save_artifact(sys.argv[2], metadata), indent=2))
    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))


if __name__ == "__main__":
    asyncio.run(main())
