from __future__ import annotations

import math
from typing import Any

import httpx

from app.providers.cache import AsyncMemo
from app.config import Settings
from app.errors import AssetError, ErrorCode


def _endpoint(base: str, suffix: str) -> str:
    return base.rstrip("/") + (suffix if base.rstrip("/").endswith("/v1") else "/v1" + suffix)


class EmbeddingProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.embedding_timeout,
            limits=httpx.Limits(max_connections=settings.model_http_max_connections,
                                max_keepalive_connections=settings.model_http_max_connections),
        )

        self.cache = AsyncMemo(maxsize=settings.model_cache_max_entries,
                               ttl=settings.embedding_cache_ttl_seconds)

    @property
    def enabled(self) -> bool:
        return self.settings.embedding_configured

    async def embed(self, text: str) -> list[float]:
        return await self.cache.get((self.settings.embedding_model, text),
                                    lambda: self._embed_one(text))

    async def _embed_one(self, text: str) -> list[float]:
        return (await self.embed_many([text]))[0]

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.enabled:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产向量模型未配置，无法检索真实资产候选。",
                "Embedding is required but EMBEDDING_ENABLED/EMBEDDING_BASE_URL/EMBEDDING_MODEL is incomplete",
            )
        headers = {"Content-Type": "application/json"}
        if self.settings.embedding_api_key:
            headers["Authorization"] = f"Bearer {self.settings.embedding_api_key}"
        try:
            r = await self.client.post(
                _endpoint(self.settings.embedding_base_url, "/embeddings"),
                headers=headers,
                json={"model": self.settings.embedding_model, "input": texts},
            )
            r.raise_for_status()
            data: Any = r.json()
            items = data["data"]
            if not isinstance(items, list) or len(items) != len(texts):
                raise ValueError("embedding response count does not match input count")
            ordered = sorted(items, key=lambda item: int(item.get("index", 0)))
            vectors: list[list[float]] = []
            for item in ordered:
                values = [float(v) for v in item["embedding"]]
                if len(values) != self.settings.embedding_dimension or not all(
                    math.isfinite(v) for v in values
                ):
                    raise ValueError("invalid embedding dimension or non-finite values")
                vectors.append(values)
            return vectors
        except Exception as exc:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产向量模型暂时不可用，请稍后重试。",
                f"embedding request failed: {type(exc).__name__}: {exc}",
            ) from exc

    async def status(self) -> str:
        if not self.enabled:
            return "DISABLED"
        await self.embed("health")
        return "UP"

    async def close(self) -> None:
        await self.cache.close()
        await self.client.aclose()
