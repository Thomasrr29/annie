"""
Engine async + gestión del contexto de tenant para Row-Level Security.

Capa 2 del aislamiento multi-tenant (RNF-04): toda transacción que toque datos
de un negocio debe fijar `app.current_business_id` con `SET LOCAL`, de modo que
las políticas RLS de Postgres filtren aunque una consulta olvide el WHERE.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text

from app.config import settings


def _crear_engine() -> AsyncEngine:
    """
    Engine async del proceso.

    Bajo pytest se usa NullPool: cada test corre en su propio event loop, y una
    conexion del pool creada en un loop ya cerrado revienta con
    "attached to a different loop". Sin pool, cada operacion abre y cierra su
    conexion en el loop vigente. En produccion el pool normal si conviene.
    """
    if os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
        return create_async_engine(
            settings.database_url, poolclass=NullPool, echo=settings.debug
        )

    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_pre_ping=True,
        echo=settings.debug,
    )


_engine: AsyncEngine = _crear_engine()

_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine,
    expire_on_commit=False,
    autoflush=False,
)


def get_engine() -> AsyncEngine:
    """Engine async compartido del proceso."""
    return _engine


async def dispose_engine() -> None:
    """Cierra el pool. Se llama en el shutdown de la app."""
    await _engine.dispose()


async def set_tenant(session: AsyncSession, business_id: UUID) -> None:
    """
    Fija el tenant de la transacción actual para RLS.

    `SET LOCAL` vive hasta el fin de la transacción, así que no se filtra a otra
    conexión del pool. El uuid va como parámetro y luego a `set_config` para no
    concatenar valores en el SQL.
    """
    await session.execute(
        text("SELECT set_config('app.current_business_id', :bid, true)"),
        {"bid": str(business_id)},
    )


@asynccontextmanager
async def tenant_session(business_id: UUID) -> AsyncIterator[AsyncSession]:
    """
    Sesión async con el contexto de tenant fijado y transacción abierta.

    Uso:
        async with tenant_session(business_id) as session:
            ...  # toda consulta queda acotada al tenant por RLS

    Hace commit al salir sin excepción; rollback si algo falla.
    """
    async with _session_factory() as session, session.begin():
        await set_tenant(session, business_id)
        yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """
    Sesión SIN contexto de tenant, para operaciones que preceden a la resolución
    del negocio (p. ej. mapear un número de WhatsApp -> business_id) o tareas de
    plataforma. Usar con moderación: aquí RLS no protege.
    """
    async with _session_factory() as session, session.begin():
        yield session


async def check_database() -> bool:
    """Ping de salud de la DB para `GET /health`."""
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
