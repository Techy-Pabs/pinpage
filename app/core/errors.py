"""One error shape everywhere: {"error": {"code": "...", "message": "..."}}.

Routes raise ApiError(status, code, message). Errors raised by FastAPI or
Starlette themselves (404 for an unknown path, 405, 422 validation) are
turned into the same shape by the handlers in app.main.
"""

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


# Codes for errors that don't come from our own routes
DEFAULT_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
}
