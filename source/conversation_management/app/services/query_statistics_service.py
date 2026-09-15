from __future__ import annotations

import re

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert

from app.models.query_statistics import QueryStatistic


class QueryStatisticsService:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def normalize(query: str) -> str:
        return re.sub(r"\s+", " ", query.strip().lower())[:255]

    async def record(self, user_token: str, app_code: str, query: str) -> None:
        normalized = self.normalize(query)
        if not normalized:
            return
        async with self.session_factory() as session, session.begin():
            stmt = insert(QueryStatistic).values(
                user_token=user_token,
                app_code=app_code,
                normalized_query=normalized,
                sample_query=query,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["user_token", "app_code", "normalized_query"],
                set_={
                    "sample_query": query,
                    "usage_count": QueryStatistic.usage_count + 1,
                    "last_used_at": __import__("sqlalchemy").func.now(),
                },
            )
            await session.execute(stmt)

    async def suggestions(self, user_token: str, app_code: str, limit: int = 10) -> list[str]:
        async with self.session_factory() as session:
            stmt = (
                select(QueryStatistic.sample_query)
                .where(
                    QueryStatistic.user_token == user_token,
                    QueryStatistic.app_code == app_code,
                )
                .order_by(desc(QueryStatistic.usage_count), desc(QueryStatistic.last_used_at))
                .limit(limit)
            )
            return list((await session.scalars(stmt)).all())
