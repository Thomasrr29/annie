"""
Modelos SQLAlchemy: espejo del DDL de docs/MVP_Estructura_Base_de_Datos.md §2.

La fuente de verdad del esquema son las migraciones de `migrations/versions/`.
Estos modelos deben reflejarlas, no al reves: el test
`tests/test_modelos_vs_ddl.py` verifica que no se separen.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base declarativa comun."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _business_fk() -> Mapped[uuid.UUID]:
    """FK al tenant. Presente en toda tabla salvo `businesses` (RF-24)."""
    return mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("businesses.id", ondelete="CASCADE"),
        nullable=False,
    )


# El DDL usa timestamptz en todas las fechas. Sin timezone=True, SQLAlchemy
# genera TIMESTAMP WITHOUT TIME ZONE y asyncpg rechaza los datetime aware que
# usa la app (dt.datetime.now(dt.UTC)).
TZDateTime = DateTime(timezone=True)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(TZDateTime, server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[dt.datetime]:
    """La actualiza el trigger set_updated_at() de la migracion 0001."""
    return mapped_column(TZDateTime, server_default=func.now(), nullable=False)


def _optional_timestamp() -> Mapped[dt.datetime | None]:
    return mapped_column(TZDateTime)


# ============================================================
# 1. NEGOCIOS (tenant)
# ============================================================
class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    persona_tone: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="es")
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="America/Bogota"
    )
    business_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    notify_channel: Mapped[str | None] = mapped_column(Text)
    notify_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    # Unico dato del webhook de Meta que identifica al tenant (migracion 0006).
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(Text, unique=True)
    # Circuit breaker de costo (migracion 0010). None = limite por defecto.
    daily_turn_limit: Mapped[int | None] = mapped_column()
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 2. USUARIOS ADMIN
# ============================================================
class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (UniqueConstraint("business_id", "email"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="operator")
    created_at: Mapped[dt.datetime] = _created_at()


# ============================================================
# 3. AGENTES (configuracion enrutable)
# ============================================================
class Agent(Base):
    __tablename__ = "agents"
    __table_args__ = (
        Index(
            "idx_agents_business_active",
            "business_id",
            postgresql_where=("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    routing_description: Mapped[str] = mapped_column(Text, nullable=False)
    instructions_md: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_tools: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    memory_scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="customer")
    response_style: Mapped[str | None] = mapped_column(Text)
    handler_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="llm")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 4. DOCUMENTOS DE CONOCIMIENTO
# ============================================================
class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    raw_content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 5. CHUNKS DE CONOCIMIENTO (RAG diferido en el MVP)
# ============================================================
class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("idx_chunks_business", "business_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # La dimension se ajusta cuando se elija modelo de embeddings (post-MVP).
    embedding: Mapped[Any | None] = mapped_column(Vector(1024))
    doc_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[dt.datetime] = _created_at()


# ============================================================
# 5b. CATÁLOGO (migración 0012): lo que vende el negocio y su precio
# ============================================================
class CatalogItem(Base):
    """El modelo nunca escribe estos precios: los referencia y el sistema los pone."""

    __tablename__ = "catalog_items"
    __table_args__ = (UniqueConstraint("business_id", "name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="COP")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 6. CLIENTES (identidad por canal, RF-03)
# ============================================================
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "channel", "channel_identifier"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="whatsapp")
    channel_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()
    last_seen_at: Mapped[dt.datetime | None] = _optional_timestamp()
    # Aviso de tratamiento de datos: se manda una sola vez (RNF-05).
    privacy_notice_sent_at: Mapped[dt.datetime | None] = _optional_timestamp()


# ============================================================
# 7. MEMORIA DE CLIENTE (1:1, estructurada, NO vectorial)
# ============================================================
class CustomerMemory(Base):
    __tablename__ = "customer_memory"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    business_id: Mapped[uuid.UUID] = _business_fk()
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 8. CONVERSACIONES (estado de sesion, sin Redis)
# ============================================================
class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("idx_conversations_customer", "customer_id", "last_message_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="whatsapp")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="auto")
    active_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id")
    )
    flow_state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[dt.datetime] = _created_at()
    last_message_at: Mapped[dt.datetime | None] = _optional_timestamp()
    updated_at: Mapped[dt.datetime] = _updated_at()
    # Hasta donde leyo el negocio en la bandeja (migracion 0011).
    owner_last_read_at: Mapped[dt.datetime | None] = _optional_timestamp()
    # Migración 0013: seq del último mensaje del cliente que ya entró a un turno.
    last_processed_seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )


# ============================================================
# 9. MENSAJES (historial; NUNCA alimenta el RAG)
# ============================================================
class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("idx_messages_conversation", "conversation_id", "seq"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    # Orden de insercion real. `created_at` no sirve para ordenar dentro de una
    # conversacion: now() es constante en toda la transaccion, asi que los
    # mensajes de un mismo turno comparten timestamp (migracion 0005).
    seq: Mapped[int] = mapped_column(
        BigInteger, server_default=text("nextval('messages_seq_seq')"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    business_id: Mapped[uuid.UUID] = _business_fk()
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # Para una imagen, `content` es el pie de foto (puede ser vacio) y el archivo
    # vive en `message_media` (migracion 0011).
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="text")
    created_at: Mapped[dt.datetime] = _created_at()


class MessageMedia(Base):
    """Archivo de un mensaje (1:1). Se borra en cascada con el mensaje."""

    __tablename__ = "message_media"

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Id de Meta: solo diagnostico, expira a los pocos dias.
    wa_media_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()


# ============================================================
# 10. LOG DE TURNOS (costo/observabilidad, RNF-02)
# ============================================================
class TurnLog(Base):
    __tablename__ = "turn_logs"
    __table_args__ = (Index("idx_turnlogs_business_time", "business_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_in_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id")
    )
    message_out_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("messages.id")
    )
    detected_intent: Mapped[str | None] = mapped_column(Text)
    routed_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("agents.id")
    )
    tools_used: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    model_calls: Mapped[int] = mapped_column(nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    cache_read_tokens: Mapped[int] = mapped_column(nullable=False, server_default="0")
    cost_estimate_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    latency_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[dt.datetime] = _created_at()


# ============================================================
# 11. SOLICITUDES CAPTURADAS (F2 / ACT)
# ============================================================
class CapturedRequest(Base):
    __tablename__ = "captured_requests"
    __table_args__ = (
        Index("idx_requests_business_status", "business_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    request_type: Mapped[str] = mapped_column(Text, nullable=False)
    structured_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="new")
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = _updated_at()


# ============================================================
# 12. ESCALAMIENTOS (F6)
# ============================================================
class Escalation(Base):
    __tablename__ = "escalations"
    __table_args__ = (
        Index("idx_escalations_business_status", "business_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("customers.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")
    created_at: Mapped[dt.datetime] = _created_at()
    resolved_at: Mapped[dt.datetime | None] = _optional_timestamp()
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("admin_users.id")
    )


# ============================================================
# 15. EVIDENCIA DE ELIMINACION DE DATOS (Ley 1581)
# ============================================================
class DataDeletionLog(Base):
    """
    Prueba de cumplimiento de una solicitud de borrado.

    Guarda el HASH del identificador, no el telefono: conservar el dato personal
    aqui vaciaria de sentido el borrado.
    """

    __tablename__ = "data_deletion_log"
    __table_args__ = (Index("idx_deletion_log_business", "business_id", "deleted_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    identifier_hash: Mapped[str] = mapped_column(Text, nullable=False)
    messages_deleted: Mapped[int] = mapped_column(nullable=False, server_default="0")
    requests_deleted: Mapped[int] = mapped_column(nullable=False, server_default="0")
    requested_by: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )


# ============================================================
# 14. TOKENS DEL PANEL (autenticacion admin)
# ============================================================
class AdminToken(Base):
    """
    Token de acceso al panel. Se guarda el hash, nunca el token en claro.
    """

    __tablename__ = "admin_tokens"
    __table_args__ = (
        Index("idx_admin_tokens_hash", "token_hash"),
        Index("idx_admin_tokens_business", "business_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("admin_users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()
    last_used_at: Mapped[dt.datetime | None] = _optional_timestamp()
    revoked_at: Mapped[dt.datetime | None] = _optional_timestamp()


# ============================================================
# 13. EVENTOS ENTRANTES (deduplicacion del webhook)
# ============================================================
class InboundEvent(Base):
    """
    Un evento entrante ya visto. La constraint unica sobre
    (channel, external_message_id) es lo que corta las reentregas de WhatsApp.
    """

    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("channel", "external_message_id"),
        Index("idx_inbound_events_business_time", "business_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    business_id: Mapped[uuid.UUID] = _business_fk()
    channel: Mapped[str] = mapped_column(Text, nullable=False, server_default="whatsapp")
    external_message_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="processing")
    received_at: Mapped[dt.datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    processed_at: Mapped[dt.datetime | None] = _optional_timestamp()
    error: Mapped[str | None] = mapped_column(Text)


__all__ = [
    "AdminToken",
    "AdminUser",
    "Agent",
    "Base",
    "Business",
    "CapturedRequest",
    "CatalogItem",
    "Conversation",
    "Customer",
    "CustomerMemory",
    "DataDeletionLog",
    "Escalation",
    "InboundEvent",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "Message",
    "MessageMedia",
    "TurnLog",
]
