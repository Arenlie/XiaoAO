from __future__ import annotations

from sqlalchemy import select

from app.models.agent_runtime_config import AgentRuntimeConfig


class AgentRegistryRepository:
    async def list_all(self, session) -> list[AgentRuntimeConfig]:
        return list(
            (
                await session.scalars(
                    select(AgentRuntimeConfig).order_by(AgentRuntimeConfig.agent_id.asc())
                )
            ).all()
        )

    async def get(self, session, agent_id: str, *, for_update: bool = False):
        statement = select(AgentRuntimeConfig).where(
            AgentRuntimeConfig.agent_id == agent_id
        )
        if for_update:
            statement = statement.with_for_update()
        return await session.scalar(statement)
