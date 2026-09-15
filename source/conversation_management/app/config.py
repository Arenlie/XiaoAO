from __future__ import annotations

from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "conversation-backend"
    # Release identity is code-owned.  A preserved .env from an older deployment must
    # never make the health/version checks report the previous release.
    app_version: ClassVar[str] = "1.7.0"
    app_env: Literal["development", "test", "production"] = "production"
    app_host: str = "0.0.0.0"
    app_port: int = 8032
    # Authoritative business/runtime timezone used when injecting the live server clock into LLM calls.
    business_timezone: str = "Asia/Shanghai"
    api_prefix: Literal["/chat/v1"] = "/chat/v1"

    log_level: str = "INFO"
    log_dir: str = "./logs"
    log_file_enabled: bool = True
    log_console_enabled: bool = True
    log_max_bytes: int = 50 * 1024 * 1024
    log_backup_count: int = 10

    # Detailed performance tracing. Disabled in production when minimum overhead is preferred.
    performance_monitor_enabled: bool = False
    # Generates a deterministic per-question performance summary from recorded spans.
    performance_report_enabled: bool = False
    performance_trace_include_mcp_details: bool = True
    performance_trace_max_mcp_spans: int = 120
    performance_trace_preview_chars: int = 500
    log_retention_days: int = 30

    default_execution_mode: Literal["quick", "normal", "expert"] = "normal"
    langgraph_checkpoint_enabled: bool = False
    langgraph_checkpoint_database_url: str | None = None
    langgraph_checkpoint_setup_on_start: bool = False

    # QuickGraph: always executes GeneralContentAgent exactly once.
    quick_model_base_url: str | None = None
    quick_model_api_key: str | None = None
    quick_model: str | None = None
    quick_vision_model: str | None = None
    quick_timeout_seconds: int = 60
    quick_enable_reasoning: bool = False
    quick_reasoning_effort: Literal["low", "medium", "high"] = "low"
    model_reasoning_parameters: Literal["auto", "enable_thinking", "omit"] = "auto"
    structured_query_enabled: bool = True
    supervisor_final_enable_reasoning: bool = False
    attachment_analysis_enabled: bool = True
    advanced_snapshot_sql_enabled: bool = True
    supervisor_enable_reasoning: bool = True
    stream_reasoning_enabled: bool = True
    answer_stream_flush_seconds: float = Field(default=0.08, ge=0.02, le=0.5)
    diagnosis_confirmation_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    reasoning_api_mode: Literal["chat_enable_thinking", "responses"] = "chat_enable_thinking"

    # Normal mode: goal-first, bounded ReAct planner.  Existing Recipes are optional
    # accelerators; dynamic MCP composition remains available for all other tasks.
    supervisor_model_base_url: str | None = None
    supervisor_model_api_key: str | None = None
    supervisor_model: str | None = None
    supervisor_timeout_seconds: int = 180
    supervisor_classification_max_attempts: int = 2
    # Task understanding is a short control decision; final/planning profiles keep
    # their existing reasoning settings. The total budget covers all attempts.
    supervisor_classification_enable_reasoning: bool = False
    supervisor_classification_timeout_seconds: float = Field(default=45, gt=0, le=180)
    supervisor_early_entity_enabled: bool = True
    supervisor_early_entity_timeout_seconds: float = Field(default=8, gt=0, le=30)
    supervisor_early_entity_max_parallel: int = Field(default=4, ge=1, le=32)
    normal_max_agent_calls: int = 6
    normal_max_execution_steps: int = 8
    normal_max_parallel_calls: int = 3
    normal_recipe_min_confidence: float = Field(default=0.92, ge=0.0, le=1.0)
    normal_diagnosis_gate_confidence: float = Field(default=0.90, ge=0.0, le=1.0)

    # Evidence Fabric v1. PostgreSQL is authoritative; Redis may cache manifests but
    # never becomes the only historical source. Limits are broker/context safety
    # boundaries and are deliberately configuration-driven rather than hard-coded in
    # business workflows.
    evidence_fabric_enabled: bool = True
    evidence_dual_write_enabled: bool = True
    evidence_inline_max_bytes: int = Field(default=65536, ge=4096, le=1048576)
    evidence_max_rows_per_slice: int = Field(default=200, ge=1, le=5000)
    evidence_max_text_chars: int = Field(default=16000, ge=1000, le=200000)
    evidence_max_tokens_per_slice: int = Field(default=8000, ge=500, le=64000)
    evidence_catalog_max_items: int = Field(default=64, ge=8, le=500)
    evidence_cold_search_candidates: int = Field(default=5, ge=1, le=20)
    evidence_cold_search_scan_limit: int = Field(default=80, ge=10, le=500)

    # Read-only Dify dataset API; credentials remain server-side.
    dify_knowledge_enabled: bool = True
    dify_knowledge_base_url: str = "http://10.10.47.231/v1"
    dify_knowledge_api_key: str = ""
    dify_knowledge_dataset_ids: dict[str, str] = Field(default_factory=dict)
    dify_knowledge_retrieval_model: dict = Field(default_factory=dict)
    dify_knowledge_timeout_seconds: float = Field(default=20, gt=0, le=120)
    dify_knowledge_catalog_ttl_seconds: int = Field(default=300, ge=0, le=3600)
    dify_knowledge_max_parallel: int = Field(default=3, ge=1, le=12)
    dify_knowledge_top_k: int = Field(default=3, ge=1, le=10)
    dify_knowledge_max_segment_characters: int = Field(default=2500, ge=100, le=10000)
    dify_knowledge_auto_enrich: bool = True
    dify_knowledge_enrichment_timeout_seconds: float = Field(default=8, gt=0, le=120)

    # Legacy expert-model settings are still accepted from an old .env for deployment
    # compatibility, but execution_mode=expert is normalized to normal at runtime.
    expert_model_base_url: str | None = None
    expert_model_api_key: str | None = None
    expert_model: str | None = None
    expert_timeout_seconds: int = 300
    expert_reasoning_effort: Literal["low", "medium", "high"] = "high"
    expert_max_agent_calls: int = 10
    expert_max_execution_steps: int = 15

    # Native image/file understanding.
    multimodal_base_url: str | None = None
    multimodal_api_key: str | None = None
    multimodal_model: str | None = None
    multimodal_timeout_seconds: float = 180.0
    multimodal_pdf_detail: Literal["auto", "low", "high"] = "high"

    agent_default_timeout_seconds: float = 300.0
    agent_default_max_retries: int = 2
    agent_retry_base_delay_seconds: float = 1.0
    agent_retry_max_delay_seconds: float = 8.0
    agent_circuit_breaker_threshold: int = 5
    agent_circuit_breaker_reset_seconds: int = 60
    agent_registry_cache_ttl_seconds: int = 30
    controller_admin_token: str | None = None

    content_understanding_max_characters_per_attachment: int = 12000
    content_understanding_max_total_characters: int = 36000
    file_quick_max_characters: int = 30000
    file_standard_max_characters: int = 200000
    file_retrieval_chunk_size: int = 1500
    file_retrieval_chunk_overlap: int = 200
    file_allow_native_model_upload: bool = True
    file_external_model_policy: Literal[
        "NEVER_SEND_ORIGINAL", "ALLOW_EXTRACTED_CONTENT", "ALLOW_NATIVE_FILE"
    ] = "ALLOW_NATIVE_FILE"
    file_enable_vector_index: bool = False
    file_enable_antivirus_scan: bool = False
    spreadsheet_tool_max_scan_rows: int = 100000

    attachment_storage_backend: Literal["local", "minio"] = "local"
    attachment_local_dir: str = "./data/attachments"
    attachment_max_file_bytes: int = 25 * 1024 * 1024
    attachment_max_total_bytes_per_message: int = 50 * 1024 * 1024
    attachment_max_count_per_message: int = 6
    attachment_unbound_ttl_seconds: int = 24 * 3600
    attachment_max_unbound_count_per_user: int = 20
    attachment_max_unbound_bytes_per_user: int = 200 * 1024 * 1024
    attachment_max_storage_bytes_per_user: int = 1024 * 1024 * 1024
    attachment_allow_other_types: bool = False

    minio_endpoint: str | None = None
    minio_access_key: str | None = None
    minio_secret_key: str | None = None
    minio_secure: bool = False
    minio_region: str | None = None
    minio_attachment_bucket: str = "conversation-attachments"

    database_url: str
    database_pool_size: int = 20
    database_max_overflow: int = 20
    database_pool_timeout_seconds: float = 5.0
    database_command_timeout_seconds: float = 10.0
    postgresql_rls_enabled: bool = True

    redis_url: str
    redis_max_connections: int = 100
    # Explicit Redis network timeouts. redis-py 8.x defaults to 5 seconds, which
    # collides with blocking XREAD/XREADGROUP calls when BLOCK is also 5000 ms.
    redis_socket_timeout_seconds: float = 15.0
    redis_connect_timeout_seconds: float = 3.0
    # Shared blocking interval for auxiliary stream consumers (summary/title/cleanup).
    redis_stream_block_ms: int = 3000

    # Legacy Dify entity Workflow settings are kept only so old deployment files
    # remain parseable. Production entity resolution no longer reads these values.
    entity_workflow_base_url: str = ""
    entity_workflow_api_key: str = ""
    entity_workflow_timeout_seconds: float = 120.0
    entity_connect_timeout_seconds: float = 2.0
    entity_lookup_cache_ttl_seconds: int = 120
    entity_profile_input_name: str = "user_profile"
    entity_profile_max_characters: int = 12000

    # Standalone ASR (speech-to-text). The API is independent from the dialogue graph.
    # dashscope: Qwen3-ASR-Flash via DashScope synchronous REST API.
    # openai_compatible: local/remote OpenAI-compatible Qwen3-ASR service (for example vLLM).
    asr_enabled: bool = True
    asr_provider: Literal["dashscope", "openai_compatible"] = "dashscope"
    asr_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    asr_api_key: str | None = None
    asr_model: str = "qwen3-asr-flash"
    asr_timeout_seconds: float = 90.0
    asr_max_file_bytes: int = 10 * 1024 * 1024
    asr_default_language: str | None = "zh"
    asr_enable_itn: bool = False
    asr_context: str | None = None

    # Realtime ASR. Browser streams PCM16/16 kHz to Conversation Service over
    # WebSocket; the backend proxies it to the realtime provider so API keys are
    # never exposed to the browser.
    asr_realtime_enabled: bool = True
    asr_realtime_provider: Literal["dashscope"] = "dashscope"
    asr_realtime_protocol: Literal["qwen_audio_streaming", "qwen3_realtime"] = "qwen_audio_streaming"
    asr_realtime_base_url: str = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
    asr_realtime_api_key: str | None = None
    asr_realtime_workspace_id: str | None = None
    asr_realtime_model: str = "qwen-audio-3.0-asr-flash-streaming"
    asr_realtime_connect_timeout_seconds: float = 15.0
    asr_realtime_finish_timeout_seconds: float = 12.0
    # Qwen-Audio-3.0 Streaming protocol. ASR_CONTEXT is also used as context text;
    # comma-separated terms are automatically reused as instant hotwords.
    asr_realtime_hotwords: str | None = None
    asr_realtime_hotword_weight: int = Field(default=4, ge=1, le=50)
    asr_realtime_max_sentence_silence_ms: int = Field(default=400, ge=200, le=6000)
    # Qwen3 realtime compatibility protocol.
    asr_realtime_vad_threshold: float = 0.0
    asr_realtime_silence_duration_ms: int = Field(default=400, ge=200, le=6000)

    # PHM Asset MCP is the single production owner of entity decision, fuzzy lookup,
    # hierarchy, equipment and point facts for every execution mode.
    phm_asset_mcp_enabled: bool = True
    phm_sensor_tools_enabled: bool = True
    phm_asset_mcp_url: str = "http://127.0.0.1:8765/mcp"
    phm_asset_mcp_timeout_seconds: float = 60.0
    phm_asset_mcp_verify_tools_on_start: bool = False
    phm_asset_unified_query_enabled: bool = False
    phm_asset_query_context_secret: str = ""
    phm_asset_query_shared_catalog: bool = True

    # PHM Data MCP. All PHM waveform/trend/alarm reads use this Streamable HTTP service.
    phm_data_mcp_enabled: bool = True
    phm_data_mcp_url: str = "http://127.0.0.1:9531/mcp"
    phm_data_mcp_timeout_seconds: float = 120.0
    # Whole-device reads can aggregate many waveform/trend payloads and legitimately
    # take longer than a single-point query.
    phm_data_mcp_device_timeout_seconds: float = 300.0
    # MCP Streamable HTTP teardown should be quick; close-only errors after a valid result
    # are logged and ignored instead of converting success into PHM_DATA_MCP_UNAVAILABLE.
    phm_data_mcp_close_timeout_seconds: float = 5.0
    phm_data_mcp_verify_tools_on_start: bool = False

    # PHM Diagnosis MCP. Professional chart/algorithm/diagnosis execution lives here.
    phm_diagnosis_mcp_enabled: bool = True
    phm_diagnosis_mcp_url: str = "http://127.0.0.1:9532/mcp"
    phm_diagnosis_mcp_timeout_seconds: float = 120.0
    # diagnose_device processes all available points and may invoke the model once;
    # keep a dedicated budget instead of timing it out like a single-chart call.
    phm_diagnosis_mcp_device_timeout_seconds: float = 300.0
    phm_diagnosis_mcp_verify_tools_on_start: bool = False

    # PHM Feature MCP. Converts Data MCP waveform payloads into scalar vibration
    # features and reliable rotational-speed/1X features. It never queries databases.
    phm_feature_mcp_enabled: bool = True
    phm_feature_mcp_url: str = "http://127.0.0.1:9533/mcp"
    phm_feature_mcp_feature_timeout_seconds: float = 30.0
    phm_feature_mcp_rpm_timeout_seconds: float = 45.0
    phm_feature_mcp_verify_tools_on_start: bool = False
    # A device diagnosis can fan out to many waveforms.  Keep the client-side fan-out
    # below the Feature MCP worker queue and retry only its explicit queue-pressure
    # signal; algorithmically unsupported RPM results remain valid completed results.
    phm_feature_batch_max_parallel: int = Field(default=4, ge=1, le=32)
    phm_feature_queue_retry_attempts: int = Field(default=2, ge=0, le=5)
    phm_feature_queue_retry_delay_seconds: float = Field(default=4.0, ge=0.0, le=60.0)

    # Legacy Dify comprehensive alarm Workflow. Retained only for rollback; disabled
    # because alarm reads are now provided by PHM Data MCP query_alarm_records.
    comprehensive_alarm_workflow_enabled: bool = False
    comprehensive_alarm_workflow_base_url: str | None = None
    comprehensive_alarm_workflow_api_key: str | None = None
    comprehensive_alarm_workflow_timeout_seconds: float = 180.0
    comprehensive_alarm_connect_timeout_seconds: float = 2.0

    # Legacy Dify diagnosis agent. Diagnosis calculation is now provided by PHM
    # Diagnosis MCP; this switch exists only as an explicit rollback gate.
    legacy_ai_diagnosis_enabled: bool = False

    # Legacy XiaoAo assistant integration. It is retained as a reserved capability but
    # intentionally excluded from routing and execution in this release.
    legacy_xiaoao_enabled: bool = False

    agent_base_url: str
    agent_api_key: str
    agent_timeout_seconds: float = 600.0
    agent_connect_timeout_seconds: float = 2.0
    agent_max_connections: int = 100
    agent_max_keepalive_connections: int = 40
    agent_keepalive_expiry_seconds: float = 60.0
    agent_http2: bool = False

    title_mode: Literal["rule", "dify", "openai"] = "rule"
    title_dify_base_url: str | None = None
    title_dify_api_key: str | None = None
    title_timeout_seconds: float = 20.0

    max_active_generations_per_conversation: int = 1
    max_active_generations_per_user: int = 3
    max_active_generations_global: int = 30

    task_context_ttl_seconds: int = 1800
    entity_selection_ttl_seconds: int = 900
    task_event_ttl_seconds: int = 3600
    task_event_maxlen: int = 5000
    sse_heartbeat_seconds: int = 2
    sse_xread_block_ms: int = 1000
    sse_redis_retry_seconds: float = 1.0
    sse_client_retry_ms: int = 1500
    generation_queue_block_ms: int = 1000
    generation_queue_batch_size: int = 4
    worker_concurrency: int = 12
    worker_heartbeat_seconds: int = 10

    conversation_retention_days: int = 365
    deleted_conversation_purge_days: int = 7
    context_recent_message_limit: int = 12
    context_max_characters: int = 16000
    summary_trigger_message_count: int = 10

    allow_cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    outbox_enabled: bool = True
    outbox_fast_publish_enabled: bool = True
    outbox_poll_interval_seconds: float = 0.1
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 20
    outbox_lock_timeout_seconds: int = 60
    outbox_base_retry_seconds: float = 0.5
    outbox_max_retry_seconds: float = 60.0

    otel_enabled: bool = False
    otel_service_name: str = "conversation-backend"
    otel_service_version: str = "1.0.0"
    otel_exporter_otlp_endpoint: str | None = None
    otel_exporter_otlp_headers: str | None = None
    otel_exporter_otlp_insecure: bool = True
    otel_trace_sample_ratio: float = 0.1
    otel_console_exporter: bool = False
    otel_excluded_urls: str = "/health,/ready,/metrics"

    @property
    def entity_workflow_url(self) -> str:
        return f"{self.entity_workflow_base_url.rstrip('/')}/workflows/run"

    @property
    def phm_asset_mcp_ready_url(self) -> str:
        base = self.phm_asset_mcp_url.rstrip("/")
        if base.endswith("/mcp"):
            base = base[:-4]
        return f"{base}/ready"

    @property
    def phm_data_mcp_ready_url(self) -> str:
        base = self.phm_data_mcp_url.rstrip("/")
        if base.endswith("/mcp"):
            base = base[:-4]
        return f"{base}/ready"

    @property
    def phm_diagnosis_mcp_ready_url(self) -> str:
        base = self.phm_diagnosis_mcp_url.rstrip("/")
        if base.endswith("/mcp"):
            base = base[:-4]
        return f"{base}/ready"

    @property
    def comprehensive_alarm_workflow_url(self) -> str:
        base = (self.comprehensive_alarm_workflow_base_url or "").rstrip("/")
        return f"{base}/workflows/run"

    @property
    def agent_chat_url(self) -> str:
        return f"{self.agent_base_url.rstrip('/')}/chat-messages"

    def agent_stop_url(self, task_id: str) -> str:
        return f"{self.agent_base_url.rstrip('/')}/chat-messages/{task_id}/stop"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
