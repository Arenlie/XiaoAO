# PHM 问题1最小修复方案：全局设备集合查询被错误解析成具体设备

版本目标：基于 `PHM Full Stack 1.7.0 Evidence Fabric v1` 做单问题 Hotfix。  
本次只修复：`现在一共有多少台水泵？` 这类**没有明确区域、但明确要求设备集合数量/清单/分类**的问题，被错误执行为单设备实体检索。

## 1. 已确认根因

真实调试日志中，主控任务理解已经正确输出：

```json
{
  "asset_query": {
    "active": true,
    "operation": "count",
    "predicate": {
      "field": "equipment_class",
      "operator": "is",
      "value": "水泵",
      "include_descendants": true
    }
  },
  "asset_semantics": {
    "needs_asset_lookup": true,
    "equipment_type": {
      "raw_text": "水泵",
      "retrieval_text": "水泵"
    }
  }
}
```

错误发生在后续 `identity_dependency()`：现有逻辑只要 `needs_asset_lookup=true`，就认为必须做实体解析；随后 `UnifiedEntityResolutionLayer` 对 equipment/any 级别允许 `equipment_type` 参与检索，导致 `水泵` 被当作一台具体设备候选，最终把查询范围缩到该设备所在区域。

因此本次不改 Task Understanding 字段，也不删除 `equipment_type`。只在**新资产集合查询且没有明确根实体**时，确定性地跳过 singular entity resolution。

## 2. 最小修复原则

### 2.1 不修改的字段

以下字段名称、类型和含义全部保持：

- `asset_semantics.equipment_type`
- `asset_semantics.needs_asset_lookup`
- `goal_frame.anchor_entity_level`
- `goal_frame.target_entity_level`
- `asset_query.active`
- `asset_query.operation`
- `asset_query.predicate`
- `completion_contract`
- `EvidenceRequirement`
- `TaskDelta`

因此不会引入 Completion/Evidence 评估层字段兼容问题。

### 2.2 新增的唯一协议能力

只在 Conversation → `phm-asset-mcp.query_asset_collection` 的**内部 AssetQuery scope**增加：

```json
{
  "scope": {
    "scope_type": "shared_catalog",
    "target_entity_level": "equipment"
  }
}
```

它不是 LLM Task 字段，不进入 Completion/Evidence schema。

含义：使用受信任 Conversation 服务已经授权的共享资产目录进行设备集合查询，不构造虚假的 `root_space_id`。

## 3. 判定规则

新增纯程序函数：

```python
is_unscoped_new_asset_collection(state)
```

只有同时满足以下条件才返回 `True`：

1. `asset_query.active == true`；
2. `reference_mode == new`；
3. 没有 `reference_target_level` 指向历史 area/equipment/point；
4. 本轮没有明确 `area`；
5. 本轮没有明确 `equipment`；
6. 本轮没有明确 `equip_no`；
7. 本轮没有明确 `point/point_no`。

特别注意：

```text
equipment_type
```

故意不作为 singular identity 判断字段。

### 示例

`现在一共有多少台水泵？`

```text
asset_query.active = true
area = empty
equipment = empty
equip_no = empty
equipment_type = 水泵
=> shared_catalog collection
=> 不做单设备实体解析
```

`总部钢铁有多少台水泵？`

```text
area = 总部钢铁
=> 不是 global collection
=> 仍解析“总部钢铁”区域
=> 水泵仍只是 predicate
```

`水泵的健康度怎么样？`

```text
asset_query.active = false
health requires equipment
=> 不触发 global collection 规则
=> 仍要求 equipment entity resolution
```

`这个区域有多少台水泵？`

```text
reference_target_level = space
=> 不是 global collection
=> 使用上下文区域实体
```

## 4. 代码修改清单

### 4.1 新增 `conversation_management/app/asset_collection_scope.py`

职责：统一判定本轮是否属于“无明确根实体的新设备集合查询”。

要求：

- 只读结构化语义；
- 禁止自然语言关键词规则；
- 禁止识别“水泵/风机/电机”等具体类别词；
- `equipment_type` 只作为 collection predicate，不参与 root identity 判定。

### 4.2 修改 `conversation_management/app/orchestration/entity_dependency.py`

原逻辑：

```python
required = workflow_required or bool(semantics.get("needs_asset_lookup")) or ...
```

修改为：

