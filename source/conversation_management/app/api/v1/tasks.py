from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.domain.error_contract import default_public_message
from app.schemas.common import ApiResponse
from app.schemas.message import ExecutionEventView
from app.schemas.task import (
    EntitySelectionRequest,
    DiagnosisModeRequest,
    EntitySelectionView,
    StopTaskResponse,
    TaskExecutionHistory,
    TaskView,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
log = structlog.get_logger(__name__)

_TERMINAL_EVENTS = {
    "task.completed",
    "task.failed",
    "task.stopped",
    "task.waiting_input",
}
_TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "STOPPED",
    "TIMEOUT",
    "WAITING_INPUT",
}
_SSE_PROTOCOL_VERSION = "2.3"


@router.get("/{task_id}", response_model=ApiResponse[TaskView])
async def get_task(
    task_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[TaskView]:
    task = await container.task_service.get(task_id, identity.user_token)
    return ApiResponse(data=TaskView.model_validate(task), request_id=request.state.request_id)


@router.get(
    "/{task_id}/execution-events",
    response_model=ApiResponse[TaskExecutionHistory],
)
async def task_execution_events(
    task_id: UUID,
    request: Request,
    include_reasoning: bool = Query(default=False),
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=5000, ge=1, le=10000),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[TaskExecutionHistory]:
    task = await container.task_service.get(task_id, identity.user_token)
    rows, has_more = await container.events.task_event_page(task_id, after_sequence, limit)
    next_sequence = rows[-1].sequence_no if rows else after_sequence
    if not include_reasoning or not container.settings.stream_reasoning_enabled:
        rows = [row for row in rows if not row.event_type.startswith("answer.reasoning.")]
    return ApiResponse(
        data=TaskExecutionHistory(
            task=TaskView.model_validate(task),
            events=[ExecutionEventView.model_validate(row) for row in rows],
            next_after_sequence=next_sequence, has_more=has_more,
        ),
        request_id=request.state.request_id,
    )



@router.get("/{task_id}/events")
async def task_events(
    task_id: UUID,
    request: Request,
    include_reasoning: bool = Query(default=False),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    final_delta_event: str | None = Query(default=None),
    chat_ui_build: str | None = Header(default=None, alias="X-Chat-UI-Build"),
) -> StreamingResponse:
    initial_task = await container.task_service.get(task_id, identity.user_token)
    settings = container.settings
    requested_final_delta_event = str(final_delta_event or ("agent.output.delta" if chat_ui_build else "answer.delta")).strip()
    if requested_final_delta_event not in {"answer.delta", "agent.output.delta"}:
        requested_final_delta_event = "answer.delta"

    async def stream() -> AsyncIterator[bytes]:
        cursor = last_event_id or "0-0"
        close_reason = "unknown"
        last_output_at = time.monotonic()
        last_status_check_at = 0.0
        read_failure_count = 0
        events_sent = 0
        heartbeats_sent = 0
        deferred_terminal = None
        recovered_boundary = False
        log.info(
            "sse_stream_opened",
            task_id=str(task_id),
            last_event_id=cursor,
            initial_status=initial_task.status,
            protocol_version=_SSE_PROTOCOL_VERSION,
            final_delta_event=requested_final_delta_event,
            chat_ui_build=chat_ui_build or None,
            client_host=(request.client.host if request.client else None),
        )
        try:
            yield container.events.encode_retry(settings.sse_client_retry_ms)
            yield container.events.encode_sse(
                "",
                "sse.connected",
                {
                    "task_id": str(task_id),
                    "last_event_id": cursor,
                    "status": initial_task.status,
                    "execution_mode": initial_task.execution_mode,
                    "protocol_version": _SSE_PROTOCOL_VERSION,
                    "reasoning_enabled": bool(include_reasoning and settings.stream_reasoning_enabled),
                    "supported_channels": ["reasoning", "final"],
                    "resume_supported": True,
                    "entity_selection_in_band": True,
                    "final_delta_event": requested_final_delta_event,
                    "heartbeat_seconds": settings.sse_heartbeat_seconds,
                    "time": datetime.now(UTC).isoformat(),
                },
            )
            # Entity selection is an in-band pause, not a transport terminal state.
            # When a browser reconnects after the task already entered WAITING_SELECTION,
            # re-send one snapshot so the UI can rebuild the selection dialog immediately,
            # then keep the same SSE connection alive for the resumed task.
            if initial_task.status == "WAITING_SELECTION":
                pending = await container.task_service.get_pending_selection(
                    task_id, identity.user_token
                )
                pending_event_id = str(pending["last_event_id"])
                if container.events._cursor_value(cursor) >= container.events._cursor_value(
                    pending_event_id
                ):
                    yield container.events.encode_sse(
                        "",
                        "entity.selection.required",
                        {
                            "task_id": str(task_id),
                            "assistant_message_id": str(initial_task.assistant_message_id) if initial_task.assistant_message_id else None,
                            "status": "WAITING_SELECTION",
                            "candidates": pending["candidates"],
                            "expires_at": pending["expires_at"].isoformat(),
                            "expires_in_seconds": pending["expires_in_seconds"],
                            "last_event_id": pending_event_id,
                            "requires_action": True,
                            "stream_continues": True,
                            "recovered_from_task_state": True,
                        },
                    )
                    last_output_at = time.monotonic()
            if initial_task.status == "WAITING_CONFIRMATION":
                pending = await container.task_service.get_diagnosis_confirmation(task_id, identity.user_token)
                if pending:
                    yield container.events.encode_sse("", "diagnosis.mode.required", pending)
            while True:
                try:
                    rows = await container.events.read_batch(
                        task_id,
                        cursor,
                        block_ms=0 if deferred_terminal or recovered_boundary else min(
                            settings.sse_xread_block_ms,
                            max(500, settings.sse_heartbeat_seconds * 1000),
                        ),
                    )
                    read_failure_count = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    read_failure_count += 1
                    log.warning(
                        "sse_event_read_failed",
                        task_id=str(task_id),
                        failure_count=read_failure_count,
                        error=str(exc),
                    )
                    yield container.events.encode_sse(
                        "",
                        "sse.warning",
                        {
                            "task_id": str(task_id),
                            "message": "事件读取暂时失败，服务端正在重试",
                            "retry_count": read_failure_count,
                            "time": datetime.now(UTC).isoformat(),
                        },
                    )
                    await asyncio.sleep(settings.sse_redis_retry_seconds)
                    continue

                for event_id, event, raw_data in rows:
                    cursor = event_id
                    if event.startswith("answer.reasoning.") and not (include_reasoning and settings.stream_reasoning_enabled):
                        continue
                    data = dict(raw_data or {})
                    data.setdefault("sse_event_id", event_id)
                    if event in _TERMINAL_EVENTS:
                        # Legacy tasks can have span completions AFTER this record.
                        # Drain them before the terminal notification closes the UI.
                        deferred_terminal = (event_id, event, data)
                        continue
                    if event == "entity.selection.required":
                        data.setdefault("requires_action", True)
                        data.setdefault("stream_continues", True)
                    if event in _TERMINAL_EVENTS:
                        data.setdefault("stream_continues", False)
                        data.setdefault("stream_terminal", True)
                    public_event = event
                    if event == "answer.delta" and requested_final_delta_event == "agent.output.delta":
                        public_event = "agent.output.delta"
                        data.setdefault("source_event", "answer.delta")
                        data.setdefault("output_scope", "final")
                        data.setdefault("channel", "final")
                        data.setdefault("agent_id", data.get("actor_id") or "builtin.supervisor")
                    yield container.events.encode_sse(event_id, public_event, data)
                    events_sent += 1
                    last_output_at = time.monotonic()
                # A completed task may have more than one page of durable deltas.
                # Drain all of them before synthesizing a terminal state snapshot.
                if rows:
                    continue

                if deferred_terminal:
                    if not recovered_boundary:
                        await container.events.close_open_spans(task_id)
                        recovered_boundary = True
                        continue
                    terminal_id, event, data = deferred_terminal
                    if terminal_id != cursor:
                        # A snapshot must not rewind Last-Event-ID or impersonate a
                        # different durable sequence after late legacy span records.
                        terminal_id = ""
                        data.pop("sequence_no", None)
                        data.pop("sse_event_id", None)
                        data["recovered_from_task_state"] = True
                    data.update(stream_continues=False, stream_terminal=True)
                    yield container.events.encode_sse(terminal_id, event, data)
                    close_reason = f"terminal:{event}"
                    events_sent += 1
                    break

                now = time.monotonic()
                if now - last_output_at >= settings.sse_heartbeat_seconds:
                    yield container.events.encode_sse(
                        "",
                        "heartbeat",
                        {
                            "task_id": str(task_id),
                            "last_event_id": cursor,
                            "time": datetime.now(UTC).isoformat(),
                        },
                    )
                    heartbeats_sent += 1
                    last_output_at = now

                if recovered_boundary or now - last_status_check_at >= max(5.0, settings.sse_heartbeat_seconds):
                    last_status_check_at = now
                    current = await container.task_service.get(task_id, identity.user_token)
                    if current.status == "WAITING_CONFIRMATION":
                        await container.task_service.get_diagnosis_confirmation(task_id, identity.user_token)
                    if current.status in _TERMINAL_STATUSES:
                        if not recovered_boundary:
                            await container.events.close_open_spans(task_id)
                            recovered_boundary = True
                            continue
                        if current.status == "COMPLETED":
                            event = "task.completed"
                            data = {
                                "task_id": str(task_id),
                                "status": current.status,
                                "recovered_from_task_state": True,
                            }
                        elif current.status == "WAITING_INPUT":
                            event = "task.waiting_input"
                            data = {
                                "task_id": str(task_id),
                                "status": current.status,
                                "recovered_from_task_state": True,
                            }
                        elif current.status == "STOPPED":
                            event = "task.stopped"
                            data = {
                                "task_id": str(task_id),
                                "recovered_from_task_state": True,
                            }
                        else:
                            event = "task.failed"
                            data = {
                                "task_id": str(task_id),
                                "code": current.error_code or current.status,
                                "message": default_public_message(current.error_code or current.status, "处理服务"),
                                "recovered_from_task_state": True,
                            }
                        data.setdefault("stream_continues", False)
                        data.setdefault("stream_terminal", True)
                        data.setdefault("recovered_from_task_state", True)
                        yield container.events.encode_sse("", event, data)
                        events_sent += 1
                        close_reason = f"terminal_recovered:{event}"
                        break
        except asyncio.CancelledError:
            close_reason = "client_disconnected"
            raise
        except Exception:
            close_reason = "stream_exception"
            log.exception(
                "sse_stream_failed",
                task_id=str(task_id),
                user_token=identity.user_token,
                last_event_id=cursor,
            )
            raise
        finally:
            log.info(
                "sse_stream_closed",
                task_id=str(task_id),
                user_token=identity.user_token,
                last_event_id=cursor,
                close_reason=close_reason,
                events_sent=events_sent,
                heartbeats_sent=heartbeats_sent,
                protocol_version=_SSE_PROTOCOL_VERSION,
            )

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Encoding": "identity",
            "X-SSE-Protocol-Version": _SSE_PROTOCOL_VERSION,
        },
    )


