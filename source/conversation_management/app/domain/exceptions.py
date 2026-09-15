from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    status_code: int = 400

    def __str__(self) -> str:
        return self.message


class NotFoundError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=404)


class ConflictError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=409)


class TooManyRequestsError(AppError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code=code, message=message, status_code=429)
