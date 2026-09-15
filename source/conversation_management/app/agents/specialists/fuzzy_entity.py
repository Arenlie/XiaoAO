from __future__ import annotations

from datetime import UTC, datetime

from app.agents.catalog import FUZZY_ENTITY_AGENT_ID, default_agent_descriptors
from app.agents.contracts import (
    AgentExecutionRuntime,
    AgentHealthResult,
    AgentHealthStatus,
    AgentRequest,
    AgentResult,
    AgentResultStatus,
)
from app.domain.enums import EntityStatus
from app.integrations.phm_asset_mcp import PhmAssetMcpClient


class FuzzyEntityAgent:
    descriptor = next(
        item for item in default_agent_descriptors() if item.agent_id == FUZZY_ENTITY_AGENT_ID
    )

    def __init__(self, client: PhmAssetMcpClient) -> None:
        self.client = client

    async def execute(
        self, request: AgentRequest, runtime: AgentExecutionRuntime
    ) -> AgentResult:
        required_level = str(getattr(request, "required_entity_level", "none") or "none").lower()
        result = await self.client.lookup(
            query=request.query,
            required_entity_level=("any" if required_level == "none" else required_level),
            active_entity=request.active_entity or request.resolved_entity or None,
            user_profile=request.user_profile or None,
            previous_resolution=request.entity_result or None,
            conversation_context={
                "summary": str((request.memory_context or {}).get("summary") or "")[:2000],
                "recent_messages": list((request.memory_context or {}).get("recent_messages") or [])[-4:],
            },
            request_id=runtime.request_id,
        )
        payload = result.model_dump(mode="json")
        status = result.status
        resolved = dict(result.resolved_entity or {})
        if status == EntityStatus.UNIQUE:
            answer = "已确定目标实体。"
            can_support = True
            missing: list[str] = []
        elif status == EntityStatus.NO_LOOKUP:
            answer = "该问题不需要企业实体编码。"
            can_support = True
            missing = []
        elif status == EntityStatus.MULTIPLE:
            answer = "需要用户从候选实体中选择目标。"
            can_support = False
            missing = ["ENTITY_SELECTION_REQUIRED"]
        elif status == EntityStatus.COLLECTION:
            answer = f"已按集合模式定位 {result.match_count} 个目标实体。"
            can_support = True
            missing = []
        elif status == EntityStatus.NOT_FOUND:
            answer = "未找到可信的区域、设备或测点对应关系。"
            can_support = False
            missing = ["更完整的厂区、设备、部件或测点名称"]
        else:
            answer = result.message or "实体检索未能完成。"
            can_support = False
            missing = ["可用的实体检索结果"]
        return AgentResult(
            agent_id=self.descriptor.agent_id,
            status=(
                AgentResultStatus.NEEDS_INPUT
                if status == EntityStatus.MULTIPLE
                else AgentResultStatus.COMPLETED
            ),
            answer_markdown=answer,
            evidence=[{"source_type": "phm_asset_mcp", "result": payload}],
            missing_information=missing,
            can_support_final_answer=can_support,
            state_updates={
                "entity_result": payload,
                "resolved_entity": resolved,
                "query_scope": dict(result.query_scope or {}),
                "entity_constraints": dict(result.entity_constraints or {}),
            },
            external_ids={},
            execution_summary={
                "status": status.value,
                "match_count": result.match_count,
                "top_similarity": result.top_similarity,
            },
        )

    async def health_check(self) -> AgentHealthResult:
        configured = bool(self.client.url)
        return AgentHealthResult(
            agent_id=self.descriptor.agent_id,
            status=AgentHealthStatus.HEALTHY if configured else AgentHealthStatus.UNHEALTHY,
            message="PHM Asset MCP 实体解析已配置" if configured else "PHM Asset MCP 地址缺失",
            checked_at=datetime.now(UTC).isoformat(),
        )
