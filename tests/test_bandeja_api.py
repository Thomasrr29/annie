"""
Bandeja de conversaciones (RF-14): historial completo, respuesta humana con
texto e imagen, tomar/devolver la conversacion y ventana de 24h de WhatsApp.

El criterio de siempre aplica tambien aca: un negocio no puede ver ni tocar la
conversacion o las fotos de otro, ni conociendo los uuid.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.channel_whatsapp import WhatsAppError
from app.main import app
from tests.conftest import requires_db
from tests.test_admin_api import Tenant, _ejecutar

pytestmark = [requires_db]

JPEG = b"\xff\xd8\xff\xe0foto-de-prueba"


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def tenant_a() -> Iterator[Tenant]:
    t = Tenant("Bandeja A")
    yield t
    t.borrar()


@pytest.fixture
def tenant_b() -> Iterator[Tenant]:
    t = Tenant("Bandeja B")
    yield t
    t.borrar()


@pytest.fixture
def envios(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Sustituye el envio real a WhatsApp y registra lo que se habria mandado."""
    registro: dict[str, list[Any]] = {"texto": [], "imagen": []}

    async def fake_reply(msg: Any, phone_number_id: str | None = None) -> list[str]:
        registro["texto"].append((msg, phone_number_id))
        return ["wamid.OUT"]

    async def fake_image(to: str, data: bytes, mime_type: str, **kwargs: Any) -> str:
        registro["imagen"].append((to, data, mime_type, kwargs))
        return "wamid.IMG"

    monkeypatch.setattr("app.api.admin.send_whatsapp_reply", fake_reply)
    monkeypatch.setattr("app.api.admin.send_whatsapp_image", fake_image)
    return registro


def _conversacion(
    tenant: Tenant,
    canal: str = "whatsapp",
    nombre: str = "Ana",
    telefono: str = "573001112233",
    horas_desde_el_cliente: float = 1.0,
    estado: str = "auto",
) -> UUID:
    """Cliente + conversacion con un mensaje del cliente y uno del agente."""
    cliente_id, conv_id = uuid4(), uuid4()
    _ejecutar(
        tenant.id,
        [
            (
                "INSERT INTO customers (id, business_id, channel, channel_identifier, "
                "display_name) VALUES (:c, :b, :canal, :tel, :n)",
                {"c": cliente_id, "b": tenant.id, "canal": canal, "tel": telefono, "n": nombre},
            ),
            (
                "INSERT INTO conversations (id, business_id, customer_id, channel, status, "
                "last_message_at) VALUES (:i, :b, :c, :canal, :s, now())",
                {"i": conv_id, "b": tenant.id, "c": cliente_id, "canal": canal, "s": estado},
            ),
            (
                "INSERT INTO messages (conversation_id, business_id, role, content, created_at) "
                "VALUES (:i, :b, 'customer', 'hola, ¿tienen bolsos? 👜', "
                "now() - make_interval(secs => :h * 3600))",
                {"i": conv_id, "b": tenant.id, "h": horas_desde_el_cliente},
            ),
            (
                "INSERT INTO messages (conversation_id, business_id, role, content) "
                "VALUES (:i, :b, 'agent', 'Sí, desde 150.000')",
                {"i": conv_id, "b": tenant.id},
            ),
        ],
    )
    return conv_id


