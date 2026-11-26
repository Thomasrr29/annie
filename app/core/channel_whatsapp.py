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
from app.schemas import (
    ContentType,
    InboundMedia,
    InboundMessage,
    OutboundMessage,
    text_for_model,
)

logger = logging.getLogger(__name__)

GRAPH_API_VERSION: Final = "v21.0"
GRAPH_API_BASE: Final = "https://graph.facebook.com"

# WhatsApp corta los mensajes de texto en 4096 caracteres.
WHATSAPP_MAX_TEXT_LEN: Final = 4096

# Reintentos del envio saliente ante fallos transitorios.
SEND_MAX_ATTEMPTS: Final = 3
SEND_BACKOFF_SECONDS: Final = (0.5, 2.0)

# WhatsApp limita el pie de foto a 1024 caracteres.
WHATSAPP_MAX_CAPTION_LEN: Final = 1024

# Imagenes: los formatos y el tope que acepta WhatsApp. Se aplican igual a lo
# que entra (descarga) y a lo que sale (subida desde el panel).
IMAGE_MIME_TYPES: Final = frozenset({"image/jpeg", "image/png", "image/webp"})
MAX_IMAGE_BYTES: Final = 5 * 1024 * 1024

# Respuesta cuando el cliente manda algo que no sabemos leer (audio, ubicacion...).
UNSUPPORTED_TYPE_REPLY: Final = (
    "Por ahora solo puedo leer mensajes de texto y fotos. "
    "¿Me contás por escrito en qué te ayudo?"
)


class WhatsAppError(RuntimeError):
    """Fallo al hablar con la API de WhatsApp."""


class SignatureError(WhatsAppError):
    """Firma del webhook invalida o ausente."""


class UnsupportedMessageType(WhatsAppError):
    """Mensaje de un tipo que el MVP no procesa (audio, ubicacion, documento...)."""

    def __init__(self, message_type: str, sender: str, phone_number_id: str) -> None:
        super().__init__(f"tipo de mensaje no soportado: {message_type}")
        self.message_type = message_type
        self.sender = sender
        self.phone_number_id = phone_number_id


class UnknownTenant(WhatsAppError):
    """El phone_number_id del payload no corresponde a ningun negocio."""


class InvalidMedia(WhatsAppError):
    """Archivo con formato o tamaño que WhatsApp (o nosotros) no aceptamos."""


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

    Texto, imagen y audio se procesan. Imagen y audio llegan como referencia
    (`media`): el archivo se descarga despues, en segundo plano, con
    `download_whatsapp_media`.

    Lanza `UnsupportedMessageType` para cualquier otro tipo, para que el llamador
    pueda responder con amabilidad en vez de quedarse callado.
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

    media: InboundMedia | None = None
    if message_type == "text":
        text = str((message.get("text") or {}).get("body", "")).strip()
        if not text:
            raise WhatsAppError("mensaje de texto vacio")
    elif message_type == "image":
        imagen = message.get("image") or {}
        if not imagen.get("id"):
            raise WhatsAppError("imagen sin id de media")
        media = InboundMedia(
            content_type=ContentType.IMAGE,
            external_media_id=str(imagen["id"]),
            mime_type=str(imagen.get("mime_type") or "image/jpeg"),
            caption=str(imagen.get("caption") or "").strip(),
        )
        text = text_for_model(ContentType.IMAGE, media.caption)
    elif message_type == "audio":
        # Nota de voz (voice=true) o archivo de audio: entra al flujo como un
        # mensaje más y se transcribe después (app/core/transcription.py).
        audio = message.get("audio") or {}
        if not audio.get("id"):
            raise WhatsAppError("audio sin id de media")
        media = InboundMedia(
            content_type=ContentType.AUDIO,
            external_media_id=str(audio["id"]),
            mime_type=str(audio.get("mime_type") or "audio/ogg"),
        )
        text = text_for_model(ContentType.AUDIO, "", "pending")
    else:
        raise UnsupportedMessageType(message_type, sender, phone_number_id)

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
        media=media,
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


async def send_typing_indicator(message_id: str, phone_number_id: str | None = None) -> None:
    """
    Muestra "escribiendo…" al cliente y marca su mensaje como leído (palomitas
    azules). En un chat 1:1, marcar un mensaje también marca los anteriores.

    `message_id` es el id que Meta le puso al mensaje del cliente (el "wamid"
    que llega en el webhook, `InboundMessage.external_message_id`).

    Meta lo quita cuando llega la respuesta o a los 25 s. Es cosmético: UN
    intento, timeout corto y sin reintentos (reintentar solo sumaría peticiones).
    Docs: https://developers.facebook.com/docs/whatsapp/cloud-api/typing-indicators/
    """
    number_id = phone_number_id or settings.whatsapp_phone_number_id
    url = f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{number_id}/messages"
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
        "typing_indicator": {"type": "text"},
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise WhatsAppError(f"no se pudo mostrar 'escribiendo': {exc}") from exc
    if response.status_code >= 400:
        raise WhatsAppError(
            f"WhatsApp rechazo el indicador ({response.status_code}): {response.text}"
        )


