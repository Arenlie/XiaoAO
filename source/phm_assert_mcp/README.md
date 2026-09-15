# PHM Asset MCP 1.5.0

新增 `query_asset_collection`：设备范围、审核类别、正式名称条件统一通过固定参数化 SQL 执行，支持数量、清单、分类和原集合追问。只新增查询暂存表，不修改原资产目录和传感器实现。

部署与接口以总包 `docs/DEPLOYMENT_1.5.0.md`、`docs/FRONTEND_API_1.5.0.md` 为准。下面是历史版本说明。

# 1.2.1：监测注册表查询与传感器范围核验

新增 query_monitored_sensor_points，监测清单以传感器注册表为准。完整清单可免逐点核验；截断不认定未监测。详见总包 docs/SENSOR_RETRIEVAL_AND_DEPLOYMENT_1.2.1.md。

# PHM Asset MCP 1.2.0

新增六个只读传感器查询工具，复用当前服务与资产实体。接口、配置、边界及部署说明见总包 `docs/SENSOR_API_AND_DEPLOYMENT_1.2.0.md`。原有实体检索和层级工具接口不变。

# PHM Asset MCP 1.1.2

已有部署使用总包根目录的 `sudo bash deploy.sh --check`、`sudo bash deploy.sh --apply` 增量更新，保留原有环境配置和依赖。

1.1.2 修复 stateless MCP 请求关闭共享模型客户端的问题：Runtime 在 ASGI 应用启动时创建，仅在应用关闭时清理。`/ready` 检查模型客户端是否仍打开，返回应用版本和生命周期信息。详情见总包 `docs/PATCH_AND_API_CHANGES_1.1.2.md`。
