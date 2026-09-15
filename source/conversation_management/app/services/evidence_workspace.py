from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.cold_index import search_topics
from app.context.hot_window import SLOT_NAMES, reorder_hot_topics
from app.context.models import ContextResolutionDecision
from app.context.repository import TopicRepository
from app.evidence.adapters import adapt_answer_result, adapt_attachment_result, adapt_tool_observation
from app.evidence.catalog import to_catalog_entry
from app.evidence.models import EvidenceCompleteness, EvidenceDraft
from app.evidence.repository import EvidenceRepository
from app.evidence.matching import requirement_entry_satisfies
from app.planning.capability_registry import CapabilityRegistry
from app.planning.gap_resolver import GapResolver
from app.planning.task_compiler import TaskCompiler
from app.planning.task_delta import build_task_delta

log = structlog.get_logger(__name__)


class EvidenceWorkspaceService:
    """Persistent Conversation state around Topics and Evidence.

    QueryPlan/ResultFollowup/AnswerResult remain compatibility projections. This service
    owns the durable subject/topic/evidence graph and can rebuild LLM context after a
    complete Redis loss.
    """

    def __init__(self, *, session_factory, settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.topics = TopicRepository()
        self.evidence = EvidenceRepository()
        self.compiler = TaskCompiler()
        self.capabilities = CapabilityRegistry()
        self.gap_resolver = GapResolver(self.capabilities)

    @staticmethod
    def _topic_title(query: str, subject: dict[str, Any]) -> str:
        name = str(
            subject.get("name")
            or subject.get("equipment")
            or subject.get("area")
            or subject.get("equip_no")
            or subject.get("point_no")
            or ""
        ).strip()
        if name:
            return f"{name}：{query.strip()[:80]}"[:300]
        compact = " ".join((query or "").strip().split())
        return compact[:120] or "新主题"

    @staticmethod
    def _classification_subject(classification: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        semantics = classification.get("asset_semantics") if isinstance(classification.get("asset_semantics"), dict) else {}
        subject: dict[str, Any] = {}
        for key in ("area", "equipment", "equipment_type", "point", "component", "position", "direction", "measurement"):
            value = semantics.get(key)
            if isinstance(value, dict):
                raw = str(value.get("raw_text") or value.get("retrieval_text") or "").strip()
                if raw:
                    subject[key] = raw
        for key in ("equip_no", "point_no"):
            value = semantics.get(key)
            if isinstance(value, dict):
                raw = str(value.get("raw_text") or value.get("retrieval_text") or "").strip()
                if raw:
                    subject[key] = raw
            elif value:
                subject[key] = str(value)
        # Only reuse the branch anchor when Task Understanding explicitly continues a
        # prior topic.  For a NEW_TOPIC we merge resolved identity only when this turn
        # actually expressed/requested an asset, avoiding inheritance from an unrelated
        # previous subject while still persisting real equip_no/point_no/space_id.
        action = str((classification.get("context_resolution") or {}).get("action") or "")
        needs_lookup = bool(semantics.get("needs_asset_lookup"))
        may_merge_resolved = bool(subject or needs_lookup or action != "NEW_TOPIC")
        if may_merge_resolved:
            for state_key in ("resolved_entity", "selected_entity", "active_entity"):
                entity = state.get(state_key)
                if isinstance(entity, dict) and entity:
                    resolved = {
                        k: entity.get(k)
                        for k in ("entity_type", "name", "equip_no", "equipment_no", "point_no", "pointNo", "space_id", "area")
                        if entity.get(k) not in (None, "")
                    }
                    if resolved:
                        # Stable IDs from entity resolution override raw natural-language
                        # placeholders; semantic labels remain available for retrieval.
                        subject.update(resolved)
                        break
        return subject

    async def _catalog(self, session: AsyncSession, conversation_id: UUID, topic_id: UUID) -> list[dict[str, Any]]:
        rows = await self.evidence.catalog_for_topic(
            session,
            conversation_id=conversation_id,
            topic_id=topic_id,
            limit=int(getattr(self.settings, "evidence_catalog_max_items", 64)),
        )
        return [to_catalog_entry(item) for item in rows]

    async def _manifest(self, session: AsyncSession, topic, *, slot: str | None) -> dict[str, Any]:
        catalog = await self._catalog(session, topic.conversation_id, topic.topic_id)
        return {
            "topic_id": str(topic.topic_id),
            "slot": slot,
            "title": topic.title,
            "topic_summary": topic.topic_summary,
            "subject": dict(topic.primary_subject or {}),
            "scope": dict(topic.scope or {}),
            "goal_summary": str(topic.current_goal or ""),
            "evidence_types": list(dict.fromkeys(str(x.get("semantic_type") or "") for x in catalog if x.get("semantic_type")))[:32],
            "updated_at": topic.updated_at.isoformat() if topic.updated_at else None,
        }

    async def _lazy_migrate_legacy(
        self,
        session: AsyncSession,
        *,
        conversation_id: UUID,
        legacy_context: dict[str, Any],
    ) -> None:
        existing = await self.topics.list_recent(session, conversation_id, limit=1)
        if existing:
            return
        results = [x for x in legacy_context.get("answer_results") or legacy_context.get("recent_results") or [] if isinstance(x, dict)]
        asset_queries = [x for x in legacy_context.get("recent_asset_queries") or [] if isinstance(x, dict)]
        if not results and not asset_queries and not legacy_context.get("summary"):
            return
        subject = {}
        recent_entities = [x for x in legacy_context.get("recent_entities") or [] if isinstance(x, dict)]
        if recent_entities:
            subject = dict(recent_entities[-1])
        topic = await self.topics.create(
            session,
            conversation_id=conversation_id,
            title="历史对话（自动迁移）",
            topic_summary=str(legacy_context.get("summary") or "")[-3000:],
            primary_subject=subject,
            current_goal="从旧版 AnswerResult / 上下文按需恢复",
            searchable_text=" ".join([str(legacy_context.get("summary") or ""), str(subject)])[:16000],
        )
        await self.topics.replace_slots(session, conversation_id, [topic.topic_id])
        # Old AnswerResults often lack their original assistant message id in ContextBuilder.
        # Register minimal metadata evidence rather than fabricating a storage locator.
        for result in results[-8:]:
            plan = result.get("plan") if isinstance(result.get("plan"), dict) else {}
            domain = str(plan.get("domain") or "")
            semantic = {"asset": "entity_set", "health": "health_score", "alarm": "alarm_set", "sensor": "sensor_fault_set"}.get(domain, "dataset_result")
            total = result.get("total_count")
            try:
                total_count = int(total) if total is not None else len(result.get("rows") or [])
            except (TypeError, ValueError):
                total_count = len(result.get("rows") or [])
            draft = EvidenceDraft(
                kind="dataset",
                semantic_type=semantic,
                authority="AUTHORITATIVE",
                subject=subject,
                summary={"legacy_result_id": result.get("result_id"), "domain": domain, "count": total_count},
                completeness=EvidenceCompleteness(
                    status="complete" if result.get("source_complete") is True else "unknown",
                    expected_count=total_count,
                    available_count=total_count if result.get("source_complete") is True else len(result.get("rows") or []),
                    materialized_count=len(result.get("rows") or []),
                    has_more=bool(total_count > len(result.get("rows") or [])),
                    reason="lazy_migration_from_legacy_answer_result",
                ),
                freshness={"observed_at": datetime.now(UTC), "immutable": False, "freshness_class": "slow_changing"},
                storage_backend="legacy_context_reference",
                storage_ref={"result_id": str(result.get("result_id") or "")},
                source_system="legacy_migration",
                immutable=False,
                observed_at=datetime.now(UTC),
            )
            ev = await self.evidence.create(session, conversation_id=conversation_id, source_task_id=None, draft=draft)
            await self.evidence.attach_topic(session, topic_id=topic.topic_id, evidence_id=ev.evidence_id, role="historical")
        log.info("evidence_legacy_context_migrated", conversation_id=str(conversation_id), topic_id=str(topic.topic_id), result_count=len(results))

    async def prepare_context(
        self,
        *,
        conversation_id: UUID,
        query: str,
        legacy_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.session_factory() as session, session.begin():
            await self._lazy_migrate_legacy(
                session,
                conversation_id=conversation_id,
                legacy_context=dict(legacy_context or {}),
            )
            slots = await self.topics.load_slots(session, conversation_id)
            hot_ids = [item.topic_id for item in slots]
            hot: list[dict[str, Any]] = []
            for slot in slots:
                topic = await self.topics.get(session, conversation_id, slot.topic_id)
                if topic is not None:
                    hot.append(await self._manifest(session, topic, slot=SLOT_NAMES.get(slot.slot_no)))
            recent = await self.topics.list_recent(
                session,
                conversation_id,
                limit=int(getattr(self.settings, "evidence_cold_search_scan_limit", 80)),
            )
            cold_pool = [item for item in recent if item.topic_id not in hot_ids]
            cold = search_topics(query, cold_pool, limit=int(getattr(self.settings, "evidence_cold_search_candidates", 5)))
            cold_manifests = [await self._manifest(session, topic, slot="COLD") for topic in cold]
            current_catalog: list[dict[str, Any]] = []
            if hot_ids:
                current_catalog = await self._catalog(session, conversation_id, hot_ids[0])
            return {
                "current_topic_id": str(hot_ids[0]) if hot_ids else None,
                "hot_topic_manifests": hot,
                "cold_topic_candidates": cold_manifests,
                "evidence_catalog": current_catalog,
                "evidence_catalog_scope": "CURRENT",
            }

    async def activate_turn(
        self,
        *,
        conversation_id: UUID,
        task_id: UUID,
        query: str,
        classification: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        async with self.session_factory() as session, session.begin():
            slots = await self.topics.load_slots(session, conversation_id)
            hot_ids = [item.topic_id for item in slots]
            raw_decision = classification.get("context_resolution") if isinstance(classification.get("context_resolution"), dict) else {}
            try:
                decision = ContextResolutionDecision.model_validate(raw_decision or {"action": "NEW_TOPIC"})
            except Exception:
                decision = ContextResolutionDecision(action="NEW_TOPIC", reason="invalid_context_resolution", confidence=0.0)

            target = None
            if decision.action == "CONTINUE_CURRENT" and hot_ids:
                target = await self.topics.get(session, conversation_id, hot_ids[0])
            elif decision.action == "RESUME_PREVIOUS_1" and len(hot_ids) > 1:
                target = await self.topics.get(session, conversation_id, hot_ids[1])
            elif decision.action == "RESUME_PREVIOUS_2" and len(hot_ids) > 2:
                target = await self.topics.get(session, conversation_id, hot_ids[2])
            elif decision.action == "RESUME_COLD_TOPIC" and decision.topic_id:
                target = await self.topics.get(session, conversation_id, decision.topic_id)
            elif decision.topic_id and decision.action != "NEW_TOPIC":
                target = await self.topics.get(session, conversation_id, decision.topic_id)

            subject = self._classification_subject(classification, state)
            scope = dict(state.get("query_scope") or {}) if isinstance(state.get("query_scope"), dict) else {}
            goal = classification.get("goal_frame") if isinstance(classification.get("goal_frame"), dict) else {}
            goal_text = str(goal.get("goal") or query)[:4000]
            created = False
            if target is None:
                decision = ContextResolutionDecision(action="NEW_TOPIC", reason=(decision.reason or "no_valid_reusable_topic"), confidence=decision.confidence)
                target = await self.topics.create(
                    session,
                    conversation_id=conversation_id,
                    title=self._topic_title(query, subject),
                    primary_subject=subject,
                    scope=scope,
                    current_goal=goal_text,
                    searchable_text=" ".join([query, json.dumps(subject, ensure_ascii=False), json.dumps(scope, ensure_ascii=False)])[:16000],
                    last_task_id=task_id,
                )
                created = True
            else:
                merged_subject = dict(target.primary_subject or {})
                merged_subject.update({k: v for k, v in subject.items() if v not in (None, "")})
                merged_scope = dict(target.scope or {})
                merged_scope.update(scope)
                await self.topics.update(
                    session,
                    target,
                    primary_subject=merged_subject,
                    scope=merged_scope,
                    current_goal=goal_text,
                    searchable_text=" ".join([target.searchable_text or "", query, json.dumps(subject, ensure_ascii=False)])[-16000:],
                    last_task_id=task_id,
                )

            new_hot = reorder_hot_topics(hot_ids, target.topic_id)
            await self.topics.replace_slots(session, conversation_id, new_hot)
            # Keep status cheap and deterministic: hot topics ACTIVE, older topics COLD.
            for topic in await self.topics.list_recent(session, conversation_id, limit=100):
                desired = "ACTIVE" if topic.topic_id in new_hot else "COLD"
                if topic.status != desired:
                    topic.status = desired

            catalog = await self._catalog(session, conversation_id, target.topic_id)
            requirements = self.compiler.compile(
                classification,
                subject_constraint=dict(target.primary_subject or {}),
                scope_constraint=dict(target.scope or {}),
            )
            task_delta = build_task_delta({**classification, "context_resolution": decision.model_dump(mode="json")})
            # Record only Evidence that genuinely satisfies the compiled requirement
            # (subject/scope/authority/freshness/completeness/fields), not merely an
            # Evidence with the same semantic family. This prevents a stale or wrong-
            # subject fact from entering diagnosis lineage as an INPUT.
            requirement_dicts = [item.model_dump(mode="json") for item in requirements]
            for entry in catalog:
                matched = [req for req in requirement_dicts if requirement_entry_satisfies(entry, req)[0]]
                if not matched:
                    continue
                await self.evidence.add_task_ref(
                    session,
                    task_id=task_id,
                    evidence_id=UUID(str(entry["evidence_id"])),
                    direction="INPUT",
                    purpose="reuse:" + ",".join(str(req.get("family") or req.get("semantic_type") or "evidence") for req in matched)[:110],
                )
                log.info(
                    "evidence.reused",
                    task_id=str(task_id),
                    evidence_id=str(entry.get("evidence_id") or ""),
                    semantic_type=str(entry.get("semantic_type") or ""),
                )

            return {
                "topic_workspace": {
                    "topic_id": str(target.topic_id),
                    "title": target.title,
                    "topic_summary": target.topic_summary,
                    "primary_subject": dict(target.primary_subject or {}),
                    "scope": dict(target.scope or {}),
                    "current_goal": target.current_goal,
                    "context_action": decision.action,
                    "context_reason": decision.reason,
                    "created": created,
                },
                "evidence_catalog": catalog,
                "task_delta": task_delta.model_dump(mode="json"),
                "evidence_requirements": [item.model_dump(mode="json") for item in requirements],
                "context_resolution": decision.model_dump(mode="json"),
            }

    @staticmethod
    def _topic_summary(state: dict[str, Any], evidence_types: list[str]) -> str:
        topic = state.get("topic_workspace") if isinstance(state.get("topic_workspace"), dict) else {}
        intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
        goal = intent.get("goal_frame") if isinstance(intent.get("goal_frame"), dict) else {}
        subject = topic.get("primary_subject") or state.get("resolved_entity") or state.get("active_entity") or {}
        parts = []
        if subject:
            parts.append("主体=" + json.dumps(subject, ensure_ascii=False, default=str)[:600])
        if goal.get("goal"):
            parts.append("当前目标=" + str(goal.get("goal"))[:600])
        if state.get("final_answer"):
            parts.append("最近结论=" + str(state.get("final_answer"))[:1200])
        if evidence_types:
            parts.append("已有Evidence=" + ",".join(list(dict.fromkeys(evidence_types))[:24]))
        return "；".join(parts)[:4000]

    async def persist_turn(
        self,
        session: AsyncSession,
        *,
        task_id: UUID,
        conversation_id: UUID,
        assistant_message_id: UUID,
        state: dict[str, Any],
        final_answer: str,
    ) -> dict[str, Any]:
        # Transaction-level idempotency: _persist_final is atomic; a successful prior
        # write leaves OUTPUT refs, so a replay does not duplicate immutable Evidence.
        existing_outputs = await self.evidence.task_refs(session, task_id=task_id, direction="OUTPUT")
        if existing_outputs:
            return {
                "topic_id": str((state.get("topic_workspace") or {}).get("topic_id") or ""),
                "output_evidence_ids": [str(item.evidence_id) for item in existing_outputs],
                "input_evidence_ids": [str(item.evidence_id) for item in await self.evidence.task_refs(session, task_id=task_id, direction="INPUT")],
                "idempotent_reuse": True,
            }

        raw_topic_id = (state.get("topic_workspace") or {}).get("topic_id") if isinstance(state.get("topic_workspace"), dict) else None
        topic = None
        if raw_topic_id:
            try:
                topic = await self.topics.get(session, conversation_id, UUID(str(raw_topic_id)))
            except ValueError:
                topic = None
        if topic is None:
            slots = await self.topics.load_slots(session, conversation_id)
            if slots:
                topic = await self.topics.get(session, conversation_id, slots[0].topic_id)
        if topic is None:
            topic = await self.topics.create(
                session,
                conversation_id=conversation_id,
                title=self._topic_title(str(state.get("query") or ""), {}),
                current_goal=str(state.get("query") or ""),
                last_task_id=task_id,
            )
            await self.topics.replace_slots(session, conversation_id, [topic.topic_id])

        inline_limit = int(getattr(self.settings, "evidence_inline_max_bytes", 65536))
        created: list[tuple[Any, str]] = []
        query_result_tool_ids = {"workflow.phm.structured_query", "workflow.phm.result_followup"}
        for observation in [x for x in state.get("observations") or [] if isinstance(x, dict)]:
            if str(observation.get("tool_id") or "") in query_result_tool_ids:
                continue
            draft = adapt_tool_observation(observation, state, inline_max_bytes=inline_limit)
            if draft is None:
                continue
            ev = await self.evidence.create(session, conversation_id=conversation_id, source_task_id=task_id, draft=draft)
            await self.evidence.attach_topic(session, topic_id=topic.topic_id, evidence_id=ev.evidence_id, role="derived" if draft.authority in {"DERIVED", "MODEL_INFERRED", "COMPUTED"} else "supporting")
            await self.evidence.add_task_ref(session, task_id=task_id, evidence_id=ev.evidence_id, direction="OUTPUT", purpose=draft.semantic_type)
            created.append((ev, draft.semantic_type))

        for result in [x for x in state.get("answer_results") or [] if isinstance(x, dict)]:
            draft = adapt_answer_result(result, state, assistant_message_id=assistant_message_id)
            if draft is None:
                continue
            ev = await self.evidence.create(session, conversation_id=conversation_id, source_task_id=task_id, draft=draft)
            await self.evidence.attach_topic(session, topic_id=topic.topic_id, evidence_id=ev.evidence_id, role="primary")
            await self.evidence.add_task_ref(session, task_id=task_id, evidence_id=ev.evidence_id, direction="OUTPUT", purpose=draft.semantic_type)
            created.append((ev, draft.semantic_type))

        attachment_parent_ids: dict[str, UUID] = {}
        for item in [x for x in state.get("understanding_results") or [] if isinstance(x, dict)]:
            for draft in adapt_attachment_result(item):
                ev = await self.evidence.create(session, conversation_id=conversation_id, source_task_id=task_id, draft=draft)
                await self.evidence.attach_topic(session, topic_id=topic.topic_id, evidence_id=ev.evidence_id, role="supporting")
                await self.evidence.add_task_ref(session, task_id=task_id, evidence_id=ev.evidence_id, direction="OUTPUT", purpose=draft.semantic_type)
                created.append((ev, draft.semantic_type))
                attachment_id = str((draft.subject or {}).get("attachment_id") or "")
                if draft.semantic_type == "attachment_document" and attachment_id:
                    attachment_parent_ids[attachment_id] = ev.evidence_id
                elif draft.semantic_type == "analysis_result" and attachment_id and attachment_id in attachment_parent_ids:
                    await self.evidence.add_lineage(session, parent_evidence_id=attachment_parent_ids[attachment_id], child_evidence_id=ev.evidence_id, relation_type="parsed_from")

        intent = state.get("business_intent") if isinstance(state.get("business_intent"), dict) else {}
        contract = intent.get("completion_contract") if isinstance(intent.get("completion_contract"), dict) else {}
        if final_answer and str(contract.get("response_mode") or "auto") != "facts_only":
            analysis = EvidenceDraft(
                kind="analysis",
                semantic_type="analysis_result",
                authority="MODEL_INFERRED",
                subject=dict(topic.primary_subject or {}),
                scope=dict(topic.scope or {}),
                content_descriptor={"message_id": str(assistant_message_id), "response_mode": contract.get("response_mode")},
                summary={"answer_preview": final_answer[:1800]},
                completeness=EvidenceCompleteness(status="not_applicable"),
                freshness={"observed_at": datetime.now(UTC), "immutable": True, "freshness_class": "immutable"},
                storage_backend="message_reference",
                storage_ref={"message_id": str(assistant_message_id)},
                source_system="conversation_llm",
                immutable=True,
                observed_at=datetime.now(UTC),
            )
            ev = await self.evidence.create(session, conversation_id=conversation_id, source_task_id=task_id, draft=analysis)
            await self.evidence.attach_topic(session, topic_id=topic.topic_id, evidence_id=ev.evidence_id, role="derived")
            await self.evidence.add_task_ref(session, task_id=task_id, evidence_id=ev.evidence_id, direction="OUTPUT", purpose="analysis_result")
            created.append((ev, "analysis_result"))

        input_refs = await self.evidence.task_refs(session, task_id=task_id, direction="INPUT")
        input_ids = [item.evidence_id for item in input_refs]
        all_support_ids = list(input_ids) + [ev.evidence_id for ev, sem in created if sem != "analysis_result"]
        for ev, semantic in created:
            if semantic == "analysis_result":
                for parent_id in all_support_ids:
                    await self.evidence.add_lineage(session, parent_evidence_id=parent_id, child_evidence_id=ev.evidence_id, relation_type="supports")
            elif semantic == "diagnosis_result":
                for parent_id in all_support_ids:
                    if parent_id != ev.evidence_id:
                        await self.evidence.add_lineage(session, parent_evidence_id=parent_id, child_evidence_id=ev.evidence_id, relation_type="diagnosed_from")
            elif semantic in {"time_domain_features", "frequency_domain_features", "computed_metric"}:
                for parent_id in input_ids:
                    await self.evidence.add_lineage(session, parent_evidence_id=parent_id, child_evidence_id=ev.evidence_id, relation_type="derived_from")

        evidence_types = [semantic for _, semantic in created]
        await self.topics.update(
            session,
            topic,
            topic_summary=self._topic_summary({**state, "final_answer": final_answer}, evidence_types),
            primary_subject=(dict(state.get("resolved_entity") or {}) or dict(topic.primary_subject or {})),
            scope=(dict(state.get("query_scope") or {}) or dict(topic.scope or {})),
            current_goal=str(((intent.get("goal_frame") or {}).get("goal") if isinstance(intent.get("goal_frame"), dict) else "") or state.get("query") or "")[:4000],
            last_task_id=task_id,
        )
        return {
            "topic_id": str(topic.topic_id),
            "output_evidence_ids": [str(ev.evidence_id) for ev, _ in created],
            "input_evidence_ids": [str(item.evidence_id) for item in input_refs],
            "evidence_types": list(dict.fromkeys(evidence_types)),
            "idempotent_reuse": False,
        }