# ======================================================================
# Imagenes: descarga (entrada) y subida + envio (salida)
# ======================================================================
def check_image(data: bytes, mime_type: str) -> None:
    """
    Valida una imagen contra lo que acepta WhatsApp. Lanza `InvalidMedia`.

    Se aplica en los dos sentidos: lo que se descarga se guarda en la base, asi
    que un archivo enorme o de otro tipo no debe entrar aunque Meta lo entregue.
    """
    if mime_type not in IMAGE_MIME_TYPES:
        raise InvalidMedia(f"formato de imagen no soportado: {mime_type}")
    if not data:
        raise InvalidMedia("imagen vacia")
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidMedia(
            f"imagen de {len(data) // 1024} KB; el maximo es {MAX_IMAGE_BYTES // 1024} KB"
        )


# WhatsApp acepta audios de hasta 16 MB.
MAX_AUDIO_BYTES: Final = 16 * 1024 * 1024


async def download_whatsapp_media(
    media_id: str, *, audio: bool = False
) -> tuple[bytes, str]:
    """
    Descarga un archivo que mando el cliente. Devuelve (bytes, mime_type).

    Son dos pasos: `GET /{media_id}` devuelve una URL temporal, y esa URL se baja
    con el mismo token. La URL expira en minutos, por eso se descarga apenas
    llega el mensaje y no cuando alguien abre la bandeja.
    """
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            meta = await client.get(
                f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{media_id}", headers=headers
            )
            if meta.status_code >= 400:
                raise WhatsAppError(
                    f"no se pudo resolver la media {media_id} ({meta.status_code})"
                )
            info = meta.json()
            url = info.get("url")
            if not url:
                raise WhatsAppError(f"la media {media_id} no trae url de descarga")

            # Se rechaza ANTES de bajar si Meta ya avisa que es demasiado grande.
            declarado = int(info.get("file_size") or 0)
            maximo = MAX_AUDIO_BYTES if audio else MAX_IMAGE_BYTES
            if declarado > maximo:
                raise InvalidMedia(f"archivo de {declarado // 1024} KB, supera el maximo")

            archivo = await client.get(str(url), headers=headers)
            if archivo.status_code >= 400:
                raise WhatsAppError(
                    f"no se pudo descargar la media {media_id} ({archivo.status_code})"
                )
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"error de red al descargar la media: {exc}") from exc

    mime_type = str(info.get("mime_type") or archivo.headers.get("content-type") or "")
    if audio:
        # Se conserva el mime completo ("audio/ogg; codecs=opus"): lo usa el
        # proveedor de transcripcion. El audio no se guarda en ningun lado.
        if not archivo.content:
            raise InvalidMedia("audio vacio")
        return archivo.content, mime_type
    mime_type = mime_type.split(";")[0].strip()
    check_image(archivo.content, mime_type)
    return archivo.content, mime_type


async def send_whatsapp_image(
    to: str,
    data: bytes,
    mime_type: str,
    caption: str = "",
    phone_number_id: str | None = None,
) -> str:
    """
    Sube una imagen a Meta y la envia. Devuelve el id del mensaje.

    Se sube primero (`POST /{phone_number_id}/media`) y se envia por id, en vez de
    pasar un link publico: los archivos del negocio no tienen URL publica y no
    deben tenerla.
    """
    check_image(data, mime_type)
    number_id = phone_number_id or settings.whatsapp_phone_number_id
    auth = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            subida = await client.post(
                f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{number_id}/media",
                headers=auth,
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": ("imagen", data, mime_type)},
            )
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"error de red al subir la imagen: {exc}") from exc

        if subida.status_code >= 400:
            raise WhatsAppError(
                f"WhatsApp rechazo la imagen ({subida.status_code}): {subida.text}"
            )
        media_id = subida.json().get("id")
        if not media_id:
            raise WhatsAppError("WhatsApp no devolvio el id de la imagen subida")

        imagen: dict[str, Any] = {"id": str(media_id)}
        if caption.strip():
            imagen["caption"] = caption.strip()[:WHATSAPP_MAX_CAPTION_LEN]

        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "image",
            "image": imagen,
        }
        return await _post_with_retry(
            client,
            f"{GRAPH_API_BASE}/{GRAPH_API_VERSION}/{number_id}/messages",
            {**auth, "Content-Type": "application/json"},
            body,
        )


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
    "IMAGE_MIME_TYPES",
    "MAX_AUDIO_BYTES",
    "MAX_IMAGE_BYTES",
    "UNSUPPORTED_TYPE_REPLY",
    "InvalidMedia",
    "SignatureError",
    "UnknownTenant",
    "UnsupportedMessageType",
    "WhatsAppError",
    "adapt_whatsapp_payload",
    "check_image",
    "download_whatsapp_media",
    "extract_phone_number_id",
    "is_status_update",
    "send_whatsapp_image",
    "send_typing_indicator",
    "send_whatsapp_reply",
    "verify_subscription",
    "verify_webhook_signature",
]
