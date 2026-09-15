"""Private collection references and customer-facing factual output."""
from __future__ import annotations

import hashlib
import hmac
from uuid import UUID, uuid4

from sqlalchemy import text

from app.asset_query_contract import AssetQuery, QueryError, TOOL_ID, sign_context, signature, normalize_tree
from app.asset_collection_scope import is_unscoped_new_asset_collection

GROUP_NAMES = {"equipment_class": "设备类别", "equipment_subclass": "设备种类", "purpose": "用途",
    "structure_type": "结构类型", "model": "型号", "company": "企业", "plant": "厂区", "line": "产线", "area": "区域"}


def collection_intent(state):
    value = (state.get("business_intent") or {}).get("asset_query") or {}
    return value if value.get("active") is True else {}


def recent_collections(state):
    return [v for v in (state.get("memory_context") or {}).get("recent_asset_queries", []) if isinstance(v, dict)]


def resolve_reference(state, intent):
    available = recent_collections(state)
    source = intent.get("source_message_id")
    candidates = [v for v in available if not source or v.get("source_message_id") == source]
    if not candidates:
        raise QueryError("QUERY_REFERENCE_MISSING", "当前对话中没有可复用的设备集合。可以重新查询该区域的设备。")
    if not source:
        latest_message = candidates[-1]["source_message_id"]
        candidates = [v for v in candidates if v["source_message_id"] == latest_message]
    if len({v["query_id"] for v in candidates}) > 1:
        raise QueryError("QUERY_REFERENCE_AMBIGUOUS", "这条历史回复包含多个设备集合，请明确要继续分析哪一组设备。")
    # Nearest actual ancestor, not a process-global or user-global last-result key.
    return candidates[-1]


def build_query(state):
    from app.tools.phm_asset_context import resolve_asset_identity
    intent = collection_intent(state)
    if not intent:
        raise QueryError("QUERY_INTENT_REQUIRED", "尚未明确本次资产查询的筛选方式")
    mode = intent.get("reference_mode", "new")
    query = {"operation": intent.get("operation", "list"), "group_by": intent.get("group_by"),
             "page_size": intent.get("page_size", 1000), "predicate": intent.get("predicate")}
    if mode != "new":
        ref = resolve_reference(state, intent)
        query["reference"] = {"query_id": ref["query_id"], "mode": mode}
        query["freshness"] = "referenced_snapshot" if mode in {"same_set", "refine_set"} else "current"
        if mode == "same_set":
            if query["predicate"]:
                raise QueryError("QUERY_REFERENCE_CONFLICT", "复用原集合与增加筛选条件不能同时执行，请明确本次要查询的范围")
    else:
        semantics = (state.get("business_intent") or {}).get("asset_semantics") or {}
        if is_unscoped_new_asset_collection(state):
            # Server-owned global collection scope.  Category/name predicates remain
            # predicates and are never converted into a singular equipment identity.
            query["scope"] = {"scope_type": "shared_catalog"}
        else:
            root = resolve_asset_identity(state).get("space_id")
            if not root:
                raise QueryError("SCOPE_NOT_RESOLVED", "尚未确认本次查询区域，无法统计其设备")
            query["scope"] = {"root_space_id": root, "recursive": semantics.get("descendant_recursive", True)}
        query["freshness"] = "current"
    return AssetQuery.model_validate(query).model_dump(mode="json", exclude_none=True)


def subject_key(secret, user_token):
    return hmac.new(secret.encode(), ("asset-query-owner:"+user_token).encode(), hashlib.sha256).hexdigest()


def signed_arguments(request, runtime):
    settings = runtime.settings
    query = dict(request.arguments.get("query") or {})
    secret = settings.phm_asset_query_context_secret
    claims = {"subject": subject_key(secret, request.user_token), "conversation_id": request.conversation_id,
              "branch_id": request.branch_id, "task_id": request.task_id,
              "call_id": getattr(request, "asset_query_call_id", None) or request.task_id,
              "allowed_query_ids": list(getattr(request, "asset_allowed_query_ids", []) or []),
              "shared_catalog": settings.phm_asset_query_shared_catalog}
    return {"query": query, "request_context": sign_context(secret, claims, query)}


