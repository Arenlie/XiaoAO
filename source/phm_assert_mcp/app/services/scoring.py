from __future__ import annotations

from app.domain.entities import Candidate


def clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def rank_candidates(
    candidates: list[Candidate],
    rerank_scores: dict[int, float],
    profile_equip_nos: list[str] | None = None,
) -> list[Candidate]:
    """Rank pgvector candidates with the configured reranker.

    No language keyword, alias, edit-distance or hand-written field weight is used.
    A profile equipment number is recorded as an authoritative prior so the resolver
    can collapse an ambiguity only when exactly one real candidate belongs to it.
    """

    profile_numbers = {
        str(value).strip().upper()
        for value in (profile_equip_nos or [])
        if str(value).strip()
    }
    ranked: list[Candidate] = []
    for index, candidate in enumerate(candidates):
        candidate.rerank_score = clamp(float(rerank_scores.get(index, 0.0)))
        candidate.vector_score = clamp(float(candidate.vector_score or 0.0))
        candidate.profile_adjustment = (
            0.05
            if str(candidate.equip_no or candidate.metadata.get("equip_no") or "")
            .strip()
            .upper()
            in profile_numbers
            else 0.0
        )
        candidate.final_score = clamp(
            0.72 * candidate.rerank_score
            + 0.28 * candidate.vector_score
            + candidate.profile_adjustment
        )
        candidate.source = "embedding_rerank"
        ranked.append(candidate)
    ranked.sort(
        key=lambda candidate: (
            -candidate.final_score,
            -candidate.rerank_score,
            -candidate.vector_score,
            candidate.entity_key,
        )
    )
    return ranked
