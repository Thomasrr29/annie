"""
Capa LLM: ensamblado del prompt, caché, tiering y allowlist (fase 4).

Todo con el cliente Anthropic mockeado: no se llama a la API real.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.core.llm import (
    _parse_classification,
    build_messages,
    build_system_prefix,
    estimate_cost,
    run_agent_turn,
)
from app.core.tools import TOOL_SCHEMAS, tool_schemas_for
from app.schemas import (
    AgentConfig,
    BusinessConfig,
    ConversationContext,
    ConversationStatus,
    ConversationTurn,
    Intent,
    MessageRole,
)


def _business(knowledge: str | None = "Horarios: 8 a 18.") -> BusinessConfig:
    return BusinessConfig(
        id=uuid4(),
        name="Peluquería Demo",
        persona_tone="Cercano y breve.",
        language="es",
        knowledge_context=knowledge,
    )


def _agent(tools: list[str] | None = None) -> AgentConfig:
    return AgentConfig(
        id=uuid4(),
        business_id=uuid4(),
        name="Agendador",
        routing_description="agenda citas",
        instructions_md="# Rol\nTomás citas.",
        allowed_tools=tools or [],
        response_style="Cordial.",
    )


def _ctx(
    turnos: list[ConversationTurn] | None = None,
    flow: dict[str, Any] | None = None,
) -> ConversationContext:
    return ConversationContext(
        conversation_id=uuid4(),
        customer_id=uuid4(),
        status=ConversationStatus.AUTO,
        flow_state=flow or {},
        recent_turns=turnos or [],
    )


def _turno(rol: MessageRole, texto: str) -> ConversationTurn:
    return ConversationTurn(role=rol, content=texto, created_at=datetime.now(UTC))


# ======================================================================
# Prefijo cacheable (RNF-02) — el criterio central de la fase
# ======================================================================
def test_el_prefijo_lleva_el_breakpoint_de_cache() -> None:
    prefijo = build_system_prefix(_agent(), _business())

    assert len(prefijo) == 1
    assert prefijo[-1]["cache_control"] == {"type": "ephemeral"}


def test_el_prefijo_es_byte_identico_entre_turnos() -> None:
    """
    Si el prefijo cambia entre turnos, la caché no sirve y el costo por
    conversación se dispara. Es EL requisito de RNF-02.
    """
    agente, negocio = _agent(), _business()

    primero = build_system_prefix(agente, negocio)
    segundo = build_system_prefix(agente, negocio)

    assert primero[0]["text"] == segundo[0]["text"]


def test_el_prefijo_no_depende_del_contexto_de_conversacion() -> None:
    """
    Dos conversaciones distintas del mismo negocio comparten prefijo: es lo que
    permite que la caché se reuse entre clientes.
    """
    agente, negocio = _agent(), _business()

    a = build_system_prefix(agente, negocio)
    b = build_system_prefix(agente, negocio)

    assert a[0]["text"] == b[0]["text"]


def test_el_prefijo_no_contiene_nada_dinamico() -> None:
    """Timestamps o ids de sesión en el prefijo invalidarían la caché."""
    ctx = _ctx([_turno(MessageRole.CUSTOMER, "hola")])
    texto = build_system_prefix(_agent(), _business())[0]["text"]

    assert str(ctx.conversation_id) not in texto
    assert str(ctx.customer_id) not in texto
    assert str(datetime.now(UTC).year) not in texto


def test_el_conocimiento_va_en_el_prefijo() -> None:
    """'Base completa': el conocimiento entero va cacheado, no se busca."""
    texto = build_system_prefix(_agent(), _business("Precio corte: 30.000"))[0]["text"]

    assert "Precio corte: 30.000" in texto


def test_sin_conocimiento_no_hay_seccion_vacia() -> None:
    texto = build_system_prefix(_agent(), _business(None))[0]["text"]

    assert "Información del negocio" not in texto


def test_el_prefijo_incluye_instrucciones_y_tono() -> None:
    texto = build_system_prefix(_agent(), _business())[0]["text"]

    assert "Tomás citas" in texto
    assert "Cercano y breve" in texto
    assert "Peluquería Demo" in texto


# ======================================================================
# Parte dinámica
# ======================================================================
def test_el_historial_se_mapea_a_roles_de_la_api() -> None:
    ctx = _ctx([
        _turno(MessageRole.CUSTOMER, "hola"),
        _turno(MessageRole.AGENT, "buenas"),
    ])

    mensajes = build_messages(ctx, "quiero una cita")

    assert mensajes[0]["role"] == "user"
    assert mensajes[1]["role"] == "assistant"
    assert mensajes[-1]["content"].endswith("quiero una cita")


def test_turnos_seguidos_del_mismo_rol_se_fusionan() -> None:
    """La API rechaza dos mensajes seguidos del mismo rol."""
    ctx = _ctx([
        _turno(MessageRole.CUSTOMER, "hola"),
        _turno(MessageRole.CUSTOMER, "estás?"),
    ])

    mensajes = build_messages(ctx, "hola?")

    roles = [m["role"] for m in mensajes]
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1))


def test_el_primer_mensaje_siempre_es_del_usuario() -> None:
    """La API lo exige; un historial que arranca con el agente lo violaría."""
    ctx = _ctx([_turno(MessageRole.AGENT, "hola, en qué ayudo")])

    mensajes = build_messages(ctx, "quiero algo")

    assert mensajes[0]["role"] == "user"


def test_el_flow_state_viaja_en_la_parte_dinamica() -> None:
    """Nunca en el prefijo: cambia en cada paso del flujo (F5)."""
    ctx = _ctx(flow={"servicio": "corte"})

    mensajes = build_messages(ctx, "el martes")

    assert "corte" in mensajes[-1]["content"]
    assert "corte" not in build_system_prefix(_agent(), _business())[0]["text"]


def test_los_roles_system_y_human_se_mapean_a_assistant() -> None:
    """
    Para el modelo, lo que dijo el operador humano es algo que ya se le dijo al
    cliente: va como 'assistant'. Se antepone un turno del cliente porque la API
    exige que el historial empiece con 'user'.
    """
    ctx = _ctx([
        _turno(MessageRole.CUSTOMER, "hola?"),
        _turno(MessageRole.HUMAN, "te atiendo yo"),
    ])

    mensajes = build_messages(ctx, "gracias")

    assert [m["role"] for m in mensajes] == ["user", "assistant", "user"]
    assert mensajes[1]["content"] == "te atiendo yo"


# ======================================================================
# Allowlist de herramientas (RF-07) — criterio de la fase
# ======================================================================
def test_solo_se_ofrecen_las_herramientas_permitidas() -> None:
    esquemas = tool_schemas_for(["capture_request"])

    nombres = {e["name"] for e in esquemas}
    assert nombres == {"capture_request"}
    assert "notify_owner" not in nombres


def test_una_herramienta_no_permitida_no_llega_al_request() -> None:
    """
    RF-07 se aplica no incluyéndola: el modelo ni sabe que existe.
    """
    esquemas = tool_schemas_for([])

    assert esquemas == []


def test_herramientas_inexistentes_se_ignoran() -> None:
    esquemas = tool_schemas_for(["capture_request", "herramienta_inventada"])

    assert {e["name"] for e in esquemas} == {"capture_request"}


def test_el_orden_de_las_herramientas_es_estable() -> None:
    """Van en el prefijo cacheado: reordenarlas invalidaría la caché."""
    a = [e["name"] for e in tool_schemas_for(["notify_owner", "capture_request"])]
    b = [e["name"] for e in tool_schemas_for(["capture_request", "notify_owner"])]

    assert a == b


def test_los_esquemas_no_exponen_business_id() -> None:
    """
    Aislamiento (RNF-04): el modelo no elige el tenant. Si no hay dónde ponerlo,
    no puede alucinarlo.
    """
    for esquema in TOOL_SCHEMAS.values():
        propiedades = esquema["input_schema"]["properties"]
        assert "business_id" not in propiedades
        assert "customer_id" not in propiedades
        assert "conversation_id" not in propiedades


# ======================================================================
# Parseo de la clasificación
# ======================================================================
def test_parsea_json_limpio() -> None:
    agentes = [_agent()]
    intent, agent_id = _parse_classification(
        f'{{"intent": "act", "agent_id": "{agentes[0].id}"}}', agentes
    )

    assert intent == Intent.ACT
    assert agent_id == str(agentes[0].id)


def test_parsea_json_envuelto_en_prosa() -> None:
    """El modelo a veces agrega texto alrededor del JSON."""
    intent, _ = _parse_classification(
        'Claro:\n```json\n{"intent": "greet", "agent_id": null}\n```', []
    )

    assert intent == Intent.GREET


def test_json_invalido_cae_a_clarify() -> None:
    """Ante la duda, preguntar: es lo único seguro."""
    intent, agent_id = _parse_classification("no soy json", [])

    assert intent == Intent.CLARIFY
    assert agent_id is None


def test_intencion_desconocida_cae_a_clarify() -> None:
    intent, _ = _parse_classification('{"intent": "bailar"}', [])

    assert intent == Intent.CLARIFY


def test_un_agente_inventado_se_descarta() -> None:
    """
    Aislamiento: si el modelo alucina un uuid, no debe enrutarse a un agente
    que el tenant no tiene.
    """
    agentes = [_agent()]
    intent, agent_id = _parse_classification(
        f'{{"intent": "act", "agent_id": "{uuid4()}"}}', agentes
    )

    assert intent == Intent.ACT
    assert agent_id is None, "se aceptó un agente que no pertenece al tenant"


# ======================================================================
# Costo (RNF-02)
# ======================================================================
def test_el_costo_de_haiku_es_menor_que_el_de_sonnet() -> None:
    """El tiering existe justamente por esto."""
    haiku = estimate_cost("claude-3-5-haiku-20241022", 1000, 100)
    sonnet = estimate_cost("claude-3-5-sonnet-20241022", 1000, 100)

    assert haiku < sonnet


def test_los_tokens_de_cache_son_mas_baratos() -> None:
    """Es el ahorro que persigue RNF-02."""
    sin_cache = estimate_cost("claude-3-5-sonnet-20241022", 10_000, 100)
    con_cache = estimate_cost("claude-3-5-sonnet-20241022", 0, 100, cache_read_tokens=10_000)

    assert con_cache < sin_cache


def test_costo_cero_sin_tokens() -> None:
    assert estimate_cost("claude-3-5-sonnet-20241022", 0, 0) == 0.0


# ======================================================================
# Turno del agente: forma del request (regresiones contra la API real)
# ======================================================================
class _Bloque:
    def __init__(self, **campos: Any) -> None:
        self.__dict__.update(campos)


class _Respuesta:
    def __init__(self, contenido: list[_Bloque], stop_reason: str) -> None:
        self.content = contenido
        self.stop_reason = stop_reason
        self.usage = _Bloque(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )


async def test_un_agente_sin_herramientas_no_manda_el_campo_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La API rechaza `tools: null` con un 400: el campo se omite."""
    pedidos: list[dict[str, Any]] = []

    async def fake_call(**kwargs: Any) -> _Respuesta:
        pedidos.append(kwargs)
        return _Respuesta([_Bloque(type="text", text="Abrimos a las 9.")], "end_turn")

    monkeypatch.setattr("app.core.llm._call_with_retry", fake_call)

    resultado = await run_agent_turn(_agent(), _business(), _ctx(), "¿horario?", tools=None)

    assert resultado.reply_text == "Abrimos a las 9."
    assert "tools" not in pedidos[0]


