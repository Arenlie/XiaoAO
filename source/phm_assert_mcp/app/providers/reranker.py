from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.domain.entities import Candidate
from app.errors import AssetError, ErrorCode
from app.providers.embedding import _endpoint


class RerankProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.rerank_timeout,
            limits=httpx.Limits(max_connections=settings.model_http_max_connections,
                                max_keepalive_connections=settings.model_http_max_connections),
        )

    @property
    def enabled(self) -> bool:
        return self.settings.rerank_configured

    async def rerank(self, query: str, candidates: list[Candidate], structured_query: str, profile_context: str, has_explicit_area: bool) -> dict[int, float]:
        if not candidates:
            return {}
        if not self.enabled:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产重排模型未配置，无法校验候选相关性。",
                "Reranker is required but RERANK_ENABLED/RERANK_BASE_URL/RERANK_MODEL is incomplete",
            )
        documents = []
        for index, c in enumerate(candidates):
            m = c.metadata
            area = str(m.get("space_path") or "") or " / ".join(
                str(m.get(k) or "")
                for k in (
                    "company_name",
                    "factory_name",
                    "region_name",
                    "area_name",
                    "workshop_name",
                    "line_name",
                    "production_line",
                )
                if m.get(k)
            )
            documents.append("\n".join([
                f"候选序号：{index}", f"实体类型：{c.entity_type}",
                f"设备名称：{m.get('equip_name') or (c.display_name if c.entity_type=='equipment' else '')}",
                f"设备编号：{c.equip_no}", f"测点名称：{m.get('point_name') or (c.display_name if c.entity_type=='point' else '')}",
                f"测点编号：{c.point_no}", f"所属部件：{m.get('station_name') or m.get('component_name') or m.get('part_name') or ''}",
                f"安装位置：{m.get('position_name') or m.get('install_position') or ''}",
                f"测量方向：{m.get('direction_name') or m.get('direction') or ''}", f"所属区域：{area}",
                f"完整描述：{c.search_text}", f"向量相似度：{c.vector_score}",
            ]))
        rerank_query = "\n".join([
            "请按工业实体的多字段一致性重排候选。",
            "排序必须优先满足用户明确提出的所有字段；部件、位置、方向任一冲突都应显著降权。",
            "用户画像只在用户未明确区域时作为同等相关候选的弱先验，不能覆盖显式条件。",
            f"用户原始问题：{query}", f"显式结构化条件：{structured_query}",
            f"用户画像弱先验：{profile_context if profile_context and not has_explicit_area else '不启用或用户已明确区域'}",
        ])
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.settings.rerank_api_key:
            headers["Authorization"] = f"Bearer {self.settings.rerank_api_key}"
        try:
            r = await self.client.post(
                _endpoint(self.settings.rerank_base_url, "/rerank"), headers=headers,
                json={"model": self.settings.rerank_model, "query": rerank_query, "documents": documents},
            )
            r.raise_for_status()
            payload: Any = r.json()
            items = payload.get("results") or payload.get("data") or []
            result: dict[int, float] = {}
            for item in items:
                idx = item.get("index")
                score = item.get("relevance_score", item.get("score"))
                if isinstance(idx, int) and score is not None:
                    result[idx] = max(0.0, min(1.0, float(score)))
            return result
        except Exception as exc:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产重排模型暂时不可用，请稍后重试。",
                f"reranker request failed: {type(exc).__name__}: {exc}",
            ) from exc

    async def status(self) -> str:
        return "CONFIGURED" if self.enabled else "DISABLED"

    async def close(self) -> None:
        await self.client.aclose()
