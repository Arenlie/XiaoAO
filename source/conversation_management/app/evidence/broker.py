from __future__ import annotations

import json
from collections import Counter
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence.catalog import available_operations, to_catalog_entry
from app.evidence.models import EvidenceRequest, EvidenceSlice
from app.evidence.repository import EvidenceRepository
from app.models.message import Message


class EvidenceBrokerError(RuntimeError):
    pass


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return 0


def _project(row: Any, projection: list[str]) -> Any:
    if not projection or not isinstance(row, dict):
        return row
    return {key: row.get(key) for key in projection if key in row}


def _match(row: Any, filters: list[dict[str, Any]]) -> bool:
    if not filters:
        return True
    if not isinstance(row, dict):
        return False
    for item in filters:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        op = str(item.get("op") or "eq").lower()
        expected = item.get("value")
        actual = row.get(field)
        if op == "eq" and actual != expected:
            return False
        if op == "ne" and actual == expected:
            return False
        if op == "in" and actual not in (expected if isinstance(expected, list) else [expected]):
            return False
        if op == "contains" and str(expected or "").lower() not in str(actual or "").lower():
            return False
        if op in {"gt", "gte", "lt", "lte"}:
            try:
                left = float(actual)
                right = float(expected)
            except (TypeError, ValueError):
                return False
            if op == "gt" and not left > right:
                return False
            if op == "gte" and not left >= right:
                return False
            if op == "lt" and not left < right:
                return False
            if op == "lte" and not left <= right:
                return False
    return True


