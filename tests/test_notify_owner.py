"""
RF-11: el aviso al asesor sale de verdad por correo (SES) y degrada con gracia.

Sin base de datos ni AWS: se reemplazan la lectura de la configuración del
negocio y el cliente de SES. Lo que se prueba es la lógica de notify_owner:
a quién se envía, qué ve el asesor y qué pasa cuando algo falla.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

from app.core import email_ses, tools


def _negocio(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    """La configuración de aviso que "devuelve la base" para este test."""

    @asynccontextmanager
    async def _sesion_falsa(_business_id: Any) -> AsyncIterator[object]:
        yield object()

    class _RepoFalso:
        def __init__(self, _session: object) -> None:
            pass

        async def get_notify_config(self, _business_id: Any) -> dict[str, Any]:
            return config

    monkeypatch.setattr(tools, "tenant_session", _sesion_falsa)
    monkeypatch.setattr(tools, "BusinessRepo", _RepoFalso)


@pytest.fixture
def enviados(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Reemplaza el envío real: guarda cada correo en una lista."""
    bandeja: list[dict[str, str]] = []

    async def _send(to: str, subject: str, body: str) -> str:
        bandeja.append({"to": to, "subject": subject, "body": body})
        return "msg-1"

    monkeypatch.setattr(email_ses, "send_email", _send)
    return bandeja


async def test_envia_al_correo_configurado(
    monkeypatch: pytest.MonkeyPatch, enviados: list[dict[str, str]]
) -> None:
    _negocio(
        monkeypatch,
        {"name": "Nott", "channel": "email", "config": {"email": "asesor@nott.test"}},
    )

    resultado = await tools.notify_owner(uuid4(), "Nuevo embajador", "Laura quiere ser embajadora")

    assert resultado == tools.AVISO_ENVIADO
    assert len(enviados) == 1
    correo = enviados[0]
    assert correo["to"] == "asesor@nott.test"
    assert correo["subject"] == "[Nott] Nuevo embajador"
    assert "Laura quiere ser embajadora" in correo["body"]
    # El marco lo pone el sistema: el asesor sabe que el resumen es automático.
    assert "lo resume el asistente" in correo["body"]
    assert "/panel" in correo["body"]


async def test_asunto_del_modelo_queda_en_una_linea(
    monkeypatch: pytest.MonkeyPatch, enviados: list[dict[str, str]]
) -> None:
    """Un asunto con saltos de línea (venga del modelo o de una inyección) no rompe el correo."""
    _negocio(monkeypatch, {"name": "Nott", "channel": "email", "config": {"email": "a@nott.test"}})

    await tools.notify_owner(uuid4(), "Hola\r\nBcc: todos@spam.test\n" + "x" * 300, "cuerpo")

    asunto = enviados[0]["subject"]
    assert "\n" not in asunto and "\r" not in asunto
    assert len(asunto) <= len("[Nott] ") + 150


@pytest.mark.parametrize(
    "config",
    [
        {"name": "Nott", "channel": None, "config": {}},
        {"name": "Nott", "channel": "whatsapp", "config": {"phone": "+57"}},
        {"name": "Nott", "channel": "email", "config": {}},
    ],
    ids=["sin-canal", "canal-no-soportado", "email-sin-direccion"],
)
async def test_sin_configuracion_valida_no_envia_y_lo_dice(
    monkeypatch: pytest.MonkeyPatch,
    enviados: list[dict[str, str]],
    config: dict[str, Any],
) -> None:
    _negocio(monkeypatch, config)

    resultado = await tools.notify_owner(uuid4(), "asunto", "cuerpo")

    assert enviados == []
    # Honesto: no le dice al modelo (ni al cliente) que avisó.
    assert resultado == tools.AVISO_SOLO_PANEL


async def test_falla_de_ses_no_rompe_la_conversacion(monkeypatch: pytest.MonkeyPatch) -> None:
    _negocio(monkeypatch, {"name": "Nott", "channel": "email", "config": {"email": "a@nott.test"}})

    async def _falla(to: str, subject: str, body: str) -> str:
        raise email_ses.EmailError("MessageRejected: Email address is not verified")

    monkeypatch.setattr(email_ses, "send_email", _falla)

    resultado = await tools.notify_owner(uuid4(), "asunto", "cuerpo")

    assert resultado == tools.AVISO_SOLO_PANEL


async def test_send_email_sin_remitente_falla_claro(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_ses.settings, "ses_sender", None)

    with pytest.raises(email_ses.EmailError, match="SES_SENDER"):
        await email_ses.send_email("a@nott.test", "asunto", "cuerpo")


async def test_send_email_arma_el_pedido_a_ses(monkeypatch: pytest.MonkeyPatch) -> None:
    pedidos: list[dict[str, Any]] = []

    class _ClienteSesFalso:
        def send_email(self, **kwargs: Any) -> dict[str, str]:
            pedidos.append(kwargs)
            return {"MessageId": "abc-123"}

    monkeypatch.setattr(email_ses.settings, "ses_sender", "annie@nott.test")
    monkeypatch.setattr(email_ses, "_client", lambda: _ClienteSesFalso())

    message_id = await email_ses.send_email("asesor@nott.test", "Asunto ñ", "Cuerpo")

    assert message_id == "abc-123"
    pedido = pedidos[0]
    assert pedido["FromEmailAddress"] == "annie@nott.test"
    assert pedido["Destination"] == {"ToAddresses": ["asesor@nott.test"]}
    simple = pedido["Content"]["Simple"]
    assert simple["Subject"] == {"Data": "Asunto ñ", "Charset": "UTF-8"}
    assert simple["Body"]["Text"]["Data"] == "Cuerpo"
