from __future__ import annotations

from contextlib import asynccontextmanager

from app.config import Settings


def _checkpoint_url(settings: Settings) -> str:
    raw = settings.langgraph_checkpoint_database_url or settings.database_url
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


@asynccontextmanager
async def open_checkpointer(settings: Settings):
    if not settings.langgraph_checkpoint_enabled:
        yield None
        return
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError(
            "LANGGRAPH_CHECKPOINT_ENABLED=true but langgraph-checkpoint-postgres is missing"
        ) from exc
    async with AsyncPostgresSaver.from_conn_string(_checkpoint_url(settings)) as saver:
        if settings.langgraph_checkpoint_setup_on_start:
            await saver.setup()
        yield saver
