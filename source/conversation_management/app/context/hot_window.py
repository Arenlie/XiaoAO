from __future__ import annotations

from uuid import UUID


SLOT_NAMES = {0: "CURRENT", 1: "PREVIOUS_1", 2: "PREVIOUS_2"}


def reorder_hot_topics(existing: list[UUID], activated: UUID) -> list[UUID]:
    """MRU ordering. Continuing CURRENT does not rotate slots."""
    compact: list[UUID] = []
    for item in existing:
        if item not in compact:
            compact.append(item)
    if compact and compact[0] == activated:
        return compact[:3]
    return [activated, *[item for item in compact if item != activated]][:3]
