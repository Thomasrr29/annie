"""
Limites de proteccion y privacidad (fase 7).

Criterios de aceptacion:
  - un flood no genera costo descontrolado;
  - la eliminacion de datos borra de verdad y deja evidencia;
  - el logging no filtra contenido de mensajes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import text

from app.core.limits import (
    MENSAJES_POR_VENTANA,
    check_daily_limit,
    check_rate_limit,
    eliminar_datos_cliente,
    hash_identificador,
    marcar_aviso_enviado,
    necesita_aviso_privacidad,
)
from app.core.logging_config import JsonFormatter
from app.db.session import set_tenant, system_session
from tests.conftest import requires_db


# ======================================================================
# Rate limit por cliente (sin base de datos)
# ======================================================================
def test_los_primeros_mensajes_pasan() -> None:
    business_id = uuid4()

    for _ in range(MENSAJES_POR_VENTANA):
        assert check_rate_limit(business_id, "+573001112233") is True


def test_al_exceder_la_ventana_se_corta() -> None:
    """Un flood no debe generar un turno con el modelo por cada mensaje."""
    business_id = uuid4()
    for _ in range(MENSAJES_POR_VENTANA):
        check_rate_limit(business_id, "+573001112233")

    assert check_rate_limit(business_id, "+573001112233") is False


def test_el_limite_es_por_cliente_no_global() -> None:
    """Un cliente abusivo no puede dejar sin servicio a los demás."""
    business_id = uuid4()
    for _ in range(MENSAJES_POR_VENTANA + 5):
        check_rate_limit(business_id, "+573001112233")

    assert check_rate_limit(business_id, "+573009998877") is True


def test_el_limite_no_cruza_negocios() -> None:
    a, b = uuid4(), uuid4()
    for _ in range(MENSAJES_POR_VENTANA + 5):
        check_rate_limit(a, "+573001112233")

    assert check_rate_limit(b, "+573001112233") is True


# ======================================================================
# Logging estructurado (RNF-07)
# ======================================================================
def test_el_log_sale_como_json() -> None:
    registro = logging.LogRecord(
        "test", logging.INFO, "f.py", 1, "algo pasó", None, None
    )
    salida = json.loads(JsonFormatter().format(registro))

    assert salida["level"] == "INFO"
    assert salida["msg"] == "algo pasó"


def test_el_log_incluye_el_contexto_de_tenant() -> None:
    registro = logging.LogRecord("test", logging.INFO, "f.py", 1, "turno", None, None)
    registro.business_id = "abc-123"
    registro.conversation_id = "conv-9"

    salida = json.loads(JsonFormatter().format(registro))

    assert salida["business_id"] == "abc-123"
    assert salida["conversation_id"] == "conv-9"


def test_el_log_nunca_lleva_contenido_de_mensajes() -> None:
    """
    El contenido vive en la base, bajo RLS y con política de retención. Copiarlo
    al log lo saca de ese perímetro (Ley 1581).
    """
    registro = logging.LogRecord("test", logging.INFO, "f.py", 1, "turno", None, None)
    registro.text = "mi número de tarjeta es 4111 1111 1111 1111"
    registro.reply_text = "respuesta con datos personales"

    salida = json.loads(JsonFormatter().format(registro))

    assert salida["text"] == "[omitido]"
    assert salida["reply_text"] == "[omitido]"
    assert "4111" not in json.dumps(salida)


def test_hash_del_identificador_es_estable_y_no_reversible() -> None:
    h1 = hash_identificador("+573001112233")
    h2 = hash_identificador("+573001112233")

    assert h1 == h2
    assert "573001112233" not in h1
    assert len(h1) == 64


# ======================================================================
# Con base de datos
# ======================================================================
pytestmark_db = requires_db


@pytest_asyncio.fixture
async def negocio_con_cliente() -> AsyncIterator[tuple[UUID, str]]:
    """Negocio con un cliente que tiene conversación, mensajes y solicitudes."""
    business_id = uuid4()
    telefono = "+573001234567"

    async with system_session() as s:
        await set_tenant(s, business_id)
        await s.execute(
            text("INSERT INTO businesses (id, name) VALUES (:i, 'Privacidad')"),
            {"i": business_id},
        )
        customer_id = (
            await s.execute(
                text("""
                    INSERT INTO customers (business_id, channel, channel_identifier)
                    VALUES (:b, 'whatsapp', :t) RETURNING id
                """),
                {"b": business_id, "t": telefono},
            )
        ).scalar_one()
        conv_id = (
            await s.execute(
                text("""
                    INSERT INTO conversations (business_id, customer_id)
                    VALUES (:b, :c) RETURNING id
                """),
                {"b": business_id, "c": customer_id},
            )
        ).scalar_one()
        for i in range(3):
            await s.execute(
                text("""
                    INSERT INTO messages (conversation_id, business_id, role, content)
                    VALUES (:cv, :b, 'customer', :t)
                """),
                {"cv": conv_id, "b": business_id, "t": f"mensaje {i}"},
            )
        await s.execute(
            text("""
                INSERT INTO captured_requests
                  (business_id, customer_id, conversation_id, request_type)
                VALUES (:b, :c, :cv, 'agenda')
            """),
            {"b": business_id, "c": customer_id, "cv": conv_id},
        )

    try:
        yield business_id, telefono
    finally:
        async with system_session() as s:
            await set_tenant(s, business_id)
            await s.execute(
                text("DELETE FROM businesses WHERE id = :i"), {"i": business_id}
            )


@requires_db
async def test_la_eliminacion_borra_los_datos_del_cliente(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    business_id, telefono = negocio_con_cliente

    resultado = await eliminar_datos_cliente(business_id, telefono, "titular")

    assert resultado["found"] == 1
    assert resultado["messages_deleted"] == 3
    assert resultado["requests_deleted"] == 1

    async with system_session() as s:
        await set_tenant(s, business_id)
        quedan = (
            await s.execute(
                text("SELECT count(*) FROM customers WHERE business_id = :b"),
                {"b": business_id},
            )
        ).scalar_one()
        mensajes = (
            await s.execute(
                text("SELECT count(*) FROM messages WHERE business_id = :b"),
                {"b": business_id},
            )
        ).scalar_one()

    assert quedan == 0
    assert mensajes == 0, "los mensajes debieron borrarse en cascada"


@requires_db
async def test_la_eliminacion_deja_evidencia_sin_el_dato_personal(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    """
    La evidencia debe probar el cumplimiento sin conservar el teléfono: si
    guardáramos el número, el borrado no sería tal.
    """
    business_id, telefono = negocio_con_cliente

    await eliminar_datos_cliente(business_id, telefono, "titular")

    async with system_session() as s:
        await set_tenant(s, business_id)
        fila = (
            await s.execute(
                text(
                    "SELECT identifier_hash, messages_deleted, requested_by "
                    "FROM data_deletion_log WHERE business_id = :b"
                ),
                {"b": business_id},
            )
        ).one()

    assert fila.identifier_hash == hash_identificador(telefono)
    assert telefono not in fila.identifier_hash
    assert fila.messages_deleted == 3
    assert fila.requested_by == "titular"


@requires_db
async def test_eliminar_un_cliente_inexistente_no_falla(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    business_id, _ = negocio_con_cliente

    resultado = await eliminar_datos_cliente(business_id, "+570000000000")

    assert resultado["found"] == 0


@requires_db
async def test_el_aviso_de_privacidad_se_manda_una_sola_vez(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    """RNF-05: aviso en el primer contacto, no en cada mensaje."""
    business_id, telefono = negocio_con_cliente

    async with system_session() as s:
        await set_tenant(s, business_id)
        customer_id = (
            await s.execute(
                text("SELECT id FROM customers WHERE business_id = :b"), {"b": business_id}
            )
        ).scalar_one()

    assert await necesita_aviso_privacidad(business_id, customer_id) is True

    await marcar_aviso_enviado(business_id, customer_id)

    assert await necesita_aviso_privacidad(business_id, customer_id) is False


@requires_db
async def test_el_tope_diario_permite_operar_normalmente(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    business_id, _ = negocio_con_cliente

    assert await check_daily_limit(business_id) is True


@requires_db
async def test_el_tope_diario_corta_al_alcanzarse(
    negocio_con_cliente: tuple[UUID, str]
) -> None:
    """Circuit breaker de costo: sin esto un bucle genera una factura enorme."""
    business_id, _ = negocio_con_cliente

    async with system_session() as s:
        await set_tenant(s, business_id)
        await s.execute(
            text("UPDATE businesses SET daily_turn_limit = 2 WHERE id = :b"),
            {"b": business_id},
        )
        conv_id = (
            await s.execute(
                text("SELECT id FROM conversations WHERE business_id = :b"),
                {"b": business_id},
            )
        ).scalar_one()
        for _ in range(2):
            await s.execute(
                text("""
                    INSERT INTO turn_logs (business_id, conversation_id, detected_intent)
                    VALUES (:b, :c, 'inform')
                """),
                {"b": business_id, "c": conv_id},
            )

    assert await check_daily_limit(business_id) is False
