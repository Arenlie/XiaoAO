from __future__ import annotations

import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

from app.errors import AssetError, ErrorCode
from app.providers.llm import LlmProvider


LOOKUP_SCOPES = {
    "any",
    "space",
    "equipment",
    "point",
    "equipment_and_point",
    "area_aggregate",
}
RETURN_MODES = {"none", "single", "candidates", "collection", "hierarchy", "list"}
REFERENCE_LEVELS = {"none", "space", "equipment", "point"}
TERM_FIELDS = (
    "equipment",
    "equipment_type",
    "area",
    "point",
    "component",
    "position",
    "direction",
    "measurement",
    "equip_no",
    "point_no",
)


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_name(value: Any) -> str:
    """Generic comparison normalization; contains no PHM business aliases."""

    text = unicodedata.normalize("NFKC", clean(value)).casefold()
    return "".join(text.split())


def _bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _enum(value: Any, allowed: set[str], field_name: str) -> str:
    normalized = clean(value).lower()
    if normalized not in allowed:
        raise AssetError(
            ErrorCode.INVALID_ARGUMENT,
            "资产参数提取模型返回格式不正确，请稍后重试。",
            f"asset llm returned invalid {field_name}: {value!r}",
        )
    return normalized


def _verified_term(
    *,
    query: str,
    payload: dict[str, Any],
    field_name: str,
    provenance_texts: list[str] | None = None,
) -> tuple[str, str]:
    """Accept a model term only when its raw span exists in the current utterance.

    This is generic provenance validation, not language extraction: no device type,
    area suffix, component alias or wording pattern is encoded here.
    """

    value = payload.get(field_name)
    if not isinstance(value, dict):
        raise AssetError(
            ErrorCode.INVALID_ARGUMENT,
            "资产参数提取模型返回格式不正确，请稍后重试。",
            f"asset llm omitted structured term field: {field_name}",
        )
    raw = clean(value.get("raw_text"))
    retrieval = clean(value.get("retrieval_text"))
    if not raw and not retrieval:
        return "", ""
    if not raw or not retrieval:
        raise AssetError(
            ErrorCode.INVALID_ARGUMENT,
            "资产参数提取模型返回了无法校验的资产条件，请稍后重试。",
            f"asset llm returned incomplete raw/retrieval pair for {field_name}",
        )
    if field_name in {"equip_no", "point_no"} and normalize_name(raw) != normalize_name(retrieval):
        raise AssetError(ErrorCode.INVALID_ARGUMENT, "资产编码与提供的原文不一致，请重新查询。",
                         f"code provenance changed for {field_name}")
    haystacks = [query, *(provenance_texts or [])]
    if not any(normalize_name(raw) in normalize_name(text) for text in haystacks if text):
        raise AssetError(
            ErrorCode.INVALID_ARGUMENT,
            "资产参数提取模型返回了不在用户原话中的条件，请重新描述目标资产。",
            f"asset llm provenance check failed for {field_name}: {raw!r}",
        )
    return raw, retrieval


def _profile(value: dict[str, Any] | None) -> tuple[list[str], list[str], list[str]]:
    profile = value or {}
    areas: list[str] = []
    equipment_numbers: list[str] = []
    equipment_names: list[str] = []
    for key in (
        "responsible_areas",
        "preferred_areas",
        "focus_areas",
        "areas",
        "area_names",
        "frequent_areas",
        "responsible_area",
    ):
        raw = profile.get(key, [])
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, str):
                areas.append(item)
            elif isinstance(item, dict):
                term = (
                    item.get("space_link")
                    or item.get("space_path")
                    or item.get("area_name")
                    or item.get("name")
                )
                if term:
                    areas.append(str(term))
    for key in (
        "responsible_equipment",
        "responsible_equipments",
        "frequent_equipment",
        "preferred_equipment",
        "equipments",
        "equipment_list",
    ):
        raw = profile.get(key, [])
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if isinstance(item, str):
                equipment_names.append(item)
            elif isinstance(item, dict):
                number = item.get("equip_no") or item.get("equipment_no") or item.get("code")
                name = item.get("equip_name") or item.get("equipment_name") or item.get("name")
                area = item.get("space_link") or item.get("space_path") or item.get("area_name")
                if number:
                    equipment_numbers.append(str(number).upper())
                if name:
                    equipment_names.append(str(name))
                if area:
                    areas.append(str(area))

    def unique(items: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in items if item))

    return unique(areas), unique(equipment_numbers), unique(equipment_names)


