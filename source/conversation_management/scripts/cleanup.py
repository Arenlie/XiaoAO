from __future__ import annotations

import asyncio

from app.config import get_settings
from app.container import create_container
from app.security_context import rls_bypass_scope
from app.workers.cleanup_worker import CleanupWorker


async def main() -> None:
    settings = get_settings()
    container = create_container(settings)
    try:
        with rls_bypass_scope():
            worker = CleanupWorker(
                container.database.session_factory,
                settings,
                container.attachment_service,
            )
            count = await worker.run_once()
        print({
            "purged_conversations": count,
            "purged_attachments": worker.last_attachment_count,
        })
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
