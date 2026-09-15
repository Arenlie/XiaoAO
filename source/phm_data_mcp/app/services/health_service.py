from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.repositories.health import HealthRepository


def _round_numbers(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, list):
        return [_round_numbers(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_numbers(item) for key, item in value.items()}
    return value


class HealthService:
    def __init__(self, repository: "HealthRepository"):
        self.repository = repository

    def query(
        self,
        scope_type: str,
        scope_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        scope_type = scope_type.strip().lower()
        scope_id = scope_id.strip()
        if not scope_id:
            raise ValueError("scope_id不能为空")

        if scope_type == "device":
            data = self.repository.get_device_health(scope_id, start_time, end_time, limit)
        elif scope_type == "space":
            if start_time or end_time:
                raise ValueError("区域健康度只支持实时查询，不接受start_time或end_time")
            data = self.repository.get_space_health(scope_id)
        else:
            raise ValueError("scope_type只支持device或space")

        return {"success": True, "data": _round_numbers(data)}
