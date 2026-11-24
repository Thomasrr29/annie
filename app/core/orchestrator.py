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

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final
from uuid import UUID

from app.core import tools as tools_module
from app.core.catalog import render_prices, verification_lines
from app.core.channel_whatsapp import (
    WhatsAppError,
    download_whatsapp_media,
    send_whatsapp_reply,
)
from app.core.guards import check_prices, hand_written_amounts
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
    ConversationContext,
    ConversationStatus,
    HandlerResult,
    InboundMessage,
    Intent,
    MessageRole,
    OutboundMessage,
    TurnUsage,
)

logger = logging.getLogger(__name__)

# Fallos seguidos del agente en una conversacion antes de escalar solo.
MAX_FALLOS_ANTES_DE_ESCALAR = 3
CLAVE_FALLOS = "_fallos_consecutivos"

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
    # ni llamadas al modelo (fase 7, proteccion de costo).
    if not check_rate_limit(business_id, inbound.channel_identifier):
        await _send_reply(inbound, RESPUESTA_RATE_LIMIT, phone_number_id)
        return TurnOutcome(TurnOutcomeKind.RATE_LIMITED, RESPUESTA_RATE_LIMIT, None)

    # La URL de descarga de Meta expira en minutos: la imagen se baja ahora,
    # antes de abrir la transaccion (no se retiene una conexion durante la red).
    archivo = await _descargar_media(inbound)

    ctx, business, message_in_id = await _resolve_and_load(inbound, archivo)

    # Paso 3: modo humano -> el agente calla (F6, RF-12).
    if ctx.status == ConversationStatus.HUMAN:
        logger.info("conversacion %s en modo humano; no se responde", ctx.conversation_id)
        return TurnOutcome(TurnOutcomeKind.HUMAN_MODE, None, message_in_id)

    # Circuit breaker de costo por tenant: al superarlo se escala a humano en vez
    # de seguir gastando turnos.
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
        )
        await _send_reply(inbound, RESPUESTA_LIMITE_DIARIO, phone_number_id)
        return TurnOutcome(TurnOutcomeKind.DAILY_LIMIT, RESPUESTA_LIMITE_DIARIO, message_in_id)

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

    latencia_ms = int((time.monotonic() - inicio) * 1000)
    await _persist_turn(inbound, ctx, intent, agent, result, usage, message_in_id, latencia_ms)

    # Aviso de tratamiento de datos en el PRIMER contacto (RNF-05, Ley 1581).
    # Va pegado a la primera respuesta para no gastar un mensaje aparte.
    texto = result.reply_text
    if await necesita_aviso_privacidad(business_id, ctx.customer_id):
        texto = f"{texto}\n\n---\n{AVISO_PRIVACIDAD}"
        await marcar_aviso_enviado(business_id, ctx.customer_id)

    await _send_reply(inbound, texto, phone_number_id)
    return TurnOutcome(TurnOutcomeKind.REPLIED, texto, message_in_id)


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
    try:
        return await download_whatsapp_media(inbound.media.external_media_id)
    except WhatsAppError:
        logger.warning(
            "no se pudo descargar la imagen del mensaje %s", inbound.external_message_id
        )
        return None


async def _resolve_and_load(
    inbound: InboundMessage,
    archivo: tuple[bytes, str] | None = None,
) -> tuple[ConversationContext, BusinessConfig, UUID | None]:
    """
    Upsert de cliente, conversacion activa, guardado del mensaje entrante (con
    su imagen, si la hay) y carga del contexto (historial + memoria).

    Todo en una transaccion: si algo falla, no queda un cliente sin conversacion
    ni un mensaje colgado.
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

        # El historial se lee ANTES de guardar el entrante: el mensaje nuevo le
        # llega al modelo aparte (`message` en classify/run_agent_turn, y
        # build_messages lo agrega al final). Leerlo después lo duplicaba en el
        # prompt, y con eso el modelo confundía precios (estudio/experimento_tinte.md:
        # 6 de 12 respuestas con el precio de otro servicio).
        recent = await msg_repo.recent(business_id, conversation.id)

        if inbound.media is not None:
            # Se guarda el pie de foto; el marcador para el modelo lo agrega
            # `recent()` al leer el historial.
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

        memoria = await MemoryRepo(session).get(business_id, customer.id)
        business = await BusinessRepo(session).get_config(business_id)

        ctx = ConversationContext(
            conversation_id=conversation.id,
            customer_id=customer.id,
            status=ConversationStatus(conversation.status),
            active_agent_id=conversation.active_agent_id,
            flow_state=dict(conversation.flow_state or {}),
            recent_turns=recent,
            customer_memory=memoria,
        )
        message_in_id = entrante.id

    if business is None:
        raise RuntimeError(f"negocio {business_id} sin configuracion")

    return ctx, business, message_in_id


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
            if activo is not None:
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
    Flujo en curso = hay un agente activo y el turno anterior dejo el flujo
    abierto (`_intent`), o hay datos parciales del cliente.

    Las claves internas empiezan con `_`, pero `_intent` SI cuenta: es la marca
    de que el agente hizo una pregunta y espera la respuesta. Sin esto, un "el
    martes" suelto se re-clasificaria como saludo y se perderia el hilo (F5).

    `_fallos_consecutivos` no cuenta: un flujo que solo acumula errores no es un
    flujo que continuar.
    """
    if ctx.active_agent_id is None:
        return False
    relevantes = {
        k: v for k, v in ctx.flow_state.items() if k != CLAVE_FALLOS and v not in (None, "")
    }
    return bool(relevantes)


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

    nuevo_flow_state = _calcular_flow_state(intent, ctx, resultado)

    texto, usage, marcas = await _guardia_de_precios(
        resultado, agent=agent, business=business, ctx=ctx, inbound=inbound
    )

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
    intent: Intent, ctx: ConversationContext, resultado: AgentTurnResult
) -> dict[str, Any] | None:
    """
    Decide como queda el flujo tras el turno.

    - Si se capturo la solicitud, el flujo termina: se limpia (evita que el
      proximo mensaje entre por el atajo CONTINUE y quede pegado).
    - En CLARIFY/ACT/CONTINUE se marca que hay un flujo abierto.
    - En GREET/INFORM no hay flujo que sostener.
    """
    if any(t == tools_module.CAPTURE_REQUEST for t in resultado.tools_used):
        return {}

    if intent in (Intent.ACT, Intent.CLARIFY, Intent.CONTINUE):
        estado = dict(ctx.flow_state)
        estado["_intent"] = intent.value
        estado.pop(CLAVE_FALLOS, None)  # hubo turno exitoso
        return estado

    if intent in (Intent.GREET, Intent.INFORM) and ctx.flow_state:
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
) -> None:
    """
    Guarda la respuesta y todos los efectos del turno en una sola transaccion.

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
