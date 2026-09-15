from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db import DatabaseManager
from app.errors import AssetError, ErrorCode
from app.repositories.equipment_repository import EquipmentRepository


class PointRepository:
    def __init__(self, db: DatabaseManager, settings: Settings, equipment: EquipmentRepository) -> None:
        self.db = db
        self.settings = settings
        self.equipment = equipment
        self.table = settings.asset_catalog_table
        self.norm = settings.asset_normalize_function

    async def query(self, *, equip_no: str, point_type: str | None, keyword: str | None, limit: int) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        equipment = await self.equipment.get_by_no(equip_no)
        if not equipment:
            raise AssetError(ErrorCode.EQUIPMENT_NOT_FOUND, "设备不存在")
        requested = (point_type or "").strip().lower()
        effective_limit = min(limit, self.settings.asset_query_max_points)

        # The Dify YML does not expose one authoritative point_type column. It filters diagnosis
        # acceleration points using param_name/metric_name/measurement_name/point_name/search_text.
        # This implementation preserves that verified behavior and also honors metadata.point_type
        # / measurement_type when those fields exist.
        measurement_blob = f"{self.norm}(concat_ws(' ', metadata->>'point_type', metadata->>'measurement_type', metadata->>'param_name', metadata->>'metric_name', metadata->>'measurement_name', metadata->>'point_name', display_name, search_text))"
        # Keep $3 referenced and explicitly typed in every branch.  The previous
        # Python-side branching produced SQL containing $1, $2 and $4 but no $3 for
        # the common vibration_acceleration request. asyncpg still received four
        # arguments, so PostgreSQL raised IndeterminateDatatypeError for $3.
        type_clause = f"""
            CASE
              WHEN $3::text = '' THEN TRUE
              WHEN $3::text = 'vibration_acceleration'
                THEN {measurement_blob} LIKE '%加速度%'
              WHEN $3::text = 'vibration'
                THEN ({measurement_blob} LIKE '%振动%' OR {measurement_blob} LIKE '%加速度%')
              WHEN $3::text = 'temperature'
                THEN {measurement_blob} LIKE '%温度%'
              ELSE {measurement_blob} LIKE '%'||{self.norm}($3::text)||'%'
            END
        """

        sql = f"""
            SELECT entity_key, equip_no, point_no, display_name, search_text, metadata
            FROM {self.table}
            WHERE entity_type='point'
              AND upper(equip_no)=upper($1)
              AND ($2='' OR {self.norm}(concat_ws(' ',display_name,metadata->>'point_name',metadata->>'station_name',metadata->>'component_name',metadata->>'position_name',metadata->>'direction_name',metadata->>'measurement_name',search_text)) LIKE '%'||{self.norm}($2)||'%')
              AND ({type_clause})
            ORDER BY COALESCE(metadata->>'point_name',display_name), point_no
            LIMIT $4
        """
        rows = await self.db.fetch(sql, equip_no, keyword or "", requested, effective_limit + 1)
        truncated = len(rows) > effective_limit
        return equipment, [self._to_point(r) for r in rows[:effective_limit]], truncated

    @staticmethod
    def _to_point(row: dict[str, Any]) -> dict[str, Any]:
        m = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        measurement = str(m.get("point_type") or m.get("measurement_type") or m.get("measurement_name") or m.get("param_name") or m.get("metric_name") or "")
        compact = measurement + str(m.get("point_name") or row.get("display_name") or "")
        if "加速度" in compact:
            point_type = "vibration_acceleration"
        elif "温度" in compact:
            point_type = "temperature"
        elif "振动" in compact:
            point_type = "vibration"
        else:
            point_type = measurement or None
        return {
            "point_id": str(m.get("point_id") or row.get("entity_key") or "") or None,
            "point_no": str(row.get("point_no") or m.get("point_no") or "") or None,
            "point_name": str(m.get("point_name") or row.get("display_name") or "") or None,
            "point_type": point_type,
            "equip_no": str(row.get("equip_no") or m.get("equip_no") or "") or None,
            "space_id": str(m.get("space_id") or m.get("leaf_space_id") or "") or None,
            "metadata": m,
        }
