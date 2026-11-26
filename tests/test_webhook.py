"""
Webhook de WhatsApp: ACK inmediato, firma, deduplicacion (fase 3).

Criterios de aceptacion de la fase:
  - responde 200 en <500 ms aunque el handler tarde;
  - un payload duplicado no genera doble procesamiento.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.main import app
from tests.conftest import TEST_DATABASE_URL, requires_db
from tests.test_canal_whatsapp import _payload_texto

PHONE_NUMBER_ID = "test-phone-number-id"


def _firmar(cuerpo: bytes) -> str:
    mac = hmac.new(settings.whatsapp_app_secret.encode(), cuerpo, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _post(client: TestClient, payload: dict[str, Any], firmar: bool = True) -> Any:
    cuerpo = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if firmar:
        headers["X-Hub-Signature-256"] = _firmar(cuerpo)
    return client.post("/webhooks/whatsapp", content=cuerpo, headers=headers)


# ======================================================================
# Verificacion de suscripcion (GET)
# ======================================================================
def test_get_devuelve_el_challenge(client: TestClient) -> None:
    r = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.whatsapp_verify_token,
            "hub.challenge": "12345",
        },
    )
    assert r.status_code == 200
    assert r.text == "12345"


def test_get_con_token_invalido_da_403(client: TestClient) -> None:
    r = client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "malo", "hub.challenge": "x"},
    )
    assert r.status_code == 403


# ======================================================================
# Firma
# ======================================================================
def test_sin_firma_da_403(client: TestClient) -> None:
    """Sin esto cualquiera inyecta mensajes en conversaciones ajenas."""
    assert _post(client, _payload_texto(), firmar=False).status_code == 403


def test_firma_invalida_da_403(client: TestClient) -> None:
    cuerpo = json.dumps(_payload_texto()).encode()
    r = client.post(
        "/webhooks/whatsapp",
        content=cuerpo,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert r.status_code == 403


# ======================================================================
# ACK inmediato (RF-01, RNF-01)
# ======================================================================
@requires_db
def test_ack_es_inmediato_aunque_el_handler_tarde(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, negocio_whatsapp: UUID
) -> None:
    """
    El criterio de la fase: 200 en <500 ms con un handler lento.

    Si tardaramos, Meta reintenta y el cliente recibe respuestas duplicadas.
    """
    async def handler_lento(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(3.0)

    monkeypatch.setattr("app.core.orchestrator.handle_inbound_message", handler_lento)

    inicio = time.monotonic()
    r = _post(
        client,
        _payload_texto(message_id=f"wamid.{uuid4()}", phone_number_id=PHONE_NUMBER_ID),
    )
    transcurrido = time.monotonic() - inicio

    assert r.status_code == 200
    assert transcurrido < 0.5, f"el ACK tardo {transcurrido:.2f}s"


@requires_db
def test_acuse_de_entrega_no_genera_trabajo(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    procesados: list[Any] = []

    async def fake_handler(inbound: Any, *args: Any, **kwargs: Any) -> None:
        procesados.append(inbound)

    monkeypatch.setattr("app.core.orchestrator.handle_inbound_message", fake_handler)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [{"id": "wamid.X", "status": "read"}],
                        }
                    }
                ]
            }
        ]
    }
    assert _post(client, payload).status_code == 200
    assert procesados == []


def test_numero_desconocido_no_rompe(client: TestClient) -> None:
    """Un phone_number_id sin negocio: se ignora con 200, sin excepcion."""
    r = _post(client, _payload_texto(phone_number_id="numero-inexistente-999"))
    assert r.status_code == 200


# ======================================================================
# Deduplicacion (criterio de la fase)
# ======================================================================
@requires_db
def test_mensaje_duplicado_se_procesa_una_sola_vez(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, negocio_whatsapp: UUID
) -> None:
    """
    WhatsApp reentrega si el ACK tarda o falla. Sin dedup, el cliente recibe la
    respuesta dos veces y el turno se paga dos veces.
    """
    procesados: list[Any] = []

    async def fake_handler(inbound: Any, *args: Any, **kwargs: Any) -> None:
        procesados.append(inbound.text)

    monkeypatch.setattr("app.core.orchestrator.handle_inbound_message", fake_handler)

    payload = _payload_texto(
        texto="mensaje unico", message_id="wamid.DUPLICADO", phone_number_id=PHONE_NUMBER_ID
    )

    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200
    assert _post(client, payload).status_code == 200

    assert len(procesados) == 1, f"se proceso {len(procesados)} veces en vez de 1"


@requires_db
def test_mensajes_distintos_se_procesan_ambos(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, negocio_whatsapp: UUID
) -> None:
    """La dedup no debe descartar mensajes legitimos distintos."""
    procesados: list[str] = []

    async def fake_handler(inbound: Any, *args: Any, **kwargs: Any) -> None:
        procesados.append(inbound.text)

    monkeypatch.setattr("app.core.orchestrator.handle_inbound_message", fake_handler)

    _post(client, _payload_texto("primero", "wamid.A1", PHONE_NUMBER_ID))
    _post(client, _payload_texto("segundo", "wamid.A2", PHONE_NUMBER_ID))

    assert procesados == ["primero", "segundo"]


@requires_db
def test_tipo_no_soportado_responde_amable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, negocio_whatsapp: UUID
) -> None:
    """Una ubicación no se procesa, pero el cliente no queda sin respuesta."""
    enviados: list[str] = []

    async def fake_send(msg: Any, **kwargs: Any) -> list[str]:
        enviados.append(msg.text)
        return ["wamid.OUT"]

    monkeypatch.setattr("app.api.webhooks.send_whatsapp_reply", fake_send)

    r = _post(
        client,
        _payload_texto(
            tipo="location", message_id="wamid.UBICACION1", phone_number_id=PHONE_NUMBER_ID
        ),
    )

    assert r.status_code == 200
    assert enviados, "no se le respondio nada al cliente"
    assert "texto" in enviados[0].lower()


@requires_db
def test_el_fallo_del_handler_no_rompe_el_webhook(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, negocio_whatsapp: UUID
) -> None:
    """
    El webhook ya respondio 200: una excepcion del handler se loguea y se
    registra, pero no puede escapar al event loop (RNF-10).
    """
    async def handler_que_falla(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr("app.core.orchestrator.handle_inbound_message", handler_que_falla)

    r = _post(
        client, _payload_texto(message_id="wamid.FALLO1", phone_number_id=PHONE_NUMBER_ID)
    )
    assert r.status_code == 200


# ======================================================================
# Fixture: negocio con numero de WhatsApp asociado
# ======================================================================
@pytest.fixture
def negocio_whatsapp() -> Iterator[UUID]:
    """
    Crea un negocio con `whatsapp_phone_number_id` y lo limpia al final.

    Es una fixture SINCRONA que abre su propio event loop: el TestClient corre el
    webhook en un loop aparte, y compartir un engine entre loops rompe asyncpg.
    Por eso tampoco puede reusar la fixture `session`.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("Sin base de datos configurada")

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    business_id = uuid4()

    async def _ejecutar(sentencias: list[tuple[str, dict[str, Any]]]) -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    # `false` = alcance de sesion, no de transaccion: el INSERT
                    # que sigue tiene que verlo para pasar el WITH CHECK de RLS.
                    text("SELECT set_config('app.current_business_id', :bid, false)"),
                    {"bid": str(business_id)},
                )
                for sql, params in sentencias:
                    await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(
        _ejecutar([
            (
                "INSERT INTO businesses (id, name, whatsapp_phone_number_id) "
                "VALUES (:id, 'Negocio Webhook', :pid)",
                {"id": business_id, "pid": PHONE_NUMBER_ID},
            )
        ])
    )
    try:
        yield business_id
    finally:
        asyncio.run(
            _ejecutar([("DELETE FROM businesses WHERE id = :id", {"id": business_id})])
        )
