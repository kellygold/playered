"""Stable API problem envelopes and FastAPI exception handlers."""

import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from image23mf.contracts.api import ApiError, ApiErrorCode, ApiErrorEnvelope

logger = logging.getLogger(__name__)


class ApiProblem(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: ApiErrorCode,
        message: str,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", uuid.uuid4().hex)


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: ApiErrorCode,
    message: str,
    retryable: bool = False,
    details: Optional[dict[str, Any]] = None,
) -> JSONResponse:
    envelope = ApiErrorEnvelope(
        error=ApiError(
            code=code,
            message=message,
            request_id=request_id(request),
            retryable=retryable,
            details=details or {},
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(mode="json"),
        headers={"X-Request-ID": envelope.error.request_id},
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def api_problem_handler(request: Request, error: ApiProblem) -> JSONResponse:
        return problem_response(
            request,
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            details=error.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, error: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "location": [str(part) for part in item["loc"]],
                "message": item["msg"],
                "type": item["type"],
            }
            for item in error.errors()
        ]
        return problem_response(
            request,
            status_code=422,
            code=ApiErrorCode.VALIDATION_ERROR,
            message="The request did not match the API contract.",
            details={"issues": issues},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        code = (
            ApiErrorCode.NOT_FOUND
            if error.status_code == 404
            else ApiErrorCode.METHOD_NOT_ALLOWED
            if error.status_code == 405
            else ApiErrorCode.CONFLICT
        )
        return problem_response(
            request,
            status_code=error.status_code,
            code=code,
            message=str(error.detail),
        )

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled API error request_id=%s", request_id(request), exc_info=error)
        return problem_response(
            request,
            status_code=500,
            code=ApiErrorCode.INTERNAL_ERROR,
            message="An unexpected local application error occurred.",
            details={},
        )
