from __future__ import annotations

import json
import re
from typing import Any, Mapping

_INTENT_KEYS = (
    "alarm_types",
    "query_type",
    "alarm_state",
    "group_by",
    "limit",
    "time_range",
    "sort_by",
    "sort_order",
    "warn_levels",
    "deal_status",
    "confirm_status",
    "restrain_flag",
    "model_no",
    "model_name",
)

_AREA_SCOPE_PATTERN = re.compile(
    r"(?P<name>[\u4e00-\u9fffA-Za-z0-9#号一二三四五六七八九十（）()·._-]{1,40}"
    r"(?:事业部|车间|产线|工段|厂区|区域|基地|公司))"
)
_GLOBAL_SCOPE_PATTERN = re.compile(r"(?:全厂|全公司|全集团|全局|全部区域|所有区域|各区域)")
_POINT_HINT_PATTERN = re.compile(r"(?:测点|点位|监测点|传感器)")
_EQUIPMENT_TYPE_PATTERN = re.compile(
    r"(?:轧机|飞剪|鼓风机|引风机|风机|水泵|泵机|电机|减速机|压缩机|磨机|"
    r"冷床|锥箱|吐丝机|夹送辊)"
)

_ENTITY_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]{6,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z][A-Za-z0-9_-]{5,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_REFERENTIAL_EQUIPMENT_PATTERN = re.compile(
    r"(?:这个|该|这台|本)(?:设备|机器|机组|轧机|飞剪|鼓风机|引风机|风机|"
    r"水泵|泵机|电机|减速机|压缩机|磨机|冷床|锥箱|吐丝机|夹送辊)"
)
_CONCRETE_EQUIPMENT_PATTERN = re.compile(
    r"(?:\d+|[一二三四五六七八九十百]+)\s*(?:#|号|架)?\s*"
    r"(?:轧机|飞剪|鼓风机|引风机|风机|水泵|泵机|电机|减速机|压缩机|磨机|"
    r"冷床|锥箱|吐丝机|夹送辊)"
)
_AREA_SCOPE_CORRECTION_PATTERN = re.compile(
    r"(?:不要|不是|别查|不查).{0,8}(?:设备|单机|机器)|"
    r"(?:事业部|车间|产线|工段|厂区|区域|基地|公司).{0,8}(?:整体|全部|所有|全量)|"
    r"(?:整个|全部|所有|全)(?:事业部|车间|产线|工段|厂区|区域|基地|公司)"
)
_SCOPE_IDENTITY_KEYS = {
    "device_code", "device_id", "equip_no", "equip_id", "equipment_no", "equipNo",
    "equip_name", "equipment_name", "point_no", "point_id", "pointNo", "pointId",
    "wave_point_no", "wave_point_code", "feature_point_id", "temperature_point_no",
    "temperature_point_id",
}



def _split_space_path(value: Any) -> list[str]:
    return [item.strip() for item in re.split(r"[/\\>|｜]+", str(value or "")) if item.strip()]


def _split_space_link(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split("/") if item.strip()]


def _scope_only_metadata(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(entity or {})
    metadata = source.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if key not in _SCOPE_IDENTITY_KEYS and value not in (None, "", [], {}):
            result[key] = value
    return result


def _implicit_scope_suffix(query: str) -> str | None:
    text = str(query or "")
    for suffix in ("事业部", "车间", "产线", "工段", "厂区", "区域", "基地", "公司"):
        if suffix in text:
            return suffix
    return None


def derive_context_area_target(
    query: str,
    entity: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Promote a known equipment/point context to an explicitly requested ancestor scope.

    Example: after querying ``1#飞剪``, the follow-up ``第一炼钢事业部有哪些报警``
    must target the ancestor business-unit scope, not reuse the equipment filter.  When
    ``space_path`` and ``space_link`` are aligned, the ancestor ``space_id``/prefix is
    derived deterministically without another fuzzy lookup.  If a concrete equipment is
    explicitly named in the same utterance, equipment scope wins and no promotion occurs.
    """

    if not entity:
        return None
    text = str(query or "").strip()
    if not text:
        return None

    correction = bool(_AREA_SCOPE_CORRECTION_PATTERN.search(text))
    if _CONCRETE_EQUIPMENT_PATTERN.search(text) and not correction:
        return None

    source = dict(entity)
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}

    named = _AREA_SCOPE_PATTERN.search(text)
    target_name = str(named.group("name") if named else "").strip()
    target_suffix = _implicit_scope_suffix(text)

    path = _split_space_path(source.get("space_path"))
    links = _split_space_link(source.get("space_link"))

    # A correction such as “不要设备，要事业部整体” may omit the actual ancestor
    # name or be greedily captured as “要事业部”. Recover the real hierarchy node
    # deterministically from the requested suffix before doing name matching.
    if correction:
        suffix = target_suffix or "事业部"
        hierarchy_target = next((item for item in path if item.endswith(suffix)), "")
        if not hierarchy_target:
            for key in (
                "plant_name", "region_name", "area_name", "workshop_name",
                "line_name", "company_name", "group_name",
            ):
                value = str(source.get(key) or "").strip()
                if value and value.endswith(suffix):
                    hierarchy_target = value
                    break
        if hierarchy_target:
            target_name = hierarchy_target

    if not target_name:
        return None

    expected = _normalize_entity_text(target_name)
    # Ensure a named scope really belongs to the current hierarchy. Otherwise this is
    # a new entity and the normal replace/fuzzy path must handle it.
    hierarchy_values = [
        str(source.get(key) or "")
        for key in (
            "group_name", "company_name", "plant_name", "region_name", "area_name",
            "workshop_name", "line_name", "leaf_space_name",
        )
        if source.get(key) not in (None, "")
    ] + path
    matched_value = next(
        (value for value in hierarchy_values if expected and (expected in _normalize_entity_text(value) or _normalize_entity_text(value) in expected)),
        "",
    )
    if not matched_value:
        return None
    target_name = matched_value
    expected = _normalize_entity_text(target_name)

    target_index: int | None = None
    for index, value in enumerate(path):
        normalized = _normalize_entity_text(value)
        if expected and (expected in normalized or normalized in expected):
            target_index = index
            target_name = value
            break

    # We only promote without fuzzy resolution when the ancestor has deterministic
    # identity. Aligned path/link arrays are the common fuzzy-entity contract.
    target_id = ""
    target_link = ""
    target_path = ""
    if target_index is not None and len(path) == len(links) and target_index < len(links):
        target_id = links[target_index]
        target_link = "/".join(links[: target_index + 1]) + "/"
        target_path = "/".join(path[: target_index + 1])
    else:
        current_space_name = str(source.get("space_name") or source.get("leaf_space_name") or "")
        if current_space_name and _normalize_entity_text(current_space_name) == expected:
            target_id = str(source.get("space_id") or "")
            target_link = str(source.get("space_link") or "")
            target_path = str(source.get("space_path") or target_name)

    if not target_id and not target_link:
        return None

    result: dict[str, Any] = {
        "entity_type": "space",
        "space_name": target_name,
        "leaf_space_name": target_name,
        "space_path": target_path or target_name,
    }
    if target_id:
        result["space_id"] = target_id
    if target_link:
        result["space_link"] = target_link

    # Preserve only hierarchy facts; device/point identity must never leak into the
    # promoted scope because alarm scope injection treats resolved_entity as authority.
    for key in (
        "group_name", "company_name", "plant_name", "region_name", "area_name",
        "workshop_name", "line_name", "space_number",
    ):
        value = source.get(key)
        if value not in (None, ""):
            result[key] = value
    metadata_out = _scope_only_metadata(entity)
    for key in _SCOPE_IDENTITY_KEYS:
        metadata_out.pop(key, None)
    if metadata_out:
        result["metadata"] = metadata_out
    return result


def is_alarm_query(query: str) -> bool:
    """Return whether the utterance requests PHM alarm facts from the business system.

    Alarm routing is a capability admission decision, not an entity-resolution decision.
    The supervisor may still choose diagnosis in addition to alarm lookup for causal
    questions, but an explicit request for alarm records/statistics must never fall back
    to the general model merely because the planner omitted the alarm tool.
    """

    text = re.sub(r"\s+", "", str(query or "")).lower()
    if not text or not any(token in text for token in ("报警", "预警")):
        return False

    # Explanations of the *concept/capability* are general questions, not live business
    # reads.  Concrete asset/scope wording below still wins, so ``第一炼钢事业部报警``
    # remains a business query even without an explicit verb.
    general_only = any(
        token in text
        for token in (
            "什么是报警", "报警是什么意思", "什么是预警", "预警是什么意思",
            "报警功能", "预警功能", "报警能力", "预警能力",
            "怎么使用报警", "如何使用报警", "报警怎么用", "预警怎么用",
        )
    )
    if general_only:
        return False

    record_markers = (
        "查询", "查一下", "查下", "查看", "看看", "有哪些", "有什么",
        "多少", "几条", "记录", "信息", "情况", "明细", "列表",
        "统计", "汇总", "排行", "排名", "最近", "当前", "历史",
        "活跃", "待处理", "待确认", "已处理",
    )
    if any(token in text for token in record_markers):
        return True

    # A concrete business scope plus the alarm noun is already a request for facts.
    if (
        _AREA_SCOPE_PATTERN.search(text)
        or _GLOBAL_SCOPE_PATTERN.search(text)
        or _CONCRETE_EQUIPMENT_PATTERN.search(text)
        or _ENTITY_CODE_PATTERN.search(text)
        or _REFERENTIAL_EQUIPMENT_PATTERN.search(text)
    ):
        return True

    return False

def infer_alarm_equipment_keyword(query: str) -> str | None:
    """Return an equipment-type filter explicitly present in this utterance.

    The filter is intentionally derived from the *current* user text so a previous
    turn's equipment scope (for example 飞剪) cannot leak into a later whole-area
    alarm query.
    """

    match = _EQUIPMENT_TYPE_PATTERN.search(str(query or ""))
    return str(match.group(0)) if match else None


def build_alarm_business_intent(
    existing: Mapping[str, Any] | None,
    call_arguments: Mapping[str, Any] | None,
    *,
    objective: str = "",
) -> dict[str, Any]:
    intent = dict(existing or {})
    arguments = dict(call_arguments or {})
    for key in _INTENT_KEYS:
        if key in arguments and arguments[key] not in (None, "", [], {}):
            intent[key] = arguments[key]
    if objective:
        intent.setdefault("objective", objective)
    intent.setdefault("domain", "alarm")
    return intent


def infer_required_entity_level(
    query: str,
    requested_level: str | None,
    query_scope: Mapping[str, Any] | None = None,
    *,
    structured_level: str | None = None,
) -> str:
    """Apply deterministic safeguards to the supervisor's entity-level choice.

    The model remains responsible for normal routing, but named area queries must not
    accidentally degrade to a global database query when it emits ``none``. Explicit
    model choices are retained. Global terms such as ``全厂`` remain global.
    """

    # The current turn's structured identity contract is authoritative. Legacy
    # vocabulary cannot demote an unlisted equipment name to its parent area.
    if structured_level in {"none", "area", "equipment", "point"}:
        return structured_level
    requested = str(requested_level or "none").strip().lower()

    scope = dict(query_scope or {})
    scope_type = str(scope.get("scope_type") or "").upper()
    lookup_scope = str(scope.get("lookup_scope") or "").lower()
    if scope_type == "AREA_DESCENDANTS" or lookup_scope == "area_aggregate":
        return "area"

    text = str(query or "").strip()
    if not text or _GLOBAL_SCOPE_PATTERN.search(text):
        return "none"
    # User text wins over a stale planner level. Named/whole-area alarm queries are
    # area-scoped unless the same utterance explicitly names a concrete equipment.
    # This prevents a previous equipment target from pinning “第一炼钢事业部有哪些报警”
    # to the old equip_no.
    area_requested = bool(_AREA_SCOPE_PATTERN.search(text) or _AREA_SCOPE_CORRECTION_PATTERN.search(text))
    if area_requested and not _CONCRETE_EQUIPMENT_PATTERN.search(text):
        return "area"
    if _POINT_HINT_PATTERN.search(text) and not re.search(r"(?:全部|所有|各)测点", text):
        return "point"
    if requested in {"area", "equipment", "point"}:
        return requested
    return "none"



def entity_satisfies_required_level(
    entity: Mapping[str, Any] | None,
    required_level: str | None,
) -> bool:
    """Return whether an entity contains enough identity for the requested PHM level.

    ``bool(entity)`` is not sufficient for diagnosis: an equipment entity is real, but
    point-scoped Data/Feature/Diagnosis MCP calls still require a concrete measurement
    point.  Point-level validation is deliberately strict and also requires a device
    code because PHM Data MCP uses both values.
    """

    required = str(required_level or "none").strip().lower()
    if required == "none":
        return True
    if not entity:
        return False

    source = dict(entity)
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        source = {**dict(metadata), **source}

    device_code = next(
        (
            source.get(key)
            for key in ("device_code", "equip_no", "equipment_no", "equipNo")
            if source.get(key) not in (None, "")
        ),
        None,
    )
    point_code = next(
        (
            source.get(key)
            for key in (
                "point_no",
                "pointNo",
                "wave_point_no",
                "wave_point_code",
                "feature_point_id",
                "featurePointId",
                "point_id",
                "pointId",
                "temperature_point_no",
                "temperature_point_id",
            )
            if source.get(key) not in (None, "")
        ),
        None,
    )

    if required == "point":
        return bool(device_code and point_code)
    if required == "equipment":
        return bool(device_code)
    if required == "area":
        # Every area-scoped Asset/Data tool ultimately requires a real database ID.
        # Names, paths and links are useful retrieval evidence but cannot be injected as
        # root_space_id.  Keeping the capability check equally strict prevents the
        # planner from declaring an area ready only for tool argument construction to
        # fail later.
        return bool(source.get("space_id") or source.get("spaceId"))
    return bool(source)

def active_entity_matches_query(
    query: str,
    entity: Mapping[str, Any] | None,
) -> bool:
    """Return whether a branch-level active entity is safe for this query.

    Pronoun follow-ups may reuse the active entity. If the user explicitly names a
    different area *or a different concrete equipment*, the old entity must not
    suppress a fresh resolver call.
    """

    if not entity:
        return False
    text = str(query or "").strip()
    source = dict(entity)
    metadata = source.get("metadata")
    if isinstance(metadata, dict):
        source = {**metadata, **source}

    named_area = _AREA_SCOPE_PATTERN.search(text)
    if named_area:
        expected = _normalize_entity_text(named_area.group("name"))
        values: list[str] = []
        for key in (
            "group_name",
            "company_name",
            "plant_name",
            "region_name",
            "area_name",
            "line_name",
            "space_path",
            "leaf_space_name",
        ):
            if source.get(key) not in (None, ""):
                values.append(_normalize_entity_text(source[key]))
        if not any(expected in value or value in expected for value in values if value):
            return False

    # Only apply equipment-name mismatch protection when the active entity is itself an
    # equipment. Area entities remain reusable for queries such as “这个区域有哪些风机”.
    device_code = next(
        (
            source.get(key)
            for key in ("device_code", "equip_no", "equipment_no", "equipNo")
            if source.get(key) not in (None, "")
        ),
        None,
    )
    if not device_code:
        return True

    normalized_query = _normalize_entity_text(text)
    active_values = [
        _normalize_entity_text(source.get(key))
        for key in ("equip_no", "device_code", "equipment_no", "equipNo", "equip_name", "equipment_name")
        if source.get(key) not in (None, "")
    ]
    if any(value and value in normalized_query for value in active_values):
        return True

    # Explicit PHM/entity codes are stronger than branch context. If the user names a
    # different code, never allow the old equipment to suppress fresh resolution.
    explicit_codes = {item.upper() for item in _ENTITY_CODE_PATTERN.findall(text)}
    active_codes = {
        str(source.get(key) or "").strip().upper()
        for key in ("device_code", "equip_no", "equipment_no", "equipNo", "point_no", "pointNo")
        if source.get(key) not in (None, "")
    }
    if explicit_codes and not (explicit_codes & active_codes):
        return False

    if _REFERENTIAL_EQUIPMENT_PATTERN.search(text):
        return True
    if _EQUIPMENT_TYPE_PATTERN.search(text):
        # A concrete equipment-like phrase is present but does not match the active
        # equipment, so force fresh entity resolution rather than leaking old context.
        return False
    return True


def _normalize_entity_text(value: Any) -> str:
    text = str(value or "").lower().replace("酸扎", "酸轧")
    return re.sub(r"[\s/#号\\>｜|\-—_（）()·.]", "", text)


def build_alarm_workflow_inputs(
    *,
    query: str,
    resolved_entity: Mapping[str, Any] | None,
    active_entity: Mapping[str, Any] | None,
    query_scope: Mapping[str, Any] | None,
    entity_constraints: Mapping[str, Any] | None,
    business_intent: Mapping[str, Any] | None,
) -> dict[str, str]:
    compact = lambda value: json.dumps(  # noqa: E731
        dict(value or {}), ensure_ascii=False, separators=(",", ":")
    )
    return {
        "query": query,
        "resolved_entity": compact(resolved_entity),
        "active_entity": compact(active_entity),
        "query_scope_json": compact(query_scope),
        "entity_constraints_json": compact(entity_constraints),
        "business_intent_json": compact(business_intent),
    }
