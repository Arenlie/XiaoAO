from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.schemas.resolve import ResolveEntityRequest
from app.services.query_understanding import QueryConstraints, normalize_name

ResolutionAction = Literal["SKIP", "REUSE", "SEARCH", "REFRESH", "DOWN_DRILL"]
ReturnMode = Literal["none", "single", "candidates", "collection", "hierarchy", "list"]

_LEVEL_RANK = {"any": 0, "space": 1, "equipment": 2, "point": 3}


def query_fingerprint(query: str, required_level: str) -> str:
    compact = normalize_name(query)
    return hashlib.sha256(f"{required_level}\0{compact}".encode("utf-8")).hexdigest()[:24]


def _target_level(required_level: str, lookup_scope: str) -> str:
    if required_level in {"space", "area", "line"}:
        return "space"
    if required_level in {"equipment", "point"}:
        return required_level
    if lookup_scope in {"space", "area", "line", "area_aggregate"}:
        return "space"
    if lookup_scope in {"point", "equipment_and_point"}:
        return "point"
    if lookup_scope == "equipment":
        return "equipment"
    return "any"


def _entity_level(entity: dict[str, Any]) -> str:
    metadata = entity.get("metadata") if isinstance(entity.get("metadata"), dict) else {}
    merged = {**metadata, **entity}
    if any(merged.get(key) for key in ("point_no", "point_id", "pointNo", "pointId")):
        return "point"
    if any(merged.get(key) for key in ("equip_no", "equip_id", "device_code", "device_id")):
        return "equipment"
    if any(merged.get(key) for key in ("space_id", "space_link", "space_path", "space_name")):
        return "space"
    raw = str(merged.get("entity_type") or merged.get("type") or "").lower()
    return "space" if raw in {"area", "line", "region", "space"} else raw


def _identity_terms(query: QueryConstraints) -> set[str]:
    return {
        normalize_name(value)
        for value in (
            query.area_keyword,
            query.equipment_keyword,
            query.equipment_type_keyword,
            query.equip_no,
            query.point_keyword,
            query.point_no,
        )
        if normalize_name(value)
    }


def _can_reference_level(entity: dict[str, Any], reference_level: str) -> bool:
    level = _entity_level(entity)
    if reference_level == "point":
        return level == "point"
    if reference_level == "equipment":
        return level in {"equipment", "point"}
    if reference_level == "space":
        return level in {"space", "equipment", "point"}
    return bool(level)


def context_entity(
    request: ResolveEntityRequest,
    *,
    reference_level: str = "none",
) -> dict[str, Any]:
    context = request.conversation_context or {}
    previous = request.previous_resolution or {}
    candidates: list[dict[str, Any]] = []

    def add(value: Any) -> None:
        if isinstance(value, dict) and value:
            candidates.append(dict(value))

    add(context.get("selected_entity"))
    add(request.active_entity)
    for item in reversed(list(context.get("recent_entities") or [])):
        if isinstance(item, dict):
            add(item.get("entity") or item.get("resolved_entity") or item)
    add(context.get("active_entity"))
    add(context.get("last_resolved_entity"))
    add(previous.get("resolved_entity") or previous.get("entity"))

    if reference_level in {"space", "equipment", "point"}:
        compatible = [
            entity
            for entity in candidates
            if _can_reference_level(entity, reference_level)
        ]
        if compatible:
            value = compatible[0]
            # A typed parent reference must not carry a child's identity into reuse.
            if reference_level == "space":
                merged = {**(value.get("metadata") or {}), **value}
                return {"entity_type": "space", **{k: v for k, v in merged.items()
                        if k.startswith("space_") and v}}
            if reference_level == "equipment":
                merged = {**(value.get("metadata") or {}), **value}
                return {"entity_type": "equipment", **{k: v for k, v in merged.items()
                        if (k.startswith("space_") or k.startswith("equip_") or k.startswith("device_")) and v}}
            return value
    return candidates[0] if candidates else {}