@dataclass(slots=True)
class QueryConstraints:
    raw_query: str
    retrieval_query: str
    lookup_scope: str
    required_entity_level: str = "any"
    return_mode: str = "single"
    needs_asset_lookup: bool = True
    equipment_keyword: str = ""
    equip_no: str = ""
    equipment_type_keyword: str = ""
    point_keyword: str = ""
    point_no: str = ""
    component_keyword: str = ""
    component_expression: str = ""
    position_keyword: str = ""
    direction_keyword: str = ""
    measurement_keyword: str = ""
    area_keyword: str = ""
    diagnostic_mode: bool = False
    context_reference: bool = False
    context_reference_level: str = "none"
    has_explicit_area: bool = False
    area_keywords: list[str] = field(default_factory=list)
    collection_requested: bool = False
    descendant_collection_requested: bool = False
    descendant_target_level: str = "none"
    descendant_target_type_keyword: str = ""
    descendant_recursive: bool = True
    refresh_requested: bool = False
    profile_areas: list[str] = field(default_factory=list)
    profile_equip_nos: list[str] = field(default_factory=list)
    profile_equip_names: list[str] = field(default_factory=list)
    raw_spans: dict[str, str] = field(default_factory=dict)
    extraction_source: str = "asset_llm"
    extraction_confidence: float = 0.0
    llm_elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class QueryUnderstandingService:
    def __init__(self, llm: LlmProvider) -> None:
        self.llm = llm

    @staticmethod
    def _semantic_hint_payload(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="json")
            return dumped if isinstance(dumped, dict) else {}
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _scope_from_hints(required: str, terms: dict[str, str], reference_level: str) -> str:
        if required in {"space", "area", "line"}:
            return "space"
        if required in {"equipment", "point"}:
            return required
        if reference_level == "point":
            return "point"
        if reference_level == "equipment":
            return "equipment"
        if reference_level == "space":
            return "space"
        if terms.get("point_no"):
            return "point"
        if terms.get("equip_no"):
            return "equipment"
        if any(terms.get(name) for name in ("point", "component", "position", "direction", "measurement")):
            return "point"
        if any(terms.get(name) for name in ("equipment", "equipment_type")):
            return "equipment"
        if terms.get("area"):
            return "space"
        return "any"

    def _from_semantic_hints(
        self,
        *,
        query: str,
        required_entity_level: str,
        user_profile: dict[str, Any] | None,
        semantic_hints: Any,
        conversation_context: dict[str, Any] | None = None,
    ) -> QueryConstraints | None:
        payload = self._semantic_hint_payload(semantic_hints)
        if not payload:
            return None
        ctx = conversation_context or {}
        if payload.get("needs_asset_lookup") is False:
            return QueryConstraints(raw_query=query,retrieval_query=query,lookup_scope="any",
                required_entity_level=required_entity_level,return_mode="none",needs_asset_lookup=False,
                extraction_source="supervisor_skip_gate",extraction_confidence=1.0)
        provenance_texts = [
            str(item.get("content") or item.get("text") or "")
            for item in list(ctx.get("recent_messages") or [])[-6:]
            if isinstance(item, dict) and str(item.get("role") or "").lower() == "user"
            and str(payload.get("reference_target_level") or "none") != "none"
        ]
        provenance_texts.extend(str(x)[:10000] for x in ctx.get("attachment_texts") or [])
        pending = ctx.get("pending_clarification") if isinstance(ctx.get("pending_clarification"), dict) else {}
        if pending.get("origin_query"):
            provenance_texts.append(str(pending.get("origin_query")))

        required = clean(required_entity_level).lower() or "any"
        # A descendant collection still has one root identity.  When the current
        # resolve request asks for a space/area/line, downstream equipment/point
        # categories are intentionally ignored here and are consumed later by
        # query_scope_collection.  This prevents preserved clarification semantics
        # (for example "水泵") from failing provenance while resolving the newly
        # supplied root phrase (for example "总部钢铁").
        if required in {"space", "area", "line"}:
            hint_fields = ("area",)
        elif required == "equipment":
            hint_fields = ("equipment", "equipment_type", "area", "equip_no")
        else:
            hint_fields = (
                "equipment", "equipment_type", "area", "point",
                "component", "position", "direction", "measurement", "equip_no", "point_no",
            )
        terms: dict[str, str] = {name: "" for name in TERM_FIELDS}
        raw_spans: dict[str, str] = {name: "" for name in TERM_FIELDS}
        has_term = False
        for field_name in hint_fields:
            raw, retrieval = _verified_term(
                query=query,
                payload={field_name: payload.get(field_name, {})},
                field_name=field_name,
                provenance_texts=provenance_texts,
            )
            raw_spans[field_name] = raw
            terms[field_name] = retrieval
            has_term = has_term or bool(raw or retrieval)

        reference_level = clean(payload.get("reference_target_level")).lower() or "none"
        if reference_level not in REFERENCE_LEVELS:
            raise AssetError(
                ErrorCode.INVALID_ARGUMENT,
                "上游资产语义提示格式不正确，请稍后重试。",
                f"invalid semantic_hints.reference_target_level: {reference_level!r}",
            )
        explicit_needs_lookup = payload.get("needs_asset_lookup")
        collection_requested = _bool(payload.get("collection_requested"))
        descendant_collection_requested = _bool(payload.get("descendant_collection_requested"))
        descendant_target_level = clean(payload.get("descendant_target_level")).lower() or "none"
        if descendant_target_level not in {"none", "space", "equipment", "point"}:
            raise AssetError(
                ErrorCode.INVALID_ARGUMENT,
                "上游资产语义提示格式不正确，请稍后重试。",
                f"invalid semantic_hints.descendant_target_level: {descendant_target_level!r}",
            )
        if required in {"space", "area", "line"}:
            # This is a property of the later descendant collection, not of the root.
            descendant_type_raw, descendant_type_retrieval = "", ""
        else:
            descendant_type_raw, descendant_type_retrieval = _verified_term(
                query=query,
                payload={"descendant_target_type": payload.get("descendant_target_type", {})},
                field_name="descendant_target_type",
                provenance_texts=provenance_texts,
            )
        descendant_recursive = _bool(payload.get("descendant_recursive"), True)
        refresh_requested = _bool(payload.get("refresh_requested"))

        # A structured upstream ``needs_asset_lookup=false`` is authoritative.  This
        # lets Conversation skip Asset MCP entirely in the normal path, and also makes
        # direct callers safe when they do invoke resolve_entity defensively.
        if explicit_needs_lookup is False:
            profile_areas, profile_numbers, profile_names = _profile(user_profile)
            return QueryConstraints(
                raw_query=query,
                retrieval_query=clean(query),
                lookup_scope="any",
                required_entity_level=clean(required_entity_level).lower() or "any",
                return_mode="none",
                needs_asset_lookup=False,
                context_reference=False,
                context_reference_level="none",
                collection_requested=False,
                descendant_collection_requested=False,
                descendant_target_level="none",
                descendant_target_type_keyword="",
                descendant_recursive=True,
                refresh_requested=False,
                profile_areas=profile_areas,
                profile_equip_nos=profile_numbers,
                profile_equip_names=profile_names,
                extraction_source="supervisor_skip_gate",
                extraction_confidence=1.0,
                llm_elapsed_ms=0.0,
            )

        # Empty hints without an explicit gate are not treated as an instruction to
        # skip Asset MCP's own LLM.  This preserves compatibility for older callers.
        if not has_term and reference_level == "none":
            return None

        scope = self._scope_from_hints(required, terms, reference_level)
        # When the current operation is resolving a root space/area, category terms
        # such as equipment_type belong to the *downstream collection filter*, not
        # to the root-space retrieval query.  Mixing ``总部钢铁`` and ``水泵`` here
        # can pull equipment candidates into the space resolver and is exactly the
        # failure mode behind broad collection questions.
        if required in {"space", "area", "line"} and terms["area"]:
            retrieval_parts = [terms["area"]]
        else:
            retrieval_parts = [
                terms[name]
                for name in (
                    "area", "equipment", "equipment_type", "point",
                    "component", "position", "direction", "measurement",
                )
                if terms[name]
            ]
        retrieval_query = " ".join(dict.fromkeys(retrieval_parts)) or clean(query)
        area_keywords = [
            part.strip()
            for part in terms["area"].replace("＞", "/").replace(">", "/").split("/")
            if part.strip()
        ]
        profile_areas, profile_numbers, profile_names = _profile(user_profile)
        needs_lookup = (
            bool(explicit_needs_lookup)
            if isinstance(explicit_needs_lookup, bool)
            else (
                required in {"space", "area", "line", "equipment", "point"}
                or has_term
                or reference_level != "none"
            )
        )
        return QueryConstraints(
            raw_query=query,
            retrieval_query=retrieval_query,
            lookup_scope=scope,
            required_entity_level=required,
            # Collection intent describes what happens *below* a resolved root.
            # Root-space resolution itself must remain single/candidates so duplicate
            # roots require disambiguation instead of silently becoming a collection.
            return_mode=(
                "single"
                if required in {"space", "area", "line"}
                else ("collection" if collection_requested else "single")
            ),
            needs_asset_lookup=needs_lookup,
            equip_no=terms["equip_no"].upper(),
            point_no=terms["point_no"].upper(),
            equipment_keyword=terms["equipment"],
            equipment_type_keyword=terms["equipment_type"],
            point_keyword=terms["point"],
            component_keyword=terms["component"],
            component_expression=raw_spans["component"],
            position_keyword=terms["position"],
            direction_keyword=terms["direction"],
            measurement_keyword=terms["measurement"],
            area_keyword=terms["area"],
            context_reference=reference_level != "none",
            context_reference_level=reference_level,
            has_explicit_area=bool(raw_spans["area"]),
            area_keywords=area_keywords,
            collection_requested=collection_requested,
            descendant_collection_requested=descendant_collection_requested,
            descendant_target_level=descendant_target_level,
            descendant_target_type_keyword=descendant_type_retrieval,
            descendant_recursive=descendant_recursive,
            refresh_requested=refresh_requested,
            profile_areas=profile_areas,
            profile_equip_nos=profile_numbers,
            profile_equip_names=profile_names,
            raw_spans=raw_spans,
            extraction_source="supervisor_semantic_hints",
            extraction_confidence=0.0,
            llm_elapsed_ms=0.0,
        )

    async def understand(
        self,
        query: str,
        required_entity_level: str,
        user_profile: dict[str, Any] | None,
        conversation_context: dict[str, Any] | None = None,
        active_entity: dict[str, Any] | None = None,
        semantic_hints: Any = None,
    ) -> QueryConstraints:
        hinted = self._from_semantic_hints(
            query=query,
            required_entity_level=required_entity_level,
            user_profile=user_profile,
            semantic_hints=semantic_hints,
            conversation_context=conversation_context,
        )
        if hinted is not None:
            return hinted

        started = time.perf_counter()
        extracted = await self.llm.extract(
            query=query,
            required_entity_level=required_entity_level,
            conversation_context=conversation_context,
            active_entity=active_entity,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000

        # r6.2 adds descendant-collection fields, but ordinary asset queries must remain
        # compatible with older/partially upgraded Asset LLM responses.  The new
        # fields are optional unless the model explicitly requests a descendant
        # collection; missing values therefore mean "no descendant expansion".
        extracted.setdefault("descendant_collection_requested", False)
        extracted.setdefault("descendant_target_level", "none")
        extracted.setdefault("descendant_target_type", {"raw_text": "", "retrieval_text": ""})
        extracted.setdefault("descendant_recursive", True)

        missing = [
            field_name
            for field_name in (
                "needs_asset_lookup",
                "lookup_scope",
                "return_mode",
                "diagnostic_mode",
                "collection_requested",
                "refresh_requested",
                "context_reference",
                "context_reference_level",
                *TERM_FIELDS,
            )
            if field_name not in extracted
        ]
        if missing:
            raise AssetError(
                ErrorCode.INVALID_ARGUMENT,
                "资产参数提取模型返回字段不完整，请稍后重试。",
                f"asset llm omitted required fields: {missing}",
            )

        terms: dict[str, str] = {}
        raw_spans: dict[str, str] = {}
        for field_name in TERM_FIELDS:
            raw, retrieval = _verified_term(
                query=query,
                payload=extracted,
                field_name=field_name,
                provenance_texts=list((conversation_context or {}).get("attachment_texts") or []),
            )
            raw_spans[field_name] = raw
            terms[field_name] = retrieval

        model_scope = _enum(extracted.get("lookup_scope"), LOOKUP_SCOPES, "lookup_scope")
        required = clean(required_entity_level).lower() or "any"
        if required in {"space", "area"}:
            scope = "space"
        elif required == "line":
            scope = "space"
        elif required == "equipment":
            scope = "equipment"
        elif required == "point":
            scope = "point"
        else:
            scope = model_scope

        return_mode = _enum(extracted.get("return_mode"), RETURN_MODES, "return_mode")
        reference_level = _enum(
            extracted.get("context_reference_level"),
            REFERENCE_LEVELS,
            "context_reference_level",
        )
        context_reference = _bool(extracted.get("context_reference")) or reference_level != "none"
        collection_requested = _bool(extracted.get("collection_requested"))
        if collection_requested and return_mode in {"none", "single", "candidates"}:
            return_mode = "collection"
        descendant_collection_requested = _bool(extracted.get("descendant_collection_requested"))
        descendant_target_level = _enum(
            extracted.get("descendant_target_level"),
            {"none", "space", "equipment", "point"},
            "descendant_target_level",
        )
        _, descendant_type_retrieval = _verified_term(
            query=query, payload=extracted, field_name="descendant_target_type"
        )
        descendant_recursive = _bool(extracted.get("descendant_recursive"), True)

        needs_lookup = _bool(extracted.get("needs_asset_lookup"), True)
        if required in {"space", "area", "line", "equipment", "point"}:
            needs_lookup = True

        # Root-space resolution must be driven by the area phrase only.  Device
        # category/type constraints are preserved separately and applied after the
        # real root space is resolved.
        if required in {"space", "area", "line"} and terms["area"]:
            retrieval_parts = [terms["area"]]
        else:
            retrieval_parts = [
                terms[name]
                for name in (
                    "area",
                    "equipment",
                    "equipment_type",
                    "point",
                    "component",
                    "position",
                    "direction",
                    "measurement",
                    "equip_no",
                    "point_no",
                )
                if terms[name]
            ]
        retrieval_query = " ".join(dict.fromkeys(retrieval_parts)) or clean(query)
        area_keywords = [
            part.strip()
            for part in terms["area"].replace("＞", "/").replace(">", "/").split("/")
            if part.strip()
        ]
        profile_areas, profile_numbers, profile_names = _profile(user_profile)
        try:
            confidence = max(0.0, min(1.0, float(extracted.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0

        return QueryConstraints(
            raw_query=query,
            retrieval_query=retrieval_query,
            lookup_scope=scope,
            required_entity_level=required,
            return_mode=return_mode,
            needs_asset_lookup=needs_lookup,
            equipment_keyword=terms["equipment"],
            equip_no=terms["equip_no"],
            equipment_type_keyword=terms["equipment_type"],
            point_keyword=terms["point"],
            point_no=terms["point_no"],
            component_keyword=terms["component"],
            component_expression=raw_spans["component"],
            position_keyword=terms["position"],
            direction_keyword=terms["direction"],
            measurement_keyword=terms["measurement"],
            area_keyword=terms["area"],
            diagnostic_mode=_bool(extracted.get("diagnostic_mode")),
            context_reference=context_reference,
            context_reference_level=reference_level,
            has_explicit_area=bool(raw_spans["area"]),
            area_keywords=area_keywords,
            collection_requested=collection_requested,
            descendant_collection_requested=descendant_collection_requested,
            descendant_target_level=descendant_target_level,
            descendant_target_type_keyword=descendant_type_retrieval,
            descendant_recursive=descendant_recursive,
            refresh_requested=_bool(extracted.get("refresh_requested")),
            profile_areas=profile_areas,
            profile_equip_nos=profile_numbers,
            profile_equip_names=profile_names,
            raw_spans=raw_spans,
            extraction_confidence=confidence,
            llm_elapsed_ms=elapsed_ms,
        )
