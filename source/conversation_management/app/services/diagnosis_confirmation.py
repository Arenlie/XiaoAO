"""Persistent, task-owned diagnosis choices. Waiting never occupies a worker lease."""
from __future__ import annotations

import json
from contextlib import suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4
from sqlalchemy import select

from app.domain.exceptions import ConflictError, NotFoundError
from app.models.branch import ConversationBranch
from app.models.conversation import Conversation
from app.models.generation_task import GenerationTask
from app.models.message import Message
from app.orchestration.diagnosis_choice import public_confirmation


class DiagnosisConfirmationMixin:
    async def get_diagnosis_confirmation(self, task_id, user_token):
        async with self.session_factory() as session:
            task = await self.task_repo.get_owned(session, task_id, user_token)
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
            message = await session.get(Message, task.assistant_message_id)
            value = dict((message.metadata_json or {}).get("diagnosis_confirmation") or {}) if message else {}
            if task.status not in {"WAITING_CONFIRMATION", "COMPLETED"} or value.get("status") != "PENDING":
                return None
            if datetime.fromisoformat(value["expires_at"]) > datetime.now(UTC):
                if task.status == "COMPLETED":
                    return public_confirmation(task, value)
                pending = True
            else:
                pending = False
        if pending:
            # Compatibility for a prompt produced before the upgrade. Reading it
            # finalizes its chat turn while retaining its conversation choice.
            await self._complete_legacy_diagnosis_prompt(task_id, user_token)
            task.status = "COMPLETED"
            return public_confirmation(task, value)
        await self._expire_diagnosis_confirmation(task_id, user_token)
        return None

    async def _complete_legacy_diagnosis_prompt(self, task_id, user_token):
        async with self.session_factory() as session, session.begin():
            task = await self.task_repo.get_owned(session, task_id, user_token, for_update=True)
            if task is None or task.status != "WAITING_CONFIRMATION":
                return
            message = await session.get(Message, task.assistant_message_id, with_for_update=True)
            value = (message.metadata_json or {}).get("diagnosis_confirmation") or {}
            if value.get("status") != "PENDING":
                return
            prompt = public_confirmation(task, value)["question"]
            message.content, message.status = prompt, "COMPLETED"
            task.status, task.worker_id, task.completed_at = "COMPLETED", None, datetime.now(UTC)
            mid = task.assistant_message_id
        common = {"message_id":str(mid), "generation_id":str(uuid4()), "channel":"final", "output_kind":"confirmation_prompt"}
        await self.events.publish(task_id,"answer.started",common,message_id=mid,status="STARTED")
        await self.events.publish(task_id,"answer.delta",{**common,"content":prompt,"status":"COMPLETED"},message_id=mid,status="COMPLETED")
        await self.events.publish(task_id,"answer.completed",{**common,"content":prompt,"replace":True,"status":"COMPLETED","content_length":len(prompt)},message_id=mid,status="COMPLETED")
        await self.events.publish(task_id,"task.completed",{"task_id":str(task_id),"message_id":str(mid)},message_id=mid,status="COMPLETED")

    async def _expire_diagnosis_confirmation(self, task_id, user_token):
        async with self.session_factory() as session, session.begin():
            task = await self.task_repo.get_owned(session, task_id, user_token, for_update=True)
            if task is None or task.status not in {"WAITING_CONFIRMATION", "COMPLETED"}:
                return
            message = await session.get(Message, task.assistant_message_id, with_for_update=True)
            meta = dict(message.metadata_json or {})
            value = dict(meta.get("diagnosis_confirmation") or {})
            if not value or datetime.fromisoformat(value["expires_at"]) > datetime.now(UTC):
                return
            value.update(status="EXPIRED")
            meta["diagnosis_confirmation"] = value
            message.metadata_json = meta
            was_waiting = task.status == "WAITING_CONFIRMATION"
            if was_waiting:
                message.status = "STOPPED"
                task.status, task.completed_at = "STOPPED", datetime.now(UTC)
        await self.events.publish(task_id, "diagnosis.mode.expired", {"confirmation_id":value["confirmation_id"],
            "display_message":"本次分析方式选择已过期，未启动详细诊断。"}, status="EXPIRED")
        if was_waiting:
            await self.events.publish(task_id, "task.stopped", {"task_id":str(task_id), "reason":"confirmation_expired"}, status="STOPPED")

    async def select_diagnosis_mode(self, task_id, user_token, confirmation_id, mode):
        if mode not in {"quick", "detailed", "cancel"}:
            raise ConflictError("DIAGNOSIS_MODE_INVALID", "请选择快速分析、详细诊断或取消")
        async with self.session_factory() as session:
            task = await self.task_repo.get_owned(session, task_id, user_token)
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
            message = await session.get(Message, task.assistant_message_id)
            value = dict((message.metadata_json or {}).get("diagnosis_confirmation") or {}) if message else {}
            if value.get("confirmation_id") == str(confirmation_id) and value.get("status") == "SELECTED" and value.get("mode") == mode:
                return task  # Idempotent retry of an already committed choice.
            conversation_id = task.conversation_id
        lease = await self.concurrency.acquire(conversation_id, user_token)
        outbox_id = None
        try:
            async with self.session_factory() as session, session.begin():
                task = await self.task_repo.get_owned(session, task_id, user_token, for_update=True)
                if task is None:
                    raise NotFoundError("TASK_NOT_FOUND", "任务不存在")
                message = await session.get(Message, task.assistant_message_id, with_for_update=True)
                meta = dict(message.metadata_json or {})
                value = dict(meta.get("diagnosis_confirmation") or {})
                if task.status not in {"WAITING_CONFIRMATION", "COMPLETED"} or value.get("status") != "PENDING" or value.get("confirmation_id") != str(confirmation_id):
                    raise ConflictError("DIAGNOSIS_CONFIRMATION_STALE", "该选择已结束，请以当前问题为准")
                if datetime.fromisoformat(value["expires_at"]) <= datetime.now(UTC):
                    raise ConflictError("DIAGNOSIS_CONFIRMATION_EXPIRED", "该选择已过期，请重新提出分析请求")
                branch = await session.get(ConversationBranch, task.branch_id, with_for_update=True)
                conversation = await session.get(Conversation, task.conversation_id)
                if (branch is None or conversation is None or conversation.active_branch_id != task.branch_id
                        or branch.active_leaf_message_id != task.assistant_message_id):
                    raise ConflictError("DIAGNOSIS_CONFIRMATION_STALE", "已有新的问题或会话分支，不能恢复旧诊断")
                value.update(status="SELECTED", mode=mode)
                meta["diagnosis_confirmation"] = value
                message.metadata_json = meta
                if mode == "cancel":
                    task.status, task.completed_at = "STOPPED", datetime.now(UTC)
                    message.status = "STOPPED"
                else:
                    key = f"chat:task_context:{task_id}"
                    raw = await self.redis.get(key)
                    if not raw:
                        raise ConflictError("TASK_CONTEXT_EXPIRED", "任务上下文已过期，请重新提出分析请求")
                    context = json.loads(raw)
                    context.update(lease_id=lease, graph_attempt=int(context.get("graph_attempt") or 0)+1,
                        diagnosis_choice={"source":"user_confirmation", "mode":mode, "confirmation_id":str(confirmation_id)},
                        diagnosis_resume_state=dict(meta.get("diagnosis_resume_state") or {}))
                    await self.redis.setex(key, self.settings.task_context_ttl_seconds, json.dumps(context,ensure_ascii=False))
                    await self.redis.delete(f"chat:cancel:{task_id}")
                    task.status, task.worker_id, task.completed_at = "QUEUED", None, None
                    task.error_code, task.error_message = None, None
                    message.status, message.content = "PENDING", ""
                    if self.settings.outbox_enabled:
                        event = self.outbox.add(session, conversation_id=task.conversation_id,
                            aggregate_type="generation_task", aggregate_id=task.id, event_type="generation.enqueue",
                            payload={"task_id":str(task.id), "reason":"diagnosis_mode_selected"},
                            deduplication_key=f"generation.enqueue:{task.id}:diagnosis_mode:{confirmation_id}")
                        outbox_id = event.id
            with suppress(Exception):
                await self.events.publish(task_id, "diagnosis.mode.selected",
                    {"confirmation_id":str(confirmation_id), "mode":mode}, status="COMPLETED")
            if mode == "cancel":
                await self.events.publish(task_id,"task.stopped", {"task_id":str(task_id)}, status="STOPPED")
                await self.concurrency.release(conversation_id, user_token, lease)
            elif self.settings.outbox_enabled:
                if self.settings.outbox_fast_publish_enabled and outbox_id:
                    await self.outbox.publish_fast(outbox_id)
            else:
                await self.queue.enqueue_generation(task_id)
            return task
        except BaseException:
            await self.concurrency.release(conversation_id, user_token, lease)
            raise
