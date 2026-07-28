"""
Los modelos SQLAlchemy no deben separarse del esquema real.

La fuente de verdad son las migraciones. Si alguien agrega una columna en una
migracion y olvida el modelo (o al reves), estos tests lo detectan antes de que
aparezca como un error raro en runtime.
"""

from __future__ import annotations

import pytest
from sqlalchemy import DateTime, inspect
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import Base
from tests.conftest import requires_db

pytestmark = [requires_db]

MODEL_TABLES = sorted(Base.metadata.tables)


async def test_todas_las_tablas_del_modelo_existen_en_la_db(conn: AsyncConnection) -> None:
    real = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    faltantes = set(MODEL_TABLES) - real
    assert not faltantes, f"tablas declaradas en models.py que no existen en la DB: {faltantes}"


async def test_el_ddl_no_tiene_tablas_sin_modelo(conn: AsyncConnection) -> None:
    """Al reves: una tabla migrada sin modelo es capa de datos incompleta."""
    real = await conn.run_sync(lambda sync_conn: set(inspect(sync_conn).get_table_names()))

    # alembic_version es de la herramienta de migracion, no del dominio.
    sin_modelo = real - set(MODEL_TABLES) - {"alembic_version"}
    assert not sin_modelo, f"tablas en la DB sin modelo en models.py: {sin_modelo}"


@pytest.mark.parametrize("table_name", MODEL_TABLES)
async def test_las_columnas_coinciden(conn: AsyncConnection, table_name: str) -> None:
    """Mismo conjunto de columnas en el modelo y en la tabla real."""
    real_cols = await conn.run_sync(
        lambda sync_conn: {c["name"] for c in inspect(sync_conn).get_columns(table_name)}
    )
    model_cols = {c.name for c in Base.metadata.tables[table_name].columns}

    assert model_cols == real_cols, (
        f"{table_name}: solo en el modelo={model_cols - real_cols}, "
        f"solo en la DB={real_cols - model_cols}"
    )


@pytest.mark.parametrize("table_name", MODEL_TABLES)
async def test_la_nulabilidad_coincide(conn: AsyncConnection, table_name: str) -> None:
    """
    Una columna NOT NULL en la DB pero opcional en el modelo (o al reves) produce
    errores de integridad recien al escribir.
    """
    real = await conn.run_sync(
        lambda sync_conn: {
            c["name"]: c["nullable"] for c in inspect(sync_conn).get_columns(table_name)
        }
    )
    modelo = {c.name: c.nullable for c in Base.metadata.tables[table_name].columns}

    discrepancias = {
        name: (modelo[name], real[name])
        for name in modelo.keys() & real.keys()
        if modelo[name] != real[name]
    }
    assert not discrepancias, (
        f"{table_name}: nulabilidad distinta (modelo, db) -> {discrepancias}"
    )


@pytest.mark.parametrize("table_name", MODEL_TABLES)
async def test_las_fechas_son_timestamptz(conn: AsyncConnection, table_name: str) -> None:
    """
    Todas las fechas del DDL son `timestamptz`. Si el modelo declara un DateTime
    sin `timezone=True`, SQLAlchemy genera TIMESTAMP WITHOUT TIME ZONE y asyncpg
    rechaza los datetime aware que usa la app.
    """
    real = await conn.run_sync(
        lambda sync_conn: {
            c["name"]: c["type"] for c in inspect(sync_conn).get_columns(table_name)
        }
    )
    fechas = {
        name: tipo
        for name, tipo in real.items()
        if isinstance(tipo, DateTime)
    }

    sin_tz = [name for name, tipo in fechas.items() if not tipo.timezone]
    assert not sin_tz, f"{table_name}: columnas de fecha sin timezone en la DB: {sin_tz}"

    for name in fechas:
        col = Base.metadata.tables[table_name].columns[name]
        assert isinstance(col.type, DateTime) and col.type.timezone, (
            f"{table_name}.{name}: el modelo debe declarar DateTime(timezone=True)"
        )


async def test_toda_tabla_de_dominio_tiene_business_id(conn: AsyncConnection) -> None:
    """
    RF-24: todo dato pertenece a un tenant. `businesses` es la excepcion (su PK
    ES el tenant).
    """
    for table_name in MODEL_TABLES:
        cols = {c.name for c in Base.metadata.tables[table_name].columns}
        if table_name == "businesses":
            assert "id" in cols
            continue
        assert "business_id" in cols, f"{table_name} no tiene business_id (RF-24)"
