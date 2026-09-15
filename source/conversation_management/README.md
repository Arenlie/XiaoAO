# Conversation 1.5.0

资产查询由主控解析语义，再交统一集合工具执行。数量、清单和分类追问使用同一集合；查询关联保存到真实消息链。诊断方式询问以 `answer.completed` 和 `task.completed` 结束本轮，用户通过下一条普通消息选择或另问问题。保留已有知识库引用、附件和流式输出能力。

部署与接口以总包 `docs/DEPLOYMENT_1.5.0.md`、`docs/FRONTEND_API_1.5.0.md` 为准。下面是历史版本说明。

# 1.2.1：监测注册表查询与传感器范围核验

新增 query_monitored_sensor_points，监测清单以传感器注册表为准。完整清单可免逐点核验；截断不认定未监测。详见总包 docs/SENSOR_RETRIEVAL_AND_DEPLOYMENT_1.2.1.md。

# Conversation 1.2.0

新增传感器信息查询链路、具体故障类型筛选及上下文实体绑定。面向客户的输出区分在线、离线、未监测和未确认。详见总包 `docs/SENSOR_API_AND_DEPLOYMENT_1.2.0.md`。

# Conversation Management 1.1.3

生产部署包。支持 quick 与 normal；normal 为目标驱动、证据约束的 ReAct 主控模式。旧 expert 输入仅作为 normal 的兼容别名。

已有部署使用总包根目录的 `sudo bash deploy.sh --check`、`sudo bash deploy.sh --apply` 增量更新，保留原有环境配置和依赖。不要对现有部署重新执行旧覆盖安装脚本。

1.1.2 将真实资产身份作为业务查询的前置依赖；Asset 服务错误保留错误原因并以 FAILED 结束，设备级报警查询遵循结构化目标。详情见总包 `docs/PATCH_AND_API_CHANGES_1.1.2.md`。

1.1.3 修复测点诊断输入层级被设备锚点覆盖、跨设备测点候选选择后恢复失败的问题。按已选设备筛选本任务原有测点候选，确认测点后继续原 Data → Feature → Diagnosis 链路。其他 MCP 与现有配置保留，详见总包 `docs/PATCH_AND_API_CHANGES_1.1.3.md`。