async def remember_collection(runtime, request, payload):
    if not payload.get("query_id"):
        return
    async with runtime.event_service.session_factory() as session, session.begin():
        # A server-owned task decides the actual assistant message; no client/model
        # message_id may insert a reference into another branch's history.
        await session.execute(text("""INSERT INTO asset_query_links
            (id,query_id,conversation_id,branch_id,task_id,message_id,subject,payload)
            SELECT :id,:query_id,t.conversation_id,t.branch_id,t.id,t.assistant_message_id,:subject,CAST(:payload AS jsonb)
            FROM generation_tasks t JOIN conversations c ON c.id=t.conversation_id
            WHERE t.id=:task_id AND t.conversation_id=:conversation_id AND t.branch_id=:branch_id
              AND c.user_token=:user_token AND t.assistant_message_id IS NOT NULL
            ON CONFLICT(message_id,query_id) DO NOTHING"""),
            {"id": uuid4(), "query_id": UUID(payload["query_id"]), "task_id": UUID(request.task_id),
             "conversation_id": UUID(request.conversation_id), "branch_id": UUID(request.branch_id),
             "subject": subject_key(runtime.settings.phm_asset_query_context_secret, runtime.user_token),
             "user_token": runtime.user_token,
             "payload": __import__("json").dumps({k: payload.get(k) for k in
                 ("criteria", "criteria_signature", "scope_name", "count", "unknown_count", "snapshot_at", "expires_at")}, ensure_ascii=False)})


async def load_collections(session, branch, path, settings):
    if not settings.phm_asset_unified_query_enabled or not path:
        return []
    ordered_ids = [m.id for m in path if str(m.role).upper() == "ASSISTANT" and m.status == "COMPLETED"]
    if not ordered_ids:
        return []
    # Actual parent path allows explicit forks to inherit visible ancestors. Other
    # branches and concurrent devices cannot leak their later results into this path.
    rows = (await session.execute(text("""SELECT l.query_id,l.message_id,l.payload,l.subject,c.user_token
        FROM asset_query_links l JOIN conversations c ON c.id=l.conversation_id
        WHERE l.conversation_id=:conversation_id AND l.message_id=ANY(CAST(:ids AS uuid[]))"""),
        {"conversation_id": branch.conversation_id, "ids": ordered_ids})).mappings().all()
    position = {value: index for index, value in enumerate(ordered_ids)}
    result = []
    for row in sorted(rows, key=lambda r: position[r["message_id"]]):
        if row["subject"] != subject_key(settings.phm_asset_query_context_secret, row["user_token"]):
            continue
        result.append({"query_id": str(row["query_id"]), "source_message_id": str(row["message_id"]), **row["payload"]})
    return result[-12:]


def goal_satisfied(intent, payload):
    if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("schema_version") != "2.0":
        return False
    if payload.get("operation") != intent.get("operation", "list") or payload.get("group_by") != intent.get("group_by"):
        return False
    if not isinstance(payload.get("count"), int) or payload.get("unknown_count") is None:
        return False
    if intent.get("operation") == "group" and payload.get("count") and not payload.get("groups"):
        return False
    if intent.get("operation", "list") == "list" and payload.get("count") and not payload.get("devices"):
        return False
    if intent.get("operation", "list") == "list" and payload.get("page_complete") is False:
        return False
    return payload.get("result_complete") is True


_CATEGORY_FALLBACK_ERRORS = {"CATEGORY_UNSUPPORTED", "CATEGORY_AMBIGUOUS"}
_CATEGORY_FALLBACK_RELATIONS = {"SAME", "CHILD"}


