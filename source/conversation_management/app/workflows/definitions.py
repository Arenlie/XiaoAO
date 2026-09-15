from __future__ import annotations

from app.tools.phm_asset_mcp import (
    PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
    PHM_ASSET_QUERY_POINTS_TOOL_ID,
    PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
    PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
)
from app.tools.phm_data_mcp import (
    PHM_GET_DATA_SNAPSHOT_TOOL_ID,
    PHM_QUERY_ALARM_RECORDS_TOOL_ID,
    PHM_QUERY_HEALTH_SCORE_TOOL_ID,
)
from app.tools.phm_diagnosis_mcp import PHM_DIAGNOSIS_POINT_TOOL_ID
from app.tools.phm_feature_mcp import PHM_FEATURE_EXTRACT_RPM_TOOL_ID
from app.tools.phm_sensor_mcp import SENSOR_OPERATIONS, SENSOR_TOOL_BY_OPERATION
from app.tools.phm_workflow_tools import (
    PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
    PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
)
from app.workflows.contracts import (
    WorkflowDefinition,
    WorkflowStepDefinition,
    WorkflowVariantDefinition,
)


def _step(
    step_id: str,
    display_name: str,
    target_id: str,
    objective: str,
    level: str,
    *,
    call_type: str = "tool",
    depends_on: list[str] | None = None,
    arguments: dict | None = None,
    failure_policy: str = "stop",
) -> WorkflowStepDefinition:
    return WorkflowStepDefinition(
        step_id=step_id,
        display_name=display_name,
        call_type=call_type,
        target_id=target_id,
        objective=objective,
        required_entity_level=level,
        depends_on=depends_on or [],
        arguments=arguments or {},
        failure_policy=failure_policy,
    )


