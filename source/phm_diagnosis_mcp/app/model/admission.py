from __future__ import annotations

from app.model.gateway import ModelGateway


class ModelAdmission:
    def __init__(self, gateway: ModelGateway):
        self.gateway = gateway

    async def status(self) -> dict[str, object]:
        if not self.gateway.settings.llm_enabled:
            return {"enabled": False, "healthy": False, "admitted": False, "reason": "MODEL_DISABLED"}
        if not self.gateway.configured():
            return {"enabled": True, "healthy": False, "admitted": False, "reason": "MODEL_NOT_CONFIGURED"}
        healthy = await self.gateway.healthy()
        return {
            "enabled": True,
            "healthy": healthy,
            "admitted": healthy,
            "model": self.gateway.settings.llm_model,
            **({"reason": "MODEL_UNAVAILABLE"} if not healthy else {}),
        }
