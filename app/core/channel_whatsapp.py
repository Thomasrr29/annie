"""
Adaptador del canal WhatsApp (Cloud API de Meta).

UNICA pieza del sistema que conoce el formato de WhatsApp. Traduce en ambos
sentidos:

    payload crudo de Meta  ->  InboundMessage   (agnostico al canal)
    OutboundMessage        ->  llamada saliente a la Graph API

El resto del sistema no importa nada de este modulo salvo estas funciones: es lo
que permite agregar otro canal despues sin tocar el orquestador (RF-02).

BSP elegido: Cloud API de Meta directo (sin intermediario). El webhook viene
firmado con HMAC-SHA256 en `X-Hub-Signature-256`, verificado con el app secret.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import httpx

from app.config import settings
from app.schemas import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

GRAPH_API_VERSION: Final = "v21.0"
GRAPH_API_BASE: Final = "https://graph.facebook.com"

# WhatsApp corta los mensajes de texto en 4096 caracteres.
WHATSAPP_MAX_TEXT_LEN: Final = 4096

# Reintentos del envio saliente ante fallos transitorios.
SEND_MAX_ATTEMPTS: Final = 3
SEND_BACKOFF_SECONDS: Final = (0.5, 2.0)

# Respuesta cuando el cliente manda algo que no sabemos leer (audio, ubicacion...).
UNSUPPORTED_TYPE_REPLY: Final = (
    "Por ahora solo puedo leer mensajes de texto. "
    "¿Me contás por escrito en qué te ayudo?"
)


class WhatsAppError(RuntimeError):
    """Fallo al hablar con la API de WhatsApp."""


class SignatureError(WhatsAppError):
    """Firma del webhook invalida o ausente."""


class UnsupportedMessageType(WhatsAppError):
    """Mensaje de un tipo que el MVP no procesa (audio, imagen, ubicacion...)."""

    def __init__(self, message_type: str, sender: str, phone_number_id: str) -> None:
        super().__init__(f"tipo de mensaje no soportado: {message_type}")
        self.message_type = message_type
        self.sender = sender
        self.phone_number_id = phone_number_id


class UnknownTenant(WhatsAppError):
    """El phone_number_id del payload no corresponde a ningun negocio."""


# ======================================================================
# Verificacion del webhook
# ======================================================================
def verify_webhook_signature(raw_body: bytes, signature_header: str | None) -> None:
    """
    Verifica `X-Hub-Signature-256` (HMAC-SHA256 del cuerpo con el app secret).

    Se compara con `compare_digest` para no filtrar informacion por el tiempo de
    comparacion. Sin firma valida, el evento se descarta: cualquiera podria
    inyectar mensajes falsos en las conversaciones de un negocio.
    """
    if not signature_header:
        raise SignatureError("falta el header X-Hub-Signature-256")

    prefix = "sha256="
    if not signature_header.startswith(prefix):
        raise SignatureError("formato de firma inesperado")

    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature_header[len(prefix) :]):
        raise SignatureError("firma del webhook invalida")


def verify_subscription(mode: str | None, token: str | None, challenge: str | None) -> str:
    """
    Handshake de suscripcion (GET del webhook): Meta manda `hub.verify_token` y
    espera de vuelta `hub.challenge` en texto plano.
    """
    if mode != "subscribe" or token != settings.whatsapp_verify_token:
        raise SignatureError("verify_token invalido")
    if challenge is None:
        raise SignatureError("falta hub.challenge")
    return challenge


# ======================================================================
# Entrada: payload de Meta -> InboundMessage
# ======================================================================
def extract_phone_number_id(payload: dict[str, Any]) -> str | None:
    """
    `phone_number_id` del numero que RECIBIO el mensaje: es lo que identifica al
    tenant. Se resuelve contra `businesses.whatsapp_phone_number_id`.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        metadata = value.get("metadata") or {}
        number_id = metadata.get("phone_number_id")
        return str(number_id) if number_id else None
    except (KeyError, IndexError, TypeError):
        return None


