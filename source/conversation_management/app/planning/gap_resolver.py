from __future__ import annotations

from typing import Any

from app.planning.capability_registry import CapabilityRegistry
from app.planning.requirements import LEGACY_TO_SEMANTIC


class GapResolver:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def capability_hints(self, gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hints: list[dict[str, Any]] = []
        seen: set[str] = set()
        for gap in gaps:
            raw = str(gap.get("semantic_type") or gap.get("evidence") or "").lower()
            semantic_candidates = LEGACY_TO_SEMANTIC.get(raw, [raw])
            for semantic in semantic_candidates:
                for cap in self.registry.producers_for(semantic):
                    if cap.capability_id in seen:
                        continue
                    seen.add(cap.capability_id)
                    hints.append({
                        "capability_id": cap.capability_id,
                        "produces": list(cap.produces),
                        "tool_ids": list(cap.tool_ids),
                        "reason": gap.get("type") or "missing_evidence",
                    })
        return hints
