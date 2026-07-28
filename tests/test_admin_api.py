"""
API admin: autenticacion, CRUD, bandejas y aislamiento entre tenants (fase 6).

El criterio central: un admin del negocio A no puede alcanzar NADA del negocio B,
ni conociendo los uuid.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.admin import generate_token
from app.main import app
from tests.conftest import TEST_DATABASE_URL, requires_db

pytestmark = [requires_db]


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _ejecutar(business_id: UUID, sentencias: list[tuple[str, dict[str, Any]]]) -> None:
    """Escribe con un engine efimero en su propio loop (ver test_webhook)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    async def _run() -> None:
        engine = create_async_engine(str(TEST_DATABASE_URL), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT set_config('app.current_business_id', :b, false)"),
                    {"b": str(business_id)},
                )
                for sql, params in sentencias:
                    await conn.execute(text(sql), params)
        finally:
            await engine.dispose()

    asyncio.run(_run())


class Tenant:
    """Un negocio de prueba con su token del panel."""

    def __init__(self, nombre: str) -> None:
        self.id = uuid4()
        self.token, token_hash, prefijo = generate_token()
        admin_id = uuid4()

        _ejecutar(
            self.id,
            [
                (
                    "INSERT INTO businesses (id, name) VALUES (:i, :n)",
                    {"i": self.id, "n": nombre},
                ),
                (
                    "INSERT INTO admin_users (id, business_id, email, role) "
                    "VALUES (:a, :i, :e, 'owner')",
                    {"a": admin_id, "i": self.id, "e": f"{nombre}@test.local"},
                ),
                (
                    "INSERT INTO admin_tokens "
                    "(business_id, admin_user_id, token_hash, token_prefix) "
                    "VALUES (:i, :a, :h, :p)",
                    {"i": self.id, "a": admin_id, "h": token_hash, "p": prefijo},
                ),
            ],
        )

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def borrar(self) -> None:
        _ejecutar(self.id, [("DELETE FROM businesses WHERE id = :i", {"i": self.id})])


@pytest.fixture
def tenant_a() -> Iterator[Tenant]:
    t = Tenant("Negocio A")
    yield t
    t.borrar()


@pytest.fixture
def tenant_b() -> Iterator[Tenant]:
    t = Tenant("Negocio B")
    yield t
    t.borrar()


# ======================================================================
# Autenticacion
# ======================================================================
def test_sin_token_da_401(client: TestClient) -> None:
    assert client.get("/admin/business").status_code == 401


def test_token_invalido_da_401(client: TestClient) -> None:
    r = client.get("/admin/business", headers={"Authorization": "Bearer asa_inventado"})
    assert r.status_code == 401


def test_token_valido_devuelve_el_negocio(client: TestClient, tenant_a: Tenant) -> None:
    r = client.get("/admin/business", headers=tenant_a.headers)

    assert r.status_code == 200
    assert r.json()["id"] == str(tenant_a.id)