def _category_fallback_replacement(trace):
    """Rebuild the only predicate rewrite Asset MCP is allowed to make.

    The original request remains signed and immutable.  When strict taxonomy
    lookup fails, Asset MCP may replace one ``equipment_class`` atom with an
    OR of real reviewed tag ids selected by the temporary LLM hierarchy.
    Conversation independently reconstructs that rewrite from the audit trace
    instead of accepting an arbitrary changed predicate.
    """
    if not isinstance(trace, dict):
        return None
    if (trace.get("field") != "equipment_class" or
            trace.get("strict_error") not in _CATEGORY_FALLBACK_ERRORS or
            trace.get("resolution_source") != "llm_temporary_hierarchy" or
            trace.get("hierarchy_persisted") is not False or
            trace.get("candidate_source") != "reviewed_catalog_tags"):
        return None

    input_term = trace.get("input_term")
    codes = trace.get("selected_tag_codes")
    selected_tags = trace.get("selected_tags")
    relations = trace.get("llm_relations")
    if not isinstance(input_term, str) or not input_term.strip():
        return None
    if not isinstance(codes, list) or not 1 <= len(codes) <= 16:
        return None
    if any(not isinstance(code, str) or not code or len(code) > 256 for code in codes):
        return None
    if len(set(codes)) != len(codes):
        return None
    if not isinstance(selected_tags, list) or not isinstance(relations, list):
        return None

    tag_codes = [
        item.get("tag_code") for item in selected_tags
        if isinstance(item, dict) and isinstance(item.get("tag_code"), str)
    ]
    if set(tag_codes) != set(codes):
        return None

    relation_codes = {
        str(item.get("tag_code"))
        for item in relations
        if isinstance(item, dict)
        and item.get("selected") is True
        and str(item.get("relation") or "").upper() in _CATEGORY_FALLBACK_RELATIONS
    }
    if not set(codes) <= relation_codes:
        return None

    atoms = [{
        "field": "equipment_class",
        "operator": "is",
        "category_id": code,
        "include_descendants": False,
    } for code in codes]
    return atoms[0] if len(atoms) == 1 else {"any": atoms}


def _validated_category_rewrite(expected_predicate, criteria):
    """Return True only for a fully auditable category fallback rewrite."""
    traces = criteria.get("category_resolution")
    if not isinstance(traces, list) or not traces:
        return False
    if criteria.get("requested_predicate") != normalize_tree(expected_predicate):
        return False

    index = [0]

    def rewrite(node):
        if node is None:
            return None
        if "not" in node:
            return {"not": rewrite(node["not"])}
        for key in ("all", "any"):
            if key in node:
                return {key: [rewrite(child) for child in node[key]]}
        atom = dict(node)
        if atom.get("field") != "equipment_class" or index[0] >= len(traces):
            return atom
        trace = traces[index[0]]
        term = str(atom.get("category_id") or atom.get("value") or "").strip()
        if not isinstance(trace, dict) or str(trace.get("input_term") or "").strip() != term:
            return atom
        replacement = _category_fallback_replacement(trace)
        if replacement is None:
            return atom
        index[0] += 1
        return replacement

    effective = normalize_tree(rewrite(expected_predicate))
    if index[0] != len(traces):
        return False
    if criteria.get("predicate") != effective:
        return False
    # Asset MCP exposes the same trace at the top level for diagnostics.  If it
    # is present, it must agree with the signed criteria rather than describe a
    # different rewrite.
    return True


def validate_response(query, payload):
    expected = AssetQuery.model_validate(query)
    criteria = payload.get("criteria") or {}
    valid = (payload.get("schema_version") == "2.0" and
             payload.get("request_signature") == signature(expected.model_dump(mode="json")) and
             payload.get("operation") == expected.operation and payload.get("group_by") == expected.group_by)
    if expected.scope:
        valid = valid and criteria.get("scope") == expected.scope.model_dump()
    if expected.reference:
        if expected.reference.mode == "same_set":
            valid = valid and payload.get("query_id") == expected.reference.query_id
        else:
            valid = valid and criteria.get("parent_query_id") == expected.reference.query_id
    else:
        exact_predicate = criteria.get("predicate") == normalize_tree(expected.predicate)
        category_rewrite = _validated_category_rewrite(expected.predicate, criteria) if not exact_predicate else False
        valid = valid and (exact_predicate or category_rewrite)
        if category_rewrite and "category_resolution" in payload:
            valid = valid and payload.get("category_resolution") == criteria.get("category_resolution")
    if not valid:
        raise QueryError("QUERY_RESULT_MISMATCH", "资产查询返回的范围或内容与本次要求不一致，未将其作为统计结论。")