def _extract_rows(payload: Any) -> tuple[list[Any], int | None]:
    if isinstance(payload, list):
        return list(payload), len(payload)
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            total = payload.get("total_count")
            try:
                total_count = int(total) if total is not None else len(rows)
            except (TypeError, ValueError):
                total_count = len(rows)
            return list(rows), total_count
        for key in ("items", "records", "members", "devices", "points", "alarms", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return list(value), len(value)
    return [], None


class EvidenceBroker:
    def __init__(self, *, repository: EvidenceRepository, settings) -> None:
        self.repository = repository
        self.settings = settings

    async def _load_payload(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        evidence,
    ) -> tuple[Any, bool, str | None]:
        if evidence.storage_backend == "inline_json":
            return evidence.inline_payload, True, None
        if evidence.storage_backend == "message_answer_result":
            raw_message_id = (evidence.storage_ref or {}).get("message_id")
            result_id = str((evidence.storage_ref or {}).get("result_id") or "")
            if not raw_message_id:
                return None, False, "message_id_missing"
            try:
                message_id = UUID(str(raw_message_id))
            except ValueError:
                return None, False, "message_id_invalid"
            message = await session.get(Message, message_id)
            if message is None or message.conversation_id != conversation_id:
                return None, False, "message_not_found"
            for item in (message.metadata_json or {}).get("answer_results") or []:
                if isinstance(item, dict) and str(item.get("result_id") or "") == result_id:
                    return item, True, None
            return None, False, "answer_result_not_found"
        # Stable reference backends are intentionally not copied into Evidence PostgreSQL.
        # Their descriptor/reference is still a valid L0/L1 result; materialization is
        # performed by the corresponding backend adapter/capability.
        return None, False, "backend_materialization_required"

    async def request(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        request: EvidenceRequest,
        payload_override: Any | None = None,
    ) -> EvidenceSlice:
        evidence = await self.repository.get(
            session, conversation_id=conversation_id, evidence_id=request.evidence_id
        )
        if evidence is None:
            raise EvidenceBrokerError("EVIDENCE_NOT_FOUND")
        operations = available_operations(evidence)
        if request.operation not in operations and request.operation not in {"lineage", "supporting_evidence"}:
            raise EvidenceBrokerError("EVIDENCE_OPERATION_UNSUPPORTED")

        if request.operation == "describe":
            data = to_catalog_entry(evidence, expose_storage=True)
            data["storage_ref"] = dict(evidence.storage_ref or {})
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data=data,
                returned_count=1,
                total_count=1,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )
        if request.operation == "summarize":
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data={
                    "summary": dict(evidence.summary or {}),
                    "content_descriptor": dict(evidence.content_descriptor or {}),
                    "completeness": dict(evidence.completeness or {}),
                    "freshness": dict(evidence.freshness or {}),
                },
                returned_count=1,
                total_count=1,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )
        if request.operation in {"lineage", "supporting_evidence"}:
            parents = await self.repository.lineage_parents(session, evidence_id=evidence.evidence_id)
            children = await self.repository.lineage_children(session, evidence_id=evidence.evidence_id)
            data = {
                "parents": [
                    {
                        "evidence_id": str(x.parent_evidence_id),
                        "relation_type": x.relation_type,
                        "metadata": dict(x.metadata_json or {}),
                    }
                    for x in parents
                ],
                "children": [
                    {
                        "evidence_id": str(x.child_evidence_id),
                        "relation_type": x.relation_type,
                        "metadata": dict(x.metadata_json or {}),
                    }
                    for x in children
                ],
            }
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data=data,
                returned_count=len(data["parents"]) + len(data["children"]),
                total_count=len(data["parents"]) + len(data["children"]),
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )

        if payload_override is not None:
            payload, materialized, limitation = payload_override, True, None
        else:
            payload, materialized, limitation = await self._load_payload(
                session, conversation_id=conversation_id, evidence=evidence
            )
        if not materialized:
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data={
                    "summary": dict(evidence.summary or {}),
                    "content_descriptor": dict(evidence.content_descriptor or {}),
                },
                materialized=False,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                limitation=limitation,
                available_operations=operations,
            )

        if request.operation == "materialize" and request.detail_level in {"L0", "L1"}:
            data = payload if request.detail_level == "L1" else to_catalog_entry(evidence)
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data=data,
                total_count=1,
                returned_count=1,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )

        rows, declared_total = _extract_rows(payload)
        if not rows:
            # Records/scalars can still be projected without row semantics.
            data = _project(payload, request.projection) if request.operation == "project" else payload
            max_chars = int(getattr(self.settings, "evidence_max_text_chars", 16000))
            if _json_size(data) > max_chars:
                text = json.dumps(data, ensure_ascii=False, default=str)
                data = {"text_preview": text[:max_chars], "truncated": True}
                truncated = True
            else:
                truncated = False
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data=data,
                total_count=1,
                returned_count=1,
                truncated=truncated,
                has_more=truncated,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )

        filtered = [row for row in rows if _match(row, request.filters)]
        for sort_spec in reversed(request.sort or []):
            if not isinstance(sort_spec, dict):
                continue
            field = str(sort_spec.get("field") or "")
            reverse = str(sort_spec.get("direction") or "asc").lower() == "desc"
            filtered.sort(key=lambda row: (row.get(field) is None, row.get(field)) if isinstance(row, dict) else (True, None), reverse=reverse)

        if request.operation == "aggregate":
            spec = request.aggregation or {"op": "count"}
            op = str(spec.get("op") or "count").lower()
            field = str(spec.get("field") or "")
            if op == "count":
                data = {"count": len(filtered)}
            elif op == "group_count":
                counts = Counter(str(row.get(field)) for row in filtered if isinstance(row, dict))
                data = {"groups": [{"value": key, "count": value} for key, value in counts.most_common()]}
            elif op in {"min", "max", "sum", "avg"}:
                values = []
                for row in filtered:
                    try:
                        values.append(float(row.get(field)))
                    except (AttributeError, TypeError, ValueError):
                        continue
                if op == "min": result = min(values) if values else None
                elif op == "max": result = max(values) if values else None
                elif op == "sum": result = sum(values) if values else 0
                else: result = (sum(values) / len(values)) if values else None
                data = {op: result, "field": field, "value_count": len(values)}
            else:
                raise EvidenceBrokerError("EVIDENCE_AGGREGATION_UNSUPPORTED")
            return EvidenceSlice(
                evidence_id=evidence.evidence_id,
                operation=request.operation,
                data=data,
                total_count=len(filtered),
                returned_count=1,
                storage_backend=evidence.storage_backend,
                storage_ref=dict(evidence.storage_ref or {}),
                available_operations=operations,
            )

        offset = int(request.offset or 0)
        max_rows = int(getattr(self.settings, "evidence_max_rows_per_slice", 200))
        requested_limit = int(request.limit or max_rows)
        limit = max(1, min(requested_limit, max_rows))
        selected = filtered[offset:offset + limit]
        selected = [_project(row, request.projection) for row in selected]
        total = len(filtered)
        has_more = offset + len(selected) < total
        return EvidenceSlice(
            evidence_id=evidence.evidence_id,
            operation=request.operation,
            data=selected,
            total_count=total if declared_total is None else max(total, declared_total if not request.filters else total),
            returned_count=len(selected),
            truncated=has_more or requested_limit > limit,
            has_more=has_more,
            storage_backend=evidence.storage_backend,
            storage_ref=dict(evidence.storage_ref or {}),
            available_operations=operations,
        )