@dataclass(slots=True)
class ResolutionPolicyDecision:
    action: ResolutionAction
    need_lookup: bool
    should_refresh: bool
    target_entity_level: str
    lookup_scope: str
    return_mode: ReturnMode
    context_used: bool
    reason: str
    query_fingerprint: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ResolutionPolicy:
    """Use structured LLM output and confirmed context; never parse query wording."""

    def decide(
        self,
        request: ResolveEntityRequest,
        query: QueryConstraints,
    ) -> ResolutionPolicyDecision:
        target = _target_level(request.required_entity_level, query.lookup_scope)
        fingerprint = query_fingerprint(request.query, target)
        mode: ReturnMode = query.return_mode  # type: ignore[assignment]
        reference_level = (
            query.context_reference_level
            if query.context_reference_level in {"space", "equipment", "point"}
            else target if query.context_reference else "none"
        )
        active = context_entity(request, reference_level=reference_level)
        active_level = _entity_level(active)
        active_rank = _LEVEL_RANK.get(active_level, 0)
        target_rank = _LEVEL_RANK.get(target, 0)
        identity_terms = _identity_terms(query)

        if not query.needs_asset_lookup and not query.context_reference:
            return ResolutionPolicyDecision(
                action="SKIP",
                need_lookup=False,
                should_refresh=False,
                target_entity_level="any",
                lookup_scope="any",
                return_mode="none",
                context_used=bool(active),
                reason="资产参数模型判定本轮不需要绑定真实资产实体。",
                query_fingerprint=query_fingerprint(request.query, "any"),
            )

        if request.force_refresh or query.refresh_requested:
            return ResolutionPolicyDecision(
                action="REFRESH",
                need_lookup=True,
                should_refresh=True,
                target_entity_level=target,
                lookup_scope=query.lookup_scope,
                return_mode=mode,
                context_used=bool(active),
                reason="调用方或资产参数模型明确要求重新检索实体。",
                query_fingerprint=fingerprint,
            )

        previous = request.previous_resolution or {}
        previous_entity = previous.get("resolved_entity") or previous.get("entity")
        if (
            request.allow_context_reuse
            and str(previous.get("query_fingerprint") or "") == fingerprint
            and isinstance(previous_entity, dict)
            and previous_entity
            and _LEVEL_RANK.get(_entity_level(previous_entity), 0) >= target_rank
        ):
            return ResolutionPolicyDecision(
                action="REUSE",
                need_lookup=False,
                should_refresh=False,
                target_entity_level=target,
                lookup_scope=query.lookup_scope,
                return_mode=mode,
                context_used=True,
                reason="当前请求与上一实体解析指纹一致，复用已确认实体。",
                query_fingerprint=fingerprint,
            )

        if target == "any" and not identity_terms and not (query.context_reference and active):
            return ResolutionPolicyDecision(
                action="SKIP",
                need_lookup=False,
                should_refresh=False,
                target_entity_level="any",
                lookup_scope="any",
                return_mode="none",
                context_used=False,
                reason="结构化参数中没有需要落地的资产身份条件。",
                query_fingerprint=fingerprint,
            )

        if active and request.allow_context_reuse:
            if query.context_reference:
                has_target_conditions = any(
                    (query.point_keyword, query.point_no, query.component_keyword,
                     query.position_keyword, query.direction_keyword, query.measurement_keyword)
                    if target == "point" else
                    (query.equipment_keyword, query.equipment_type_keyword, query.equip_no)
                    if target == "equipment" else (query.area_keyword,)
                )
                # A referential parent plus an explicit child still needs a database query.
                action: ResolutionAction = "REUSE" if active_rank >= target_rank and not has_target_conditions else "DOWN_DRILL"
                return ResolutionPolicyDecision(
                    action=action,
                    need_lookup=action == "DOWN_DRILL",
                    should_refresh=False,
                    target_entity_level=target,
                    lookup_scope=query.lookup_scope,
                    return_mode=mode,
                    context_used=True,
                    reason=(
                        "资产参数模型识别到历史实体指代，复用最近兼容实体。"
                        if action == "REUSE"
                        else "资产参数模型识别到历史实体指代，在已确认实体内下钻。"
                    ),
                    query_fingerprint=fingerprint,
                )

            if identity_terms:
                parent_identity_is_implicit = (
                    target == "point"
                    and active_level in {"equipment", "point"}
                    and not any(
                        (
                            query.area_keyword,
                            query.equipment_keyword,
                            query.equipment_type_keyword,
                            query.equip_no,
                        )
                    )
                ) or (
                    target == "equipment"
                    and active_level == "space"
                    and not query.area_keyword
                )
                if parent_identity_is_implicit:
                    return ResolutionPolicyDecision(
                        action="DOWN_DRILL",
                        need_lookup=True,
                        should_refresh=False,
                        target_entity_level=target,
                        lookup_scope=query.lookup_scope,
                        return_mode=mode,
                        context_used=True,
                        reason="在已确认的父级实体范围内执行向量检索。",
                        query_fingerprint=fingerprint,
                    )
                # Never compare free text to the active entity in code.  Every new
                # explicit identity goes through Embedding/Reranker; only an explicit
                # context reference or a matching prior fingerprint may reuse an ID.
                return ResolutionPolicyDecision(
                    action="REFRESH",
                    need_lookup=True,
                    should_refresh=True,
                    target_entity_level=target,
                    lookup_scope=query.lookup_scope,
                    return_mode=mode,
                    context_used=True,
                    reason="本轮包含新的显式资产身份，重新执行向量检索。",
                    query_fingerprint=fingerprint,
                )

            if target_rank > active_rank:
                return ResolutionPolicyDecision(
                    action="DOWN_DRILL",
                    need_lookup=True,
                    should_refresh=False,
                    target_entity_level=target,
                    lookup_scope=query.lookup_scope,
                    return_mode=mode,
                    context_used=True,
                    reason="本轮没有切换身份，在活动实体范围内下钻。",
                    query_fingerprint=fingerprint,
                )
            if active_rank >= target_rank:
                return ResolutionPolicyDecision(
                    action="REUSE",
                    need_lookup=False,
                    should_refresh=False,
                    target_entity_level=target,
                    lookup_scope=query.lookup_scope,
                    return_mode=mode,
                    context_used=True,
                    reason="本轮未提供新的身份条件，复用活动实体。",
                    query_fingerprint=fingerprint,
                )

        return ResolutionPolicyDecision(
            action="SEARCH",
            need_lookup=True,
            should_refresh=False,
            target_entity_level=target,
            lookup_scope=query.lookup_scope,
            return_mode=mode,
            context_used=bool(active),
            reason="使用 LLM 结构化参数执行 Embedding 召回与 Reranker 重排。",
            query_fingerprint=fingerprint,
        )