```python
unscoped_collection = is_unscoped_new_asset_collection(state)
model_identity_lookup = bool(semantics.get("needs_asset_lookup")) and not unscoped_collection
required = workflow_required or model_identity_lookup or ...
```

若为 global collection：

```text
required = false
source = shared_catalog_collection
```

### 4.3 修改 `conversation_management/app/services/asset_collections.py`

新集合查询：

```python
if is_unscoped_new_asset_collection(state):
    query["scope"] = {"scope_type": "shared_catalog"}
else:
    root = resolve_asset_identity(state).get("space_id")
    ...
```

重要：global 判定必须在读取 `active_entity` 之前执行，避免旧主题中某台水泵污染新全局集合问题。

### 4.4 修改 `conversation_management/app/tools/phm_asset_context.py`

`asset_identity_available()` 对 global collection 返回 `True`，避免执行器因不存在 `space_id` 而再次要求实体检索。

### 4.5 修改两份共享 `asset_query_contract.py`

文件：

- `conversation_management/app/asset_query_contract.py`
- `phm_assert_mcp/app/asset_query_contract.py`

新增：

```python
class SharedCatalogScope(BaseModel):
    scope_type: Literal["shared_catalog"] = "shared_catalog"
    target_entity_level: Literal["equipment"] = "equipment"
```

并将：

```python
scope: Scope | None
```

改为：

```python
scope: Scope | SharedCatalogScope | None
```

现有区域 Scope 格式完全不变。

### 4.6 修改 `phm_assert_mcp/app/services/asset_collection_engine.py`

`_scope()` 增加：

```python
if scope.get("scope_type") == "shared_catalog":
    return "全部资产", "TRUE", []
```

因为 `query()` 在进入 `execute()` 前已经通过 `verify_context()` 校验：

```text
shared_catalog == true
```

因此只有受信任 Conversation 服务签发的请求才能走该范围。

### 4.7 Prompt 仅做语义强化，不作为修复核心

修改：

- `app/orchestration/task_instructions.py`
- `app/orchestration/asset_query_policy.py`

增加说明：没有明确区域/设备根对象时，类别/名称条件仍是集合筛选条件，不能因此选择某一台设备。

程序确定性逻辑仍是最终防线。

## 5. 明确不修改

本次禁止顺手修改：

- Evidence semantic type；
- `asset_count/entity_set`；
- Completion；
- PARTIAL 处理；
- Capability Planner 循环；
- ResultFollowup；
- 分页；
- Knowledge；
- AnswerResult；
- 其它健康度/报警/诊断执行策略。

这些问题后续单独处理。

## 6. 必须通过的回归测试

### Case A：本问题

```text
现在一共有多少台水泵？
```

必须满足：

- 不调用 `resolve_entity` 去检索一台“水泵”；
- `identity_dependency.required == false`；
- AssetQuery.scope 为 `shared_catalog`；
- predicate 仍为 `equipment_class=水泵`；
- 只执行设备集合查询。

### Case B：区域集合

```text
总部钢铁有多少台水泵？
```

必须满足：

- 仍解析 `总部钢铁`；
- `水泵` 不作为具体设备根；
- scope 使用真实 `root_space_id`；
- 不使用 shared_catalog。

### Case C：单设备健康度

```text
水泵的健康度怎么样？
```

必须满足：

- `asset_query.active=false`；
- `identity_dependency.required == true`；
- required level 为 `equipment`；
- 仍进入设备实体解析。

### Case D：上下文区域引用

```text
这个区域有多少台水泵？
```

如果 `reference_target_level=space`：

- 不使用 shared_catalog；
- 继续使用/解析上下文区域。

### Case E：旧实体污染防护

上一轮 active entity 恰好是一台水泵，本轮：

```text
现在一共有多少台水泵？
```

仍必须生成：

```json
{"scope":{"scope_type":"shared_catalog"}}
```

不能使用旧设备的 `space_id`。

## 7. 本次验收结论

修复成功的标准不是“回答数字看起来正确”，而是 Trace 中同时满足：

```text
workflow classification:
  asset_query.active=true
  predicate=equipment_class:水泵

entity dependency:
  required=false
  source=shared_catalog_collection

Asset MCP query_asset_collection:
  scope.scope_type=shared_catalog
  predicate=equipment_class:水泵
```

并且整个流程中不出现：

```text
resolve_entity -> equipment candidate "水泵"
```

