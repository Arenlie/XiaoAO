from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.asr.service import AsrService
from app.asr.realtime import DashScopeRealtimeAsrProxy

from app.agents.adapters.dify import DifyAIDiagnosisAdapter, DifyIndustrialDataAdapter
from app.agents.builtin.general_content import GeneralContentAgent
from app.agents.registry import AgentAdapterRegistry
from app.agents.specialists.fuzzy_entity import FuzzyEntityAgent
from app.attachments.service import AttachmentService
from app.attachments.storage import create_attachment_storage
from app.config import Settings, get_settings
from app.content.parser_registry import ContentParserRegistry
from app.content.understanding_service import ContentUnderstandingService
from app.database import Database
from app.execution.resilient_executor import ResilientAgentExecutor
from app.evidence.broker import EvidenceBroker
from app.evidence.adapters.asset_snapshot import AssetSnapshotBackend
from app.evidence.repository import EvidenceRepository
from app.execution.resilient_tool_executor import ResilientToolExecutor
from app.integrations.dify.agent_client import DifyAgentClient
from app.integrations.dify.title_client import TitleClient
from app.integrations.openai.chat_client import OpenAICompatibleChatClient
from app.integrations.phm_asset_mcp import PhmAssetMcpClient
from app.integrations.phm_data_mcp import PhmDataMcpClient
from app.integrations.phm_diagnosis_mcp import PhmDiagnosisMcpClient
from app.integrations.phm_feature_mcp import PhmFeatureMcpClient
from app.orchestration.supervisor.agent import SupervisorAgent
from app.orchestration.entity_resolution_layer import UnifiedEntityResolutionLayer
from app.reasoning.model_registry import ModelRegistry
from app.redis import RedisManager
from app.repositories import (
    AgentRegistryRepository,
    AttachmentRepository,
    ConversationRepository,
    ContentRepository,
    EventRepository,
    MessageRepository,
    ProfileRepository,
    TaskRepository,
)
from app.repositories.outbox_repository import OutboxRepository
from app.services.agent_registry_service import AgentRegistryService
from app.services.concurrency_service import ConcurrencyService
from app.services.context_builder import ContextBuilder
from app.services.conversation_service import ConversationService
from app.services.event_service import EventService
from app.services.evidence_workspace import EvidenceWorkspaceService
from app.services.generation_service import GenerationService
from app.services.outbox_service import OutboxService
from app.services.profile_service import ProfileService
from app.services.query_statistics_service import QueryStatisticsService
from app.services.queue_service import QueueService
from app.services.task_service import TaskService
from app.telemetry import instrument_clients, shutdown_tracing
from app.tools.builtin_file_tools import (
    FILE_SEARCH_TOOL_ID,
    SPREADSHEET_AGGREGATE_TOOL_ID,
    SPREADSHEET_FILTER_TOOL_ID,
    SPREADSHEET_INSPECT_TOOL_ID,
    SPREADSHEET_READ_RANGE_TOOL_ID,
    FileAndSpreadsheetToolHandlers,
    default_file_tool_descriptors,
)
from app.tools.comprehensive_alarm import comprehensive_alarm_tool_descriptor
from app.tools.contracts import ToolProviderType
from app.tools.phm_asset_mcp import phm_asset_tool_descriptors
from app.tools.phm_sensor_mcp import phm_sensor_tool_descriptors
from app.tools.phm_data_mcp import phm_data_tool_descriptors
from app.tools.phm_diagnosis_mcp import phm_diagnosis_tool_descriptors
from app.tools.phm_feature_mcp import phm_feature_tool_descriptors
from app.tools.phm_workflow_tools import (
    PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
    PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
    PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
    PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
    PhmWorkflowToolHandlers,
    phm_workflow_tool_descriptors,
)
from app.tools.providers.dify_workflow import DifyWorkflowToolProvider
from app.tools.providers.http import HttpToolProvider
from app.tools.providers.local import LocalToolProvider
from app.tools.providers.mcp import MCPToolProvider
from app.tools.registry import ToolRegistry
from app.tools.dify_knowledge import KNOWLEDGE_TOOL_ID, DifyKnowledgeHandler, knowledge_descriptor
from app.tools.evidence_broker import EVIDENCE_BROKER_TOOL_ID, EvidenceBrokerToolHandler, evidence_broker_descriptor
from app.tools.independent_queries import MULTI_QUERY_TOOL_ID, IndependentQueryHandler, multi_query_descriptor
from app.workflows import BusinessWorkflowRegistry


