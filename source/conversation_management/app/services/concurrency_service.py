from __future__ import annotations

from uuid import UUID, uuid4

from redis.asyncio import Redis

from app.config import Settings
from app.domain.exceptions import ConflictError, TooManyRequestsError

_ACQUIRE_SCRIPT = """
local now = redis.call('TIME')
local ms = tonumber(now[1])*1000 + math.floor(tonumber(now[2])/1000)
local ttl = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ms)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', ms)
if redis.call('EXISTS', KEYS[1]) == 1 then return -1 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return -2 end
if redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[3]) then return -3 end
redis.call('SET', KEYS[1], ARGV[4], 'EX', ttl)
redis.call('ZADD', KEYS[2], ms+ttl*1000, ARGV[4])
redis.call('ZADD', KEYS[3], ms+ttl*1000, ARGV[4])
redis.call('EXPIRE', KEYS[2], ttl+60)
redis.call('EXPIRE', KEYS[3], ttl+60)
return 1
"""
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local now = redis.call('TIME')
local ms = tonumber(now[1])*1000 + math.floor(tonumber(now[2])/1000)
local ttl = tonumber(ARGV[2])
redis.call('EXPIRE', KEYS[1], ttl)
redis.call('ZADD', KEYS[2], ms+ttl*1000, ARGV[1])
redis.call('ZADD', KEYS[3], ms+ttl*1000, ARGV[1])
redis.call('EXPIRE', KEYS[2], ttl+60)
redis.call('EXPIRE', KEYS[3], ttl+60)
return 1
"""


class ConcurrencyService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis, self.settings = redis, settings

    def _keys(self, conversation_id, user_token):
        import hashlib
        user = hashlib.sha256(user_token.encode()).hexdigest()
        return (f"chat:lock:conversation:{conversation_id}", f"chat:leases:v11:user:{user}", "chat:leases:v11:global")

    @property
    def ttl(self):
        return max(self.settings.task_context_ttl_seconds, int(self.settings.agent_timeout_seconds) + 60)

    async def acquire(self, conversation_id: UUID, user_token: str) -> str:
        lease_id = str(uuid4())
        result = await self.redis.eval(_ACQUIRE_SCRIPT, 3, *self._keys(conversation_id, user_token),
                                       self.ttl, self.settings.max_active_generations_per_user,
                                       self.settings.max_active_generations_global, lease_id)
        if result == -1:
            raise ConflictError("CONVERSATION_BUSY", "当前会话已有生成任务")
        if result == -2:
            raise TooManyRequestsError("USER_CONCURRENCY_LIMIT", "当前用户并发任务已达上限")
        if result == -3:
            raise TooManyRequestsError("GLOBAL_CONCURRENCY_LIMIT", "系统并发任务已达上限")
        return lease_id

    async def release(self, conversation_id: UUID | str, user_token: str, lease_id: str | None = None) -> None:
        # Legacy tasks may release only the old literal '1', never a newer lease.
        await self.redis.eval(_RELEASE_SCRIPT, 3, *self._keys(conversation_id, user_token), lease_id or "1")

    async def renew(self, conversation_id, user_token, lease_id) -> bool:
        return bool(await self.redis.eval(_RENEW_SCRIPT, 3, *self._keys(conversation_id, user_token), lease_id, self.ttl))
