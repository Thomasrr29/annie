"""
Modelos Pydantic: contratos de frontera (canal <-> orquestador <-> API).

Portado de `app/scaffold_referencia.py` bloque 1, que es el contrato de diseno.
Las enumeraciones evitan strings sueltos entre capas.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------
# Enumeraciones (contratos cerrados)
# ------------------------------------------------------------------
class Intent(str, Enum):
    """Los 7 flujos conversacionales F1-F7."""

    INFORM = "inform"      # F1: consulta de informacion
    ACT = "act"            # F2: ejecutar/capturar accion
    GREET = "greet"        # F3: saludo / social
    CLARIFY = "clarify"    # F4: pedir aclaracion
    CONTINUE = "continue"  # F5: continuar flujo activo
    ESCALATE = "escalate"  # F6: derivar a humano
    FALLBACK = "fallback"  # F7: fuera de alcance


class ConversationStatus(str, Enum):
    AUTO = "auto"    # el agente responde
    HUMAN = "human"  # modo humano: el agente calla (F6)


class MessageRole(str, Enum):
    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    HUMAN = "human"  # operador del negocio


class RequestStatus(str, Enum):
    NEW = "new"
    SEEN = "seen"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class EscalationStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class MemoryScope(str, Enum):
    NONE = "none"
    CUSTOMER = "customer"


class ContentType(str, Enum):
    """Tipo de mensaje. La lista la cierra un CHECK en la base (migraciones 0011 y 0014)."""

    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"  # nota de voz: `content` es su transcripción


# Lo que lee el modelo en lugar de una imagen: el MVP no le pasa imagenes al LLM
# (costo y alcance), asi que el agente sabe que hay una foto pero no la ve.
IMAGE_PLACEHOLDER = "[El cliente envió una imagen]"


# Lo que lee el modelo de una nota de voz, según cómo terminó su transcripción.
# El texto del cliente va siempre DESPUÉS del marcador: es dato, no instrucción.
AUDIO_PLACEHOLDERS: dict[str, str] = {
    "done": "[Nota de voz del cliente, transcrita]",
    "pending": "[El cliente envió una nota de voz que todavía se está transcribiendo]",
    "failed": "[El cliente envió una nota de voz que no se pudo escuchar]",
    "too_long": "[El cliente envió una nota de voz de más de 2 minutos: no se transcribió]",
}


def text_for_model(
    content_type: ContentType | str, content: str, transcription_status: str | None = None
) -> str:
    """Texto de un mensaje tal como lo ve el modelo (imagen/audio -> marcador + contenido)."""
    tipo = ContentType(content_type)
    if tipo == ContentType.IMAGE:
        pie = content.strip()
        return f"{IMAGE_PLACEHOLDER} {pie}" if pie else IMAGE_PLACEHOLDER
    if tipo == ContentType.AUDIO:
        estado = transcription_status or "pending"
        marcador = AUDIO_PLACEHOLDERS.get(estado, AUDIO_PLACEHOLDERS["pending"])
        texto = content.strip()
        return f"{marcador} {texto}" if texto and transcription_status == "done" else marcador
    return content


# ------------------------------------------------------------------
# Mensaje normalizado (frontera canal <-> orquestador)
# ------------------------------------------------------------------
class InboundMedia(BaseModel):
    """Referencia a un archivo del canal. El adaptador no lo descarga."""

    content_type: ContentType
    external_media_id: str
    mime_type: str
    caption: str = ""


class InboundMessage(BaseModel):
    """Formato comun, agnostico al canal. Lo produce el adaptador."""

    business_id: UUID
    channel: str = "whatsapp"
    channel_identifier: str  # numero de telefono en WhatsApp
    text: str
    external_message_id: str | None = None
    received_at: datetime
    # Nombre del perfil del canal, si viene. Sirve para saludar por el nombre sin
    # tener que preguntarlo.
    display_name: str | None = None
    # Adjunto, si el mensaje no es texto. `text` trae entonces lo que lee el
    # modelo (marcador + pie de foto, ver `text_for_model`).
    media: InboundMedia | None = None


class OutboundMessage(BaseModel):
    """Respuesta a enviar de vuelta por el canal saliente."""

    business_id: UUID
    channel: str
    channel_identifier: str
    text: str


# ------------------------------------------------------------------
# Configuracion cargada en runtime
# ------------------------------------------------------------------
class CatalogEntry(BaseModel):
    """Un ítem del catálogo tal como lo usa el orquestador."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    price: int
    currency: str = "COP"


