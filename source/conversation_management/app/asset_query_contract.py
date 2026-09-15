"""Versioned asset query contract; shared verbatim with Conversation."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TOOL_ID = "mcp.phm_asset.query_asset_collection"
GROUP_FIELDS = {"equipment_class", "equipment_subclass", "purpose", "structure_type",
                "model", "company", "plant", "line", "area"}
NAME_FIELDS = {"equipment_name", "legacy_search_text"}
CATEGORY_FIELDS = {"equipment_class", "purpose", "structure_type", "monitoring", "management"}
GENERIC_CLASSES = {"设备", "设备资产", "全部设备", "所有设备", "设备类", "equipment"}


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_space_id: str = Field(min_length=1, max_length=128)
    target_entity_level: Literal["equipment"] = "equipment"
    recursive: bool = True


class SharedCatalogScope(BaseModel):
    """Server-authorized whole catalog scope for unscoped collection queries."""

    model_config = ConfigDict(extra="forbid")
    scope_type: Literal["shared_catalog"] = "shared_catalog"
    target_entity_level: Literal["equipment"] = "equipment"


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query_id: str = Field(min_length=1, max_length=128)
    mode: Literal["same_set", "refine_set", "refresh"] = "same_set"


class AssetQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["2.0"] = "2.0"
    scope: Scope | SharedCatalogScope | None = None
    reference: Reference | None = None
    predicate: dict[str, Any] | None = None
    operation: Literal["count", "list", "group"] = "list"
    group_by: str | None = None
    page_size: int = Field(default=1000, ge=1, le=5000)
    cursor: str | None = Field(default=None, max_length=1024)
    freshness: Literal["current", "referenced_snapshot"] = "current"

    @model_validator(mode="after")
    def valid_query(self):
        if self.scope is None and self.reference is None:
            raise ValueError("必须提供真实区域或已确认的查询集合")
        if self.operation == "group" and self.group_by not in GROUP_FIELDS:
            raise ValueError("分类汇总需要受支持的分组维度")
        if self.operation != "group" and self.group_by is not None:
            raise ValueError("只有分类汇总可以指定分组维度")
        if self.reference and self.reference.mode == "same_set" and (self.predicate or self.scope):
            raise ValueError("复用原集合时不能增加条件或替换范围")
        if self.reference and self.reference.mode in {"same_set", "refine_set"}:
            if self.freshness != "referenced_snapshot":
                raise ValueError("原集合及集合内筛选必须使用原快照")
        elif self.freshness != "current":
            raise ValueError("新的查询必须读取当前目录")
        validate_predicate(self.predicate)
        return self


def validate_predicate(node, *, depth=0, budget=None, allow_legacy=False):
    if node is None:
        return
    if budget is None:
        budget = [0]
    if not isinstance(node, dict) or depth > 4:
        raise QueryError("INVALID_PREDICATE", "查询条件结构无效或嵌套过深")
    if set(node) & {"all", "any", "not"}:
        if len(node) != 1:
            raise QueryError("INVALID_PREDICATE", "组合条件不能混用其他字段")
        kind, children = next(iter(node.items()))
        if kind == "not":
            if children is None:
                raise QueryError("INVALID_PREDICATE", "排除条件不能为空")
            validate_predicate(children, depth=depth+1, budget=budget, allow_legacy=allow_legacy)
        else:
            if not isinstance(children, list) or not 1 <= len(children) <= 16:
                raise QueryError("INVALID_PREDICATE", "组合条件数量无效")
            for child in children:
                if child is None:
                    raise QueryError("INVALID_PREDICATE", "组合条件不能为空")
                validate_predicate(child, depth=depth+1, budget=budget, allow_legacy=allow_legacy)
        return
    budget[0] += 1
    if budget[0] > 16 or set(node) - {"field", "operator", "value", "category_id", "include_descendants"}:
        raise QueryError("INVALID_PREDICATE", "查询条件超出受支持范围")
    field, op = node.get("field"), node.get("operator")
    if field in NAME_FIELDS and (field != "legacy_search_text" or allow_legacy):
        if op not in {"contains", "equals", "starts_with"} or not isinstance(node.get("value"), str):
            raise QueryError("INVALID_PREDICATE", "名称查询操作无效")
        if not node["value"] or len(node["value"]) > 256 or "category_id" in node or "include_descendants" in node:
            raise QueryError("INVALID_PREDICATE", "名称查询内容无效")
    elif field in CATEGORY_FIELDS or (allow_legacy and field == "legacy_tag"):
        if "category_id" in node and "value" in node:
            raise QueryError("INVALID_PREDICATE", "类别名称和类别编码只能选择一种，避免条件互相矛盾")
        if op != "is" or not isinstance(node.get("category_id") or node.get("value"), str):
            raise QueryError("INVALID_PREDICATE", "类别查询条件无效")
        if len(node.get("category_id") or node.get("value")) > 256:
            raise QueryError("INVALID_PREDICATE", "类别名称过长")
        if "include_descendants" in node and not isinstance(node["include_descendants"], bool):
            raise QueryError("INVALID_PREDICATE", "子类包含策略必须明确")
    else:
        raise QueryError("UNSUPPORTED_FIELD", "当前资产查询不支持指定的筛选字段")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def signature(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def normalize_tree(node):
    if node is None:
        return None
    if "not" in node:
        return {"not": normalize_tree(node["not"])}
    for key in ("all", "any"):
        if key in node:
            children = {canonical(normalize_tree(n)): normalize_tree(n) for n in node[key]}
            return {key: [children[k] for k in sorted(children)]}
    return dict(node)


def sign_context(secret, claims, request, *, ttl=300):
    if len(secret) < 32:
        raise QueryError("QUERY_AUTH_NOT_CONFIGURED", "资产查询服务认证尚未配置")
    data = {**claims, "exp": int(time.time())+ttl, "body": signature(request)}
    raw = base64.urlsafe_b64encode(canonical(data).encode()).decode().rstrip("=")
    mac = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw+"."+mac


def verify_context(secret, token, request):
    if len(secret) < 32 or not isinstance(token, str) or len(token) > 8192:
        raise QueryError("QUERY_AUTH_REQUIRED", "资产查询调用未经认证")
    try:
        raw, mac = token.rsplit(".", 1)
        expected = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, mac):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(raw+"="*(-len(raw) % 4)))
        if value["exp"] < time.time() or value["exp"] > time.time()+600 or value["body"] != signature(request):
            raise ValueError()
        for key in ("subject", "conversation_id", "branch_id", "task_id", "call_id"):
            if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 128:
                raise ValueError()
        if value.get("shared_catalog") is not True:
            raise QueryError("QUERY_SCOPE_UNSUPPORTED", "尚未配置当前用户可访问的资产范围")
        return value
    except QueryError:
        raise
    except Exception:
        raise QueryError("QUERY_AUTH_REQUIRED", "资产查询调用认证无效或已过期") from None
