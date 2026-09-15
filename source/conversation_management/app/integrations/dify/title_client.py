from __future__ import annotations

import httpx

from app.config import Settings
from app.services.title_service import rule_title


class TitleClient:
    def __init__(self, client: httpx.AsyncClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    async def generate(self, query: str, user_token: str) -> str:
        if self.settings.title_mode == "rule" or not self.settings.title_dify_api_key:
            return rule_title(query)
        url = f"{(self.settings.title_dify_base_url or '').rstrip('/')}/workflows/run"
        try:
            response = await self.client.post(
                url,
                headers={"Authorization": f"Bearer {self.settings.title_dify_api_key}"},
                json={"inputs": {"query": query}, "response_mode": "blocking", "user": user_token},
                timeout=self.settings.title_timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            outputs = ((payload.get("data") or {}).get("outputs") or {})
            title = str(outputs.get("title") or outputs.get("text") or "").strip()
            return title[:100] if title else rule_title(query)
        except Exception:
            return rule_title(query)
