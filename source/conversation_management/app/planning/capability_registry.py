from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    capability_id: str
    requires: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    tool_ids: tuple[str, ...] = ()
    constraints: dict = field(default_factory=dict)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._items: dict[str, CapabilityDescriptor] = {}
        self._register_defaults()

    def register(self, descriptor: CapabilityDescriptor) -> None:
        self._items[descriptor.capability_id] = descriptor

    def list(self) -> list[CapabilityDescriptor]:
        return list(self._items.values())

    def producers_for(self, semantic_type: str) -> list[CapabilityDescriptor]:
        return [item for item in self._items.values() if semantic_type in item.produces]

    def _register_defaults(self) -> None:
        defaults = [
            CapabilityDescriptor("phm.asset.query", (), ("equipment_entity", "point_entity", "entity_set"), ("mcp.phm_asset.query_equipment_info", "mcp.phm_asset.query_devices", "mcp.phm_asset.query_scope_collection", "mcp.phm_asset.query_points")),
            CapabilityDescriptor("phm.health.query", ("equipment_entity",), ("health_score", "health_score_set"), ("mcp.phm_data.query_health_score", "workflow.phm.health_collection_batch")),
            CapabilityDescriptor("phm.alarm.query", ("equipment_entity",), ("alarm_set",), ("mcp.phm_data.query_alarm_records",)),
            CapabilityDescriptor("phm.sensor.query", (), ("sensor_fault_set", "sensor_monitoring_set"), tuple()),
            CapabilityDescriptor("phm.data.query", ("equipment_entity",), ("waveform", "waveform_set", "trend", "trend_set"), ("mcp.phm_data.get_waveform", "mcp.phm_data.get_device_data", "mcp.phm_data.get_feature_trend", "mcp.phm_data.get_temperature_trend")),
            CapabilityDescriptor("phm.feature.compute", ("waveform",), ("time_domain_features", "frequency_domain_features", "computed_metric"), ("mcp.phm_feature.extract_vibration_features", "mcp.phm_feature.extract_rotational_speed_feature")),
            CapabilityDescriptor("phm.diagnosis.run", ("equipment_entity",), ("diagnosis_result",), ("mcp.phm_diagnosis.diagnose_point", "mcp.phm_diagnosis.diagnose_device")),
            CapabilityDescriptor("attachment.parse", ("attachment_document",), ("attachment_table", "attachment_image"), ("file.search", "spreadsheet.inspect")),
            CapabilityDescriptor("evidence.read", (), ("evidence_slice",), ("evidence.broker",)),
        ]
        for item in defaults:
            self.register(item)
