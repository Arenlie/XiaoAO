from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Candidate:
    entity_type: str
    entity_key: str
    display_name: str = ""
    equip_no: str = ""
    point_no: str = ""
    search_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    text_score: float = 0.0
    vector_score: float = 0.0
    field_score: float = 0.0
    field_coverage: float = 0.0
    critical_mismatch: int = 0
    profile_recall_score: float = 0.0
    rrf_score: float = 0.0
    rerank_score: float = 0.0
    structured_score: float = 0.0
    profile_adjustment: float = 0.0
    final_score: float = 0.0
    source: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_type": self.entity_type,
            "entity_key": self.entity_key,
            "display_name": self.display_name,
            "equip_no": self.equip_no or None,
            "point_no": self.point_no or None,
            "search_text": self.search_text,
            "metadata": self.metadata,
            "text_score": round(self.text_score, 6),
            "vector_score": round(self.vector_score, 6),
            "field_score": round(self.field_score, 6),
            "field_coverage": round(self.field_coverage, 6),
            "critical_mismatch": self.critical_mismatch,
            "profile_recall_score": round(self.profile_recall_score, 6),
            "rrf_score": round(self.rrf_score, 6),
            "rerank_score": round(self.rerank_score, 6),
            "structured_score": round(self.structured_score, 6),
            "profile_adjustment": round(self.profile_adjustment, 6),
            "final_score": round(self.final_score, 6),
            "source": self.source or None,
            **self.extra,
        }
