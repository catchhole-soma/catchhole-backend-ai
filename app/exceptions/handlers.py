from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.exceptions.app_exception import AppException
from app.exceptions.error_code import ERROR_MESSAGE_MAP, ERROR_STATUS_MAP, ErrorCode


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    def handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        return _error_response(
            error_code=exc.error_code,
            detail=exc.detail,
        )

    @app.exception_handler(RequestValidationError)
    def handle_validation_exception(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error_response(
            error_code=ErrorCode.INVALID_REQUEST,
            detail={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    def handle_unexpected_exception(_: Request, __: Exception) -> JSONResponse:
        return _error_response(
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
            detail={},
        )


def _error_response(error_code: ErrorCode, detail: dict[str, Any]) -> JSONResponse:
    status_code = ERROR_STATUS_MAP[error_code]
    message = ERROR_MESSAGE_MAP[error_code]
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "message": message,
            "error": {
                "code": error_code.value,
                "detail": detail,
            },
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
