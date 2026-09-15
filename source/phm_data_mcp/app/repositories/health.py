from __future__ import annotations

import json
from typing import Any

import redis
from pymongo import DESCENDING, MongoClient

from app.config import Settings
from app.time_utils import parse_time, to_millis, timezone


class HealthRepository:
    """Read device health history from MongoDB and live space health from Redis."""

    def __init__(self, settings: Settings):
        timeout_ms = settings.query_timeout_seconds * 1000
        self.settings = settings
        self.mongo_client = MongoClient(
            settings.health_mongo_uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            socketTimeoutMS=timeout_ms,
            tz_aware=True,
            tzinfo=timezone(),
        )
        self.mongo_db = self.mongo_client[settings.health_mongo_db]
        self.redis = redis.Redis(
            host=settings.health_redis_host,
            port=settings.health_redis_port,
            db=settings.health_redis_db,
            socket_timeout=settings.query_timeout_seconds,
            decode_responses=True,
        )

    def close(self) -> None:
        self.mongo_client.close()
        self.redis.close()

    def ping_mongo(self) -> bool:
        self.mongo_client.admin.command("ping")
        return True

    def ping_redis(self) -> bool:
        return bool(self.redis.ping())

    def get_device_health(
        self,
        device_id: str,
        start_time: str | None = None,
        end_time: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        collection = self.mongo_db[f"health_result_new:{device_id}"]

        if not start_time and not end_time:
            return collection.find_one(
                {},
                projection={"_id": 0},
                sort=[("ts", DESCENDING)],
                max_time_ms=self.settings.mongo_query_max_time_ms,
            )

        query: dict[str, Any] = {}
        time_query: dict[str, int] = {}
        if start_time:
            start = parse_time(start_time)
            if start:
                time_query["$gte"] = to_millis(start)
        if end_time:
            end = parse_time(end_time)
            if end:
                time_query["$lte"] = to_millis(end)
        if "$gte" in time_query and "$lte" in time_query and time_query["$gte"] > time_query["$lte"]:
            raise ValueError("start_time不能晚于end_time")
        if time_query:
            query["ts"] = time_query

        return list(
            collection.find(query, projection={"_id": 0})
            .sort("ts", DESCENDING)
            .limit(max(1, min(int(limit), 1000)))
            .max_time_ms(self.settings.mongo_query_max_time_ms)
        )

    def get_space_health(self, space_id: str) -> dict[str, Any] | None:
        raw = self.redis.get(f"SPACE_HEALTH{space_id}")
        if raw is None:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("区域健康度Redis数据格式错误")
        return data