def _detalle(client: TestClient, tenant: Tenant, conv_id: UUID, **params: Any) -> Any:
    r = client.get(f"/admin/conversations/{conv_id}", headers=tenant.headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ======================================================================
# Lista y detalle
# ======================================================================
def test_la_lista_trae_cliente_ultimo_mensaje_y_no_leidos(
    client: TestClient, tenant_a: Tenant
) -> None:
    conv_id = _conversacion(tenant_a)

    lista = client.get("/admin/conversations", headers=tenant_a.headers).json()

    assert len(lista) == 1
    conv = lista[0]
    assert conv["id"] == str(conv_id)
    assert conv["customer"]["display_name"] == "Ana"
    assert conv["last_message"] == {
        "role": "agent",
        "content_type": "text",
        "content": "Sí, desde 150.000",
    }
    assert conv["unread"] == 1
    assert conv["window"]["applies"] is True
    assert conv["window"]["open"] is True


def test_el_detalle_trae_el_historial_completo_en_orden(
    client: TestClient, tenant_a: Tenant
) -> None:
    conv_id = _conversacion(tenant_a)

    detalle = _detalle(client, tenant_a, conv_id)

    assert [m["role"] for m in detalle["messages"]] == ["customer", "agent"]
    # Los emojis viajan como texto, sin tratamiento especial.
    assert detalle["messages"][0]["content"] == "hola, ¿tienen bolsos? 👜"
    assert detalle["customer"]["channel_identifier"] == "573001112233"


def test_after_seq_trae_solo_lo_nuevo(client: TestClient, tenant_a: Tenant) -> None:
    """Es lo que pide el panel en cada refresco."""
    conv_id = _conversacion(tenant_a)
    primero = _detalle(client, tenant_a, conv_id)["messages"][0]

    nuevos = _detalle(client, tenant_a, conv_id, after_seq=primero["seq"])["messages"]

    assert [m["content"] for m in nuevos] == ["Sí, desde 150.000"]


def test_marcar_leido_deja_los_no_leidos_en_cero(
    client: TestClient, tenant_a: Tenant
) -> None:
    _conversacion(tenant_a)
    conv_id = client.get("/admin/conversations", headers=tenant_a.headers).json()[0]["id"]

    r = client.post(f"/admin/conversations/{conv_id}/read", headers=tenant_a.headers)

    assert r.status_code == 204
    lista = client.get("/admin/conversations", headers=tenant_a.headers).json()
    assert lista[0]["unread"] == 0


def test_filtros_por_estado_y_busqueda(client: TestClient, tenant_a: Tenant) -> None:
    _conversacion(tenant_a, nombre="Ana", telefono="573001110001")
    humana = _conversacion(tenant_a, nombre="Bruno", telefono="573001110002", estado="human")

    esperando = client.get(
        "/admin/conversations", headers=tenant_a.headers, params={"status": "human"}
    ).json()
    por_nombre = client.get(
        "/admin/conversations", headers=tenant_a.headers, params={"search": "brun"}
    ).json()
    por_telefono = client.get(
        "/admin/conversations", headers=tenant_a.headers, params={"search": "0001"}
    ).json()

    assert [c["id"] for c in esperando] == [str(humana)]
    assert [c["customer"]["display_name"] for c in por_nombre] == ["Bruno"]
    assert [c["customer"]["display_name"] for c in por_telefono] == ["Ana"]


# ======================================================================
# Aislamiento entre negocios
# ======================================================================
def test_no_se_ve_ni_se_toca_la_conversacion_de_otro_negocio(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant, envios: dict[str, list[Any]]
) -> None:
    """Con el uuid REAL de la conversacion ajena: todo debe dar 404."""
    ajena = _conversacion(tenant_a)
    base = f"/admin/conversations/{ajena}"
    h = tenant_b.headers

    assert client.get("/admin/conversations", headers=h).json() == []
    assert client.get(base, headers=h).status_code == 404
    assert client.post(f"{base}/read", headers=h).status_code == 404
    assert client.post(f"{base}/status", headers=h, json={"status": "human"}).status_code == 404
    assert client.post(f"{base}/reply", headers=h, json={"text": "hola"}).status_code == 404
    assert envios["texto"] == [], "no debe salir nada hacia el cliente ajeno"

    # Y la conversacion sigue intacta para su dueño.
    detalle = _detalle(client, tenant_a, ajena)
    assert detalle["status"] == "auto"
    assert len(detalle["messages"]) == 2


def test_no_se_puede_bajar_la_foto_de_otro_negocio(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    conv_id = _conversacion(tenant_a, canal="sandbox")
    client.post(
        f"/admin/conversations/{conv_id}/reply-image",
        headers=tenant_a.headers,
        json={"image_base64": base64.b64encode(JPEG).decode(), "mime_type": "image/jpeg"},
    )
    media_id = _detalle(client, tenant_a, conv_id)["messages"][-1]["media_id"]

    assert client.get(f"/admin/media/{media_id}", headers=tenant_a.headers).status_code == 200
    assert client.get(f"/admin/media/{media_id}", headers=tenant_b.headers).status_code == 404


# ======================================================================
# Responder como persona
# ======================================================================
def test_responder_toma_la_conversacion(
    client: TestClient, tenant_a: Tenant, envios: dict[str, list[Any]]
) -> None:
    """Si el agente siguiera activo, le contestaria al cliente encima de la persona."""
    conv_id = _conversacion(tenant_a)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply",
        headers=tenant_a.headers,
        json={"text": "Hola Ana, te ayudo yo 😊"},
    )

    assert r.status_code == 200
    detalle = _detalle(client, tenant_a, conv_id)
    assert detalle["status"] == "human"
    assert detalle["messages"][-1]["role"] == "human"
    assert detalle["messages"][-1]["content"] == "Hola Ana, te ayudo yo 😊"
    assert envios["texto"][0][0].text == "Hola Ana, te ayudo yo 😊"


def test_se_envia_desde_el_numero_del_negocio(
    client: TestClient, tenant_a: Tenant, envios: dict[str, list[Any]]
) -> None:
    """Cada negocio responde desde su numero, no desde el global de la config."""
    _ejecutar(
        tenant_a.id,
        [
            (
                "UPDATE businesses SET whatsapp_phone_number_id = :n WHERE id = :i",
                {"n": f"PN-{tenant_a.id}", "i": tenant_a.id},
            )
        ],
    )
    conv_id = _conversacion(tenant_a)

    client.post(
        f"/admin/conversations/{conv_id}/reply", headers=tenant_a.headers, json={"text": "hola"}
    )

    _, phone_number_id = envios["texto"][0]
    assert phone_number_id == f"PN-{tenant_a.id}"


def test_devolver_al_agente_cierra_el_escalamiento(
    client: TestClient, tenant_a: Tenant, envios: dict[str, list[Any]]
) -> None:
    conv_id = _conversacion(tenant_a, estado="human")
    _ejecutar(
        tenant_a.id,
        [
            (
                "INSERT INTO escalations (business_id, conversation_id, customer_id, reason) "
                "SELECT business_id, id, customer_id, 'pidió una persona' "
                "FROM conversations WHERE id = :c",
                {"c": conv_id},
            )
        ],
    )
    assert _detalle(client, tenant_a, conv_id)["open_escalation"] is not None

    client.post(
        f"/admin/conversations/{conv_id}/reply",
        headers=tenant_a.headers,
        json={"text": "Listo, ya quedó", "return_to_auto": True},
    )

    detalle = _detalle(client, tenant_a, conv_id)
    assert detalle["status"] == "auto"
    assert detalle["open_escalation"] is None
    assert client.get("/admin/escalations", headers=tenant_a.headers).json() == []


def test_tomar_y_devolver_sin_escribir(client: TestClient, tenant_a: Tenant) -> None:
    conv_id = _conversacion(tenant_a)
    url = f"/admin/conversations/{conv_id}/status"

    assert client.post(url, headers=tenant_a.headers, json={"status": "human"}).status_code == 200
    assert _detalle(client, tenant_a, conv_id)["status"] == "human"

    assert client.post(url, headers=tenant_a.headers, json={"status": "auto"}).status_code == 200
    assert _detalle(client, tenant_a, conv_id)["status"] == "auto"


def test_con_la_ventana_de_24h_cerrada_no_se_envia(
    client: TestClient, tenant_a: Tenant, envios: dict[str, list[Any]]
) -> None:
    """WhatsApp lo rechazaria con un error generico; se bloquea antes, con motivo claro."""
    conv_id = _conversacion(tenant_a, horas_desde_el_cliente=25)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply", headers=tenant_a.headers, json={"text": "hola"}
    )

    assert r.status_code == 409
    assert "24 horas" in r.json()["detail"]
    assert envios["texto"] == []
    assert _detalle(client, tenant_a, conv_id)["window"]["open"] is False


