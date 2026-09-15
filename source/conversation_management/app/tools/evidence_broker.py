from __future__ import annotations

from uuid import UUID

from app.evidence.broker import EvidenceBroker, EvidenceBrokerError
from app.evidence.adapters.asset_snapshot import AssetSnapshotBackend
from app.evidence.models import EvidenceRequest
from app.tools.contracts import (
    ToolCallRequest, ToolCallResult, ToolDescriptor, ToolProviderType, ToolResultStatus,
)

EVIDENCE_BROKER_TOOL_ID = "evidence.broker"


def evidence_broker_descriptor(settings) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=EVIDENCE_BROKER_TOOL_ID,
        display_name="Evidence Broker",
        provider_type=ToolProviderType.LOCAL,
        description=(
            "从当前Conversation已持久化Evidence中按需读取。先看Evidence Catalog，再按evidence_id请求"
            "describe/summarize/slice/filter/project/sort/aggregate/lineage；禁止请求全量大型数据。"
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["evidence_id", "operation"],
            "properties": {
                "evidence_id": {"type": "string", "format": "uuid"},
                "operation": {
                    "type": "string",
                    "enum": [
                        "describe", "summarize", "materialize", "project", "filter", "sort",
                        "slice", "aggregate", "search", "read_section", "lineage",
                        "supporting_evidence",
                    ],
                },
                "filters": {"type": "array", "maxItems": 16, "items": {"type": "object"}},
                "projection": {"type": "array", "maxItems": 64, "items": {"type": "string"}},
                "aggregation": {"type": ["object", "null"]},
                "sort": {"type": "array", "maxItems": 8, "items": {"type": "object"}},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {
                    "type": "integer", "minimum": 1,
                    "maximum": int(getattr(settings, "evidence_max_rows_per_slice", 200)),
                },
                "detail_level": {"type": "string", "enum": ["L0", "L1", "L2", "L3"]},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "evidence_id": {"type": "string"},
                "data": {},
                "total_count": {"type": ["integer", "null"]},
                "returned_count": {"type": ["integer", "null"]},
                "truncated": {"type": "boolean"},
                "has_more": {"type": "boolean"},
                "materialized": {"type": "boolean"},
            },
        },
        timeout_seconds=30,
        metadata={"capability_id": "evidence.read", "internal": True},
    )


class EvidenceBrokerToolHandler:
    def __init__(self, *, session_factory, broker: EvidenceBroker, asset_snapshot_backend: AssetSnapshotBackend | None = None) -> None:
        self.session_factory = session_factory
        self.broker = broker
        self.asset_snapshot_backend = asset_snapshot_backend

    async def __call__(self, request: ToolCallRequest, data_access_token: str | None) -> ToolCallResult:
        try:
            conversation_id = UUID(str(request.conversation_id))
            evidence_request = EvidenceRequest.model_validate(request.arguments)
            async with self.session_factory() as session:
                evidence = await self.broker.repository.get(
                    session, conversation_id=conversation_id, evidence_id=evidence_request.evidence_id
                )
                payload_override = None
                if (
                    evidence is not None
                    and evidence.storage_backend == "asset_snapshot"
                    and self.asset_snapshot_backend is not None
                    and evidence_request.operation not in {"describe", "summarize", "lineage", "supporting_evidence"}
                ):
                    snapshot_id = str((evidence.storage_ref or {}).get("query_id") or "")
                    payload_override = await self.asset_snapshot_backend.materialize(
                        call_request=request, snapshot_id=snapshot_id
                    )
                result = await self.broker.request(
                    session,
                    conversation_id=conversation_id,
                    request=evidence_request,
                    payload_override=payload_override,
                )
            payload = result.model_dump(mode="json")
            return ToolCallResult(
                tool_id=EVIDENCE_BROKER_TOOL_ID,
                status=ToolResultStatus.SUCCESS,
                content=payload,
                structured_content=payload,
            )
        except EvidenceBrokerError as exc:
            return ToolCallResult(
                tool_id=EVIDENCE_BROKER_TOOL_ID,
                status=ToolResultStatus.FAILED,
                error_code=str(exc),
                error_message="证据读取失败",
            )
        except Exception as exc:
            return ToolCallResult(
                tool_id=EVIDENCE_BROKER_TOOL_ID,
                status=ToolResultStatus.FAILED,
                error_code="EVIDENCE_MATERIALIZATION_FAILED",
                error_message=str(exc)[:500],
            )
