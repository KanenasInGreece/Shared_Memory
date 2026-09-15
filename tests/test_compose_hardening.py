"""Verify compose hardening in postgres_neo4j_limits.yaml (Slice 8).

Ensures all six services declare:
  - security_opt: no-new-privileges:true
  - curated cap_drop keeping SETUID and SETGID for gosu
  - no read_only data volumes on Postgres or Neo4j
"""
import yaml
from pathlib import Path

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


def test_compose_services_security_opt():
    with open(COMPOSE_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    for svc in SERVICES:
        cfg = data["services"][svc]
        assert "security_opt" in cfg, f"{svc} missing security_opt"
        assert "no-new-privileges:true" in cfg["security_opt"], (
            f"{svc} missing no-new-privileges:true"
        )


def test_compose_services_curated_cap_drop_keeps_setuid_setgid():
    with open(COMPOSE_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    for svc in SERVICES:
        cfg = data["services"][svc]
        assert "cap_drop" in cfg, f"{svc} missing cap_drop"
        drops = set(cfg["cap_drop"])
        # Dangerous unneeded capabilities are dropped
        for unneeded in ("AUDIT_WRITE", "MKNOD", "NET_RAW", "SETFCAP", "SYS_CHROOT"):
            assert unneeded in drops, f"{svc} does not drop {unneeded}"
        # Critical privileges for gosu to switch user MUST NOT be dropped
        assert "ALL" not in drops, f"{svc} dropped ALL instead of curated list"
        assert "SETUID" not in drops, f"{svc} dropped SETUID which gosu requires"
        assert "SETGID" not in drops, f"{svc} dropped SETGID which gosu requires"


def test_no_read_only_on_data_mounts():
    with open(COMPOSE_FILE, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Postgres data dir must be writable
    pg_vols = data["services"]["postgres"]["volumes"]
    for v in pg_vols:
        assert ":ro" not in v, f"Postgres volume {v} must not be read-only"

    # Neo4j data dir must be writable
    neo4j_vols = data["services"]["neo4j"]["volumes"]
    for v in neo4j_vols:
        if "/data" in v:
            assert ":ro" not in v, f"Neo4j data volume {v} must not be read-only"

