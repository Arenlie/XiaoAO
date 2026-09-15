from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    OK = "OK"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    NEEDS_DISAMBIGUATION = "NEEDS_DISAMBIGUATION"
    SPACE_NOT_FOUND = "SPACE_NOT_FOUND"
    EQUIPMENT_NOT_FOUND = "EQUIPMENT_NOT_FOUND"
    POINT_NOT_FOUND = "POINT_NOT_FOUND"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    DATABASE_ERROR = "DATABASE_ERROR"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(slots=True)
class AssetError(Exception):
    code: ErrorCode
    message: str
    operator_message: str | None = None

    def __str__(self) -> str:
        return self.operator_message or self.message


class DatabaseError(AssetError):
    def __init__(
        self,
        message: str = "资产数据库查询失败",
        operator_message: str | None = None,
    ) -> None:
        super().__init__(ErrorCode.DATABASE_ERROR, message, operator_message)
