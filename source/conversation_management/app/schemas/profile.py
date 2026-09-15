from __future__ import annotations

from pydantic import BaseModel, Field


class UserProfileView(BaseModel):
    profile_json: dict = Field(default_factory=dict)


class UpdateProfileRequest(BaseModel):
    profile_json: dict
