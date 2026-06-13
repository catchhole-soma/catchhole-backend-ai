from typing import Any

from app.exceptions.error_code import ErrorCode


class AppException(Exception):
    def __init__(self, error_code: ErrorCode, detail: dict[str, Any] | None = None) -> None:
        self.error_code = error_code
        self.detail = detail or {}
        super().__init__(error_code.value)