class BusinessConfig(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    persona_tone: str | None = None
    language: str = "es"
    timezone: str = "America/Bogota"
    business_hours: dict[str, Any] = Field(default_factory=dict)
    notify_channel: str | None = None
    notify_config: dict[str, Any] = Field(default_factory=dict)
    # Conocimiento "base completa": texto estable que va al prefijo cacheado
    # del prompt (RNF-02). Debe ser deterministico: mismo input => mismo string.
    knowledge_context: str | None = None
    # Lo que vende el negocio (activo), en orden estable. Va al prefijo cacheado
    # como lista de referencias, sin precios: el precio lo pone el sistema.
    catalog: list[CatalogEntry] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Un 'agente' es config, no un proceso: enrutar = leer estos registros."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    business_id: UUID
    name: str
    routing_description: str
    instructions_md: str
    allowed_tools: list[str] = Field(default_factory=list)
    memory_scope: MemoryScope = MemoryScope.CUSTOMER
    response_style: str | None = None
    status: str = "active"


# ------------------------------------------------------------------
# Contexto de conversacion
# ------------------------------------------------------------------
class ConversationTurn(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: MessageRole
    content: str
    created_at: datetime


class CustomerMemory(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: UUID
    profile: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None


class ConversationContext(BaseModel):
    """Todo lo que el orquestador necesita para razonar este turno."""

    conversation_id: UUID
    customer_id: UUID
    status: ConversationStatus
    active_agent_id: UUID | None = None
    flow_state: dict[str, Any] = Field(default_factory=dict)  # datos parciales (F5)
    recent_turns: list[ConversationTurn] = Field(default_factory=list)
    customer_memory: CustomerMemory | None = None


# ------------------------------------------------------------------
# I/O de herramientas integradas
# ------------------------------------------------------------------
class KnowledgeHit(BaseModel):
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptureRequestInput(BaseModel):
    request_type: str  # 'agenda' | 'pedido' | ...
    structured_data: dict[str, Any]


# ------------------------------------------------------------------
# Resultado del turno (orquestador)
# ------------------------------------------------------------------
class HandlerResult(BaseModel):
    reply_text: str
    tools_used: list[str] = Field(default_factory=list)
    new_flow_state: dict[str, Any] | None = None
    escalate: bool = False


class TurnUsage(BaseModel):
    """Consumo del turno. cache_read_tokens mide el ahorro de caché (RNF-02)."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_estimate_usd: float | None = None
    latency_ms: int | None = None


# ------------------------------------------------------------------
# Modelos de la API admin
# ------------------------------------------------------------------
class AgentCreate(BaseModel):
    name: str
    routing_description: str
    instructions_md: str
    allowed_tools: list[str] = Field(default_factory=list)
    memory_scope: MemoryScope = MemoryScope.CUSTOMER
    response_style: str | None = None


class KnowledgeUpload(BaseModel):
    title: str
    source: str = "upload"
    raw_content: str


class WhatsAppAck(BaseModel):
    status: str = "received"


__all__ = [
    "AgentConfig",
    "AgentCreate",
    "BusinessConfig",
    "IMAGE_PLACEHOLDER",
    "CaptureRequestInput",
    "ContentType",
    "ConversationContext",
    "ConversationStatus",
    "ConversationTurn",
    "CustomerMemory",
    "EscalationStatus",
    "HandlerResult",
    "InboundMedia",
    "InboundMessage",
    "Intent",
    "KnowledgeHit",
    "KnowledgeUpload",
    "MemoryScope",
    "MessageRole",
    "OutboundMessage",
    "RequestStatus",
    "TurnUsage",
    "WhatsAppAck",
    "text_for_model",
]
