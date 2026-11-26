"""
Envío de correo por Amazon SES (RF-11: aviso al asesor).

boto3 es síncrono: la llamada corre en un hilo (`asyncio.to_thread`) para no
bloquear el loop de eventos que atiende los webhooks.

Credenciales: en AWS las toma solas del rol de la EC2 (IMDSv2); en local, del
perfil de la AWS CLI. Nunca van en el código ni en el .env.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Any

import boto3  # type: ignore[import-untyped]

from app.config import settings

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """No se pudo enviar el correo. El llamador decide cómo degradar (RNF-10)."""


@lru_cache(maxsize=1)
def _client() -> Any:
    return boto3.client("sesv2", region_name=settings.aws_region)


def _send_sync(sender: str, to: str, subject: str, body: str) -> str:
    response = _client().send_email(
        FromEmailAddress=sender,
        Destination={"ToAddresses": [to]},
        Content={
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
            }
        },
    )
    return str(response.get("MessageId", ""))


async def send_email(to: str, subject: str, body: str) -> str:
    """Envía un correo de texto plano. Devuelve el MessageId de SES."""
    sender = settings.ses_sender
    if not sender:
        raise EmailError("SES_SENDER no está configurado")
    try:
        message_id = await asyncio.to_thread(_send_sync, sender, to, subject, body)
    except Exception as exc:
        raise EmailError(str(exc)) from exc
    logger.info("correo enviado por SES (message_id=%s)", message_id)
    return message_id


__all__ = ["EmailError", "send_email"]
