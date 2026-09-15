from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.planning.requirements import LEGACY_TO_SEMANTIC


AUTHORITY_RULES = {
    "asset": {"AUTHORITATIVE"},
    "health": {"AUTHORITATIVE"},
    "alarm": {"AUTHORITATIVE"},
    "sensor": {"AUTHORITATIVE"},
    "sensor_active_faults": {"AUTHORITATIVE"},
    "sensor_fault_history": {"AUTHORITATIVE"},
    "sensor_points": {"AUTHORITATIVE"},
    "sensor_monitoring": {"AUTHORITATIVE"},
    "sensor_offline": {"AUTHORITATIVE"},
    "vibration": {"AUTHORITATIVE", "COMPUTED"},
    "temperature": {"AUTHORITATIVE", "COMPUTED"},
    "diagnosis_result": {"DERIVED", "MODEL_INFERRED", "COMPUTED"},
    "file": {"USER_PROVIDED", "COMPUTED", "MODEL_INFERRED"},
}


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def entry_satisfies(
    entry: dict[str, Any],
    legacy_type: str,
    *,
    allow_partial: bool = True,
    force_refresh: bool = False,
) -> tuple[bool, str | None]:
    aliases = set(LEGACY_TO_SEMANTIC.get(legacy_type, [legacy_type]))
    semantic = str(entry.get("semantic_type") or "")
    if semantic not in aliases:
        return False, None
    authority = str(entry.get("authority") or "")
    allowed = AUTHORITY_RULES.get(legacy_type)
    if allowed and authority not in allowed:
        return False, "AUTHORITY_INSUFFICIENT"
    freshness = entry.get("freshness") if isinstance(entry.get("freshness"), dict) else {}
    if force_refresh and not bool(freshness.get("immutable")):
        return False, "EVIDENCE_STALE"
    valid_until = _parse_time(freshness.get("valid_until"))
    if not bool(freshness.get("immutable")) and valid_until is not None and datetime.now(UTC) > valid_until:
        return False, "EVIDENCE_STALE"
    completeness = entry.get("completeness") if isinstance(entry.get("completeness"), dict) else {}
    status = str(completeness.get("status") or "unknown")
    if status == "partial" and not allow_partial:
        return False, "EVIDENCE_PARTIAL"
    return True, ("EVIDENCE_PARTIAL" if status == "partial" else None)


