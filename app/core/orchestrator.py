"""
Orquestador: el loop del turno (F1-F7).

Loop delgado propio sobre la API de Anthropic — sin Claude Agent SDK ni
LangGraph (decision 2 de CLAUDE.md). Una sola fuente de verdad del estado:
nuestra base de datos.

Los 8 pasos del turno:

    1. resolver cliente + conversacion, guardar el mensaje entrante
    2. cargar contexto (ultimos N turnos + memoria)
    3. si status == 'human' -> callar (F6 activo)
    4. atajo CONTINUE (F5): flujo activo -> no re-clasificar
    5. clasificar + enrutar (Haiku)
    6. ejecutar el handler segun la intencion (Sonnet + herramientas)
    7. persistir: respuesta, efectos, turn_log
    8. enviar la respuesta por el canal

Regla transversal (RNF-10): el turno SIEMPRE se loguea y el cliente SIEMPRE
recibe una respuesta honesta, incluso si el modelo o una herramienta fallan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.core import tools as tools_module
from app.core.catalog import render_prices, verification_lines
from app.core.channel_whatsapp import (
    WhatsAppError,
    download_whatsapp_media,
    send_typing_indicator,
    send_whatsapp_reply,
)
from app.core.guards import check_prices, hand_written_amounts, unbacked_promises
from app.core.limits import (
    AVISO_PRIVACIDAD,
    RESPUESTA_LIMITE_DIARIO,
    RESPUESTA_RATE_LIMIT,
    check_daily_limit,
    check_rate_limit,
    marcar_aviso_enviado,
    necesita_aviso_privacidad,
)
from app.core.llm import (
    AgentTurnResult,
    ClassifyResult,
    LLMError,
    classify,
    run_agent_turn,
)
from app.core.transcription import (
    AudioFile,
    TranscriptionResult,
    TranscriptionStatus,
    transcribe,
)
from app.db.models import Conversation, Message
from app.db.repositories import (
    AgentRepo,
    BusinessRepo,
    ConversationRepo,
    CustomerRepo,
    EscalationRepo,
    MediaRepo,
    MemoryRepo,
    MessageRepo,
    TurnLogRepo,
)
from app.db.session import tenant_session
from app.schemas import (
    AgentConfig,
    BusinessConfig,
    ContentType,
    ConversationContext,
    ConversationStatus,
    HandlerResult,
    InboundMessage,
    Intent,
    MessageRole,
    OutboundMessage,
    TurnUsage,
    text_for_model,
)

logger = logging.getLogger(__name__)

# Fallos seguidos del agente en una conversacion antes de escalar solo.
MAX_FALLOS_ANTES_DE_ESCALAR = 3
CLAVE_FALLOS = "_fallos_consecutivos"
# Id del agente que dejó el flujo abierto: solo ESE agente lo continúa (F5).
CLAVE_AGENTE_DEL_FLUJO: Final = "_agent"

RESPUESTA_ERROR = (
    "Perdoná, tuve un problema para procesar tu mensaje. "
    "¿Querés que le avise a una persona del negocio para que te ayude?"
)
RESPUESTA_SIN_AGENTES = (
    "Gracias por escribir. En este momento no puedo atenderte automáticamente, "
    "pero le paso tu mensaje a una persona del negocio."
)
RESPUESTA_ESCALADA = (
    "Listo, le avisé a una persona del negocio para que siga tu caso. "
    "En un rato te escriben por acá."
)


class TurnOutcomeKind(str, Enum):
    """Cómo terminó el turno. El webhook lo ignora; el sandbox lo muestra."""

    REPLIED = "respondido"
    GROUPED = "agrupado"  # el cliente siguió escribiendo: responde el turno del último mensaje
    HUMAN_MODE = "modo_humano"  # el agente calla: la conversación es de una persona (F6)
    RATE_LIMITED = "rate_limit"  # respuesta fija, sin modelo ni registro del turno
    DAILY_LIMIT = "limite_diario"  # circuit breaker del negocio: escala sin modelo


@dataclass(frozen=True)
class TurnOutcome:
    """
    Resultado de procesar un mensaje entrante.

    Existe para que quien observa el turno (el sandbox) no tenga que adivinarlo
    leyendo "el último mensaje" de la conversación: si el agente calló, ese
    último mensaje es el del propio cliente.
    """

    kind: TurnOutcomeKind
    reply_text: str | None
    message_in_id: UUID | None

    @property
    def replied(self) -> bool:
        return self.reply_text is not None


# ======================================================================
# Entrada principal
# ======================================================================
async def handle_inbound_message(
    inbound: InboundMessage, phone_number_id: str | None = None
) -> TurnOutcome:
    """
    Procesa un mensaje entrante de punta a punta.

    La llama el webhook en segundo plano (el ACK ya se envio). Nunca deja al
    cliente sin respuesta por un error interno. Devuelve cómo terminó el turno.
    """
    inicio = time.monotonic()
    business_id = inbound.business_id

    # Rate limit ANTES de tocar la base: un flood no debe generar ni escrituras
    # ni llamadas al modelo (fase 7, proteccion de costo). Respuesta fija e
    # instantánea: no lleva "escribiendo…".
    if not check_rate_limit(business_id, inbound.channel_identifier):
        await _send_reply(inbound, RESPUESTA_RATE_LIMIT, phone_number_id)
        return TurnOutcome(TurnOutcomeKind.RATE_LIMITED, RESPUESTA_RATE_LIMIT, None)

    # La URL de descarga de Meta expira en minutos: el archivo se baja ahora,
    # antes de abrir la transaccion (no se retiene una conexion durante la red).
    archivo = await _descargar_media(inbound)

    # Paso 1: el mensaje queda guardado YA, antes de esperar.
    guardado = await _save_inbound(inbound, archivo)

    # Invariante I2: la gente escribe en varios mensajes seguidos. Se espera unos
    # segundos (como una persona que deja terminar al otro); Thomas: "respuesta
    # inmediata" puede ser ~10 s. Una nota de voz se transcribe EN PARALELO a esa
    # espera: el tiempo de transcribir no se suma al de esperar.
    tareas: list[Any] = []
    if guardado.es_audio:
        tareas.append(_transcribir_y_guardar(business_id, guardado, archivo, inbound))
    if settings.turn_debounce_seconds > 0:
        tareas.append(asyncio.sleep(settings.turn_debounce_seconds))
    if tareas:
        await asyncio.gather(*tareas)

    # Si el cliente siguió escribiendo, este turno no responde: el del último
    # mensaje responde todo junto.
    if settings.turn_debounce_seconds > 0 and await _hay_mensaje_mas_nuevo(business_id, guardado):
        return TurnOutcome(TurnOutcomeKind.GROUPED, None, guardado.message_id)

    # Un turno a la vez por conversación: si otro turno sigue respondiendo, se
    # espera a que termine para leer el historial CON su respuesta.
    async with _turno_exclusivo(guardado.conversation_id):
        return await _turno(inbound, guardado, phone_number_id, inicio)


async def _turno(
    inbound: InboundMessage, guardado: _Guardado, phone_number_id: str | None, inicio: float
) -> TurnOutcome:
    """
    Pasos 2-3, con la conversación tomada por este turno: decide SI se responde.

    | Caso                                  | ¿Responde?          | "escribiendo…" |
    | El cliente siguió escribiendo         | no (agrupado)       | no             |
    | Modo humano                           | no                  | no             |
    | Tope diario del negocio               | sí, texto fijo      | no (inmediato) |
    | Todo lo demás                         | sí: `_responder`    | sí             |
    """
    business_id = inbound.business_id
    ctx, business, pendientes = await _load_turn(business_id, guardado)

    if not pendientes:
        # Un turno anterior ya respondió este mensaje junto con otros.
        return TurnOutcome(TurnOutcomeKind.GROUPED, None, guardado.message_id)

    message_in_id: UUID | None = pendientes[-1].id
    hasta_seq = pendientes[-1].seq

    # Paso 3: modo humano -> el agente calla (F6, RF-12). Los mensajes quedan
    # marcados como atendidos: si después se devuelve al agente, no los responde.
    if ctx.status == ConversationStatus.HUMAN:
        logger.info("conversacion %s en modo humano; no se responde", ctx.conversation_id)
        await _mark_processed(business_id, ctx.conversation_id, hasta_seq)
        return TurnOutcome(TurnOutcomeKind.HUMAN_MODE, None, message_in_id)

    # Circuit breaker de costo por tenant: al superarlo se escala a humano en vez
    # de seguir gastando turnos. Respuesta fija e instantánea.
    if not await check_daily_limit(business_id):
        await _persist_turn(
            inbound,
            ctx,
            Intent.ESCALATE,
            None,
            HandlerResult(reply_text=RESPUESTA_LIMITE_DIARIO, escalate=True),
            TurnUsage(),
            message_in_id,
            int((time.monotonic() - inicio) * 1000),
            hasta_seq,
        )
        await _send_reply(inbound, RESPUESTA_LIMITE_DIARIO, phone_number_id)
        return TurnOutcome(TurnOutcomeKind.DAILY_LIMIT, RESPUESTA_LIMITE_DIARIO, message_in_id)

    return await _responder(
        inbound, ctx, business, pendientes, message_in_id, hasta_seq, phone_number_id, inicio
    )


async def _responder(
    inbound: InboundMessage,
    ctx: ConversationContext,
    business: BusinessConfig,
    pendientes: list[Message],
    message_in_id: UUID | None,
    hasta_seq: int,
    phone_number_id: str | None,
    inicio: float,
) -> TurnOutcome:
    """
    LA función de respuesta: solo se llega aquí cuando annie VA a responder.

    1. "escribiendo…" (una petición por respuesta, nunca por mensaje: una ráfaga
       de N mensajes es UNA petición; en modo humano, ninguna).
    2. Espera las transcripciones pendientes de la ráfaga.
    3. Piensa (modelo + guardia de precios) y responde.
    """
    business_id = inbound.business_id
    await _mostrar_escribiendo(inbound, phone_number_id)

    pendientes, espero = await _esperar_transcripciones(business_id, pendientes)
    if espero > TYPING_REFRESH_AFTER_SECONDS:
        # El indicador dura 25 s: si la transcripción tardó, se renueva para
        # cubrir lo que tarde el modelo.
        await _mostrar_escribiendo(inbound, phone_number_id)

    # La ráfaga completa llega al modelo como UN mensaje, en orden.
    texto_cliente = "\n".join(
        text_for_model(m.content_type, m.content, m.transcription_status) for m in pendientes
    )
    inbound = inbound.model_copy(update={"text": texto_cliente})

    # Notas de voz que no se pudieron procesar (sin proveedor, falla o > 2 min).
    # El aviso al cliente es FIJO y lo pone el sistema, no el modelo; y como
    # promete un asesor, el asesor recibe el aviso de verdad.
    audios_sin_procesar = [m for m in pendientes if _audio_no_procesado(m)]
    if audios_sin_procesar:
        await _avisar_audio_no_procesado(inbound, len(audios_sin_procesar))

    if len(audios_sin_procesar) == len(pendientes):
        # Solo audios: no hay nada que el modelo pueda responder. Sin llamada al modelo.
        result = HandlerResult(
            reply_text=RESPUESTA_AUDIO_NO_PROCESADO, tools_used=[AUDIO_NO_PROCESADO]
        )
        latencia_ms = int((time.monotonic() - inicio) * 1000)
        await _persist_turn(
            inbound, ctx, Intent.FALLBACK, None, result, TurnUsage(), message_in_id,
            latencia_ms, hasta_seq,
        )
        texto = await _con_aviso_de_privacidad(business_id, ctx.customer_id, result.reply_text)
        await _send_reply(inbound, texto, phone_number_id)
        return TurnOutcome(TurnOutcomeKind.REPLIED, texto, message_in_id)

    intent: Intent = Intent.FALLBACK
    agent: AgentConfig | None = None
    usage = TurnUsage()

    try:
        intent, agent, usage_clasif = await _classify_and_route(inbound, ctx, business_id)
        usage = usage_clasif

        result, usage_turno = await _run_handler(intent, agent, business, ctx, inbound)
        usage = _sumar_usage(usage, usage_turno)

    except LLMError:
        logger.exception("fallo la capa LLM; se degrada con gracia")
        result = HandlerResult(
            reply_text=RESPUESTA_ERROR,
            new_flow_state=_incrementar_fallos(ctx.flow_state),
        )
    except Exception:  # noqa: BLE001 - frontera: el cliente no puede quedar sin respuesta
        logger.exception("fallo inesperado en el turno; se degrada con gracia")
        result = HandlerResult(
            reply_text=RESPUESTA_ERROR,
            new_flow_state=_incrementar_fallos(ctx.flow_state),
        )

    # Demasiados fallos seguidos: escalar sin que el cliente tenga que pedirlo.
    if _demasiados_fallos(result.new_flow_state or ctx.flow_state):
        logger.warning("conversacion %s supero el limite de fallos; escala", ctx.conversation_id)
        result = HandlerResult(
            reply_text=RESPUESTA_ESCALADA,
            escalate=True,
            new_flow_state={},
            tools_used=result.tools_used,
        )
        intent = Intent.ESCALATE

    if audios_sin_procesar and not result.escalate:
        # Ráfaga mixta: el modelo respondió el texto; el aviso del audio lo pone el sistema.
        result = result.model_copy(
            update={
                "reply_text": f"{result.reply_text}\n\n{RESPUESTA_AUDIO_NO_PROCESADO}",
                "tools_used": [*result.tools_used, AUDIO_NO_PROCESADO],
            }
        )

    latencia_ms = int((time.monotonic() - inicio) * 1000)
    await _persist_turn(
        inbound, ctx, intent, agent, result, usage, message_in_id, latencia_ms, hasta_seq
    )

    texto = await _con_aviso_de_privacidad(business_id, ctx.customer_id, result.reply_text)
    await _send_reply(inbound, texto, phone_number_id)
    return TurnOutcome(TurnOutcomeKind.REPLIED, texto, message_in_id)


async def _con_aviso_de_privacidad(business_id: UUID, customer_id: UUID, texto: str) -> str:
    """
    Aviso de tratamiento de datos en el PRIMER contacto (RNF-05, Ley 1581).
    Va pegado a la primera respuesta para no gastar un mensaje aparte.
    """
    if await necesita_aviso_privacidad(business_id, customer_id):
        await marcar_aviso_enviado(business_id, customer_id)
        return f"{texto}\n\n---\n{AVISO_PRIVACIDAD}"
    return texto


# ======================================================================
# "escribiendo…" y notas de voz
# ======================================================================
# Texto definido por Thomas (2026-11-25). Fijo: lo pone el sistema, no el modelo.
RESPUESTA_AUDIO_NO_PROCESADO: Final = (
    "No pude procesar el audio. Si deseas, puedes escribirme para recibir tu respuesta "
    "o esperar a un asesor, que estará disponible lo más rápido posible."
)
# Marca en turn_logs.tools_used: cuántos turnos tuvieron audios sin procesar.
AUDIO_NO_PROCESADO: Final = "audio:no_procesado"


def _audio_no_procesado(m: Message) -> bool:
    return m.content_type == ContentType.AUDIO.value and m.transcription_status in (
        TranscriptionStatus.FAILED.value,
        TranscriptionStatus.TOO_LONG.value,
    )


async def _avisar_audio_no_procesado(inbound: InboundMessage, cantidad: int) -> None:
    """
    El mensaje al cliente promete "un asesor estará disponible": el asesor tiene
    que enterarse. Se le avisa (correo si hay SES; siempre queda en la bandeja)
    SIN pasar la conversación a modo humano, para que el cliente pueda seguir
    escribiéndole a annie si prefiere.
    """
    try:
        await tools_module.notify_owner(
            inbound.business_id,
            subject="Nota de voz sin procesar",
            body=(
                f"El cliente {inbound.channel_identifier} envió {cantidad} nota(s) de voz "
                "que no se pudieron procesar. annie le pidió que escriba o que espere a "
                "un asesor. Revisa la conversación en la bandeja."
            ),
        )
    except Exception:  # noqa: BLE001 - un aviso fallido no rompe la respuesta al cliente
        logger.exception("no se pudo avisar de la nota de voz sin procesar")


# El indicador de WhatsApp dura hasta 25 s. Si esperar transcripciones tomó más
# que esto, se vuelve a pedir antes de llamar al modelo.
TYPING_REFRESH_AFTER_SECONDS: Final = 15.0

# Cuánto espera el turno que responde a que termine una transcripción pendiente.
TRANSCRIPTION_WAIT_SECONDS: Final = 30.0
_TRANSCRIPTION_POLL_SECONDS: Final = 0.5


async def _mostrar_escribiendo(inbound: InboundMessage, phone_number_id: str | None) -> None:
    """
    "escribiendo…" en el WhatsApp del cliente, y su mensaje marcado como leído
    (con él, toda la ráfaga). Solo la llama `_responder`. Nunca rompe el turno.
    """
    message_id = inbound.external_message_id
    if inbound.channel != "whatsapp" or not message_id:
        return  # sandbox y canales sin id de Meta: no hay a quién mostrárselo
    try:
        await send_typing_indicator(message_id, phone_number_id)
    except WhatsAppError:
        logger.warning("no se pudo mostrar 'escribiendo' (mensaje %s)", message_id)


async def _transcribir_y_guardar(
    business_id: UUID,
    guardado: _Guardado,
    archivo: tuple[bytes, str] | None,
    inbound: InboundMessage,
) -> None:
    """
    Transcribe la nota de voz con el proveedor configurado y guarda el texto.
    El archivo de audio NO se guarda (Ley 1581): se descarta al terminar.
    """
    if archivo is None:
        resultado = TranscriptionResult(TranscriptionStatus.FAILED, detail="no se pudo descargar")
    else:
        datos, mime = archivo
        resultado = await transcribe(AudioFile(data=datos, mime_type=mime))
    logger.info(
        "nota de voz %s: %s (%s)", guardado.message_id, resultado.status.value, resultado.provider
    )
    async with tenant_session(business_id) as session:
        await MessageRepo(session).set_transcription(
            business_id, guardado.message_id, resultado.status.value, resultado.text
        )


async def _esperar_transcripciones(
    business_id: UUID, pendientes: list[Message]
) -> tuple[list[Message], float]:
    """
    Si en la ráfaga hay notas de voz todavía transcribiéndose, espera a que
    terminen (máximo TRANSCRIPTION_WAIT_SECONDS). Lo que no termine a tiempo
    queda como 'no se pudo escuchar'. Devuelve los mensajes al día y lo que esperó.
    """
    inicio = time.monotonic()
    ids = [m.id for m in pendientes]

    def _en_curso(mensajes: list[Message]) -> list[Message]:
        return [m for m in mensajes if m.transcription_status == TranscriptionStatus.PENDING.value]

    while _en_curso(pendientes) and time.monotonic() - inicio < TRANSCRIPTION_WAIT_SECONDS:
        await asyncio.sleep(_TRANSCRIPTION_POLL_SECONDS)
        async with tenant_session(business_id) as session:
            pendientes = await MessageRepo(session).get_many(business_id, ids)

    atrasados = _en_curso(pendientes)
    if atrasados:
        logger.warning("%d nota(s) de voz sin transcribir a tiempo", len(atrasados))
        async with tenant_session(business_id) as session:
            repo = MessageRepo(session)
            for m in atrasados:
                await repo.set_transcription(business_id, m.id, TranscriptionStatus.FAILED.value)
            pendientes = await repo.get_many(business_id, ids)

    return pendientes, time.monotonic() - inicio


# ======================================================================
# Pasos 1-2: contexto
# ======================================================================
async def _descargar_media(inbound: InboundMessage) -> tuple[bytes, str] | None:
    """
    Baja el adjunto del mensaje, si tiene. Nunca lanza.

    Si la descarga falla, el mensaje se guarda igual (como imagen sin archivo) y
    el turno sigue: perder la foto es mejor que perder el mensaje del cliente.
    """
    if inbound.media is None or inbound.channel != "whatsapp":
        return None
    es_audio = inbound.media.content_type == ContentType.AUDIO
    if es_audio and settings.transcription_provider == "none":
        return None  # sin proveedor no hay para qué bajarlo: el audio no se guarda
    try:
        return await download_whatsapp_media(inbound.media.external_media_id, audio=es_audio)
    except WhatsAppError:
        logger.warning(
            "no se pudo descargar el archivo del mensaje %s", inbound.external_message_id
        )
        return None


@dataclass(frozen=True)
class _Guardado:
    """Lo que deja el paso 1: el mensaje entrante ya está en la base."""

    conversation_id: UUID
    customer_id: UUID
    message_id: UUID
    seq: int
    es_audio: bool = False


async def _save_inbound(
    inbound: InboundMessage,
    archivo: tuple[bytes, str] | None = None,
) -> _Guardado:
    """
    Paso 1: upsert de cliente y conversación, y guardado del mensaje entrante
    (con su imagen, si la hay). Una transacción corta: si algo falla, no queda
    un cliente sin conversación ni un mensaje colgado.

    El mensaje se guarda YA, antes de la espera de ráfaga: así un turno
    posterior lo ve como pendiente y lo agrupa.
    """
    business_id = inbound.business_id

    async with tenant_session(business_id) as session:
        customer = await CustomerRepo(session).get_or_create(
            business_id,
            inbound.channel,
            inbound.channel_identifier,
            display_name=inbound.display_name,
        )
        conv_repo = ConversationRepo(session)
        conversation = await conv_repo.get_or_create_active(
            business_id, customer.id, inbound.channel
        )

        msg_repo = MessageRepo(session)
        if inbound.media is not None and inbound.media.content_type == ContentType.AUDIO:
            # Nota de voz: se guarda vacía y "pending"; la transcripción llega
            # después (`_transcribir_y_guardar`). El audio NO se guarda.
            entrante = await msg_repo.append(
                business_id,
                conversation.id,
                MessageRole.CUSTOMER,
                "",
                content_type=ContentType.AUDIO,
                transcription_status=TranscriptionStatus.PENDING.value,
            )
        elif inbound.media is not None:
            # Se guarda el pie de foto; el marcador para el modelo lo agrega
            # `text_for_model` al leer.
            entrante = await msg_repo.append(
                business_id,
                conversation.id,
                MessageRole.CUSTOMER,
                inbound.media.caption,
                content_type=inbound.media.content_type,
            )
            if archivo is not None:
                datos, mime_type = archivo
                await MediaRepo(session).create(
                    business_id,
                    entrante.id,
                    mime_type,
                    datos,
                    wa_media_id=inbound.media.external_media_id,
                )
        else:
            entrante = await msg_repo.append(
                business_id, conversation.id, MessageRole.CUSTOMER, inbound.text
            )
        await conv_repo.touch_last_message(business_id, conversation.id, inbound.received_at)

        return _Guardado(
            conversation.id,
            customer.id,
            entrante.id,
            entrante.seq,
            es_audio=entrante.content_type == ContentType.AUDIO.value,
        )


async def _load_turn(
    business_id: UUID, guardado: _Guardado
) -> tuple[ConversationContext, BusinessConfig, list[Message]]:
    """
    Paso 2: contexto del turno, ya dentro del candado de la conversación.

    - Pendientes: los mensajes del cliente que todavía no entraron a ningún turno
      (la ráfaga completa, en orden). Llegan al modelo juntos, como el mensaje nuevo.
    - Historial: todo lo demás. Los pendientes NO van ahí: si fueran, el modelo los
      vería dos veces (estudio/experimento_tinte.md).
    """
    async with tenant_session(business_id) as session:
        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.id == guardado.conversation_id,
                    Conversation.business_id == business_id,
                )
            )
        ).scalar_one()
        msg_repo = MessageRepo(session)
        atendidos = conversation.last_processed_seq
        pendientes = await msg_repo.pending_customer(
            business_id, guardado.conversation_id, atendidos
        )
        recent = await msg_repo.recent(
            business_id, guardado.conversation_id, answered_upto=atendidos
        )
        memoria = await MemoryRepo(session).get(business_id, guardado.customer_id)
        business = await BusinessRepo(session).get_config(business_id)

        ctx = ConversationContext(
            conversation_id=guardado.conversation_id,
            customer_id=guardado.customer_id,
            status=ConversationStatus(conversation.status),
            active_agent_id=conversation.active_agent_id,
            flow_state=dict(conversation.flow_state or {}),
            recent_turns=recent,
            customer_memory=memoria,
        )

    if business is None:
        raise RuntimeError(f"negocio {business_id} sin configuracion")

    return ctx, business, pendientes


async def _mark_processed(business_id: UUID, conversation_id: UUID, seq: int) -> None:
    async with tenant_session(business_id) as session:
        await ConversationRepo(session).mark_processed(business_id, conversation_id, seq)


async def _hay_mensaje_mas_nuevo(business_id: UUID, guardado: _Guardado) -> bool:
    async with tenant_session(business_id) as session:
        return await MessageRepo(session).has_newer_customer_message(
            business_id, guardado.conversation_id, guardado.seq
        )


# Un turno a la vez por conversación (invariante I2). En memoria del proceso:
# alcanza porque la API corre con UN worker en UNA instancia (Dockerfile). Con
# varias instancias habría que pasarlo a Postgres (pg_advisory_lock).
_candados: dict[UUID, asyncio.Lock] = {}
_en_uso: dict[UUID, int] = {}


@asynccontextmanager
async def _turno_exclusivo(conversation_id: UUID) -> AsyncIterator[None]:
    candado = _candados.setdefault(conversation_id, asyncio.Lock())
    _en_uso[conversation_id] = _en_uso.get(conversation_id, 0) + 1
    try:
        async with candado:
            yield
    finally:
        _en_uso[conversation_id] -= 1
        if _en_uso[conversation_id] == 0:
            # Nadie más lo usa ni lo espera: no acumular un candado por conversación.
            del _en_uso[conversation_id]
            _candados.pop(conversation_id, None)


# ======================================================================
# Pasos 4-5: CONTINUE y clasificacion
# ======================================================================
async def _classify_and_route(
    inbound: InboundMessage, ctx: ConversationContext, business_id: UUID
) -> tuple[Intent, AgentConfig | None, TurnUsage]:
    """
    Decide intencion y agente.

    Atajo CONTINUE (F5): si hay un flujo activo con datos parciales, se retoma el
    mismo agente SIN volver a clasificar. Ademas de ahorrar una llamada a Haiku
    (RNF-02), evita que un "sí" o un "mañana" suelto se re-clasifique como saludo
    y se pierda el hilo.
    """
    async with tenant_session(business_id) as session:
        agent_repo = AgentRepo(session)
        agentes = await agent_repo.list_active(business_id)

        if _hay_flujo_activo(ctx):
            activo = await agent_repo.get(business_id, ctx.active_agent_id)  # type: ignore[arg-type]
            # Solo un agente que recolecta datos (tiene herramientas) sostiene
            # un flujo; uno que solo informa no tiene nada que continuar.
            if activo is not None and activo.allowed_tools:
                logger.info("CONTINUE: se retoma el agente %s sin re-clasificar", activo.name)
                return Intent.CONTINUE, activo, TurnUsage()

    # Sin agentes configurados igual se clasifica: un saludo o una pregunta
    # ambigua los atiende el agente generico (persona + conocimiento del
    # negocio). Solo INFORM/ACT necesitan un agente de verdad, y para esos el
    # chequeo de mas abajo cae a FALLBACK honesto.
    if not agentes:
        logger.warning("negocio %s sin agentes activos", business_id)

    resultado: ClassifyResult = await classify(inbound.text, ctx, agentes)

    agente = None
    if resultado.agent_id:
        agente = next((a for a in agentes if str(a.id) == resultado.agent_id), None)

    intent = resultado.intent

    # INFORM/ACT sin agente que haga match: no hay quien lo atienda -> FALLBACK
    # honesto en vez de una respuesta inventada.
    if intent in (Intent.INFORM, Intent.ACT) and agente is None:
        logger.info("intencion %s sin agente que la cubra; cae a FALLBACK", intent.value)
        intent = Intent.FALLBACK

    return intent, agente, resultado.usage


def _hay_flujo_activo(ctx: ConversationContext) -> bool:
    """
    Flujo en curso = el agente activo es el que dejó el flujo abierto en el turno
    anterior (está recolectando datos y espera la respuesta).

    Las claves internas empiezan con `_`, pero `_intent` SI cuenta: es la marca
    de que el agente hizo una pregunta y espera la respuesta. Sin esto, un "el
    martes" suelto se re-clasificaria como saludo y se perderia el hilo (F5).

    `_fallos_consecutivos` no cuenta: un flujo que solo acumula errores no es un
    flujo que continuar.
    """
    if ctx.active_agent_id is None:
        return False
    # El flujo es del agente que lo abrió (`_agent`). Sin esta marca, una
    # aclaración del agente genérico dejaba el flujo "abierto" con el agente
    # activo de antes (el Informador) y la conversación quedaba pegada a él:
    # "quiero agendar" nunca llegaba al Agendador (prueba del 2026-11-25).
    return ctx.flow_state.get(CLAVE_AGENTE_DEL_FLUJO) == str(ctx.active_agent_id)


# ======================================================================
# Paso 6: handlers por intencion
# ======================================================================
async def _run_handler(
    intent: Intent,
    agent: AgentConfig | None,
    business: BusinessConfig,
    ctx: ConversationContext,
    inbound: InboundMessage,
) -> tuple[HandlerResult, TurnUsage]:
    """Ejecuta el flujo correspondiente (F1-F7)."""

    # F6 - ESCALATE: no se gasta una llamada al modelo en esto.
    if intent == Intent.ESCALATE:
        return (
            HandlerResult(reply_text=RESPUESTA_ESCALADA, escalate=True, new_flow_state={}),
            TurnUsage(),
        )

    # GREET y CLARIFY no necesitan un agente enrutado: son sociales o piden una
    # aclaracion. Se atienden con un agente generico del negocio (persona + tono
    # + conocimiento) para no escalar un simple "hola".
    if agent is None and intent in (Intent.GREET, Intent.CLARIFY):
        agent = _agente_generico(business, intent)

    # F7 - FALLBACK sin agente: respuesta honesta, sin inventar.
    if agent is None:
        return (
            HandlerResult(reply_text=RESPUESTA_SIN_AGENTES, escalate=True),
            TurnUsage(),
        )

    # F1/F2/F3/F4/F5 pasan por el modelo con el agente enrutado.
    permitidas = list(agent.allowed_tools)
    esquemas = tools_module.tool_schemas_for(permitidas)

    async def ejecutor(nombre: str, entrada: dict[str, Any]) -> str:
        """Inyecta el contexto del turno; el modelo no elige tenant ni cliente."""
        return await tools_module.execute_tool(
            nombre,
            entrada,
            business_id=business.id,
            customer_id=ctx.customer_id,
            conversation_id=ctx.conversation_id,
            allowed_tools=permitidas,
        )

    resultado: AgentTurnResult = await run_agent_turn(
        agent=agent,
        business=business,
        ctx=ctx,
        message=inbound.text,
        tools=esquemas or None,
        tool_executor=ejecutor if esquemas else None,
    )

    nuevo_flow_state = _calcular_flow_state(intent, ctx, resultado, agent)

    texto, usage, marcas = await _guardia_de_precios(
        resultado, agent=agent, business=business, ctx=ctx, inbound=inbound
    )
    texto, usage, marcas_promesa = await _guardia_de_promesas(
        texto, usage, resultado, agent=agent, business=business, ctx=ctx, inbound=inbound
    )
    marcas += marcas_promesa

    return (
        HandlerResult(
            reply_text=texto,
            tools_used=list(resultado.tools_used) + marcas,
            new_flow_state=nuevo_flow_state,
        ),
        usage,
    )


# ======================================================================
# Guardia de precios (invariante I1) — patrón evaluador-optimizador
# ======================================================================
GUARDIA_CORREGIDA: Final = "guardia:precio_corregido"
GUARDIA_BLOQUEADA: Final = "guardia:precio_bloqueado"

RESPUESTA_PRECIO_SEGURA = (
    "Perdoná, no quiero darte un precio equivocado. "
    "¿Querés que una persona del negocio te lo confirme?"
)


async def _guardia_de_precios(
    resultado: AgentTurnResult,
    *,
    agent: AgentConfig,
    business: BusinessConfig,
    ctx: ConversationContext,
    inbound: InboundMessage,
) -> tuple[str, TurnUsage, list[str]]:
    """
    Evalúa la respuesta contra I1 y, si falla, la corrige UNA vez.

    1. Evaluar (código, gratis): ¿cada precio existe y corresponde a lo que nombra?
    2. Si falla: se le devuelve al modelo qué estuvo mal y responde de nuevo,
       SIN herramientas (un reintento no debe registrar otra solicitud ni volver
       a avisar al dueño). La nota va en la parte dinámica: la caché no se toca.
    3. Si vuelve a fallar, o si el primer intento ya usó herramientas: respuesta
       segura sin precios. Nunca sale un precio que no pasó la guardia.

    Con catálogo (`business.catalog`), el modelo da los precios por referencia y
    el sistema los pone: un precio escrito a mano o una referencia desconocida
    también cuentan como falla. Ver `_evaluar_precios`.

    Devuelve (texto final, uso total, marcas para turn_logs.tools_used).
    """
    texto, problemas = _evaluar_precios(resultado.reply_text or RESPUESTA_ERROR, business)
    if not problemas:
        return texto, resultado.usage, []

    detalle = " ".join(problemas)
    logger.warning(
        "guardia de precios: respuesta rechazada (conversacion %s): %s",
        ctx.conversation_id,
        detalle,
    )

    if resultado.tools_used:
        return RESPUESTA_PRECIO_SEGURA, resultado.usage, [GUARDIA_BLOQUEADA]

    como_dar_precios = (
        "Para cada precio usá solo la referencia {{precio:clave}} del ítem que "
        "pregunta el cliente, nunca un número."
        if business.catalog
        else "Usá solo precios que estén en la información del negocio."
    )
    nota = (
        "\n\n[Verificación del sistema, no la menciones al cliente: tu respuesta "
        f"anterior tenía un problema con los precios. {detalle} Respondé de nuevo. "
        f"{como_dar_precios} Si no estás seguro, no des el precio.]"
    )
    try:
        segundo = await run_agent_turn(
            agent=agent,
            business=business,
            ctx=ctx,
            message=inbound.text + nota,
            tools=None,
            tool_executor=None,
        )
    except LLMError:
        logger.exception("fallo el reintento de la guardia de precios")
        return RESPUESTA_PRECIO_SEGURA, resultado.usage, [GUARDIA_BLOQUEADA]

    usage = _sumar_usage(resultado.usage, segundo.usage)
    if segundo.reply_text:
        texto_2, problemas_2 = _evaluar_precios(segundo.reply_text, business)
        if not problemas_2:
            return texto_2, usage, [GUARDIA_CORREGIDA]

    logger.warning("guardia de precios: el reintento tambien fallo; respuesta segura")
    return RESPUESTA_PRECIO_SEGURA, usage, [GUARDIA_BLOQUEADA]


# ======================================================================
# Guardia de promesas (invariante I8): no afirmar acciones que no se hicieron
# ======================================================================
GUARDIA_PROMESA_CORREGIDA: Final = "guardia:promesa_corregida"
GUARDIA_PROMESA_BLOQUEADA: Final = "guardia:promesa_bloqueada"

RESPUESTA_PROMESA_SEGURA: Final = (
    "Para eso necesito pasarte con una persona del equipo. ¿Quieres que le avise?"
)


async def _guardia_de_promesas(
    texto: str,
    usage: TurnUsage,
    resultado: AgentTurnResult,
    *,
    agent: AgentConfig,
    business: BusinessConfig,
    ctx: ConversationContext,
    inbound: InboundMessage,
) -> tuple[str, TurnUsage, list[str]]:
    """
    Invariante I8: annie nunca dice que pasó el caso, avisó, registró o agendó si
    en ESTE turno no lo hizo (no usó `capture_request` ni `notify_owner`).

    Mismo patrón que la guardia de precios: se le devuelve al modelo qué estuvo
    mal y responde UNA vez más, sin herramientas; si vuelve a prometer, sale una
    respuesta segura que OFRECE el pase en vez de afirmarlo. Si el cliente acepta,
    el siguiente turno escala de verdad.
    """
    respaldadas = {tools_module.CAPTURE_REQUEST, tools_module.NOTIFY_OWNER}
    if respaldadas & set(resultado.tools_used):
        return texto, usage, []

    promesas = unbacked_promises(texto)
    if not promesas:
        return texto, usage, []

    logger.warning(
        "guardia de promesas: respuesta rechazada (conversacion %s): %s",
        ctx.conversation_id,
        " | ".join(promesas),
    )
    nota = (
        "\n\n[Verificación del sistema, no la menciones al cliente: en tu respuesta "
        f"anterior dijiste «{promesas[0]}», pero en este turno NO pasaste el caso ni "
        "registraste ni avisaste a nadie. No afirmes acciones que no hiciste. Si el "
        "cliente quiere agendar o hablar con una persona, OFRECÉSELO como pregunta.]"
    )
    try:
        segundo = await run_agent_turn(
            agent=agent,
            business=business,
            ctx=ctx,
            message=inbound.text + nota,
            tools=None,
            tool_executor=None,
        )
    except LLMError:
        logger.exception("fallo el reintento de la guardia de promesas")
        return RESPUESTA_PROMESA_SEGURA, usage, [GUARDIA_PROMESA_BLOQUEADA]

    usage = _sumar_usage(usage, segundo.usage)
    if segundo.reply_text:
        texto_2, problemas_precio = _evaluar_precios(segundo.reply_text, business)
        if not problemas_precio and not unbacked_promises(texto_2):
            return texto_2, usage, [GUARDIA_PROMESA_CORREGIDA]

    logger.warning("guardia de promesas: el reintento tambien prometio; respuesta segura")
    return RESPUESTA_PROMESA_SEGURA, usage, [GUARDIA_PROMESA_BLOQUEADA]


def _evaluar_precios(texto_modelo: str, business: BusinessConfig) -> tuple[str, list[str]]:
    """
    Convierte lo que escribió el modelo en lo que verá el cliente, y lista los
    problemas de precios. Sin problemas => el texto devuelto se puede enviar.

    Con catálogo (garantía fuerte):
      1. Ningún monto de dinero escrito a mano: todo precio va por referencia.
      2. Toda referencia existe en el catálogo.
      3. Se reemplazan las referencias por el precio real.
      4. El texto final pasa la guardia de existencia y atribución contra el
         conocimiento + el catálogo (atrapa "el tinte vale {{precio:corte}}").
    Sin catálogo: solo el paso 4 contra el conocimiento.
    """
    if not business.catalog:
        sin_catalogo = check_prices(texto_modelo, business.knowledge_context)
        return texto_modelo, [p.describe() for p in sin_catalogo]

    problemas: list[str] = [
        f"Escribiste el precio «{monto}» a mano." for monto in hand_written_amounts(texto_modelo)
    ]
    renderizado = render_prices(texto_modelo, business.catalog)
    problemas += [
        f"La referencia «{ref}» no existe en el catálogo." for ref in renderizado.unknown_refs
    ]
    if problemas:
        return renderizado.text, problemas

    fuente = "\n\n".join(
        x for x in (business.knowledge_context, verification_lines(business.catalog)) if x
    )
    problemas += [p.describe() for p in check_prices(renderizado.text, fuente)]
    return renderizado.text, problemas


INSTRUCCIONES_GREET = (
    "# Rol\n"
    "Atendés el saludo inicial del cliente.\n\n"
    "# Tarea\n"
    "Respondé con calidez y brevedad, y preguntá en qué podés ayudar.\n\n"
    "# Reglas\n"
    "- No inventes datos del negocio.\n"
    "- Un par de frases, nada más."
)

INSTRUCCIONES_CLARIFY = (
    "# Rol\n"
    "El mensaje del cliente es ambiguo.\n\n"
    "# Tarea\n"
    "Hacé UNA sola pregunta, concreta, para entender qué necesita.\n\n"
    "# Reglas\n"
    "- Una pregunta, no varias.\n"
    "- No inventes datos del negocio ni supongas lo que quiso decir."
)


def _es_generico(agent: AgentConfig) -> bool:
    """Un agente sintetico de GREET/CLARIFY, que no existe en la tabla."""
    return agent.name.startswith("generico:")


def _agente_generico(business: BusinessConfig, intent: Intent) -> AgentConfig:
    """
    Agente sintetico para GREET y CLARIFY.

    No sale de la base: son flujos que no dependen de la configuracion del
    tenant, solo de su persona y su conocimiento. Sin herramientas (RF-07: un
    saludo no captura solicitudes). El id es el del negocio para que
    `routed_agent_id` en `turn_logs` no apunte a un agente inexistente.
    """
    return AgentConfig(
        id=business.id,
        business_id=business.id,
        name=f"generico:{intent.value}",
        routing_description="",
        instructions_md=(
            INSTRUCCIONES_GREET if intent == Intent.GREET else INSTRUCCIONES_CLARIFY
        ),
        allowed_tools=[],
        response_style=business.persona_tone,
    )


def _calcular_flow_state(
    intent: Intent,
    ctx: ConversationContext,
    resultado: AgentTurnResult,
    agent: AgentConfig | None = None,
) -> dict[str, Any] | None:
    """
    Decide como queda el flujo tras el turno.

    - Si se capturo la solicitud, el flujo termina: se limpia (evita que el
      proximo mensaje entre por el atajo CONTINUE y quede pegado).
    - Solo un agente que RECOLECTA datos (de la tabla y con herramientas) deja un
      flujo abierto en ACT/CONTINUE, marcado con su id (`_agent`).
    - Todo lo demás (saludar, informar, una aclaración del agente genérico) no
      deja flujo: el próximo mensaje se vuelve a clasificar.
    """
    if any(t == tools_module.CAPTURE_REQUEST for t in resultado.tools_used):
        return {}

    recolecta = agent is not None and not _es_generico(agent) and bool(agent.allowed_tools)
    if intent in (Intent.ACT, Intent.CONTINUE) and recolecta:
        assert agent is not None
        estado = dict(ctx.flow_state)
        estado["_intent"] = intent.value
        estado[CLAVE_AGENTE_DEL_FLUJO] = str(agent.id)
        estado.pop(CLAVE_FALLOS, None)  # hubo turno exitoso
        return estado

    if ctx.flow_state:
        return {}

    return None


def _incrementar_fallos(flow_state: dict[str, Any]) -> dict[str, Any]:
    estado = dict(flow_state)
    estado[CLAVE_FALLOS] = int(estado.get(CLAVE_FALLOS, 0)) + 1
    return estado


def _demasiados_fallos(flow_state: dict[str, Any] | None) -> bool:
    if not flow_state:
        return False
    return int(flow_state.get(CLAVE_FALLOS, 0)) >= MAX_FALLOS_ANTES_DE_ESCALAR


# ======================================================================
# Paso 7: persistencia
# ======================================================================
async def _persist_turn(
    inbound: InboundMessage,
    ctx: ConversationContext,
    intent: Intent,
    agent: AgentConfig | None,
    result: HandlerResult,
    usage: TurnUsage,
    message_in_id: UUID | None,
    latencia_ms: int,
    processed_upto_seq: int,
) -> None:
    """
    Guarda la respuesta y todos los efectos del turno en una sola transaccion.

    Marca como atendidos los mensajes del cliente hasta `processed_upto_seq`, en
    la MISMA transacción que la respuesta: o quedan los dos, o ninguno.

    `turn_logs` se escribe SIEMPRE (RF-15): es la base de la metrica de costo por
    conversacion y del ahorro de cache (RNF-02).
    """
    business_id = inbound.business_id

    async with tenant_session(business_id) as session:
        msg_repo = MessageRepo(session)
        salida = await msg_repo.append(
            business_id, ctx.conversation_id, MessageRole.AGENT, result.reply_text
        )

        conv_repo = ConversationRepo(session)
        await conv_repo.mark_processed(business_id, ctx.conversation_id, processed_upto_seq)

        if result.new_flow_state is not None:
            await conv_repo.update_flow_state(
                business_id, ctx.conversation_id, result.new_flow_state
            )

        # Los agentes genericos (GREET/CLARIFY) son sinteticos: no existen en la
        # tabla `agents`, asi que no pueden guardarse como agente activo ni como
        # `routed_agent_id` (romperia la FK).
        agent_id = agent.id if agent is not None and not _es_generico(agent) else None

        if agent_id is not None:
            await conv_repo.set_active_agent(business_id, ctx.conversation_id, agent_id)

        # F6: modo humano + registro del escalamiento.
        if result.escalate:
            await conv_repo.set_status(
                business_id, ctx.conversation_id, ConversationStatus.HUMAN
            )
            await EscalationRepo(session).create(
                business_id,
                ctx.conversation_id,
                ctx.customer_id,
                reason=f"intencion={intent.value}",
            )

        await TurnLogRepo(session).append(
            business_id=business_id,
            conversation_id=ctx.conversation_id,
            message_in_id=message_in_id,
            message_out_id=salida.id,
            detected_intent=intent.value,
            routed_agent_id=agent_id,
            tools_used=list(result.tools_used),
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_estimate_usd=usage.cost_estimate_usd,
            latency_ms=latencia_ms,
        )

    # El aviso al dueño va fuera de la transaccion: es un efecto externo y no
    # debe abortar el guardado si falla.
    if result.escalate:
        try:
            await tools_module.notify_owner(
                business_id,
                subject="Conversación escalada",
                body=(
                    f"El cliente {inbound.channel_identifier} necesita atención humana.\n"
                    f"Último mensaje: {inbound.text}"
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("no se pudo avisar del escalamiento")


# ======================================================================
# Paso 8: envio
# ======================================================================
async def _send_reply(
    inbound: InboundMessage, texto: str, phone_number_id: str | None
) -> None:
    """Envia la respuesta. Un fallo aqui se loguea; el turno ya quedo guardado."""
    # El sandbox del panel corre el mismo loop pero no manda nada por WhatsApp:
    # la respuesta se lee del turno persistido (RF-23).
    if inbound.channel != "whatsapp":
        logger.info("canal %s: no se envia por WhatsApp", inbound.channel)
        return

    try:
        await send_whatsapp_reply(
            OutboundMessage(
                business_id=inbound.business_id,
                channel=inbound.channel,
                channel_identifier=inbound.channel_identifier,
                text=texto,
            ),
            phone_number_id=phone_number_id,
        )
    except WhatsAppError:
        logger.exception("no se pudo enviar la respuesta al cliente")


def _sumar_usage(a: TurnUsage, b: TurnUsage) -> TurnUsage:
    return TurnUsage(
        model_calls=a.model_calls + b.model_calls,
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cost_estimate_usd=(a.cost_estimate_usd or 0.0) + (b.cost_estimate_usd or 0.0) or None,
    )


__all__ = ["TurnOutcome", "TurnOutcomeKind", "handle_inbound_message"]
