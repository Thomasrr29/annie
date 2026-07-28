"""
Adaptador del canal WhatsApp: firma, normalizacion y envio (fase 3).

Sin base de datos: el adaptador es puro (RF-02).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.core.channel_whatsapp import (
    SignatureError,
    UnsupportedMessageType,
    WhatsAppError,
    _split_long_text,
    adapt_whatsapp_payload,
    extract_phone_number_id,
    is_status_update,
    send_whatsapp_reply,
    verify_subscription,
    verify_webhook_signature,
)
from app.schemas import OutboundMessage


def _payload_texto(
    texto: str = "hola",
    message_id: str = "wamid.TEST123",
    phone_number_id: str = "123456789",
    sender: str = "573001112233",
    tipo: str = "text",
) -> dict[str, Any]:
    """Payload realista de la Cloud API de Meta."""
    mensaje: dict[str, Any] = {
        "from": sender,
        "id": message_id,
        "timestamp": "1769472000",
        "type": tipo,
    }
    if tipo == "text":
        mensaje["text"] = {"body": texto}
    else:
        mensaje[tipo] = {"id": "media-id"}

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "573001110000",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {"profile": {"name": "Ana"}, "wa_id": sender}
                            ],
                            "messages": [mensaje],
                        },
                    }
                ],
            }
        ],
    }


def _firmar(cuerpo: bytes) -> str:
    mac = hmac.new(settings.whatsapp_app_secret.encode(), cuerpo, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


# ======================================================================
# Firma del webhook
# ======================================================================
def test_firma_valida_pasa() -> None:
    cuerpo = json.dumps(_payload_texto()).encode()
    verify_webhook_signature(cuerpo, _firmar(cuerpo))  # no lanza


def test_firma_invalida_se_rechaza() -> None:
    cuerpo = json.dumps(_payload_texto()).encode()
    with pytest.raises(SignatureError):
        verify_webhook_signature(cuerpo, "sha256=" + "0" * 64)


def test_firma_ausente_se_rechaza() -> None:
    with pytest.raises(SignatureError):
        verify_webhook_signature(b"{}", None)


def test_cuerpo_alterado_invalida_la_firma() -> None:
    """La firma cubre el cuerpo: cambiar un byte la rompe."""
    cuerpo = json.dumps(_payload_texto()).encode()
    firma = _firmar(cuerpo)

    with pytest.raises(SignatureError):
        verify_webhook_signature(cuerpo + b" ", firma)


def test_verificacion_de_suscripcion() -> None:
    challenge = verify_subscription(
        "subscribe", settings.whatsapp_verify_token, "desafio-123"
    )
    assert challenge == "desafio-123"


def test_verificacion_con_token_incorrecto() -> None:
    with pytest.raises(SignatureError):
        verify_subscription("subscribe", "token-falso", "x")


# ======================================================================
# Normalizacion
# ======================================================================
def test_adapta_mensaje_de_texto() -> None:
    business_id = uuid4()
    inbound = adapt_whatsapp_payload(_payload_texto("quiero una cita"), business_id)

    assert inbound.business_id == business_id
    assert inbound.channel == "whatsapp"
    assert inbound.channel_identifier == "573001112233"
    assert inbound.text == "quiero una cita"
    assert inbound.external_message_id == "wamid.TEST123"
    assert inbound.display_name == "Ana"
    assert inbound.received_at.tzinfo is not None


def test_extrae_el_phone_number_id() -> None:
    """Es lo unico del payload que identifica al tenant."""
    assert extract_phone_number_id(_payload_texto(phone_number_id="999")) == "999"


def test_phone_number_id_ausente_devuelve_none() -> None:
    assert extract_phone_number_id({"entry": []}) is None


@pytest.mark.parametrize("tipo", ["audio", "image", "location", "sticker", "document"])
def test_tipos_no_soportados_se_senalan(tipo: str) -> None:
    """El MVP solo lee texto; el llamador responde con amabilidad."""
    with pytest.raises(UnsupportedMessageType) as exc:
        adapt_whatsapp_payload(_payload_texto(tipo=tipo), uuid4())

    assert exc.value.message_type == tipo
    assert exc.value.sender == "573001112233"


def test_acuses_de_entrega_se_reconocen() -> None:
    """sent/delivered/read llegan por el mismo webhook y no son mensajes."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "123"},
                            "statuses": [{"id": "wamid.X", "status": "delivered"}],
                        }
                    }
                ]
            }
        ]
    }
    assert is_status_update(payload) is True
    assert is_status_update(_payload_texto()) is False


def test_payload_sin_mensaje_falla_claro() -> None:
    with pytest.raises(WhatsAppError):
        adapt_whatsapp_payload({"entry": [{"changes": [{"value": {}}]}]}, uuid4())


def test_texto_vacio_se_rechaza() -> None:
    with pytest.raises(WhatsAppError):
        adapt_whatsapp_payload(_payload_texto(texto="   "), uuid4())


# ======================================================================
# Particion de mensajes largos
# ======================================================================
def test_texto_corto_no_se_parte() -> None:
    assert _split_long_text("hola") == ["hola"]


def test_texto_largo_se_parte_respetando_el_limite() -> None:
    texto = "palabra " * 1000  # ~8000 caracteres
    partes = _split_long_text(texto)

    assert len(partes) > 1
    assert all(len(p) <= 4096 for p in partes)


def test_la_particion_no_corta_palabras() -> None:
    texto = "palabra " * 1000
    partes = _split_long_text(texto)

    # Reunidas deben conservar todas las palabras.
    assert " ".join(partes).split() == texto.split()


# ======================================================================
# Envio saliente
# ======================================================================
async def test_envio_exitoso(monkeypatch: pytest.MonkeyPatch) -> None:
    llamadas: list[dict[str, Any]] = []

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        llamadas.append(kwargs.get("json") or {})
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.OUT1"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    ids = await send_whatsapp_reply(
        OutboundMessage(
            business_id=uuid4(),
            channel="whatsapp",
            channel_identifier="573001112233",
            text="listo",
        )
    )

    assert ids == ["wamid.OUT1"]
    assert llamadas[0]["to"] == "573001112233"
    assert llamadas[0]["text"]["body"] == "listo"


async def test_error_4xx_no_se_reintenta(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un token invalido o un numero mal no mejoran reintentando."""
    intentos = 0

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal intentos
        intentos += 1
        return httpx.Response(400, text="Bad Request", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(WhatsAppError):
        await send_whatsapp_reply(
            OutboundMessage(
                business_id=uuid4(),
                channel="whatsapp",
                channel_identifier="573001112233",
                text="hola",
            )
        )

    assert intentos == 1, "un 4xx no debe reintentarse"


async def test_error_5xx_se_reintenta(monkeypatch: pytest.MonkeyPatch) -> None:
    intentos = 0

    async def fake_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal intentos
        intentos += 1
        if intentos < 2:
            return httpx.Response(503, text="overloaded", request=httpx.Request("POST", url))
        return httpx.Response(
            200,
            json={"messages": [{"id": "wamid.RETRY"}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("app.core.channel_whatsapp.SEND_BACKOFF_SECONDS", (0.0, 0.0))

    ids = await send_whatsapp_reply(
        OutboundMessage(
            business_id=uuid4(),
            channel="whatsapp",
            channel_identifier="573001112233",
            text="hola",
        )
    )

    assert ids == ["wamid.RETRY"]
    assert intentos == 2
