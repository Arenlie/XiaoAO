from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QueryStatistic(Base):
    __tablename__ = "query_statistics"
    __table_args__ = (UniqueConstraint("user_token", "app_code", "normalized_query"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_token: Mapped[str] = mapped_column(String(128), nullable=False)
    app_code: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_query: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_query: Mapped[str] = mapped_column(Text, nullable=False)
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
