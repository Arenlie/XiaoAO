from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentRuntimeConfig(Base):
    __tablename__ = "agent_runtime_configs"

    agent_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    routing_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    execution_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    maintenance_message: Mapped[str | None] = mapped_column(Text)
    priority_override: Mapped[int | None] = mapped_column(Integer)
    timeout_seconds_override: Mapped[int | None] = mapped_column(Integer)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    health_message: Mapped[str | None] = mapped_column(Text)
    health_details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
