"""Entorno Alembic en modo async (asyncpg)."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.config import settings

config = context.config

# Las migraciones necesitan mas privilegios que la app: crean extensiones
# (pgcrypto, vector) y el rol app_agente. La app, en cambio, corre con un rol sin
# BYPASSRLS para que RLS aplique. ADMIN_DATABASE_URL permite esa separacion;
# si no esta, se usa DATABASE_URL (comodo en local, donde suele ser el admin).
MIGRATION_URL = os.getenv("ADMIN_DATABASE_URL") or settings.database_url

config.set_main_option("sqlalchemy.url", MIGRATION_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Fase 1: la migración inicial es DDL explícito (no autogenerado).
# Al llegar la fase 2 (app/db/models.py) esto pasa a Base.metadata.
target_metadata = None


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse (alembic upgrade head --sql)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
