from __future__ import annotations

ALLOWED_RELATIONS = {
    "derived_from", "supports", "materialized_from", "filtered_from",
    "aggregated_from", "enriched_from", "parsed_from", "diagnosed_from",
    "summarized_from", "supersedes",
}


def normalize_relation(value: str) -> str:
    relation = (value or "derived_from").strip().lower()
    return relation if relation in ALLOWED_RELATIONS else "derived_from"