def catalog_status(
    state: dict[str, Any],
    required: list[str],
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    catalog = [x for x in state.get("evidence_catalog") or (state.get("memory_context") or {}).get("evidence_catalog") or [] if isinstance(x, dict)]
    intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
    contract = intent.get("completion_contract") if isinstance(intent.get("completion_contract"), dict) else {}
    allow_partial = bool(contract.get("allow_partial", True))
    semantics = intent.get("asset_semantics") if isinstance(intent.get("asset_semantics"), dict) else {}
    force_refresh = bool(semantics.get("refresh_requested"))
    result: dict[str, bool] = {}
    detail: dict[str, dict[str, Any]] = {}
    for raw in required:
        if raw == "conversation_context":
            result[raw] = bool(state.get("recent_messages") or state.get("memory_context"))
            detail[raw] = {"source": "conversation_context" if result[raw] else "none"}
            continue
        matches = []
        failures = []
        for entry in catalog:
            ok, limitation = entry_satisfies(
                entry,
                raw,
                allow_partial=allow_partial,
                force_refresh=force_refresh,
            )
            aliases = set(LEGACY_TO_SEMANTIC.get(raw, [raw]))
            if str(entry.get("semantic_type") or "") in aliases:
                if ok:
                    matches.append((entry, limitation))
                elif limitation:
                    failures.append((entry, limitation))
        result[raw] = bool(matches)
        if matches:
            entry, limitation = matches[0]
            detail[raw] = {
                "source": "evidence_catalog",
                "evidence_id": entry.get("evidence_id"),
                "semantic_type": entry.get("semantic_type"),
                "authority": entry.get("authority"),
                "limitation": limitation,
            }
        elif failures:
            entry, failure = failures[0]
            detail[raw] = {
                "source": "evidence_catalog",
                "evidence_id": entry.get("evidence_id"),
                "semantic_type": entry.get("semantic_type"),
                "authority": entry.get("authority"),
                "failure": failure,
            }
        else:
            detail[raw] = {"source": "none", "failure": "EVIDENCE_NOT_FOUND"}
    return result, detail


_STABLE_EQUIVALENTS = {
    "equip_no": ("equip_no", "equipment_no"),
    "equipment_no": ("equipment_no", "equip_no"),
    "point_no": ("point_no", "pointNo"),
    "pointNo": ("pointNo", "point_no"),
    "space_id": ("space_id", "root_space_id"),
    "root_space_id": ("root_space_id", "space_id"),
}


def _constraint_value(container: dict[str, Any], key: str) -> Any:
    for candidate in _STABLE_EQUIVALENTS.get(key, (key,)):
        value = container.get(candidate)
        if value not in (None, "", [], {}):
            return value
    return None


def _constraints_match(actual: dict[str, Any], required: dict[str, Any]) -> bool:
    if not required:
        return True
    for key, expected in required.items():
        if expected in (None, "", [], {}):
            continue
        actual_value = _constraint_value(actual, key)
        if actual_value is None:
            # A catalog entry without the constrained identity is not allowed to satisfy
            # a requirement for another subject merely because semantic_type matches.
            return False
        if str(actual_value).casefold() != str(expected).casefold():
            return False
    return True


def requirement_entry_satisfies(
    entry: dict[str, Any], requirement: dict[str, Any]
) -> tuple[bool, str | None]:
    acceptable = [str(x) for x in requirement.get("acceptable_semantic_types") or [] if str(x)]
    if not acceptable:
        acceptable = [str(requirement.get("semantic_type") or "")]
    if str(entry.get("semantic_type") or "") not in set(acceptable):
        return False, None

    required_authority = {str(x) for x in requirement.get("required_authority") or [] if str(x)}
    if required_authority and str(entry.get("authority") or "") not in required_authority:
        return False, "EVIDENCE_AUTHORITY_INSUFFICIENT"

    subject = entry.get("subject") if isinstance(entry.get("subject"), dict) else {}
    scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
    if not _constraints_match(subject, requirement.get("subject_constraint") or {}):
        return False, "EVIDENCE_SUBJECT_MISMATCH"
    if not _constraints_match(scope, requirement.get("scope_constraint") or {}):
        return False, "EVIDENCE_SCOPE_MISMATCH"

    freshness = entry.get("freshness") if isinstance(entry.get("freshness"), dict) else {}
    mode = str((requirement.get("freshness") or {}).get("mode") or "reuse_if_valid")
    if mode == "force_refresh" and not bool(freshness.get("immutable")):
        return False, "EVIDENCE_STALE"
    valid_until = _parse_time(freshness.get("valid_until"))
    if mode != "historical_ok" and not bool(freshness.get("immutable")) and valid_until is not None and datetime.now(UTC) > valid_until:
        return False, "EVIDENCE_STALE"

    completeness = entry.get("completeness") if isinstance(entry.get("completeness"), dict) else {}
    status = str(completeness.get("status") or "unknown")
    allow_partial = bool((requirement.get("completeness") or {}).get("allow_partial", True))
    if status == "partial" and not allow_partial:
        return False, "EVIDENCE_PARTIAL"

    required_fields = [str(x) for x in requirement.get("required_fields") or [] if str(x)]
    if required_fields:
        descriptor = entry.get("content_descriptor") if isinstance(entry.get("content_descriptor"), dict) else {}
        summary = entry.get("summary") if isinstance(entry.get("summary"), dict) else {}
        known_fields = set()
        for key in ("fields", "row_fields", "available_fields"):
            values = descriptor.get(key)
            if isinstance(values, list):
                known_fields.update(str(x) for x in values)
        known_fields.update(str(k) for k in summary.keys())
        missing = [field for field in required_fields if field not in known_fields]
        if missing:
            return False, "EVIDENCE_FIELDS_MISSING"

    return True, ("EVIDENCE_PARTIAL" if status == "partial" else None)


def requirements_status(
    state: dict[str, Any]
) -> tuple[dict[str, bool], dict[str, dict[str, Any]]]:
    catalog = [
        x for x in state.get("evidence_catalog")
        or (state.get("memory_context") or {}).get("evidence_catalog")
        or []
        if isinstance(x, dict)
    ]
    requirements = [x for x in state.get("evidence_requirements") or [] if isinstance(x, dict)]
    ready: dict[str, bool] = {}
    detail: dict[str, dict[str, Any]] = {}
    for index, requirement in enumerate(requirements):
        key = str(requirement.get("family") or requirement.get("semantic_type") or f"requirement_{index}")
        matches = []
        failures = []
        for entry in catalog:
            ok, limitation = requirement_entry_satisfies(entry, requirement)
            acceptable = set(requirement.get("acceptable_semantic_types") or [requirement.get("semantic_type")])
            if str(entry.get("semantic_type") or "") not in acceptable:
                continue
            if ok:
                matches.append((entry, limitation))
            elif limitation:
                failures.append((entry, limitation))
        ready[key] = bool(matches)
        if matches:
            entry, limitation = matches[0]
            detail[key] = {
                "source": "evidence_requirement",
                "evidence_id": entry.get("evidence_id"),
                "semantic_type": entry.get("semantic_type"),
                "authority": entry.get("authority"),
                "limitation": limitation,
                "requirement": requirement,
            }
        elif failures:
            entry, failure = failures[0]
            detail[key] = {
                "source": "evidence_requirement",
                "evidence_id": entry.get("evidence_id"),
                "semantic_type": entry.get("semantic_type"),
                "authority": entry.get("authority"),
                "failure": failure,
                "requirement": requirement,
            }
        else:
            detail[key] = {
                "source": "none",
                "failure": "EVIDENCE_NOT_FOUND",
                "requirement": requirement,
            }
    return ready, detail
