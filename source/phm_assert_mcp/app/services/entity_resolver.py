from __future__ import annotations

import json
import logging
import time
from app.identity_tokens import code_tokens
from typing import Any

from app.config import Settings
from app.domain.entities import Candidate
from app.providers.embedding import EmbeddingProvider
from app.providers.reranker import RerankProvider
from app.repositories.catalog_repository import CatalogRepository
from app.schemas.common import EntityView
from app.schemas.resolve import ResolveEntityRequest, ResolveEntityResponse
from app.services.query_understanding import QueryConstraints, QueryUnderstandingService
from app.services.resolution_policy import ResolutionPolicy, ResolutionPolicyDecision, context_entity, query_fingerprint
from app.services.scoring import rank_candidates

logger = logging.getLogger(__name__)


def _active_type(active: dict[str, Any]) -> str:
    metadata = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
    merged = {**metadata, **active}
    if any(merged.get(key) for key in ("point_no", "point_id", "pointNo", "pointId")):
        return "point"
    if any(merged.get(key) for key in ("equip_no", "equip_id", "device_code", "device_id")):
        return "equipment"
    raw = str(active.get("entity_type") or active.get("type") or "").lower()
    return "space" if raw in {"area", "space", "region", "line"} else raw


def _target_type(level: str, scope: str) -> str:
    if level in {"space", "area", "line"}: return "space"
    if level in {"equipment", "point"}: return level
    if scope in {"space", "area", "line", "area_aggregate"}: return "space"
    if scope in {"point", "equipment_and_point"}: return "point"
    if scope == "equipment": return "equipment"
    return "any"


def _metadata(candidate: Candidate) -> dict[str, Any]:
    return candidate.metadata if isinstance(candidate.metadata, dict) else {}


def candidate_to_entity(candidate: Candidate) -> EntityView:
    m = _metadata(candidate)
    et = candidate.entity_type
    if et == "area":
        return EntityView(
            entity_type="space",
            space_id=str(m.get("space_id") or candidate.entity_key or "") or None,
            space_name=str(m.get("leaf_space_name") or candidate.display_name or m.get("area_name") or "") or None,
            space_path=str(m.get("space_path") or "") or None,
            space_link=str(m.get("space_link") or "") or None,
            space_type=str(m.get("leaf_space_type") or m.get("space_type") or "") or None,
            space_no=str(m.get("space_number") or m.get("space_code") or "") or None,
            metadata=m,
        )
    if et == "equipment":
        return EntityView(
            entity_type="equipment",
            space_id=str(m.get("space_id") or m.get("leaf_space_id") or "") or None,
            space_name=str(m.get("leaf_space_name") or m.get("area_name") or m.get("line_name") or "") or None,
            space_path=str(m.get("space_path") or "") or None,
            space_link=str(m.get("space_link") or "") or None,
            equip_id=str(m.get("equip_id") or candidate.entity_key or "") or None,
            equip_no=candidate.equip_no or str(m.get("equip_no") or "") or None,
            equip_name=str(m.get("equip_name") or candidate.display_name or "") or None,
            metadata=m,
        )
    return EntityView(
        entity_type="point",
        space_id=str(m.get("space_id") or m.get("leaf_space_id") or "") or None,
        space_name=str(m.get("leaf_space_name") or m.get("area_name") or m.get("line_name") or "") or None,
        space_path=str(m.get("space_path") or "") or None,
        space_link=str(m.get("space_link") or "") or None,
        equip_id=str(m.get("equip_id") or "") or None,
        equip_no=candidate.equip_no or str(m.get("equip_no") or "") or None,
        equip_name=str(m.get("equip_name") or "") or None,
        point_id=str(m.get("point_id") or candidate.entity_key or "") or None,
        point_no=candidate.point_no or str(m.get("point_no") or "") or None,
        point_name=str(m.get("point_name") or candidate.display_name or "") or None,
        metadata=m,
    )


def match_view(candidate: Candidate) -> dict[str, Any]:
    entity = candidate_to_entity(candidate).model_dump()
    score = candidate.final_score or candidate.text_score or candidate.field_score
    return {
        **entity,
        "entity_key": candidate.entity_key,
        "legacy_entity_type": candidate.entity_type,
        "similarity": round(score, 6),
        "score": round(score, 6),
        "source": candidate.source or candidate.extra.get("recall_source") or None,
        "text_score": round(candidate.text_score, 6),
        "vector_score": round(candidate.vector_score, 6),
        "field_score": round(candidate.field_score, 6),
        "field_coverage": round(candidate.field_coverage, 6),
        "profile_adjustment": round(candidate.profile_adjustment, 6),
        "critical_mismatch": candidate.critical_mismatch,
    }


