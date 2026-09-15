"""Valid, bounded context JSON; current query and confirmed identities stay intact."""
from __future__ import annotations

import json


def compact_context(context):
    result = dict(context)
    # The system prompt already declares recipe semantics. Keep the live allowed
    # catalog, variants and identity levels, without duplicate long descriptions.
    if "workflow_catalog" in result:
        result["workflow_catalog"] = [{
            "workflow_id": row.get("workflow_id"),
            "variants": [{k: variant.get(k) for k in
                ("variant_id", "required_entity_level", "display_name")}
                for variant in row.get("variants") or []],
        } for row in result["workflow_catalog"]]
    result["recent_messages"] = [
        {"role": row.get("role"), "content": shorten(str(row.get("content") or row.get("text") or ""), 1800)}
        for row in (result.get("recent_messages") or [])[-6:] if isinstance(row, dict)]
    result["conversation_summary"] = shorten(str(result.get("conversation_summary") or ""), 2400)
    if "attachment_evidence" in result:
        result["attachment_evidence"] = [{
            "attachment_id": row.get("attachment_id"), "kind": row.get("kind"),
            "extraction_status": row.get("extraction_status"),
            "extracted_text": shorten(str(row.get("extracted_text") or ""), 5000),
            "summary": shorten(str(row.get("summary") or ""), 1200),
            "uncertainties": row.get("uncertainties") or [],
        } for row in (result["attachment_evidence"] or [])[:8] if isinstance(row, dict)]
    # Never truncate the serialized JSON or the current question. Source documents
    # and full prior context remain available to the entity layer and final model.
    return result


def shorten(text, limit):
    if len(text) <= limit:
        return text
    half = (limit - 24) // 2
    return text[:half] + "\n（中间内容省略）\n" + text[-half:]


def encode_context(context):
    return json.dumps(compact_context(context), ensure_ascii=False, default=str, separators=(",", ":"))
