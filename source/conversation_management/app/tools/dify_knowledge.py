"""Read-only Dify Knowledge API adapter; catalog cache contains no chat results."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.tools.contracts import ToolCallRequest, ToolCallResult, ToolDescriptor

KNOWLEDGE_TOOL_ID = "knowledge.dify.retrieve"
KNOWLEDGE_NAMES = ("企业智库", "历史案例库", "硬件部署信息库")


def knowledge_descriptor(settings) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=KNOWLEDGE_TOOL_ID, display_name="企业知识库检索", provider_type="local",
        enabled=bool(settings.dify_knowledge_enabled and settings.dify_knowledge_api_key),
        description=("检索企业智库、历史案例库、硬件部署信息库。需要企业设备原理、维护规程、"
                     "历史相似故障案例、传感器/采集器/硬件安装部署资料时调用；可与实时查询并行。"
                     "返回文档名称、段落位置和原文，引用时必须注明来源。历史案例不代表当前设备已发生同一故障。"),
        timeout_seconds=settings.dify_knowledge_timeout_seconds + 5,
        input_schema={"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 250},
            "knowledge_bases": {"type": "array", "items": {"type": "string", "enum": list(KNOWLEDGE_NAMES)}},
        }, "required": ["query"], "additionalProperties": False},
        metadata={"read_only": True, "parallel_safe": True, "evidence_type": "knowledge"},
    )


class KnowledgeError(Exception):
    pass


class DifyKnowledgeHandler:
    def __init__(self, client: httpx.AsyncClient, settings) -> None:
        self.client, self.settings = client, settings
        self._catalog: dict[str, str] = {}
        self._expires = 0.0
        self._catalog_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.dify_knowledge_max_parallel)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with self._slots:
            response = await self.client.request(
                method, self.settings.dify_knowledge_base_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.settings.dify_knowledge_api_key},
                timeout=self.settings.dify_knowledge_timeout_seconds, follow_redirects=False, **kwargs,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise KnowledgeError("知识库返回了无法识别的内容。")
        return payload

    async def _datasets(self) -> dict[str, str]:
        pinned = dict(self.settings.dify_knowledge_dataset_ids)
        if pinned and set(pinned) - set(KNOWLEDGE_NAMES):
            raise KnowledgeError("知识库配置包含未开放的库，请管理员检查配置。")
        if time.monotonic() < self._expires:
            return dict(self._catalog)
        async with self._catalog_lock:
            if time.monotonic() < self._expires:
                return dict(self._catalog)
            found: dict[str, list[str]] = {name: [] for name in KNOWLEDGE_NAMES}
            for page in range(1, 101):
                data = await self._request("GET", "/datasets", params={"page": page, "limit": 100})
                for row in data.get("data", []):
                    if not isinstance(row, dict):
                        continue
                    name, ident = str(row.get("name") or ""), str(row.get("id") or "")
                    if name in found and ident and ident not in found[name]:
                        found[name].append(ident)
                if not data.get("has_more"):
                    break
            else:
                raise KnowledgeError("知识库目录过大，未完成完整名称校验，请管理员指定知识库编号。")
            resolved = {}
            for name, ids in found.items():
                if name in pinned:
                    if pinned[name] not in ids:
                        raise KnowledgeError(f"“{name}”的配置编号与可访问目录不一致。")
                    resolved[name] = pinned[name]
                elif len(ids) == 1:
                    resolved[name] = ids[0]
                elif len(ids) > 1:
                    # Never silently select the first duplicate name.
                    continue
            self._catalog = resolved
            self._expires = time.monotonic() + self.settings.dify_knowledge_catalog_ttl_seconds
            return dict(resolved)

    async def _retrieve(self, name: str, dataset_id: str, query: str) -> list[dict]:
        body: dict[str, Any] = {"query": query}
        # By default let each existing Dify knowledge base keep its search/rerank policy.
        if self.settings.dify_knowledge_retrieval_model:
            body["retrieval_model"] = dict(self.settings.dify_knowledge_retrieval_model)
        data = await self._request("POST", f"/datasets/{dataset_id}/retrieve", json=body)
        records = data.get("records")
        if not isinstance(records, list):
            raise KnowledgeError(f"“{name}”未返回有效的检索结果。")
        sources = []
        for record in records:
            if not isinstance(record, dict):
                continue
            segment = record.get("segment") or {}
            document = segment.get("document") or {}
            content = str(segment.get("content") or "").strip()
            if segment.get("enabled") is False or segment.get("status", "completed") != "completed":
                continue
            if not content or not document.get("name") or not segment.get("id"):
                continue
            if segment.get("answer"):
                content += "\n回答：" + str(segment["answer"])
            sources.append({
                "knowledge_base": name, "document_name": str(document["name"]),
                "dataset_id": dataset_id,
                "document_id": str(document.get("id") or segment.get("document_id") or ""),
                "segment_id": str(segment["id"]), "position": segment.get("position"),
                "content": content[:self.settings.dify_knowledge_max_segment_characters],
                "content_truncated": len(content) > self.settings.dify_knowledge_max_segment_characters,
                "score": record.get("score"),
            })
            if len(sources) >= self.settings.dify_knowledge_top_k:
                break
        return sources

    async def __call__(self, request: ToolCallRequest, data_access_token: str | None) -> ToolCallResult:
        if not self.settings.dify_knowledge_enabled or not self.settings.dify_knowledge_api_key:
            return ToolCallResult(tool_id=request.tool_id, status="FAILED",
                                  error_code="KNOWLEDGE_DISABLED", error_message="企业知识库暂未启用。")
        query = str(request.arguments.get("query") or "").strip()
        names = request.arguments.get("knowledge_bases") or list(KNOWLEDGE_NAMES)
        if (not query or len(query) > 250 or not isinstance(names, list)
                or any(name not in KNOWLEDGE_NAMES for name in names)):
            return ToolCallResult(tool_id=request.tool_id, status="REJECTED", error_code="KNOWLEDGE_ARGUMENT_INVALID",
                                  error_message="请使用简明的问题检索已开放的企业知识库。")
        names = list(dict.fromkeys(names))
        tasks = []
        try:
            budget = min(float(self.settings.dify_knowledge_timeout_seconds), float(getattr(
                request, "knowledge_timeout_seconds", self.settings.dify_knowledge_timeout_seconds)))
            if budget <= 0:
                raise KnowledgeError("知识库查询时间预算无效。")
            deadline = time.monotonic() + budget
            async with asyncio.timeout(budget):
                datasets = await self._datasets()
            available = [name for name in names if name in datasets]
            warnings = [f"“{name}”不存在、无权访问或存在同名库，尚未检索该库。"
                        for name in names if name not in datasets]
            tasks = [asyncio.create_task(self._retrieve(name, datasets[name], query)) for name in available]
            done, pending = await asyncio.wait(tasks, timeout=max(0, deadline - time.monotonic())) if tasks else (set(), set())
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            sources, succeeded, completed, timed_out = [], 0, [], []
            for name, task in zip(available, tasks):
                if task not in done or task.cancelled():
                    timed_out.append(name)
                    warnings.append(f"“{name}”检索超时；保留其他知识库已返回的资料。")
                else:
                    result = task.exception() or task.result()
                    if isinstance(result, BaseException):
                        warnings.append(f"“{name}”暂时无法检索。")
                        if isinstance(result, httpx.HTTPStatusError) and result.response.status_code == 404:
                            self._expires = 0
                    else:
                        succeeded += 1
                        completed.append(name)
                        sources.extend(result)
            return ToolCallResult(
                tool_id=request.tool_id, status="SUCCESS" if succeeded else "FAILED",
                content="已检索到可引用的知识库原文。" if sources else "本次未检索到可引用的知识库原文。",
                structured_content={"sources": sources, "warnings": warnings, "query": query,
                                    "partial": bool(warnings), "searched_knowledge_bases": available,
                                    "completed_knowledge_bases": completed, "timed_out_knowledge_bases": timed_out},
                error_code=None if succeeded else "KNOWLEDGE_UNAVAILABLE",
                error_message=None if succeeded else "企业知识库暂时不可用。",
            )
        except (TimeoutError, httpx.HTTPError, ValueError, KnowledgeError):
            # No response bodies, URLs, credentials or exception strings enter customer output.
            return ToolCallResult(tool_id=request.tool_id, status="FAILED", error_code="KNOWLEDGE_UNAVAILABLE",
                                  error_message="企业知识库暂时无法检索，本轮未取得知识库依据。")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
