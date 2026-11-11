"""Fetchrow routing for save fixtures whose content is new unless specified."""
from unittest.mock import AsyncMock


def save_fetchrow(*, prior=None, **kwargs):
    """Keep the content-hash lock read separate from existing query responses.

    A supplied prior row must contain both selected columns, id and metadata.
    The default models a new content hash, not an incomplete database row.
    """
    other_queries = AsyncMock(**kwargs)

    async def fetchrow(sql, *args):
        if ' '.join(sql.split()) in (
            'SELECT id, metadata FROM technical_docs WHERE content_hash = $1 FOR UPDATE',
            'SELECT id FROM technical_docs WHERE content_hash = $1 FOR UPDATE',
        ):
            return prior
        return await other_queries(sql, *args)

    return AsyncMock(side_effect=fetchrow)
