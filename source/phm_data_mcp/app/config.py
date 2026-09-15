from __future__ import annotations

from functools import lru_cache
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Only settings that affect real production behavior live here."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "phm-data-mcp"
    # Code-owned release identity; an old preserved .env must not override it.
    app_version: ClassVar[str] = "1.6.1"
    host: str = "0.0.0.0"
    port: int = 9531
    timezone: str = "Asia/Shanghai"
    query_timeout_seconds: int = 45

    mongo_uri: str = "mongodb://127.0.0.1:27017/phm2"
    mongo_db: str = "phm2"
    mongo_query_max_time_ms: int = 45_000
    # PHM waveform/trend reads are read-only.  In a multi-node Mongo deployment use
    # the lowest-latency healthy replica instead of pinning every large device query to
    # one slow primary.  This is especially important for get_device_data, which may
    # issue many reads in one request.
    mongo_read_preference: str = "nearest"
    mongo_server_selection_timeout_seconds: int = 15
    mongo_connect_timeout_seconds: int = 10
    mongo_socket_timeout_seconds: int = 90
    mongo_read_retry_attempts: int = 3
    mongo_read_retry_backoff_seconds: float = 0.35

    health_mongo_uri: str = "mongodb://10.10.47.232:27017,10.10.47.233:27017,10.10.47.234:27017/phm2"
    health_mongo_db: str = "phm2"
    health_redis_host: str = "10.10.47.226"
    health_redis_port: int = 6379
    health_redis_db: int = 10

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_database: str = "phm"
    mysql_user: str = "root"
    mysql_password: str = ""

    audit_enabled: bool = True
    audit_database_url: str = "postgresql://phm_data_mcp:123456@127.0.0.1:5432/phm_data_mcp"
    audit_retention_days: int = 30
    audit_max_rows: int = 1_000_000
    audit_max_json_bytes: int = 65_536

    trend_inline_max_points: int = 5_000
    max_response_bytes: int = 64 * 1024 * 1024
    waveform_max_samples: int = 2_000_000
    trend_max_points: int = 2_000_000
    # Whole-device diagnosis can aggregate many points. Keep headroom for JSON
    # envelope/base64 overhead and shrink only the trend window when the payload would
    # exceed the application response limit; waveform coverage is preserved.
    device_response_budget_ratio: float = 0.85
    device_trend_fallback_days: str = "7,1"

    alarm_llm_enabled: bool = False
    alarm_llm_base_url: str = ""
    alarm_llm_api_key: str = ""
    alarm_llm_model: str = ""
    alarm_llm_timeout_seconds: int = 60

    performance_monitor_enabled: bool = False
    performance_trace_max_spans: int = 120


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
