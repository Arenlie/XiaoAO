from app.orchestration.completion import (
    STATUS_COMPLETE,
    STATUS_NEED_MORE_EVIDENCE,
    STATUS_PARTIAL_FINAL,
    STATUS_PRESENTATION_ONLY,
    evaluate_completion,
    normalize_completion_contract,
)
from app.services.query_results import render_result, sensor_result
from app.tools.phm_sensor_mcp import SENSOR_TOOL_BY_OPERATION


def sensor_intent(*, required_fields=None, result_scope="all_returned", facts=True):
    return {
        "goal_frame": {
            "goal": "列出当前温度异常的具体测点",
            "operations": ["list"],
            "output_type": "list",
            "target_entity_level": "point",
            "evidence_types": ["sensor_active_faults"],
        },
        "sensor_query": {"operation": "active", "scope": "global", "fault_type": "温度异常"},
        "completion_contract": {
            "actions": ["list"],
            "target_granularity": "point",
            "required_fields": required_fields or ["point_no", "point_name"],
            "result_scope": result_scope,
            "output_type": "list",
            "response_mode": "facts_only" if facts else "auto",
            "allow_partial": True,
        },
    }


def raw_sensor(names=True, enrichment=None, *, complete=None):
    records = []
    for index in range(1, 7):
        records.append({
            "equip_num": "E-13",
            "equip_name": "13号轧机",
            "point_num": f"P-{index}",
            "point_name": f"测点{index}" if names else None,
            "model_name": "温度异常",
            "fault_status_name": "待确认",
        })
    return {
        "records": records,
        "summary": {
            "matched_count_in_fetched": 6,
            "returned_count": 6,
            "unique_point_count_in_matched": 6,
            "unique_equipment_count_in_matched": 1,
        },
        "source": {
            "complete": complete,
            "output_limited": False,
            "truncated_possible": False,
            "asset_identity_enrichment": enrichment or {},
        },
        "filters": {"fault_type": "温度异常"},
        "warnings": [],
    }


def success_observation():
    return {"tool_id": SENSOR_TOOL_BY_OPERATION["active"], "status": "SUCCESS"}


def test_completion_contract_defaults_from_structured_goal_without_text_routing():
    normalized = normalize_completion_contract({
        "goal_frame": {
            "operations": ["list"],
            "target_entity_level": "point",
            "output_type": "list",
        },
        "completion_contract": {},
    })
    contract = normalized["completion_contract"]
    assert contract["actions"] == ["list"]
    assert contract["target_granularity"] == "point"
    assert contract["required_fields"] == ["point_no"]
    assert contract["result_scope"] == "all_returned"
    assert contract["response_mode"] == "facts_only"


def test_tool_success_is_not_completion_when_required_member_field_is_missing():
    intent = sensor_intent()
    result = sensor_result(raw_sensor(names=False), intent)
    evaluation = evaluate_completion({
        "business_intent": intent,
        "observations": [success_observation()],
        "answer_results": [result],
    })
    assert evaluation["status"] == STATUS_NEED_MORE_EVIDENCE
    assert evaluation["missing_fields"]["point_name"] == [1, 2, 3, 4, 5, 6]


def test_sensor_identity_enrichment_attempted_turns_unavailable_name_into_partial_not_requery_loop():
    intent = sensor_intent()
    result = sensor_result(raw_sensor(names=False, enrichment={"attempted": True, "missing_count": 6}), intent)
    state = {
        "business_intent": intent,
        "observations": [success_observation()],
        "answer_results": [result],
        "query_fact_text": render_result(result),
    }
    evaluation = evaluate_completion(state)
    assert evaluation["status"] == STATUS_PARTIAL_FINAL
    assert not evaluation["missing_fields"]
    assert any("资产目录已按真实编码补查" in item for item in evaluation["limitations"])


def test_six_named_sensor_members_are_complete_and_rendered_deterministically():
    intent = sensor_intent()
    result = sensor_result(raw_sensor(names=True, enrichment={"attempted": True, "enriched_count": 6}), intent)
    state = {
        "business_intent": intent,
        "observations": [success_observation()],
        "answer_results": [result],
    }
    evaluation = evaluate_completion(state)
    assert evaluation["status"] == STATUS_PRESENTATION_ONLY
    text = render_result(result)
    for index in range(1, 7):
        assert f"P-{index}" in text
        assert f"测点{index}" in text
    assert text.count("\n") >= 5
    state["query_fact_text"] = text
    assert evaluate_completion(state)["status"] == STATUS_COMPLETE


def test_unknown_upstream_total_is_allowed_for_all_returned_but_not_all_verified():
    intent = sensor_intent(required_fields=["point_no"], result_scope="all_returned")
    result = sensor_result(raw_sensor(names=True, complete=None), intent)
    state = {"business_intent": intent, "observations": [success_observation()], "answer_results": [result], "query_fact_text": "ok"}
    assert evaluate_completion(state)["status"] == STATUS_COMPLETE

    verified = sensor_intent(required_fields=["point_no"], result_scope="all_verified")
    state["business_intent"] = verified
    state["answer_results"] = [sensor_result(raw_sensor(names=True, complete=None), verified)]
    assert evaluate_completion(state)["status"] == STATUS_PARTIAL_FINAL
