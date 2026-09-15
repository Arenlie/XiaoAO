from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.tools.alarm_context import active_entity_matches_query
from app.asset_query_contract import TOOL_ID as COLLECTION_TOOL_ID, QueryError
from app.asset_collection_scope import is_unscoped_new_asset_collection
from app.tools.phm_asset_mcp import (
    PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
    PHM_ASSET_QUERY_DEVICES_TOOL_ID,
    PHM_ASSET_QUERY_POINTS_TOOL_ID,
    PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
    PHM_ASSET_TOOL_IDS,
)


@dataclass(slots=True)
class AssetQueryIntent:
    tool_id: str
    required_entity_level: str
    objective: str
    arguments: dict[str, Any] = field(default_factory=dict)


def infer_asset_query_intent(query: str) -> AssetQueryIntent | None:
    """Deprecated compatibility shim.

    Asset/list/collection intent is owned by the Supervisor structured model and the
    declarative workflow registry.  Deliberately do not classify natural language
    here: adding equipment/category keywords would reintroduce the bug where only
    enumerated wording works.
    """
    return None


def asset_tool_required_entity_level(tool_id: str) -> str:
    if tool_id in {PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID, PHM_ASSET_QUERY_POINTS_TOOL_ID}:
        return "equipment"
    if tool_id in {
        PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
        PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID,
        PHM_ASSET_QUERY_DEVICES_TOOL_ID,
        PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
    }:
        return "area"
    return "none"


def _merged_entity(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(entity or {})
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}
    return source


