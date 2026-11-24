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
    async def fake_download(media_id: str) -> tuple[bytes, str]:
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
    async def fake_download(media_id: str) -> tuple[bytes, str]:
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

    async def fake_download(media_id: str) -> tuple[bytes, str]:
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
