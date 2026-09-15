from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.agents.contracts import AgentExecutionRuntime
from app.config import Settings
from app.domain.error_contract import default_public_message
from app.domain.enums import ExecutionMode, GenerationTaskStatus, MessageStatus
from app.domain.exceptions import AppError
from app.integrations.dify.entity_identity import build_candidate_id
from app.integrations.dify.think_filter import strip_think_blocks
from app.metrics import TASKS_ACTIVE
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.entity_selection import PendingEntitySelection
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.models.profile import UserProfile
from app.models.query_statistics import QueryStatistic
from app.orchestration.runner import ConversationGraphRunner
from app.orchestration.runtime import GraphRuntimeContext
from app.performance import build_performance_report
from app.services.concurrency_service import ConcurrencyService
from app.services.context_builder import ContextBuilder
from app.services.event_service import EventService
from app.services.outbox_service import OutboxService
from app.services.queue_service import QueueService
from app.telemetry import set_attributes, tracer, use_extracted_context

log = structlog.get_logger(__name__)
_worker_tracer = tracer(__name__)
_TERMINAL_STATUSES = {
    GenerationTaskStatus.COMPLETED.value,
    GenerationTaskStatus.FAILED.value,
    GenerationTaskStatus.STOPPED.value,
    GenerationTaskStatus.TIMEOUT.value,
    GenerationTaskStatus.WAITING_SELECTION.value,
    GenerationTaskStatus.WAITING_CONFIRMATION.value,
}


