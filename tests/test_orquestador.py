"""
Orquestador: los 7 flujos F1-F7 con el LLM mockeado (fase 5).

Cada test verifica las ESCRITURAS en la base segun el mapeo tabla<->flujo de
docs/MVP_Estructura_Base_de_Datos.md §5, no solo el texto de la respuesta.

La base es real; lo unico simulado es la API de Anthropic y el envio a WhatsApp.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.llm import AgentTurnResult, ClassifyResult, LLMError
from app.core.orchestrator import handle_inbound_message
from app.db.repositories import AgentRepo
from app.db.session import set_tenant, system_session
from app.schemas import InboundMessage, Intent, TurnUsage
from tests.conftest import requires_db

pytestmark = [requires_db]


# ======================================================================
# Infraestructura de los tests
# ======================================================================
@pytest.fixture
def mock_llm(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """
    Sustituye la capa LLM y el envio saliente.

    Devuelve un dict configurable: cada test define que intencion "detecta" el
    clasificador y que responde el agente.
    """
    estado: dict[str, Any] = {
        "intent": Intent.INFORM,
        "agent_id": None,
        "reply": "Respuesta de prueba.",
        "tools_used": [],
        "raise_llm_error": False,
        "enviados": [],
        "classify_calls": 0,
    }

    async def fake_classify(message: str, ctx: Any, agents: list[Any]) -> ClassifyResult:
        estado["classify_calls"] += 1
        if estado["raise_llm_error"]:
            raise LLMError("fallo simulado del modelo")
        agent_id = estado["agent_id"]
        if agent_id is None and agents and estado["intent"] in (Intent.INFORM, Intent.ACT):
            agent_id = str(agents[0].id)
        return ClassifyResult(
            intent=estado["intent"],
            agent_id=agent_id,
            usage=TurnUsage(model_calls=1, input_tokens=100, output_tokens=10),
        )

    async def fake_run_agent_turn(**kwargs: Any) -> AgentTurnResult:
        if estado["raise_llm_error"]:
            raise LLMError("fallo simulado del modelo")
        return AgentTurnResult(
            reply_text=estado["reply"],
            tools_used=list(estado["tools_used"]),
            usage=TurnUsage(
                model_calls=1,
                input_tokens=500,
                output_tokens=50,
                cache_read_tokens=400,
                cost_estimate_usd=0.001,
            ),
        )

    async def fake_send(msg: Any, **kwargs: Any) -> list[str]:
        estado["enviados"].append(msg.text)
        return ["wamid.OUT"]

    monkeypatch.setattr("app.core.orchestrator.classify", fake_classify)
    monkeypatch.setattr("app.core.orchestrator.run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr("app.core.orchestrator.send_whatsapp_reply", fake_send)
    return estado


@pytest_asyncio.fixture
async def negocio() -> AsyncIterator[UUID]:
    """
    Negocio con un agente activo, creado con el engine GLOBAL de la app.

    Importa que sea el mismo engine que usa el orquestador: cada test corre en su
    propio event loop y mezclar engines deja las escrituras invisibles entre si
    ("attached to a different loop"). Se limpia al final porque estas escrituras
    van con commit real, no dentro de una transaccion con rollback.
    """
    business_id = uuid4()

    async with system_session() as s:
        await set_tenant(s, business_id)
        await s.execute(
            text("INSERT INTO businesses (id, name, notify_channel) VALUES (:i, 'Demo', 'email')"),
            {"i": business_id},
        )
        await AgentRepo(s).create(
            business_id,
            name="Agendador",
            routing_description="agenda citas",
            instructions_md="# Rol",
            allowed_tools=["capture_request", "notify_owner"],
        )

    try:
        yield business_id
    finally:
        async with system_session() as s:
            await set_tenant(s, business_id)
            await s.execute(
                text("DELETE FROM businesses WHERE id = :i"), {"i": business_id}
            )
        # Ojo: NO se llama a dispose_engine(). El engine es global y compartido;
        # cerrarlo aca deja al test siguiente con conexiones de un loop muerto.
        # El pool se limpia solo al terminar la sesion de pytest.


async def _consultar(business_id: UUID, sql: str) -> list[Any]:
    """Lee con el engine global (el mismo que escribio el orquestador)."""
    async with system_session() as s:
        await set_tenant(s, business_id)
        return list((await s.execute(text(sql), {"b": business_id})).all())


def _mensaje(business_id: UUID, texto: str, telefono: str = "573001112233") -> InboundMessage:
    return InboundMessage(
        business_id=business_id,
        channel="whatsapp",
        channel_identifier=telefono,
        text=texto,
        external_message_id=f"wamid.{uuid4()}",
        received_at=datetime.now(UTC),
    )


# ======================================================================
# F3 - GREET
# ======================================================================
async def test_f3_greet_responde_sin_herramientas(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.GREET
    mock_llm["reply"] = "¡Hola! ¿En qué te ayudo?"

    await handle_inbound_message(_mensaje(negocio, "hola"))

    filas = await _consultar(
        negocio, "SELECT detected_intent, tools_used FROM turn_logs WHERE business_id = :b"
    )
    assert filas[0].detected_intent == "greet"
    assert filas[0].tools_used == []
    # En el primer contacto la respuesta lleva anexado el aviso de tratamiento
    # de datos (RNF-05), asi que se compara el inicio, no la igualdad exacta.
    assert mock_llm["enviados"][0].startswith("¡Hola! ¿En qué te ayudo?")


# ======================================================================
# F1 - INFORM
# ======================================================================
async def test_f1_inform_responde_y_registra_el_turno(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = "Atendemos de 8 a 18."

    await handle_inbound_message(_mensaje(negocio, "¿qué horarios tienen?"))

    filas = await _consultar(
        negocio,
        "SELECT detected_intent, cache_read_tokens, input_tokens FROM turn_logs "
        "WHERE business_id = :b",
    )
    assert filas[0].detected_intent == "inform"
    # RNF-02: el ahorro de caché se registra SIEMPRE.
    assert filas[0].cache_read_tokens == 400


async def test_f1_guarda_ambos_mensajes_del_turno(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """RF-15/RF-16: entrada y salida quedan en el historial, en orden."""
    mock_llm["reply"] = "Atendemos de 8 a 18."

    await handle_inbound_message(_mensaje(negocio, "¿horarios?"))

    filas = await _consultar(
        negocio,
        "SELECT role, content FROM messages WHERE business_id = :b ORDER BY seq",
    )
    assert [f.role for f in filas] == ["customer", "agent"]
    assert filas[0].content == "¿horarios?"
    assert filas[1].content == "Atendemos de 8 a 18."


# ======================================================================
# F2 - ACT
# ======================================================================
async def test_f2_act_usa_herramientas_y_las_registra(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.ACT
    mock_llm["tools_used"] = ["capture_request", "notify_owner"]
    mock_llm["reply"] = "Listo, registré tu solicitud."

    await handle_inbound_message(_mensaje(negocio, "quiero una cita el martes"))

    filas = await _consultar(
        negocio,
        "SELECT detected_intent, tools_used FROM turn_logs WHERE business_id = :b",
    )
    assert filas[0].detected_intent == "act"
    assert set(filas[0].tools_used) == {"capture_request", "notify_owner"}


async def test_f2_al_capturar_se_limpia_el_flujo(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Si el flujo no se limpia, el próximo mensaje entra por el atajo CONTINUE y
    la conversación queda pegada en un flujo ya terminado.
    """
    mock_llm["intent"] = Intent.ACT
    mock_llm["tools_used"] = ["capture_request"]

    await handle_inbound_message(_mensaje(negocio, "quiero una cita"))

    filas = await _consultar(
        negocio,
        "SELECT flow_state FROM conversations WHERE business_id = :b",
    )
    assert filas[0].flow_state == {}


