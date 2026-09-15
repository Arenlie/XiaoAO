"""Governed category meanings. No model, embeddings or name guessing in SQL planning."""
from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
import re

from app.asset_query_contract import CATEGORY_FIELDS, GENERIC_CLASSES, QueryError, signature

REVIEWED = ["AI_REVIEWED", "APPROVED", "MODIFIED"]


class Taxonomy:
    def __init__(self, dictionary, policy=None, *, frozen_version=None):
        self.policy = policy or {}
        self.entries = {}
        # Existing reviewed tags are usable as positive evidence only. Absence is
        # unknown until a maintained policy certifies a complete dimension.
        for item in dictionary:
            code, name = item["code"], item["name"]
            prefix = code.split(".", 1)[0]
            dimension = {"equipment": "equipment_class", "purpose": "purpose",
                         "structure": "structure_type", "monitoring": "monitoring",
                         "management": "management"}.get(prefix, "legacy_tag")
            if dimension:
                key = (dimension, code)
                entry = self.entries.setdefault(key, {"id": code, "dimension": dimension,
                    "name": name, "aliases": [], "tags": [code], "type_codes": [], "parent": None,
                    "source": "reviewed_catalog"})
                if name != entry["name"]:
                    entry["aliases"].append(name)
        for entry in self.policy.get("categories", []):
            if not isinstance(entry, dict) or entry.get("dimension") not in CATEGORY_FIELDS:
                raise QueryError("TAXONOMY_INVALID", "资产分类配置中的维度无效")
            if not entry.get("id") or not entry.get("name"):
                raise QueryError("TAXONOMY_INVALID", "资产分类配置缺少编码或名称")
            e = {"aliases": [], "tags": [], "type_codes": [], "parent": None, "source": "policy", **entry}
            for key in ("aliases", "tags", "type_codes"):
                if not isinstance(e[key], list) or any(not isinstance(v, str) or not v for v in e[key]):
                    raise QueryError("TAXONOMY_INVALID", "资产分类配置中的映射无效")
            self.entries[(e["dimension"], e["id"])] = e
        self.version = signature({"policy": self.policy, "entries": sorted(self.entries.values(), key=lambda x: (x["dimension"], x["id"]))})
        if frozen_version:
            self.version = frozen_version
        for e in self.entries.values():
            self.ancestors(e)  # fail on dangling parents / cycles, never use partial closure

    @classmethod
    async def load(cls, conn, table, policy_path):
        rows = await conn.fetch(f"""SELECT DISTINCT codes.code, names.name
            FROM {table} c
            CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(NULLIF(to_jsonb(c)->'tag_codes','null'::jsonb),'[]'::jsonb)) WITH ORDINALITY codes(code,n)
            JOIN LATERAL jsonb_array_elements_text(COALESCE(NULLIF(to_jsonb(c)->'tag_names','null'::jsonb),'[]'::jsonb)) WITH ORDINALITY names(name,n) ON names.n=codes.n
            WHERE c.entity_type='equipment' AND upper(COALESCE(to_jsonb(c)->>'semantic_review_status',''))=ANY($1::text[])
            AND codes.code<>'' AND names.name<>'' ORDER BY codes.code,names.name""", REVIEWED)
        policy = {}
        if policy_path:
            try:
                policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
                if not isinstance(policy, dict) or policy.get("schema_version") != "1.0" or not policy.get("reviewed_by"):
                    raise ValueError()
            except (OSError, ValueError):
                raise QueryError("TAXONOMY_INVALID", "资产分类配置未通过版本或审核信息检查") from None
        return cls([dict(r) for r in rows], policy)

    def ancestors(self, entry):
        seen, result = {entry["id"]}, []
        while entry.get("parent"):
            key = (entry["dimension"], entry["parent"])
            if entry["parent"] in seen or key not in self.entries:
                raise QueryError("TAXONOMY_INVALID", "资产分类的上下级关系存在循环或缺失")
            entry = self.entries[key]
            result.append(entry["id"])
            seen.add(entry["id"])
        return result

    @staticmethod
    def _normalized_text(value):
        return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", str(value or "").casefold())

    @classmethod
    def _lexical_score(cls, term, value):
        left, right = cls._normalized_text(term), cls._normalized_text(value)
        if not left or not right:
            return 0.0
        if left == right:
            return 1.0
        score = SequenceMatcher(None, left, right).ratio()
        if left in right:
            score = max(score, 0.94 if len(left) >= 2 else 0.82)
        elif right in left:
            score = max(score, 0.9 if len(right) >= 2 else 0.78)
        overlap = len(set(left).intersection(right)) / max(1, min(len(set(left)), len(set(right))))
        return max(score, overlap * 0.82)

    def fallback_candidates(self, dimension, term, *, limit=500):
        """Return only real reviewed catalog tags, ordered for an LLM fallback.

        The database has no authoritative parent/child taxonomy.  This method therefore
        does not infer hierarchy; it only returns true reviewed tag ids/names and a
        lexical priority score.  Hierarchy is inferred transiently by the LLM later.
        """
        rows = []
        for (entry_dimension, _), entry in self.entries.items():
            if entry_dimension != dimension or entry.get("source") != "reviewed_catalog":
                continue
            names = [entry.get("name"), *(entry.get("aliases") or [])]
            score = max((self._lexical_score(term, name) for name in names if name), default=0.0)
            rows.append({
                "tag_code": str(entry["id"]),
                "tag_name": str(entry["name"]),
                "lexical_score": round(float(score), 6),
            })
        rows.sort(key=lambda item: (-item["lexical_score"], item["tag_name"], item["tag_code"]))
        return rows[:max(1, int(limit))], len(rows)

    def resolve(self, node):
        dimension = node["field"]
        term = str(node.get("category_id") or node.get("value") or "").strip()
        if dimension in {"equipment_class", "legacy_tag"} and term.lower() in GENERIC_CLASSES:
            return {"generic": True, "name": "设备"}
        candidates = [e for (d, _), e in self.entries.items() if (d == dimension or dimension == "legacy_tag") and
                      term.casefold() in {str(v).casefold() for v in [e["id"], e["name"], *e["aliases"]]}]
        if len(candidates) != 1:
            raise QueryError("CATEGORY_AMBIGUOUS" if candidates else "CATEGORY_UNSUPPORTED",
                             f"“{term}”尚未对应到唯一且已审核的资产分类；请明确类别，或明确要求按设备名称查询。")
        chosen = candidates[0]
        dimension = chosen["dimension"]
        included = [chosen]
        if node.get("include_descendants", True):
            included += [e for (d, _), e in self.entries.items() if d == dimension and chosen["id"] in self.ancestors(e)]
        return {"id": chosen["id"], "name": chosen["name"], "dimension": dimension,
                "tags": sorted({v for e in included for v in e["tags"]}),
                "type_codes": sorted({v for e in included for v in e["type_codes"]}),
                "include_descendants": node.get("include_descendants", True),
                "complete_reviewed": dimension in self.policy.get("complete_reviewed_dimensions", [])}

    def group_values(self, row, dimension):
        subclass = dimension == "equipment_subclass"
        if dimension == "equipment_subclass":
            dimension = "equipment_class"
        reviewed = str(row.get("semantic_review_status") or "").upper() in REVIEWED
        tags = set(row.get("tag_codes") or []) if reviewed else set()
        type_code = str((row.get("metadata") or {}).get("equip_type") or "")
        selected = [e for (d, _), e in self.entries.items() if d == dimension and
                    (tags.intersection(e["tags"]) or type_code and type_code in e["type_codes"])]
        if subclass:
            # A broad equipment class is not evidence of a formal fine-grained
            # type. Only a governed entry explicitly marked subclass qualifies.
            selected = [e for e in selected if e.get("group_level") == "subclass"]
        elif dimension == "equipment_class":
            parents = [self.entries[(dimension,p)] for e in selected for p in self.ancestors(e)]
            selected = [e for e in selected+parents if e.get("group_level") != "subclass"]
        # Parent + child on one row is one most-specific class, not two devices.
        parents = {p for e in selected for p in self.ancestors(e)}
        return sorted({e["name"] for e in selected if e["id"] not in parents})