async def test_el_turno_con_herramientas_reenvia_el_contenido_sin_editar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Los bloques de thinking deben volver tal cual: editarlos invalida el turno."""
    pensamiento = _Bloque(type="thinking", thinking="", signature="firma")
    uso = _Bloque(type="tool_use", id="tu_1", name="notify_owner", input={"subject": "x"})
    respuestas = [
        _Respuesta([pensamiento, uso], "tool_use"),
        _Respuesta([_Bloque(type="text", text="Listo, le avisé.")], "end_turn"),
    ]
    pedidos: list[dict[str, Any]] = []

    async def fake_call(**kwargs: Any) -> _Respuesta:
        pedidos.append({**kwargs, "messages": list(kwargs["messages"])})
        return respuestas[len(pedidos) - 1]

    async def ejecutor(nombre: str, entrada: dict[str, Any]) -> str:
        return "ok"

    monkeypatch.setattr("app.core.llm._call_with_retry", fake_call)

    resultado = await run_agent_turn(
        _agent(["notify_owner"]),
        _business(),
        _ctx(),
        "avisale al dueño",
        tools=tool_schemas_for(["notify_owner"]),
        tool_executor=ejecutor,
    )

    assert resultado.reply_text == "Listo, le avisé."
    turno_assistant = pedidos[1]["messages"][-2]
    assert turno_assistant["role"] == "assistant"
    assert turno_assistant["content"] == [pensamiento, uso]