# ======================================================================
# F4 - CLARIFY
# ======================================================================
async def test_f4_clarify_guarda_estado_del_flujo(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.CLARIFY
    mock_llm["agent_id"] = None
    mock_llm["reply"] = "¿Para qué servicio querés la cita?"

    await handle_inbound_message(_mensaje(negocio, "quiero algo"))

    filas = await _consultar(
        negocio,
        "SELECT detected_intent FROM turn_logs WHERE business_id = :b",
    )
    assert filas[0].detected_intent == "clarify"


# ======================================================================
# F5 - CONTINUE (criterio de la fase)
# ======================================================================
async def test_f5_continue_no_reclasifica(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Dos mensajes seguidos completan una solicitud, y el segundo NO pasa por el
    clasificador: ahorra una llamada a Haiku y evita perder el hilo.
    """
    mock_llm["intent"] = Intent.ACT
    mock_llm["reply"] = "¿Qué día te queda bien?"

    await handle_inbound_message(_mensaje(negocio, "quiero una cita"))
    llamadas_tras_primero = mock_llm["classify_calls"]

    # El primer turno dejó flujo abierto y agente activo.
    filas = await _consultar(
        negocio,
        "SELECT flow_state, active_agent_id FROM conversations WHERE business_id = :b",
    )
    assert filas[0].flow_state, "el primer turno debió dejar flujo abierto"
    assert filas[0].active_agent_id is not None

    mock_llm["reply"] = "Perfecto, el martes."
    await handle_inbound_message(_mensaje(negocio, "el martes"))

    assert mock_llm["classify_calls"] == llamadas_tras_primero, (
        "el segundo mensaje se re-clasificó en vez de continuar el flujo"
    )

    filas = await _consultar(
        negocio,
        "SELECT detected_intent FROM turn_logs WHERE business_id = :b ORDER BY created_at",
    )
    assert filas[-1].detected_intent == "continue"


# ======================================================================
# F6 - ESCALATE (criterio de la fase)
# ======================================================================
async def test_f6_escalate_pasa_a_modo_humano_y_registra(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.ESCALATE

    await handle_inbound_message(_mensaje(negocio, "quiero hablar con una persona"))

    conv = await _consultar(
        negocio,
        "SELECT status FROM conversations WHERE business_id = :b",
    )
    assert conv[0].status == "human", "la conversación debe quedar en modo humano"

    esc = await _consultar(
        negocio,
        "SELECT status, reason FROM escalations WHERE business_id = :b",
    )
    assert len(esc) == 1
    assert esc[0].status == "open"


async def test_f6_en_modo_humano_el_agente_calla(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """RF-12: con status='human' no se genera respuesta automática."""
    mock_llm["intent"] = Intent.ESCALATE
    await handle_inbound_message(_mensaje(negocio, "quiero una persona"))

    enviados_antes = len(mock_llm["enviados"])
    turnos_antes = len(
        await _consultar(negocio, "SELECT id FROM turn_logs WHERE business_id = :b")
    )

    await handle_inbound_message(_mensaje(negocio, "hola? sigue ahí?"))

    assert len(mock_llm["enviados"]) == enviados_antes, "el agente respondió en modo humano"
    turnos_despues = len(
        await _consultar(negocio, "SELECT id FROM turn_logs WHERE business_id = :b")
    )
    assert turnos_despues == turnos_antes


# ======================================================================
# F7 - FALLBACK
# ======================================================================
async def test_f7_sin_agentes_responde_honesto_y_escala(
    mock_llm: dict[str, Any]
) -> None:
    """Un negocio sin agentes no debe inventar respuestas."""
    business_id = uuid4()
    async with system_session() as s:
        await set_tenant(s, business_id)
        await s.execute(
            text("INSERT INTO businesses (id, name) VALUES (:i, 'Sin Agentes')"),
            {"i": business_id},
        )

    mock_llm["intent"] = Intent.FALLBACK
    await handle_inbound_message(_mensaje(business_id, "algo raro"))

    filas = await _consultar(
        business_id,
        "SELECT detected_intent FROM turn_logs WHERE business_id = :b",
    )
    assert filas[0].detected_intent == "fallback"
    assert mock_llm["enviados"], "el cliente debe recibir una respuesta honesta"


# ======================================================================
# Resiliencia (RNF-10)
# ======================================================================
async def test_un_fallo_del_llm_no_deja_al_cliente_sin_respuesta(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["raise_llm_error"] = True

    await handle_inbound_message(_mensaje(negocio, "hola"))

    assert mock_llm["enviados"], "el cliente quedó sin respuesta ante un fallo del modelo"
    assert "problema" in mock_llm["enviados"][0].lower()


async def test_el_turno_se_loguea_aunque_falle(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """RF-15: sin traza no hay forma de medir ni diagnosticar."""
    mock_llm["raise_llm_error"] = True

    await handle_inbound_message(_mensaje(negocio, "hola"))

    filas = await _consultar(
        negocio,
        "SELECT id FROM turn_logs WHERE business_id = :b",
    )
    assert len(filas) == 1


async def test_tras_varios_fallos_seguidos_escala_solo(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Si el agente falla una y otra vez, no puede dejar al cliente en un bucle:
    escala sin que tenga que pedirlo.
    """
    mock_llm["raise_llm_error"] = True

    for _ in range(3):
        await handle_inbound_message(_mensaje(negocio, "hola"))

    conv = await _consultar(
        negocio,
        "SELECT status FROM conversations WHERE business_id = :b",
    )
    assert conv[0].status == "human", "tras 3 fallos seguidos debió escalar"


# ======================================================================
# Aislamiento en el loop completo (RF-24)
# ======================================================================
async def test_el_turno_no_toca_datos_de_otro_negocio(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    otro = uuid4()
    async with system_session() as s:
        await set_tenant(s, otro)
        await s.execute(
            text("INSERT INTO businesses (id, name) VALUES (:i, 'Otro')"), {"i": otro}
        )

    await handle_inbound_message(_mensaje(negocio, "hola"))

    ajenos = await _consultar(
        otro,
        "SELECT id FROM messages WHERE business_id = :b",
    )
    assert ajenos == [], "el turno escribió en otro tenant"