@router.get(
    "/{task_id}/entity-selection",
    response_model=ApiResponse[EntitySelectionView],
)
async def get_pending_entity_selection(
    task_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[EntitySelectionView]:
    payload = await container.task_service.get_pending_selection(
        task_id, identity.user_token
    )
    return ApiResponse(
        data=EntitySelectionView.model_validate(payload),
        request_id=request.state.request_id,
    )


@router.post(
    "/{task_id}/entity-selection",
    response_model=ApiResponse[TaskView],
)
async def select_entity(
    task_id: UUID,
    payload: EntitySelectionRequest,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[TaskView]:
    task = await container.task_service.select_entity(
        task_id, identity.user_token, payload.candidate_id
    )
    return ApiResponse(
        data=TaskView.model_validate(task),
        request_id=request.state.request_id,
    )


@router.post("/{task_id}/stop", response_model=ApiResponse[StopTaskResponse])
async def stop_task(
    task_id: UUID,
    request: Request,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[StopTaskResponse]:
    task = await container.task_service.stop(task_id, identity.user_token)
    return ApiResponse(
        data=StopTaskResponse(task_id=task.id, status=task.status),
        request_id=request.state.request_id,
    )


@router.get("/{task_id}/diagnosis-mode", response_model=ApiResponse[dict | None])
async def get_diagnosis_mode(task_id: UUID, request: Request,
    identity: RequestIdentity = Depends(require_user), container: AppContainer = Depends(get_container)):
    value = await container.task_service.get_diagnosis_confirmation(task_id, identity.user_token)
    return ApiResponse(data=value, request_id=request.state.request_id)


@router.post("/{task_id}/diagnosis-mode", response_model=ApiResponse[TaskView])
async def select_diagnosis_mode(task_id: UUID, payload: DiagnosisModeRequest, request: Request,
    identity: RequestIdentity = Depends(require_user), container: AppContainer = Depends(get_container)):
    task = await container.task_service.select_diagnosis_mode(task_id, identity.user_token,
        str(payload.confirmation_id), payload.mode)
    return ApiResponse(data=TaskView.model_validate(task), request_id=request.state.request_id)