def compile_predicate(node, taxonomy, args, *, column="row_data", normalized=None):
    """Return SQL boolean (including NULL) and only bound parameter values."""
    def param(value):
        args.append(value)
        return f"${len(args)}"
    if node is None:
        return "TRUE"
    if "not" in node:
        return "(NOT " + compile_predicate(node["not"], taxonomy, args, column=column, normalized=normalized) + ")"
    for kind, joiner in (("all", " AND "), ("any", " OR ")):
        if kind in node:
            return "(" + joiner.join(compile_predicate(c, taxonomy, args, column=column, normalized=normalized) for c in node[kind]) + ")"
    field, op = node["field"], node["operator"]
    if field in {"equipment_name", "legacy_search_text"}:
        value = node["value"]
        expr = f"NULLIF({column}->>'equipment_name','')" if field == "equipment_name" else f"NULLIF({column}->>'legacy_search_text','')"
        if normalized is not None:
            normalized.append({"field": field, "operator": op, "value": value})
        p = param(value)
        if field == "legacy_search_text" and getattr(taxonomy, "normalize_function", None):
            expr = f"{taxonomy.normalize_function}({expr})"
            p = f"{taxonomy.normalize_function}({p}::text)"
        if op == "equals":
            return f"(lower({expr})=lower({p}::text))"
        if op == "starts_with":
            return f"(left(lower({expr}),length({p}::text))=lower({p}::text))"
        # strpos treats %, _ and backslash literally; no wildcard expansion.
        return f"(strpos(lower({expr}),lower({p}::text))>0)"
    resolved = taxonomy.resolve(node)
    if normalized is not None:
        normalized.append({"field": field, **resolved})
    if resolved.get("generic"):
        return "TRUE"
    tags, types, dimension = param(resolved["tags"]), param(resolved["type_codes"]), param(resolved["dimension"])
    reviewed = f"(upper(COALESCE({column}->>'semantic_review_status',''))=ANY({param(REVIEWED)}::text[]))"
    positive = f"(({reviewed} AND COALESCE({column}->'tag_codes','[]'::jsonb) ?| {tags}::text[]) OR COALESCE({column}->'metadata'->>'equip_type','')=ANY({types}::text[]))"
    complete = f"(COALESCE({column}->'metadata'->'classification_complete_dimensions','[]'::jsonb) ? {dimension}::text)"
    if resolved["complete_reviewed"]:
        complete = f"({complete} OR {reviewed})"
    return f"(CASE WHEN {positive} THEN TRUE WHEN {complete} THEN FALSE ELSE NULL END)"
