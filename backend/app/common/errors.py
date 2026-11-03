import logging

from botocore.exceptions import ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# S3 error codes → HTTP status + client-safe message
_S3_ERROR_MAP: dict[str, tuple[int, str]] = {
    "InvalidPart": (
        422,
        "One or more ETags do not match the uploaded parts — "
        "re-upload the affected parts and retry",
    ),
    "NoSuchUpload": (
        404,
        "Upload session not found — it may have expired or already been completed",
    ),
    "NoSuchKey": (404, "Resource not found"),
    "InvalidRange": (416, "Requested range not satisfiable"),
    "AccessDenied": (403, "Access denied"),
}

logger = logging.getLogger("app.errors")


def _envelope(status_code: int, error: str) -> JSONResponse:
    """Build an error response in the shared {success, data, error} shape."""
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "data": None, "error": error},
    )


def _format_validation_errors(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        # Drop the leading "body" segment; keep field path otherwise.
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Validation error: " + "; ".join(parts)


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return _envelope(422, _format_validation_errors(exc))


async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return _envelope(exc.status_code, detail)


async def _client_error_handler(request: Request, exc: ClientError) -> JSONResponse:
    code = exc.response.get("Error", {}).get("Code", "")
    if code in _S3_ERROR_MAP:
        status_code, message = _S3_ERROR_MAP[code]
        return _envelope(status_code, message)
    logger.exception("Unhandled S3 ClientError on %s %s", request.method, request.url.path)
    return _envelope(500, "Internal server error")


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log full context server-side; never leak internals to the client.
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _envelope(500, "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers so every error uses the {success, data, error} envelope."""
    # Starlette types handlers as accepting base Exception; specific subclasses
    # are correct at runtime (standard FastAPI pattern).
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ClientError, _client_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)  # type: ignore[arg-type]
