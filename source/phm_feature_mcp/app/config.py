from __future__ import annotations

import os
from functools import lru_cache
from typing import ClassVar, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for phm_feature_mcp."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str = "phm_feature_mcp"
    service_version: ClassVar[str] = "1.0.0"
    algorithm_version: str = "mechanic-dual-spectrum-1.0"
    environment: str = "production"

    host: str = "0.0.0.0"
    port: int = 9533
    mcp_path: str = "/mcp"
    web_concurrency: int = 2
    max_request_body_size: int = 32 * 1024 * 1024

    # CPU-bound feature extraction runs outside the MCP event loop.
    max_workers: int = Field(default_factory=lambda: max(1, min((os.cpu_count() or 2) // 2 or 1, 4)))
    max_concurrent_tasks: int = 8
    # Whole-device diagnosis legitimately submits several CPU jobs together.  Keep
    # back-pressure, but allow queued jobs to wait for the executor instead of failing
    # after three seconds while earlier points are still being analysed.
    queue_wait_timeout_s: float = 60.0

    max_samples_per_request: int = 262_144
    min_samples_per_request: int = 256
    min_finite_ratio: float = 0.98

    # Optional LLM conflict arbitration used only by rotational-speed feature extraction.
    enable_llm_judge: bool = False
    llm_base_url: str = ""
    llm_api_key: Optional[str] = None
    llm_model: str = ""
    llm_timeout_s: float = 15.0

    log_level: str = "INFO"
    performance_monitor_enabled: bool = False
    performance_trace_max_spans: int = 120
    log_json: bool = True

    @field_validator(
        "port",
        "web_concurrency",
        "max_request_body_size",
        "max_workers",
        "max_concurrent_tasks",
        "max_samples_per_request",
        "min_samples_per_request",
    )
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("queue_wait_timeout_s", "llm_timeout_s")
    @classmethod
    def positive_float(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be > 0")
        return value

    @field_validator("mcp_path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        value = value.strip() or "/mcp"
        return value if value.startswith("/") else f"/{value}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
