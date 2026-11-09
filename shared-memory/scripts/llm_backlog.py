"""Postgres rows for a dream item the gateway or a daemon will not store.

Read before a call. A read error fails the cycle closed, so a dark backlog
cannot send the item again. Writes use their own autocommit connection.
Retry rules live in llm_eligibility, which has no driver: no_context when the
largest context grew, cut_off when the largest reply cap grew, loop /
unparseable / unrecognised when the pool id changed, connection_lost,
write_failed and rewrite_failed when the boot id changed. An unrelated list
change retries neither no_context nor cut_off. rewrite_failed is a tier-2
graph write that did not stick (brief §3.2); eligible_write_failed still
matches only write_failed, so the release pass never clears a tier-2 node.
"""
import psycopg2
import psycopg2.extensions

from secure_env import get_secret, pg_dsn
from llm_eligibility import (  # noqa: F401 — re-exported; the gateway imports llm_eligibility itself
    BOOT_REASONS as _BOOT_REASONS,
    POOL_REASONS as _POOL_REASONS,
    eligible,
    identity,
)


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
