"""Turn-completion contract and deterministic evidence/result checks.

This module does not route by user wording.  Task Understanding defines the contract;
the evaluator checks that contract against authoritative observations and persisted
answer-result structures before the normal graph is allowed to finish.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any
from app.evidence.matching import catalog_status, requirements_status

from app.tools.phm_asset_mcp import PHM_ASSET_TOOL_IDS
from app.tools.phm_data_mcp import (
    PHM_GET_DEVICE_DATA_TOOL_ID,
    PHM_GET_FEATURE_TREND_TOOL_ID,
    PHM_GET_TEMPERATURE_TREND_TOOL_ID,
    PHM_GET_WAVEFORM_TOOL_ID,
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
)
from app.tools.phm_diagnosis_mcp import (
    PHM_DIAGNOSIS_DEVICE_TOOL_ID,
    PHM_DIAGNOSIS_POINT_TOOL_ID,
)
from app.tools.phm_sensor_mcp import PHM_SENSOR_TOOL_IDS, SENSOR_TOOL_BY_OPERATION
from app.tools.phm_workflow_tools import (
    PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
)

STATUS_COMPLETE = "COMPLETE"
STATUS_NEED_MORE_EVIDENCE = "NEED_MORE_EVIDENCE"
STATUS_PRESENTATION_ONLY = "PRESENTATION_ONLY"
STATUS_PARTIAL_FINAL = "PARTIAL_FINAL"

_LIST_ACTIONS = {"list", "count", "detail", "enrich", "filter", "sort", "rank", "compare", "render", "aggregate"}
_ANALYSIS_ACTIONS = {"explain", "analyze", "analyse", "diagnose", "assess", "interpret"}


def normalize_completion_contract(classification: dict[str, Any]) -> dict[str, Any]:
    """Fill conservative defaults without reinterpreting natural language.

    Explicit model output always wins.  Defaults come only from already-structured
    fields such as goal_frame/result_followup/query_plan, so this remains a control
    plane normalization rather than a second keyword router.
    """
    value = deepcopy(classification)
    goal = value.get("goal_frame") if isinstance(value.get("goal_frame"), dict) else {}
    follow = value.get("result_followup") if isinstance(value.get("result_followup"), dict) else {}
    plan = value.get("query_plan") if isinstance(value.get("query_plan"), dict) else {}
    contract = value.get("completion_contract") if isinstance(value.get("completion_contract"), dict) else {}
    contract = dict(contract)

    actions = [str(x).strip().lower() for x in contract.get("actions") or [] if str(x).strip()]
    if not actions:
        actions = [str(x).strip().lower() for x in goal.get("operations") or [] if str(x).strip()]
    if follow.get("active") and str(follow.get("action") or "").strip():
        action = str(follow["action"]).strip().lower()
        if action not in actions:
            actions.append(action)
    contract["actions"] = list(dict.fromkeys(actions))[:12]

    target = str(contract.get("target_granularity") or "none").strip().lower()
    if target in {"", "none"}:
        target = str(goal.get("target_entity_level") or plan.get("target") or "none").strip().lower()
    if target == "area":
        target = "space"
    if target not in {"none", "space", "equipment", "point", "record"}:
        target = "none"
    contract["target_granularity"] = target

    fields = [str(x).strip() for x in contract.get("required_fields") or [] if str(x).strip()]
    for field in follow.get("fields") or []:
        field = str(field).strip()
        if field and field not in fields:
            fields.append(field)
    for field in plan.get("include_fields") or []:
        field = str(field).strip()
        if field and field not in fields:
            fields.append(field)
    # A list of concrete members needs a stable identity even when the older model
    # did not yet emit completion_contract. Human-readable names are requested by the
    # model through required_fields; the fallback only guarantees an authoritative ID.
    if any(a in _LIST_ACTIONS for a in contract["actions"]):
        fallback_id = {"point": "point_no", "equipment": "equip_no", "space": "space_id"}.get(target)
        if fallback_id and fallback_id not in fields:
            fields.append(fallback_id)
    contract["required_fields"] = fields[:24]

    result_source = str(contract.get("result_source") or "auto").strip().lower()
    collection_action = str(contract.get("collection_action") or "none").strip().lower()
    if follow.get("active"):
        if result_source == "auto":
            result_source = "previous_all" if follow.get("selection") == "all" else "previous_displayed"
        if collection_action == "none":
            collection_action = {
                "enrich": "preserve",
                "render": "preserve",
                "verify": "preserve",
                "refresh_values": "refresh",
                "refresh_query": "requery",
            }.get(str(follow.get("action") or ""), "preserve")
        contract["preserve_members"] = collection_action == "preserve"
        contract["preserve_order"] = collection_action == "preserve"
    contract["result_source"] = result_source if result_source in {"auto", "new_query", "previous_displayed", "previous_all", "context"} else "auto"
    contract["collection_action"] = collection_action if collection_action in {"none", "new", "preserve", "refine", "refresh", "requery"} else "none"

    output = str(contract.get("output_type") or "answer").strip().lower()
    goal_output = str(goal.get("output_type") or "").strip().lower()
    if output == "answer" and goal_output in {"list", "table", "prose", "report", "count", "analysis"}:
        output = goal_output
    if plan.get("operation") == "count":
        output = "count"
    contract["output_type"] = output if output in {"answer", "list", "table", "prose", "report", "count", "analysis"} else "answer"

    result_scope = str(contract.get("result_scope") or "unspecified").strip().lower()
    if result_scope == "unspecified" and any(a in _LIST_ACTIONS for a in contract["actions"]):
        result_scope = "all_returned"
    contract["result_scope"] = result_scope if result_scope in {"unspecified", "all_returned", "all_verified", "top_n", "current_page", "sample"} else "unspecified"

    response_mode = str(contract.get("response_mode") or "auto").strip().lower()
    if response_mode == "auto":
        if contract["output_type"] in {"list", "table", "count"} and not any(a in _ANALYSIS_ACTIONS for a in contract["actions"]):
            response_mode = "facts_only"
        elif any(a in _ANALYSIS_ACTIONS for a in contract["actions"]):
            response_mode = "analysis"
    contract["response_mode"] = response_mode if response_mode in {"auto", "facts_only", "facts_plus_explanation", "analysis"} else "auto"
    contract["allow_partial"] = bool(contract.get("allow_partial", True))
    contract["preserve_members"] = bool(contract.get("preserve_members", False))
    contract["preserve_order"] = bool(contract.get("preserve_order", False))
    value["completion_contract"] = contract
    return value


def _tool_status(observations: list[dict[str, Any]], tool_ids: set[str]) -> bool:
    return any(
        str(item.get("tool_id") or "") in tool_ids
        and str(item.get("status") or "").upper() in {"SUCCESS", "COMPLETED"}
        for item in observations
        if isinstance(item, dict)
    )


def evidence_status(state: dict[str, Any]) -> tuple[list[str], dict[str, bool]]:
    intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
    goal = intent.get("goal_frame") if isinstance(intent.get("goal_frame"), dict) else {}
    required = [str(x).strip().lower() for x in goal.get("evidence_types") or [] if str(x).strip()]
    observations = [x for x in state.get("observations") or [] if isinstance(x, dict)]
    mapping = {
        "alarm": {PHM_QUERY_ALARM_RECORDS_TOOL_ID},
        "health": {PHM_QUERY_HEALTH_SCORE_TOOL_ID, PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID},
        "vibration": {PHM_GET_DEVICE_DATA_TOOL_ID, PHM_GET_DATA_SNAPSHOT_TOOL_ID, PHM_GET_WAVEFORM_TOOL_ID, PHM_GET_FEATURE_TREND_TOOL_ID},
        "temperature": {PHM_GET_TEMPERATURE_TREND_TOOL_ID, PHM_GET_DEVICE_DATA_TOOL_ID},
        "diagnosis_result": {PHM_DIAGNOSIS_POINT_TOOL_ID, PHM_DIAGNOSIS_DEVICE_TOOL_ID, PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID},
        "asset": set(PHM_ASSET_TOOL_IDS),
        "sensor": set(PHM_SENSOR_TOOL_IDS),
        "sensor_active_faults": {SENSOR_TOOL_BY_OPERATION[k] for k in ("active", "overview")},
        "sensor_fault_history": {SENSOR_TOOL_BY_OPERATION["history"]},
        "sensor_points": {SENSOR_TOOL_BY_OPERATION["points"]},
        "sensor_monitoring": set(SENSOR_TOOL_BY_OPERATION.values()),
        "sensor_offline": {SENSOR_TOOL_BY_OPERATION[k] for k in ("offline", "overview")},
    }
    latest = next((x for x in reversed(state.get("answer_results") or []) if isinstance(x, dict)), None)
    plan = latest.get("plan") if isinstance(latest, dict) and isinstance(latest.get("plan"), dict) else {}
    result_domain = str(plan.get("domain") or "").strip().lower()
    sensor_operation = str(plan.get("sensor_operation") or "").strip().lower()
    result_evidence = {
        "asset": result_domain == "asset",
        "health": result_domain == "health",
        "alarm": result_domain == "alarm",
        "sensor": result_domain == "sensor",
        "sensor_active_faults": result_domain == "sensor" and sensor_operation in {"active", "overview"},
        "sensor_fault_history": result_domain == "sensor" and sensor_operation == "history",
        "sensor_points": result_domain == "sensor" and sensor_operation == "points",
        "sensor_monitoring": result_domain == "sensor" and sensor_operation in set(SENSOR_TOOL_BY_OPERATION),
        "sensor_offline": result_domain == "sensor" and sensor_operation in {"offline", "overview"},
    }
    catalog_ready, _catalog_detail = catalog_status(state, required)
    status: dict[str, bool] = {}
    for evidence in required:
        if evidence == "conversation_context":
            status[evidence] = bool(state.get("recent_messages") or state.get("memory_context"))
        elif evidence == "file":
            status[evidence] = bool(state.get("understanding_results")) or bool(catalog_ready.get(evidence))
        elif evidence in mapping:
            status[evidence] = (
                _tool_status(observations, mapping[evidence])
                or bool(result_evidence.get(evidence))
                or bool(catalog_ready.get(evidence))
            )
        else:
            status[evidence] = bool(result_evidence.get(evidence)) or bool(catalog_ready.get(evidence))
    return required, status


def _latest_result(state: dict[str, Any]) -> dict[str, Any] | None:
    rows = [x for x in state.get("answer_results") or [] if isinstance(x, dict)]
    return rows[-1] if rows else None


def _field_missing(rows: list[dict[str, Any]], field: str) -> list[int]:
    missing = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get(field) in (None, "", [], {}):
            missing.append(index)
    return missing


def evaluate_completion(state: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether real evidence/content satisfies this turn's contract."""
    intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
    normalized = normalize_completion_contract(intent)
    contract = normalized.get("completion_contract") or {}
    required_evidence, _combined_legacy = evidence_status({**state, "business_intent": normalized})
    _catalog_ready, catalog_detail = catalog_status({**state, "business_intent": normalized}, required_evidence)
    requirement_ready, requirement_detail = requirements_status({**state, "business_intent": normalized})
    # Separate current-run observations/results from persisted Catalog.  Otherwise an
    # old Evidence with the right semantic_type but the wrong subject could satisfy the
    # legacy family check before EvidenceRequirement gets a chance to reject it.
    memory_without_catalog = dict(state.get("memory_context") or {})
    memory_without_catalog["evidence_catalog"] = []
    _, current_ready = evidence_status({
        **state,
        "business_intent": normalized,
        "evidence_catalog": [],
        "memory_context": memory_without_catalog,
    })
    has_requirements = bool(state.get("evidence_requirements"))
    evidence: dict[str, bool] = {}
    for name in required_evidence:
        persisted_ok = requirement_ready.get(name) if has_requirements and name in requirement_ready else _catalog_ready.get(name, False)
        evidence[name] = bool(current_ready.get(name) or persisted_ok)
    missing_evidence = [name for name in required_evidence if not evidence.get(name)]

    result = _latest_result(state)
    required_fields = list(contract.get("required_fields") or [])
    actions = set(contract.get("actions") or [])
    needs_result = bool(required_fields or actions & _LIST_ACTIONS or contract.get("output_type") in {"list", "table", "count"})
    missing_fields: dict[str, list[int]] = {}
    missing_result = False
    limitations: list[str] = []

    if needs_result:
        if result is None:
            # Some non-collection domain tools have not yet been adapted to AnswerResult.
            # Do not invalidate a successful analytical task solely for lacking the
            # persistence adapter; field/list contracts do require it.
            if required_fields or contract.get("output_type") in {"list", "table", "count"}:
                missing_result = True
        else:
            rows = [x for x in result.get("rows") or [] if isinstance(x, dict)]
            total = result.get("total_count")
            enrichment = result.get("identity_enrichment") if isinstance(result.get("identity_enrichment"), dict) else {}
            identity_enrichment_attempted = bool(enrichment.get("attempted"))
            identity_fields = {"point_name", "equip_name", "area"}
            for field in required_fields:
                indexes = _field_missing(rows, field)
                if indexes:
                    # Empty valid sets do not have row-level missing fields.
                    if rows or total not in {0, "0"}:
                        if identity_enrichment_attempted and field in identity_fields:
                            limitations.append(
                                f"资产目录已按真实编码补查，但仍有 {len(indexes)} 个成员缺少 {field}"
                            )
                        else:
                            missing_fields[field] = indexes
            if result.get("display_complete") is False:
                limitations.append("当前保存/展示结果不是本次已取得结果的完整成员集合")
            if result.get("source_complete") is False:
                limitations.append("上游明确声明结果不完整或已截断")
            elif result.get("source_complete") is None and contract.get("result_scope") == "all_verified":
                limitations.append("上游未提供可核验的完整性声明")

    gaps = []
    for name in missing_evidence:
        detail = requirement_detail.get(name) or catalog_detail.get(name) or {}
        gaps.append({
            "type": "missing_evidence",
            "evidence": name,
            "semantic_type": detail.get("semantic_type"),
            "reason": detail.get("failure") or "EVIDENCE_NOT_FOUND",
        })
    if missing_result:
        gaps.append({"type": "missing_result_set", "target_granularity": contract.get("target_granularity")})
    for field, indexes in missing_fields.items():
        gaps.append({"type": "missing_field", "field": field, "row_indexes": indexes[:100]})

    if gaps:
        status = STATUS_NEED_MORE_EVIDENCE
    elif limitations:
        status = STATUS_PARTIAL_FINAL
    elif contract.get("response_mode") == "facts_only" and result is not None and not state.get("query_fact_text"):
        status = STATUS_PRESENTATION_ONLY
    else:
        status = STATUS_COMPLETE

    return {
        "status": status,
        "contract": contract,
        "evidence_status": evidence,
        "evidence_requirement_status": requirement_detail or catalog_detail,
        "missing_evidence": missing_evidence,
        "missing_fields": missing_fields,
        "gaps": gaps,
        "limitations": limitations,
        "result_id": result.get("result_id") if result else None,
        "answerable_with_limitations": bool(status == STATUS_PARTIAL_FINAL and contract.get("allow_partial", True)),
    }