def is_status_update(payload: dict[str, Any]) -> bool:
    """
    Meta manda tambien acuses (sent/delivered/read) por el mismo webhook. No son
    mensajes del cliente: se ignoran sin ruido.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return False
    return "statuses" in value and "messages" not in value


def adapt_whatsapp_payload(payload: dict[str, Any], business_id: UUID) -> InboundMessage:
    """
    Payload crudo de Meta -> InboundMessage.

    El `business_id` llega ya resuelto (lo hace el webhook con
    `extract_phone_number_id`): este adaptador no toca la base de datos.

    Lanza `UnsupportedMessageType` si el mensaje no es de texto, para que el
    llamador pueda responder con amabilidad en vez de quedarse callado.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise WhatsAppError(f"payload de WhatsApp sin mensaje utilizable: {exc}") from exc

    sender = str(message.get("from", ""))
    if not sender:
        raise WhatsAppError("mensaje sin remitente")

    message_type = str(message.get("type", "unknown"))
    phone_number_id = extract_phone_number_id(payload) or ""

    if message_type != "text":
        raise UnsupportedMessageType(message_type, sender, phone_number_id)

    text = str((message.get("text") or {}).get("body", "")).strip()
    if not text:
        raise WhatsAppError("mensaje de texto vacio")

    # `timestamp` viene en segundos epoch como string.
    raw_ts = message.get("timestamp")
    try:
        received_at = datetime.fromtimestamp(int(raw_ts), tz=UTC)
    except (TypeError, ValueError):
        received_at = datetime.now(UTC)

    display_name = None
    contacts = value.get("contacts") or []
    if contacts:
        display_name = (contacts[0].get("profile") or {}).get("name")

    return InboundMessage(
        business_id=business_id,
        channel="whatsapp",
        channel_identifier=sender,
        text=text,
        external_message_id=str(message.get("id")) if message.get("id") else None,
        received_at=received_at,
        display_name=display_name,
    )


# ======================================================================
# Salida: OutboundMessage -> Graph API
# ======================================================================
def _split_long_text(text: str, limit: int = WHATSAPP_MAX_TEXT_LEN) -> list[str]:
    """
    Parte un texto que excede el limite de WhatsApp, cortando por saltos de linea
    o espacios para no romper palabras a la mitad.
    """
    if len(text) <= limit:
        return [text]

    partes: list[str] = []
    restante = text
    while len(restante) > limit:
        ventana = restante[:limit]
        corte = max(ventana.rfind("\n"), ventana.rfind(" "))
        if corte <= 0:
            corte = limit
        partes.append(restante[:corte].rstrip())
        restante = restante[corte:].lstrip()
    if restante:
        partes.append(restante)
    return partes


async def send_whatsapp_reply(
    msg: OutboundMessage, phone_number_id: str | None = None
) -> list[str]:
    """
    Envia la respuesta por la Graph API. Devuelve los ids de mensaje de Meta.

    Va dentro de la ventana de servicio de 24h (sin plantilla, sin costo por
    mensaje). Reintenta ante errores transitorios (5xx, 429, red) con backoff;
    los 4xx no se reintentan porque no van a mejorar solos.
    """
    number_id = phone_number_id or settings.whatsapp_phone_number_id
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }

    enviados: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for parte in _split_long_text(msg.text):
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": msg.channel_identifier,
                "type": "text",
                "text": {"preview_url": False, "body": parte},
            }
            enviados.append(await _post_with_retry(client, url, headers, body))
    return enviados


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> str:
    """POST con reintentos; devuelve el id del mensaje enviado."""
    import asyncio

    ultimo_error: Exception | None = None

    for intento in range(SEND_MAX_ATTEMPTS):
        try:
            response = await client.post(url, headers=headers, json=body)

            if response.status_code < 400:
                data = response.json()
                mensajes = data.get("messages") or []
                return str(mensajes[0]["id"]) if mensajes else ""

            # 4xx (salvo 429) no mejora reintentando: token malo, numero
            # invalido, ventana de 24h cerrada.
            if response.status_code != 429 and response.status_code < 500:
                raise WhatsAppError(
                    f"WhatsApp rechazo el envio ({response.status_code}): {response.text}"
                )

            ultimo_error = WhatsAppError(
                f"error transitorio de WhatsApp ({response.status_code})"
            )
        except httpx.HTTPError as exc:
            ultimo_error = exc

        if intento < SEND_MAX_ATTEMPTS - 1:
            await asyncio.sleep(SEND_BACKOFF_SECONDS[min(intento, len(SEND_BACKOFF_SECONDS) - 1)])
            logger.warning(
                "reintentando envio a WhatsApp (intento %d/%d)",
                intento + 2,
                SEND_MAX_ATTEMPTS,
            )

    raise WhatsAppError(f"no se pudo enviar el mensaje tras {SEND_MAX_ATTEMPTS} intentos") from (
        ultimo_error
    )


__all__ = [
    "UNSUPPORTED_TYPE_REPLY",
    "SignatureError",
    "UnknownTenant",
    "UnsupportedMessageType",
    "WhatsAppError",
    "adapt_whatsapp_payload",
    "extract_phone_number_id",
    "is_status_update",
    "send_whatsapp_reply",
    "verify_subscription",
    "verify_webhook_signature",
]
