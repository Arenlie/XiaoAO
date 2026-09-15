from __future__ import annotations

import asyncio
import os
import socket
from uuid import uuid4

import structlog

from app.config import Settings
from app.services.outbox_service import OutboxService

log = structlog.get_logger(__name__)


class OutboxWorker:
    def __init__(self, settings: Settings, outbox: OutboxService) -> None:
        self.settings = settings
        self.outbox = outbox
        self.worker_id = f"outbox-{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        log.info("outbox_worker_started", worker_id=self.worker_id)
        while not self._stopping.is_set():
            try:
                rows = await self.outbox.claim_batch(self.worker_id)
                if not rows:
                    await asyncio.sleep(self.settings.outbox_poll_interval_seconds)
                    continue
                results = await asyncio.gather(
                    *(self.outbox.publish_claimed(row) for row in rows),
                    return_exceptions=True,
                )
                for row, result in zip(rows, results, strict=True):
                    if isinstance(result, Exception):
                        log.warning(
                            "outbox_publish_retry_scheduled",
                            event_id=str(row.id),
                            event_type=row.event_type,
                            attempt=row.attempt_count,
                            error=str(result),
                        )
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("outbox_worker_loop_failed")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping.set()