@dataclass(slots=True)
class AppContainer:
    settings: Settings
    database: Database
    redis_manager: RedisManager
    http_client: httpx.AsyncClient
    conversation_service: ConversationService
    generation_service: GenerationService
    task_service: TaskService
    profile_service: ProfileService
    query_statistics_service: QueryStatisticsService
    events: EventService
    queue: QueueService
    concurrency: ConcurrencyService
    outbox: OutboxService
    agent_client: DifyAgentClient
    title_client: TitleClient
    asr_service: AsrService
    asr_realtime_proxy: DashScopeRealtimeAsrProxy
    phm_asset_mcp_client: PhmAssetMcpClient
    phm_data_mcp_client: PhmDataMcpClient
    phm_diagnosis_mcp_client: PhmDiagnosisMcpClient
    phm_feature_mcp_client: PhmFeatureMcpClient
    context_builder: ContextBuilder
    evidence_workspace_service: EvidenceWorkspaceService
    agent_registry_service: AgentRegistryService
    agent_adapter_registry: AgentAdapterRegistry
    agent_executor: ResilientAgentExecutor
    supervisor_agent: SupervisorAgent
    tool_registry: ToolRegistry
    tool_executor: ResilientToolExecutor
    workflow_registry: BusinessWorkflowRegistry
    entity_resolution_layer: UnifiedEntityResolutionLayer
    attachment_service: AttachmentService
    content_understanding_service: ContentUnderstandingService
    model_registry: ModelRegistry

    async def close(self) -> None:
        await self.http_client.aclose()
        await self.redis_manager.close()
        await self.database.close()
        shutdown_tracing()


