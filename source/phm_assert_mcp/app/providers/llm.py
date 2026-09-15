from __future__ import annotations

import json
from typing import Any

import httpx

from app.providers.cache import AsyncMemo
from app.config import Settings
from app.errors import AssetError, ErrorCode
from app.providers.embedding import _endpoint


SYSTEM_PROMPT = """
你是工业 PHM 资产查询参数提取器。你必须理解用户整句话和给定的短期会话上下文，完成语义参数提取；系统不会再使用关键词表、正则表达式或业务别名规则补充你的结果。

你的职责仅限于提取语言条件，不得生成数据库事实，不得猜测不存在于用户原话中的设备编号、测点编号、空间 ID 或层级 ID。数据库向量检索和重排模型负责返回真实候选。

只返回一个 JSON 对象，字段必须完整：
- needs_asset_lookup: boolean，本轮是否需要绑定真实资产实体。
- lookup_scope: any、space、equipment、point、equipment_and_point、area_aggregate 之一。
- return_mode: none、single、candidates、collection、hierarchy、list 之一。
- diagnostic_mode: boolean。
- collection_requested: boolean，仅表示“当前检索目标本身需要返回多个匹配实体”。
- descendant_collection_requested: boolean，用户是否要求在一个已确认区域下面展开某个层级/类型的全部实体。
- descendant_target_level: none、space、equipment、point 之一；只有 descendant_collection_requested=true 时才非 none。
- descendant_target_type: {"raw_text":"","retrieval_text":""}。空间集合时 raw_text 必须逐字来自用户原话，retrieval_text 可按资产目录常用空间类型语义规范化（例如“车间”→workshop、“产线”→line）；不得猜数据库 ID。
- descendant_recursive: boolean，是否允许跨多级空间向下查找；根据整句语义判断，不使用关键词规则。
- refresh_requested: boolean，只有用户明确要求更换、重新匹配或忽略历史实体时为 true。
- context_reference: boolean，当前问题是否引用历史实体。
- context_reference_level: none、space、equipment、point 之一。
- equipment、equipment_type、area、point、component、position、direction、measurement、equip_no、point_no：每项都是 {"raw_text":"","retrieval_text":""}。
- retrieval_query: 用于向量检索的简洁完整资产描述。
- confidence: 0 到 1。

提取要求：
1. raw_text 必须逐字来自当前用户问题；没有明确表达时留空。
2. retrieval_text 可以做开放语义规范化，但不能补造身份事实或编码。
3. 设备名称、区域路径、部件、位置、方向和测量量必须分开表达；设备前后的操作词不能进入参数。
4. 带专名、序号或工艺编号的表达属于具体设备，例如“R2平辊设备”应提取 equipment.raw_text="R2平辊设备"、equipment.retrieval_text="R2平辊"。
5. 多级区域可以在 area.retrieval_text 中用“/”表达真实语义层级，例如“热卷板事业部热卷精轧车间粗轧区”可写为“热卷板事业部/热卷精轧车间/粗轧区”；这只是检索语义，不是数据库事实。
6. “这个设备/该测点/上述区域”等只声明 context_reference 和层级，不得猜具体实体。
7. required_entity_level 是下游能力要求：equipment 必须面向设备召回，point 必须面向测点召回，space/area/line 必须面向空间召回。
8. retrieval_query 应组合用户明确表达的区域、设备、设备类型、测点、部件、位置、方向和测量量；不要包含“请、查询、诊断、分析”等无关操作词。
9. “某区域下面的全部设备/各车间/各产线”等问题，根区域仍按 single 解析；不要把根区域误设为 collection。此时 descendant_collection_requested=true，并设置 descendant_target_level/descendant_target_type。
10. 不输出思维链，不返回候选 ID，不生成 SQL。
""".strip()


class LlmProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.llm_timeout,
            limits=httpx.Limits(max_connections=settings.model_http_max_connections,
                                max_keepalive_connections=settings.model_http_max_connections),
        )

        self.cache = AsyncMemo(maxsize=settings.model_cache_max_entries,
                               ttl=settings.understanding_cache_ttl_seconds)

    async def extract(self, **kwargs) -> dict[str, Any]:
        # Include the entire context, never key pronouns by query text alone.
        return await self.cache.get(kwargs, lambda: self._extract_uncached(**kwargs))

    @property
    def enabled(self) -> bool:
        return self.settings.llm_configured

    async def _extract_uncached(
        self,
        *,
        query: str,
        required_entity_level: str,
        conversation_context: dict[str, Any] | None = None,
        active_entity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产参数提取模型未配置，无法解析本次资产条件。",
                "Asset LLM is required but LLM_ENABLED/LLM_BASE_URL/LLM_MODEL is incomplete",
            )

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        request_context = {
            "attachment_texts": list((conversation_context or {}).get("attachment_texts") or [])[:6],
            "query": query,
            "required_entity_level": required_entity_level,
            "active_entity": active_entity or {},
            "recent_messages": list((conversation_context or {}).get("recent_messages") or [])[-6:],
            "recent_entities": list((conversation_context or {}).get("recent_entities") or [])[-12:],
            "conversation_summary": str((conversation_context or {}).get("summary") or "")[:3000],
            "pending_clarification": (conversation_context or {}).get("pending_clarification") or {},
        }
        body = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request_context, ensure_ascii=False, default=str)[:20000],
                },
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self.client.post(
                _endpoint(self.settings.llm_base_url, "/chat/completions"),
                headers=headers,
                json=body,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except httpx.HTTPStatusError as exc:
            response_text = exc.response.text.strip().replace("\n", " ")[:1500]
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产参数提取模型调用失败，请检查模型配置。",
                (
                    "asset llm extraction failed: "
                    f"HTTP {exc.response.status_code}; model={self.settings.llm_model!r}; "
                    f"url={exc.request.url}; response={response_text or '<empty>'}"
                ),
            ) from exc
        except Exception as exc:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产参数提取模型暂时不可用，请稍后重试。",
                f"asset llm extraction failed: {type(exc).__name__}: {exc}",
            ) from exc
        if not isinstance(parsed, dict):
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "资产参数提取模型返回格式不正确，请稍后重试。",
                "asset llm extraction response is not a JSON object",
            )
        return parsed


    async def classify_equipment_types(
        self, *, target_type: str, type_labels: list[str]
    ) -> dict[str, str]:
        """Conservatively classify distinct authoritative equipment-type labels.

        The model is never asked to invent assets.  It only labels values already
        returned by PostgreSQL, which avoids substring errors such as counting a
        "水泵房风机" as a pump merely because the equipment name contains 水泵.
        """
        labels = list(dict.fromkeys(str(x).strip() for x in type_labels if str(x).strip()))
        if not labels:
            return {}
        normalized_target = "".join(str(target_type).casefold().split())
        exact = {label: "MATCH" for label in labels if "".join(label.casefold().split()) == normalized_target}
        remaining = [label for label in labels if label not in exact]
        if not remaining:
            return exact
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        system = """
你是工业资产类别核验器。目标类别和候选值都来自真实数据库。
逐项判断候选 equipment_type 是否可以确定属于目标类别。只基于类别语义，禁止利用设备名称猜测，禁止补造事实。
MATCH=可以确定属于；NO_MATCH=可以确定不属于；UNCERTAIN=仅凭类别标签无法安全确定。
例如更宽/更窄类别、介质或用途不明确时宁可 UNCERTAIN，不要为了提高召回率强行 MATCH。
只返回 JSON：{"items":[{"label":"必须逐字复制输入候选","status":"MATCH|NO_MATCH|UNCERTAIN"}]}。
""".strip()
        body = {
            "model": self.settings.llm_model, "temperature": 0,
            "messages": [
                {"role":"system","content":system},
                {"role":"user","content":json.dumps({"target_type":target_type,"candidate_type_labels":remaining}, ensure_ascii=False)},
            ],
            "response_format": {"type":"json_object"},
        }
        try:
            response = await self.client.post(_endpoint(self.settings.llm_base_url, "/chat/completions"), headers=headers, json=body, timeout=max(self.settings.llm_timeout, 60.0))
            response.raise_for_status()
            parsed = json.loads(response.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            raise AssetError(ErrorCode.UPSTREAM_ERROR, "设备类别语义核验暂时不可用，请稍后重试。", f"equipment type classification failed: {type(exc).__name__}: {exc}") from exc
        allowed = set(remaining)
        result = dict(exact)
        for item in parsed.get("items", []) if isinstance(parsed, dict) else []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "")
            status = str(item.get("status") or "").upper()
            if label in allowed and status in {"MATCH","NO_MATCH","UNCERTAIN"}:
                result[label] = status
        for label in remaining:
            result.setdefault(label, "UNCERTAIN")
        return result


    async def resolve_equipment_category_fallback(
        self, *, input_term: str, candidates: list[dict[str, Any]], candidate_total: int
    ) -> dict[str, Any]:
        """Infer a temporary hierarchy over real reviewed equipment-class tags.

        The asset database does not provide authoritative parent/child relations.
        The model may infer SAME/CHILD/PARENT/RELATED only for this request, and
        may select only tag_code values that are present in ``candidates``.
        """
        term = str(input_term or "").strip()
        if not term or not candidates or not self.enabled:
            return {
                "input_term": term,
                "selected_tag_codes": [],
                "items": [],
                "coverage_complete": False,
                "source": "llm_unavailable" if not self.enabled else "no_candidates",
            }
        clean_candidates = []
        seen = set()
        for item in candidates:
            code = str(item.get("tag_code") or "").strip()
            name = str(item.get("tag_name") or "").strip()
            if not code or not name or code in seen:
                continue
            seen.add(code)
            clean_candidates.append({
                "tag_code": code,
                "tag_name": name,
                "lexical_score": float(item.get("lexical_score") or 0.0),
            })
        if not clean_candidates:
            return {"input_term": term, "selected_tag_codes": [], "items": [], "coverage_complete": False, "source": "no_candidates"}

        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        system = """
你是工业资产分类纠偏器。数据库没有可靠的父子分类关系；candidate_tags 全部是真实 PostgreSQL 资产目录中已审核的设备分类标签。

你的任务只在本轮临时判断 input_term 与每个候选分类之间的语义层级，并选择可用于替代原分类词的真实标签。禁止创造任何新标签、编码、设备或数据库事实。

relation 只能取：
- SAME：与用户分类表达语义相同，可能只是简称、缺字或现场叫法；
- CHILD：候选是用户所指较宽类别下的真实子类；
- PARENT：候选比用户表达更宽；
- RELATED：有业务关系但不是“是一种”的分类继承关系，例如轧机主电机与轧机；
- NO_MATCH：无关。

选择规则：
1. selected=true 只允许 SAME 或 CHILD；PARENT/RELATED/NO_MATCH 一律不得选择。
2. 用户表达较宽且数据库没有同名父标签时，可以选择多个 CHILD，以真实标签并集表达本轮用户类别。
3. 用户表达较窄时，不得为了得到结果选择更宽的 PARENT。
4. “某设备的电机/部件/工位/区域”等 RELATED 不能当成该设备类别的子类。
5. coverage_complete 表示：在本次提供的真实候选列表中，所选标签是否足以覆盖 input_term 的语义范围。不能确定就 false。
6. candidate_total 大于 candidate_tags 数量时，coverage_complete 必须为 false，因为你没有看到完整标签词典。
7. items 只需返回与 input_term 有潜在关联的候选；明显 NO_MATCH 的候选可以省略，避免无意义的大结果。
8. reason 只需给简短结论，不输出思维链。

只返回 JSON：
{
  "items":[{"tag_code":"逐字复制候选code","relation":"SAME|CHILD|PARENT|RELATED|NO_MATCH","selected":true,"confidence":0到1}],
  "coverage_complete":true,
  "reason":"简短说明"
}
""".strip()
        body = {
            "model": self.settings.llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps({
                    "input_term": term,
                    "candidate_total": int(candidate_total),
                    "candidate_tags": clean_candidates,
                }, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            response = await self.client.post(
                _endpoint(self.settings.llm_base_url, "/chat/completions"),
                headers=headers,
                json=body,
                timeout=max(self.settings.llm_timeout, 60.0),
            )
            response.raise_for_status()
            parsed = json.loads(response.json()["choices"][0]["message"]["content"])
        except Exception as exc:
            raise AssetError(
                ErrorCode.UPSTREAM_ERROR,
                "设备分类语义纠偏暂时不可用，请稍后重试。",
                f"equipment category fallback failed: {type(exc).__name__}: {exc}",
            ) from exc

        allowed = {item["tag_code"]: item for item in clean_candidates}
        result_items: list[dict[str, Any]] = []
        selected: list[str] = []
        seen_output = set()
        for item in parsed.get("items", []) if isinstance(parsed, dict) else []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("tag_code") or "").strip()
            relation = str(item.get("relation") or "").upper().strip()
            if code not in allowed or code in seen_output or relation not in {"SAME", "CHILD", "PARENT", "RELATED", "NO_MATCH"}:
                continue
            seen_output.add(code)
            try:
                confidence = max(0.0, min(1.0, float(item.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            is_selected = bool(item.get("selected")) and relation in {"SAME", "CHILD"}
            result_items.append({
                **allowed[code],
                "relation": relation,
                "selected": is_selected,
                "confidence": confidence,
            })
            if is_selected:
                selected.append(code)
        selected = list(dict.fromkeys(selected))
        coverage = bool(parsed.get("coverage_complete")) if isinstance(parsed, dict) else False
        if int(candidate_total) > len(clean_candidates):
            coverage = False
        return {
            "input_term": term,
            "selected_tag_codes": selected,
            "items": result_items,
            "coverage_complete": coverage,
            "reason": str(parsed.get("reason") or "")[:500] if isinstance(parsed, dict) else "",
            "source": "llm_temporary_hierarchy",
        }

    async def resolve_semantic_tags(
        self, *, terms: list[str], candidates: list[dict[str, str]], entity_type: str
    ) -> dict[str, dict[str, Any]]:
        """Map user semantic terms only to already-reviewed database tag codes.

        The candidate dictionary is authoritative. Any code not present in candidates
        is discarded, so this model call can never create an asset fact or new taxonomy.
        """
        clean_terms = list(dict.fromkeys(str(x).strip() for x in terms if str(x).strip()))
        if not clean_terms:
            return {}
        normalized = lambda x: "".join(str(x or '').casefold().split())
        by_name: dict[str, list[dict[str, str]]] = {}
        by_code: dict[str, dict[str, str]] = {}
        for candidate in candidates:
            code = str(candidate.get('tag_code') or '').strip()
            name = str(candidate.get('tag_name') or '').strip()
            if not code or not name:
                continue
            by_code[code] = {'tag_code': code, 'tag_name': name}
            by_name.setdefault(normalized(name), []).append({'tag_code': code, 'tag_name': name})
        result: dict[str, dict[str, Any]] = {}
        remaining: list[str] = []
        for term in clean_terms:
            direct_code = by_code.get(term)
            exact_names = by_name.get(normalized(term), [])
            if direct_code:
                result[term] = {**direct_code, 'source': 'exact_code', 'confidence': 1.0}
            elif len(exact_names) == 1:
                result[term] = {**exact_names[0], 'source': 'exact_name', 'confidence': 1.0}
            else:
                remaining.append(term)
        if not remaining:
            return result
        if not self.enabled:
            for term in remaining:
                result[term] = {'unresolved': True, 'source': 'no_llm'}
            return result
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        system = """
你是工业资产“检索词到既有标签”的受限映射器，不负责给设备分类。
候选 tag_code/tag_name 全部来自已经人工/离线复核并写入 PostgreSQL 的资产语义字段。
对每个 input_term：只能选择候选列表中一个最贴切的 tag_code；无法确定就 UNRESOLVED。
禁止生成候选列表不存在的 tag_code，禁止根据设备名称推断设备事实，禁止扩大查询口径。
例如用户说“水泵”应优先映射到候选中的“水泵”而不是更宽的“泵类设备”；用户说“泵”可映射“泵类设备”。
只返回 JSON：{"items":[{"input_term":"逐字复制输入","tag_code":"候选中的code或UNRESOLVED","confidence":0到1}]}。
""".strip()
        # Candidate count is small for this offline taxonomy; keep prompt bounded anyway.
        body = {
            'model': self.settings.llm_model,
            'temperature': 0,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': json.dumps({'entity_type': entity_type, 'input_terms': remaining, 'candidate_tags': candidates[:800]}, ensure_ascii=False)},
            ],
            'response_format': {'type': 'json_object'},
        }
        try:
            response = await self.client.post(_endpoint(self.settings.llm_base_url, '/chat/completions'), headers=headers, json=body, timeout=max(self.settings.llm_timeout, 60.0))
            response.raise_for_status()
            parsed = json.loads(response.json()['choices'][0]['message']['content'])
        except Exception as exc:
            raise AssetError(ErrorCode.UPSTREAM_ERROR, '资产语义标签映射暂时不可用，请稍后重试。', f'semantic tag resolution failed: {type(exc).__name__}: {exc}') from exc
        allowed_terms = set(remaining)
        for item in parsed.get('items', []) if isinstance(parsed, dict) else []:
            if not isinstance(item, dict):
                continue
            term = str(item.get('input_term') or '')
            code = str(item.get('tag_code') or '')
            if term not in allowed_terms:
                continue
            if code == 'UNRESOLVED':
                result[term] = {'unresolved': True, 'source': 'llm_constrained'}
            elif code in by_code:
                confidence = float(item.get('confidence') or 0.0)
                result[term] = {**by_code[code], 'source': 'llm_constrained', 'confidence': max(0.0, min(1.0, confidence))}
        for term in remaining:
            result.setdefault(term, {'unresolved': True, 'source': 'llm_constrained'})
        return result

    async def status(self) -> str:
        return "CONFIGURED" if self.enabled else "DISABLED"

    async def close(self) -> None:
        await self.cache.close()
        await self.client.aclose()
