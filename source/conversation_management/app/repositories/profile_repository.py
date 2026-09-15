from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import UserProfile


class ProfileRepository:
    async def get(self, session: AsyncSession, user_token: str) -> UserProfile | None:
        return await session.scalar(select(UserProfile).where(UserProfile.user_token == user_token))

    async def upsert(self, session: AsyncSession, user_token: str, profile_json: dict) -> None:
        stmt = insert(UserProfile).values(user_token=user_token, profile_json=profile_json)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserProfile.user_token],
            set_={"profile_json": profile_json},
        )
        await session.execute(stmt)

    async def delete(self, session: AsyncSession, user_token: str) -> None:
        await session.execute(delete(UserProfile).where(UserProfile.user_token == user_token))
