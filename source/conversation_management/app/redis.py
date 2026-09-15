from __future__ import annotations

from redis.asyncio import Redis

from app.config import Settings


class RedisManager:
    def __init__(self, settings: Settings) -> None:
        max_block_ms = max(
            settings.redis_stream_block_ms,
            settings.generation_queue_block_ms,
            settings.sse_xread_block_ms,
        )
        if settings.redis_socket_timeout_seconds * 1000 <= max_block_ms:
            raise ValueError(
                "REDIS_SOCKET_TIMEOUT_SECONDS must be greater than every Redis "
                f"blocking-read interval; timeout={settings.redis_socket_timeout_seconds}s, "
                f"max_block={max_block_ms}ms"
            )
        self.client: Redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout_seconds,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_keepalive=True,
            health_check_interval=30,
        )

    async def ping(self) -> None:
        await self.client.ping()

    async def close(self) -> None:
        await self.client.aclose()
