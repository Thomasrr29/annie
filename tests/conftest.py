"""Fixtures compartidas de la suite."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import text


def _resolve_test_database_url() -> str | None:
    """
    URL de la DB de pruebas: variable de entorno y, si no, el .env del proyecto.

    Leer tambien el .env importa: es lo que usa la app, asi que sin esto un
    `pytest -q` local se saltaria los tests de aislamiento en silencio aunque la
    base este configurada y corriendo.
    """
    if explicit := (os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")):
        return explicit
    try:
        from app.config import settings

        return settings.database_url
    except Exception:
        # .env incompleto (faltan secretos): se cae al salto normal.
        return None


# Sin URL, los tests que necesitan Postgres se saltan para no bloquear el trabajo
# local sin la base levantada.
TEST_DATABASE_URL = _resolve_test_database_url()

# En CI saltarse los tests de aislamiento equivale a no probarlo: un descuido en
# la configuracion dejaria el pipeline en verde sin haber verificado RLS, que es
# el criterio de aceptacion de la fase 1. Ahi el salto es un error.
_IN_CI = os.getenv("CI", "").lower() in {"1", "true", "yes"}

if _IN_CI and not TEST_DATABASE_URL:
    raise RuntimeError(
        "CI sin DATABASE_URL: los tests de aislamiento RLS no correrian. "
        "Configurar DATABASE_URL apuntando a la Postgres del pipeline."
    )

@pytest.fixture(autouse=True)
def _sin_espera_de_rafaga(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    En los tests el turno no espera los 10 s de ráfaga (invariante I2): cada
    test que quiera probar la espera la activa con un valor chico.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "turn_debounce_seconds", 0.0)


requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Sin TEST_DATABASE_URL/DATABASE_URL: se requiere Postgres con las migraciones aplicadas",
)


@pytest.fixture(autouse=True)
def _rate_limits_limpios() -> None:
    """
    El rate limiter guarda estado en memoria del proceso. Sin limpiarlo, los
    mensajes de un test cuentan contra la ventana del siguiente y aparecen
    fallos que solo se dan al correr la suite completa.
    """
    from app.core.limits import reset_rate_limits

    reset_rate_limits()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """
    Engine contra la DB de pruebas.

    De alcance por test y con NullPool a proposito: pytest-asyncio crea un event
    loop nuevo por test, y un engine con pool compartido entre loops falla al
    cerrar ("Event loop is closed"). Sin pool, cada test abre y cierra su propia
    conexion dentro de su loop.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("Sin base de datos de pruebas configurada")
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def conn(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """
    Conexion con transaccion que SIEMPRE hace rollback: los tests no dejan
    residuo en la base.
    """
    async with engine.connect() as connection:
        trans = await connection.begin()
        try:
            yield connection
        finally:
            await trans.rollback()


def _test_role_url() -> str | None:
    """
    URL con el rol de pruebas (BYPASSRLS) derivada de la URL normal.

    Se sustituye usuario y contrasena manteniendo host/puerto/base, para que
    funcione igual en local y en CI sin una variable extra.
    """
    if not TEST_DATABASE_URL:
        return None
    if override := os.getenv("TEST_ROLE_DATABASE_URL"):
        return override

    parsed = urlsplit(TEST_DATABASE_URL)
    if not parsed.hostname:
        return None
    password = os.getenv("TEST_DB_PASSWORD", "test_password_dev")
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"app_agente_test:{password}@{parsed.hostname}{port}"
    return urlunsplit(parsed._replace(netloc=netloc))


@pytest_asyncio.fixture
async def bypass_engine() -> AsyncIterator[AsyncEngine]:
    """Engine con el rol de pruebas que omite RLS (migracion 0004)."""
    url = _test_role_url()
    if not url:
        pytest.skip("Sin base de datos de pruebas configurada")
    eng = create_async_engine(url, poolclass=NullPool)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session(bypass_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """
    AsyncSession para los tests de repositorio, con rollback al final.

    Corre con `app_agente_test`, que OMITE RLS, y no fija
    `app.current_business_id`. Es deliberado: estos tests ejercitan el filtrado
    por `business_id` de los repositorios (capa 1). Con RLS activo, la capa 2
    taparia un repositorio que olvidara filtrar y el test pasaria sin probar
    nada. Sin bypass, una fuga de la capa 1 se ve como fuga.

    El aislamiento por RLS (capa 2) se prueba aparte con el rol sin privilegios,
    en `test_rls_aislamiento.py`.
    """
    async with AsyncSession(bypass_engine, expire_on_commit=False) as s:
        await s.begin()
        try:
            yield s
        finally:
            await s.rollback()


@pytest_asyncio.fixture
async def business_a(session: AsyncSession) -> UUID:
    """Negocio de pruebas A."""
    bid = uuid4()
    await session.execute(
        text("INSERT INTO businesses (id, name) VALUES (:id, 'Negocio A')"), {"id": bid}
    )
    await session.flush()
    return bid


@pytest_asyncio.fixture
async def business_b(session: AsyncSession) -> UUID:
    """Negocio de pruebas B: el 'otro tenant' de los tests de aislamiento."""
    bid = uuid4()
    await session.execute(
        text("INSERT INTO businesses (id, name) VALUES (:id, 'Negocio B')"), {"id": bid}
    )
    await session.flush()
    return bid


@pytest_asyncio.fixture
async def seed_knowledge(session: AsyncSession, business_a: UUID) -> None:
    """Dos documentos de conocimiento para el negocio A."""
    for titulo, contenido in (
        ("Horarios", "Lunes a viernes de 8am a 6pm."),
        ("Servicios", "Corte, tinte y peinado."),
    ):
        await session.execute(
            text("""
                INSERT INTO knowledge_documents (business_id, title, raw_content, source)
                VALUES (:bid, :titulo, :contenido, 'faq')
            """),
            {"bid": business_a, "titulo": titulo, "contenido": contenido},
        )
    await session.flush()


@pytest_asyncio.fixture
async def two_tenants(conn: AsyncConnection) -> tuple[UUID, UUID]:
    """
    Crea dos negocios con datos propios para probar el aislamiento.

    El montaje corre con el mismo rol sin privilegios que la app: por eso cada
    bloque de inserciones fija antes su propio tenant, como exige WITH CHECK.
    Todo ocurre dentro de la transaccion de `conn`, que hace rollback al final.
    """
    business_a, business_b = uuid4(), uuid4()

    for bid, name in ((business_a, "Negocio A"), (business_b, "Negocio B")):
        # Fijamos el tenant antes de insertar: WITH CHECK de la politica lo exige.
        await conn.execute(
            text("SELECT set_config('app.current_business_id', :bid, true)"),
            {"bid": str(bid)},
        )
        await conn.execute(
            text("INSERT INTO businesses (id, name) VALUES (:id, :name)"),
            {"id": bid, "name": name},
        )
        customer_id = uuid4()
        await conn.execute(
            text("""
                INSERT INTO customers (id, business_id, channel, channel_identifier)
                VALUES (:id, :bid, 'whatsapp', :ident)
            """),
            {"id": customer_id, "bid": bid, "ident": f"+57300{str(bid)[:7]}"},
        )
        conversation_id = uuid4()
        await conn.execute(
            text("""
                INSERT INTO conversations (id, business_id, customer_id)
                VALUES (:id, :bid, :cid)
            """),
            {"id": conversation_id, "bid": bid, "cid": customer_id},
        )
        await conn.execute(
            text("""
                INSERT INTO messages (conversation_id, business_id, role, content)
                VALUES (:conv, :bid, 'customer', :content)
            """),
            {"conv": conversation_id, "bid": bid, "content": f"mensaje privado de {name}"},
        )

    return business_a, business_b
