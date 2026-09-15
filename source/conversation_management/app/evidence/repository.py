from __future__ import annotations

from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.evidence.models import EvidenceDraft
from app.models.evidence import (
    EvidenceLineageEdge,
    EvidenceObject,
    TaskEvidenceRef,
    TopicEvidenceRef,
)


class EvidenceRepository:
    async def create(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        source_task_id: UUID | None,
        draft: EvidenceDraft,
    ) -> EvidenceObject:
        supersedes_evidence_id = draft.supersedes_evidence_id
        # Mutable/dynamic facts are versioned, never overwritten. Resolve the most
        # recent Evidence with the same business identity and link the new observation
        # to it. This makes refresh history auditable without text-based de-duplication.
        if not draft.immutable and supersedes_evidence_id is None:
            previous = await session.scalar(
                select(EvidenceObject)
                .where(
                    EvidenceObject.conversation_id == conversation_id,
                    EvidenceObject.semantic_type == draft.semantic_type,
                    EvidenceObject.subject == dict(draft.subject),
                    EvidenceObject.scope == dict(draft.scope),
                )
                .order_by(EvidenceObject.created_at.desc(), EvidenceObject.evidence_id.desc())
                .limit(1)
            )
            if previous is not None and previous.source_task_id != source_task_id:
                supersedes_evidence_id = previous.evidence_id

        evidence = EvidenceObject(
            evidence_id=uuid4(),
            conversation_id=conversation_id,
            kind=draft.kind,
            semantic_type=draft.semantic_type,
            authority=draft.authority,
            subject=dict(draft.subject),
            scope=dict(draft.scope),
            content_descriptor=dict(draft.content_descriptor),
            summary=dict(draft.summary),
            completeness=draft.completeness.model_dump(mode="json"),
            freshness=draft.freshness.model_dump(mode="json"),
            storage_backend=draft.storage_backend,
            storage_ref=dict(draft.storage_ref),
            inline_payload=draft.inline_payload,
            source_system=draft.source_system,
            source_task_id=source_task_id,
            source_tool=draft.source_tool,
            immutable=draft.immutable,
            supersedes_evidence_id=supersedes_evidence_id,
            checksum=draft.checksum,
            observed_at=draft.observed_at or draft.freshness.observed_at,
        )
        session.add(evidence)
        await session.flush()
        if supersedes_evidence_id is not None:
            session.add(
                EvidenceLineageEdge(
                    parent_evidence_id=supersedes_evidence_id,
                    child_evidence_id=evidence.evidence_id,
                    relation_type="supersedes",
                    metadata_json={"reason": "newer_observation"},
                )
            )
            await session.flush()
        return evidence

    async def get(
        self, session: AsyncSession, *, conversation_id: UUID, evidence_id: UUID
    ) -> EvidenceObject | None:
        result = await session.execute(
            select(EvidenceObject).where(
                EvidenceObject.conversation_id == conversation_id,
                EvidenceObject.evidence_id == evidence_id,
            )
        )
        return result.scalar_one_or_none()

    async def attach_topic(
        self,
        session: AsyncSession,
        *,
        topic_id: UUID,
        evidence_id: UUID,
        role: str = "supporting",
        pinned: bool = False,
    ) -> None:
        existing = await session.get(TopicEvidenceRef, (topic_id, evidence_id))
        if existing is None:
            session.add(
                TopicEvidenceRef(
                    topic_id=topic_id,
                    evidence_id=evidence_id,
                    role=role,
                    pinned=pinned,
                )
            )
        else:
            if role == "primary" or existing.role != "primary":
                existing.role = role
            existing.pinned = bool(existing.pinned or pinned)
        await session.flush()

    async def add_task_ref(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        evidence_id: UUID,
        direction: str,
        purpose: str | None = None,
    ) -> None:
        key = (task_id, evidence_id, direction)
        existing = await session.get(TaskEvidenceRef, key)
        if existing is None:
            session.add(
                TaskEvidenceRef(
                    task_id=task_id,
                    evidence_id=evidence_id,
                    direction=direction,
                    purpose=(purpose or "")[:128] or None,
                )
            )
        elif purpose:
            existing.purpose = purpose[:128]
        await session.flush()

    async def add_lineage(
        self,
        session: AsyncSession,
        *,
        parent_evidence_id: UUID,
        child_evidence_id: UUID,
        relation_type: str,
        metadata: dict | None = None,
    ) -> None:
        if parent_evidence_id == child_evidence_id:
            return
        key = (parent_evidence_id, child_evidence_id, relation_type)
        existing = await session.get(EvidenceLineageEdge, key)
        if existing is None:
            session.add(
                EvidenceLineageEdge(
                    parent_evidence_id=parent_evidence_id,
                    child_evidence_id=child_evidence_id,
                    relation_type=relation_type,
                    metadata_json=dict(metadata or {}),
                )
            )
        await session.flush()

    async def catalog_for_topic(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        topic_id: UUID,
        limit: int = 100,
    ) -> list[EvidenceObject]:
        result = await session.execute(
            select(EvidenceObject)
            .join(TopicEvidenceRef, TopicEvidenceRef.evidence_id == EvidenceObject.evidence_id)
            .where(
                TopicEvidenceRef.topic_id == topic_id,
                EvidenceObject.conversation_id == conversation_id,
            )
            .order_by(TopicEvidenceRef.pinned.desc(), EvidenceObject.created_at.desc())
            .limit(max(1, min(limit, 500)))
        )
        return list(result.scalars())

    async def task_refs(
        self, session: AsyncSession, *, task_id: UUID, direction: str | None = None
    ) -> list[TaskEvidenceRef]:
        stmt = select(TaskEvidenceRef).where(TaskEvidenceRef.task_id == task_id)
        if direction:
            stmt = stmt.where(TaskEvidenceRef.direction == direction)
        result = await session.execute(stmt.order_by(TaskEvidenceRef.created_at.asc()))
        return list(result.scalars())

    async def lineage_parents(
        self, session: AsyncSession, *, evidence_id: UUID
    ) -> list[EvidenceLineageEdge]:
        result = await session.execute(
            select(EvidenceLineageEdge).where(
                EvidenceLineageEdge.child_evidence_id == evidence_id
            )
        )
        return list(result.scalars())

    async def lineage_children(
        self, session: AsyncSession, *, evidence_id: UUID
    ) -> list[EvidenceLineageEdge]:
        result = await session.execute(
            select(EvidenceLineageEdge).where(
                EvidenceLineageEdge.parent_evidence_id == evidence_id
            )
        )
        return list(result.scalars())
