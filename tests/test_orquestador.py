"""
Orquestador: los 7 flujos F1-F7 con el LLM mockeado (fase 5).

Cada test verifica las ESCRITURAS en la base segun el mapeo tabla<->flujo de
docs/MVP_Estructura_Base_de_Datos.md §5, no solo el texto de la respuesta.

La base es real; lo unico simulado es la API de Anthropic y el envio a WhatsApp.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.channel_whatsapp import WhatsAppError
from app.core.llm import AgentTurnResult, ClassifyResult, LLMError
from app.core.orchestrator import handle_inbound_message
from app.db.repositories import AgentRepo
from app.db.session import set_tenant, system_session
from app.schemas import (
    IMAGE_PLACEHOLDER,
    ContentType,
    InboundMedia,
    InboundMessage,
    Intent,
    TurnUsage,
    text_for_model,
)
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
        "en_modelo": asyncio.Event(),
    }

    async def fake_classify(message: str, ctx: Any, agents: list[Any]) -> ClassifyResult:
        estado["classify_calls"] += 1
        estado["ctx_al_clasificador"] = ctx
        estado["mensaje_al_clasificador"] = message
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
        estado["mensaje_al_modelo"] = kwargs.get("message")
        estado["ctx_al_modelo"] = kwargs.get("ctx")
        estado["ejecutor"] = kwargs.get("tool_executor")
        estado.setdefault("llamadas_turno", []).append(
            {"message": kwargs.get("message"), "tools": kwargs.get("tools")}
        )
        estado["en_modelo"].set()  # señal: el turno ya está esperando al modelo
        if estado.get("demora"):
            await asyncio.sleep(estado["demora"])  # un modelo que tarda en responder
        if estado["raise_llm_error"]:
            raise LLMError("fallo simulado del modelo")
        # "reply" puede ser una lista: una respuesta por llamada (guardia de precios).
        respuesta = estado["reply"]
        if isinstance(respuesta, list):
            respuesta = respuesta.pop(0)
        return AgentTurnResult(
            reply_text=respuesta,
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

    estado["escribiendo"] = []

    async def fake_typing(message_id: str, phone_number_id: str | None = None) -> None:
        estado["escribiendo"].append(message_id)

    monkeypatch.setattr("app.core.orchestrator.send_typing_indicator", fake_typing)
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


# ======================================================================
# Imagenes del cliente (bandeja)
# ======================================================================
def _mensaje_con_imagen(business_id: UUID, pie: str = "") -> InboundMessage:
    return InboundMessage(
        business_id=business_id,
        channel="whatsapp",
        channel_identifier="573001112233",
        text=text_for_model(ContentType.IMAGE, pie),
        external_message_id=f"wamid.{uuid4()}",
        received_at=datetime.now(UTC),
        media=InboundMedia(
            content_type=ContentType.IMAGE,
            external_media_id="MEDIA123",
            mime_type="image/jpeg",
            caption=pie,
        ),
    )


async def test_la_foto_del_cliente_se_guarda_con_su_archivo(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(media_id: str, **_: Any) -> tuple[bytes, str]:
        assert media_id == "MEDIA123"
        return b"\xff\xd8foto", "image/jpeg"

    monkeypatch.setattr("app.core.orchestrator.download_whatsapp_media", fake_download)

    await handle_inbound_message(_mensaje_con_imagen(negocio, "¿tienen este?"))

    mensajes = await _consultar(
        negocio,
        "SELECT id, role, content, content_type FROM messages "
        "WHERE business_id = :b ORDER BY seq",
    )
    assert mensajes[0].role == "customer"
    assert mensajes[0].content_type == "image"
    # Se guarda el pie tal cual; el marcador es solo para el modelo.
    assert mensajes[0].content == "¿tienen este?"

    media = await _consultar(
        negocio,
        "SELECT message_id, mime_type, size_bytes FROM message_media WHERE business_id = :b",
    )
    assert len(media) == 1
    assert media[0].message_id == mensajes[0].id
    assert media[0].size_bytes == len(b"\xff\xd8foto")


async def test_el_modelo_ve_un_marcador_y_no_la_foto(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_download(media_id: str, **_: Any) -> tuple[bytes, str]:
        return b"\xff\xd8foto", "image/jpeg"

    monkeypatch.setattr("app.core.orchestrator.download_whatsapp_media", fake_download)

    await handle_inbound_message(_mensaje_con_imagen(negocio, "este modelo"))

    assert mock_llm["mensaje_al_modelo"] == f"{IMAGE_PLACEHOLDER} este modelo"
    # El mensaje nuevo va UNA sola vez: como `message`, no también en el historial.
    assert mock_llm["ctx_al_modelo"].recent_turns == []


async def test_el_mensaje_nuevo_no_se_duplica_en_el_prompt(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Regresión (estudio/experimento_tinte.md): el historial se leía después de
    guardar el mensaje entrante y build_messages lo volvía a agregar, así que el
    modelo recibía la pregunta dos veces. Con eso confundía precios.
    """
    from app.core.llm import build_messages

    mock_llm["intent"] = Intent.INFORM
    await handle_inbound_message(_mensaje(negocio, "primera pregunta", "573005550001"))
    await handle_inbound_message(_mensaje(negocio, "¿cuánto vale el tinte?", "573005550001"))

    ctx = mock_llm["ctx_al_modelo"]
    # El historial trae el turno anterior completo, no el mensaje nuevo.
    assert [t.content for t in ctx.recent_turns][0] == "primera pregunta"
    assert all(t.content != "¿cuánto vale el tinte?" for t in ctx.recent_turns)

    prompt = build_messages(ctx, mock_llm["mensaje_al_modelo"])
    texto_total = " ".join(m["content"] for m in prompt)
    assert texto_total.count("¿cuánto vale el tinte?") == 1
    # Y el clasificador tampoco lo ve repetido en "Últimos turnos".
    assert all(
        t.content != "¿cuánto vale el tinte?" for t in mock_llm["ctx_al_clasificador"].recent_turns
    )


