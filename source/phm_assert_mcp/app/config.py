from __future__ import annotations

import re
from functools import lru_cache
from typing import ClassVar, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUALIFIED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")


def validate_identifier(value: str, *, qualified: bool = False) -> str:
    value = (value or "").strip()
    pattern = _QUALIFIED if qualified else _IDENT
    if not value or not pattern.fullmatch(value):
        raise ValueError(f"invalid SQL identifier: {value!r}")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "phm-asset-mcp"
    app_version: ClassVar[str] = "1.7.0"
    app_env: str = "production"
    host: str = "0.0.0.0"
    port: int = 8765
    mcp_path: str = "/mcp"
    log_level: str = "INFO"
    performance_monitor_enabled: bool = False
    performance_trace_max_spans: int = 120

    postgres_host: str
    postgres_port: int = 5432
    postgres_database: str
    postgres_user: str
    postgres_password: str
    postgres_pool_min_size: int = 2
    postgres_pool_max_size: int = 20
    postgres_connect_timeout: float = 5.0
    postgres_command_timeout: float = 10.0

    asset_catalog_table: str = "public.entity_search_catalog"
    asset_normalize_function: str = "normalize_entity_name"
    asset_hierarchy_backend: Literal["catalog_path", "native_recursive"] = "catalog_path"
    space_table: str = ""
    space_id_column: str = ""
    space_parent_id_column: str = ""
    space_name_column: str = ""
    space_type_column: str = ""
    space_no_column: str = ""

    asset_tree_max_depth: int = Field(default=10, ge=1, le=100)
    asset_tree_max_nodes: int = Field(default=5000, ge=10, le=100000)
    asset_query_max_devices: int = Field(default=5000, ge=1, le=100000)
    asset_query_max_points: int = Field(default=5000, ge=1, le=100000)
    asset_collection_max_parallel: int = Field(default=8, ge=1, le=64)
    asset_collection_page_size: int = Field(default=1000, ge=100, le=5000)
    asset_collection_max_items: int = Field(default=50000, ge=1000, le=200000)
    asset_cache_enabled: bool = True
    asset_cache_ttl_seconds: int = Field(default=300, ge=1, le=86400)
    asset_unified_query_enabled: bool = False
    asset_query_context_secret: str = ""
    asset_taxonomy_policy_file: str = ""
    # Soft freshness window returned to callers. Historical same_set/refine_set
    # snapshots are physically retained much longer so Evidence references survive
    # Redis loss and multi-day/multi-topic conversations.
    asset_query_snapshot_ttl_seconds: int = Field(default=86400, ge=60, le=315576000)
    asset_query_snapshot_retention_seconds: int = Field(default=315576000, ge=86400, le=630720000)
    asset_query_snapshot_max_members: int = Field(default=50000, ge=1, le=200000)
    asset_query_snapshot_max_bytes: int = Field(default=67108864, ge=1024)
    asset_query_snapshot_owner_limit: int = Field(default=500, ge=1, le=10000)
    asset_query_snapshot_global_bytes: int = Field(default=2147483648, ge=1024)

    model_cache_max_entries: int = Field(default=512, ge=0, le=10000)
    embedding_cache_ttl_seconds: float = Field(default=3600, ge=0, le=86400)
    understanding_cache_ttl_seconds: float = Field(default=120, ge=0, le=3600)
    model_http_max_connections: int = Field(default=40, ge=1, le=200)

    # Optional read-only Sensor Agent capability; never a prerequisite for assets.
    sensor_agent_enabled: bool = True
    sensor_agent_base_url: str = ""
    sensor_agent_timeout_seconds: float = Field(default=8, ge=0.1, le=60)
    sensor_agent_max_retries: int = Field(default=1, ge=0, le=2)
    sensor_agent_verify_tls: bool = True
    sensor_agent_auth_header: str = "Authorization"
    sensor_agent_auth_value: str = ""
    sensor_agent_timezone: str = "Asia/Shanghai"
    sensor_agent_max_parallel: int = Field(default=4, ge=1, le=16)
    sensor_agent_cache_ttl_seconds: float = Field(default=10, ge=0, le=60)
    sensor_agent_fetch_limit: int = Field(default=15000, ge=1, le=1000000)
    sensor_agent_registry_mapping_ttl_seconds: float = Field(default=300, ge=0, le=3600)
    sensor_agent_rules_cache_ttl_seconds: float = Field(default=300, ge=0, le=3600)
    sensor_agent_scope_max_points: int = Field(default=200, ge=1, le=5000)
    sensor_agent_scope_timeout_seconds: float = Field(default=25, ge=1, le=40)
    sensor_agent_max_response_mb: int = Field(default=64, ge=1, le=256)

    embedding_enabled: bool = True
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "Qwen3-Embedding-0.6B"
    embedding_dimension: int = 1024
    embedding_timeout: float = 10.0

    rerank_enabled: bool = True
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "Qwen3-Reranker-0.6B"
    rerank_timeout: float = 10.0

    llm_enabled: bool = True
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "qwen3.6-flash"
    llm_timeout: float = 30.0

    resolve_candidate_min_score: float = Field(default=0.20, ge=0.0, le=1.0)
    resolve_max_candidates: int = 40
    resolve_output_compat_mode: Literal["dual", "new"] = "dual"

    @model_validator(mode="after")
    def validate_runtime(self) -> "Settings":
        validate_identifier(self.asset_catalog_table, qualified=True)
        validate_identifier(self.asset_normalize_function, qualified=True)
        if self.postgres_pool_min_size > self.postgres_pool_max_size:
            raise ValueError("POSTGRES_POOL_MIN_SIZE cannot exceed POSTGRES_POOL_MAX_SIZE")
        if not self.llm_configured:
            raise ValueError(
                "Asset LLM is mandatory: configure LLM_ENABLED, LLM_BASE_URL, "
                "LLM_API_KEY and LLM_MODEL"
            )
        if not self.embedding_configured:
            raise ValueError(
                "Embedding is mandatory: configure EMBEDDING_ENABLED, "
                "EMBEDDING_BASE_URL and EMBEDDING_MODEL"
            )
        if not self.rerank_configured:
            raise ValueError(
                "Reranker is mandatory: configure RERANK_ENABLED, "
                "RERANK_BASE_URL and RERANK_MODEL"
            )
        if self.asset_hierarchy_backend == "native_recursive":
            validate_identifier(self.space_table, qualified=True)
            for value in (
                self.space_id_column,
                self.space_parent_id_column,
                self.space_name_column,
                self.space_type_column,
                self.space_no_column,
            ):
                validate_identifier(value)
        return self

    @property
    def embedding_configured(self) -> bool:
        return self.embedding_enabled and bool(self.embedding_base_url and self.embedding_model)

    @property
    def rerank_configured(self) -> bool:
        return self.rerank_enabled and bool(self.rerank_base_url and self.rerank_model)

    @property
    def llm_configured(self) -> bool:
        return self.llm_enabled and bool(
            self.llm_base_url and self.llm_api_key and self.llm_model
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
