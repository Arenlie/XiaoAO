from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select, text

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.domain.exceptions import AppError
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.models.outbox_event import OutboxEvent
from app.models.execution_event import TaskExecutionEvent
from app.schemas.common import ApiResponse
from app.services.queue_service import QueueService

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _task_dict(row: GenerationTask) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id),
        "branch_id": str(row.branch_id),
        "user_message_id": str(row.user_message_id),
        "assistant_message_id": str(row.assistant_message_id) if row.assistant_message_id else None,
        "operation": row.operation,
        "execution_mode": row.execution_mode,
        "status": row.status,
        "request_id": row.request_id,
        "entity_workflow_run_id": row.entity_workflow_run_id,
        "dify_task_id": row.dify_task_id,
        "worker_id": row.worker_id,
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "heartbeat_at": _iso(row.heartbeat_at),
        "completed_at": _iso(row.completed_at),
    }


def _outbox_dict(row: OutboxEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "conversation_id": str(row.conversation_id),
        "aggregate_type": row.aggregate_type,
        "aggregate_id": str(row.aggregate_id) if row.aggregate_id else None,
        "event_type": row.event_type,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "available_at": _iso(row.available_at),
        "locked_at": _iso(row.locked_at),
        "locked_by": row.locked_by,
        "published_at": _iso(row.published_at),
        "last_error": row.last_error,
        "deduplication_key": row.deduplication_key,
        "trace_context": row.trace_context,
        "payload": row.payload,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


@router.get("/overview", response_model=ApiResponse[dict[str, Any]])
async def overview(
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    database_status = "ok"
    redis_status = "ok"
    database_error = None
    redis_error = None
    try:
        await container.database.ping()
    except Exception as exc:  # pragma: no cover - operational fallback
        database_status = "error"
        database_error = str(exc)
    try:
        await container.redis_manager.ping()
    except Exception as exc:  # pragma: no cover - operational fallback
        redis_status = "error"
        redis_error = str(exc)

    async with container.database.session_factory() as session:
        conversation_count = int(
            await session.scalar(select(func.count(Conversation.id))) or 0
        )
        branch_count = int(
            await session.scalar(select(func.count(ConversationBranch.id))) or 0
        )
        message_count = int(await session.scalar(select(func.count(Message.id))) or 0)

        task_rows = (
            await session.execute(
                select(GenerationTask.status, func.count(GenerationTask.id)).group_by(
                    GenerationTask.status
                )
            )
        ).all()
        task_counts = {str(status): int(count) for status, count in task_rows}

        outbox_rows = (
            await session.execute(
                select(OutboxEvent.status, func.count(OutboxEvent.id)).group_by(
                    OutboxEvent.status
                )
            )
        ).all()
        outbox_counts = {str(status): int(count) for status, count in outbox_rows}

        recent_tasks = (
            await session.scalars(
                select(GenerationTask)
                .order_by(GenerationTask.created_at.desc())
                .limit(8)
            )
        ).all()

        rls_rows = (
            await session.execute(
                text(
                    """
                    SELECT relname, relrowsecurity, relforcerowsecurity
                    FROM pg_class
                    WHERE relname IN (
                        'conversations',
                        'conversation_branches',
                        'messages',
                        'generation_tasks',
                        'outbox_events',
                        'user_profiles'
                    )
                    ORDER BY relname
                    """
                )
            )
        ).mappings().all()

    redis = container.redis_manager.client
    stream_lengths: dict[str, int | None] = {}
    for name, key in {
        "generation": QueueService.GENERATION_QUEUE,
        "title": QueueService.TITLE_QUEUE,
        "summary": QueueService.SUMMARY_QUEUE,
        "dify_cleanup": QueueService.DIFY_CLEANUP_QUEUE,
    }.items():
        try:
            stream_lengths[name] = int(await redis.xlen(key))
        except Exception:
            stream_lengths[name] = None

    data = {
        "user_token": identity.user_token,
        "application": {
            "name": container.settings.app_name,
            "environment": container.settings.app_env,
            "version": container.settings.otel_service_version,
            "api_prefix": container.settings.api_prefix,
            "server_time": datetime.now(UTC).isoformat(),
        },
        "dependencies": {
            "postgresql": {"status": database_status, "error": database_error},
            "redis": {"status": redis_status, "error": redis_error},
        },
        "tenant_data": {
            "conversation_count": conversation_count,
            "branch_count": branch_count,
            "message_count": message_count,
            "task_counts": task_counts,
            "outbox_counts": outbox_counts,
        },
        "redis_stream_lengths": stream_lengths,
        "production_features": {
            "transactional_outbox": container.settings.outbox_enabled,
            "outbox_fast_publish": container.settings.outbox_fast_publish_enabled,
            "postgresql_rls": container.settings.postgresql_rls_enabled,
            "opentelemetry": container.settings.otel_enabled,
            "otel_sample_ratio": container.settings.otel_trace_sample_ratio,
        },
        "rls_tables": [dict(row) for row in rls_rows],
        "database_pool": container.database.engine.pool.status(),
        "recent_tasks": [_task_dict(item) for item in recent_tasks],
    }
    return ApiResponse(data=data, request_id=request.state.request_id)


@router.get("/tasks", response_model=ApiResponse[list[dict[str, Any]]])
async def recent_tasks(
    request: Request,
    status: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    del identity  # RLS is already bound by the request middleware.
    statement = select(GenerationTask).order_by(GenerationTask.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(GenerationTask.status == status.upper())
    async with container.database.session_factory() as session:
        rows = (await session.scalars(statement)).all()
    return ApiResponse(
        data=[_task_dict(row) for row in rows], request_id=request.state.request_id
    )


@router.get("/outbox", response_model=ApiResponse[list[dict[str, Any]]])
async def recent_outbox(
    request: Request,
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=50, ge=1, le=200),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    del identity
    statement = select(OutboxEvent).order_by(OutboxEvent.created_at.desc()).limit(limit)
    if status:
        statement = statement.where(OutboxEvent.status == status.upper())
    async with container.database.session_factory() as session:
        rows = (await session.scalars(statement)).all()
    return ApiResponse(
        data=[_outbox_dict(row) for row in rows], request_id=request.state.request_id
    )


@router.get(
    "/tasks/{task_id}/execution-events",
    response_model=ApiResponse[list[dict[str, Any]]],
)
async def task_execution_events(
    task_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict[str, Any]]]:
    await container.task_service.get(task_id, identity.user_token)
    async with container.database.session_factory() as session:
        rows = (
            await session.scalars(
                select(TaskExecutionEvent)
                .where(TaskExecutionEvent.task_id == task_id)
                .order_by(TaskExecutionEvent.sequence_no.asc())
            )
        ).all()
    data = [
        {
            "id": row.id,
            "sequence_no": row.sequence_no,
            "task_id": str(row.task_id),
            "message_id": str(row.message_id) if row.message_id else None,
            "graph_mode": row.graph_mode,
            "span_id": row.span_id,
            "parent_span_id": row.parent_span_id,
            "event_type": row.event_type,
            "stage": row.stage,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "status": row.status,
            "attempt": row.attempt,
            "input_payload": row.input_payload,
            "output_payload": row.output_payload,
            "payload": row.payload,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "duration_ms": row.duration_ms,
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]
    return ApiResponse(data=data, request_id=request.state.request_id)


@router.get("/runtime", response_model=ApiResponse[dict[str, Any]])
async def runtime_configuration(
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict[str, Any]]:
    del identity
    settings = container.settings
    data = {
        "application": {
            "name": settings.app_name,
            "environment": settings.app_env,
            "version": settings.app_version,
            "api_prefix": settings.api_prefix,
            "default_execution_mode": settings.default_execution_mode,
        },
        "execution_modes": {
            "quick": {
                "agent_id": "builtin.general_content",
                "model": settings.quick_model,
                "vision_model": settings.quick_vision_model,
                "reasoning_enabled": settings.quick_enable_reasoning,
                "reasoning_effort": settings.quick_reasoning_effort,
                "max_model_calls": 1,
            },
            "normal": {
                "supervisor_model": settings.supervisor_model,
                "max_agent_calls": settings.normal_max_agent_calls,
                "max_parallel_calls": settings.normal_max_parallel_calls,
            },
            "expert": {
                "supervisor_model": settings.supervisor_model,
                "max_agent_calls": settings.normal_max_agent_calls,
                "max_parallel_calls": settings.normal_max_parallel_calls,
                "alias_of": "normal",
            },
        },
        "execution_mode_compatibility": {"expert": "normal"},
        "entity_resolution": {
            "provider": "phm_asset_mcp",
            "url": settings.phm_asset_mcp_url,
            "enabled": settings.phm_asset_mcp_enabled,
            "timeout_seconds": settings.phm_asset_mcp_timeout_seconds,
            "entrypoint": "unified_entity_resolution_layer",
            "legacy_dify_active": False,
        },
        "industrial_agents": {
            "base_url": settings.agent_base_url,
            "api_key_configured": bool(settings.agent_api_key),
            "timeout_seconds": settings.agent_timeout_seconds,
            "max_connections": settings.agent_max_connections,
            "http2": settings.agent_http2,
        },
        "file_processing": {
            "quick_max_characters": settings.file_quick_max_characters,
            "standard_max_characters": settings.file_standard_max_characters,
            "chunk_size": settings.file_retrieval_chunk_size,
            "chunk_overlap": settings.file_retrieval_chunk_overlap,
            "native_model_upload": settings.file_allow_native_model_upload,
            "external_model_policy": settings.file_external_model_policy,
            "vector_index": settings.file_enable_vector_index,
            "antivirus_scan": settings.file_enable_antivirus_scan,
        },
        "resilience": {
            "agent_timeout_seconds": settings.agent_default_timeout_seconds,
            "max_retries": settings.agent_default_max_retries,
            "retry_base_delay_seconds": settings.agent_retry_base_delay_seconds,
            "circuit_breaker_threshold": settings.agent_circuit_breaker_threshold,
        },
        "streaming": {
            "heartbeat_seconds": settings.sse_heartbeat_seconds,
            "xread_block_ms": settings.sse_xread_block_ms,
            "redis_retry_seconds": settings.sse_redis_retry_seconds,
            "client_retry_ms": settings.sse_client_retry_ms,
            "event_ttl_seconds": settings.task_event_ttl_seconds,
            "event_maxlen": settings.task_event_maxlen,
            "history_source": "postgresql",
        },
        "logging": {
            "directory": settings.log_dir,
            "file_enabled": settings.log_file_enabled,
            "console_enabled": settings.log_console_enabled,
            "max_bytes_per_file": settings.log_max_bytes,
            "backup_count": settings.log_backup_count,
            "retention_days": settings.log_retention_days,
        },
        "concurrency": {
            "per_conversation": settings.max_active_generations_per_conversation,
            "per_user": settings.max_active_generations_per_user,
            "global": settings.max_active_generations_global,
            "worker_concurrency": settings.worker_concurrency,
        },
        "outbox": {
            "enabled": settings.outbox_enabled,
            "fast_publish": settings.outbox_fast_publish_enabled,
            "poll_interval_seconds": settings.outbox_poll_interval_seconds,
            "batch_size": settings.outbox_batch_size,
            "max_attempts": settings.outbox_max_attempts,
        },
        "security": {
            "postgresql_rls_enabled": settings.postgresql_rls_enabled,
            "cors_origins": settings.allow_cors_origins,
        },
        "telemetry": {
            "enabled": settings.otel_enabled,
            "service_name": settings.otel_service_name,
            "service_version": settings.otel_service_version,
            "sample_ratio": settings.otel_trace_sample_ratio,
            "collector_configured": bool(settings.otel_exporter_otlp_endpoint),
        },
    }
    return ApiResponse(data=data, request_id=request.state.request_id)