class EntityResolver:
    def __init__(self, settings: Settings, catalog: CatalogRepository, understanding: QueryUnderstandingService, embedding: EmbeddingProvider, reranker: RerankProvider) -> None:
        self.settings = settings
        self.catalog = catalog
        self.understanding = understanding
        self.embedding = embedding
        self.reranker = reranker
        self.policy = ResolutionPolicy()

    async def resolve(self, request: ResolveEntityRequest, request_id: str = "") -> ResolveEntityResponse:
        started = time.perf_counter()
        db_elapsed = embedding_elapsed = rerank_elapsed = 0.0
        hints = request.semantic_hints
        if hasattr(hints,"model_dump"): hints=hints.model_dump()
        tokens = [] if isinstance(hints,dict) and hints.get("needs_asset_lookup") is False else code_tokens(request.query)
        if tokens and request.required_entity_level not in {"space", "area", "line"} and hasattr(self.catalog, "lookup_code_tokens"):
            found = await self.catalog.lookup_code_tokens(tokens)
            wanted = {x.upper() for x in tokens}
            target = request.required_entity_level
            candidates = [c for c in found if wanted.issubset({c.equip_no.upper(), c.point_no.upper()})
                          and (target not in {"equipment", "point"} or c.entity_type == target)]
            area = request.semantic_hints.area if request.semantic_hints else None
            if area and area.raw_text and area.raw_text in request.query:
                term = (area.retrieval_text or area.raw_text).casefold()
                candidates = [c for c in candidates if term in "/".join(str(c.metadata.get(k) or "")
                    for k in ("space_path", "leaf_space_name", "area_name", "line_name")).casefold()]
            if target == "any" and any(c.entity_type == "point" for c in candidates):
                candidates = [c for c in candidates if c.entity_type == "point"]
            q = QueryConstraints(raw_query=request.query, retrieval_query=" ".join(tokens),
                lookup_scope=target, required_entity_level=target, extraction_source="literal_code_constraints",
                raw_spans={"literal_codes": " ".join(tokens)})
            decision = ResolutionPolicyDecision(action="SEARCH", need_lookup=True, should_refresh=request.force_refresh,
                target_entity_level=target, lookup_scope=target, return_mode="single", context_used=False,
                reason="当前明确编码执行精确核验。", query_fingerprint=query_fingerprint(request.query, target))
            response = (self._resolve_exact(candidates, q, decision, "code_exact") if candidates and len(found) < 1001
                        else self._not_found(q, decision, "未找到与输入编码全部一致的资产记录，请核对编码或补充所属设备。"))
            self._log(request_id, request, q, response, started, (time.perf_counter()-started)*1000, 0, 0)
            return response
        # Literal identity input can be checked directly without a second language model.
        # No token extraction/aliases are invented here: the entire input must match.
        if (not request.semantic_hints and not request.active_entity and not request.previous_resolution
                and not request.conversation_context and request.required_entity_level in {"equipment", "point", "space", "area", "line"}):
            scope = "space" if request.required_entity_level in {"space", "area", "line"} else request.required_entity_level
            literal = request.query.strip()
            direct = await self.catalog.exact_code(
                equip_no=literal if scope == "equipment" else "",
                point_no=literal if scope == "point" else "", scope=scope)
            source = "code_exact"
            if not direct:
                direct = await self.catalog.official_exact_name(term=literal, scope=scope,
                            limit=max(2, self.settings.resolve_max_candidates))
                source = "official_exact_name"
            if direct:
                q = QueryConstraints(raw_query=request.query, retrieval_query=literal, lookup_scope=scope,
                                     required_entity_level=request.required_entity_level,
                                     extraction_source="literal_catalog_identity")
                decision = self.policy.decide(request, q)
                response = self._resolve_exact(direct, q, decision, source)
                self._log(request_id, request, q, response, started,
                          (time.perf_counter() - started) * 1000, 0, 0)
                return response

        q = await self.understanding.understand(
            request.query,
            request.required_entity_level,
            request.user_profile,
            conversation_context=request.conversation_context,
            active_entity=request.active_entity,
            semantic_hints=request.semantic_hints,
        )
        policy = self.policy.decide(request, q)
        target = policy.target_entity_level
        reference_level = (
            q.context_reference_level
            if q.context_reference_level in {"space", "equipment", "point"}
            else target if q.context_reference else "none"
        )
        active = context_entity(request, reference_level=reference_level)
        active_type = _active_type(active)

        if policy.action == "SKIP":
            response = self._no_lookup(q, policy)
            self._log(request_id, request, q, response, started, 0, 0, 0)
            return response

        if policy.action == "REUSE":
            previous = request.previous_resolution or {}
            reuse_entity = active or previous.get("resolved_entity") or previous.get("entity") or {}
            if isinstance(reuse_entity, dict) and reuse_entity:
                response = self._active_response(reuse_entity, q, policy)
                self._log(request_id, request, q, response, started, 0, 0, 0)
                return response

        # Context can also constrain a deeper lookup, e.g. active equipment -> "这个设备的驱动端测点".
        active_space_link = ""
        if active and policy.action == "DOWN_DRILL":
            active_space_link = str(active.get("space_link") or (active.get("metadata") or {}).get("space_link") or "").strip("/")
            if active_space_link:
                active_space_link += "/"
            if target == "point" and not q.equip_no and active_type in {"equipment", "point"}:
                q.equip_no = str(active.get("equip_no") or (active.get("metadata") or {}).get("equip_no") or "")
            if target == "equipment" and active_type == "space" and not q.area_keyword:
                q.area_keyword = str(active.get("space_name") or active.get("area_name") or active.get("leaf_space_name") or "")

        # Explicit codes are database identities, not natural-language matching.
        # Multiple rows are still never selected by score.
        t0 = time.perf_counter()
        exact = await self.catalog.exact_code(equip_no=q.equip_no, point_no=q.point_no, scope=q.lookup_scope, active_space_link=active_space_link, area_keywords=q.area_keywords)
        db_elapsed += (time.perf_counter() - t0) * 1000
        if exact:
            response = self._resolve_exact(exact, q, policy, "code_exact")
            self._log(request_id, request, q, response, started, db_elapsed, 0, 0)
            return response

        if q.point_no or q.equip_no:
            return self._not_found(q, policy, "未找到与明确编码一致的资产记录，未使用相似名称替换查询对象。")

        # Exact official catalog identity is authoritative and must not depend on
        # semantic-enrichment review state.  This prevents a company/area that was not
        # part of the offline tagging set (for example a company root) from falling
        # through to vector search even when its official display_name is an exact hit.
        # Multiple physical rows with the same official name still require selection.
        exact_term = q.equipment_keyword if target == "equipment" else q.point_keyword if target == "point" else q.area_keyword if target in {"space", "area", "line"} else ""
        has_unverified_qualifiers = target == "point" and any((
            q.equipment_keyword, q.equipment_type_keyword, q.component_keyword,
            q.position_keyword, q.direction_keyword, q.measurement_keyword, q.equip_no,
        ))
        if exact_term and not has_unverified_qualifiers:
            t0 = time.perf_counter()
            official_exact = await self.catalog.official_exact_name(
                term=exact_term, scope=q.lookup_scope, active_space_link=active_space_link,
                area_keywords=([] if target in {"space", "area", "line"} else q.area_keywords),
                limit=max(2, self.settings.resolve_max_candidates),
            )
            db_elapsed += (time.perf_counter() - t0) * 1000
            if official_exact:
                response = self._resolve_exact(official_exact, q, policy, "official_exact_name")
                self._log(request_id, request, q, response, started, db_elapsed, 0, 0)
                return response

            # Reviewed normalized names/aliases are the second lexical identity layer.
            # They intentionally require semantic_review_status, unlike display_name.
            t0 = time.perf_counter()
            semantic_exact = await self.catalog.semantic_exact_name(
                term=exact_term, scope=q.lookup_scope, active_space_link=active_space_link,
                area_keywords=([] if target in {"space", "area", "line"} else q.area_keywords),
                limit=max(2, self.settings.resolve_max_candidates),
            )
            db_elapsed += (time.perf_counter() - t0) * 1000
            if semantic_exact:
                response = self._resolve_exact(semantic_exact, q, policy, "semantic_exact_name_or_alias")
                self._log(request_id, request, q, response, started, db_elapsed, 0, 0)
                return response

        # Otherwise retain the established natural-language recall path: embedding -> pgvector.
        t0 = time.perf_counter()
        vector = await self.embedding.embed(q.retrieval_query)
        embedding_elapsed = (time.perf_counter() - t0) * 1000
        literal = "[" + ",".join(format(value, ".10g") for value in vector) + "]"
        t0 = time.perf_counter()
        candidates = await self.catalog.vector_search(
            vector_literal=literal,
            scope=q.lookup_scope,
            equip_no=q.equip_no,
            point_no=q.point_no,
            active_space_link=active_space_link,
            area_keywords=q.area_keywords,
            limit=max(2, self.settings.resolve_max_candidates),
        )
        db_elapsed += (time.perf_counter() - t0) * 1000
        if not candidates and q.area_keywords:
            # Metadata can be incomplete on legacy catalog rows.  Do not re-run LLM,
            # embedding or reranking; perform one DB-only fallback without the area
            # prefilter and let the structured area be enforced by the reranker.
            t0 = time.perf_counter()
            candidates = await self.catalog.vector_search(
                vector_literal=literal,
                scope=q.lookup_scope,
                equip_no=q.equip_no,
                point_no=q.point_no,
                active_space_link=active_space_link,
                area_keywords=[],
                limit=max(2, self.settings.resolve_max_candidates),
            )
            db_elapsed += (time.perf_counter() - t0) * 1000
        if not candidates:
            response = self._not_found(q, policy, "向量检索未召回真实资产候选。")
            self._log(request_id, request, q, response, started, db_elapsed, embedding_elapsed, 0)
            return response

        structured = json.dumps({k: v for k, v in q.as_dict().items() if k.endswith("keyword") or k.endswith("_no") or k == "lookup_scope"}, ensure_ascii=False)
        profile_context = json.dumps({"areas": q.profile_areas, "equip_nos": q.profile_equip_nos, "equip_names": q.profile_equip_names}, ensure_ascii=False)
        t0 = time.perf_counter()
        rerank_scores = await self.reranker.rerank(request.query, candidates, structured, profile_context, q.has_explicit_area)
        rerank_elapsed = (time.perf_counter() - t0) * 1000
        ranked = rank_candidates(candidates, rerank_scores, q.profile_equip_nos)
        response = self._decide(ranked, q, policy, request.limit)
        self._log(request_id, request, q, response, started, db_elapsed, embedding_elapsed, rerank_elapsed)
        return response

    @staticmethod
    def _profile_exact_candidates(
        candidates: list[Candidate], q: QueryConstraints
    ) -> list[Candidate]:
        """Return exact-name candidates owned by the current user's profile.

        Only equipment codes are authoritative enough to collapse an exact-name
        ambiguity.  Profile names are intentionally not used here because the same
        display name can identify many physical assets.  Point candidates inherit
        their parent ``equip_no``, so the same rule safely narrows repeated point names.
        """

        profile_equip_nos = {
            str(value).strip().upper()
            for value in q.profile_equip_nos
            if str(value).strip()
        }
        if not profile_equip_nos:
            return []
        return [
            candidate
            for candidate in candidates
            if str(
                candidate.equip_no
                or _metadata(candidate).get("equip_no")
                or ""
            ).strip().upper()
            in profile_equip_nos
        ]

    def _resolve_exact(self, candidates: list[Candidate], q: QueryConstraints, policy: ResolutionPolicyDecision, source: str) -> ResolveEntityResponse:
        for c in candidates:
            c.final_score = 1.0
            c.vector_score = 1.0
            c.source = source

        profile_matches = (
            self._profile_exact_candidates(candidates, q)
            if len(candidates) > 1
            else []
        )
        if profile_matches:
            profile_ids = {id(candidate) for candidate in profile_matches}
            for candidate in profile_matches:
                candidate.profile_adjustment = max(candidate.profile_adjustment, 0.05)
                candidate.source = "code_exact_profile"
            candidates = sorted(
                candidates,
                key=lambda candidate: (0 if id(candidate) in profile_ids else 1),
            )

        if len(candidates) == 1:
            return self._response("RESOLVED", candidates[0], [], q, policy, "UNIQUE", f"已通过{source}定位唯一实体。", source)
        if policy.return_mode == "collection":
            return self._response("RESOLVED_COLLECTION", None, candidates, q, policy, "COLLECTION", "已按集合模式返回全部精确匹配实体。", source)
        if policy.return_mode == "single" and len(profile_matches) == 1:
            return self._response(
                "RESOLVED",
                profile_matches[0],
                [],
                q,
                policy,
                "UNIQUE",
                "设备编码条件命中多个目录行，已根据当前用户画像中的负责设备编码定位唯一实体。",
                "code_exact_profile",
            )
        if profile_matches:
            return self._response(
                "NEEDS_DISAMBIGUATION",
                None,
                candidates,
                q,
                policy,
                "MULTIPLE",
                "设备编码条件命中多个目录行，用户画像内仍有多个候选，请用户选择。",
                "code_exact_profile",
            )
        return self._response("NEEDS_DISAMBIGUATION", None, candidates, q, policy, "MULTIPLE", "精确条件命中多个实体，需要消歧。", source)

    def _decide(self, ranked: list[Candidate], q: QueryConstraints, policy: ResolutionPolicyDecision, limit: int) -> ResolveEntityResponse:
        """Apply the strict 0.4.x ambiguity contract.

        Similarity gaps never authorize an automatic choice.  A result is resolved
        only when relevance filtering leaves one real row, or exactly one real row is
        owned by an authoritative profile equipment number.
        """

        viable = [
            candidate
            for candidate in ranked
            if candidate.final_score >= self.settings.resolve_candidate_min_score
        ]
        if not viable:
            return self._not_found(
                q,
                policy,
                "Embedding 与 Reranker 未形成可信的真实资产候选。",
            )

        if policy.return_mode in {"collection", "list"}:
            return self._response(
                "RESOLVED_COLLECTION",
                None,
                viable,
                q,
                policy,
                "COLLECTION",
                f"已按用户要求返回 {len(viable)} 个真实资产结果。",
                "embedding_rerank_collection",
            )

        profile_matches = self._profile_exact_candidates(viable, q)
        if len(profile_matches) == 1:
            profile_matches[0].source = "embedding_rerank_profile"
            return self._response(
                "RESOLVED",
                profile_matches[0],
                [],
                q,
                policy,
                "UNIQUE",
                "多个语义候选中，用户画像的负责设备编码唯一命中。",
                "embedding_rerank_profile",
            )

        if len(viable) == 1:
            return self._response(
                "RESOLVED",
                viable[0],
                [],
                q,
                policy,
                "UNIQUE",
                "Embedding 召回并经 Reranker 过滤后仅剩一个真实候选。",
                "embedding_rerank_unique",
            )

        if profile_matches:
            profile_ids = {id(candidate) for candidate in profile_matches}
            viable.sort(key=lambda candidate: 0 if id(candidate) in profile_ids else 1)
        return self._response(
            "NEEDS_DISAMBIGUATION",
            None,
            viable,
            q,
            policy,
            "MULTIPLE",
            "Embedding 与 Reranker 返回多个真实候选，请用户选择目标实体。",
            "embedding_rerank_candidates",
        )

    def _no_lookup(self, q: QueryConstraints, policy: ResolutionPolicyDecision) -> ResolveEntityResponse:
        payload = self._legacy_fields(q, "NO_LOOKUP", [], "no_lookup")
        payload["need_lookup"] = False
        return ResolveEntityResponse(
            success=True, status="NO_LOOKUP", entity=None, confidence=0.0, needs_disambiguation=False,
            matches=[], candidate_count=0, message="当前问题不需要绑定企业资产实体。",
            decision=policy.as_dict(), return_mode=policy.return_mode,
            query_fingerprint=policy.query_fingerprint, **payload
        )

    def _not_found(self, q: QueryConstraints, policy: ResolutionPolicyDecision, message: str) -> ResolveEntityResponse:
        return self._response("ENTITY_NOT_FOUND", None, [], q, policy, "NOT_FOUND", message, "not_found")

    def _active_response(self, active: dict[str, Any], q: QueryConstraints, policy: ResolutionPolicyDecision) -> ResolveEntityResponse:
        entity_type = _active_type(active)
        target_type = policy.target_entity_level
        if target_type == "space":
            entity_type = "space"
        elif target_type == "equipment" and entity_type == "point":
            entity_type = "equipment"
        view = EntityView(
            entity_type=entity_type or "space",
            space_id=active.get("space_id"), space_name=active.get("space_name") or active.get("area_name"),
            space_path=active.get("space_path"), space_link=active.get("space_link"), space_type=active.get("space_type"), space_no=active.get("space_no") or active.get("space_number"),
            equip_id=active.get("equip_id") if entity_type != "space" else None,
            equip_no=active.get("equip_no") if entity_type != "space" else None,
            equip_name=active.get("equip_name") if entity_type != "space" else None,
            point_id=active.get("point_id") if entity_type == "point" else None,
            point_no=active.get("point_no") if entity_type == "point" else None,
            point_name=active.get("point_name") if entity_type == "point" else None,
            metadata=active.get("metadata") or {},
        )
        payload = self._legacy_fields(q, "UNIQUE", [view.model_dump() | {"similarity": 1.0, "source": "active_context"}], "active_context")
        return ResolveEntityResponse(
            success=True,status="RESOLVED",entity=view,confidence=1.0,
            needs_disambiguation=False,matches=[],candidate_count=1,
            message="已复用当前会话活动实体。",decision=policy.as_dict(),
            return_mode=policy.return_mode,resolved_entities=[view.model_dump()],
            query_fingerprint=policy.query_fingerprint,**payload
        )

    def _response(self, status: str, entity_candidate: Candidate | None, matches: list[Candidate], q: QueryConstraints, policy: ResolutionPolicyDecision, legacy_status: str, message: str, source: str) -> ResolveEntityResponse:
        entity = candidate_to_entity(entity_candidate) if entity_candidate else None
        match_items = [match_view(c) for c in ([entity_candidate] if entity_candidate else matches) if c]
        user_matches = [] if entity_candidate else match_items
        confidence = match_items[0]["similarity"] if match_items else 0.0
        payload = self._legacy_fields(q, legacy_status, match_items, source)
        actual_return_mode = (
            "candidates"
            if status == "NEEDS_DISAMBIGUATION"
            else "collection"
            if status == "RESOLVED_COLLECTION"
            else policy.return_mode
        )
        return ResolveEntityResponse(
            success=status not in {"DATABASE_ERROR","INTERNAL_ERROR"}, status=status, entity=entity, confidence=confidence,
            needs_disambiguation=status=="NEEDS_DISAMBIGUATION", matches=user_matches,
            candidate_count=len(match_items), message=message, decision=policy.as_dict(),
            return_mode=actual_return_mode, resolved_entities=match_items if status == "RESOLVED_COLLECTION" else ([entity.model_dump()] if entity else []),
            query_fingerprint=policy.query_fingerprint, **payload,
        )

    def _legacy_fields(self, q: QueryConstraints, legacy_status: str, matches: list[dict[str, Any]], source: str) -> dict[str, Any]:
        profile_used=any(float(x.get("profile_adjustment") or 0)>0 for x in matches)
        query_scope={"scope_type":"ENTITY","target_entity_type":_target_type("any",q.lookup_scope),"include_descendants":q.lookup_scope=="area_aggregate"}
        if legacy_status=="UNIQUE" and matches and matches[0].get("entity_type")=="space":
            query_scope["resolved_area"]={k:matches[0].get(k) for k in ("space_id","space_name","space_link","space_path","space_type","space_no") if matches[0].get(k)}
        return {
            "legacy_status":legacy_status,"need_lookup":True,"need_disambiguation":legacy_status=="MULTIPLE" and len(matches)>1,
            "match_count":len(matches),"top_similarity":round(float(matches[0].get("similarity") or 0),6) if matches else 0.0,
            "matches_json":json.dumps(matches,ensure_ascii=False),"lookup_scope":q.lookup_scope,"resolution_source":source,"profile_prior_used":profile_used,
            "query_scope":query_scope,"entity_constraints":q.as_dict(),
        }

    def _log(self, request_id: str, request: ResolveEntityRequest, q: QueryConstraints, response: ResolveEntityResponse, started: float, db_ms: float, embedding_ms: float, rerank_ms: float) -> None:
        logger.info("asset_tool_completed",extra={"fields":{
            "request_id":request_id,"tool_name":"resolve_entity","query":request.query,"required_entity_level":request.required_entity_level,
            "candidate_count":response.candidate_count,"selected_entity":response.entity.model_dump() if response.entity else None,"confidence":response.confidence,
            "database_elapsed_ms":round(db_ms,2),"embedding_elapsed_ms":round(embedding_ms,2),"llm_elapsed_ms":round(q.llm_elapsed_ms,2),
            "rerank_elapsed_ms":round(rerank_ms,2),"total_elapsed_ms":round((time.perf_counter()-started)*1000,2),"status":response.status,"lookup_scope":q.lookup_scope,
        }})
