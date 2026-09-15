from __future__ import annotations

import re
from collections import Counter
from typing import Iterable

from app.models.evidence import ConversationTopic


def _tokens(text: str) -> Counter[str]:
    value = re.sub(r"\s+", "", (text or "").lower())
    grams: list[str] = []
    grams.extend(re.findall(r"[a-z0-9_.#-]+", value))
    grams.extend(value[i:i+2] for i in range(max(0, len(value)-1)))
    return Counter(x for x in grams if x)


def search_topics(query: str, topics: Iterable[ConversationTopic], *, limit: int = 5) -> list[ConversationTopic]:
    q = _tokens(query)
    if not q:
        return []
    scored: list[tuple[float, ConversationTopic]] = []
    for topic in topics:
        hay = " ".join(
            [
                topic.title or "",
                topic.topic_summary or "",
                topic.searchable_text or "",
                str(topic.primary_subject or {}),
                str(topic.scope or {}),
            ]
        )
        t = _tokens(hay)
        overlap = sum(min(count, t.get(token, 0)) for token, count in q.items())
        if overlap <= 0:
            continue
        score = overlap / max(1, sum(q.values()))
        scored.append((score, topic))
    scored.sort(key=lambda item: (item[0], item[1].updated_at), reverse=True)
    return [item[1] for item in scored[: max(1, limit)]]
