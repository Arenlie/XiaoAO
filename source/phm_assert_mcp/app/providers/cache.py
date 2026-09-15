"""Bounded, copy-on-read single-flight cache for model computations, never assets."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any


class AsyncMemo:
    def __init__(self, *, maxsize: int = 512, ttl: float = 300) -> None:
        self.maxsize, self.ttl = maxsize, ttl
        self.values: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self.pending: dict[str, asyncio.Task] = {}
        self.hits = self.misses = self.coalesced = 0

    async def get(self, inputs: Any, factory: Callable[[], Awaitable[Any]], *, refresh: bool = False, trace: dict | None = None) -> Any:
        trace = trace if trace is not None else {}
        if self.maxsize <= 0 or self.ttl <= 0:
            trace["mode"] = "bypass"
            return await factory()
        key = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True,
                                        default=str).encode()).hexdigest()
        cached = self.values.get(key)
        if not refresh and cached and cached[0] > time.monotonic():
            self.hits += 1
            trace["mode"] = "hit"
            self.values.move_to_end(key)
            return copy.deepcopy(cached[1])
        self.values.pop(key, None)
        task = self.pending.get(key)
        if task is None:
            # Bound in-flight bookkeeping as well as retained values.
            if len(self.pending) >= self.maxsize:
                trace["mode"] = "capacity_bypass"
                return await factory()
            self.misses += 1
            trace["mode"] = "refresh" if refresh else "miss"
            task = asyncio.create_task(factory())
            self.pending[key] = task

            def completed(done: asyncio.Task) -> None:
                if self.pending.get(key) is not done:
                    return
                self.pending.pop(key, None)
                if done.cancelled() or done.exception() is not None:
                    return
                self.values[key] = (time.monotonic() + self.ttl, copy.deepcopy(done.result()))
                while len(self.values) > self.maxsize:
                    self.values.popitem(last=False)

            task.add_done_callback(completed)
        else:
            self.coalesced += 1
            trace["mode"] = "coalesced"
        # One disconnected caller cannot cancel other callers' identical work.
        return copy.deepcopy(await asyncio.shield(task))

    async def close(self) -> None:
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.values.clear()
