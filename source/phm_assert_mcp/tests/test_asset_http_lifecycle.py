"""Real MCP HTTP lifecycle regression; only external model/database I/O is fake.

Run this file with --baseline and PYTHONPATH pointing to the old Asset source
to reproduce the incident using exactly the same protocol requests and fixtures.
All equipment identities below are test data, never production catalog claims.
"""
import asyncio
import json
import re
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.domain.entities import Candidate
from app.mcp import server


def arguments(number=1):
    return {"query": f"白灰三车间{number}#斗提有问题吗？", "required_entity_level": "equipment",
            "semantic_hints": {"needs_asset_lookup": True,
                "area": {"raw_text": "白灰三车间", "retrieval_text": "白灰三车间"},
                "equipment": {"raw_text": f"{number}#斗提", "retrieval_text": f"{number}号斗式提升机"}}}


@asynccontextmanager
async def asset_app(monkeypatch):
    settings = Settings.model_construct(postgres_host="unused", postgres_database="unused",
        postgres_user="unused", postgres_password="unused", host="127.0.0.1",
        embedding_base_url="http://models/v1", embedding_dimension=2,
        rerank_base_url="http://models/v1", llm_base_url="http://models/v1", llm_api_key="test-only")
    originals, instances = [], []
    closes, requests = Counter(), Counter()
    gate = SimpleNamespace(entered=asyncio.Event(), release=asyncio.Event(), block=False)

    async def model_response(request):
        body = json.loads(request.content)
        requests[request.url.path] += 1
        if request.url.path.endswith("/embeddings"):
            if gate.block:
                gate.entered.set()
                await gate.release.wait()
            text = body["input"][0]
            match = re.search(r"(\d+)号", text)
            number = int(match.group(1)) if match else 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [number, .2]}]})
        if request.url.path.endswith("/rerank"):
            return httpx.Response(200, json={"results": [{"index": i, "relevance_score": .99}
                for i, _ in enumerate(body["documents"])]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"test":true}'}}]})
        raise AssertionError(request.url.path)

    runtime_class = server.Runtime

    def runtime_factory(config):
        runtime = runtime_class(config)
        instances.append(runtime)
        for name in ("embedding", "reranker", "llm"):
            provider = getattr(runtime, name)
            originals.append(provider.client)
            provider.client = httpx.AsyncClient(transport=httpx.MockTransport(model_response))
            original_close = provider.close

            async def counted_close(name=name, original_close=original_close):
                closes[name] += 1
                await original_close()

            provider.close = counted_close
        runtime.db.ping = AsyncMock(return_value=True)

        async def close_db():
            closes["db"] += 1

        runtime.db.close = close_db
        runtime.semantic.capability = AsyncMock(return_value={"available": True, "missing_columns": []})
        for method in ("exact_code", "official_exact_name", "semantic_exact_name"):
            setattr(runtime.catalog, method, AsyncMock(return_value=[]))

        async def vector_search(**kwargs):
            assert kwargs["scope"] == "equipment"
            assert kwargs["area_keywords"] == ["白灰三车间"]
            number = int(json.loads(kwargs["vector_literal"])[0])
            return [Candidate(entity_type="equipment", entity_key=f"TEST-{number}",
                display_name=f"{number}号斗式提升机", equip_no=f"TEST-BUCKET-{number}", vector_score=.98,
                metadata={"space_id": "TEST-AREA", "leaf_space_name": "白灰三车间",
                    "space_path": "测试厂/白灰三车间", "space_link": "test/area/",
                    "equip_name": f"{number}号斗式提升机"})]

        runtime.catalog.vector_search = AsyncMock(side_effect=vector_search)
        return runtime

    monkeypatch.setattr(server, "Runtime", runtime_factory)
    mcp = server.build_mcp(settings)
    app = mcp.streamable_http_app()
    async with app.router.lifespan_context(app):
        for client in originals:
            await client.aclose()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
            base_url="http://127.0.0.1:8765", headers={"Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18"}) as client:
            yield SimpleNamespace(client=client, runtime=instances[0], instances=instances,
                                  closes=closes, requests=requests, gate=gate, mcp=mcp)


