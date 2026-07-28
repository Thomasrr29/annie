"""Punto de entrada de la API: app FastAPI + routers + healthcheck."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api.admin import router as admin_router
from app.api.webhooks import router as webhooks_router
from app.config import settings
from app.core.logging_config import configure_logging
from app.db.session import check_database, dispose_engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ciclo de vida: el pool se crea perezosamente y se cierra al apagar."""
    # JSON en producción (para agregadores); texto plano en desarrollo.
    configure_logging(level=settings.log_level, json_output=not settings.debug)
    yield
    await dispose_engine()


app = FastAPI(
    title="Agente de Soporte — MVP",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)


app.include_router(webhooks_router)
app.include_router(admin_router)


@app.get("/health", tags=["infra"])
async def health() -> JSONResponse:
    """
    Salud del servicio. 200 con la DB arriba, 503 si la DB no responde
    (criterio de aceptación de la fase 1).
    """
    db_ok = await check_database()
    payload = {
        "status": "ok" if db_ok else "degraded",
        "env": settings.env,
        "database": "up" if db_ok else "down",
    }
    return JSONResponse(content=payload, status_code=200 if db_ok else 503)
