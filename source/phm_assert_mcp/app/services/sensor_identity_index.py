"""Read-only identity snapshots. Live status never comes from this cache."""
from __future__ import annotations

import asyncio
import time
import logging

from app.providers.sensor_agent import SensorError
from app.services.sensor_registry import SensorRegistry, point_aliases


class SensorIdentityIndex:
    def __init__(self, client, ttl=300):
        self.client, self.ttl = client, ttl
        self.snapshot = self.last_complete = None
        self.expires = 0.0
        self.pending = self.background = None
        self.closed = False

    async def _refresh(self):
        snapshot = SensorRegistry.parse(await self.client.monitored_points(refresh=True))
        # Construct all indexes before publishing. Partial snapshots do not replace
        # the last complete map; neither old map nor merged rows prove live absence.
        self.snapshot = snapshot
        if snapshot.complete:
            self.last_complete = snapshot
        self.expires = time.monotonic() + (self.ttl if snapshot.complete else min(self.ttl, 10))
        return snapshot

    async def get(self, *, refresh=False):
        error = None
        if refresh or self.snapshot is None or time.monotonic() >= self.expires:
            if self.pending is None or self.pending.done():
                self.pending = asyncio.create_task(self._refresh())
            try:
                await asyncio.shield(self.pending)
            except SensorError as exc:
                error = exc.payload()["error"]
                if self.last_complete is None:
                    raise
        if self.background is None and self.ttl > 0 and getattr(getattr(self.client, "settings", None), "sensor_agent_max_parallel", 4) > 1:
            self.background = asyncio.create_task(self._loop())
        return (self.last_complete if error else self.snapshot), error

    async def _loop(self):
        while not self.closed:
            await asyncio.sleep(max(1, self.ttl))
            try:
                # One registry request at most; never a per-point background sweep.
                await self.get(refresh=True)
            except Exception:
                logging.getLogger(__name__).warning("sensor_identity_background_refresh_failed")

    async def close(self):
        self.closed = True
        tasks = [t for t in (self.pending, self.background) if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def resolve_tokens(registry, tokens, *, stale=False):
    sets = []
    point_tokens = []
    for token in tokens:
        equipment_rows = registry.by_equipment.get(token.casefold(), [])
        alias_rows = registry.by_alias.get(token, [])
        rows = equipment_rows + alias_rows
        if alias_rows and not equipment_rows:
            point_tokens.append(token)
        sets.append({(r["equip_num"], r["point_num"]) for r in rows})
    keys = set.intersection(*sets) if sets else set()
    targets = []
    seen = set()
    for eq, pt in sorted(keys):
        row = next(r for r in registry.find(eq, pt) if r["point_num"] == pt)
        key = (eq, pt) if point_tokens else (eq, None)
        if key in seen:
            continue
        seen.add(key)
        targets.append({"equip_num": eq, "point_num": pt if point_tokens else None,
            "equip_name": row.get("equip_name"), "point_name": row.get("point_name") if point_tokens else None,
            "aliases": sorted(point_aliases(row)) if point_tokens else [],
            "identity_source": "sensor_registry", "mapping_stale": stale})
    return {"targets": targets[:100], "match_count": len(targets), "output_limited": len(targets) > 100,
            "input_tokens": tokens, "registry": registry.metadata(), "mapping_stale": stale,
            "absence_verified": not targets and registry.complete and not stale}
