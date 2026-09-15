from __future__ import annotations

from app.agents.contracts import AgentAdapter
from app.domain.exceptions import AppError


class AgentAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        agent_id = adapter.descriptor.agent_id
        if agent_id in self._adapters:
            raise ValueError(f"duplicate agent adapter: {agent_id}")
        self._adapters[agent_id] = adapter

    def get(self, agent_id: str) -> AgentAdapter:
        adapter = self._adapters.get(agent_id)
        if adapter is None:
            raise AppError("AGENT_ADAPTER_NOT_FOUND", f"智能体适配器未注册: {agent_id}", 503)
        return adapter

    def registered_ids(self) -> set[str]:
        return set(self._adapters)
