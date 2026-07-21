"""FastAPI application entry point (uvicorn app.main:app)."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from app.api import (
    analytics,
    auth,
    copytrading,
    health,
    reports,
    stats,
    system,
    tokens,
    trades,
    wallets,
    ws,
)
from app.config import get_settings
from app.db.session import build_engine, build_session_factory
from app.logging_config import configure_logging, get_logger
from app.services.redis import create_redis

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Engine/clients are lazy: nothing connects until first use, so the API
    # boots even while the database is still coming up (readiness reports it).
    engine = build_engine(settings)
    app.state.engine = engine
    app.state.session_factory = build_session_factory(engine)
    app.state.redis = create_redis(settings.redis_url)
    log.info("api_started", env=settings.app_env)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    app = FastAPI(
        title="Solana Copytrader Intelligence API",
        description="Phase 1: on-chain data collection and learning database",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(wallets.router)
    app.include_router(tokens.router)
    app.include_router(trades.router)
    app.include_router(stats.router)
    app.include_router(analytics.router)
    app.include_router(copytrading.router)
    app.include_router(reports.router)
    app.include_router(auth.router)
    app.include_router(system.router)
    app.include_router(ws.router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
