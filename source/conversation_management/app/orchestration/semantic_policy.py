from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping


_CONSTRAINT_FIELDS = (
    "equipment",
    "equipment_type",
    "area",
    "point",
    "component",
    "position",
    "direction",
    "measurement",
    "descendant_target_type",
    "scope_expression",
    "collection_output_expression",
)

_OVERRIDE_FIELDS = {
    "equipment",
    "equipment_type",
    "area",
    "point",
    "component",
    "position",
    "direction",
    "measurement",
    "descendant_target_type",
    "collection_filters",
    "collection_output_mode",
    "scope_mode",
    "descendant_target_level",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _constraint(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {"raw_text": "", "retrieval_text": ""}
    return {
        "raw_text": _text(value.get("raw_text")),
        "retrieval_text": _text(value.get("retrieval_text")),
    }


def _contains_verbatim(source: str, raw_text: str) -> bool:
    source = _text(source)
    raw_text = _text(raw_text)
    return bool(source and raw_text and raw_text in source)


def _has_current_evidence(semantics: Mapping[str, Any], field: str, current_query: str) -> bool:
    if field == "scope_mode":
        return _contains_verbatim(
            current_query,
            _constraint(semantics.get("scope_expression"))["raw_text"],
        )
    if field == "collection_output_mode":
        return _contains_verbatim(
            current_query,
            _constraint(semantics.get("collection_output_expression"))["raw_text"],
        )
    if field == "collection_filters":
        for item in semantics.get("collection_filters") or []:
            if _contains_verbatim(current_query, _constraint(item)["raw_text"]):
                return True
        return False
    if field == "descendant_target_level":
        # A changed descendant level must be backed by a current-turn descendant type
        # phrase.  This keeps a bare clarification such as “总部钢铁” from changing the
        # already-confirmed equipment collection into a space/point collection.
        return _contains_verbatim(
            current_query,
            _constraint(semantics.get("descendant_target_type"))["raw_text"],
        )
    return _contains_verbatim(current_query, _constraint(semantics.get(field))["raw_text"])


def _normalize_scope_mode(value: Any) -> str:
    value = _text(value).lower()
    return value if value in {"auto", "recursive", "direct_only"} else "auto"


def _valid_scope_evidence(semantics: Mapping[str, Any], allowed_sources: list[str]) -> bool:
    raw = _constraint(semantics.get("scope_expression"))["raw_text"]
    if not any(_contains_verbatim(source, raw) for source in allowed_sources):
        return False
    # A scope restriction needs evidence beyond the identity/category itself.  This is
    # provenance validation, not keyword interpretation: if a model labels
    # “总部钢铁” itself as the DIRECT_ONLY expression, there is no user-supplied scope
    # qualifier and the restriction is rejected.  “总部钢铁直属” leaves the qualifier
    # part after removing the known identity phrase and is therefore eligible.
    residual = raw
    for field in ("area", "equipment", "equipment_type", "descendant_target_type"):
        identity_raw = _constraint(semantics.get(field))["raw_text"]
        if identity_raw:
            residual = residual.replace(identity_raw, "")
    residual = re.sub(r"[\s\W_]+", "", residual, flags=re.UNICODE)
    return bool(residual)


def _effective_recursive(
    semantics: Mapping[str, Any],
    *,
    current_query: str,
    origin_query: str,
) -> tuple[bool, str, str]:
    """Return a deterministic executable recursive flag.

    The model may describe a scope mode, but it cannot directly control the database
    traversal boolean.  A restrictive DIRECT_ONLY decision is accepted only when a
    verbatim scope phrase exists in an authorized user utterance.  AUTO is recursive
    for descendant collection queries because an area-scoped collection naturally
    means its descendants unless the user explicitly narrows it.
    """

    descendant = bool(semantics.get("descendant_collection_requested"))
    mode = _normalize_scope_mode(semantics.get("scope_mode"))
    expression = _constraint(semantics.get("scope_expression"))
    allowed_sources = [current_query]
    if origin_query:
        allowed_sources.append(origin_query)
    evidence_ok = _valid_scope_evidence(semantics, allowed_sources)

    if not descendant:
        # This value is not consumed by query_scope_collection, but keep a stable
        # default in the semantic contract for downstream observability.
        return True, "not_descendant_collection", expression["raw_text"]
    if mode == "direct_only" and evidence_ok:
        return False, "explicit_direct_only", expression["raw_text"]
    if mode == "recursive" and evidence_ok:
        return True, "explicit_recursive", expression["raw_text"]
    # AUTO or an unproven model restriction always chooses the safe collection
    # semantics: include descendant spaces.  This prevents LLM sampling variance from
    # silently changing an authoritative count from 115 to 0.
    return True, "default_descendant_collection", expression["raw_text"]


def _clean_overrides(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:16]:
        name = _text(value)
        if name in _OVERRIDE_FIELDS and name not in result:
            result.append(name)
    return result


def _merge_pending_semantics(
    current: dict[str, Any],
    *,
    pending: Mapping[str, Any],
    current_query: str,
) -> tuple[dict[str, Any], list[str], list[str]]:
    origin = pending.get("origin_asset_semantics")
    if not isinstance(origin, Mapping) or not origin:
        return current, [], []

    merged = deepcopy(dict(origin))
    missing_fields = {
        _text(item)
        for item in (pending.get("missing_fields") or [])
        if _text(item)
    }
    requested_overrides = _clean_overrides(current.get("explicit_overrides"))
    applied_overrides: list[str] = []
    rejected_overrides: list[str] = []

    # New fields introduced after an older pending clarification must get stable
    # defaults rather than inheriting an old raw boolean produced by the model.
    merged.setdefault("scope_mode", "auto")
    merged.setdefault("scope_expression", {"raw_text": "", "retrieval_text": ""})
    merged.setdefault(
        "collection_output_expression",
        {"raw_text": "", "retrieval_text": ""},
    )
    merged.setdefault("explicit_overrides", [])

    for field in _CONSTRAINT_FIELDS:
        candidate = _constraint(current.get(field))
        if not candidate["raw_text"]:
            continue
        origin_candidate = _constraint(merged.get(field))
        allowed_fill = field in missing_fields or not origin_candidate["raw_text"]
        explicit = field in requested_overrides
        if (allowed_fill or explicit) and _has_current_evidence(current, field, current_query):
            merged[field] = candidate
            if explicit and field not in applied_overrides:
                applied_overrides.append(field)
        elif explicit:
            rejected_overrides.append(field)

    # Collection filters are additive when they are actually present in this user
    # message.  Otherwise preserve the original narrowed collection unchanged.
    current_filters = [
        _constraint(item)
        for item in (current.get("collection_filters") or [])
        if _constraint(item)["raw_text"]
        and _contains_verbatim(current_query, _constraint(item)["raw_text"])
    ]
    if current_filters:
        if "collection_filters" in requested_overrides:
            merged["collection_filters"] = current_filters
            applied_overrides.append("collection_filters")
        elif not merged.get("collection_filters"):
            merged["collection_filters"] = current_filters

    # Identity/category fields can be explicitly replaced only with current-turn
    # verbatim evidence.  This supports “总部钢铁，不过改查风机” without letting a bare
    # “总部钢铁” erase the original 水泵 condition.
    for field in (
        "equipment",
        "equipment_type",
        "area",
        "point",
        "component",
        "position",
        "direction",
        "measurement",
        "descendant_target_type",
    ):
        if field not in requested_overrides:
            continue
        candidate = _constraint(current.get(field))
        if _contains_verbatim(current_query, candidate["raw_text"]):
            merged[field] = candidate
            if field not in applied_overrides:
                applied_overrides.append(field)
        elif field not in rejected_overrides:
            rejected_overrides.append(field)

    # These execution-shaping fields are never replaced merely because the second
    # classifier happened to emit a different default.  They require an explicit
    # override declaration plus current user evidence.
    if "collection_output_mode" in requested_overrides:
        if _has_current_evidence(current, "collection_output_mode", current_query):
            mode = _text(current.get("collection_output_mode")).lower()
            if mode in {"list", "count"}:
                merged["collection_output_mode"] = mode
                merged["collection_output_expression"] = _constraint(
                    current.get("collection_output_expression")
                )
                applied_overrides.append("collection_output_mode")
        else:
            rejected_overrides.append("collection_output_mode")

    if "scope_mode" in requested_overrides:
        if _has_current_evidence(current, "scope_mode", current_query):
            merged["scope_mode"] = _normalize_scope_mode(current.get("scope_mode"))
            merged["scope_expression"] = _constraint(current.get("scope_expression"))
            applied_overrides.append("scope_mode")
        else:
            rejected_overrides.append("scope_mode")

    if "descendant_target_level" in requested_overrides:
        if _has_current_evidence(current, "descendant_target_level", current_query):
            level = _text(current.get("descendant_target_level")).lower()
            if level in {"none", "space", "equipment", "point"}:
                merged["descendant_target_level"] = level
                applied_overrides.append("descendant_target_level")
        else:
            rejected_overrides.append("descendant_target_level")

    # Preserve structural collection intent from the original question. Current-turn
    # values can only strengthen lookup requirements, not silently turn collection off.
    for field in (
        "needs_asset_lookup",
        "collection_requested",
        "descendant_collection_requested",
    ):
        merged[field] = bool(merged.get(field)) or bool(current.get(field))
    merged["refresh_requested"] = bool(current.get("refresh_requested"))
    merged["explicit_overrides"] = applied_overrides
    return merged, applied_overrides, sorted(set(rejected_overrides))


@dataclass(frozen=True, slots=True)
class SemanticPolicyResult:
    classification: dict[str, Any]
    audit: dict[str, Any]


def apply_semantic_policy(
    classification: Mapping[str, Any],
    *,
    current_query: str,
    pending_clarification: Mapping[str, Any] | None,
) -> SemanticPolicyResult:
    """Turn model semantics into a deterministic, provenance-checked execution intent."""

    result = deepcopy(dict(classification))
    current = deepcopy(dict(result.get("asset_semantics") or {}))
    pending = (
        dict(pending_clarification)
        if isinstance(pending_clarification, Mapping)
        else {}
    )
    origin_query = _text(pending.get("origin_query"))
    had_pending = bool(pending.get("pending") is True or pending.get("origin_asset_semantics"))

    applied_overrides: list[str] = []
    rejected_overrides: list[str] = []
    if had_pending:
        current, applied_overrides, rejected_overrides = _merge_pending_semantics(
            current,
            pending=pending,
            current_query=current_query,
        )

    current.setdefault("scope_mode", "auto")
    current.setdefault("scope_expression", {"raw_text": "", "retrieval_text": ""})
    current.setdefault(
        "collection_output_expression",
        {"raw_text": "", "retrieval_text": ""},
    )
    current["scope_mode"] = _normalize_scope_mode(current.get("scope_mode"))

    effective_recursive, policy_source, scope_raw = _effective_recursive(
        current,
        current_query=current_query,
        origin_query=origin_query,
    )
    current["descendant_recursive"] = effective_recursive
    result["asset_semantics"] = current

    audit = {
        "policy_version": "1.0",
        "pending_merge_applied": had_pending,
        "origin_query_present": bool(origin_query),
        "scope_mode": current.get("scope_mode") or "auto",
        "scope_expression_raw": scope_raw,
        "effective_recursive": effective_recursive,
        "recursive_policy_source": policy_source,
        "collection_output_mode": current.get("collection_output_mode") or "list",
        "descendant_target_level": current.get("descendant_target_level") or "none",
        "applied_overrides": sorted(set(applied_overrides)),
        "rejected_overrides": sorted(set(rejected_overrides)),
    }
    return SemanticPolicyResult(classification=result, audit=audit)