def _entity_candidates(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for key in ("selected_entity", "resolved_entity"):
        value = state.get(key)
        if isinstance(value, Mapping) and value:
            candidates.append(_merged_entity(value))
    active = state.get("active_entity")
    if isinstance(active, Mapping) and active and active_entity_matches_query(str(state.get("query") or ""), active):
        candidates.append(_merged_entity(active))
    return candidates


def resolve_asset_identity(state: Mapping[str, Any]) -> dict[str, str]:
    """Return strict, database-backed IDs accepted by Asset MCP.

    For spaces, names/paths/links are deliberately not accepted as scope IDs. The
    fuzzy resolver must provide a real space_id. For equipment, equip_no/device_code is
    accepted because that is the canonical key used by query_points.
    """

    candidates = _entity_candidates(state)
    for index, source in enumerate(candidates):
        space_id = next(
            (source.get(key) for key in ("space_id", "spaceId") if source.get(key) not in (None, "")),
            None,
        )
        equip_no = next(
            (
                source.get(key)
                for key in ("equip_no", "device_code", "equipment_no", "equipNo")
                if source.get(key) not in (None, "")
            ),
            None,
        )
        if space_id or equip_no:
            # A user-selected candidate is authoritative, but older candidate payloads
            # may omit its parent space_id.  After query_equipment_info returns, the
            # enriched resolved_entity can safely supply that space only when the
            # canonical equipment number is identical.
            if equip_no and not space_id:
                normalized_equip = str(equip_no).strip().upper()
                for secondary in candidates[index + 1 :]:
                    secondary_equip = next(
                        (
                            secondary.get(key)
                            for key in (
                                "equip_no",
                                "device_code",
                                "equipment_no",
                                "equipNo",
                            )
                            if secondary.get(key) not in (None, "")
                        ),
                        None,
                    )
                    secondary_space = next(
                        (
                            secondary.get(key)
                            for key in ("space_id", "spaceId")
                            if secondary.get(key) not in (None, "")
                        ),
                        None,
                    )
                    if (
                        secondary_space
                        and str(secondary_equip or "").strip().upper()
                        == normalized_equip
                    ):
                        space_id = secondary_space
                        break
            return {
                "space_id": str(space_id or "").strip(),
                "equip_no": str(equip_no or "").strip(),
            }
    return {"space_id": "", "equip_no": ""}


def enrich_equipment_entity_from_asset_detail(
    current: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge authoritative query_equipment_info fields into one equipment anchor."""

    existing = dict(current or {})
    raw_equipment = (payload or {}).get("equipment")
    if not isinstance(raw_equipment, Mapping) or not raw_equipment:
        return existing
    equipment = dict(raw_equipment)
    existing_merged = _merged_entity(existing)
    returned_merged = _merged_entity(equipment)
    existing_no = str(
        existing_merged.get("equip_no")
        or existing_merged.get("device_code")
        or ""
    ).strip().upper()
    returned_no = str(
        returned_merged.get("equip_no")
        or returned_merged.get("device_code")
        or ""
    ).strip().upper()
    if existing_no and returned_no and existing_no != returned_no:
        return existing

    metadata = {}
    if isinstance(existing.get("metadata"), Mapping):
        metadata.update(dict(existing["metadata"]))
    if isinstance(equipment.get("metadata"), Mapping):
        metadata.update(dict(equipment["metadata"]))
    enriched = {
        **existing,
        **{
            key: value
            for key, value in equipment.items()
            if value not in (None, "", [], {})
        },
        "entity_type": "equipment",
    }
    if metadata:
        enriched["metadata"] = metadata
    return enriched


def asset_identity_available(state: Mapping[str, Any], tool_id: str) -> bool:
    if tool_id == COLLECTION_TOOL_ID:
        from app.services.asset_collections import collection_intent
        if collection_intent(state).get("reference_mode", "new") != "new":
            return True
        if is_unscoped_new_asset_collection(state):
            return True
    identity = resolve_asset_identity(state)
    if tool_id in {PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID, PHM_ASSET_QUERY_POINTS_TOOL_ID}:
        return bool(identity.get("equip_no"))
    if tool_id in PHM_ASSET_TOOL_IDS:
        return bool(identity.get("space_id"))
    return True


def build_phm_asset_arguments(
    *,
    tool_id: str,
    state: Mapping[str, Any],
    call_arguments: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if tool_id == COLLECTION_TOOL_ID:
        from app.services.asset_collections import build_query
        try:
            return {"query": build_query(state)}, []
        except (QueryError, ValueError) as exc:
            return {}, [str(exc)]
    args = {
        key: value
        for key, value in dict(call_arguments or {}).items()
        if key not in {
            "objective",
            "required_entity_level",
            "root_space_id",
            "space_id",
            "equip_no",
            "_entity_down_drill",
            "_identity_source",
        }
        and value not in (None, "")
    }
    identity = resolve_asset_identity(state)
    missing: list[str] = []

    if tool_id == PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID:
        if identity.get("equip_no"):
            args["equip_no"] = identity["equip_no"]
        else:
            missing.append("equip_no")
    elif tool_id == PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID:
        if identity.get("space_id"):
            args["root_space_id"] = identity["space_id"]
        else:
            missing.append("space_id")
        args.setdefault("max_depth", 10)
        args.setdefault("include_devices", False)
        args.setdefault("include_points", False)
    elif tool_id in {PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID, PHM_ASSET_QUERY_DEVICES_TOOL_ID}:
        if identity.get("space_id"):
            args["space_id"] = identity["space_id"]
        else:
            missing.append("space_id")
        if tool_id == PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID:
            args.setdefault("recursive", False)
        else:
            args.setdefault("recursive", True)
            args.setdefault("limit", 1000)
    elif tool_id == PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID:
        # The root identity is always platform-owned.  The model only supplies the
        # structured descendant target level/type; it can never inject a database ID.
        if identity.get("space_id"):
            args["root_space_id"] = identity["space_id"]
        else:
            missing.append("space_id")

        business_intent = (
            dict(state.get("business_intent") or {})
            if isinstance(state.get("business_intent"), Mapping)
            else {}
        )
        semantics = (
            dict(business_intent.get("asset_semantics") or {})
            if isinstance(business_intent.get("asset_semantics"), Mapping)
            else {}
        )
        target_level = str(
            semantics.get("descendant_target_level")
            or args.get("target_entity_level")
            or ""
        ).strip().lower()
        if target_level in {"space", "equipment", "point"}:
            args["target_entity_level"] = target_level
        else:
            missing.append("target_entity_level")

        target_type = semantics.get("descendant_target_type")
        if isinstance(target_type, Mapping) and target_level == "space":
            normalized_target_type = str(
                target_type.get("retrieval_text") or target_type.get("raw_text") or ""
            ).strip()
            if normalized_target_type:
                args["target_space_type"] = normalized_target_type
        equipment_type = semantics.get("equipment_type")
        if isinstance(equipment_type, Mapping) and target_level == "equipment":
            normalized_equipment_type = str(
                equipment_type.get("retrieval_text") or equipment_type.get("raw_text") or ""
            ).strip()
            if normalized_equipment_type:
                args["target_equipment_type"] = normalized_equipment_type
        raw_filters = semantics.get("collection_filters")
        if isinstance(raw_filters, list) and target_level in {"equipment", "space"}:
            filters: list[str] = []
            for item in raw_filters[:8]:
                if not isinstance(item, Mapping):
                    continue
                value = str(item.get("retrieval_text") or item.get("raw_text") or "").strip()
                if value and value not in filters:
                    filters.append(value)
            if filters:
                args["semantic_filters"] = filters
        output_mode = str(semantics.get("collection_output_mode") or "list").strip().lower()
        args["output_mode"] = output_mode if output_mode in {"list", "count"} else "list"
        semantic_policy = (
            dict(business_intent.get("semantic_policy") or {})
            if isinstance(business_intent.get("semantic_policy"), Mapping)
            else {}
        )
        if isinstance(semantic_policy.get("effective_recursive"), bool):
            # Semantic Policy is authoritative. The Supervisor model does not get to
            # flip a database traversal boolean directly.
            args["recursive"] = bool(semantic_policy["effective_recursive"])
        elif "descendant_recursive" in semantics:
            # Compatibility for older persisted intent objects. New classifications
            # always carry semantic_policy.effective_recursive.
            args["recursive"] = bool(semantics.get("descendant_recursive"))
        else:
            args.setdefault("recursive", True)
        args.setdefault("limit", 50000)
    elif tool_id == PHM_ASSET_QUERY_POINTS_TOOL_ID:
        if identity.get("equip_no"):
            args["equip_no"] = identity["equip_no"]
        else:
            missing.append("equip_no")
        args.setdefault("limit", 1000)
    return args, missing


def format_asset_result(tool_id: str, payload: Mapping[str, Any]) -> str:
    if tool_id == COLLECTION_TOOL_ID:
        from app.services.asset_collections import fact_text
        return fact_text(payload)
    """Create a deterministic readable fallback while preserving structured evidence."""

    if not payload:
        return "资产查询未返回数据。"
    if payload.get("success") is False:
        return str(payload.get("message") or "资产查询失败。")

    truncated = bool(payload.get("truncated"))
    suffix = "\n\n注意：结果达到服务端上限，以下结构不是完整全集。" if truncated else ""

    if tool_id == PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID:
        equipment = payload.get("equipment") if isinstance(payload.get("equipment"), Mapping) else {}
        hierarchy = payload.get("hierarchy") if isinstance(payload.get("hierarchy"), Mapping) else {}
        lines = [
            f"设备名称：{equipment.get('equip_name') or '未提供'}",
            f"设备编码：{equipment.get('equip_no') or '未提供'}",
        ]
        if equipment.get("equipment_type"):
            lines.append(f"设备类型：{equipment.get('equipment_type')}")
        if equipment.get("space_path"):
            lines.append(f"所属路径：{equipment.get('space_path')}")
        elif hierarchy.get("space_path"):
            lines.append(f"所属路径：{hierarchy.get('space_path')}")
        for key, label in (
            ("company_name", "公司"),
            ("plant_name", "厂区"),
            ("region_name", "区域/事业部"),
            ("area_name", "区域"),
            ("line_name", "产线"),
            ("leaf_space_name", "所属空间"),
        ):
            value = hierarchy.get(key)
            if value:
                lines.append(f"{label}：{value}")
        return "\n".join(lines)

    if tool_id == PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID:
        root = payload.get("root") if isinstance(payload.get("root"), Mapping) else {}
        nodes = [node for node in (payload.get("nodes") or []) if isinstance(node, Mapping)]
        root_id = str(root.get("space_id") or "")
        root_name = str(root.get("space_name") or root_id or "根空间")
        children: dict[str, list[Mapping[str, Any]]] = {}
        for node in nodes:
            if str(node.get("node_type") or "space") != "space":
                continue
            parent = str(node.get("parent_space_id") or root_id)
            children.setdefault(parent, []).append(node)
        for values in children.values():
            values.sort(key=lambda item: (int(item.get("depth") or 0), str(item.get("path") or ""), str(item.get("space_name") or "")))

        lines = [root_name]
        visited: set[str] = set()

        def walk(parent_id: str, prefix: str) -> None:
            items = children.get(parent_id, [])
            for index, node in enumerate(items):
                node_id = str(node.get("space_id") or "")
                if node_id and node_id in visited:
                    continue
                if node_id:
                    visited.add(node_id)
                last = index == len(items) - 1
                branch = "└── " if last else "├── "
                lines.append(prefix + branch + str(node.get("space_name") or node_id or "未命名空间"))
                walk(node_id, prefix + ("    " if last else "│   "))

        walk(root_id, "")
        # Preserve otherwise-unattached returned nodes rather than silently dropping them.
        attached_ids = visited
        unattached = [n for n in nodes if str(n.get("node_type") or "space") == "space" and str(n.get("space_id") or "") not in attached_ids]
        if unattached:
            lines.append("\n未能按 parent_space_id 挂接但服务端已返回的节点：")
            lines.extend(f"- {n.get('space_name') or n.get('space_id')} (space_id={n.get('space_id')})" for n in unattached)
        return "\n".join(lines) + suffix

    if tool_id == PHM_ASSET_QUERY_SPACE_CHILDREN_TOOL_ID:
        items = [x for x in (payload.get("children") or []) if isinstance(x, Mapping)]
        lines = [f"共查询到 {payload.get('count', len(items))} 个空间节点："]
        lines.extend(f"- {x.get('space_name') or x.get('space_id')}（space_id={x.get('space_id')}）" for x in items)
        return "\n".join(lines) + suffix

    if tool_id == PHM_ASSET_QUERY_DEVICES_TOOL_ID:
        items = [x for x in (payload.get("devices") or []) if isinstance(x, Mapping)]
        lines = [f"共查询到 {payload.get('count', len(items))} 台设备："]
        lines.extend(f"- {x.get('equip_name') or x.get('equip_no')}（{x.get('equip_no') or '无设备编码'}）" for x in items)
        return "\n".join(lines) + suffix

    if tool_id == PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID:
        root = payload.get("root") if isinstance(payload.get("root"), Mapping) else {}
        sample = [x for x in (payload.get("sample") or []) if isinstance(x, Mapping)]
        level = str(payload.get("target_entity_level") or "实体")
        target_type = str(payload.get("target_space_type") or "").strip()
        equipment_type = str(payload.get("target_equipment_type") or "").strip()
        extra_filters = [str(x) for x in (payload.get("semantic_filters") or []) if str(x).strip()]
        label_parts = [x for x in [equipment_type, *extra_filters] if x]
        label = "、".join(label_parts) or target_type or {"space": "空间节点", "equipment": "设备", "point": "测点"}.get(level, "实体")
        count = payload.get("count")
        if count is None:
            lines = [str(payload.get("message") or "资产分类条件无法安全映射到已审核标签，未执行统计。")]
        else:
            noun = "台" if level == "equipment" else "个"
            lines = [f"已在 {root.get('space_name') or root.get('space_id') or '目标区域'} 下按已审核资产语义查询 {label}，共确认 {count} {noun}。"]
        resolution = payload.get("semantic_resolution") if isinstance(payload.get("semantic_resolution"), Mapping) else {}
        resolved = [x for x in (resolution.get("resolved") or []) if isinstance(x, Mapping)]
        if resolved:
            lines.append("数据库标签条件：" + "；".join(f"{x.get('input_term')}→{x.get('tag_name')}[{x.get('tag_code')}]" for x in resolved))
        uncertain_count = int(payload.get("uncertain_count") or 0)
        if uncertain_count:
            lines.append(f"另有 {uncertain_count} 个旧数据设备因原设备类型字段缺失或语义不足标记为待确认，未计入确认数量。")
        strategy = payload.get("query_strategy") if isinstance(payload.get("query_strategy"), Mapping) else {}
        if strategy.get("mode") == "parallel_paged":
            lines.append(str(strategy.get("message_cn") or "超过单次查询上限，已进行多次并行查询，速度较慢。"))
        items = [x for x in (payload.get("items") or []) if isinstance(x, Mapping)]
        if payload.get("items_complete") and items:
            lines.append("确认属于该集合的实体：")
            for item in items:
                name = item.get("space_name") or item.get("equip_name") or item.get("point_name")
                code = item.get("space_id") or item.get("equip_no") or item.get("point_no")
                lines.append(f"- {name or code or '未命名实体'}（{code or '无编码'}）")
        elif sample:
            lines.append("结果数量较多，以下展示前10个实体示例：")
            for item in sample:
                name = item.get("space_name") or item.get("equip_name") or item.get("point_name")
                code = item.get("space_id") or item.get("equip_no") or item.get("point_no")
                lines.append(f"- {name or code or '未命名实体'}（{code or '无编码'}）")
        if payload.get("collection_forwarded_to_batch"):
            lines.append("完整集合已保留在本轮运行内存中，交给下一步并行健康度查询使用。")
        elif not payload.get("items_complete") and int(payload.get("count") or 0) > len(items):
            lines.append("集合过大，最终回答保留完整确认数量并仅展示部分样例，避免把数万条资产记录塞入大模型上下文。")
        return "\n".join(lines) + suffix

    if tool_id == PHM_ASSET_QUERY_POINTS_TOOL_ID:
        equipment = payload.get("equipment") if isinstance(payload.get("equipment"), Mapping) else {}
        items = [x for x in (payload.get("points") or []) if isinstance(x, Mapping)]
        lines = [f"{equipment.get('equip_name') or equipment.get('equip_no') or '目标设备'}共查询到 {payload.get('count', len(items))} 个测点："]
        lines.extend(f"- {x.get('point_name') or x.get('point_no')}（{x.get('point_no') or '无测点编码'}）" for x in items)
        return "\n".join(lines) + suffix

    return str(payload)