async def rpc(harness, method, params=None, id=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id is not None:
        body["id"] = id
    response = await harness.client.post("/mcp", json=body)
    assert response.status_code in (200, 202), response.text
    if id is None:
        return None
    payload = response.json()
    assert "error" not in payload, payload
    return payload["result"]


async def initialize(h):
    await rpc(h, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "incident-regression", "version": "1"}})
    await rpc(h, "notifications/initialized", id=None)
    result = await rpc(h, "tools/list")
    assert "resolve_entity" in {tool["name"] for tool in result["tools"]}


async def resolve(h, number=1):
    result = await rpc(h, "tools/call", {"name": "resolve_entity", "arguments": arguments(number)}, id=number)
    assert not result.get("isError"), result
    return result["structuredContent"]


@pytest.mark.asyncio
async def test_initialize_list_sequential_and_parallel_queries_keep_clients_open(monkeypatch):
    async with asset_app(monkeypatch) as h:
        await initialize(h)
        assert not h.closes, "Protocol initialization must never close application resources"
        for number in (1, 2):
            payload = await resolve(h, number)
            assert payload["status"] == "RESOLVED", payload
            assert payload["entity"]["equip_no"] == f"TEST-BUCKET-{number}"
        payloads = await asyncio.gather(*(resolve(h, n) for n in range(3, 13)))
        assert [p["entity"]["equip_no"] for p in payloads] == [f"TEST-BUCKET-{n}" for n in range(3, 13)]
        assert len(h.instances) == 1 and not h.closes
        assert h.requests["/v1/embeddings"] == 12
        assert h.requests["/v1/rerank"] == 12
        # Exercise the third real HTTP client too, after protocol calls have completed.
        assert await h.runtime.llm.extract(query="test", required_entity_level="any") == {"test": True}
        ready = await h.client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["model_clients"] == {"embedding": "OPEN", "reranker": "OPEN", "llm": "OPEN"}
    assert h.closes == {"embedding": 1, "reranker": 1, "llm": 1, "db": 1}
    assert all(getattr(h.runtime, name).client.is_closed for name in ("embedding", "reranker", "llm"))


@pytest.mark.asyncio
async def test_cancelled_http_request_does_not_close_other_requests_resources(monkeypatch):
    async with asset_app(monkeypatch) as h:
        await initialize(h)
        h.gate.block = True
        cancelled = asyncio.create_task(resolve(h, 20))
        await asyncio.wait_for(h.gate.entered.wait(), 3)
        surviving = asyncio.create_task(resolve(h, 21))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert not h.closes
        h.gate.release.set()
        payload = await asyncio.wait_for(surviving, 3)
        assert payload["entity"]["equip_no"] == "TEST-BUCKET-21"
        assert (await resolve(h, 22))["status"] == "RESOLVED"
    assert h.closes == {"embedding": 1, "reranker": 1, "llm": 1, "db": 1}


@pytest.mark.asyncio
async def test_readiness_detects_closed_client_and_shutdown_continues_after_close_error(monkeypatch):
    async with asset_app(monkeypatch) as h:
        await initialize(h)
        await h.runtime.embedding.client.aclose()
        ready = await h.client.get("/ready")
        assert ready.status_code == 503
        assert ready.json()["model_clients"]["embedding"] == "CLOSED"
        original = h.runtime.llm.close

        async def broken_close():
            await original()
            raise RuntimeError("test shutdown failure")

        h.runtime.llm.close = broken_close
    assert h.closes == {"embedding": 1, "reranker": 1, "llm": 1, "db": 1}


async def baseline_probe():
    with pytest.MonkeyPatch.context() as monkeypatch:
        async with asset_app(monkeypatch) as h:
            await initialize(h)
            payload = await resolve(h)
            error = (payload.get("error") or {}).get("operator_message", "")
            assert payload["status"] == "UPSTREAM_ERROR", payload
            assert "Cannot send a request, as the client has been closed." in error, payload
            print(json.dumps({"baseline_status": payload["status"], "operator_message": error,
                "resource_closes_after_initialize_list_and_one_call": dict(h.closes)}, ensure_ascii=False))


if __name__ == "__main__":
    import sys
    if "--baseline" not in sys.argv:
        raise SystemExit("Use pytest for the fixed release, or --baseline with PYTHONPATH pointing to 1.1.1 Asset source")
    asyncio.run(baseline_probe())
