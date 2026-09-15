"""Request idempotency scoped by user, app, client session and conversation."""
from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from app.domain.exceptions import ConflictError


class IdempotencyGuard:
    def __init__(self, redis, *, user, app, client_session, conversation, key, payload, ttl):
        scope = [user, app, client_session or "legacy", str(conversation or "new"), key]
        self.digest = hashlib.sha256(json.dumps(scope).encode()).hexdigest()
        self.redis_key = "chat:idempotency:v11:" + self.digest
        self.fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        self.redis, self.ttl = redis, ttl
        self.owner = str(uuid4())
        self.claim = json.dumps({"owner": self.owner, "fingerprint": self.fingerprint})

    async def begin(self):
        acquired = await self.redis.set(self.redis_key, self.claim, nx=True, ex=self.ttl)
        if acquired:
            return None
        raw = await self.redis.get(self.redis_key)
        if not raw:
            raise ConflictError("REQUEST_IN_PROGRESS", "请求状态正在变化，请使用原请求标识重试。")
        existing = json.loads(raw)
        if existing.get("fingerprint") != self.fingerprint:
            raise ConflictError("IDEMPOTENCY_CONFLICT", "同一请求标识对应的内容发生变化，请为新消息使用新的请求标识。")
        if existing.get("accepted"):
            return existing["accepted"]
        raise ConflictError("REQUEST_IN_PROGRESS", "这条消息正在提交，请使用原请求标识稍后重试。")

    async def finish(self, accepted):
        value = json.dumps({"fingerprint": self.fingerprint, "accepted": accepted}, ensure_ascii=False)
        await self.redis.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then "
                              "return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) end return 0",
                              1, self.redis_key, self.claim, value, self.ttl)

    async def abort(self):
        await self.redis.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then "
                              "return redis.call('DEL', KEYS[1]) end return 0",
                              1, self.redis_key, self.claim)
