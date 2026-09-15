from __future__ import annotations

from app.repositories.profile_repository import ProfileRepository


class ProfileService:
    def __init__(self, session_factory, repo: ProfileRepository) -> None:
        self.session_factory = session_factory
        self.repo = repo

    async def get(self, user_token: str) -> dict:
        async with self.session_factory() as session:
            profile = await self.repo.get(session, user_token)
            return profile.profile_json if profile else {}

    async def update(self, user_token: str, profile_json: dict) -> dict:
        async with self.session_factory() as session, session.begin():
            await self.repo.upsert(session, user_token, profile_json)
        return profile_json

    async def delete(self, user_token: str) -> None:
        async with self.session_factory() as session, session.begin():
            await self.repo.delete(session, user_token)