def create_container(settings: Settings | None = None) -> AppContainer:
    settings = settings or get_settings()
    database = Database(settings)
    redis_manager = RedisManager(settings)
    limits = httpx.Limits(
        max_connections=settings.agent_max_connections,
        max_keepalive_connections=settings.agent_max_keepalive_connections,
        keepalive_expiry=settings.agent_keepalive_expiry_seconds,
    )
    http_client = httpx.AsyncClient(
        limits=limits,
        http2=settings.agent_http2,
        follow_redirects=False,
        headers={"User-Agent": f"{settings.app_name}/{settings.app_version}"},
    )
    instrument_clients(settings=settings, sqlalchemy_engine=database.engine)

    redis = redis_manager.client
    conversation_repo = ConversationRepository()
    message_repo = MessageRepository()
    task_repo = TaskRepository()
    profile_repo = ProfileRepository()
    outbox_repo = OutboxRepository()
    event_repo = EventRepository()
    agent_registry_repo = AgentRegistryRepository()
    attachment_repo = AttachmentRepository()

    events = EventService(redis, settings, database.session_factory, event_repo)
    queue = QueueService(redis)
    concurrency = ConcurrencyService(redis, settings)
    context_builder = ContextBuilder(settings, message_repo)
    evidence_workspace_service = EvidenceWorkspaceService(
        session_factory=database.session_factory, settings=settings
    )
    evidence_broker = EvidenceBroker(repository=EvidenceRepository(), settings=settings)
    agent_client = DifyAgentClient(http_client, settings)
    title_client = TitleClient(http_client, settings)
    asr_service = AsrService(http_client, settings)
    asr_realtime_proxy = DashScopeRealtimeAsrProxy(settings)
    phm_asset_mcp_client = PhmAssetMcpClient(
        url=settings.phm_asset_mcp_url,
        timeout_seconds=settings.phm_asset_mcp_timeout_seconds,
        http_client=http_client,
    )
    phm_data_mcp_client = PhmDataMcpClient(
        url=settings.phm_data_mcp_url,
        timeout_seconds=settings.phm_data_mcp_timeout_seconds,
        device_timeout_seconds=getattr(
            settings, "phm_data_mcp_device_timeout_seconds", settings.phm_data_mcp_timeout_seconds
        ),
        http_client=http_client,
        close_timeout_seconds=getattr(settings, "phm_data_mcp_close_timeout_seconds", 5.0),
    )
    phm_diagnosis_mcp_client = PhmDiagnosisMcpClient(
        url=settings.phm_diagnosis_mcp_url,
        timeout_seconds=settings.phm_diagnosis_mcp_timeout_seconds,
        device_timeout_seconds=getattr(
            settings, "phm_diagnosis_mcp_device_timeout_seconds", settings.phm_diagnosis_mcp_timeout_seconds
        ),
        http_client=http_client,
    )
    phm_feature_mcp_client = PhmFeatureMcpClient(
        url=settings.phm_feature_mcp_url,
        feature_timeout_seconds=settings.phm_feature_mcp_feature_timeout_seconds,
        rpm_timeout_seconds=settings.phm_feature_mcp_rpm_timeout_seconds,
    )

    agent_registry_service = AgentRegistryService(
        database.session_factory,
        redis,
        settings.agent_registry_cache_ttl_seconds,
        agent_registry_repo,
        legacy_ai_diagnosis_enabled=settings.legacy_ai_diagnosis_enabled,
        legacy_xiaoao_enabled=settings.legacy_xiaoao_enabled,
    )
    attachment_storage = create_attachment_storage(settings)
    attachment_service = AttachmentService(
        session_factory=database.session_factory,
        settings=settings,
        repository=attachment_repo,
        storage=attachment_storage,
    )
    content_understanding_service = ContentUnderstandingService(
        session_factory=database.session_factory,
        attachment_service=attachment_service,
        parser_registry=ContentParserRegistry(),
        repository=ContentRepository(),
        quick_max_characters=settings.file_quick_max_characters,
        standard_max_characters=settings.file_standard_max_characters,
        context_max_characters_per_attachment=settings.content_understanding_max_characters_per_attachment,
        context_max_total_characters=settings.content_understanding_max_total_characters,
        chunk_size=settings.file_retrieval_chunk_size,
        chunk_overlap=settings.file_retrieval_chunk_overlap,
    )

    model_client = OpenAICompatibleChatClient(http_client, settings)
    model_registry = ModelRegistry(settings)
    supervisor_agent = SupervisorAgent(
        model_client=model_client,
        model_registry=model_registry,
        settings=settings,
    )

    agent_adapter_registry = AgentAdapterRegistry()
    # Entity lookup is now owned by PHM Asset MCP. The legacy Dify entity client is
    # retained only for rollback/compatibility and is not registered on the main path.
    agent_adapter_registry.register(FuzzyEntityAgent(phm_asset_mcp_client))
    if settings.legacy_ai_diagnosis_enabled:
        agent_adapter_registry.register(DifyAIDiagnosisAdapter(agent_client, context_builder))
    if settings.legacy_xiaoao_enabled:
        agent_adapter_registry.register(
            DifyIndustrialDataAdapter(agent_client, context_builder)
        )
    # With the default LEGACY_XIAOAO_ENABLED=false, the catalog row is force-disabled
    # and no adapter is registered, so stale runtime DB rows cannot reactivate it.
    agent_adapter_registry.register(
        GeneralContentAgent(
            model_client=model_client,
            model_registry=model_registry,
            attachment_service=attachment_service,
        )
    )
    agent_executor = ResilientAgentExecutor(agent_adapter_registry, settings)

    tool_registry = ToolRegistry()
    tool_registry.register_provider(ToolProviderType.HTTP, HttpToolProvider(http_client))
    tool_registry.register_provider(
        ToolProviderType.DIFY_WORKFLOW, DifyWorkflowToolProvider(http_client, settings)
    )
    local_tool_provider = LocalToolProvider()
    local_tool_provider.register(
        EVIDENCE_BROKER_TOOL_ID,
        EvidenceBrokerToolHandler(
            session_factory=database.session_factory, broker=evidence_broker,
            asset_snapshot_backend=AssetSnapshotBackend(client=phm_asset_mcp_client, settings=settings),
        ),
    )
    tool_registry.register_tool(evidence_broker_descriptor(settings))
    local_tool_provider.register(KNOWLEDGE_TOOL_ID, DifyKnowledgeHandler(http_client, settings))
    tool_registry.register_tool(knowledge_descriptor(settings))
    local_tool_provider.register(MULTI_QUERY_TOOL_ID, IndependentQueryHandler(phm_asset_mcp_client, phm_data_mcp_client, settings))
    tool_registry.register_tool(multi_query_descriptor(settings))
    local_tool_handlers = FileAndSpreadsheetToolHandlers(
        attachment_service=attachment_service,
        content_service=content_understanding_service,
        max_scan_rows=settings.spreadsheet_tool_max_scan_rows,
    )
    local_tool_provider.register(FILE_SEARCH_TOOL_ID, local_tool_handlers.file_search)
    local_tool_provider.register(
        SPREADSHEET_INSPECT_TOOL_ID, local_tool_handlers.spreadsheet_inspect
    )
    local_tool_provider.register(
        SPREADSHEET_READ_RANGE_TOOL_ID, local_tool_handlers.spreadsheet_read_range
    )
    local_tool_provider.register(
        SPREADSHEET_FILTER_TOOL_ID, local_tool_handlers.spreadsheet_filter
    )
    local_tool_provider.register(
        SPREADSHEET_AGGREGATE_TOOL_ID, local_tool_handlers.spreadsheet_aggregate
    )
    from app.tools.structured_query import StructuredQueryHandler, descriptors as query_descriptors
    query_handler = StructuredQueryHandler(phm_asset_mcp_client, phm_data_mcp_client, settings, model_client, model_registry)
    for descriptor in query_descriptors(settings):
        local_tool_provider.register(descriptor.tool_id, query_handler.call)
        tool_registry.register_tool(descriptor)
    phm_workflow_handlers = PhmWorkflowToolHandlers(
        data_client=phm_data_mcp_client,
        feature_client=phm_feature_mcp_client,
        diagnosis_client=phm_diagnosis_mcp_client,
        max_parallel=settings.normal_max_parallel_calls,
        feature_max_parallel=settings.phm_feature_batch_max_parallel,
        feature_queue_retry_attempts=settings.phm_feature_queue_retry_attempts,
        feature_queue_retry_delay_seconds=settings.phm_feature_queue_retry_delay_seconds,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_HEALTH_DIMENSIONS_TOOL_ID,
        phm_workflow_handlers.health_dimensions,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_HEALTH_ALARMS_TOOL_ID,
        phm_workflow_handlers.health_dimension_alarms,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_HEALTH_COLLECTION_BATCH_TOOL_ID,
        phm_workflow_handlers.health_collection_batch,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_POINT_DATA_BATCH_TOOL_ID,
        phm_workflow_handlers.point_data_batch,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_POINT_RPM_BATCH_TOOL_ID,
        phm_workflow_handlers.point_rpm_batch,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_POINT_DIAGNOSIS_BATCH_TOOL_ID,
        phm_workflow_handlers.point_diagnosis_batch,
    )
    local_tool_provider.register(
        PHM_WORKFLOW_RAG_STRUCTURE_STATUS_TOOL_ID,
        phm_workflow_handlers.rag_structure_status,
    )
    tool_registry.register_provider(ToolProviderType.LOCAL, local_tool_provider)
    tool_registry.register_provider(
        ToolProviderType.MCP,
        MCPToolProvider(
            phm_asset_client=phm_asset_mcp_client,
            phm_data_client=phm_data_mcp_client,
            phm_diagnosis_client=phm_diagnosis_mcp_client,
            phm_feature_client=phm_feature_mcp_client,
        ),
    )
    from app.tools.raw_file_tools import RawFileTools, descriptors as raw_file_descriptors
    raw_files = RawFileTools(attachment_service)
    for descriptor in raw_file_descriptors():
        local_tool_provider.register(descriptor.tool_id, raw_files.execute)
        tool_registry.register_tool(descriptor)
    for descriptor in default_file_tool_descriptors():
        tool_registry.register_tool(descriptor)
    for descriptor in phm_asset_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    for descriptor in phm_sensor_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    for descriptor in phm_data_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    for descriptor in phm_diagnosis_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    for descriptor in phm_feature_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    for descriptor in phm_workflow_tool_descriptors(settings):
        tool_registry.register_tool(descriptor)
    # Retain the old Dify alarm Workflow only as an explicit rollback path. It is
    # disabled by default and is not visible to Supervisor Function Calling when off.
    if settings.comprehensive_alarm_workflow_enabled:
        tool_registry.register_tool(comprehensive_alarm_tool_descriptor(settings))
    tool_executor = ResilientToolExecutor(tool_registry, settings)
    workflow_registry = BusinessWorkflowRegistry()
    entity_resolution_layer = UnifiedEntityResolutionLayer(phm_asset_mcp_client)

    outbox = OutboxService(
        session_factory=database.session_factory,
        redis=redis,
        settings=settings,
        queue=queue,
        events=events,
        repository=outbox_repo,
        concurrency=concurrency,
    )
    conversation_service = ConversationService(
        database.session_factory,
        redis,
        settings,
        conversation_repo,
        message_repo,
        queue,
        outbox,
        events=events,
    )
    generation_service = GenerationService(
        session_factory=database.session_factory,
        redis=redis,
        settings=settings,
        conversation_repo=conversation_repo,
        message_repo=message_repo,
        events=events,
        queue=queue,
        concurrency=concurrency,
        outbox=outbox,
        attachment_service=attachment_service,
    )
    task_service = TaskService(
        database.session_factory,
        redis,
        settings,
        task_repo,
        events,
        queue,
        concurrency,
        agent_client,
        outbox,
    )
    profile_service = ProfileService(database.session_factory, profile_repo)
    query_statistics_service = QueryStatisticsService(database.session_factory)

    return AppContainer(
        settings=settings,
        database=database,
        redis_manager=redis_manager,
        http_client=http_client,
        conversation_service=conversation_service,
        generation_service=generation_service,
        task_service=task_service,
        profile_service=profile_service,
        query_statistics_service=query_statistics_service,
        events=events,
        queue=queue,
        concurrency=concurrency,
        outbox=outbox,
        agent_client=agent_client,
        title_client=title_client,
        asr_service=asr_service,
        asr_realtime_proxy=asr_realtime_proxy,
        phm_asset_mcp_client=phm_asset_mcp_client,
        phm_data_mcp_client=phm_data_mcp_client,
        phm_diagnosis_mcp_client=phm_diagnosis_mcp_client,
        phm_feature_mcp_client=phm_feature_mcp_client,
        context_builder=context_builder,
        evidence_workspace_service=evidence_workspace_service,
        agent_registry_service=agent_registry_service,
        agent_adapter_registry=agent_adapter_registry,
        agent_executor=agent_executor,
        supervisor_agent=supervisor_agent,
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        workflow_registry=workflow_registry,
        entity_resolution_layer=entity_resolution_layer,
        attachment_service=attachment_service,
        content_understanding_service=content_understanding_service,
        model_registry=model_registry,
    )