async def test_si_falla_la_descarga_el_mensaje_se_guarda_igual(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Perder la foto es mejor que perder el mensaje: el turno sigue."""

    async def fake_download(media_id: str, **_: Any) -> tuple[bytes, str]:
        raise WhatsAppError("la URL expiró")

    monkeypatch.setattr("app.core.orchestrator.download_whatsapp_media", fake_download)

    await handle_inbound_message(_mensaje_con_imagen(negocio))

    mensajes = await _consultar(
        negocio, "SELECT content_type FROM messages WHERE business_id = :b ORDER BY seq"
    )
    assert mensajes[0].content_type == "image"
    assert await _consultar(negocio, "SELECT id FROM message_media WHERE business_id = :b") == []
    assert mock_llm["enviados"], "el cliente debe recibir respuesta igual"


# ======================================================================
# Aislamiento entre clientes del MISMO negocio
# ======================================================================
def _textos(ctx: Any, mensaje: str = "") -> str:
    """Todo lo que el modelo ve: el historial y el mensaje nuevo, en un solo string."""
    return "\n".join([t.content for t in ctx.recent_turns] + [mensaje])


async def test_el_agente_no_ve_el_historial_de_otro_cliente(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Lo que le dijo Ana no puede aparecer en el contexto con el que se le
    responde a Bruno: ni en el clasificador ni en el agente.
    """
    await handle_inbound_message(
        _mensaje(negocio, "mi cédula es 1012345678, dato-secreto-de-ana", telefono="573001110001")
    )
    await handle_inbound_message(_mensaje(negocio, "hola, ¿qué horarios tienen?", "573001110002"))

    for ctx_clave, msg_clave in (
        ("ctx_al_clasificador", "mensaje_al_clasificador"),
        ("ctx_al_modelo", "mensaje_al_modelo"),
    ):
        visto = _textos(mock_llm[ctx_clave], mock_llm[msg_clave])
        assert "dato-secreto-de-ana" not in visto, f"{ctx_clave} vio el historial de Ana"
        assert "qué horarios tienen" in visto

    conversaciones = await _consultar(
        negocio, "SELECT DISTINCT conversation_id FROM messages WHERE business_id = :b"
    )
    assert len(conversaciones) == 2, "cada cliente debe tener su propia conversación"


async def test_cada_cliente_retoma_solo_su_propia_conversacion(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Mensajes intercalados de dos clientes no se mezclan al volver a escribir."""
    await handle_inbound_message(_mensaje(negocio, "ana-1", "573001110001"))
    await handle_inbound_message(_mensaje(negocio, "bruno-1", "573001110002"))
    await handle_inbound_message(_mensaje(negocio, "ana-2", "573001110001"))

    visto = _textos(mock_llm["ctx_al_modelo"], mock_llm["mensaje_al_modelo"])
    assert "ana-1" in visto and "ana-2" in visto
    assert "bruno-1" not in visto


async def test_el_flujo_a_medias_de_un_cliente_no_pasa_a_otro(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Los datos parciales (F4/F5) viven en la conversación, no en el negocio."""
    mock_llm["intent"] = Intent.CLARIFY
    await handle_inbound_message(_mensaje(negocio, "quiero agendar", "573001110001"))

    mock_llm["intent"] = Intent.GREET
    await handle_inbound_message(_mensaje(negocio, "hola", "573001110002"))

    assert mock_llm["ctx_al_modelo"].flow_state == {}, "Bruno heredó el flujo de Ana"


async def test_una_solicitud_queda_a_nombre_de_quien_la_pidio(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    El cliente y la conversación de la herramienta los pone el código desde el
    turno en curso: aunque el modelo quisiera, no hay campo para elegir otro.
    """
    await handle_inbound_message(_mensaje(negocio, "ana-1", "573001110001"))
    mock_llm["intent"] = Intent.ACT
    await handle_inbound_message(_mensaje(negocio, "quiero un turno", "573001110002"))

    await mock_llm["ejecutor"](
        "capture_request",
        {"request_type": "agenda", "structured_data": {"hora": "10:00"}, "customer_id": "x"},
    )

    filas = await _consultar(
        negocio,
        "SELECT cu.channel_identifier FROM captured_requests r "
        "JOIN customers cu ON cu.id = r.customer_id WHERE r.business_id = :b",
    )
    assert [f.channel_identifier for f in filas] == ["573001110002"]


async def test_el_mismo_numero_en_otro_negocio_no_trae_su_historial(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Un teléfono que escribe a dos negocios es un cliente distinto en cada uno."""
    otro = uuid4()
    async with system_session() as s:
        await set_tenant(s, otro)
        await s.execute(
            text("INSERT INTO businesses (id, name) VALUES (:i, 'Otro')"), {"i": otro}
        )

    try:
        mock_llm["intent"] = Intent.GREET
        await handle_inbound_message(_mensaje(otro, "secreto-del-otro-negocio", "573001119999"))
        await handle_inbound_message(_mensaje(negocio, "hola", "573001119999"))

        assert "secreto-del-otro-negocio" not in _textos(
            mock_llm["ctx_al_modelo"], mock_llm["mensaje_al_modelo"]
        )
    finally:
        async with system_session() as s:
            await set_tenant(s, otro)
            await s.execute(text("DELETE FROM businesses WHERE id = :i"), {"i": otro})


# ======================================================================
# Guardia de precios (invariante I1): evaluador-optimizador
# ======================================================================
CONOCIMIENTO_PRECIOS = "Corte de cabello: $30.000\nTinte completo: $90.000"


async def _con_conocimiento(business_id: UUID, contenido: str) -> None:
    async with system_session() as s:
        await set_tenant(s, business_id)
        await s.execute(
            text(
                "INSERT INTO knowledge_documents (business_id, title, raw_content) "
                "VALUES (:b, 'Precios', :c)"
            ),
            {"b": business_id, "c": contenido},
        )


async def test_guardia_corrige_un_precio_de_otro_servicio(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """El caso real: '$30.000... perdón, $90.000' para el tinte."""
    await _con_conocimiento(negocio, CONOCIMIENTO_PRECIOS)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = [
        "El tinte completo vale $30.000... perdón, $90.000.",
        "El tinte completo vale $90.000.",
    ]

    resultado = await handle_inbound_message(_mensaje(negocio, "¿cuánto vale el tinte?"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("El tinte completo vale $90.000.")
    assert "$30.000" not in mock_llm["enviados"][0]
    llamadas = mock_llm["llamadas_turno"]
    assert len(llamadas) == 2
    # El reintento le dice al modelo qué estuvo mal y va SIN herramientas.
    assert "Corte de cabello" in llamadas[1]["message"]
    assert llamadas[1]["tools"] is None
    filas = await _consultar(negocio, "SELECT tools_used FROM turn_logs WHERE business_id = :b")
    assert "guardia:precio_corregido" in filas[0].tools_used


async def test_guardia_bloquea_si_el_reintento_tambien_falla(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    await _con_conocimiento(negocio, CONOCIMIENTO_PRECIOS)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = ["Te hago el 50% de descuento.", "Bueno, el 40% entonces."]

    resultado = await handle_inbound_message(_mensaje(negocio, "dame un descuento"))

    assert resultado.reply_text is not None
    assert "%" not in resultado.reply_text
    assert "precio equivocado" in resultado.reply_text
    filas = await _consultar(negocio, "SELECT tools_used FROM turn_logs WHERE business_id = :b")
    assert "guardia:precio_bloqueado" in filas[0].tools_used


async def test_guardia_no_reintenta_si_ya_se_usaron_herramientas(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Reintentar repetiría efectos (otra solicitud, otro aviso): respuesta segura directa."""
    await _con_conocimiento(negocio, CONOCIMIENTO_PRECIOS)
    mock_llm["intent"] = Intent.ACT
    mock_llm["tools_used"] = ["capture_request"]
    mock_llm["reply"] = "Listo, agendé tu tinte por $45.000."

    resultado = await handle_inbound_message(_mensaje(negocio, "agéndame un tinte"))

    assert len(mock_llm["llamadas_turno"]) == 1
    assert resultado.reply_text is not None
    assert "$45.000" not in resultado.reply_text


async def test_guardia_deja_pasar_respuestas_correctas_sin_costo_extra(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    await _con_conocimiento(negocio, CONOCIMIENTO_PRECIOS)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = "El corte vale $30.000 y el tinte completo $90.000."

    await handle_inbound_message(_mensaje(negocio, "precios"))

    assert len(mock_llm["llamadas_turno"]) == 1
    filas = await _consultar(negocio, "SELECT tools_used FROM turn_logs WHERE business_id = :b")
    assert filas[0].tools_used == []


# ======================================================================
# Catálogo: precios por referencia (el modelo elige el ítem, el sistema pone el número)
# ======================================================================
async def _con_catalogo(business_id: UUID, items: list[tuple[str, int]]) -> None:
    async with system_session() as s:
        await set_tenant(s, business_id)
        for nombre, precio in items:
            await s.execute(
                text(
                    "INSERT INTO catalog_items (business_id, name, price) VALUES (:b, :n, :p)"
                ),
                {"b": business_id, "n": nombre, "p": precio},
            )


CATALOGO_PELUQUERIA = [("Corte de cabello", 30_000), ("Tinte completo", 90_000)]


async def test_catalogo_el_sistema_pone_el_precio(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    await _con_catalogo(negocio, CATALOGO_PELUQUERIA)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = "El tinte completo vale {{precio:tinte-completo}}."

    resultado = await handle_inbound_message(_mensaje(negocio, "¿cuánto vale el tinte?"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("El tinte completo vale $90.000.")
    assert "{{" not in mock_llm["enviados"][0], "nunca sale una referencia sin reemplazar"
    assert len(mock_llm["llamadas_turno"]) == 1
    # Lo que se guarda en el historial es lo que vio el cliente.
    salida = await _consultar(
        negocio, "SELECT content FROM messages WHERE business_id = :b AND role = 'agent'"
    )
    assert salida[0].content == "El tinte completo vale $90.000."


async def test_catalogo_un_precio_escrito_a_mano_se_corrige(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Aunque el número sea CORRECTO, escrito a mano no sale: no está garantizado."""
    await _con_catalogo(negocio, CATALOGO_PELUQUERIA)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = [
        "El tinte completo vale $90.000.",
        "El tinte completo vale {{precio:tinte-completo}}.",
    ]

    resultado = await handle_inbound_message(_mensaje(negocio, "¿cuánto vale el tinte?"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("El tinte completo vale $90.000.")
    llamadas = mock_llm["llamadas_turno"]
    assert len(llamadas) == 2
    assert "a mano" in llamadas[1]["message"]
    filas = await _consultar(negocio, "SELECT tools_used FROM turn_logs WHERE business_id = :b")
    assert "guardia:precio_corregido" in filas[0].tools_used


async def test_catalogo_la_referencia_de_otro_item_se_corrige(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """El error del tinte, en versión catálogo: nombra el tinte y referencia el corte."""
    await _con_catalogo(negocio, CATALOGO_PELUQUERIA)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = [
        "El tinte completo vale {{precio:corte-de-cabello}}.",
        "El tinte completo vale {{precio:tinte-completo}}.",
    ]

    resultado = await handle_inbound_message(_mensaje(negocio, "¿cuánto vale el tinte?"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("El tinte completo vale $90.000.")
    assert "$30.000" not in mock_llm["enviados"][0]


async def test_catalogo_una_referencia_inventada_nunca_sale(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    await _con_catalogo(negocio, CATALOGO_PELUQUERIA)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = [
        "La keratina vale {{precio:keratina}}.",
        "La keratina vale {{precio:keratina-premium}}.",
    ]

    resultado = await handle_inbound_message(_mensaje(negocio, "¿y la keratina?"))

    assert resultado.reply_text is not None
    assert "{{" not in resultado.reply_text
    assert "precio equivocado" in resultado.reply_text



# ======================================================================
# Ráfagas (invariante I2): una ráfaga recibe UNA respuesta
# ======================================================================
async def test_rafaga_se_responde_una_sola_vez(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    "hola" · "quería saber el precio" llegan seguidos: el primer turno ve que el
    cliente siguió escribiendo y no responde; el segundo responde todo junto.
    """
    from app.config import settings
    from app.core.orchestrator import TurnOutcomeKind

    monkeypatch.setattr(settings, "turn_debounce_seconds", 0.4)
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = "¡Hola! Te cuento los precios."

    async def segundo() -> Any:
        await asyncio.sleep(0.1)
        return await handle_inbound_message(
            _mensaje(negocio, "quería saber el precio", "573007770001")
        )

    primero, ultimo = await asyncio.gather(
        handle_inbound_message(_mensaje(negocio, "hola", "573007770001")), segundo()
    )

    assert primero.kind == TurnOutcomeKind.GROUPED and primero.reply_text is None
    assert ultimo.kind == TurnOutcomeKind.REPLIED
    assert len(mock_llm["llamadas_turno"]) == 1, "una ráfaga = una llamada al modelo"
    assert len(mock_llm["enviados"]) == 1, "una ráfaga = una respuesta"
    assert mock_llm["mensaje_al_modelo"] == "hola\nquería saber el precio"
    assert mock_llm["ctx_al_modelo"].recent_turns == []


async def test_mensaje_mientras_el_agente_responde_no_repite_lo_anterior(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Regresión (prueba final, 2026-11-25): "¿hacen keratina?" llegó mientras el
    agente respondía los precios. Los dos turnos corrían a la vez y el segundo,
    sin la primera respuesta en el historial, volvía a saludar y a dar precios.
    Ahora el segundo espera su turno y ve la respuesta anterior.
    """
    mock_llm["intent"] = Intent.INFORM
    mock_llm["demora"] = 0.3
    mock_llm["reply"] = ["Atendemos de 8 a 18.", "No tengo información de la keratina."]

    async def segundo() -> Any:
        # Llega cuando el primer turno ya leyó la conversación y espera al modelo.
        await mock_llm["en_modelo"].wait()
        return await handle_inbound_message(_mensaje(negocio, "¿hacen keratina?", "573007770002"))

    await asyncio.gather(
        handle_inbound_message(_mensaje(negocio, "¿qué horario tienen?", "573007770002")),
        segundo(),
    )

    llamadas = mock_llm["llamadas_turno"]
    assert [ll["message"] for ll in llamadas] == ["¿qué horario tienen?", "¿hacen keratina?"]
    historial = [t.content for t in mock_llm["ctx_al_modelo"].recent_turns]
    assert historial == ["¿qué horario tienen?", "Atendemos de 8 a 18."]


async def test_lo_que_llega_en_modo_humano_no_lo_responde_despues_el_agente(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Al devolver la conversación al agente, no contesta lo que ya atendió la persona."""
    mock_llm["intent"] = Intent.ESCALATE
    await handle_inbound_message(_mensaje(negocio, "quiero hablar con alguien", "573007770003"))
    await handle_inbound_message(_mensaje(negocio, "mensaje para la persona", "573007770003"))

    async with system_session() as s:
        await set_tenant(s, negocio)
        await s.execute(
            text("UPDATE conversations SET status = 'auto' WHERE business_id = :b"), {"b": negocio}
        )

    mock_llm["intent"] = Intent.INFORM
    await handle_inbound_message(_mensaje(negocio, "ya volví, ¿horarios?", "573007770003"))

    assert mock_llm["mensaje_al_modelo"] == "ya volví, ¿horarios?"



# ======================================================================
# "escribiendo…": solo cuando annie va a responder
# ======================================================================
def _wamid(inbound: InboundMessage) -> str:
    assert inbound.external_message_id is not None
    return inbound.external_message_id


async def test_escribiendo_una_vez_cuando_va_a_responder(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.INFORM
    mensaje = _mensaje(negocio, "¿qué horario tienen?", "573008880001")

    await handle_inbound_message(mensaje)

    assert mock_llm["escribiendo"] == [_wamid(mensaje)]
    assert len(mock_llm["enviados"]) == 1


async def test_en_modo_humano_no_hay_escribiendo(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.ESCALATE
    await handle_inbound_message(_mensaje(negocio, "quiero una persona", "573008880002"))
    mock_llm["escribiendo"].clear()

    await handle_inbound_message(_mensaje(negocio, "¿hola?", "573008880002"))

    assert mock_llm["escribiendo"] == [], "annie no va a responder: no se muestra"


async def test_una_rafaga_es_una_sola_peticion_de_escribiendo(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "turn_debounce_seconds", 0.4)
    mock_llm["intent"] = Intent.INFORM
    mensajes = [_mensaje(negocio, f"parte {i}", "573008880003") for i in range(3)]

    async def enviar(i: int) -> Any:
        await asyncio.sleep(0.1 * i)
        return await handle_inbound_message(mensajes[i])

    await asyncio.gather(*(enviar(i) for i in range(3)))

    # Una petición, sobre el último mensaje (marcarlo leído marca los anteriores).
    assert mock_llm["escribiendo"] == [_wamid(mensajes[-1])]
    assert len(mock_llm["enviados"]) == 1


async def test_si_falla_escribiendo_igual_se_responde(
    negocio: UUID, mock_llm: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def typing_que_falla(message_id: str, phone_number_id: str | None = None) -> None:
        raise WhatsAppError("Meta caído")

    monkeypatch.setattr("app.core.orchestrator.send_typing_indicator", typing_que_falla)
    mock_llm["intent"] = Intent.INFORM

    resultado = await handle_inbound_message(_mensaje(negocio, "hola", "573008880004"))

    assert resultado.reply_text is not None
    assert len(mock_llm["enviados"]) == 1


# ======================================================================
# Notas de voz: se transcriben y entran al flujo como un mensaje más
# ======================================================================
def _audio(business_id: UUID, telefono: str) -> InboundMessage:
    return InboundMessage(
        business_id=business_id,
        channel="whatsapp",
        channel_identifier=telefono,
        text=text_for_model(ContentType.AUDIO, "", "pending"),
        external_message_id=f"wamid.{uuid4()}",
        received_at=datetime.now(UTC),
        media=InboundMedia(
            content_type=ContentType.AUDIO,
            external_media_id="audio-1",
            mime_type="audio/ogg; codecs=opus",
        ),
    )


@pytest.fixture
def audio_falso(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Descarga y transcripción simuladas: devuelven lo que diga cada test."""
    from app.config import settings
    from app.core.transcription import TranscriptionResult, TranscriptionStatus

    # Hay un proveedor activo (simulado): el audio se descarga y se transcribe.
    monkeypatch.setattr(settings, "transcription_provider", "simulado")
    estado: dict[str, Any] = {
        "texto": "quería saber el precio",
        "demora": 0.0,
        "fallo": False,
        "avisos": [],
    }

    async def fake_notify(business_id: Any, subject: str, body: str) -> str:
        estado["avisos"].append(subject)
        return "ok"

    monkeypatch.setattr("app.core.orchestrator.tools_module.notify_owner", fake_notify)

    async def fake_download(media_id: str, **kwargs: Any) -> tuple[bytes, str]:
        estado["descarga_audio"] = kwargs.get("audio")
        return b"OggS-audio", "audio/ogg; codecs=opus"

    async def fake_transcribe(audio: Any, transcriber: Any = None) -> Any:
        estado["mime"] = audio.mime_type
        await asyncio.sleep(estado["demora"])
        if estado["fallo"]:
            return TranscriptionResult(TranscriptionStatus.FAILED, detail="fallo simulado")
        return TranscriptionResult(TranscriptionStatus.DONE, text=estado["texto"])

    monkeypatch.setattr("app.core.orchestrator.download_whatsapp_media", fake_download)
    monkeypatch.setattr("app.core.orchestrator.transcribe", fake_transcribe)
    return estado


async def test_una_nota_de_voz_le_llega_transcrita_al_modelo(
    negocio: UUID, mock_llm: dict[str, Any], audio_falso: dict[str, Any]
) -> None:
    mock_llm["intent"] = Intent.INFORM

    resultado = await handle_inbound_message(_audio(negocio, "573009990001"))

    assert resultado.reply_text is not None
    assert audio_falso["descarga_audio"] is True
    assert audio_falso["mime"] == "audio/ogg; codecs=opus"
    assert mock_llm["mensaje_al_modelo"] == (
        "[Nota de voz del cliente, transcrita] quería saber el precio"
    )
    filas = await _consultar(
        negocio,
        "SELECT content, content_type, transcription_status FROM messages "
        "WHERE business_id = :b AND role = 'customer'",
    )
    assert (filas[0].content, filas[0].content_type, filas[0].transcription_status) == (
        "quería saber el precio",
        "audio",
        "done",
    )
    media = await _consultar(negocio, "SELECT 1 FROM message_media WHERE business_id = :b")
    assert media == [], "el audio NO se guarda (Ley 1581): solo su transcripción"


async def test_si_no_se_pudo_procesar_responde_el_aviso_fijo_y_avisa_al_asesor(
    negocio: UUID, mock_llm: dict[str, Any], audio_falso: dict[str, Any]
) -> None:
    """Solo audio y sin poder procesarlo: aviso fijo, sin llamar al modelo."""
    from app.core.orchestrator import RESPUESTA_AUDIO_NO_PROCESADO

    audio_falso["fallo"] = True

    resultado = await handle_inbound_message(_audio(negocio, "573009990002"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith(RESPUESTA_AUDIO_NO_PROCESADO)
    assert mock_llm.get("llamadas_turno", []) == [] and mock_llm["classify_calls"] == 0
    assert audio_falso["avisos"] == ["Nota de voz sin procesar"], "el asesor se entera"
    conv = await _consultar(negocio, "SELECT status FROM conversations WHERE business_id = :b")
    assert conv[0].status == "auto", "annie sigue disponible si el cliente escribe"


async def test_sin_proveedor_ni_siquiera_se_descarga_el_audio(
    negocio: UUID, mock_llm: dict[str, Any], audio_falso: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.core.orchestrator import RESPUESTA_AUDIO_NO_PROCESADO

    monkeypatch.setattr(settings, "transcription_provider", "none")

    resultado = await handle_inbound_message(_audio(negocio, "573009990005"))

    assert "descarga_audio" not in audio_falso, "sin proveedor no se baja el archivo de Meta"
    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith(RESPUESTA_AUDIO_NO_PROCESADO)


async def test_en_la_misma_rafaga_texto_y_audio_fallido_van_juntos(
    negocio: UUID, mock_llm: dict[str, Any], audio_falso: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings
    from app.core.orchestrator import RESPUESTA_AUDIO_NO_PROCESADO

    monkeypatch.setattr(settings, "turn_debounce_seconds", 0.3)
    audio_falso["fallo"] = True
    mock_llm["intent"] = Intent.INFORM
    mock_llm["reply"] = "Atendemos de 8 a 18."

    async def texto_despues() -> Any:
        await asyncio.sleep(0.1)
        return await handle_inbound_message(_mensaje(negocio, "¿horario?", "573009990007"))

    await asyncio.gather(handle_inbound_message(_audio(negocio, "573009990007")), texto_despues())

    assert len(mock_llm["enviados"]) == 1
    respuesta = mock_llm["enviados"][0]
    assert respuesta.startswith("Atendemos de 8 a 18.")
    assert RESPUESTA_AUDIO_NO_PROCESADO in respuesta
    assert "no se pudo escuchar" in mock_llm["mensaje_al_modelo"]


async def test_rafaga_mixta_espera_la_transcripcion_y_responde_una_vez(
    negocio: UUID,
    mock_llm: dict[str, Any],
    audio_falso: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    🎤 (tarda en transcribirse) + "¿y el sábado?" un instante después: el turno
    del texto espera la transcripción y responde los dos, en orden, una sola vez.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "turn_debounce_seconds", 0.2)
    audio_falso["demora"] = 0.8
    mock_llm["intent"] = Intent.INFORM

    async def texto_despues() -> Any:
        await asyncio.sleep(0.1)
        return await handle_inbound_message(_mensaje(negocio, "¿y el sábado?", "573009990003"))

    await asyncio.gather(handle_inbound_message(_audio(negocio, "573009990003")), texto_despues())

    assert len(mock_llm["llamadas_turno"]) == 1
    assert mock_llm["mensaje_al_modelo"] == (
        "[Nota de voz del cliente, transcrita] quería saber el precio\n¿y el sábado?"
    )
    assert len(mock_llm["enviados"]) == 1


async def test_una_nota_de_voz_en_modo_humano_se_transcribe_y_annie_calla(
    negocio: UUID, mock_llm: dict[str, Any], audio_falso: dict[str, Any]
) -> None:
    """Antes, annie respondía 'solo puedo leer texto' aunque atendiera una persona."""
    mock_llm["intent"] = Intent.ESCALATE
    await handle_inbound_message(_mensaje(negocio, "quiero una persona", "573009990004"))
    mock_llm["enviados"].clear()
    mock_llm["escribiendo"].clear()

    resultado = await handle_inbound_message(_audio(negocio, "573009990004"))

    assert resultado.reply_text is None
    assert mock_llm["enviados"] == [] and mock_llm["escribiendo"] == []
    filas = await _consultar(
        negocio,
        "SELECT content FROM messages WHERE business_id = :b AND content_type = 'audio'",
    )
    assert filas[0].content == "quería saber el precio", "el asesor la lee en la bandeja"


# ======================================================================
# Enrutamiento: el flujo es del agente que lo abrió (prueba del 2026-11-25)
# ======================================================================
async def _con_informador(business_id: UUID) -> str:
    """Agrega un agente que solo informa (sin herramientas), como el del seed."""
    async with system_session() as s:
        await set_tenant(s, business_id)
        agente = await AgentRepo(s).create(
            business_id,
            name="Informador",
            routing_description="responde horarios, precios y servicios",
            instructions_md="# Rol",
            allowed_tools=[],
        )
    return str(agente.id)


async def _agente_del_ultimo_turno(business_id: UUID) -> str | None:
    filas = await _consultar(
        business_id,
        "SELECT a.name FROM turn_logs t LEFT JOIN agents a ON a.id = t.routed_agent_id "
        "WHERE t.business_id = :b ORDER BY t.created_at DESC LIMIT 1",
    )
    return filas[0].name if filas else None


async def test_tu_conversacion_agendar_despues_de_informar_llega_al_agendador(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """
    Regresión exacta: precios (Informador) -> pedir descuento (aclaración del agente
    genérico) -> "quiero agendar el sábado". Antes, el atajo CONTINUE seguía con el
    Informador (sin herramientas) y la solicitud nunca se registraba.
    """
    informador = await _con_informador(negocio)
    tel = "573006660001"

    mock_llm["intent"], mock_llm["agent_id"] = Intent.INFORM, informador
    mock_llm["reply"] = "Atendemos de 8 a 18."
    await handle_inbound_message(_mensaje(negocio, "¿cuáles son los precios?", tel))

    mock_llm["intent"], mock_llm["agent_id"] = Intent.CLARIFY, None
    mock_llm["reply"] = "¿Me cuentas un poco más?"
    await handle_inbound_message(_mensaje(negocio, "dame un descuento", tel))

    llamadas_antes = mock_llm["classify_calls"]
    mock_llm["intent"] = Intent.ACT  # el clasificador elige al Agendador
    mock_llm["reply"] = "¿Para cuántas personas?"
    await handle_inbound_message(_mensaje(negocio, "quiero agendar el sábado", tel))

    assert mock_llm["classify_calls"] == llamadas_antes + 1, "debió volver a clasificar"
    assert await _agente_del_ultimo_turno(negocio) == "Agendador"


async def test_informar_no_deja_un_flujo_abierto(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    informador = await _con_informador(negocio)
    mock_llm["intent"], mock_llm["agent_id"] = Intent.INFORM, informador
    mock_llm["reply"] = "Atendemos de 8 a 18."

    await handle_inbound_message(_mensaje(negocio, "¿horario?", "573006660002"))
    llamadas = mock_llm["classify_calls"]
    await handle_inbound_message(_mensaje(negocio, "¿y los domingos?", "573006660002"))

    assert mock_llm["classify_calls"] == llamadas + 1, "una pregunta nueva se clasifica"


async def test_un_flujo_viejo_sin_la_marca_del_agente_se_vuelve_a_clasificar(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Conversaciones que quedaron pegadas antes del arreglo se despegan solas."""
    informador = await _con_informador(negocio)
    tel = "573006660003"
    mock_llm["intent"], mock_llm["agent_id"] = Intent.INFORM, informador
    mock_llm["reply"] = "Atendemos de 8 a 18."
    await handle_inbound_message(_mensaje(negocio, "hola", tel))

    async with system_session() as s:
        await set_tenant(s, negocio)
        await s.execute(
            text(
                "UPDATE conversations SET flow_state = '{\"_intent\": \"continue\"}', "
                "active_agent_id = :a WHERE business_id = :b"
            ),
            {"a": informador, "b": negocio},
        )

    llamadas = mock_llm["classify_calls"]
    await handle_inbound_message(_mensaje(negocio, "quiero agendar", tel))
    assert mock_llm["classify_calls"] == llamadas + 1


# ======================================================================
# Guardia de promesas (invariante I8)
# ======================================================================
async def test_una_promesa_sin_respaldo_se_corrige(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    informador = await _con_informador(negocio)
    mock_llm["intent"], mock_llm["agent_id"] = Intent.INFORM, informador
    mock_llm["reply"] = [
        "¡Perfecto! Le paso tu solicitud a una persona del salón.",
        "¿Quieres que te pase con una persona del salón para agendar?",
    ]

    resultado = await handle_inbound_message(
        _mensaje(negocio, "3 personas a las 10", "573006660004")
    )

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("¿Quieres que te pase")
    llamadas = mock_llm["llamadas_turno"]
    assert len(llamadas) == 2 and llamadas[1]["tools"] is None
    assert "No afirmes acciones que no hiciste" in llamadas[1]["message"]
    filas = await _consultar(negocio, "SELECT tools_used FROM turn_logs WHERE business_id = :b")
    assert "guardia:promesa_corregida" in filas[0].tools_used


async def test_si_vuelve_a_prometer_sale_la_respuesta_segura(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    from app.core.orchestrator import RESPUESTA_PROMESA_SEGURA

    informador = await _con_informador(negocio)
    mock_llm["intent"], mock_llm["agent_id"] = Intent.INFORM, informador
    mock_llm["reply"] = ["Le avisé al equipo.", "Ya le avisé a una persona."]

    resultado = await handle_inbound_message(_mensaje(negocio, "agéndame", "573006660005"))

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith(RESPUESTA_PROMESA_SEGURA)


async def test_una_promesa_respaldada_por_la_herramienta_pasa(
    negocio: UUID, mock_llm: dict[str, Any]
) -> None:
    """Si el agente SÍ registró la solicitud, decirlo es la verdad."""
    mock_llm["intent"] = Intent.ACT
    mock_llm["tools_used"] = ["capture_request"]
    mock_llm["reply"] = "Dejé registrada tu solicitud; el salón te confirma."

    resultado = await handle_inbound_message(
        _mensaje(negocio, "el sábado a las 10", "573006660006")
    )

    assert resultado.reply_text is not None
    assert resultado.reply_text.startswith("Dejé registrada tu solicitud")
    assert len(mock_llm["llamadas_turno"]) == 1
