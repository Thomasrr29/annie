"""
========================================================================
MVP Agente de Soporte — Scaffold de la API (FastAPI)
========================================================================
Esqueleto de diseño: tipados (Pydantic), endpoints, flujo de datos y
firmas de funciones. Los cuerpos son STUBS con el flujo documentado en
comentarios; se implementan en el siguiente paso.

Referencias:
  - Tablas:  MVP_Estructura_Base_de_Datos.md  (businesses, agents, ...)
  - Flujos:  MVP_Agente_Soporte_Alcance_Requerimientos.md  (F1..F7, RF-XX)

Estructura de proyecto sugerida (no es un solo archivo en producción):
  app/
    schemas.py        <- modelos Pydantic (este bloque 1)
    api/
      webhooks.py     <- superficie RUNTIME (bloque 2a)
      admin.py        <- superficie ADMIN  (bloque 2b)
    core/
      orchestrator.py <- el loop del agente (bloque 3)
      tools.py        <- herramientas integradas (bloque 4)
      llm.py          <- wrapper del cliente Anthropic (Haiku/Sonnet + caché)
    db/
      repositories.py <- acceso a datos (async, filtrado por business_id)
========================================================================
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, FastAPI, Depends
from pydantic import BaseModel, Field


# ========================================================================
# BLOQUE 1 — SCHEMAS / TIPADOS (Pydantic)
# ========================================================================

# --- Enumeraciones (contratos cerrados; evitan strings sueltos) ---------

class Intent(str, Enum):
    INFORM = "inform"        # F1: consulta de información
    ACT = "act"              # F2: ejecutar/capturar acción
    GREET = "greet"          # F3: saludo / social
    CLARIFY = "clarify"      # F4: pedir aclaración
    CONTINUE = "continue"    # F5: continuar flujo activo
    ESCALATE = "escalate"    # F6: derivar a humano
    FALLBACK = "fallback"    # F7: fuera de alcance


class ConversationStatus(str, Enum):
    AUTO = "auto"            # el agente responde
    HUMAN = "human"          # modo humano: el agente calla


class MessageRole(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"          # operador del negocio


# --- Mensaje normalizado (la frontera entre canal y orquestador) --------
# Cualquier canal (WhatsApp hoy; email/web mañana) se traduce a ESTO.

class InboundMessage(BaseModel):
    """Formato común, agnóstico al canal. Lo produce el adaptador."""
    business_id: UUID
    channel: str = "whatsapp"
    channel_identifier: str            # nº de teléfono en WhatsApp
    text: str
    external_message_id: str | None = None   # id del mensaje en WhatsApp
    received_at: datetime


class OutboundMessage(BaseModel):
    """Respuesta a enviar de vuelta por el canal saliente."""
    business_id: UUID
    channel: str
    channel_identifier: str
    text: str


# --- Configuración cargada en runtime (lookup por clave) ----------------

class BusinessConfig(BaseModel):
    id: UUID
    name: str
    persona_tone: str | None = None
    language: str = "es"
    timezone: str = "America/Bogota"
    business_hours: dict = Field(default_factory=dict)
    notify_channel: str | None = None
    notify_config: dict = Field(default_factory=dict)
    # Conocimiento "base completa": texto estable que va al prefijo cacheado.
    knowledge_context: str | None = None


class AgentConfig(BaseModel):
    """Un 'agente' es config, no un proceso aparte. El orquestador enruta
    entre estos según routing_description (RF-06)."""
    id: UUID
    business_id: UUID
    name: str
    routing_description: str           # lo que el orquestador lee para enrutar
    instructions_md: str               # cuerpo .md (tarea/persona)
    allowed_tools: list[str] = Field(default_factory=list)  # allowlist (RF-07)
    memory_scope: str = "customer"     # 'none' | 'customer'
    response_style: str | None = None
    status: str = "active"


# --- Contexto de conversación (paso "cargar contexto" del loop) ---------

class ConversationTurn(BaseModel):
    role: MessageRole
    content: str
    created_at: datetime


class CustomerMemory(BaseModel):
    customer_id: UUID
    profile: dict = Field(default_factory=dict)   # nombre, preferencias
    summary: str | None = None


class ConversationContext(BaseModel):
    """Todo lo que el orquestador necesita para razonar este turno."""
    conversation_id: UUID
    customer_id: UUID
    status: ConversationStatus
    active_agent_id: UUID | None = None
    flow_state: dict = Field(default_factory=dict)      # datos parciales (F5)
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    customer_memory: CustomerMemory | None = None


# --- I/O de herramientas integradas -------------------------------------

class KnowledgeHit(BaseModel):
    content: str
    score: float
    metadata: dict = Field(default_factory=dict)


class CaptureRequestInput(BaseModel):
    request_type: str                  # 'agenda' | 'pedido' | ...
    structured_data: dict


# --- Modelos de la API ADMIN (request/response del panel) ---------------

class AgentCreate(BaseModel):
    name: str
    routing_description: str
    instructions_md: str
    allowed_tools: list[str] = Field(default_factory=list)
    memory_scope: str = "customer"
    response_style: str | None = None


class KnowledgeUpload(BaseModel):
    title: str
    source: str = "upload"
    raw_content: str


class WhatsAppAck(BaseModel):
    status: str = "received"


# ========================================================================
# BLOQUE 2a — SUPERFICIE RUNTIME (webhook de WhatsApp)
# ========================================================================
# Patrón clave: ACK inmediato + procesamiento en segundo plano.
# El cliente NO espera a que corra el loop del agente.

runtime = APIRouter(prefix="/webhooks", tags=["runtime"])


@runtime.post("/whatsapp", response_model=WhatsAppAck)
async def whatsapp_webhook(payload: dict, background: BackgroundTasks) -> WhatsAppAck:
    """
    Flujo de datos:
      1. Recibe el payload crudo de WhatsApp (vía BSP/Cloud API).
      2. El adaptador lo normaliza a InboundMessage (agnóstico al canal).
      3. Dispara handle_inbound_message en segundo plano.
      4. Responde 200 de inmediato (WhatsApp reintenta si tardas).
    """
    inbound: InboundMessage = adapt_whatsapp_payload(payload)   # bloque adaptador
    background.add_task(handle_inbound_message, inbound)
    return WhatsAppAck()


def adapt_whatsapp_payload(payload: dict) -> InboundMessage:
    """Adaptador de canal: payload crudo de WhatsApp -> InboundMessage.
    Aquí se resuelve a qué business_id pertenece el número destino."""
    raise NotImplementedError  # STUB


# ========================================================================
# BLOQUE 2b — SUPERFICIE ADMIN (CRUD de configuración del panel)
# ========================================================================
# Toda dependencia inyecta el tenant autenticado y fija
# app.current_business_id en la transacción (RLS, RNF-04).

admin = APIRouter(prefix="/admin", tags=["admin"])


async def current_business(...) -> UUID:  # type: ignore[valid-type]
    """Resuelve el negocio autenticado desde el token del panel y fija el
    contexto de tenant para RLS. STUB."""
    raise NotImplementedError


# --- Agentes ---
@admin.get("/agents")
async def list_agents(business_id: UUID = Depends(current_business)) -> list[AgentConfig]:
    ...

@admin.post("/agents")
async def create_agent(
    body: AgentCreate, business_id: UUID = Depends(current_business)
) -> AgentConfig:
    """INSERT en agents. No toca el orquestador: enrutamiento por datos (RF-06)."""
    ...

@admin.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: UUID, body: AgentCreate, business_id: UUID = Depends(current_business)
) -> AgentConfig:
    ...


# --- Conocimiento ---
@admin.post("/knowledge")
async def upload_knowledge(
    body: KnowledgeUpload, business_id: UUID = Depends(current_business)
) -> dict:
    """INSERT en knowledge_documents. En 'base completa' basta con guardar el
    texto; no hay pipeline de embeddings todavía. RF-19."""
    ...


# --- Bandejas (RF-21) ---
@admin.get("/requests")
async def list_captured_requests(business_id: UUID = Depends(current_business)) -> list[dict]:
    ...

@admin.get("/escalations")
async def list_escalations(business_id: UUID = Depends(current_business)) -> list[dict]:
    ...

@admin.get("/metrics")
async def get_metrics(business_id: UUID = Depends(current_business)) -> dict:
    """Lee turn_logs: volumen, % resuelto sin humano, escalamientos, costo."""
    ...


# ========================================================================
# BLOQUE 3 — ORQUESTADOR (el loop del agente)
# ========================================================================
# Corre en segundo plano. Es el corazón: un loop delgado sobre la API de
# Anthropic. NO usa Claude Agent SDK ni LangGraph en el MVP.

async def handle_inbound_message(inbound: InboundMessage) -> None:
    """
    Loop completo de un turno. Cada paso indica qué tabla toca (R/W).

      1. resolve...      -> customers (upsert), conversations (busca/crea),
                            messages (W: mensaje entrante)
      2. load_context    -> messages (R: últimos N), customer_memory (R)
      3. si status==HUMAN -> return (modo humano: el agente calla, F6)
      4. classify_intent -> agents (R: lista activa)          [HAIKU]
      5. route_to_agent  -> agents (R: match)
      6. run_handler     -> según intención (F1..F7)          [SONNET + tools]
      7. compose+persist -> messages (W: respuesta), turn_logs (W)
      8. send_reply      -> llamada SALIENTE a la API de WhatsApp
    """
    ctx = await resolve_customer_and_conversation(inbound)

    if ctx.status == ConversationStatus.HUMAN:
        return  # un operador está atendiendo; no responder (F6)

    intent, agent = await classify_and_route(inbound, ctx)
    result = await run_handler(intent, agent, inbound, ctx)

    await persist_turn(inbound, ctx, intent, agent, result)
    await send_whatsapp_reply(
        OutboundMessage(
            business_id=inbound.business_id,
            channel=inbound.channel,
            channel_identifier=inbound.channel_identifier,
            text=result.reply_text,
        )
    )


async def resolve_customer_and_conversation(inbound: InboundMessage) -> ConversationContext:
    """Paso 1-2. Identidad por (business_id, channel, channel_identifier).
    Crea customer/conversation si no existen; guarda el mensaje entrante;
    carga últimos N turnos + memoria. R/W: customers, conversations, messages,
    customer_memory."""
    raise NotImplementedError  # STUB


class ClassifyRouteResult(BaseModel):
    intent: Intent
    agent: AgentConfig | None = None


async def classify_and_route(
    inbound: InboundMessage, ctx: ConversationContext
) -> tuple[Intent, AgentConfig | None]:
    """Pasos 4-5. Si hay flujo activo y el mensaje lo continúa -> CONTINUE
    (no re-clasificar desde cero, F5). Si no, clasifica con HAIKU sobre las
    routing_description de los agentes activos del tenant. R: agents."""
    raise NotImplementedError  # STUB


class HandlerResult(BaseModel):
    reply_text: str
    tools_used: list[str] = Field(default_factory=list)
    new_flow_state: dict | None = None
    escalate: bool = False


async def run_handler(
    intent: Intent,
    agent: AgentConfig | None,
    inbound: InboundMessage,
    ctx: ConversationContext,
) -> HandlerResult:
    """
    Paso 6. Ensambla el prompt y ejecuta el turno con SONNET.

    Estructura del prompt (para maximizar el caché — RNF-02):
      [ system: instructions_md + persona + knowledge_context ] <- PREFIJO CACHEADO
      [ definiciones de herramientas (allowed_tools) ]          <- PREFIJO CACHEADO
      --- cache_control breakpoint ---
      [ recent_turns + flow_state ]                             <- dinámico
      [ mensaje nuevo del cliente ]                             <- dinámico

    Según la intención:
      INFORM  -> con 'base completa' el conocimiento ya está en el prefijo;
                 el modelo responde directo (sin búsqueda).
      ACT     -> puede invocar capture_request + notify_owner.
      GREET   -> respuesta ligera, sin herramientas.
      CLARIFY -> una pregunta; guarda parcial en flow_state.
      ESCALATE-> marca escalate=True (lo procesa persist_turn).
      FALLBACK-> respuesta honesta; puede ofrecer escalar.
    """
    raise NotImplementedError  # STUB


async def persist_turn(
    inbound: InboundMessage,
    ctx: ConversationContext,
    intent: Intent,
    agent: AgentConfig | None,
    result: HandlerResult,
) -> None:
    """Paso 7. W: messages (respuesta del agente), turn_logs (intención,
    herramientas, tokens, cache_read_tokens, costo, latencia).
    Si result.escalate -> W: conversations.status='human' + escalations (F6).
    Si result.new_flow_state -> W: conversations.flow_state (F4/F5).
    Actualización deliberada de customer_memory si aplica."""
    raise NotImplementedError  # STUB


async def send_whatsapp_reply(msg: OutboundMessage) -> None:
    """Paso 8. Llamada SALIENTE a la API de WhatsApp (dentro de la ventana
    de 24h => gratis). STUB."""
    raise NotImplementedError


# ========================================================================
# BLOQUE 4 — HERRAMIENTAS INTEGRADAS (RF-09..RF-11)
# ========================================================================
# Cada herramienta valida business_id internamente. Un agente solo puede
# invocar las que estén en su allowed_tools (se aplica en CÓDIGO, no en .md).

async def search_knowledge(
    business_id: UUID, query: str, top_k: int = 5
) -> list[KnowledgeHit]:
    """RF-09. (Diferida en 'base completa': el conocimiento va en el prefijo.
    Se activará cuando un tenant crezca a RAG.) R: knowledge_chunks."""
    raise NotImplementedError


async def capture_request(
    business_id: UUID, customer_id: UUID, conversation_id: UUID, data: CaptureRequestInput
) -> UUID:
    """RF-10. W: captured_requests. Devuelve el id de la solicitud."""
    raise NotImplementedError


async def notify_owner(business_id: UUID, subject: str, body: str) -> None:
    """RF-11. R: businesses.notify_config; envío por el medio configurado."""
    raise NotImplementedError


# ========================================================================
# APP
# ========================================================================

app = FastAPI(title="Agente de Soporte — MVP")
app.include_router(runtime)
app.include_router(admin)
