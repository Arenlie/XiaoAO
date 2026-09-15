"""Owned message result pages and deterministic CSV export, independent of LLM context."""
from __future__ import annotations

import asyncio
import csv
import io
from types import SimpleNamespace
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select

from app.api.dependencies import RequestIdentity, get_container, require_user
from app.container import AppContainer
from app.domain.exceptions import AppError, NotFoundError
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.common import ApiResponse
from app.services.query_results import identity as entity_identity

router = APIRouter(prefix="/messages", tags=["messages"])
LABELS = {
    "name": "对象名称",
    "equip_no": "设备编码",
    "point_no": "测点编码",
    "area": "所在区域",
    "health_score": "健康度",
    "grade": "健康等级",
    "health_time": "健康度数据时间",
    "alarm_count": "报警数量",
    "equipment_type": "设备类型",
    "model": "型号",
    "count": "数量",
    "supplement_status": "补充查询说明",
}


async def owned_results(container, message_id, user_token):
    async with container.conversation_service.session_factory() as session:
        message = await session.scalar(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Conversation.user_token == user_token,
                Conversation.deleted_at.is_(None),
                Message.deleted_at.is_(None),
            )
        )
        if message is None:
            raise NotFoundError("MESSAGE_NOT_FOUND", "消息不存在")
        results = (message.metadata_json or {}).get("answer_results", []) if message.status == "COMPLETED" else []
        return message, list(results)


async def _materialize_asset_snapshot(
    *,
    container: AppContainer,
    identity: RequestIdentity,
    message: Message,
    snapshot_id: str,
    offset: int = 0,
    limit: int | None = None,
    hard_limit: int = 50000,
) -> tuple[list[dict], int | None, bool]:
    """Read the frozen Asset MCP member set without re-running entity/category search.

    Cursor pagination is deliberately resolved in program code. The LLM never receives
    the full collection merely because an API page/export requested it.
    """
    from app.asset_query_contract import TOOL_ID
    from app.services.asset_collections import signed_arguments, validate_response
    from app.tools.contracts import ToolCallRequest

    wanted = hard_limit if limit is None else min(hard_limit, offset + max(1, limit))
    page_size = min(5000, max(200, min(wanted, 5000)))
    query: dict = {
        "reference": {"query_id": snapshot_id, "mode": "same_set"},
        "operation": "list",
        "freshness": "referenced_snapshot",
        "page_size": page_size,
    }
    rows: list[dict] = []
    seen: set[str] = set()
    total_count: int | None = None
    async with asyncio.timeout(90):
        while True:
            call = ToolCallRequest(
                tool_id=TOOL_ID,
                user_token=identity.user_token,
                task_id=str(message.id),
                conversation_id=str(message.conversation_id),
                branch_id=str(message.branch_id),
                arguments={"query": query},
                asset_allowed_query_ids=[snapshot_id],
            )
            args = signed_arguments(call, SimpleNamespace(settings=container.settings))
            payload = await container.phm_asset_mcp_client.call_tool("query_asset_collection", args)
            validate_response(query, payload)
            if total_count is None:
                raw_total = payload.get("matched_count", payload.get("count"))
                try:
                    total_count = int(raw_total) if raw_total is not None else None
                except (TypeError, ValueError):
                    total_count = None
            rows.extend(entity_identity(r) for r in payload.get("devices") or [])
            if len(rows) >= wanted:
                break
            cursor = payload.get("next_cursor")
            if not cursor:
                break
            if cursor in seen or len(rows) >= hard_limit:
                raise ValueError("asset snapshot pagination exceeded safety budget")
            seen.add(cursor)
            query = {**query, "cursor": cursor}

    if limit is None:
        if total_count is not None and total_count > hard_limit:
            raise ValueError("完整清单超过导出预算")
        selected = rows
    else:
        selected = rows[offset : offset + limit]
    known_total = total_count if total_count is not None else len(rows)
    return selected, total_count, offset + len(selected) < known_total


@router.get("/{message_id}/query-results")
async def result_pages(
    message_id: UUID,
    request: Request,
    result_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
):
    message, results = await owned_results(container, message_id, identity.user_token)
    if result_id:
        results = [r for r in results if r.get("result_id") == result_id]
    data = []
    for result in results:
        rows = result.get("all_rows") or result.get("rows") or []
        total_count = result.get("total_count")
        has_more = offset + limit < len(rows)
        page_rows = rows[offset : offset + limit]
        if result.get("asset_snapshot"):
            try:
                page_rows, snapshot_total, has_more = await _materialize_asset_snapshot(
                    container=container,
                    identity=identity,
                    message=message,
                    snapshot_id=str(result["asset_snapshot"]),
                    offset=offset,
                    limit=limit,
                )
                if snapshot_total is not None:
                    total_count = snapshot_total
            except Exception as exc:
                # First stored page remains usable if the frozen snapshot backend is
                # temporarily unavailable; do not silently claim it is complete.
                if offset >= len(rows):
                    raise AppError(
                        "RESULT_MATERIALIZATION_UNAVAILABLE",
                        "原查询快照暂时无法物化，请稍后重试或重新查询。",
                        409,
                    ) from exc
                page_rows = rows[offset : offset + limit]
                has_more = bool(total_count is not None and offset + len(page_rows) < int(total_count))
        data.append(
            {
                "result_id": result["result_id"],
                "created_at": result.get("created_at"),
                "matched_count": total_count,
                "stored_count": len(rows),
                "displayed_count": result.get("displayed_count"),
                "data_complete": result.get("complete", False),
                "offset": offset,
                "has_more": has_more,
                "rows": page_rows,
                "notes": result.get("notes") or [],
            }
        )
    return ApiResponse(data=data, request_id=request.state.request_id)


@router.get("/{message_id}/query-results/{result_id}/export")
async def export_result(
    message_id: UUID,
    result_id: str,
    identity: RequestIdentity = Depends(require_user),
    container: AppContainer = Depends(get_container),
):
    message, results = await owned_results(container, message_id, identity.user_token)
    result = next((r for r in results if r.get("result_id") == result_id), None)
    if result is None:
        raise NotFoundError("RESULT_NOT_FOUND", "原查询结果不存在")
    rows = result.get("all_rows") or result.get("rows") or []
    total_count = result.get("total_count")
    if result.get("asset_snapshot"):
        try:
            rows, snapshot_total, _ = await _materialize_asset_snapshot(
                container=container,
                identity=identity,
                message=message,
                snapshot_id=str(result["asset_snapshot"]),
                offset=0,
                limit=None,
            )
            if snapshot_total is not None:
                total_count = snapshot_total
        except Exception as exc:
            raise AppError(
                "RESULT_EXPORT_UNAVAILABLE",
                "原查询快照已过期、不可用或完整清单超过导出预算，请重新查询后导出。",
                409,
            ) from exc

    columns = [k for k in LABELS if any(k in r for r in rows)]
    if result.get("advanced_columns"):
        columns = result["advanced_columns"]

    def cell(value):
        if value is None:
            return ""
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
            return "'" + value
        return value

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([cell(LABELS.get(k, k)) for k in columns])
    writer.writerows([[cell(row.get(k)) for k in columns] for row in rows])
    complete = bool(result.get("complete")) and (total_count is None or len(rows) == int(total_count))
    return Response(
        content=("\ufeff" + buffer.getvalue()).encode(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="phm_query_result.csv"',
            "X-Result-Complete": str(complete).lower(),
            "X-Exported-Rows": str(len(rows)),
            "X-Total-Matched": str(total_count if total_count is not None else len(rows)),
        },
    )
