from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from app.config import Settings
from app.security_context import (
    get_current_request_id,
    get_current_user_token,
    is_rls_bypass,
)


class TenantSyncSession(Session):
    """Sync session used internally by AsyncSession for transaction hooks."""


class TenantAsyncSession(AsyncSession):
    sync_session_class = TenantSyncSession


@event.listens_for(TenantSyncSession, "after_begin")
def _set_postgresql_rls_context(
    session: TenantSyncSession, transaction, connection
) -> None:
    """Bind tenant context to each PostgreSQL transaction with SET LOCAL semantics."""

    if connection.dialect.name != "postgresql":
        return
    enabled = bool(session.info.get("postgresql_rls_enabled", True))
    user_token = get_current_user_token() or ""
    bypass = "on" if (not enabled or is_rls_bypass()) else "off"
    request_id = get_current_request_id() or ""
    connection.execute(
        text("SELECT set_config('app.user_token', :value, true)"),
        {"value": user_token},
    )
    connection.execute(
        text("SELECT set_config('app.rls_bypass', :value, true)"),
        {"value": bypass},
    )
    connection.execute(
        text("SELECT set_config('app.request_id', :value, true)"),
        {"value": request_id},
    )


class Database:
    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            connect_args={"command_timeout": settings.database_command_timeout_seconds},
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=TenantAsyncSession,
            expire_on_commit=False,
            autoflush=False,
            info={"postgresql_rls_enabled": settings.postgresql_rls_enabled},
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def close(self) -> None:
        await self.engine.dispose()
