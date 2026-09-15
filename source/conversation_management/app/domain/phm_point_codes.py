from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# PHM point-code convention used by Data MCP.
# Raw vibration and temperature identities share the same prefix and differ only by
# their terminal measurement marker: A = vibration waveform, T = temperature source.
VIBRATION_RAW_SUFFIX = "A"
TEMPERATURE_RAW_SUFFIX = "T"
TEMPERATURE_KPI_ID = "000"

VIBRATION_FEATURES: dict[str, str] = {
    "001": "通频速度有效值",
    "002": "低频加速度有效值",
    "003": "高频加速度有效值",
    "004": "加速度峰值",
    "005": "振动冲击值",
    "006": "加速度峭度指标",
    "007": "低频冲击值LR",
    "008": "高频冲击值HR",
}
VIBRATION_FEATURE_KPI_IDS: tuple[str, ...] = tuple(VIBRATION_FEATURES)


def _text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    result = str(value).strip()
    return result or None


def raw_measurement_point_code(value: Any) -> str | None:
    """Normalize a PHM raw/feature code to its raw A/T measurement code.

    Accepted forms:
      - ``<prefix>A`` / ``<prefix>T``
      - ``<prefix>A001`` ... ``<prefix>A008``
      - ``<prefix>T000``

    Unknown code families are deliberately not guessed. This keeps legacy entities
    whose ``point_no`` does not follow the A/T convention fully backward compatible.
    """

    code = _text(value)
    if not code:
        return None

    terminal = code[-1:].upper()
    if terminal in {VIBRATION_RAW_SUFFIX, TEMPERATURE_RAW_SUFFIX}:
        return f"{code[:-1]}{terminal}"

    if len(code) >= 4 and code[-3:].isdigit():
        measurement_suffix = code[-4].upper()
        feature_suffix = code[-3:]
        raw = f"{code[:-4]}{measurement_suffix}"
        if measurement_suffix == VIBRATION_RAW_SUFFIX and feature_suffix in VIBRATION_FEATURES:
            return raw
        if measurement_suffix == TEMPERATURE_RAW_SUFFIX and feature_suffix == TEMPERATURE_KPI_ID:
            return raw
    return None


def derive_phm_point_codes(value: Any) -> dict[str, Any]:
    """Derive the complete PHM A/T + feature-code family from one point code."""

    raw = raw_measurement_point_code(value)
    if not raw:
        return {}

    prefix = raw[:-1]
    wave_point_no = f"{prefix}{VIBRATION_RAW_SUFFIX}"
    temperature_point_no = f"{prefix}{TEMPERATURE_RAW_SUFFIX}"
    vibration_feature_codes = {
        kpi_id: f"{wave_point_no}{kpi_id}" for kpi_id in VIBRATION_FEATURE_KPI_IDS
    }
    return {
        "source_point_code": _text(value),
        "wave_point_no": wave_point_no,
        "temperature_point_no": temperature_point_no,
        # Data MCP stores/query trends using the raw point id plus a KPI id. Keep these
        # explicit aliases so existing get_feature_trend/get_temperature_trend contracts
        # do not need the LLM to manufacture identifiers.
        "feature_point_id": wave_point_no,
        "temperature_point_id": temperature_point_no,
        "vibration_feature_codes": vibration_feature_codes,
        "vibration_feature_code_list": list(vibration_feature_codes.values()),
        "temperature_feature_code": f"{temperature_point_no}{TEMPERATURE_KPI_ID}",
    }


def enrich_phm_point_entity(entity: Mapping[str, Any] | None) -> dict[str, Any]:
    """Add deterministic PHM code aliases to a fuzzy-entity point result.

    Existing explicit fields always win. ``point_no`` itself is never rewritten, which
    preserves the public fuzzy-entity contract and alarm-query semantics.
    """

    result = dict(entity or {})
    if not result:
        return result

    metadata = result.get("metadata")
    metadata_map = dict(metadata) if isinstance(metadata, Mapping) else {}
    source: Any = None
    for key in (
        "point_no",
        "pointNo",
        "wave_point_no",
        "wave_point_code",
        "temperature_point_no",
        "temperature_point_code",
    ):
        if result.get(key) not in (None, ""):
            source = result[key]
            break
        if metadata_map.get(key) not in (None, ""):
            source = metadata_map[key]
            break

    derived = derive_phm_point_codes(source)
    for key, value in derived.items():
        result.setdefault(key, value)
    return result
