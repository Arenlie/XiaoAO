from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.agents.catalog import FUZZY_ENTITY_AGENT_ID
from app.domain.error_contract import build_layered_error
from app.domain.enums import EntityStatus
from app.integrations.phm_asset_mcp import PhmAssetMcpClient, PhmAssetMcpError
from app.integrations.phm_asset_mcp.errors import PhmAssetMcpTimeout
from app.orchestration.entity_lifecycle import build_scope_hint
from app.orchestration.entity_postrank import postrank_entity_result
from app.tools.alarm_context import entity_satisfies_required_level


@dataclass(slots=True)
class EntityLayerOutcome:
    updates: dict[str, Any]
    observation: dict[str, Any]


class UnifiedEntityResolutionLayer:
    """The only production entry point for entity lookup decisions.

    Every graph mode calls this layer once per user turn. The layer forwards the
    current question, branch entity, previous resolution and recent context to Asset
    MCP. Asset MCP owns recall and fuzzy policy; this layer only applies the
    workflow-aware deterministic contract (for example collapsing an incorrectly
    returned collection by explicit equipment ordinal and area) before routing.
    """

    def __init__(self, client: PhmAssetMcpClient) -> None:
        self.client = client

    @staticmethod
    def _has_explicit_current_identity(
        semantic_hints: dict[str, Any] | None, required_entity_level: str
    ) -> bool:
        """Whether Task Understanding found an explicit asset expression this turn.

        This is intentionally based on structured semantic provenance, never regexes
        over the user's wording.  Referential follow-ups use reference_target_level
        and therefore may reuse the previous active entity; explicit area/equipment/
        point phrases force Asset MCP to resolve the current turn independently.
        """

        hints = dict(semantic_hints or {})
        reference = str(hints.get("reference_target_level") or "none").strip().lower()
        if any(isinstance(hints.get(k),dict) and hints[k].get("raw_text") for k in ("equip_no","point_no")):
            return True
        # A referenced parent plus an explicit child is a scoped drill-down.
        if reference == "space" and not (hints.get("area") or {}).get("raw_text"):
            return False
        if reference == "equipment" and not any((hints.get(k) or {}).get("raw_text") for k in ("area","equipment")):
            return False
        if reference == "point" and not any((hints.get(k) or {}).get("raw_text") for k in ("area","equipment","point")):
            return False
        level = str(required_entity_level or "any").strip().lower()
        fields = {
            "space": ("area",),
            "area": ("area",),
            "equipment": ("area", "equipment"),
            "point": ("area", "equipment", "point"),
            "any": ("area", "equipment", "point"),
        }.get(level, ("area", "equipment", "point"))
        for field in fields:
            value = hints.get(field)
            if not isinstance(value, dict):
                continue
            if str(value.get("raw_text") or "").strip():
                return True
        return False

    @staticmethod
    def _conversation_context(state: dict[str, Any]) -> dict[str, Any]:
        memory = state.get("memory_context") if isinstance(state.get("memory_context"), dict) else {}
        recent = state.get("recent_messages") or memory.get("recent_messages") or []
        recent_entities = state.get("recent_entities") or memory.get("recent_entities") or []
        compact_recent: list[dict[str, Any]] = []
        for item in list(recent)[-4:]:
            if not isinstance(item, dict):
                continue
            compact_recent.append(
                {
                    "role": item.get("role"),
                    "content": str(item.get("content") or item.get("text") or "")[:1200],
                }
            )
        last_resolved = state.get("resolved_entity") or state.get("active_entity") or {}
        return {
            "attachment_texts": [str(x.get("extracted_text") or "")[:10000]
                                 for x in state.get("understanding_results") or [] if isinstance(x, dict)],
            "summary": str(memory.get("summary") or "")[:2400],
            "recent_messages": compact_recent,
            "recent_entities": [
                dict(item)
                for item in list(recent_entities)[-12:]
                if isinstance(item, dict)
            ],
            "last_resolved_entity": (
                dict(last_resolved) if isinstance(last_resolved, dict) else {}
            ),
            "pending_clarification": (
                dict(memory.get("pending_clarification") or {})
                if isinstance(memory.get("pending_clarification"), dict)
                else {}
            ),
        }

    async def resolve(
        self,
        state: dict[str, Any],
        *,
        required_entity_level: str = "any",
        force_refresh: bool = False,
    ) -> EntityLayerOutcome:
        selected_entity = state.get("selected_entity") if isinstance(state.get("selected_entity"), dict) else {}
        active = selected_entity or state.get("active_entity") or {}
        query = str(state.get("query") or "")
        business_intent = (
            state.get("business_intent")
            if isinstance(state.get("business_intent"), dict)
            else {}
        )
        semantic_hints_all = (
            business_intent.get("asset_semantics")
            if isinstance(business_intent.get("asset_semantics"), dict)
            else {}
        )
        explicit_current_identity = bool(
            not selected_entity
            and self._has_explicit_current_identity(
                semantic_hints_all, required_entity_level
            )
        )
        from app.services.sensor_references import code_tokens
        if code_tokens(query):
            explicit_current_identity = True
            active = {}
        # Keep a wider candidate window for equipment/point workflows.  Candidate
        # breadth is a retrieval concern and must not depend on regex-parsing the
        # operator's wording.
        lookup_limit = 30 if required_entity_level in {"equipment", "point"} else 10

        async def lookup(*, refresh: bool, allow_reuse: bool):
            semantic_hints = business_intent.get("asset_semantics")
            # resolve_entity owns exactly one identity decision.  Descendant/collection
            # semantics belong to query_scope_collection and must never influence the
            # root lookup.  This separation is especially important on a clarification
            # turn such as query="总部钢铁" while the preserved collection category is
            # equipment_type="水泵": provenance for "水泵" belongs to the collection
            # intent, not to the root-space identity request.
            if isinstance(semantic_hints, dict):
                identity_fields_by_level = {
                    "space": {"area"},
                    "area": {"area"},
                    "line": {"area"},
                    "equipment": {"area", "equipment", "equipment_type", "equip_no"},
                    "point": {
                        "area", "equipment", "equipment_type", "point",
                        "component", "position", "direction", "measurement", "equip_no", "point_no",
                    },
                    "any": {
                        "area", "equipment", "equipment_type", "point",
                        "component", "position", "direction", "measurement", "equip_no", "point_no",
                    },
                }
                identity_fields = identity_fields_by_level.get(
                    required_entity_level, identity_fields_by_level["any"]
                )
                control_fields = {
                    "reference_target_level", "needs_asset_lookup", "refresh_requested"
                }
                semantic_hints = {
                    key: value
                    for key, value in semantic_hints.items()
                    if key in identity_fields or key in control_fields
                }
            return await self.client.lookup(
                query=query,
                required_entity_level=(required_entity_level if required_entity_level != "none" else "any"),
                active_entity=(
                    None
                    if explicit_current_identity
                    else (active if isinstance(active, dict) and active else None)
                ),
                user_profile=(state.get("user_profile") if isinstance(state.get("user_profile"), dict) else None),
                previous_resolution=(state.get("entity_result") if isinstance(state.get("entity_result"), dict) else None),
                conversation_context=self._conversation_context(state),
                force_refresh=refresh,
                allow_context_reuse=(allow_reuse and not explicit_current_identity),
                request_id=str(state.get("task_id") or ""),
                limit=lookup_limit,
                semantic_hints=(
                    semantic_hints if isinstance(semantic_hints, dict) else None
                ),
            )
        attempts = 0
        upstream_failures: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + float(getattr(self.client, "timeout_seconds", 60.0))

        async def budgeted_lookup():
            try:
                # Both attempts share the original configured timeout budget.
                async with asyncio.timeout_at(deadline):
                    return await lookup(refresh=force_refresh, allow_reuse=not force_refresh)
            except TimeoutError as exc:
                raise PhmAssetMcpTimeout("resolve_entity") from exc

        try:
            # Retry only an explicitly transient service/transport failure, once.
            # No matching result is retried or promoted into a fabricated identity.
            for attempt in range(1, 3):
                attempts = attempt
                try:
                    raw_result = await budgeted_lookup()
                    break
                except PhmAssetMcpError as exc:
                    upstream_failures.append({"attempt": attempt, "code": exc.code,
                                              "operator_message": exc.message,
                                              "retryable": exc.retryable, "details": exc.details})
                    if not exc.retryable or attempt == 2 or loop.time() >= deadline:
                        raise
            tokens = code_tokens(query)
            if tokens and required_entity_level not in {"space", "area", "line"}:
                wanted = {t.casefold() for t in tokens}
                candidates = raw_result.matches or ([raw_result.resolved_entity] if raw_result.resolved_entity else [])
                def compatible_code(row):
                    merged = {**(row.get("metadata") or {}), **row}
                    return wanted.issubset({str(merged.get(k) or "").casefold() for k in ("equip_no", "point_no")})
                filtered = [r for r in candidates if compatible_code(r)]
                if len(filtered) != len(candidates):
                    raw_result = raw_result.model_copy(update={"matches": filtered, "match_count": len(filtered),
                        "status": EntityStatus.MULTIPLE if len(filtered) > 1 else EntityStatus.UNIQUE if filtered else EntityStatus.NOT_FOUND,
                        "need_disambiguation": len(filtered) > 1, "resolved_entity": filtered[0] if len(filtered) == 1 else None,
                        "query_scope": {}, "resolution_source": "literal_code_constraint_guard",
                        "message": "已按原始编码核验候选。" if filtered else "没有候选与原问题的编码一致，未使用其他设备替代。"})
            if raw_result.status == EntityStatus.UNIQUE and not entity_satisfies_required_level(
                raw_result.resolved_entity, required_entity_level
            ):
                raise PhmAssetMcpError("PHM_ASSET_MCP_INVALID_RESULT",
                    f"resolve_entity: returned identity does not satisfy {required_entity_level}")
            result = postrank_entity_result(
                raw_result,
                query=query,
                required_level=required_entity_level,
            )
            if result.status == EntityStatus.UNIQUE and not entity_satisfies_required_level(
                result.resolved_entity, required_entity_level
            ):
                raise PhmAssetMcpError("PHM_ASSET_MCP_INVALID_RESULT",
                    f"resolve_entity: returned identity does not satisfy {required_entity_level}")
            if result.status in {EntityStatus.ERROR, EntityStatus.NO_LOOKUP} and required_entity_level != "none":
                raise PhmAssetMcpError("PHM_ASSET_MCP_INVALID_RESULT",
                    f"resolve_entity: required identity lookup returned {result.status}")
            # Root resolution asks for one anchor identity.  If Asset MCP returns a
            # collection while the required root is a space, those are candidates for
            # disambiguation, not a descendant collection.  Descendant collections are
            # queried later through query_scope_collection after the root is unique.
            if (
                str(required_entity_level or "").lower() in {"area", "space"}
                and result.status == EntityStatus.COLLECTION
                and result.matches
            ):
                result = result.model_copy(
                    update={
                        "status": EntityStatus.MULTIPLE,
                        "need_disambiguation": True,
                        "resolved_entity": None,
                        "return_mode": "candidates",
                        "message": result.message or "找到多个同名区域，请选择需要查询的具体区域。",
                    }
                )
        except Exception as exc:
            code = (
                exc.code
                if isinstance(exc, PhmAssetMcpError)
                else "ENTITY_RESOLUTION_INTERNAL"
            )
            operator_message = (
                exc.message
                if isinstance(exc, PhmAssetMcpError)
                else f"{type(exc).__name__}: {exc}"
            )
            error = build_layered_error(
                code=code,
                operator_message=operator_message,
                component="统一实体检索层",
                public_message="设备与区域检索服务暂时未能完成查询，因此目前无法确认目标设备或读取其运行状态。请稍后重试。",
                workflow_id=(state.get("business_workflow") or {}).get("workflow_id") if isinstance(state.get("business_workflow"), dict) else None,
                step_id="entity_resolution",
                request_id=str(state.get("task_id") or ""),
                retryable=bool(isinstance(exc, PhmAssetMcpError) and exc.retryable),
                upstream_failures=upstream_failures,
            )
            entity_result = {
                "status": EntityStatus.ERROR.value,
                "need_lookup": required_entity_level != "none",
                "need_disambiguation": False,
                "matches": [],
                "match_count": 0,
                "resolved_entity": None,
                "message": error.public_message,
                "decision": {
                    "action": "ERROR",
                    "target_entity_level": required_entity_level,
                    "reason": error.public_message,
                    "lookup_attempts": attempts,
                },
                "error": error.model_dump(mode="json"),
            }
            lifecycle = {
                "action": "none",
                "source": "phm_asset_mcp_error",
                "anchor_entity": {},
                "scope_hint": build_scope_hint(active if isinstance(active, dict) else {}),
                "reason": error.public_message,
                "confidence": 0.0,
                "policy_action": "ERROR",
            }
            observation = {
                "call_id": "entity_resolution",
                "call_type": "agent",
                "agent_id": FUZZY_ENTITY_AGENT_ID,
                "target_id": FUZZY_ENTITY_AGENT_ID,
                "objective": "统一判断本轮是否需要资产实体检索并完成解析",
                "status": "FAILED",
                "answer_markdown": error.public_message,
                "error_code": error.code,
                "error_message": error.public_message,
                "operator_error": error.model_dump(mode="json"),
                "can_support_final_answer": False,
            }
            return EntityLayerOutcome(
                updates={
                    "entity_resolution": lifecycle,
                    "entity_result": entity_result,
                    "resolved_entity": {},
                    "query_scope": {},
                    "entity_constraints": {},
                    "invalidate_active_entity": bool(explicit_current_identity),
                    "explicit_current_identity": explicit_current_identity,
                    "errors": [error.model_dump(mode="json")],
                },
                observation=observation,
            )

        payload = result.model_dump(mode="json")
        payload.setdefault("decision", {})["lookup_attempts"] = attempts
        if upstream_failures:
            payload["lookup_recovery"] = {"attempts": attempts, "upstream_failures": upstream_failures}
        policy = dict(result.decision or {})
        policy_action = str(policy.get("action") or "SKIP").upper()
        lifecycle_action = {
            "REUSE": "reuse",
            "SKIP": "none",
            "SEARCH": "replace",
            "REFRESH": "replace",
            "DOWN_DRILL": "replace",
        }.get(policy_action, "none")
        resolved = dict(result.resolved_entity or {})
        lifecycle = {
            "action": lifecycle_action,
            "source": "phm_asset_mcp",
            "anchor_entity": resolved if lifecycle_action == "reuse" else {},
            "scope_hint": build_scope_hint(active if isinstance(active, dict) else {}),
            "reason": str(policy.get("reason") or result.message or ""),
            "confidence": result.top_similarity,
            "policy_action": policy_action,
            "target_entity_level": policy.get("target_entity_level"),
            "return_mode": result.return_mode,
            "query_fingerprint": result.query_fingerprint,
        }
        observation = {
            "call_id": "entity_resolution",
            "call_type": "agent",
            "agent_id": FUZZY_ENTITY_AGENT_ID,
            "target_id": FUZZY_ENTITY_AGENT_ID,
            "objective": "统一判断本轮是否需要资产实体检索并完成解析",
            "status": "COMPLETED" if result.status != EntityStatus.ERROR else "FAILED",
            "answer_markdown": result.message or "实体检索决策已完成。",
            "evidence": [{"source_type": "phm_asset_mcp", "result": payload}],
            "can_support_final_answer": result.status in {EntityStatus.NO_LOOKUP, EntityStatus.UNIQUE, EntityStatus.COLLECTION},
            "state_updates": {
                "resolved_entity": resolved,
                "entity_result": payload,
                "query_scope": dict(result.query_scope or {}),
                "entity_constraints": dict(result.entity_constraints or {}),
            },
        }
        return EntityLayerOutcome(
            updates={
                "entity_resolution": lifecycle,
                "entity_result": payload,
                "resolved_entity": resolved,
                "query_scope": dict(result.query_scope or {}),
                "entity_constraints": dict(result.entity_constraints or {}),
                "invalidate_active_entity": bool(
                    explicit_current_identity and not resolved
                ),
                "explicit_current_identity": explicit_current_identity,
            },
            observation=observation,
        )
