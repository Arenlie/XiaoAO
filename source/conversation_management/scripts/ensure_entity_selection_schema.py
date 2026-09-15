from __future__ import annotations

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import all_models  # noqa: F401
from app.models.entity_selection import PendingEntitySelection


async def ensure_schema() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: PendingEntitySelection.__table__.create(
                    bind=sync_connection,
                    checkfirst=True,
                )
            )
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS conversation_security"))
            await connection.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION conversation_security.current_user_token()
                    RETURNS text LANGUAGE sql STABLE AS $$
                      SELECT COALESCE(current_setting('app.user_token', true), '')
                    $$
                    """
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE OR REPLACE FUNCTION conversation_security.bypass_rls()
                    RETURNS boolean LANGUAGE sql STABLE AS $$
                      SELECT COALESCE(current_setting('app.rls_bypass', true), 'off') = 'on'
                    $$
                    """
                )
            )
            await connection.execute(
                text("ALTER TABLE pending_entity_selections ENABLE ROW LEVEL SECURITY")
            )
            await connection.execute(
                text("ALTER TABLE pending_entity_selections FORCE ROW LEVEL SECURITY")
            )
            await connection.execute(
                text(
                    "DROP POLICY IF EXISTS tenant_isolation_pending_entity_selections "
                    "ON pending_entity_selections"
                )
            )
            await connection.execute(
                text(
                    """
                    CREATE POLICY tenant_isolation_pending_entity_selections
                    ON pending_entity_selections FOR ALL
                    USING (
                      conversation_security.bypass_rls()
                      OR EXISTS (
                        SELECT 1 FROM conversations c
                        WHERE c.id = pending_entity_selections.conversation_id
                          AND c.user_token = conversation_security.current_user_token()
                      )
                    )
                    WITH CHECK (
                      conversation_security.bypass_rls()
                      OR EXISTS (
                        SELECT 1 FROM conversations c
                        WHERE c.id = pending_entity_selections.conversation_id
                          AND c.user_token = conversation_security.current_user_token()
                      )
                    )
                    """
                )
            )
        print("Entity selection schema is ready.")
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(ensure_schema())


if __name__ == "__main__":
    main()
