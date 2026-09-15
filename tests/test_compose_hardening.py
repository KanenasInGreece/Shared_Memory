"""Verify compose hardening in postgres_neo4j_limits.yaml (Slice 8).

No PyYAML: sibling compose tests render through `docker compose config`.
This file reads the shipped YAML as text so collection works in the
documented `uv run --with pytest …` extra set (fact:2523).
"""
from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "shared-memory" / "ops" / "postgres_neo4j_limits.yaml"

SERVICES = [
    "neo4j",
    "postgres",
    "retriever-api",
    "reranker-api",
    "retriever-api-gpu",
    "reranker-api-gpu",
]

_SVC_START = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
_UNNEEDED = ("AUDIT_WRITE", "MKNOD", "NET_RAW", "SETFCAP", "SYS_CHROOT")


def _service_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = _SVC_START.match(line)
        if m:
            current = m.group(1)
            blocks[current] = []
            continue
        if current is not None:
            if line.startswith("  ") and not line.startswith("    ") and line.strip():
                current = None
                continue
            blocks[current].append(line)
    return {k: "\n".join(v) for k, v in blocks.items()}


def _cap_drop(block: str) -> set[str]:
    caps: set[str] = set()
    in_drop = False
    for line in block.splitlines():
        if re.match(r"^\s+cap_drop:\s*$", line):
            in_drop = True
            continue
        if in_drop:
            item = re.match(r"^\s+- (\S+)\s*$", line)
            if item:
                caps.add(item.group(1))
                continue
            in_drop = False
    return caps


def test_compose_services_security_opt():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    blocks = _service_blocks(text)
    for svc in SERVICES:
        assert svc in blocks, f"{svc} missing from compose"
        assert "no-new-privileges:true" in blocks[svc], (
            f"{svc} missing no-new-privileges:true"
        )


def test_compose_services_curated_cap_drop_keeps_setuid_setgid():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    blocks = _service_blocks(text)
    for svc in SERVICES:
        drops = _cap_drop(blocks[svc])
        assert drops, f"{svc} missing cap_drop"
        for unneeded in _UNNEEDED:
            assert unneeded in drops, f"{svc} does not drop {unneeded}"
        assert "ALL" not in drops, f"{svc} dropped ALL instead of curated list"
        assert "SETUID" not in drops, f"{svc} dropped SETUID which gosu requires"
        assert "SETGID" not in drops, f"{svc} dropped SETGID which gosu requires"


def test_no_read_only_on_data_mounts():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    blocks = _service_blocks(text)
    for line in blocks["postgres"].splitlines():
        if "volumes:" in line or line.strip().startswith("- "):
            if ":" in line:
                assert ":ro" not in line, f"Postgres volume {line!r} must not be read-only"
    for line in blocks["neo4j"].splitlines():
        if "/data" in line:
            assert ":ro" not in line, f"Neo4j data volume {line!r} must not be read-only"
