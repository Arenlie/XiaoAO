from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.services.asset_collections import signed_arguments, validate_response
from app.services.query_results import identity
from app.tools.contracts import ToolCallRequest


class AssetSnapshotMaterializationError(RuntimeError):
    pass


class AssetSnapshotBackend:
    """Read the exact immutable member set created by PHM Asset MCP.

    Evidence PostgreSQL stores only the snapshot id.  The actual frozen rows remain in
    ``phm_asset_query_runs/phm_asset_query_members`` and are fetched only when a Broker
    request needs members.  This keeps Evidence metadata small while preserving the
    count -> list / list -> enrich identity boundary.
    """

    def __init__(self, *, client, settings) -> None:
        self.client = client
        self.settings = settings

    async def materialize(
        self,
        *,
        call_request: ToolCallRequest,
        snapshot_id: str,
        max_rows: int = 50000,
    ) -> dict[str, Any]:
        if not snapshot_id:
            raise AssetSnapshotMaterializationError("asset_snapshot_id_missing")
        query: dict[str, Any] = {
            "reference": {"query_id": str(snapshot_id), "mode": "same_set"},
            "operation": "list",
            "freshness": "referenced_snapshot",
            "page_size": 5000,
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        result_complete = True
        last_payload: dict[str, Any] = {}
        while True:
            request = call_request.model_copy(
                update={
                    "arguments": {"query": query},
                    "asset_allowed_query_ids": list(
                        dict.fromkeys(
                            [
                                *(getattr(call_request, "asset_allowed_query_ids", []) or []),
                                str(snapshot_id),
                            ]
                        )
                    ),
                }
            )
            args = signed_arguments(request, SimpleNamespace(settings=self.settings))
            payload = await self.client.call_tool("query_asset_collection", args)
            if not isinstance(payload, dict) or payload.get("success") is False:
                raise AssetSnapshotMaterializationError("asset_snapshot_query_failed")
            validate_response(query, payload)
            last_payload = payload
            rows.extend(identity(item) for item in payload.get("devices") or [])
            total = int(payload.get("count", len(rows)) or 0)
            result_complete = result_complete and bool(payload.get("result_complete"))
            cursor = payload.get("next_cursor")
            if not cursor:
                break
            if cursor in seen or len(rows) >= max_rows:
                raise AssetSnapshotMaterializationError("asset_snapshot_materialization_budget_exceeded")
            seen.add(cursor)
            query = {**query, "cursor": cursor}
        if len(rows) != total:
            result_complete = False
        return {
            "rows": rows,
            "total_count": total,
            "returned_count": len(rows),
            "complete": result_complete,
            "snapshot_id": str(last_payload.get("query_id") or snapshot_id),
            "snapshot_at": last_payload.get("snapshot_at"),
            "expires_at": last_payload.get("expires_at"),
        }
