from __future__ import annotations

from typing import ClassVar

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "phm-diagnosis-mcp"
    app_version: ClassVar[str] = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 9532
    mcp_max_request_body_bytes: int = 64 * 1024 * 1024

    llm_enabled: bool = True
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 60.0

    audit_enabled: bool = True
    audit_database_url: str = "postgresql://phm_diagnosis_mcp:123456@127.0.0.1:5432/phm_diagnosis_mcp"
    audit_retention_days: int = 30

    performance_monitor_enabled: bool = False
    performance_trace_max_spans: int = 120


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
