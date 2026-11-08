import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import DEFAULT_CODES, ApiError, error_body
from app.routers import auth, documents

# Show INFO logs from our own modules
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

app = FastAPI(title="Pinpage")


@app.exception_handler(StarletteHTTPException)
def http_error_handler(request: Request, exc: StarletteHTTPException):
    # Our own ApiError carries a code; errors raised by FastAPI or Starlette
    # (unknown path, wrong method) get a code from their status
    code = exc.code if isinstance(exc, ApiError) else DEFAULT_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    extra = exc.extra if isinstance(exc, ApiError) else None
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(error_body(code, message, extra)),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
def validation_error_handler(request: Request, exc: RequestValidationError):
    # FastAPI's default 422 response echoes back the invalid value, which
    # could be a password. Send the error details without the "input".
    errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
    body = error_body("validation_error", "The request is not valid")
    body["error"]["details"] = jsonable_encoder(errors)
    return JSONResponse(status_code=422, content=body)


@app.exception_handler(Exception)
def unexpected_error_handler(request: Request, exc: Exception):
    # Anything we didn't plan for. The details go to the server log (Starlette
    # logs the traceback after this runs); the response never contains
    # exception text, file paths or SQL.
    return JSONResponse(
        status_code=500,
        content=error_body("internal_error", "Something went wrong on our side. Please try again."),
    )


app.include_router(auth.router)
app.include_router(documents.router)


# Visiting the base URL opens the Swagger page. Hidden from the docs themselves.
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    return {"status": "ok", "service": "pinpage"}
