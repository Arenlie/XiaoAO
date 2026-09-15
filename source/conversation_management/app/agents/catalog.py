from __future__ import annotations

from app.agents.contracts import AgentAdapterType, AgentDescriptor, AgentHealthStatus

SUPERVISOR_AGENT_ID = "builtin.supervisor"
GENERAL_CONTENT_AGENT_ID = "builtin.general_content"
FUZZY_ENTITY_AGENT_ID = "builtin.fuzzy_entity"
AI_DIAGNOSIS_AGENT_ID = "dify.ai_diagnosis"
LEGACY_XIAOAO_AGENT_ID = "dify.industrial_data"
INDUSTRIAL_DATA_AGENT_ID = LEGACY_XIAOAO_AGENT_ID


def default_agent_descriptors(
    *,
    legacy_ai_diagnosis_enabled: bool = False,
    legacy_xiaoao_enabled: bool = False,
) -> list[AgentDescriptor]:
    return [
        AgentDescriptor(
            agent_id=FUZZY_ENTITY_AGENT_ID,
            display_name="统一实体解析能力",
            adapter_type=AgentAdapterType.BUILTIN,
            description="由 PHM Asset MCP 统一决定跳过、复用、刷新、范围下钻和实体候选解析。",
            public_description="结合当前问题与上下文，按需定位真实区域、设备和测点。",
            capabilities=[
                "entity_resolution", "plant_structure_resolution", "equipment_resolution",
                "point_resolution", "entity_candidate_search",
            ],
            supported_entity_types=["area", "equipment", "point"],
            requires_data_access_token=False,
            priority=50,
            timeout_seconds=120,
            max_retries=2,
            health_status=AgentHealthStatus.UNKNOWN,
        ),
        AgentDescriptor(
            agent_id=AI_DIAGNOSIS_AGENT_ID,
            display_name="旧 Dify AI诊断（已关闭）",
            adapter_type=AgentAdapterType.DIFY,
            description=(
                "旧 Dify AI诊断回滚入口。专业诊断计算已迁移到 PHM Diagnosis MCP，"
                "默认不参与主控路由或执行。"
            ),
            public_description="旧 Dify 诊断能力，当前已关闭；专业诊断由 PHM Diagnosis MCP 提供。",
            capabilities=[
                "legacy_ai_diagnosis", "legacy_initial_diagnosis", "legacy_diagnosis_followup",
            ],
            supported_entity_types=["equipment", "point"],
            requires_data_access_token=True,
            supports_streaming=True,
            supports_resume=True,
            supports_attachments=True,
            supported_attachment_kinds=["image", "pdf", "document", "spreadsheet", "text"],
            enabled=legacy_ai_diagnosis_enabled,
            routing_enabled=legacy_ai_diagnosis_enabled,
            execution_enabled=legacy_ai_diagnosis_enabled,
            maintenance_message=(
                None
                if legacy_ai_diagnosis_enabled
                else "旧 Dify AI诊断已关闭，专业诊断由 PHM Diagnosis MCP 提供。"
            ),
            priority=70,
            timeout_seconds=600,
            max_retries=2 if legacy_ai_diagnosis_enabled else 0,
            health_status=(
                AgentHealthStatus.UNKNOWN
                if legacy_ai_diagnosis_enabled
                else AgentHealthStatus.DISABLED
            ),
            metadata={
                "force_disabled": not legacy_ai_diagnosis_enabled,
                "legacy_feature": True,
                "release_gate": "LEGACY_AI_DIAGNOSIS_ENABLED",
            },
        ),
        AgentDescriptor(
            agent_id=INDUSTRIAL_DATA_AGENT_ID,
            display_name="小奥助手（暂定功能，已关闭）",
            adapter_type=AgentAdapterType.DIFY,
            description=(
                "预留的小奥助手能力入口。当前业务边界尚未确定，本版本不参与主控路由，"
                "也不允许执行。"
            ),
            public_description="暂定功能，当前处于维护关闭状态。",
            capabilities=["reserved_xiaoao_capability"],
            supported_entity_types=["none", "area", "equipment", "point"],
            requires_data_access_token=True,
            supports_streaming=True,
            enabled=legacy_xiaoao_enabled,
            routing_enabled=legacy_xiaoao_enabled,
            execution_enabled=legacy_xiaoao_enabled,
            maintenance_message=(
                None
                if legacy_xiaoao_enabled
                else "小奥助手为暂定功能，当前已关闭，待业务方案明确后再启用。"
            ),
            priority=900,
            timeout_seconds=600,
            max_retries=2 if legacy_xiaoao_enabled else 0,
            health_status=(
                AgentHealthStatus.UNKNOWN
                if legacy_xiaoao_enabled
                else AgentHealthStatus.DISABLED
            ),
            metadata={
                "force_disabled": not legacy_xiaoao_enabled,
                "reserved_feature": True,
                "release_gate": "LEGACY_XIAOAO_ENABLED",
            },
        ),
        AgentDescriptor(
            agent_id=GENERAL_CONTENT_AGENT_ID,
            display_name="通用内容分析智能体",
            adapter_type=AgentAdapterType.BUILTIN,
            description="回答平台能力、普通知识和现有专业能力之外的问题，并分析图片和常见文件。",
            public_description="处理通用问答。也可以平台使用说明、图片、PDF、文档、表格和代码分析。",
            capabilities=[
                "platform_capability_answer", "platform_usage_answer", "general_knowledge",
                "text_reasoning", "image_understanding", "pdf_understanding",
                "document_understanding", "presentation_understanding",
                "spreadsheet_inspection", "code_analysis", "file_analysis",
            ],
            supported_entity_types=["none", "area", "equipment", "point"],
            requires_data_access_token=False,
            supports_attachments=True,
            supported_attachment_kinds=["image", "pdf", "document", "spreadsheet", "text"],
            supports_vision=True,
            priority=200,
            timeout_seconds=300,
            max_retries=2,
            health_status=AgentHealthStatus.HEALTHY,
        ),
    ]
