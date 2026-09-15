from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ApiResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T
    message: str = "ok"
    request_id: str


class CursorApiResponse(ApiResponse[T], Generic[T]):
    """API envelope for cursor-paginated resources."""

    next_cursor: str | None = None
