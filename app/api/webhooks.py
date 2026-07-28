"""
Superficie RUNTIME: webhook de WhatsApp.

Patron clave (RF-01, RNF-01): **ACK inmediato**. El webhook valida la firma,
resuelve el tenant, deduplica y devuelve 200 enseguida; el turno del agente corre
en segundo plano. Si tardaramos, Meta reintenta la entrega y el cliente recibe
respuestas duplicadas.

Corolario importante: cuando el procesamiento falla, el webhook YA respondio 200.
No hay forma de "devolver un error"; el fallo se loguea y, si se puede, se le
avisa al cliente por el canal saliente (RNF-10).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import PlainTextResponse

from app.core.channel_whatsapp import (
    UNSUPPORTED_TYPE_REPLY,
    SignatureError,
    UnsupportedMessageType,
    WhatsAppError,
    adapt_whatsapp_payload,
    extract_phone_number_id,
    is_status_update,
    send_whatsapp_reply,
    verify_subscription,
    verify_webhook_signature,
)
from app.db.repositories import BusinessRepo, InboundEventRepo
from app.db.session import system_session, tenant_session
from app.schemas import InboundMessage, OutboundMessage, WhatsAppAck

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["runtime"])


@router.get("/whatsapp")
async def verify_whatsapp_webhook(request: Request) -> Response:
    """
    Handshake de suscripcion de Meta. Devuelve `hub.challenge` en texto plano.
    """
    params = request.query_params
    try:
        challenge = verify_subscription(
            params.get("hub.mode"),
            params.get("hub.verify_token"),
            params.get("hub.challenge"),
        )
    except SignatureError as exc:
        logger.warning("verificacion de webhook rechazada: %s", exc)
        return PlainTextResponse("forbidden", status_code=403)

    return PlainTextResponse(challenge, status_code=200)


@router.post("/whatsapp", response_model=WhatsAppAck)
async def whatsapp_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    """
    Recibe eventos de WhatsApp. Responde 200 SIEMPRE que la firma sea valida,
    incluso si el evento no genera trabajo (acuses, tipos no soportados): un
    error aqui solo provocaria reintentos inutiles de Meta.

    Todo lo que puede tardar (LLM, herramientas, envio) va a segundo plano.
    """
    raw_body = await request.body()

    # 1. Firma: sin esto, cualquiera inyecta mensajes en las conversaciones.
    try:
        verify_webhook_signature(raw_body, x_hub_signature_256)
    except SignatureError as exc:
        logger.warning("firma de webhook invalida: %s", exc)
        return Response(status_code=403)

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("payload de webhook no es JSON valido")
        return _ack()

    # 2. Acuses de entrega (sent/delivered/read): no son mensajes del cliente.
    if is_status_update(payload):
        return _ack()

    # 3. Resolver el tenant desde el numero que recibio el mensaje.
    phone_number_id = extract_phone_number_id(payload)
    if not phone_number_id:
        logger.warning("webhook sin phone_number_id; se ignora")
        return _ack()

    async with system_session() as session:
        business_id = await BusinessRepo(session).get_id_by_whatsapp_number(phone_number_id)

    if business_id is None:
        # Numero no asociado a ningun negocio activo: puede ser un numero dado
        # de baja o una mala configuracion. Se registra, no se responde.
        logger.warning("phone_number_id %s sin negocio activo asociado", phone_number_id)
        return _ack()

    # 4. Normalizar. Un tipo no soportado se contesta con amabilidad.
    try:
        inbound = adapt_whatsapp_payload(payload, business_id)
    except UnsupportedMessageType as exc:
        logger.info("tipo de mensaje no soportado (%s)", exc.message_type)
        _spawn(_reply_unsupported(business_id, exc.sender, phone_number_id))
        return _ack()
    except WhatsAppError as exc:
        logger.warning("payload de WhatsApp no utilizable: %s", exc)
        return _ack()

    # 5. Deduplicar ANTES de encolar: WhatsApp reentrega si el ACK tarda, y el
    #    mismo mensaje procesado dos veces significa doble respuesta y doble costo.
    if inbound.external_message_id:
        async with tenant_session(business_id) as session:
            es_nuevo = await InboundEventRepo(session).claim(
                business_id, inbound.external_message_id, inbound.channel
            )
        if not es_nuevo:
            logger.info(
                "mensaje %s ya procesado; se ignora la reentrega",
                inbound.external_message_id,
            )
            return _ack()

    # 6. A segundo plano y ACK inmediato.
    _spawn(process_inbound_message(inbound, phone_number_id))
    return _ack()


def _ack() -> Response:
    """200 con el cuerpo minimo que espera Meta."""
    return Response(content='{"status":"received"}', media_type="application/json")


# Referencias fuertes a las tareas en vuelo: sin esto el GC puede recolectar una
# tarea a medio correr (asyncio solo guarda referencias debiles).
_TAREAS_EN_VUELO: set[asyncio.Task[None]] = set()


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    """
    Lanza el procesamiento desacompanado del ciclo del request.

    NO se usa `BackgroundTasks`: esas tareas corren despues de enviar la
    respuesta pero ANTES de cerrar el request, asi que un turno lento (LLM +
    herramientas) mantiene la conexion abierta y Meta reintenta la entrega.
    Con `create_task` el ACK sale de inmediato y el turno sigue por su cuenta.
    """
    tarea = asyncio.create_task(coro)
    _TAREAS_EN_VUELO.add(tarea)
    tarea.add_done_callback(_TAREAS_EN_VUELO.discard)


async def _reply_unsupported(business_id: Any, sender: str, phone_number_id: str) -> None:
    """Avisa al cliente que por ahora solo leemos texto."""
    try:
        await send_whatsapp_reply(
            OutboundMessage(
                business_id=business_id,
                channel="whatsapp",
                channel_identifier=sender,
                text=UNSUPPORTED_TYPE_REPLY,
            ),
            phone_number_id=phone_number_id,
        )
    except WhatsAppError:
        logger.exception("no se pudo avisar del tipo no soportado")


async def process_inbound_message(inbound: InboundMessage, phone_number_id: str) -> None:
    """
    Procesa el turno en segundo plano y envia la respuesta.

    Delega en el orquestador (`app.core.orchestrator`), que corre el loop
    completo de los 7 flujos. El import es local a proposito: evita el ciclo
    webhooks -> orchestrator -> channel_whatsapp.

    Nunca propaga excepciones: el webhook ya respondio 200 y una excepcion aqui
    solo dejaria un error sin manejar en el event loop (RNF-10). El evento se
    marca 'done' o 'failed' para no reprocesar una reentrega de algo ya fallado.
    """
    from app.core.orchestrator import handle_inbound_message

    try:
        await handle_inbound_message(inbound, phone_number_id)
        if inbound.external_message_id:
            async with tenant_session(inbound.business_id) as session:
                await InboundEventRepo(session).mark_done(
                    inbound.business_id, inbound.external_message_id, inbound.channel
                )
    except Exception as exc:  # noqa: BLE001 - frontera: nada debe escapar de aqui
        logger.exception("fallo el procesamiento del mensaje entrante")
        if inbound.external_message_id:
            try:
                async with tenant_session(inbound.business_id) as session:
                    await InboundEventRepo(session).mark_failed(
                        inbound.business_id,
                        inbound.external_message_id,
                        str(exc),
                        inbound.channel,
                    )
            except Exception:  # noqa: BLE001
                logger.exception("no se pudo registrar el fallo del evento")


__all__ = ["process_inbound_message", "router"]
