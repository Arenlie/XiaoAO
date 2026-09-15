from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.integrations.dify.think_filter import strip_think_blocks
from app.models.branch import ConversationBranch
from app.models.context_snapshot import ContextSnapshot
from app.repositories.message_repository import MessageRepository


_UNSET = object()


class ContextBuilder:
    def __init__(self, settings: Settings, message_repo: MessageRepository) -> None:
        self.settings = settings
        self.message_repo = message_repo

    @staticmethod
    def _recent_entities(messages: list[Any], *, limit: int = 12) -> list[dict[str, Any]]:
        """Keep recent confirmed entities independently from the branch's active one.

        ``ConversationBranch.active_entity`` intentionally represents the latest
        conversational scope and can therefore change from equipment to area.  A
        later typed reference such as "这个设备" still needs the most recent
        equipment entity.  Message ``entity_result`` rows are already persisted facts,
        so retaining a compact history does not introduce another inference layer.
        """

        result: list[dict[str, Any]] = []
        for message in messages:
            payload = getattr(message, "entity_result", None)
            if not isinstance(payload, dict):
                continue
            entity = payload.get("resolved_entity") or payload.get("entity")
            if not isinstance(entity, dict) or not entity:
                continue
            result.append(
                {
                    "message_id": str(getattr(message, "id", "") or ""),
                    "role": str(getattr(message, "role", "") or "").lower(),
                    "content": str(getattr(message, "content", "") or "")[:500],
                    "entity": dict(entity),
                }
            )
        return result[-max(1, limit) :]

    async def build(
        self,
        session: AsyncSession,
        branch: ConversationBranch,
        *,
        before_leaf_id: Any = _UNSET,
    ) -> dict[str, Any]:
        leaf = branch.active_leaf_message_id if before_leaf_id is _UNSET else before_leaf_id
        path = await self.message_repo.path_to_leaf(session, leaf, conversation_id=branch.conversation_id)
        messages = [
            m
            for m in path
            if m.include_in_context and m.status in {"COMPLETED", "STOPPED"}
        ]
        from app.services.query_results import load_results, summaries
        answer_results = load_results(messages)
        recent_entities = self._recent_entities(messages)
        from app.services.sensor_references import load_references
        sensor_fault_references = load_references(messages)
        from app.services.asset_collections import load_collections
        recent_asset_queries = await load_collections(session, branch, path, self.settings)
        pending_clarification: dict[str, Any] = {}
        if messages:
            last_message = messages[-1]
            metadata = getattr(last_message, "metadata_json", None)
            clarification = metadata.get("clarification") if isinstance(metadata, dict) else None
            if (
                str(getattr(last_message, "role", "") or "").upper() == "ASSISTANT"
                and isinstance(clarification, dict)
                and clarification.get("pending") is True
            ):
                pending_clarification = dict(clarification)
        snapshot = await session.scalar(
            select(ContextSnapshot)
            .where(ContextSnapshot.branch_id == branch.id)
            .order_by(desc(ContextSnapshot.created_at))
            .limit(1)
        )
        summary = strip_think_blocks(branch.summary or "")
        if snapshot is not None:
            summary = strip_think_blocks(snapshot.summary)
            for index, message in enumerate(messages):
                if message.id == snapshot.up_to_message_id:
                    messages = messages[index + 1 :]
                    break
        messages = messages[-self.settings.context_recent_message_limit :]
        max_chars = int(self.settings.context_max_characters)

        def compact_message(role: str, content: str) -> str:
            text = str(content or "")
            if role == "ASSISTANT":
                text = strip_think_blocks(text)
                cap = max(1200, min(4000, max_chars // 4))
            else:
                cap = max(2000, min(8000, max_chars // 2))
            if len(text) <= cap:
                return text
            # Preserve both the conclusion/opening and the latest details.
            head = cap // 2
            tail = cap - head
            return text[:head] + "\n…[历史消息已压缩]…\n" + text[-tail:]

        # A long assistant report must never consume the entire context budget and
        # erase the user's latest questions.  Reserve the two most recent user turns
        # before allocating space to assistant history.
        prepared: list[tuple[str, str]] = []
        for message in messages:
            role = str(message.role or "").upper()
            content = compact_message(role, message.content)
            if content:
                prepared.append((role, content))

        protected_user_indexes = [
            i for i, (role, _) in enumerate(prepared) if role == "USER"
        ][-2:]
        protected_remaining = sum(len(prepared[i][1]) for i in protected_user_indexes)

        # Keep summary useful but bounded so recent turns always have room.
        summary_cap = max(1000, min(4000, max_chars // 3))
        if len(summary) > summary_cap:
            summary = summary[-summary_cap:]

        selected: list[dict[str, str]] = []
        total_chars = len(summary)
        protected = set(protected_user_indexes)
        for index in range(len(prepared) - 1, -1, -1):
            role, content = prepared[index]
            remaining = max_chars - total_chars
            if remaining <= 0:
                break
            if index in protected:
                protected_remaining -= len(content)
                allowed = remaining
            else:
                allowed = max(0, remaining - protected_remaining)
            if allowed <= 0:
                continue
            if len(content) > allowed:
                content = content[-allowed:]
            selected.append({"role": role.lower(), "content": content})
            total_chars += len(content)
        selected.reverse()
        return {
            "summary": summary,
            "recent_messages": selected,
            "recent_entities": recent_entities,
            "recent_asset_queries": recent_asset_queries,
            "answer_results": answer_results,
            "recent_results": summaries(answer_results),
            "sensor_fault_references": sensor_fault_references,
            "pending_clarification": pending_clarification,
        }

    @staticmethod
    def serialize(value: dict[str, Any] | None) -> str:
        return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))
