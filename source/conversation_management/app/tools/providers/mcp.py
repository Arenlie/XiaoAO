from __future__ import annotations

from typing import Any

from app.integrations.phm_asset_mcp import PhmAssetMcpClient, PhmAssetMcpError
from app.integrations.phm_data_mcp import PhmDataMcpClient, PhmDataMcpError
from app.integrations.phm_diagnosis_mcp import PhmDiagnosisMcpClient, PhmDiagnosisMcpError
from app.integrations.phm_feature_mcp import PhmFeatureMcpClient, PhmFeatureMcpError
from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor, ToolResultStatus
from app.asset_query_contract import QueryError


class MCPToolProvider:
    """MCP provider for PHM Asset, Data, Diagnosis and Feature services."""

    def __init__(
        self,
        *,
        phm_asset_client: PhmAssetMcpClient,
        phm_data_client: PhmDataMcpClient,
        phm_diagnosis_client: PhmDiagnosisMcpClient,
        phm_feature_client: PhmFeatureMcpClient,
    ) -> None:
        self.clients: dict[str, Any] = {
            "phm_asset": phm_asset_client,
            "phm_data": phm_data_client,
            "phm_diagnosis": phm_diagnosis_client,
            "phm_feature": phm_feature_client,
        }

    async def call(
        self,
        descriptor: ToolDescriptor,
        request: ToolCallRequest,
        *,
        data_access_token: str | None,
    ) -> ToolCallResult:
        del data_access_token
        profile = str(descriptor.metadata.get("config_profile") or "").strip()
        client = self.clients.get(profile)
        if client is None:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code="MCP_PROFILE_UNSUPPORTED",
                error_message=f"未配置 MCP profile: {profile or '<empty>'}",
            )
        server_tool_name = str(
            descriptor.metadata.get("server_tool_name") or descriptor.tool_id
        )
        try:
            arguments = dict(request.arguments)
            from app.asset_query_contract import TOOL_ID as COLLECTION_TOOL_ID
            if descriptor.tool_id == COLLECTION_TOOL_ID:
                from app.orchestration.runtime import current_graph_runtime
                from app.services.asset_collections import signed_arguments
                arguments = signed_arguments(request, current_graph_runtime().agent_runtime)
            payload = await client.call_tool(
                server_tool_name,
                arguments,
            )
            if descriptor.tool_id == COLLECTION_TOOL_ID:
                from app.services.asset_collections import validate_response
                validate_response(arguments["query"], payload)
        except (PhmAssetMcpError, PhmDataMcpError, PhmDiagnosisMcpError, PhmFeatureMcpError, QueryError) as exc:
            return ToolCallResult(
                tool_id=descriptor.tool_id,
                status=ToolResultStatus.FAILED,
                error_code=exc.code,
                error_message=exc.message,
                metadata={
                    "server_tool_name": server_tool_name,
                    "config_profile": profile,
                },
            )
        return ToolCallResult(
            tool_id=descriptor.tool_id,
            status=ToolResultStatus.SUCCESS,
            content=None,
            structured_content=payload,
            metadata={
                "server_tool_name": server_tool_name,
                "config_profile": profile,
                "transport": "streamable_http",
            },
        )
