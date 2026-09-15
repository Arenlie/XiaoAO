from __future__ import annotations

import logging

import anyio
from contextlib import asynccontextmanager
import uuid
from typing import Any, Literal

from pydantic import ValidationError

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import Settings
from app.db import DatabaseManager
from app.errors import AssetError, DatabaseError
from app.providers.embedding import EmbeddingProvider
from app.providers.llm import LlmProvider
from app.providers.reranker import RerankProvider
from app.repositories.catalog_repository import CatalogRepository
from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.point_repository import PointRepository
from app.repositories.space_repository import SpaceRepository
from app.repositories.semantic_repository import SemanticRepository
from app.schemas.equipment import QueryDevicesRequest, QueryEquipmentInfoRequest
from app.schemas.collection import QueryScopeCollectionRequest
from app.schemas.point import QueryPointsRequest
from app.schemas.resolve import ResolveEntityRequest
from app.schemas.space import QuerySpaceChildrenRequest, QuerySpaceTreeRequest
from app.services.asset_query_service import AssetQueryService
from app.services.entity_resolver import EntityResolver
from app.services.query_understanding import QueryUnderstandingService
from app.performance import attach_trace, instrument_object, performance_request
from app.providers.sensor_agent import SensorAgentClient
from app.services.sensor_query_service import SensorQueryService
from app.mcp.sensor_tools import register_sensor_tools
from app.asset_query_contract import QueryError
from app.services.asset_collection_engine import AssetCollectionEngine

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = DatabaseManager(settings)
        self.embedding = EmbeddingProvider(settings)
        self.reranker = RerankProvider(settings)
        self.llm = LlmProvider(settings)
        self.catalog = CatalogRepository(self.db, settings)
        self.spaces = SpaceRepository(self.db, settings)
        self.equipment = EquipmentRepository(self.db, settings, self.spaces)
        self.points = PointRepository(self.db, settings, self.equipment)
        self.semantic = SemanticRepository(self.db, settings, self.spaces)
        self.understanding = QueryUnderstandingService(self.llm)
        self.resolver = EntityResolver(settings, self.catalog, self.understanding, self.embedding, self.reranker)
        self.assets = AssetQueryService(settings, self.spaces, self.equipment, self.points, self.llm, self.semantic)
        self.collections = AssetCollectionEngine(self.db, settings, self.llm)
        if settings.asset_unified_query_enabled:
            self.assets.collection_engine = self.collections
        self.sensor_client = SensorAgentClient(settings)
        self.sensors = SensorQueryService(settings, self.sensor_client, self.points, self.catalog)
        instrument_object(self.embedding, component_code="embedding", component_cn="Embedding 模型", category="embedding")
        instrument_object(self.reranker, component_code="reranker", component_cn="Reranker 模型", category="reranker")
        instrument_object(self.llm, component_code="asset_llm", component_cn="Asset 语义大模型", category="llm")
        instrument_object(self.catalog, component_code="catalog_db", component_cn="资产目录 PostgreSQL", category="database")
        instrument_object(self.spaces, component_code="space_db", component_cn="空间层级 PostgreSQL", category="database")
        instrument_object(self.equipment, component_code="equipment_db", component_cn="设备 PostgreSQL", category="database")
        instrument_object(self.points, component_code="point_db", component_cn="测点 PostgreSQL", category="database")
        instrument_object(self.semantic, component_code="semantic_db", component_cn="资产语义标签 PostgreSQL", category="database")


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, QueryError):
        return {"success": False, "status": exc.code, "message": exc.message,
                "error": {"code": exc.code, "public_message": exc.message,
                          "component": "phm-asset-mcp", "retryable": False}}
    if isinstance(exc, ValidationError):
        operator = str(exc)
        return {
            "success": False,
            "status": "INVALID_ARGUMENT",
            "message": "请求参数不完整或格式不正确。",
            "details": exc.errors(include_url=False),
            "error": {
                "code": "INVALID_ARGUMENT",
                "public_message": "请求参数不完整或格式不正确。",
                "operator_message": operator,
                "component": "phm-asset-mcp",
                "retryable": False,
            },
        }
    if isinstance(exc, AssetError):
        return {
            "success": False,
            "status": exc.code.value,
            "message": exc.message,
            "error": {
                "code": exc.code.value,
                "public_message": exc.message,
                "operator_message": exc.operator_message or exc.message,
                "component": "phm-asset-mcp",
                "retryable": exc.code.value in {"DATABASE_ERROR", "UPSTREAM_ERROR"},
            },
        }
    logger.exception("unhandled_asset_mcp_error")
    return {
        "success": False,
        "status": "INTERNAL_ERROR",
        "message": "资产服务暂时无法完成本次查询。",
        "error": {
            "code": "INTERNAL_ERROR",
            "public_message": "资产服务暂时无法完成本次查询。",
            "operator_message": f"{type(exc).__name__}: {exc}",
            "component": "phm-asset-mcp",
            "retryable": False,
        },
    }



