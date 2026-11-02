"""Postgres rows for a dream item the gateway or a daemon will not store.

Read before a call. A read error fails the cycle closed, so a dark backlog
cannot send the item again. Writes use their own autocommit connection.
Retry rules: no_context when the largest context grew,
cut_off when the largest reply cap grew, loop / unparseable / unrecognised
when the pool id changed, connection_lost and write_failed when the boot id
changed. An unrelated list change retries neither no_context nor cut_off.
"""
import os

import psycopg2
import psycopg2.extensions

from secure_env import get_secret, pg_dsn

_POOL_REASONS = frozenset({"loop", "unparseable", "unrecognised"})
_BOOT_REASONS = frozenset({"connection_lost", "write_failed"})


def identity() -> dict:
    """Spawn values the gateway put on this process."""
    def _int(name: str) -> int:
        try:
            return int(os.environ.get(name) or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "pool_id": os.environ.get("SM_LLM_POOL_ID") or "",
        "largest_ctx": _int("SM_LLM_LARGEST_CTX"),
        "largest_reply": _int("SM_LLM_LARGEST_REPLY"),
        "boot_id": os.environ.get("SM_GATEWAY_BOOT_ID") or "",
    }


def eligible(reason: str, stored_ctx, stored_reply, stored_pool, stored_boot,
             ident: dict | None = None) -> bool:
    """True when this stored row may be tried again under ident."""
    ident = ident or identity()
    if reason == "no_context":
        return ident["largest_ctx"] > (stored_ctx or 0)
    if reason == "cut_off":
        return ident["largest_reply"] > (stored_reply or 0)
    if reason in _POOL_REASONS:
        return ident["pool_id"] != (stored_pool or "")
    if reason in _BOOT_REASONS:
        return ident["boot_id"] != (stored_boot or "")
    return False


def _connect():
    explicit = get_secret("PG_CONN", "")
    dsn = explicit or pg_dsn(get_secret("PG_PASSWORD", ""))
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def eligible_write_failed(item_kind: str) -> list[str]:
    """write_failed keys this boot id may retry. Raises if the table cannot be read."""
    ident = identity()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_key, reason, largest_ctx, largest_reply, pool_id, boot_id"
                " FROM llm_backlog WHERE item_kind = %s AND reason = 'write_failed'",
                (item_kind,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    out: list[str] = []
    for key, reason, ctx, reply, pool, boot in rows:
        if eligible(reason, ctx, reply, pool, boot, ident):
            out.append(str(key))
    return out


def blocked_keys(item_kind: str) -> set[str]:
    """Item keys that must not be sent. Raises if the table cannot be read."""
    ident = identity()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT item_key, reason, largest_ctx, largest_reply, pool_id, boot_id"
                " FROM llm_backlog WHERE item_kind = %s",
                (item_kind,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    blocked: set[str] = set()
    for key, reason, ctx, reply, pool, boot in rows:
        if not eligible(reason, ctx, reply, pool, boot, ident):
            blocked.add(str(key))
    return blocked


def upsert(item_kind: str, item_key, reason: str, specimen: str | None = None) -> None:
    """Insert or replace the row for this item. The specimen is the tail only."""
    ident = identity()
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_backlog"
                " (item_kind, item_key, reason, largest_ctx, largest_reply,"
                "  pool_id, boot_id, specimen, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())"
                " ON CONFLICT (item_kind, item_key) DO UPDATE SET"
                " reason = EXCLUDED.reason,"
                " largest_ctx = EXCLUDED.largest_ctx,"
                " largest_reply = EXCLUDED.largest_reply,"
                " pool_id = EXCLUDED.pool_id,"
                " boot_id = EXCLUDED.boot_id,"
                " specimen = EXCLUDED.specimen,"
                " updated_at = now()",
                (
                    item_kind, str(item_key), reason,
                    ident["largest_ctx"], ident["largest_reply"],
                    ident["pool_id"], ident["boot_id"], specimen,
                ),
            )
    finally:
        conn.close()


def delete(item_kind: str, item_key) -> None:
    """Drop the row after the item is stored. A missing row is fine."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM llm_backlog WHERE item_kind = %s AND item_key = %s",
                (item_kind, str(item_key)),
            )
    finally:
        conn.close()
