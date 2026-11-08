import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.errors import DEFAULT_CODES, ApiError, error_body
from app.dependencies import get_session_factory
from app.routers import auth, documents
from app.services.processing import mark_stuck_documents

# Show INFO logs from our own modules
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the app starts. It uses the same session factory as the
    # routes, so a test's override applies here too.
    session_factory = app.dependency_overrides.get(get_session_factory, get_session_factory)()
    try:
        mark_stuck_documents(session_factory, settings.processing_timeout_minutes)
    except Exception as error:
        # A housekeeping step: log it and start anyway
        logger.error("Startup check for stuck documents failed (%s)", type(error).__name__)
    yield


app = FastAPI(
    title="Pinpage",
    description="Upload PDFs, then read them page by page. Every document belongs to a workspace, "
    "and only its members can reach it.",
    lifespan=lifespan,
)


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


@app.get("/health", tags=["health"], summary="Health check")
def health():
    """Answers when the API is running. Doesn't contact the database or storage."""
    return {"status": "ok", "service": "pinpage"}
