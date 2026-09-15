"""Only deterministic validation/dispatch here; wording semantics stay in the main model."""
from uuid import uuid4

from app.asset_query_contract import QueryError, TOOL_ID
from app.services.asset_collections import build_query, collection_intent, goal_satisfied

ASSET_QUERY_RULES = """
设备资产集合的数量、清单、筛选、种类或分类汇总必须输出asset_query（不适用于单设备详情、测点清单、健康度比较或诊断）。
- active=true；operation=count/list/group；group操作必须给group_by，支持equipment_class、equipment_subclass、purpose、structure_type、model、company、plant、line、area。
- “有几台水泵”按equipment_class类别查询；“名字带有水泵的设备”按equipment_name的contains查询。必须依据完整语义决定，不用工具名称或某个关键词代替理解。“设备/设备资产/全部设备”只是对象层级，predicate=null，不能映射为生产设备等任何特定类别。
- predicate为空或布尔树：{"all":[条件,...]}、{"any":[条件,...]}、{"not":条件}。不能把“或”变成“且”，排除条件也要保留。
- 名称条件：{"field":"equipment_name","operator":"contains/equals/starts_with","value":"用户指定的正式名称文字"}；仅查正式名称，不查别名、说明、标签；百分号下划线当普通文字。
- 类别条件：{"field":"equipment_class/purpose/structure_type/monitoring/management","operator":"is","value":"用户要求的类别名称","include_descendants":true}。类别由审核分类词典确定，不可猜标签编码、SQL或无依据的同义类别。查询未知类别也应如实传给后端校验。
- reference_mode=new（新查询或换了区域/对象），same_set（沿用刚才那批设备，仅改变数量/清单/分组展示），refine_set（在原集合内增加条件），refresh（按原条件重新查询当前目录）。same_set时predicate=null；refine_set只填写新增条件；refresh保留原条件时predicate=null。
- “这些设备有哪几类/刚才115台是什么种类”应same_set + group，不要再次按名称搜水泵。明确改查别的区域、其他设备类别时new，并重新解析区域；不能因存在旧集合固定查询对象。“现在重新统计”选refresh。
- 使用memory中的recent_asset_queries选择实际历史来源，source_message_id只能取其中source_message_id；不能猜query_id或真实区域ID。未明确指定较早回复时可省略source_message_id，后端使用当前消息链最近的设备集合。历史集合不存在也不能擅自改查同名集合。
- source_expression逐字摘录本轮问题中支持判断的短语。仅变化措辞而含义相同时，应给出相同的筛选逻辑。
- 新设备集合查询设置asset_semantics.needs_asset_lookup=true；有明确区域时只解析这个真实区域作为根。没有明确区域/设备根对象时，类别或名称条件仍只是集合筛选条件，不能把equipment_type/equipment_class解析成某一台设备；后端使用受信任的共享资产目录作为本次集合范围。纯原集合追问且没有新对象不重新解析实体，needs_asset_lookup=false。
"""


def validate_classification(payload, state):
    intent = payload.get("asset_query") or {}
    pure_asset_recipe = payload.get("workflow_id") == "asset_information_query" and payload.get("variant_id") == "asset_collection"
    if pure_asset_recipe and not intent.get("active"):
        raise ValueError("设备集合查询缺少asset_query，需要明确类别/名称及数量/清单/分类语义")
    if not intent.get("active"):
        return
    expression = intent.get("source_expression") or ""
    if expression and expression not in str(state.get("query") or ""):
        raise ValueError("资产查询语义依据必须来自当前用户输入")
    semantics = payload.get("asset_semantics") or {}
    if intent.get("reference_mode", "new") in {"same_set", "refine_set"}:
        if (semantics.get("area") or {}).get("raw_text") or (semantics.get("equipment") or {}).get("raw_text"):
            raise ValueError("当前明确提出新对象时不能直接沿用原集合；必须先确定是否改查范围")
    payload["recipe_recommended"] = False
    payload["recipe_match_confidence"] = 0.0


def collection_verdict(state, descriptors):
    from app.orchestration.supervisor.contracts import SupervisorVerdict, SupervisorVerdictType
    from app.orchestration.supervisor.contracts import AgentCall
    intent = collection_intent(state)
    if not intent or not any(t.tool_id == TOOL_ID and t.enabled for t in descriptors):
        return None
    attempted = [o for o in state.get("observations", []) if o.get("tool_id") == TOOL_ID]
    if not attempted:
        try:
            build_query(state)
        except (QueryError, ValueError) as exc:
            return SupervisorVerdict(verdict=SupervisorVerdictType.CANNOT_ANSWER, answerable=False,
                final_answer_draft=str(exc), reason_summary="资产查询范围或原集合尚未确认，未执行不同条件的替代查询。")
        return SupervisorVerdict(verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
            next_call=AgentCall(call_id=str(uuid4()), call_type="tool", tool_id=TOOL_ID,
                objective="按统一条件查询设备集合，并保留原集合供后续数量、清单与分类追问", arguments={}))
    payload = state.get("asset_query_result") or {}
    if attempted[-1].get("error_code") == "QUERY_RESULT_MISMATCH" and len(attempted) < 2:
        return SupervisorVerdict(verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
            next_call=AgentCall(call_id=str(uuid4()),call_type="tool",tool_id=TOOL_ID,
                objective="保持原条件，核验一次资产查询返回的不一致结果",arguments={}))
    complete = goal_satisfied(intent, payload)
    # Partial classification is useful evidence, not grounds for silently changing
    # a category into a name filter. A success code alone never certifies the goal.
    if payload.get("success"):
        if (payload.get("operation") != intent.get("operation", "list") or payload.get("group_by") != intent.get("group_by")) and len(attempted) < 2:
            return SupervisorVerdict(verdict=SupervisorVerdictType.NEED_MORE_EVIDENCE,
                next_call=AgentCall(call_id=str(uuid4()), call_type="tool", tool_id=TOOL_ID,
                    objective="保持原筛选条件，修复一次返回内容与用户要求不一致的问题", arguments={}))
        return SupervisorVerdict(verdict=SupervisorVerdictType.ANSWERABLE, answerable=True,
            reason_summary="已核验数量、清单和分类均来自所请求的集合。" if complete else "已取得部分资产证据，最终回答必须说明资料缺口或未完成部分。")
    error = attempted[-1].get("error_message") or attempted[-1].get("answer_markdown") or "本次资产查询未完成，不能将失败解释成零台设备。"
    return SupervisorVerdict(verdict=SupervisorVerdictType.CANNOT_ANSWER, answerable=False,
        final_answer_draft=error, reason_summary="资产查询失败，保留原条件，不把没有结果当成零台设备。")