async def _run_traced(runtime: Runtime, tool_name: str, func):
    with performance_request(runtime.settings, "phm-asset-mcp", tool_name) as perf:
        try:
            value = func()
            if hasattr(value, "__await__"):
                value = await value
            return attach_trace(value, perf)
        except Exception as exc:
            return attach_trace(_error(exc), perf)


class AssetFastMCP(FastMCP):
    """Own shared clients at the ASGI application lifetime, never a protocol request.

    MCP SDK 1.x invokes FastMCP's protocol lifespan for every stateless HTTP
    request, including initialize/list_tools. Closing a shared Runtime there
    invalidates clients used by subsequent and concurrent requests.
    """

    def __init__(self, asset_settings: Settings, **kwargs):
        self.asset_settings = asset_settings
        self._phm_runtime: Runtime | None = None
        super().__init__(**kwargs)

    @property
    def runtime(self) -> Runtime:
        if self._phm_runtime is None:
            raise RuntimeError("Asset application runtime is not started")
        return self._phm_runtime

    def streamable_http_app(self):
        app = super().streamable_http_app()
        sdk_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def application_lifespan(application):
            if self._phm_runtime is not None:
                raise RuntimeError("Asset application runtime is already running")
            runtime = Runtime(self.asset_settings)
            self._phm_runtime = runtime
            logger.info("asset_runtime_started", extra={"fields": {"resource_lifecycle": "application"}})
            try:
                if self.asset_settings.asset_unified_query_enabled:
                    await runtime.collections.migrate()
                # Session manager stops/drains before shared resources are closed.
                async with sdk_lifespan(application) as context:
                    yield context
            finally:
                with anyio.CancelScope(shield=True):
                    for name in ("sensors", "sensor_client", "llm", "embedding", "reranker", "db"):
                        try:
                            await getattr(runtime, name).close()
                        except Exception:
                            logger.exception("asset_runtime_close_failed", extra={"fields": {"resource": name}})
                    self._phm_runtime = None
                    logger.info("asset_runtime_stopped")

        app.router.lifespan_context = application_lifespan
        return app