def test_si_whatsapp_rechaza_el_mensaje_no_queda_en_el_historial(
    client: TestClient, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El historial no debe mostrar un mensaje que el cliente nunca recibio."""

    async def fake_reply(msg: Any, phone_number_id: str | None = None) -> list[str]:
        raise WhatsAppError("numero invalido")

    monkeypatch.setattr("app.api.admin.send_whatsapp_reply", fake_reply)
    conv_id = _conversacion(tenant_a)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply", headers=tenant_a.headers, json={"text": "hola"}
    )

    assert r.status_code == 502
    detalle = _detalle(client, tenant_a, conv_id)
    assert [m["role"] for m in detalle["messages"]] == ["customer", "agent"]
    assert detalle["status"] == "auto"


def test_mensaje_vacio_se_rechaza(client: TestClient, tenant_a: Tenant) -> None:
    conv_id = _conversacion(tenant_a)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply", headers=tenant_a.headers, json={"text": "  "}
    )

    assert r.status_code == 422


# ======================================================================
# Imagenes
# ======================================================================
def test_responder_con_imagen_la_envia_y_la_guarda(
    client: TestClient, tenant_a: Tenant, envios: dict[str, list[Any]]
) -> None:
    conv_id = _conversacion(tenant_a)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply-image",
        headers=tenant_a.headers,
        json={
            "image_base64": base64.b64encode(JPEG).decode(),
            "mime_type": "image/jpeg",
            "caption": "Este es el modelo 👜",
        },
    )

    assert r.status_code == 200, r.text
    to, data, mime, kwargs = envios["imagen"][0]
    assert (to, data, mime) == ("573001112233", JPEG, "image/jpeg")
    assert kwargs["caption"] == "Este es el modelo 👜"

    ultimo = _detalle(client, tenant_a, conv_id)["messages"][-1]
    assert ultimo["role"] == "human"
    assert ultimo["content_type"] == "image"
    assert ultimo["content"] == "Este es el modelo 👜"

    archivo = client.get(f"/admin/media/{ultimo['media_id']}", headers=tenant_a.headers)
    assert archivo.content == JPEG
    assert archivo.headers["content-type"] == "image/jpeg"
    assert "private" in archivo.headers["cache-control"]


@pytest.mark.parametrize(
    ("contenido", "mime"),
    [
        ("esto-no-es-base64!!", "image/jpeg"),
        (base64.b64encode(b"GIF89a").decode(), "image/gif"),
        ("", "image/png"),
    ],
    ids=["base64-invalido", "formato-no-soportado", "vacia"],
)
def test_imagen_invalida_se_rechaza_sin_enviar(
    client: TestClient,
    tenant_a: Tenant,
    envios: dict[str, list[Any]],
    contenido: str,
    mime: str,
) -> None:
    conv_id = _conversacion(tenant_a)

    r = client.post(
        f"/admin/conversations/{conv_id}/reply-image",
        headers=tenant_a.headers,
        json={"image_base64": contenido, "mime_type": mime},
    )

    assert r.status_code == 422
    assert envios["imagen"] == []


def test_la_foto_se_borra_con_los_datos_del_cliente(
    client: TestClient, tenant_a: Tenant
) -> None:
    """Ley 1581: borrar al cliente se lleva tambien sus fotos (cascada)."""
    conv_id = _conversacion(tenant_a, canal="sandbox", telefono="sandbox-borrar")
    client.post(
        f"/admin/conversations/{conv_id}/reply-image",
        headers=tenant_a.headers,
        json={"image_base64": base64.b64encode(JPEG).decode(), "mime_type": "image/jpeg"},
    )
    media_id = _detalle(client, tenant_a, conv_id)["messages"][-1]["media_id"]

    r = client.post(
        "/admin/privacy/delete-customer",
        headers=tenant_a.headers,
        json={"channel_identifier": "sandbox-borrar"},
    )

    assert r.status_code == 200
    assert client.get(f"/admin/media/{media_id}", headers=tenant_a.headers).status_code == 404
