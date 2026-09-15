"""Tool contracts are registered in app.mcp.server.

This module exists intentionally as the stable tool-layer boundary for future
Conversation Service adapters and unit imports.
"""

TOOL_NAMES = (
    "resolve_entity",
    "query_equipment_info",
    "query_space_tree",
    "query_space_children",
    "query_devices",
    "query_scope_collection",
    "query_points",
    "query_active_sensor_faults",
    "query_sensor_fault_history",
    "query_offline_sensors",
    "query_sensor_monitoring_status",
    "query_sensor_status_overview",
    "get_sensor_fault_evidence",
    "query_monitored_sensor_points",
)