# ======================================================================
# Aislamiento entre tenants (criterio de la fase)
# ======================================================================
def test_cada_token_solo_ve_su_negocio(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = client.get("/admin/business", headers=tenant_a.headers).json()
    b = client.get("/admin/business", headers=tenant_b.headers).json()

    assert a["id"] == str(tenant_a.id)
    assert b["id"] == str(tenant_b.id)
    assert a["id"] != b["id"]


def test_no_se_puede_editar_un_agente_de_otro_tenant(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Con el uuid REAL del agente ajeno: debe dar 404, no editarlo."""
    creado = client.post(
        "/admin/agents",
        headers=tenant_b.headers,
        json={
            "name": "Agente de B",
            "routing_description": "hace cosas de B",
            "instructions_md": "# B",
        },
    ).json()

    r = client.patch(
        f"/admin/agents/{creado['id']}",
        headers=tenant_a.headers,
        json={"name": "secuestrado"},
    )

    assert r.status_code == 404

    sigue = client.get("/admin/agents", headers=tenant_b.headers).json()
    assert sigue[0]["name"] == "Agente de B"


def test_los_agentes_de_otro_tenant_no_se_listan(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    client.post(
        "/admin/agents",
        headers=tenant_b.headers,
        json={
            "name": "Solo de B",
            "routing_description": "cosas de B",
            "instructions_md": "# B",
        },
    )

    assert client.get("/admin/agents", headers=tenant_a.headers).json() == []


def test_no_se_borra_conocimiento_de_otro_tenant(
    client: TestClient, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    doc = client.post(
        "/admin/knowledge",
        headers=tenant_b.headers,
        json={"title": "Secreto de B", "raw_content": "...", "confirmed_public": True},
    ).json()

    r = client.delete(f"/admin/knowledge/{doc['id']}", headers=tenant_a.headers)
    assert r.status_code == 404

    assert len(client.get("/admin/knowledge", headers=tenant_b.headers).json()) == 1


# ======================================================================
# Agentes (RF-20, RF-06)
# ======================================================================
def test_crear_agente_es_un_insert(client: TestClient, tenant_a: Tenant) -> None:
    """RF-06: agregar un agente no requiere tocar código ni redeploy."""
    r = client.post(
        "/admin/agents",
        headers=tenant_a.headers,
        json={
            "name": "Agendador",
            "routing_description": "agenda citas del salón",
            "instructions_md": "# Rol\nTomás citas.",
            "allowed_tools": ["capture_request"],
        },
    )

    assert r.status_code == 201
    assert r.json()["name"] == "Agendador"
    assert r.json()["allowed_tools"] == ["capture_request"]


def test_no_se_acepta_una_herramienta_inexistente(
    client: TestClient, tenant_a: Tenant
) -> None:
    """Si no se valida, falla recién en runtime con el cliente esperando."""
    r = client.post(
        "/admin/agents",
        headers=tenant_a.headers,
        json={
            "name": "X",
            "routing_description": "y",
            "instructions_md": "z",
            "allowed_tools": ["borrar_base_de_datos"],
        },
    )

    assert r.status_code == 422
    assert "borrar_base_de_datos" in r.text


def test_desactivar_un_agente_lo_saca_del_listado(
    client: TestClient, tenant_a: Tenant
) -> None:
    creado = client.post(
        "/admin/agents",
        headers=tenant_a.headers,
        json={"name": "Temporal", "routing_description": "x", "instructions_md": "y"},
    ).json()

    client.patch(
        f"/admin/agents/{creado['id']}", headers=tenant_a.headers, json={"status": "inactive"}
    )

    assert client.get("/admin/agents", headers=tenant_a.headers).json() == []
    con_inactivos = client.get(
        "/admin/agents?include_inactive=true", headers=tenant_a.headers
    ).json()
    assert len(con_inactivos) == 1


# ======================================================================
# Conocimiento (RF-19)
# ======================================================================
def test_subir_conocimiento_exige_confirmacion_de_curacion(
    client: TestClient, tenant_a: Tenant
) -> None:
    """
    Regla de curación: el agente lee el conocimiento COMPLETO y puede citarlo
    textualmente. Subir algo interno sin darse cuenta lo filtra al cliente.
    """
    r = client.post(
        "/admin/knowledge",
        headers=tenant_a.headers,
        json={
            "title": "Costos internos",
            "raw_content": "margen 60%",
            "confirmed_public": False,
        },
    )

    assert r.status_code == 422
    assert "cliente" in r.text.lower()


def test_el_conocimiento_confirmado_se_guarda(client: TestClient, tenant_a: Tenant) -> None:
    r = client.post(
        "/admin/knowledge",
        headers=tenant_a.headers,
        json={
            "title": "Horarios",
            "raw_content": "Lunes a viernes 8-18",
            "confirmed_public": True,
        },
    )

    assert r.status_code == 201
    docs = client.get("/admin/knowledge", headers=tenant_a.headers).json()
    assert docs[0]["title"] == "Horarios"


def test_el_conocimiento_llega_al_contexto_del_negocio(
    client: TestClient, tenant_a: Tenant
) -> None:
    """Lo que se sube debe aparecer en el prefijo que ve el agente."""
    client.post(
        "/admin/knowledge",
        headers=tenant_a.headers,
        json={
            "title": "Precios",
            "raw_content": "Corte: 30.000",
            "confirmed_public": True,
        },
    )

    negocio = client.get("/admin/business", headers=tenant_a.headers).json()
    assert "Corte: 30.000" in (negocio["knowledge_context"] or "")


# ======================================================================
# Bandejas y metricas
# ======================================================================
def test_bandejas_vacias_no_fallan(client: TestClient, tenant_a: Tenant) -> None:
    assert client.get("/admin/requests", headers=tenant_a.headers).json() == []
    assert client.get("/admin/escalations", headers=tenant_a.headers).json() == []


def test_metricas_sin_datos(client: TestClient, tenant_a: Tenant) -> None:
    r = client.get("/admin/metrics", headers=tenant_a.headers)

    assert r.status_code == 200
    datos = r.json()
    assert datos["turnos"] == 0
    assert datos["conversaciones"] == 0


def test_estado_de_solicitud_invalido_se_rechaza(
    client: TestClient, tenant_a: Tenant
) -> None:
    r = client.patch(
        f"/admin/requests/{uuid4()}", headers=tenant_a.headers, json={"status": "inventado"}
    )
    assert r.status_code == 422


# ======================================================================
# Sandbox (RF-23)
# ======================================================================
def test_el_sandbox_responde_sin_tocar_whatsapp(
    client: TestClient, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Corre el MISMO orquestador con un canal falso: lo que se ve acá es lo que
    pasaría en producción, pero sin teléfono.
    """
    from app.core.llm import AgentTurnResult, ClassifyResult
    from app.schemas import Intent, TurnUsage

    async def fake_classify(*args: Any, **kwargs: Any) -> ClassifyResult:
        agentes = args[2] if len(args) > 2 else kwargs.get("agents", [])
        return ClassifyResult(
            intent=Intent.INFORM,
            agent_id=str(agentes[0].id) if agentes else None,
            usage=TurnUsage(model_calls=1),
        )

    async def fake_turn(**kwargs: Any) -> AgentTurnResult:
        return AgentTurnResult(
            reply_text="Atendemos de 8 a 18.", usage=TurnUsage(model_calls=1)
        )

    enviados: list[Any] = []

    async def fake_send(msg: Any, **kwargs: Any) -> list[str]:
        enviados.append(msg)
        return []

    monkeypatch.setattr("app.core.orchestrator.classify", fake_classify)
    monkeypatch.setattr("app.core.orchestrator.run_agent_turn", fake_turn)
    monkeypatch.setattr("app.core.orchestrator.send_whatsapp_reply", fake_send)

    client.post(
        "/admin/agents",
        headers=tenant_a.headers,
        json={
            "name": "Informador",
            "routing_description": "responde horarios y precios",
            "instructions_md": "# Rol",
        },
    )

    r = client.post(
        "/admin/sandbox/message", headers=tenant_a.headers, json={"text": "¿horarios?"}
    )

    assert r.status_code == 200
    datos = r.json()
    assert datos["reply_text"] == "Atendemos de 8 a 18."
    assert datos["detected_intent"] == "inform"
    assert datos["routed_agent"] == "Informador"
    assert enviados == [], "el sandbox no debe enviar nada por WhatsApp"


def test_el_sandbox_expone_la_traza_para_depurar(
    client: TestClient, tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HU-ADM-05: sin ver intención y herramientas no se puede depurar."""
    from app.core.llm import AgentTurnResult, ClassifyResult
    from app.schemas import Intent, TurnUsage

    async def fake_classify(*args: Any, **kwargs: Any) -> ClassifyResult:
        return ClassifyResult(intent=Intent.GREET, usage=TurnUsage(model_calls=1))

    async def fake_turn(**kwargs: Any) -> AgentTurnResult:
        return AgentTurnResult(
            reply_text="¡Hola!",
            usage=TurnUsage(
                model_calls=1, input_tokens=120, output_tokens=8, cache_read_tokens=100
            ),
        )

    monkeypatch.setattr("app.core.orchestrator.classify", fake_classify)
    monkeypatch.setattr("app.core.orchestrator.run_agent_turn", fake_turn)

    r = client.post(
        "/admin/sandbox/message", headers=tenant_a.headers, json={"text": "hola"}
    ).json()

    assert r["detected_intent"] == "greet"
    assert r["cache_read_tokens"] == 100
    assert r["latency_ms"] is not None
