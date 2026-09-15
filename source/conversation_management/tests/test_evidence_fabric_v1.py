from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.context.hot_window import reorder_hot_topics
from app.evidence.context_budget import ContextBudgetManager
from app.evidence.matching import requirement_entry_satisfies
from app.planning.task_compiler import TaskCompiler


def _entry(*, equip_no="BB1", authority="AUTHORITATIVE", partial=False, valid_until=None):
    return {
        "evidence_id": str(uuid4()),
        "semantic_type": "health_score",
        "authority": authority,
        "subject": {"equip_no": equip_no},
        "scope": {},
        "content_descriptor": {"fields": ["health_score", "grade"]},
        "summary": {"health_score": 99.1, "grade": "优秀"},
        "completeness": {"status": "partial" if partial else "complete"},
        "freshness": {
            "immutable": False,
            "freshness_class": "dynamic",
            "valid_until": valid_until,
        },
    }


def test_hot_window_is_mru_and_continue_current_does_not_rotate():
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    assert reorder_hot_topics([a, b, c], a) == [a, b, c]
    assert reorder_hot_topics([a, b, c], d) == [d, a, b]
    assert reorder_hot_topics([a, b, c], b) == [b, a, c]


def test_requirement_checks_subject_authority_freshness_and_completeness():
    req = {
        "semantic_type": "health_score",
        "acceptable_semantic_types": ["health_score", "health_score_set"],
        "subject_constraint": {"equip_no": "BB1"},
        "scope_constraint": {},
        "required_authority": ["AUTHORITATIVE"],
        "required_fields": ["health_score", "grade"],
        "freshness": {"mode": "reuse_if_valid"},
        "completeness": {"allow_partial": False},
    }
    assert requirement_entry_satisfies(_entry(), req)[0]
    assert requirement_entry_satisfies(_entry(equip_no="BB2"), req) == (False, "EVIDENCE_SUBJECT_MISMATCH")
    assert requirement_entry_satisfies(_entry(authority="MODEL_INFERRED"), req) == (False, "EVIDENCE_AUTHORITY_INSUFFICIENT")
    assert requirement_entry_satisfies(_entry(partial=True), req) == (False, "EVIDENCE_PARTIAL")
    stale = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert requirement_entry_satisfies(_entry(valid_until=stale), req) == (False, "EVIDENCE_STALE")


def test_task_compiler_preserves_stable_subject_and_accepts_semantic_family():
    intent = {
        "goal_frame": {"evidence_types": ["health"]},
        "completion_contract": {"allow_partial": False, "required_fields": ["health_score"]},
        "query_plan": {"domain": "health"},
    }
    [req] = TaskCompiler().compile(intent, subject_constraint={"equip_no": "BB1", "name": "13号轧机"})
    assert req.family == "health"
    assert req.subject_constraint == {"equip_no": "BB1"}
    assert req.acceptable_semantic_types == ["health_score", "health_score_set"]
    assert req.required_authority == ["AUTHORITATIVE"]
    assert req.completeness["allow_partial"] is False


def test_context_budget_never_opaque_slices_large_lists():
    manager = ContextBudgetManager(max_chars=12000)
    raw = {"evidence_catalog": [{"id": i, "text": "x" * 2000} for i in range(200)]}
    compiled = json.loads(manager.compile(raw))
    catalog = compiled["evidence_catalog"]
    assert isinstance(catalog, dict)
    assert catalog["total_count"] == 200
    assert catalog["returned_count"] < 200
    assert catalog["truncated"] is True
    assert catalog["has_more"] is True
