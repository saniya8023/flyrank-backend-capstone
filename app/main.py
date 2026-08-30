"""
FastAPI application entrypoint.

Global exception handling policy: unhandled exceptions from the
service layer should never leak as a bare 500 with a stack trace to
the client. `RequestValidationError` (Pydantic/body validation) maps
to a structured 422; anything truly unexpected still returns 500 but
with the same structured error envelope, and is logged server-side.
"""
import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.routers import quota, tenants, usage, webhooks

logger = logging.getLogger("billing_engine")
settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="Usage Metering & Billing Engine — metering, quota enforcement, and pricing for SaaS APIs.",
)

app.include_router(tenants.router)
app.include_router(usage.router)
app.include_router(quota.router)
app.include_router(webhooks.router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed.",
                "details": exc.errors(),
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
            }
        },
    )


@app.get("/health", tags=["health"])
async def health_check():
    return {"status": "ok", "service": settings.APP_NAME}