def build_mcp(settings: Settings) -> FastMCP:
    mcp = AssetFastMCP(
        asset_settings=settings,
        name=settings.app_name,
        instructions="PHM asset/entity/hierarchy service. Reviewed offline semantic names/tags are queried directly from PostgreSQL; ambiguous natural-language entity recall falls back to the configured Asset LLM, pgvector and reranker. Runtime models never create asset facts.",
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.mcp_path,
        stateless_http=True,
        json_response=True,
    )
    register_sensor_tools(mcp, _run_traced)

    @mcp.tool(name="query_asset_collection", description="按真实区域或受信任对话服务授权的共享资产目录，以及明确的类别或名称条件统一查询设备数量、清单、分类。支持原集合追问。调用上下文由受信任的对话服务签发。")
    async def query_asset_collection(query: dict[str, Any], request_context: str) -> dict[str, Any]:
        runtime = mcp.runtime
        return await _run_traced(runtime, "query_asset_collection", lambda: runtime.collections.query(query, request_context))

    @mcp.tool(name="resolve_entity", description="Resolve a natural-language PHM space/equipment/point description to PostgreSQL-backed asset entities.")
    async def resolve_entity(
        query: str,
        required_entity_level: Literal["any", "space", "area", "line", "equipment", "point"] = "any",
        active_entity: dict[str, Any] | None = None,
        user_profile: dict[str, Any] | None = None,
        previous_resolution: dict[str, Any] | None = None,
        conversation_context: dict[str, Any] | None = None,
        semantic_hints: dict[str, Any] | None = None,
        force_refresh: bool = False,
        allow_context_reuse: bool = True,
        limit: int = 10,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = ResolveEntityRequest(
                query=query, required_entity_level=required_entity_level, active_entity=active_entity,
                user_profile=user_profile, previous_resolution=previous_resolution,
                conversation_context=conversation_context, semantic_hints=semantic_hints,
                force_refresh=force_refresh, allow_context_reuse=allow_context_reuse, limit=limit,
            )
            return (await runtime.resolver.resolve(request, request_id or str(uuid.uuid4()))).model_dump()
        return await _run_traced(runtime, "resolve_entity", operation)

    @mcp.tool(name="query_equipment_info", description="Return authoritative equipment identity, type and hierarchy metadata by real equipment code.")
    async def query_equipment_info(
        equip_no: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QueryEquipmentInfoRequest(equip_no=equip_no)
            return await runtime.assets.query_equipment_info(request, request_id or str(uuid.uuid4()))
        return await _run_traced(runtime, "query_equipment_info", operation)

    @mcp.tool(name="query_space_tree", description="Return a deterministic complete PHM space subtree as flat nodes with parent/depth/path; optionally append equipment and points.")
    async def query_space_tree(
        root_space_id: str,
        max_depth: int = 10,
        include_devices: bool = False,
        include_points: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QuerySpaceTreeRequest(root_space_id=root_space_id, max_depth=max_depth, include_devices=include_devices, include_points=include_points)
            return await runtime.assets.query_space_tree(request, request_id or str(uuid.uuid4()))
        return await _run_traced(runtime, "query_space_tree", operation)

    @mcp.tool(name="query_space_children", description="List direct children of a PHM space, or descendants when recursive=true.")
    async def query_space_children(
        space_id: str,
        child_type: str | None = None,
        recursive: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QuerySpaceChildrenRequest(space_id=space_id, child_type=child_type, recursive=recursive)
            return await runtime.assets.query_space_children(request, request_id or str(uuid.uuid4()))
        return await _run_traced(runtime, "query_space_children", operation)

    @mcp.tool(name="query_devices", description="Query equipment under a space, optionally recursively and by equipment keyword.")
    async def query_devices(
        space_id: str,
        recursive: bool = True,
        keyword: str | None = None,
        limit: int = 1000,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QueryDevicesRequest(space_id=space_id, recursive=recursive, keyword=keyword, limit=limit)
            return await runtime.assets.query_devices(request, request_id or str(uuid.uuid4()))
        return await _run_traced(runtime, "query_devices", operation)

    @mcp.tool(name="query_scope_collection", description="Return all real descendant entities of a requested level below one resolved space; language intent must already be structured by the caller/model.")
    async def query_scope_collection(
        root_space_id: str,
        target_entity_level: Literal["space", "equipment", "point"],
        target_space_type: str | None = None,
        target_equipment_type: str | None = None,
        semantic_filters: list[str] | None = None,
        output_mode: Literal["list", "count"] = "list",
        recursive: bool = True,
        limit: int = 50000,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QueryScopeCollectionRequest(
                root_space_id=root_space_id,
                target_entity_level=target_entity_level,
                target_space_type=target_space_type,
                target_equipment_type=target_equipment_type,
                semantic_filters=semantic_filters or [],
                output_mode=output_mode,
                recursive=recursive,
                limit=limit,
            )
            return await runtime.assets.query_scope_collection(
                request, request_id or str(uuid.uuid4())
            )
        return await _run_traced(runtime, "query_scope_collection", operation)

    @mcp.tool(name="query_points", description="Query real points under an equipment code; supports vibration_acceleration/vibration/temperature filters without point-number suffix guessing.")
    async def query_points(
        equip_no: str,
        point_type: str | None = None,
        keyword: str | None = None,
        limit: int = 1000,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        runtime = mcp.runtime
        async def operation():
            request = QueryPointsRequest(equip_no=equip_no, point_type=point_type, keyword=keyword, limit=limit)
            return await runtime.assets.query_points(request, request_id or str(uuid.uuid4()))
        return await _run_traced(runtime, "query_points", operation)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "UP",
                "service": settings.app_name,
                "version": settings.app_version,
            }
        )

    @mcp.custom_route("/ready", methods=["GET"])
    async def ready(_: Request) -> JSONResponse:
        if mcp._phm_runtime is None:
            return JSONResponse({"core_ready": False, "runtime_started": False,
                                 "resource_lifecycle": "application"}, status_code=503)
        runtime = mcp.runtime
        model_clients = {name: "CLOSED" if getattr(runtime, name).client.is_closed else "OPEN"
                         for name in ("embedding", "reranker", "llm")}
        postgres = "UP" if await runtime.db.ping() else "DOWN"
        semantic = await runtime.semantic.capability() if postgres == "UP" else {"available": False, "missing_columns": ["postgres_down"]}
        payload = {
            "postgres": postgres,
            "version": settings.app_version,
            "asset_collection": {"schema_version": "2.0", "enabled": settings.asset_unified_query_enabled,
                                 "storage_ready": await runtime.collections.ready() if settings.asset_unified_query_enabled else False,
                                 "authentication_configured": len(settings.asset_query_context_secret) >= 32},
            "sensor_queries": {"enabled": settings.sensor_agent_enabled,
                               "configured": bool(settings.sensor_agent_base_url),
                               "required_for_asset_readiness": False},
            "resource_lifecycle": "application",
            "runtime_started": True,
            "model_clients": model_clients,
            "semantic_tags": semantic,
            "embedding": "REQUIRED_CONFIGURED" if runtime.embedding.enabled else "MISSING",
            "rerank": "REQUIRED_CONFIGURED" if runtime.reranker.enabled else "MISSING",
            "llm": "REQUIRED_CONFIGURED" if runtime.llm.enabled else "MISSING",
            "hierarchy_backend": settings.asset_hierarchy_backend,
            "core_ready": bool(
                postgres == "UP"
                and runtime.embedding.enabled
                and runtime.reranker.enabled
                and runtime.llm.enabled
                and all(value == "OPEN" for value in model_clients.values())
            ),
        }
        return JSONResponse(payload, status_code=200 if payload["core_ready"] else 503)

    return mcp
