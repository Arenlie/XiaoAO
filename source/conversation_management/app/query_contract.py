"""One model-owned semantic plan; execution never parses wording to select a path."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

QUERY_TOOL_ID = "workflow.phm.structured_query"
RESULT_TOOL_ID = "workflow.phm.result_followup"


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = False
    domain: Literal["asset", "health", "alarm"] = "asset"
    target: Literal["space", "equipment", "point"] = "equipment"
    operation: Literal["count", "list", "detail", "group", "rank", "compare", "trend"] = "list"
    anchor: Literal["space", "equipment", "point", "context"] = "space"
    recursive: bool = True
    target_space_type: str | None = Field(default=None, max_length=64)
    predicate: dict | None = None
    metric: Literal["health_score", "count_records", "sum_occurrences", "distinct_equipment"] = "health_score"
    group_by: Literal["area", "equipment", "alarm_type", "warn_level", "equipment_class"] | None = None
    order: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=20, ge=1, le=5000)
    alarm_types: list[Literal["threshold", "trend", "diagnosis", "ai"]] = Field(default_factory=list)
    alarm_state: Literal["all", "active", "ended"] = "active"
    start_time: str | None = None
    end_time: str | None = None
    comparison_start: str | None = None
    comparison_end: str | None = None
    include_fields: list[Literal["area", "health", "alarms", "equipment_type", "model"]] = Field(default_factory=list)
    # This expression is a request, never a bypass of server capability checks.
    statistic: Literal["builtin", "median", "p95", "stddev"] = "builtin"
    advanced_expression: str = Field(default="", max_length=1500)
    source_expression: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.predicate:
            from app.asset_query_contract import validate_predicate
            validate_predicate(self.predicate)
        if bool(self.start_time) != bool(self.end_time):
            raise ValueError("时间范围需要同时提供开始与结束时间")
        if bool(self.comparison_start) != bool(self.comparison_end):
            raise ValueError("比较时间范围不完整")
        from datetime import datetime
        for start,end in ((self.start_time,self.end_time),(self.comparison_start,self.comparison_end)):
            if start and datetime.fromisoformat(start.replace("Z","+00:00")) >= datetime.fromisoformat(end.replace("Z","+00:00")):
                raise ValueError("开始时间必须早于结束时间")
        if self.active and self.domain == "alarm" and self.metric == "health_score":
            self.metric = "count_records"
        if self.statistic != "builtin" and self.domain not in {"health","alarm"}:
            raise ValueError("高级统计需要有明确的健康度或报警数值字段")
        if self.statistic != "builtin" and (self.operation not in {"group","detail"} or not self.advanced_expression):
            raise ValueError("高级统计需要明确计算目标，使用detail或group操作")
        if self.advanced_expression and self.statistic == "builtin":
            raise ValueError("标准查询能够完成的任务不进入高级统计；其他能力缺口需先明确可用数据")
        if self.active:
            if self.domain == "health" and self.statistic == "builtin" and (self.operation in {"group", "compare"} or self.group_by):
                raise ValueError("健康度分组需要明确计算口径；区域排名应查询区域自身的健康度，不能统计设备数量代替")
            if self.domain == "asset" and (self.operation in {"rank", "compare", "trend"} or self.start_time):
                raise ValueError("资产目录不支持此指标或历史操作，请选择健康度、报警查询或资产集合能力")
            if self.domain == "alarm" and self.group_by in {"alarm_type", "warn_level"}:
                raise ValueError("按报警类型或等级分组应使用现有报警明细统计能力")
        return self


class ResultFollowup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = False
    action: Literal["render", "enrich", "filter", "sort", "explain", "verify", "refresh_values", "refresh_query"] = "render"
    source_result_id: str | None = Field(default=None, max_length=128)
    source_message_id: str | None = Field(default=None, max_length=128)
    selection: Literal["displayed", "all", "ordinals"] = "displayed"
    ordinals: list[int] = Field(default_factory=list, max_length=100)
    fields: list[Literal["area", "health", "alarms", "equipment_type", "model"]] = Field(default_factory=list)
    predicate: dict | None = None
    sort_by: Literal["health_score", "area", "name", "alarm_count"] | None = None
    order: Literal["asc", "desc"] = "asc"
    format: Literal["table", "list", "prose"] = "table"
    source_expression: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.selection == "ordinals" and (not self.ordinals or any(i < 1 for i in self.ordinals)):
            raise ValueError("引用序号必须是原回答中实际存在的正整数")
        if self.predicate:
            from app.asset_query_contract import validate_predicate
            validate_predicate(self.predicate)
        return self


QUERY_RULES = """
输出 query_plan 和 result_followup（无需求时省略，默认active=false）。它们只表达语义，不填真实编码或SQL。
query_plan: active,domain(asset/health/alarm),target(space/equipment/point),operation(count/list/detail/group/rank/compare/trend),anchor(space/equipment/point/context),recursive,target_space_type,predicate,metric(health_score/count_records/sum_occurrences/distinct_equipment),group_by(area/equipment/alarm_type/warn_level/equipment_class),order(asc/desc),limit,start_time,end_time,comparison_start,comparison_end,alarm_types,alarm_state(all/active/ended),include_fields,advanced_expression,source_expression。
区域/测点清单、健康度查询和排序、报警统计/排名/比较优先用query_plan。单设备报警具体记录用operation=detail；报警集合list返回逐设备数量，不是报警明细。报警按区域统计/排名用group_by=area,target=equipment；按类型或等级的明细统计使用现有报警查询能力。单区域范围内排名根为space，候选设备是输出，不先让用户选设备。简单设备资产count/list/group继续使用asset_query，不要同时激活query_plan。平台设备默认全部已接入传感器，predicate=null；“监测设备”不是monitoring=是分类，传感器自检覆盖也不是资产统计条件。
健康度排名使用平台health_score，升序为最低；最需关注而未指定规则时采用健康度升序并在理由中说明依据。区域健康度使用平台区域分数，不用设备均值代替。健康度原因解释继续健康度解释能力，不启动专业诊断。查询时段必须输出绝对时间并保持时间字段含义。
结果追问优先result_followup：active,action(render/enrich/filter/sort/explain/verify/refresh_values/refresh_query),source_result_id,source_message_id,selection(displayed/all/ordinals),ordinals,fields(area/health/alarms/equipment_type/model),predicate,sort_by(health_score/area/name/alarm_count),order,format(table/list/prose),source_expression。
“给每台加区域”=enrich fields=[area] selection=displayed；不重新做全局排名。“刷新这十台”=refresh_values；“重新找当前最低十台”=refresh_query。仅换格式=render，事实质疑=verify。序号来自原表，引用只能选recent_results中的标识；不确定指的是哪张表时保留歧义，不能猜另一组。
追问沿用原结果时query_plan.active=false,asset_query.active=false,recipe_recommended=false,workflow_id=none,asset_semantics.needs_asset_lookup=false。只在用户明确新对象时走新查询。query_plan已激活时recipe_recommended=false,workflow_id=none，asset_semantics只提取根对象。
复杂计算用advanced_expression描述缺口，并填statistic=median/p95/stddev（中位数/95分位数/总体标准差）；默认statistic=builtin，标准执行器不支持这三个扩展统计时才进入受控SQL。高级统计用operation=detail或group；只有服务端确认现有算子不支持且数据足够时才可生成SQL。空结果、超时、权限不足不是SQL兜底理由。内置支持时不填写advanced_expression。
"""


def normalize_plan(payload, state):
    """Normalize execution topology from model semantics, never from keywords."""
    result = dict(payload)
    follow = ResultFollowup.model_validate(result.get("result_followup") or {})
    plan = QueryPlan.model_validate(result.get("query_plan") or {})
    if follow.active and plan.active:
        raise ValueError("原结果续用和新查询不能同时激活；刷新原条件用refresh_query")
    hints = dict(result.get("asset_semantics") or {})
    goal = dict(result.get("goal_frame") or {})
    if follow.active:
        # Contradictory model output must be repaired before it can hide a newly named object.
        known=[]
        for saved in (state.get("memory_context") or {}).get("answer_results") or []:
            if follow.source_result_id and saved.get("result_id")!=follow.source_result_id:continue
            for row in [saved.get("root") or {},*(saved.get("rows") or [])]:
                known.extend(str(row.get(k) or "").strip().casefold() for k in ("name","area","equip_no","point_no","space_name"))
        phrases=[str((hints.get(k) or {}).get("raw_text") or "").strip().casefold() for k in ("area","equipment","point","equip_no","point_no") if isinstance(hints.get(k),dict)]
        if any(term and term not in known for term in phrases):
            raise ValueError("原结果追问包含未属于原结果的新对象，请按当前新对象重新理解，不可直接复用旧结果")
        result.update(workflow_id="none", variant_id="none", recipe_recommended=False, asset_query={"active": False})
        hints["needs_asset_lookup"] = False
        goal.update(anchor_entity_level="none", can_answer_from_context=True, target_is_system_output=True)
    elif plan.active:
        result.update(workflow_id="none", variant_id="none", recipe_recommended=False, asset_query={"active": False})
        root = plan.anchor
        hints["needs_asset_lookup"] = True
        goal.update(anchor_entity_level=root if root != "context" else hints.get("reference_target_level", "none"),
                    target_entity_level=plan.target, evidence_types=[plan.domain], evidence_required=True)
    if not goal.get("goal"):
        goal["goal"] = str(state.get("query") or "")
    if state.get("understanding_results"):
        goal["evidence_types"] = list(dict.fromkeys([*(goal.get("evidence_types") or []), "file"]))
    result.update(asset_semantics=hints, goal_frame=goal,
                  query_plan=plan.model_dump(), result_followup=follow.model_dump())
    return result
