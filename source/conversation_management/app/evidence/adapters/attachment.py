from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.evidence.models import EvidenceCompleteness, EvidenceDraft
from app.evidence.adapters.common import freshness_for


def adapt_attachment_result(item: dict[str, Any]) -> list[EvidenceDraft]:
    attachment_id = str(item.get("attachment_id") or "")
    if not attachment_id:
        return []
    observed = datetime.now(UTC)
    filename = str(item.get("filename") or "")
    report = item.get("attachment_report")
    warnings = list(item.get("warnings") or [])
    drafts = [
        EvidenceDraft(
            kind="document",
            semantic_type="attachment_document",
            authority="USER_PROVIDED",
            subject={"attachment_id": attachment_id, "filename": filename},
            content_descriptor={"filename": filename, "extraction_status": item.get("extraction_status")},
            summary={"filename": filename, "extraction_status": item.get("extraction_status"), "warnings": warnings[:8]},
            completeness=EvidenceCompleteness(status="partial" if warnings else "complete", coverage=None, reason=("; ".join(str(x) for x in warnings[:3]) if warnings else None)),
            freshness=freshness_for("attachment_document", observed_at=observed),
            storage_backend="attachment_reference",
            storage_ref={"attachment_id": attachment_id},
            source_system="attachment",
            immutable=True,
            observed_at=observed,
        )
    ]
    if report not in (None, "", {}, []):
        drafts.append(
            EvidenceDraft(
                kind="analysis",
                semantic_type="analysis_result",
                authority="MODEL_INFERRED",
                subject={"attachment_id": attachment_id, "filename": filename},
                content_descriptor={"source": "attachment_analysis"},
                summary={"analysis": str(report)[:1600]},
                completeness=EvidenceCompleteness(status="complete"),
                freshness=freshness_for("analysis_result", observed_at=observed),
                storage_backend="inline_json",
                storage_ref={"attachment_id": attachment_id},
                inline_payload={"attachment_report": report},
                source_system="attachment_analysis",
                immutable=True,
                observed_at=observed,
            )
        )
    return drafts
