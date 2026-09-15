from __future__ import annotations

import asyncio
import os
import signal

from app.config import get_settings
from app.container import create_container
from app.logging import configure_logging
from app.orchestration.checkpoint import open_checkpointer
from app.orchestration.nodes import ConversationGraphNodes
from app.orchestration.runner import ConversationGraphRunner
from app.security_context import rls_bypass_scope
from app.workers.cleanup_worker import DifyCleanupWorker
from app.workers.generation_worker import GenerationWorker
from app.workers.outbox_worker import OutboxWorker
from app.workers.summary_worker import SummaryWorker
from app.workers.title_worker import TitleWorker


async def main() -> None:
    settings = get_settings()
    configure_logging(settings, component=f"worker-{os.getenv('WORKER_INSTANCE', '1')}")
    container = create_container(settings)
    try:
        async with open_checkpointer(settings) as checkpointer:
            graph_nodes = ConversationGraphNodes(
                registry_service=container.agent_registry_service,
                content_understanding_service=container.content_understanding_service,
                supervisor=container.supervisor_agent,
                agent_executor=container.agent_executor,
                tool_registry=container.tool_registry,
                tool_executor=container.tool_executor,
                workflow_registry=container.workflow_registry,
                entity_resolution_layer=container.entity_resolution_layer,
            )
            graph_runner = ConversationGraphRunner(
                settings=settings,
                nodes=graph_nodes,
                checkpointer=checkpointer,
            )
            generation = GenerationWorker(
                session_factory=container.database.session_factory,
                redis=container.redis_manager.client,
                settings=settings,
                queue=container.queue,
                events=container.events,
                concurrency=container.concurrency,
                graph_runner=graph_runner,
                context_builder=container.context_builder,
                evidence_workspace_service=container.evidence_workspace_service,
                outbox=container.outbox,
            )
            outbox = OutboxWorker(settings, container.outbox)
            title = TitleWorker(
                container.database.session_factory,
                container.redis_manager.client,
                container.queue,
                container.events,
                container.title_client,
                settings,
            )
            summary = SummaryWorker(
                container.database.session_factory,
                container.redis_manager.client,
                settings,
                container.queue,
            )
            dify_cleanup = DifyCleanupWorker(
                container.redis_manager.client,
                container.queue,
                container.agent_client,
                settings,
            )

            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop_event.set)

            with rls_bypass_scope():
                tasks = [
                    asyncio.create_task(generation.run()),
                    asyncio.create_task(summary.run()),
                    asyncio.create_task(dify_cleanup.run()),
                ]
                if settings.outbox_enabled:
                    tasks.append(asyncio.create_task(outbox.run()))
                if settings.title_mode != "rule":
                    tasks.append(asyncio.create_task(title.run()))

                await stop_event.wait()
                await generation.stop()
                await outbox.stop()
                await title.stop()
                await summary.stop()
                await dify_cleanup.stop()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
