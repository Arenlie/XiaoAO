from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.agents.catalog import default_agent_descriptors
from app.agents.contracts import AgentDescriptor, AgentHealthResult, AgentHealthStatus
from app.domain.exceptions import NotFoundError
from app.models.agent_runtime_config import AgentRuntimeConfig
from app.repositories.agent_registry_repository import AgentRegistryRepository
from app.schemas.agent import AgentRuntimeUpdate


class AgentRegistryService:
    CACHE_KEY = "chat:agent-registry:snapshot:v2"

    def __init__(
        self,
        session_factory,
        redis: Redis,
        cache_ttl_seconds: int,
        repository: AgentRegistryRepository,
        *,
        legacy_ai_diagnosis_enabled: bool = False,
        legacy_xiaoao_enabled: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.cache_ttl_seconds = cache_ttl_seconds
        self.repository = repository
        self._catalog = {
            item.agent_id: item
            for item in default_agent_descriptors(
                legacy_ai_diagnosis_enabled=legacy_ai_diagnosis_enabled,
                legacy_xiaoao_enabled=legacy_xiaoao_enabled,
            )
        }

    def catalog(self) -> dict[str, AgentDescriptor]:
        return {key: value.model_copy(deep=True) for key, value in self._catalog.items()}

    @staticmethod
    def _merge(base: AgentDescriptor, row: AgentRuntimeConfig | None) -> AgentDescriptor:
        if row is None:
            return base.model_copy(deep=True)
        force_disabled = bool(base.metadata.get("force_disabled"))
        updates: dict[str, Any] = {
            "enabled": False if force_disabled else (True if base.mandatory else row.enabled),
            "routing_enabled": (
                False if force_disabled else (True if base.mandatory else row.routing_enabled)
            ),
            "execution_enabled": (
                False if force_disabled else (True if base.mandatory else row.execution_enabled)
            ),
            "maintenance_message": (
                base.maintenance_message if force_disabled else row.maintenance_message
            ),
            "health_status": (
                AgentHealthStatus.DISABLED.value if force_disabled else row.health_status
            ),
            "health_message": (
                base.maintenance_message if force_disabled else row.health_message
            ),
        }
        if row.priority_override is not None:
            updates["priority"] = row.priority_override
        if row.timeout_seconds_override is not None:
            updates["timeout_seconds"] = float(row.timeout_seconds_override)
        metadata = dict(base.metadata)
        metadata.update(row.config_json or {})
        metadata.update(
            {
                "last_health_check_at": row.last_health_check_at.isoformat()
                if row.last_health_check_at
                else None,
                "health_details": row.health_details or {},
                "updated_by": row.updated_by,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
        updates["metadata"] = metadata
        payload = base.model_dump(mode="python")
        payload.update(updates)
        return AgentDescriptor.model_validate(payload)

    async def list_descriptors(self, *, use_cache: bool = True) -> list[AgentDescriptor]:
        if use_cache:
            cached = await self.redis.get(self.CACHE_KEY)
            if cached:
                payload = json.loads(cached)
                return [AgentDescriptor.model_validate(item) for item in payload]
        async with self.session_factory() as session:
            rows = await self.repository.list_all(session)
        by_id = {row.agent_id: row for row in rows}
        result = [self._merge(base, by_id.get(agent_id)) for agent_id, base in self._catalog.items()]
        result.sort(key=lambda item: (item.priority, item.agent_id))
        if self.cache_ttl_seconds > 0:
            await self.redis.setex(
                self.CACHE_KEY,
                self.cache_ttl_seconds,
                json.dumps(
                    [item.model_dump(mode="json") for item in result],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        return result

    async def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            item.agent_id: item.model_dump(mode="json")
            for item in await self.list_descriptors()
        }

    async def get_descriptor(self, agent_id: str) -> AgentDescriptor:
        for descriptor in await self.list_descriptors():
            if descriptor.agent_id == agent_id:
                return descriptor
        raise NotFoundError("AGENT_NOT_FOUND", "智能体不存在")

    async def update(
        self,
        agent_id: str,
        payload: AgentRuntimeUpdate,
        *,
        updated_by: str,
    ) -> AgentDescriptor:
        base = self._catalog.get(agent_id)
        if base is None:
            raise NotFoundError("AGENT_NOT_FOUND", "智能体不存在")
        values = payload.model_dump(exclude_unset=True)
        if "config_json" in values and values["config_json"] is None:
            values["config_json"] = {}
        if bool(base.metadata.get("force_disabled")):
            values["enabled"] = False
            values["routing_enabled"] = False
            values["execution_enabled"] = False
            values["maintenance_message"] = base.maintenance_message
        elif base.mandatory:
            values["enabled"] = True
            values["routing_enabled"] = True
            values["execution_enabled"] = True
        async with self.session_factory() as session, session.begin():
            row = await self.repository.get(session, agent_id, for_update=True)
            if row is None:
                row = AgentRuntimeConfig(
                    agent_id=agent_id,
                    enabled=base.enabled,
                    routing_enabled=base.routing_enabled,
                    execution_enabled=base.execution_enabled,
                    maintenance_message=base.maintenance_message,
                    health_status=base.health_status.value,
                    config_json={},
                    health_details={},
                )
                session.add(row)
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_by = updated_by
            await session.flush()
            descriptor = self._merge(base, row)
        await self.invalidate()
        return descriptor

    async def set_enabled(
        self,
        agent_id: str,
        *,
        enabled: bool,
        updated_by: str,
    ) -> AgentDescriptor:
        return await self.update(
            agent_id,
            AgentRuntimeUpdate(
                enabled=enabled,
                routing_enabled=enabled,
                execution_enabled=enabled,
            ),
            updated_by=updated_by,
        )

    async def save_health(
        self,
        result: AgentHealthResult,
        *,
        updated_by: str,
    ) -> AgentDescriptor:
        base = self._catalog.get(result.agent_id)
        if base is None:
            raise NotFoundError("AGENT_NOT_FOUND", "智能体不存在")
        checked_at = datetime.fromisoformat(result.checked_at.replace("Z", "+00:00"))
        async with self.session_factory() as session, session.begin():
            row = await self.repository.get(session, result.agent_id, for_update=True)
            if row is None:
                row = AgentRuntimeConfig(
                    agent_id=result.agent_id,
                    enabled=base.enabled,
                    routing_enabled=base.routing_enabled,
                    execution_enabled=base.execution_enabled,
                    maintenance_message=base.maintenance_message,
                    config_json={},
                    health_details={},
                )
                session.add(row)
            row.health_status = result.status.value
            row.health_message = result.message
            row.health_details = result.details
            row.last_health_check_at = checked_at.astimezone(UTC)
            row.updated_by = updated_by
            await session.flush()
            descriptor = self._merge(base, row)
        await self.invalidate()
        return descriptor

    async def invalidate(self) -> None:
        await self.redis.delete(self.CACHE_KEY)
        await self.redis.publish("chat:agent-registry:invalidate", datetime.now(UTC).isoformat())
