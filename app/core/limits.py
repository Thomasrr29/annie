"""
Limites de proteccion y privacidad (fase 7).

Dos protecciones de costo (riesgo #1 del documento de alcance):

  - **Rate limit por cliente**: un cliente que manda 50 mensajes en un minuto no
    debe generar 50 turnos con el modelo.
  - **Tope diario por tenant**: circuit breaker. Sin esto, un bucle o un abuso
    puede generar una factura enorme antes de que nadie lo note.

Y el aviso de tratamiento de datos (RNF-05, Ley 1581), que se manda una sola vez
por cliente en el primer contacto.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from collections import defaultdict, deque
from typing import Final
from uuid import UUID

from sqlalchemy import text

from app.db.session import tenant_session

logger = logging.getLogger(__name__)

# Rate limit por cliente final.
MENSAJES_POR_VENTANA: Final = 10
VENTANA_SEGUNDOS: Final = 60

# Tope diario de turnos por tenant si el negocio no configuro uno propio.
LIMITE_DIARIO_POR_DEFECTO: Final = 1000

RESPUESTA_RATE_LIMIT: Final = (
    "Estás escribiendo muy rápido y no llego a procesarlo todo. "
    "Dame un momento y volvé a intentar."
)
RESPUESTA_LIMITE_DIARIO: Final = (
    "En este momento no puedo atenderte automáticamente. "
    "Le paso tu mensaje a una persona del negocio."
)

AVISO_PRIVACIDAD: Final = (
    "Este es un canal de atención automatizado. Guardamos tu número y los "
    "mensajes de esta conversación para poder atenderte, conforme a la Ley 1581 "
    "de 2012. Podés pedir la consulta o eliminación de tus datos escribiendo "
    "\"eliminar mis datos\"."
)

# Ventana deslizante en memoria: {(business_id, identificador): [timestamps]}.
# En memoria a proposito: el MVP corre en una instancia (RNF-03) y esto es
# proteccion de costo, no seguridad. Si se escala a varias instancias, esto pasa
# a Redis o a la base.
_ventanas: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def check_rate_limit(business_id: UUID, channel_identifier: str) -> bool:
    """
    True si el cliente puede seguir enviando; False si excedio la ventana.

    Cuenta hacia atras desde ahora y descarta lo viejo, asi no hace falta
    limpiar el diccionario por separado.
    """
    clave = (str(business_id), channel_identifier)
    ahora = time.monotonic()
    ventana = _ventanas[clave]

    while ventana and ahora - ventana[0] > VENTANA_SEGUNDOS:
        ventana.popleft()

    if len(ventana) >= MENSAJES_POR_VENTANA:
        logger.warning(
            "rate limit alcanzado", extra={"business_id": str(business_id)}
        )
        return False

    ventana.append(ahora)
    return True


def reset_rate_limits() -> None:
    """Limpia el estado. Solo para tests."""
    _ventanas.clear()


async def check_daily_limit(business_id: UUID) -> bool:
    """
    True si el tenant todavia tiene presupuesto de turnos hoy.

    Circuit breaker de costo: cuenta los turnos de las ultimas 24h contra
    `businesses.daily_turn_limit`.
    """
    desde = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

    async with tenant_session(business_id) as session:
        limite = (
            await session.execute(
                text("SELECT daily_turn_limit FROM businesses WHERE id = :b"),
                {"b": business_id},
            )
        ).scalar_one_or_none()

        tope = int(limite) if limite else LIMITE_DIARIO_POR_DEFECTO

        usados = (
            await session.execute(
                text(
                    "SELECT count(*) FROM turn_logs "
                    "WHERE business_id = :b AND created_at >= :d"
                ),
                {"b": business_id, "d": desde},
            )
        ).scalar_one()

    if int(usados) >= tope:
        logger.error(
            "tope diario de turnos alcanzado",
            extra={"business_id": str(business_id), "usados": int(usados), "tope": tope},
        )
        return False

    return True


async def necesita_aviso_privacidad(business_id: UUID, customer_id: UUID) -> bool:
    """True si a este cliente todavia no se le envio el aviso (RNF-05)."""
    async with tenant_session(business_id) as session:
        enviado = (
            await session.execute(
                text(
                    "SELECT privacy_notice_sent_at FROM customers "
                    "WHERE id = :c AND business_id = :b"
                ),
                {"c": customer_id, "b": business_id},
            )
        ).scalar_one_or_none()
    return enviado is None


async def marcar_aviso_enviado(business_id: UUID, customer_id: UUID) -> None:
    async with tenant_session(business_id) as session:
        await session.execute(
            text(
                "UPDATE customers SET privacy_notice_sent_at = now() "
                "WHERE id = :c AND business_id = :b"
            ),
            {"c": customer_id, "b": business_id},
        )


def hash_identificador(identificador: str) -> str:
    """Hash del telefono para la evidencia de borrado, sin guardar el dato."""
    return hashlib.sha256(identificador.encode("utf-8")).hexdigest()


async def eliminar_datos_cliente(
    business_id: UUID, channel_identifier: str, requested_by: str | None = None
) -> dict[str, int]:
    """
    Borra los datos de un cliente por solicitud del titular (Ley 1581).

    Deja evidencia en `data_deletion_log` con el HASH del identificador: sirve
    como prueba de cumplimiento sin conservar el dato personal.

    El borrado del `customer` arrastra en cascada conversaciones, mensajes,
    memoria y solicitudes.
    """
    async with tenant_session(business_id) as session:
        customer_id = (
            await session.execute(
                text(
                    "SELECT id FROM customers "
                    "WHERE business_id = :b AND channel_identifier = :i"
                ),
                {"b": business_id, "i": channel_identifier},
            )
        ).scalar_one_or_none()

        if customer_id is None:
            return {"messages_deleted": 0, "requests_deleted": 0, "found": 0}

        mensajes = (
            await session.execute(
                text(
                    "SELECT count(*) FROM messages m "
                    "JOIN conversations c ON c.id = m.conversation_id "
                    "WHERE c.customer_id = :c AND m.business_id = :b"
                ),
                {"c": customer_id, "b": business_id},
            )
        ).scalar_one()

        solicitudes = (
            await session.execute(
                text(
                    "SELECT count(*) FROM captured_requests "
                    "WHERE customer_id = :c AND business_id = :b"
                ),
                {"c": customer_id, "b": business_id},
            )
        ).scalar_one()

        # ON DELETE CASCADE se lleva conversaciones, mensajes, memoria y
        # solicitudes de este cliente.
        await session.execute(
            text("DELETE FROM customers WHERE id = :c AND business_id = :b"),
            {"c": customer_id, "b": business_id},
        )

        await session.execute(
            text("""
                INSERT INTO data_deletion_log
                  (business_id, identifier_hash, messages_deleted, requests_deleted,
                   requested_by)
                VALUES (:b, :h, :m, :r, :who)
            """),
            {
                "b": business_id,
                "h": hash_identificador(channel_identifier),
                "m": int(mensajes),
                "r": int(solicitudes),
                "who": requested_by,
            },
        )

    logger.info(
        "datos de cliente eliminados",
        extra={"business_id": str(business_id), "mensajes": int(mensajes)},
    )
    return {
        "messages_deleted": int(mensajes),
        "requests_deleted": int(solicitudes),
        "found": 1,
    }


__all__ = [
    "AVISO_PRIVACIDAD",
    "RESPUESTA_LIMITE_DIARIO",
    "RESPUESTA_RATE_LIMIT",
    "check_daily_limit",
    "check_rate_limit",
    "eliminar_datos_cliente",
    "hash_identificador",
    "marcar_aviso_enviado",
    "necesita_aviso_privacidad",
    "reset_rate_limits",
]