def default_workflow_definitions() -> list[WorkflowDefinition]:
    """Operator-visible business workflows, including read-only sensor queries."""

    return [
        WorkflowDefinition(
            workflow_id="sensor_information_query", display_name="传感器监测清单、故障与在线状态查询链路", version="1.2.1",
            description="复用统一实体解析，查询传感器正式故障、历史、在线/离线及监测范围；故障类型来自本轮结构化语义，具体编码由后端注入。全局查询不继承旧设备。",
            match_priority=15,
            variants=[WorkflowVariantDefinition(variant_id=op, display_name=spec[1], selection_rule=spec[2],
                required_entity_level="none", steps=[_step("sensor_query", spec[1], SENSOR_TOOL_BY_OPERATION[op], spec[2], "none")])
                for op, spec in SENSOR_OPERATIONS.items()],
        ),
        WorkflowDefinition(
            workflow_id="diagnosis_analysis",
            display_name="诊断分析链路",
            version="1.0.0",
            description=(
                "统一实体解析后区分区域风险筛查、测点级和设备级。区域风险筛查先展开真实设备集合并"
                "并行读取当前健康风险事实，用于回答‘哪台/哪些设备更值得优先诊断’；设备级再从 Asset MCP 读取全部振动加速度"
                "测点，再逐测点并行读取 Data MCP、识别 Feature MCP 转速、调用 Diagnosis MCP，"
                "最后由主智能体汇总并流式返回。"
            ),
            match_priority=10,
            variants=[
                WorkflowVariantDefinition(
                    variant_id="scope_screening",
                    display_name="区域下属设备故障风险筛查",
                    selection_rule=(
                        "用户给出一个区域，并要求在该区域下的设备集合中比较、排序或找出最可能存在严重故障/最需要优先诊断的设备；"
                        "此时具体设备是系统需要发现的结果，不得反问用户先指定设备"
                    ),
                    required_entity_level="area",
                    steps=[
                        _step(
                            "scope_collection",
                            "区域下属设备集合",
                            PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
                            "从已解析区域根向下查询真实设备集合，设备是待比较对象而不是待用户补充的单一目标",
                            "area",
                            arguments={"recursive": True, "limit": 5000},
                        ),
                        _step(
                            "health_collection_batch",
                            "设备风险事实并行读取",
                            PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
                            "对区域内真实设备限流并行读取当前健康度及模型分项事实，供主控模型筛出最需要优先诊断的设备；该步骤是风险筛查，不冒充逐设备完整故障确诊",
                            "area",
                            depends_on=["scope_collection"],
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="device",
                    display_name="设备级全部测点诊断",
                    selection_rule="用户要求诊断设备且未明确单个测点",
                    required_entity_level="equipment",
                    steps=[
                        _step(
                            "point_catalog",
                            "设备全部振动加速度测点",
                            PHM_ASSET_QUERY_POINTS_TOOL_ID,
                            "从 Asset MCP 查询设备的全部振动加速度测点",
                            "equipment",
                            arguments={"point_type": "vibration_acceleration", "limit": 1000},
                        ),
                        _step(
                            "point_data_batch",
                            "逐测点 Data MCP 并行读取",
                            PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
                            "未指定时间时读取最近24小时特征趋势（超时降级12小时）及每个测点最新波形",
                            "equipment",
                            depends_on=["point_catalog"],
                            arguments={"trend_hours": 24.0, "fallback_trend_hours": 12.0},
                        ),
                        _step(
                            "point_rpm_batch",
                            "逐测点 Feature MCP 转速识别",
                            PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
                            "使用每个测点自己的波形限流并行识别转速",
                            "equipment",
                            depends_on=["point_data_batch"],
                            failure_policy="continue",
                        ),
                        _step(
                            "point_diagnosis_batch",
                            "逐测点 Diagnosis MCP 诊断",
                            PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
                            "把每个测点的数据及同测点转速传入 Diagnosis MCP，并保留逐点结果供主智能体汇总",
                            "equipment",
                            depends_on=["point_data_batch", "point_rpm_batch"],
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="point",
                    display_name="测点级诊断",
                    selection_rule=(
                        "用户明确单个测点、轴承位置、驱动端或非驱动端，或要求诊断某设备的一个测点；"
                        "后者由模型从 Asset MCP 真实候选中选择一个测点"
                    ),
                    required_entity_level="point",
                    steps=[
                        _step(
                            "point_snapshot",
                            "测点 Data MCP 数据读取",
                            PHM_GET_DATA_SNAPSHOT_TOOL_ID,
                            "读取目标测点的波形与特征趋势快照",
                            "point",
                        ),
                        _step(
                            "rpm",
                            "Feature MCP 转速识别",
                            PHM_FEATURE_EXTRACT_RPM_TOOL_ID,
                            "从同一测点波形提取可靠转速和1X证据",
                            "point",
                            depends_on=["point_snapshot"],
                            failure_policy="continue",
                        ),
                        _step(
                            "point_diagnosis",
                            "Diagnosis MCP 测点诊断",
                            PHM_DIAGNOSIS_POINT_TOOL_ID,
                            "把该测点的数据与可用转速传入 Diagnosis MCP",
                            "point",
                            depends_on=["point_snapshot", "rpm"],
                        ),
                    ],
                ),
            ],
        ),
        WorkflowDefinition(
            workflow_id="health_analysis",
            display_name="健康度查询与解释链路",
            version="1.0.0",
            description=(
                "健康度普通查询读取权威分数；健康度原因/等级解释必须复用或解析目标设备，"
                "先识别四类分项中所有未满分项，再只检索这些分项对应的报警并总结。"
            ),
            match_priority=20,
            variants=[
                WorkflowVariantDefinition(
                    variant_id="device_current",
                    display_name="设备健康度查询",
                    selection_rule="设备当前或历史健康度查询",
                    required_entity_level="equipment",
                    steps=[
                        _step(
                            "health_score",
                            "设备健康度读取",
                            PHM_QUERY_HEALTH_SCORE_TOOL_ID,
                            "查询设备权威健康度并供主智能体呈现",
                            "equipment",
                            arguments={"scope_type": "device"},
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="space_current",
                    display_name="区域实时健康度查询",
                    selection_rule="事业部、车间、产线等区域健康度查询",
                    required_entity_level="area",
                    steps=[
                        _step(
                            "health_score",
                            "区域健康度读取",
                            PHM_QUERY_HEALTH_SCORE_TOOL_ID,
                            "查询区域实时权威健康度并供主智能体呈现",
                            "area",
                            arguments={"scope_type": "space"},
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="scope_collection",
                    display_name="区域下属实体健康度集合比较",
                    selection_rule=(
                        "用户要求比较一个已指定区域下面由模型指定层级/类型的实体健康度，"
                        "例如全部设备、某类空间节点，并可能要求最低、最高或排序"
                    ),
                    required_entity_level="area",
                    steps=[
                        _step(
                            "scope_collection",
                            "区域下属目标实体集合",
                            PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
                            "从已解析区域根向下查询主控模型结构化指定的真实实体层级/类型集合",
                            "area",
                            arguments={"recursive": True, "limit": 5000},
                        ),
                        _step(
                            "health_collection_batch",
                            "下属实体健康度并行读取",
                            PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
                            "对 Asset MCP 返回的每个真实空间/设备限流并行读取 Data MCP 当前健康度，完整事实供主控模型统一比较",
                            "area",
                            depends_on=["scope_collection"],
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="explanation",
                    display_name="设备健康度扣分与等级解释",
                    selection_rule=(
                        "为什么不是满分、为什么只有某分、为什么需要重点关注、健康度下降/扣分原因等表达"
                    ),
                    required_entity_level="equipment",
                    steps=[
                        _step(
                            "health_dimensions",
                            "四类健康度扣分项识别",
                            PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
                            "从 Data MCP 刷新结构化健康度，校验四类分项完整，并找出所有未满分项",
                            "equipment",
                        ),
                        _step(
                            "dimension_alarms",
                            "扣分项对应报警检索",
                            PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
                            "按未满分分项映射阈值、趋势、AI、机理/诊断报警并查询事实",
                            "equipment",
                            depends_on=["health_dimensions"],
                        ),
                    ],
                ),
            ],
        ),
        WorkflowDefinition(
            workflow_id="asset_information_query",
            display_name="区域/设备信息查询链路",
            version="1.0.0",
            description=(
                "查询设备测点清单时只读取权威测点目录；查询区域或设备总览时先读取上下资产结构和位置，再依次读取健康度、报警；"
                "设备结构 RAG 作为显式未接入步骤保留，最后由主智能体查漏补缺并流式返回。"
            ),
            match_priority=30,
            variants=[
                WorkflowVariantDefinition(
                    variant_id="asset_collection",
                    display_name="区域资产集合统计",
                    selection_rule=(
                        "用户要求统计、列举或筛选某个已确认区域范围内的一类或全部资产；"
                        "设备类别/功能/管理条件由模型结构化表达，Asset MCP 仅映射到已审核语义标签并由 PostgreSQL 确定成员/数量"
                    ),
                    required_entity_level="area",
                    supports_collection=True,
                    steps=[
                        _step(
                            "asset_collection",
                            "区域资产集合检索",
                            PHM_ASSET_QUERY_SCOPE_COLLECTION_TOOL_ID,
                            "查询区域下真实资产；数量模式直接数据库计数，列表模式按需分页；分类条件由 Asset MCP 受限映射到已审核 tag_code",
                            "area",
                            arguments={
                                "target_entity_level": "equipment",
                                "recursive": True,
                                "limit": 50000,
                            },
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="device_points",
                    display_name="设备测点清单",
                    selection_rule=(
                        "用户只要求查看或列出某台设备有哪些测点，不包含诊断、故障判断、"
                        "波形或机理分析目标"
                    ),
                    required_entity_level="equipment",
                    steps=[
                        _step(
                            "point_catalog",
                            "设备测点目录",
                            PHM_ASSET_QUERY_POINTS_TOOL_ID,
                            "从 Asset MCP 查询设备下全部真实测点，仅返回测点目录，不读取波形、不执行诊断",
                            "equipment",
                            arguments={"limit": 1000},
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="device_overview",
                    display_name="设备信息总览",
                    selection_rule="用户想了解某台设备的信息、位置、状态或整体情况",
                    required_entity_level="equipment",
                    steps=[
                        _step(
                            "asset_detail",
                            "设备详情与上级位置",
                            PHM_ASSET_QUERY_EQUIPMENT_INFO_TOOL_ID,
                            "查询设备编码、名称、类型及完整所属空间路径",
                            "equipment",
                        ),
                        _step(
                            "space_structure",
                            "所在区域及下级设备结构",
                            PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
                            "从设备详情返回的真实所属空间查询上下级结构并列出区域内设备",
                            "equipment",
                            depends_on=["asset_detail"],
                            arguments={
                                "max_depth": 10,
                                "include_devices": True,
                                "include_points": False,
                                "_identity_source": "equipment_parent",
                            },
                        ),
                        _step(
                            "health_score",
                            "设备当前健康度",
                            PHM_QUERY_HEALTH_SCORE_TOOL_ID,
                            "查询设备当前最新健康度与等级",
                            "equipment",
                            depends_on=["asset_detail", "space_structure"],
                            arguments={"scope_type": "device"},
                        ),
                        _step(
                            "alarm_records",
                            "设备当前与近期报警",
                            PHM_QUERY_ALARM_RECORDS_TOOL_ID,
                            "查询设备当前与近期报警事实",
                            "equipment",
                            depends_on=["health_score"],
                            arguments={"time_mode": "default", "metric": "detail", "limit": 20},
                        ),
                        _step(
                            "structure_rag",
                            "设备结构知识 RAG",
                            PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
                            "检索设备结构知识；当前未接入时明确标记并禁止补造",
                            "equipment",
                            depends_on=["alarm_records"],
                        ),
                    ],
                ),
                WorkflowVariantDefinition(
                    variant_id="area_overview",
                    display_name="区域信息总览",
                    selection_rule="用户想了解某区域、车间、产线的结构、设备或整体情况",
                    required_entity_level="area",
                    steps=[
                        _step(
                            "space_structure",
                            "区域上下级结构与设备",
                            PHM_ASSET_QUERY_SPACE_TREE_TOOL_ID,
                            "查询区域真实空间结构并列出下属设备",
                            "area",
                            arguments={
                                "max_depth": 10,
                                "include_devices": True,
                                "include_points": False,
                            },
                        ),
                        _step(
                            "health_score",
                            "区域当前健康度",
                            PHM_QUERY_HEALTH_SCORE_TOOL_ID,
                            "查询区域当前实时健康度与等级",
                            "area",
                            depends_on=["space_structure"],
                            arguments={"scope_type": "space"},
                        ),
                        _step(
                            "alarm_records",
                            "区域当前与近期报警",
                            PHM_QUERY_ALARM_RECORDS_TOOL_ID,
                            "查询区域及下属设备当前与近期报警事实",
                            "area",
                            depends_on=["health_score"],
                            arguments={"time_mode": "default", "metric": "detail", "limit": 50},
                        ),
                        _step(
                            "structure_rag",
                            "区域/设备结构知识 RAG",
                            PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
                            "检索区域内设备结构知识；当前未接入时明确标记并禁止补造",
                            "area",
                            depends_on=["alarm_records"],
                        ),
                    ],
                ),
            ],
        ),
    ]
