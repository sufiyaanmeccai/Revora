"""
app/main.py
-----------
Application entry-point for the Revora Revenue Recovery Engine.

Responsibilities:
  • Instantiate the FastAPI application with OpenAPI metadata.
  • Register CORS middleware permissive for local development.
  • Include the versioned API router.
  • Expose startup / shutdown lifecycle hooks for future resource management.
"""

import logging

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.database import engine, init_db

# --------------------------------------------------------------------------- #
# Logging                                                                      #
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Lifecycle hooks                                                               #
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown side-effects."""
    logger.info("🚀  Revora engine starting up…")
    await init_db()
    yield
    logger.info("🛑  Revora engine shutting down…")
    await engine.dispose()
    logger.info("🗄️   Database engine disposed.")


# --------------------------------------------------------------------------- #
# FastAPI application                                                          #
# --------------------------------------------------------------------------- #
app = FastAPI(
    title=settings.PROJECT_NAME,
    description=(
        "Autonomous AI Revenue Recovery Engine for subscription and recurring "
        "payment failures — powered by Razorpay (Buildathon Track 03)."
    ),
    version="1.0.0",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------- #
# Middleware                                                                   #
# --------------------------------------------------------------------------- #
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Routers                                                                      #
# --------------------------------------------------------------------------- #
app.include_router(api_router, prefix=settings.API_V1_STR)


# --------------------------------------------------------------------------- #
# Root redirect                                                                #
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": settings.PROJECT_NAME,
        "version": "1.0.0",
        "docs": "/docs",
    }