def _cell(value):
    return str(value or "待补充").replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;")


def predicate_text(node, conditions):
    if not node:
        return "全部设备"
    if "not" in node:
        return "排除（"+predicate_text(node["not"], conditions)+"）"
    for key, word in (("all", "，并且"), ("any", "，或者")):
        if key in node:
            return "（"+word.join(predicate_text(c, conditions) for c in node[key])+"）"
    field = node.get("field")
    if field == "equipment_name":
        op = {"contains":"包含", "equals":"为", "starts_with":"开头为"}.get(node.get("operator"), "包含")
        return f"设备名称{op}“{_cell(node.get('value'))}”"
    term = node.get("value") or node.get("category_id")
    label = next((v.get("name") for v in conditions if v.get("id") == term or v.get("name") == term), None)
    label = label or (term if node.get("value") else "已确认类别")
    dimension = {**GROUP_NAMES, "monitoring":"监测适用类别", "management":"管理类别"}.get(field, "类别")
    return f"{dimension}为“{_cell(label)}”"


def fact_text(payload):
    if not payload or payload.get("success") is not True:
        return ""
    count, unknown = int(payload.get("count") or 0), int(payload.get("unknown_count") or 0)
    scope = _cell(payload.get("scope_name") or "目标区域")
    criteria = payload.get("criteria") or {}
    suffix = "符合本次筛选条件的设备" if criteria.get("predicate") else "设备"
    origin = "上次查询的资产目录中" if payload.get("freshness") == "referenced_snapshot" else "当前资产目录中"
    lines = [f"{origin}，{scope}范围内已确认的{suffix}共 **{count} 台**。"]
    if criteria.get("predicate"):
        lines.append("筛选依据："+predicate_text(criteria["predicate"],criteria.get("normalized_conditions") or [])+"。")
    if payload.get("freshness") == "referenced_snapshot":
        lines.append("本次沿用上次查询确认的设备集合。")
    elif payload.get("snapshot_at"):
        lines.append("本次读取的是查询时刻的资产目录。")
    if unknown:
        lines.append(f"另有 **{unknown} 台**设备资料不足，暂时不能确定是否符合条件，以上数量不是已确认的完整总数。")
    if payload.get("operation") == "group" and payload.get("groups"):
        label = GROUP_NAMES.get(payload.get("group_by"), "分类")
        lines += ["", f"| {label} | 设备数量 |", "| --- | ---: |"]
        lines += [f"| {_cell(row['name'])} | {row['count']} 台 |" for row in payload["groups"]]
        if payload.get("grouping_mode") == "overlapping":
            lines += ["", "同一设备可能归入多个类别，各类别数量不能直接相加作为设备总数。"]
    elif payload.get("operation") == "list" and payload.get("devices"):
        devices = payload["devices"]
        cap = 200
        lines += ["", "| 设备名称 | 设备编号 | 所属位置 |", "| --- | --- | --- |"]
        lines += [f"| {_cell(d.get('equip_name'))} | {_cell(d.get('equip_no'))} | {_cell(d.get('path'))} |" for d in devices[:cap]]
        if len(devices) > cap or not payload.get("page_complete", True):
            lines += ["", f"本次正文展示 {min(cap, len(devices))} 台，完整统计仍为 {count} 台。可按区域或类别进一步缩小范围。"]
    for warning in payload.get("warnings") or []:
        if str(warning) and str(warning) not in lines and "资料不足" not in warning and "归入多个" not in warning:
            lines.append(str(warning))
    return "\n".join(lines) + "\n\n"