class GenerationWorker:
    def __init__(
        self,
        *,
        session_factory,
        redis: Redis,
        settings: Settings,
        queue: QueueService,
        events: EventService,
        concurrency: ConcurrencyService,
        graph_runner: ConversationGraphRunner,
        context_builder: ContextBuilder,
        evidence_workspace_service,
        outbox: OutboxService,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings
        self.queue = queue
        self.events = events
        self.concurrency = concurrency
        self.graph_runner = graph_runner
        self.context_builder = context_builder
        self.evidence_workspace_service = evidence_workspace_service
        self.outbox = outbox
        self.consumer = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
        self.semaphore = asyncio.Semaphore(settings.worker_concurrency)
        self._running: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        await self.queue.ensure_groups()
        await self._claim_stale()
        log.info("generation_worker_started", consumer=self.consumer)
        while not self._stopping.is_set():
            try:
                result = await self.redis.xreadgroup(
                    groupname=self.queue.GENERATION_GROUP,
                    consumername=self.consumer,
                    streams={self.queue.GENERATION_QUEUE: ">"},
                    count=self.settings.generation_queue_batch_size,
                    block=self.settings.generation_queue_block_ms,
                )
                if not result:
                    continue
                for _, records in result:
                    for stream_id, fields in records:
                        running = asyncio.create_task(
                            self._guarded_process(str(stream_id), fields)
                        )
                        self._running.add(running)
                        running.add_done_callback(self._running.discard)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("generation_worker_loop_failed")
                await asyncio.sleep(1)

    async def stop(self) -> None:
        self._stopping.set()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)

    async def _claim_stale(self) -> None:
        start_id = "0-0"
        while True:
            try:
                response = await self.redis.xautoclaim(
                    self.queue.GENERATION_QUEUE,
                    self.queue.GENERATION_GROUP,
                    self.consumer,
                    min_idle_time=max(
                        30000, self.settings.worker_heartbeat_seconds * 3000
                    ),
                    start_id=start_id,
                    count=self.settings.generation_queue_batch_size,
                )
            except Exception:
                return
            if not response or len(response) < 2:
                return
            start_id, records = str(response[0]), response[1]
            for stream_id, fields in records:
                running = asyncio.create_task(
                    self._guarded_process(str(stream_id), fields)
                )
                self._running.add(running)
                running.add_done_callback(self._running.discard)
            if not records or start_id == "0-0":
                return

    async def _guarded_process(self, stream_id: str, fields: dict[str, str]) -> None:
        trace_context = self.queue.trace_context_from_fields(fields)
        with use_extracted_context(trace_context):
            with _worker_tracer.start_as_current_span("generation.consume") as span:
                set_attributes(
                    span,
                    {
                        "messaging.system": "redis",
                        "messaging.operation": "process",
                        "messaging.message.id": stream_id,
                        "generation.task_id": fields.get("task_id"),
                        "outbox.event_id": fields.get("outbox_id"),
                    },
                )
                async with self.semaphore:
                    heartbeat = asyncio.create_task(
                        self._heartbeat_pending(stream_id, fields.get("task_id"))
                    )
                    try:
                        await self._process(fields)
                    finally:
                        heartbeat.cancel()
                        with suppress(asyncio.CancelledError):
                            await heartbeat
                        await self.redis.xack(
                            self.queue.GENERATION_QUEUE,
                            self.queue.GENERATION_GROUP,
                            stream_id,
                        )

    async def _heartbeat_pending(self, stream_id: str, raw_task_id: str | None) -> None:
        while True:
            await asyncio.sleep(self.settings.worker_heartbeat_seconds)
            with suppress(Exception):
                await self.redis.xclaim(
                    self.queue.GENERATION_QUEUE,
                    self.queue.GENERATION_GROUP,
                    self.consumer,
                    min_idle_time=0,
                    message_ids=[stream_id],
                    justid=True,
                )
            if raw_task_id:
                with suppress(Exception):
                    async with self.session_factory() as session, session.begin():
                        task = await session.get(GenerationTask, UUID(raw_task_id))
                        if task is not None and task.status not in _TERMINAL_STATUSES:
                            task.heartbeat_at = datetime.now(UTC)
                    raw = await self.redis.get(f"chat:task_context:{raw_task_id}")
                    if raw:
                        context = json.loads(raw)
                        if context.get("lease_id"):
                            await self.concurrency.renew(context["conversation_id"], context["user_token"], context["lease_id"])

    async def _process(self, fields: dict[str, str]) -> None:
        raw_task_id = fields.get("task_id")
        if not raw_task_id:
            return
        task_id = UUID(raw_task_id)
        context_key = f"chat:task_context:{task_id}"
        raw_context = await self.redis.get(context_key)
        if not raw_context:
            await self._fail_task(
                task_id, "TASK_CONTEXT_EXPIRED", "任务临时上下文已过期"
            )
            return

        context: dict[str, Any] = json.loads(raw_context)
        user_token = str(context["user_token"])
        conversation_id = UUID(str(context["conversation_id"]))
        worker_start = time.perf_counter()
        release_required = False
        metric_active = False
        graph_runtime = None

        try:
            loaded, release_required = await self._load_and_start(task_id)
            if loaded is None:
                if release_required and await self._is_cancelled(task_id):
                    await self.events.publish(
                        task_id,
                        "task.stopped",
                        {"task_id": str(task_id)},
                        actor_type="worker",
                        actor_id=self.consumer,
                        status="STOPPED",
                    )
                return

            TASKS_ACTIVE.inc()
            metric_active = True
            task, conversation, branch, user_message, assistant_message, profile = loaded
            mode = self._resolve_execution_mode(task, context, user_message)
            task.execution_mode = mode

            await self.events.publish(
                task_id,
                "task.started",
                {
                    "task_id": str(task_id),
                    "worker_id": self.consumer,
                    "execution_mode": mode,
                    "queue_wait_ms": (
                        round((datetime.now(UTC) - task.created_at).total_seconds() * 1000, 3)
                        if task.created_at else None
                    ),
                    "performance_monitor_enabled": self.settings.performance_monitor_enabled,
                },
                graph_mode=mode,
                actor_type="worker",
                actor_id=self.consumer,
                status="STARTED",
            )
            await self.events.publish(
                task_id,
                "graph.selected",
                {"execution_mode": mode},
                graph_mode=mode,
                actor_type="graph_router",
                actor_id="execution_mode_router",
                status="COMPLETED",
            )

            if await self._is_cancelled(task_id):
                await self._finish_stopped(task, assistant_message, mode)
                return

            before_id = context.get("context_before_message_id")
            before_leaf_id = UUID(before_id) if before_id else None
            memory_context = await self._build_memory_context(branch.id, before_leaf_id)
            if bool(getattr(self.settings, "evidence_fabric_enabled", True)):
                evidence_context = await self.evidence_workspace_service.prepare_context(
                    conversation_id=conversation.id,
                    query=str(context.get("query") or ""),
                    legacy_context=memory_context,
                )
                memory_context = {**memory_context, **evidence_context}
            active_entity = dict(branch.active_entity or {})
            selected_entity = dict(context.get("selected_entity") or {})
            if selected_entity:
                active_entity = selected_entity

            active_diagnosis_task = None
            if mode != ExecutionMode.QUICK.value and branch.dify_conversation_id:
                active_diagnosis_task = {
                    "diagnosis_task_id": branch.dify_conversation_id,
                    "external_conversation_id": branch.dify_conversation_id,
                    "external_message_id": assistant_message.dify_message_id,
                    "conversation_id": str(conversation.id),
                    "branch_id": str(branch.id),
                    "status": "ACTIVE",
                    "resolved_entity": active_entity,
                }

            stored_entity_result = dict(user_message.entity_result or {})
            selection_resume = bool(
                selected_entity and context.get("selection_resume")
            )
            initial_state: dict[str, Any] = {
                "task_id": str(task.id),
                "conversation_id": str(conversation.id),
                "branch_id": str(branch.id),
                "user_message_id": str(user_message.id),
                "assistant_message_id": str(assistant_message.id),
                "app_code": conversation.app_code,
                "operation": task.operation,
                "graph_attempt": int(context.get("graph_attempt") or 0),
                "selection_resume": selection_resume,
                "diagnosis_choice": dict(context.get("diagnosis_choice") or {}),
                "diagnosis_resume_state": dict(context.get("diagnosis_resume_state") or {}),
                "pending_diagnosis_context": dict(context.get("pending_diagnosis_context") or {}),
                "execution_mode": mode,
                "query": str(context["query"]),
                "locale": "zh-CN",
                "attachments": list(
                    (user_message.metadata_json or {}).get("attachments") or []
                ),
                "memory_context": memory_context,
                "evidence_catalog": list(memory_context.get("evidence_catalog") or []),
                "recent_messages": memory_context.get("recent_messages", []),
                "recent_entities": memory_context.get("recent_entities", []),
                "user_profile": (
                    profile
                    if isinstance(profile, dict)
                    else (profile.profile_json if profile else {})
                ),
                "active_entity": active_entity,
                "selected_entity": selected_entity,
                "resolved_entity": selected_entity,
                "entity_result": stored_entity_result,
                "query_scope": dict(stored_entity_result.get("query_scope") or {}),
                "entity_constraints": dict(
                    stored_entity_result.get("entity_constraints") or {}
                ),
                "business_intent": (
                    dict(context.get("selection_business_intent") or {})
                    if selection_resume
                    else {}
                ),
                "business_workflow": (
                    dict(context.get("selection_business_workflow") or {})
                    if selection_resume
                    else {}
                ),
                "active_diagnosis_task": active_diagnosis_task,
                "content_envelope": {},
                "understanding_results": list(context.get("selection_understanding_results") or []) if selection_resume else [],
                "observations": [],
                "external_ids": {
                    "task_id": task.dify_task_id,
                    "message_id": assistant_message.dify_message_id,
                    "conversation_id": branch.dify_conversation_id,
                },
                "errors": [],
            }

            async def is_cancelled() -> bool:
                return await self._is_cancelled(task_id)

            async def cache_external_ids(ids: dict[str, str | None]) -> None:
                await self._cache_external_ids(context_key, ids)

            agent_runtime = AgentExecutionRuntime(
                task_id=task_id,
                task_start_monotonic=worker_start,
                user_token=user_token,
                data_access_token=str(context.get("data_access_token") or ""),
                context_key=context_key,
                request_id=task.request_id or "",
                execution_mode=mode,
                event_service=self.events,
                redis=self.redis,
                settings=self.settings,
                is_cancelled=is_cancelled,
                cache_external_ids=cache_external_ids,
            )
            graph_runtime = GraphRuntimeContext(
                agent_runtime=agent_runtime,
                set_task_status=lambda status: self._set_task_status(task_id, status),
                set_task_streaming=lambda: self._set_task_and_message_streaming(
                    task.id, assistant_message.id
                ),
                transient_tool_payloads={},
                workspace_service=self.evidence_workspace_service,
            )
            outcome = await self.graph_runner.run(initial_state, graph_runtime)
            state = outcome.state
            answer_meta = dict(graph_runtime.transient_tool_payloads.get("_answer_generation") or {})
            answer_snapshot = {key:value for key,value in answer_meta.items()
                if key in {"generation_id", "generation_span_id", "citations", "reasoning_available"}}
            answer_snapshot.update(content=outcome.final_answer, replace=True, channel="final")

            entity_result = dict(state.get("entity_result") or {})
            if entity_result:
                await self._save_entity_result(
                    task_id,
                    user_message.id,
                    entity_result.get("workflow_run_id"),
                    entity_result,
                )
            resolved_entity = dict(state.get("resolved_entity") or {})
            if resolved_entity:
                await self._update_active_entity(branch.id, resolved_entity)
            elif (
                bool(state.get("invalidate_active_entity"))
                and outcome.final_status != "WAITING_SELECTION"
            ):
                # The user explicitly introduced a new asset expression this turn,
                # but it has not been uniquely resolved.  Clear the stale branch anchor
                # unless the task is currently waiting for candidate selection.  While
                # WAITING_SELECTION, the conversation-level concurrency fence blocks a
                # new turn and TaskService writes the chosen entity atomically; skipping
                # the clear here avoids racing a very fast user selection.
                await self._update_active_entity(branch.id, {})

            external_ids = outcome.external_ids
            if outcome.final_status == "STOPPED":
                await self._persist_final(
                    task_id,
                    assistant_message.id,
                    conversation.id,
                    branch.id,
                    outcome.final_answer,
                    MessageStatus.STOPPED,
                    GenerationTaskStatus.STOPPED,
                    external_ids,
                )
                await self.events.publish(
                    task_id,
                    "task.stopped",
                    {
                        "task_id": str(task_id),
                        "partial_content": outcome.final_answer,
                    },
                    graph_mode=mode,
                    actor_type="worker",
                    actor_id=self.consumer,
                    status="STOPPED",
                )
                return

            if outcome.final_status == "WAITING_CONFIRMATION":
                await self._wait_for_diagnosis_confirmation(task, assistant_message, state, mode,
                    user_token=user_token, lease_id=context.get("lease_id"))
                return

            if outcome.final_status == "WAITING_SELECTION":
                await self._cache_selection_resume_state(context_key, state)
                await self._wait_for_entity_selection(
                    task=task,
                    assistant_message=assistant_message,
                    query=str(context.get("query") or ""),
                    entity_result=entity_result,
                    graph_mode=mode,
                )
                return

            if outcome.final_status == "FAILED":
                raw_error = entity_result.get("error") if isinstance(entity_result.get("error"), dict) else {}
                error_code = str(raw_error.get("code") or "WORKFLOW_FAILED")
                error_message = str(
                    raw_error.get("public_message")
                    or entity_result.get("message")
                    or "业务流程执行失败"
                )
                await self._persist_final(
                    task_id,
                    assistant_message.id,
                    conversation.id,
                    branch.id,
                    outcome.final_answer or error_message,
                    MessageStatus.FAILED,
                    GenerationTaskStatus.FAILED,
                    external_ids,
                )
                # Populate task error_code/error_message and publish the canonical
                # task.failed event. _fail_task does not overwrite persisted content.
                await self._fail_task(task_id, error_code, error_message)
                await self._log_performance_report_if_enabled(task_id)
                return

            if outcome.final_status == "WAITING_INPUT":
                await self._persist_final(
                    task_id,
                    assistant_message.id,
                    conversation.id,
                    branch.id,
                    outcome.final_answer,
                    MessageStatus.COMPLETED,
                    GenerationTaskStatus.WAITING_INPUT,
                    external_ids,
                    metadata_patch={
                        "clarification": dict(outcome.state.get("clarification_context") or {})
                    },
                    state=state,
                )
                await self.events.publish(
                    task_id,
                    "answer.completed",
                    {
                        "message_id": str(assistant_message.id),
                        "content_length": len(outcome.final_answer),
                        **answer_snapshot,
                        "status": "WAITING_INPUT",
                    },
                    message_id=assistant_message.id,
                    graph_mode=mode,
                    actor_type="answer",
                    actor_id="final_answer",
                    status="WAITING_INPUT",
                )
                await self.events.publish(
                    task_id,
                    "task.waiting_input",
                    {"task_id": str(task_id), "message_id": str(assistant_message.id)},
                    graph_mode=mode,
                    actor_type="supervisor",
                    actor_id="builtin.supervisor",
                    status="WAITING_INPUT",
                )
                return

            if outcome.final_status != "COMPLETED":
                raise AppError(
                    "LANGGRAPH_INVALID_TERMINAL_STATE",
                    f"图返回未知终态: {outcome.final_status}",
                    500,
                )

            from app.services.sensor_references import persist_references
            await self._persist_final(
                task_id, assistant_message.id, conversation.id, branch.id,
                outcome.final_answer, MessageStatus.COMPLETED, GenerationTaskStatus.COMPLETED,
                external_ids, metadata_patch={**persist_references(state, outcome.final_answer),
                    "answer_results": state.get("answer_results") or [],
                    "attachment_reports": [{k:r.get(k) for k in ("attachment_id","filename","attachment_report","extraction_status","warnings")} for r in state.get("understanding_results") or []]},
                state=state,
            )
            await self.events.publish(
                task_id,
                "answer.completed",
                {
                    "message_id": str(assistant_message.id),
                    "content_length": len(outcome.final_answer),
                        **answer_snapshot,
                    "external_message_id": external_ids.get("message_id"),
                },
                message_id=assistant_message.id,
                graph_mode=mode,
                actor_type="answer",
                actor_id="final_answer",
                status="COMPLETED",
            )
            await self.events.publish(
                task_id,
                "task.completed",
                {"task_id": str(task_id), "execution_mode": mode},
                message_id=assistant_message.id,
                graph_mode=mode,
                actor_type="worker",
                actor_id=self.consumer,
                status="COMPLETED",
            )
            await self._log_performance_report_if_enabled(task_id)
        except AppError as exc:
            await self._fail_task(task_id, exc.code, exc.message, graph_runtime=graph_runtime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("generation_task_failed", task_id=str(task_id))
            await self._fail_task(task_id, "INTERNAL_ERROR", str(exc), graph_runtime=graph_runtime)
        finally:
            if metric_active:
                TASKS_ACTIVE.dec()
            if release_required:
                # A duplicate queue delivery does not own the executing worker's spans.
                with suppress(Exception):
                    await self.events.close_open_spans(task_id)
                await self.concurrency.release(conversation_id, user_token, context.get("lease_id"))

    async def _log_performance_report_if_enabled(self, task_id: UUID) -> None:
        """Generate the optional full report server-side only.

        The report is deliberately never exposed through the chat HTTP/SSE APIs.
        When enabled it is written to the worker structured log so production UI
        payloads stay unchanged and no extra frontend transfer cost is introduced.
        """
        if not (
            self.settings.performance_monitor_enabled
            and self.settings.performance_report_enabled
        ):
            return
        try:
            async with self.session_factory() as session:
                task = await session.get(GenerationTask, task_id)
            if task is None:
                return
            events = await self.events.list_task_events(task_id)
            report = build_performance_report(task, events)
            log.info("conversation_performance_report", **report)
        except Exception:
            # Observability must never make the business request fail.
            log.exception("conversation_performance_report_failed", task_id=str(task_id))

    def _resolve_execution_mode(
        self,
        task: GenerationTask,
        context: dict[str, Any],
        user_message: Message,
    ) -> str:
        candidate = str(
            context.get("execution_mode")
            or task.execution_mode
            or (user_message.metadata_json or {}).get("execution_mode")
            or self.settings.default_execution_mode
        ).lower()
        allowed = {item.value for item in ExecutionMode}
        if candidate not in allowed:
            raise AppError("EXECUTION_MODE_INVALID", f"不支持的执行模式: {candidate}", 422)
        return "normal" if candidate == "expert" else candidate

    async def _load_and_start(self, task_id: UUID):
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            if task is None:
                return None, False
            if task.status not in {
                GenerationTaskStatus.QUEUED.value,
                GenerationTaskStatus.STOP_REQUESTED.value,
            }:
                return None, False
            conversation = await session.get(Conversation, task.conversation_id)
            branch = await session.get(ConversationBranch, task.branch_id)
            user_message = await session.get(Message, task.user_message_id)
            assistant_message = await session.get(Message, task.assistant_message_id)
            if not all((conversation, branch, user_message, assistant_message)):
                raise AppError("TASK_DATA_INVALID", "任务关联数据不完整", 500)
            profile = await session.get(UserProfile, conversation.user_token)
            if task.status == GenerationTaskStatus.STOP_REQUESTED.value:
                task.status = GenerationTaskStatus.STOPPED.value
                assistant_message.status = MessageStatus.STOPPED.value
                task.completed_at = datetime.now(UTC)
                return None, True
            task.status = GenerationTaskStatus.PREPARING.value
            task.started_at = task.started_at or datetime.now(UTC)
            task.worker_id = self.consumer
            task.heartbeat_at = datetime.now(UTC)
            await session.flush()
            return (
                (
                    task,
                    conversation,
                    branch,
                    user_message,
                    assistant_message,
                    profile.profile_json if profile else {},
                ),
                True,
            )

    async def _cache_external_ids(
        self, context_key: str, external_ids: dict[str, str | None]
    ) -> None:
        raw = await self.redis.get(context_key)
        if not raw:
            return
        context = json.loads(raw)
        context.update(
            {f"external_{key}": value for key, value in external_ids.items() if value}
        )
        await self.redis.setex(
            context_key,
            self.settings.task_context_ttl_seconds,
            json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        )

    async def _cache_selection_resume_state(
        self,
        context_key: str,
        state: dict[str, Any],
    ) -> None:
        """Persist the already-approved workflow before waiting for user selection.

        Selection resumes are re-queued by design.  Saving these model decisions keeps
        the resumed attempt deterministic and prevents a second workflow-classifier or
        Asset-LLM call for the same user turn.
        """

        raw = await self.redis.get(context_key)
        if not raw:
            raise AppError(
                "TASK_CONTEXT_EXPIRED",
                "任务临时上下文已过期，无法安全等待实体选择。",
                409,
            )
        context = json.loads(raw)
        context["selection_understanding_results"] = list(state.get("understanding_results") or [])
        context["selection_business_intent"] = dict(
            state.get("business_intent") or {}
        )
        context["selection_business_workflow"] = dict(
            state.get("business_workflow") or {}
        )
        await self.redis.setex(
            context_key,
            self.settings.task_context_ttl_seconds,
            json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        )

    async def _build_memory_context(
        self, branch_id: UUID, before_leaf_id: UUID | None
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            branch = await session.get(ConversationBranch, branch_id)
            if branch is None:
                return {"summary": "", "recent_messages": []}
            return await self.context_builder.build(
                session, branch, before_leaf_id=before_leaf_id
            )

    async def _persist_final(
        self,
        task_id: UUID,
        assistant_message_id: UUID,
        conversation_id: UUID,
        branch_id: UUID,
        content: str,
        message_status: MessageStatus,
        task_status: GenerationTaskStatus,
        external_ids: dict[str, str | None],
        metadata_patch: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        content = strip_think_blocks(content)
        now = datetime.now(UTC)
        summary_event_id: UUID | None = None
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            assistant = await session.get(
                Message, assistant_message_id, with_for_update=True
            )
            conversation = await session.get(
                Conversation, conversation_id, with_for_update=True
            )
            branch = await session.get(
                ConversationBranch, branch_id, with_for_update=True
            )
            if not all((task, assistant, conversation, branch)):
                raise AppError(
                    "TASK_DATA_INVALID", "持久化生成结果时关联数据缺失", 500
                )
            assistant.content = content
            assistant.status = message_status.value
            assistant.dify_message_id = external_ids.get("message_id")
            if metadata_patch:
                metadata = dict(assistant.metadata_json or {})
                metadata.update(metadata_patch)
                assistant.metadata_json = metadata
            if (
                state is not None
                and bool(getattr(self.settings, "evidence_fabric_enabled", True))
                and bool(getattr(self.settings, "evidence_dual_write_enabled", True))
                and task_status in {GenerationTaskStatus.COMPLETED, GenerationTaskStatus.WAITING_INPUT}
            ):
                evidence_context = await self.evidence_workspace_service.persist_turn(
                    session,
                    task_id=task_id,
                    conversation_id=conversation_id,
                    assistant_message_id=assistant_message_id,
                    state=state,
                    final_answer=content,
                )
                metadata = dict(assistant.metadata_json or {})
                metadata["evidence_context"] = evidence_context
                assistant.metadata_json = metadata
            task.status = task_status.value
            task.dify_task_id = external_ids.get("task_id")
            task.completed_at = now
            task.heartbeat_at = now
            branch.dify_conversation_id = (
                external_ids.get("conversation_id") or branch.dify_conversation_id
            )
            branch.active_leaf_message_id = assistant.id
            conversation.last_message_preview = content[:300]
            conversation.last_message_at = now

            user_message = await session.get(Message, task.user_message_id)
            if user_message is not None and user_message.content.strip():
                normalized_query = " ".join(
                    user_message.content.strip().lower().split()
                )[:255]
                stmt = insert(QueryStatistic).values(
                    user_token=conversation.user_token,
                    app_code=conversation.app_code,
                    normalized_query=normalized_query,
                    sample_query=user_message.content,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["user_token", "app_code", "normalized_query"],
                    set_={
                        "sample_query": user_message.content,
                        "usage_count": QueryStatistic.usage_count + 1,
                        "last_used_at": now,
                    },
                )
                await session.execute(stmt)

            if self.settings.outbox_enabled and task_status in {
                GenerationTaskStatus.COMPLETED,
                GenerationTaskStatus.WAITING_INPUT,
            }:
                event = self.outbox.add(
                    session,
                    conversation_id=conversation.id,
                    aggregate_type="conversation_branch",
                    aggregate_id=branch.id,
                    event_type="summary.enqueue",
                    payload={"branch_id": str(branch.id)},
                    deduplication_key=f"summary.enqueue:{task_id}",
                )
                summary_event_id = event.id

        if summary_event_id is not None:
            if self.settings.outbox_fast_publish_enabled:
                await self.outbox.publish_fast(summary_event_id, worker_id=self.consumer)
        elif (
            not self.settings.outbox_enabled
            and task_status
            in {GenerationTaskStatus.COMPLETED, GenerationTaskStatus.WAITING_INPUT}
        ):
            await self.queue.enqueue_summary(branch_id)

    async def _wait_for_diagnosis_confirmation(self, task, assistant, state, mode, *, user_token=None, lease_id=None):
        from app.orchestration.diagnosis_choice import public_confirmation, resume_state
        value = {**dict(state["diagnosis_confirmation"]), "task_id":str(task.id), "message_id":str(assistant.id)}
        await self.redis.expire(f"chat:task_context:{task.id}", self.settings.task_context_ttl_seconds)
        # The pending state and resumable goal/identity are durable; no credentials or raw waveforms.
        async with self.session_factory() as session, session.begin():
            db_task = await session.get(GenerationTask, task.id, with_for_update=True)
            db_message = await session.get(Message, assistant.id, with_for_update=True)
            if db_task.status in {"STOP_REQUESTED", "STOPPED"}:
                db_task.status, db_task.completed_at = "STOPPED", datetime.now(UTC)
                db_message.status = "STOPPED"
                stopped = True
            else:
                db_message.metadata_json = {**dict(db_message.metadata_json or {}),
                    "diagnosis_confirmation":value, "diagnosis_resume_state":resume_state(state)}
                db_message.content, db_message.status = value["question"], "COMPLETED"
                db_task.status, db_task.worker_id, db_task.completed_at = "COMPLETED", None, datetime.now(UTC)
                db_task.heartbeat_at = db_task.completed_at
                conversation = await session.get(Conversation, db_task.conversation_id, with_for_update=True)
                if conversation is not None:
                    conversation.last_message_preview = value["question"][:300]
                    conversation.last_message_at = db_task.completed_at
                stopped = False
        if stopped:
            await self.events.publish(task.id,"task.stopped", {"task_id":str(task.id)}, status="STOPPED")
            return
        task.status = "COMPLETED"
        if user_token is not None and lease_id:
            # The UI can send immediately after seeing the prompt. Release this
            # task's lease first; the outer finally's same-lease release is safe.
            await self.concurrency.release(task.conversation_id, user_token, lease_id)
        generation_id = str(uuid4())
        common = {"message_id": str(assistant.id), "generation_id": generation_id,
                  "channel": "final", "output_kind": "confirmation_prompt"}
        await self.events.publish(task.id, "answer.started", {**common, "generation_source": "diagnosis_confirmation"},
                                  message_id=assistant.id, graph_mode=mode, status="STARTED")
        await self.events.publish(task.id, "answer.delta", {**common, "content": value["question"], "status": "COMPLETED"},
                                  message_id=assistant.id, graph_mode=mode, status="COMPLETED")
        await self.events.publish(task.id, "diagnosis.mode.required", public_confirmation(task, value),
                                  message_id=assistant.id, graph_mode=mode, status="COMPLETED")
        await self.events.publish(task.id, "answer.completed",
                                  {**common, "status": "COMPLETED", "content": value["question"],
                                   "content_length": len(value["question"]), "replace": True},
                                  message_id=assistant.id, graph_mode=mode, status="COMPLETED")
        await self.events.publish(task.id, "task.completed",
                                  {"task_id": str(task.id), "message_id": str(assistant.id), "execution_mode": mode},
                                  message_id=assistant.id, graph_mode=mode, status="COMPLETED")

    async def _wait_for_entity_selection(
        self,
        *,
        task: GenerationTask,
        assistant_message: Message,
        query: str,
        entity_result: dict[str, Any],
        graph_mode: str,
    ) -> None:
        candidates: list[dict[str, Any]] = []
        for raw in entity_result.get("matches") or []:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            item["candidate_id"] = build_candidate_id(item)
            candidates.append(item)
        if len({c["candidate_id"] for c in candidates}) != len(candidates):
            raise AppError("ENTITY_SELECTION_AMBIGUOUS", "资产候选包含重复身份，暂时无法生成可靠的选择列表。", 502)
        if len(candidates) < 2:
            raise AppError(
                "ENTITY_SELECTION_INVALID",
                "MULTIPLE 状态没有提供足够的候选实体",
                502,
            )

        now = datetime.now(UTC)
        expires_at = now + timedelta(
            seconds=self.settings.entity_selection_ttl_seconds
        )
        async with self.session_factory() as session, session.begin():
            db_task = await session.get(GenerationTask, task.id, with_for_update=True)
            db_message = await session.get(
                Message, assistant_message.id, with_for_update=True
            )
            if db_task is None or db_message is None:
                raise AppError("TASK_DATA_INVALID", "实体选择任务关联数据缺失", 500)
            pending = await session.scalar(
                select(PendingEntitySelection)
                .where(PendingEntitySelection.task_id == task.id)
                .with_for_update()
            )
            if pending is None:
                pending = PendingEntitySelection(
                    task_id=task.id,
                    conversation_id=task.conversation_id,
                    original_query=query,
                    candidates=candidates,
                    status="PENDING",
                    expires_at=expires_at,
                )
                session.add(pending)
            else:
                pending.original_query = query
                pending.candidates = candidates
                pending.selected_entity = None
                pending.status = "PENDING"
                pending.expires_at = expires_at
            db_task.status = GenerationTaskStatus.WAITING_SELECTION.value
            db_task.worker_id = None
            db_task.heartbeat_at = now
            db_task.completed_at = None
            db_message.content = ""
            db_message.status = MessageStatus.PENDING.value
            await session.flush()

        await self.events.publish(
            task.id,
            "entity.selection.required",
            {
                "task_id": str(task.id),
                "message_id": str(assistant_message.id),
                "candidates": candidates,
                "expires_at": expires_at.isoformat(),
                "expires_in_seconds": self.settings.entity_selection_ttl_seconds,
                "resume_endpoint": f"/chat/v1/tasks/{task.id}/entity-selection",
            },
            message_id=assistant_message.id,
            graph_mode=graph_mode,
            stage="entity_selection",
            actor_type="agent",
            actor_id="builtin.fuzzy_entity",
            status="WAITING_SELECTION",
            output_payload={
                "candidate_count": len(candidates),
                "candidate_ids": [item["candidate_id"] for item in candidates],
            },
        )

    async def _save_entity_result(
        self,
        task_id: UUID,
        user_message_id: UUID,
        workflow_run_id: str | None,
        result: dict[str, Any],
    ) -> None:
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            message = await session.get(Message, user_message_id, with_for_update=True)
            if task:
                task.entity_workflow_run_id = workflow_run_id
            if message:
                message.entity_result = result

    async def _set_task_status(
        self, task_id: UUID, status: GenerationTaskStatus | str
    ) -> None:
        value = status.value if isinstance(status, GenerationTaskStatus) else str(status)
        valid = {item.value for item in GenerationTaskStatus}
        if value not in valid:
            raise AppError("TASK_STATUS_INVALID", f"无效任务状态: {value}", 500)
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            if task:
                task.status = value
                task.heartbeat_at = datetime.now(UTC)

    async def _set_task_and_message_streaming(
        self, task_id: UUID, message_id: UUID
    ) -> None:
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            message = await session.get(Message, message_id, with_for_update=True)
            if task:
                task.status = GenerationTaskStatus.STREAMING.value
                task.heartbeat_at = datetime.now(UTC)
            if message:
                message.status = MessageStatus.STREAMING.value

    async def _update_active_entity(self, branch_id: UUID, entity: dict[str, Any]) -> None:
        async with self.session_factory() as session, session.begin():
            branch = await session.get(ConversationBranch, branch_id, with_for_update=True)
            if branch:
                branch.active_entity = entity

    async def _is_cancelled(self, task_id: UUID) -> bool:
        return bool(await self.redis.exists(f"chat:cancel:{task_id}"))

    async def _finish_stopped(
        self, task: GenerationTask, assistant: Message, mode: str
    ) -> None:
        async with self.session_factory() as session, session.begin():
            db_task = await session.get(GenerationTask, task.id, with_for_update=True)
            db_message = await session.get(Message, assistant.id, with_for_update=True)
            if db_task:
                db_task.status = GenerationTaskStatus.STOPPED.value
                db_task.completed_at = datetime.now(UTC)
            if db_message:
                db_message.status = MessageStatus.STOPPED.value
        await self.events.publish(
            task.id,
            "task.stopped",
            {"task_id": str(task.id)},
            graph_mode=mode,
            actor_type="worker",
            actor_id=self.consumer,
            status="STOPPED",
        )

    async def _fail_task(self, task_id: UUID, code: str, message: str, *, graph_runtime=None) -> None:
        assistant_id: UUID | None = None
        mode: str | None = None
        async with self.session_factory() as session, session.begin():
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            if task is None:
                return
            task.status = GenerationTaskStatus.FAILED.value
            task.error_code = code
            task.error_message = message[:4000]
            task.completed_at = datetime.now(UTC)
            assistant_id = task.assistant_message_id
            mode = task.execution_mode
            if assistant_id:
                assistant = await session.get(Message, assistant_id, with_for_update=True)
                if assistant:
                    assistant.status = MessageStatus.FAILED.value
                    answer_meta = (graph_runtime.transient_tool_payloads.get("_answer_generation") or {}) if graph_runtime else {}
                    if answer_meta.get("content"):
                        assistant.content = answer_meta["content"]
                        assistant.metadata_json = {**dict(assistant.metadata_json or {}), "partial_answer":True}
        await self.events.publish(
            task_id,
            "task.failed",
            {"code": code, "message": default_public_message(code, "处理服务")},
            message_id=assistant_id,
            graph_mode=mode,
            actor_type="worker",
            actor_id=self.consumer,
            status="FAILED",
            error_code=code,
            error_message=message[:4000],
        )
