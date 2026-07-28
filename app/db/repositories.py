"""
Repositorios async: unico acceso a datos de la aplicacion (capa 1 del
aislamiento, RNF-04).

Regla de oro: **todo metodo publico recibe `business_id` como primer parametro y
lo aplica en el WHERE**. No hay atajos "por id" sin tenant: aunque un uuid sea
unico, filtrar igual evita que un id filtrado desde otro tenant sirva de oraculo.
RLS (capa 2) es el respaldo, no la defensa principal.

El LLM nunca construye SQL: las herramientas llaman a estos metodos.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Agent,
    Business,
    CapturedRequest,
    Conversation,
    Customer,
    CustomerMemory,
    Escalation,
    InboundEvent,
    KnowledgeDocument,
    Message,
    TurnLog,
)
from app.schemas import (
    AgentConfig,
    BusinessConfig,
    ConversationStatus,
    ConversationTurn,
    MessageRole,
)
from app.schemas import CustomerMemory as CustomerMemorySchema

# Turnos de historial que se cargan por defecto en cada turno del loop.
DEFAULT_RECENT_TURNS = 10


class TenantScopedRepo:
    """Base: guarda la sesion y expone el filtro por tenant."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _scoped(stmt: Select[Any], model: Any, business_id: UUID) -> Select[Any]:
        """Aplica el filtro de tenant. Todo SELECT del repositorio pasa por aqui."""
        return stmt.where(model.business_id == business_id)


# ======================================================================
# Clientes (RF-03)
# ======================================================================
class CustomerRepo(TenantScopedRepo):
    async def get_or_create(
        self,
        business_id: UUID,
        channel: str,
        channel_identifier: str,
        display_name: str | None = None,
    ) -> Customer:
        """
        Identidad por (negocio, canal, identificador) — RF-03.

        Usa INSERT ... ON CONFLICT sobre la constraint unica en vez de
        SELECT-then-INSERT: dos mensajes simultaneos del mismo numero llegarian a
        crear dos clientes con la carrera clasica.
        """
        stmt = (
            pg_insert(Customer)
            .values(
                business_id=business_id,
                channel=channel,
                channel_identifier=channel_identifier,
                display_name=display_name,
                last_seen_at=dt.datetime.now(dt.UTC),
            )
            .on_conflict_do_update(
                index_elements=["business_id", "channel", "channel_identifier"],
                set_={"last_seen_at": dt.datetime.now(dt.UTC)},
            )
            .returning(Customer)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def get(self, business_id: UUID, customer_id: UUID) -> Customer | None:
        stmt = self._scoped(select(Customer), Customer, business_id).where(
            Customer.id == customer_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_identifier(
        self, business_id: UUID, channel: str, channel_identifier: str
    ) -> Customer | None:
        stmt = self._scoped(select(Customer), Customer, business_id).where(
            Customer.channel == channel,
            Customer.channel_identifier == channel_identifier,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


# ======================================================================
# Conversaciones (estado de sesion)
# ======================================================================
class ConversationRepo(TenantScopedRepo):
    async def get_or_create_active(
        self, business_id: UUID, customer_id: UUID, channel: str = "whatsapp"
    ) -> Conversation:
        """
        Conversacion activa del cliente; la crea si no hay.

        "Activa" = la mas reciente. El MVP no cierra conversaciones por tiempo:
        cuando exista esa politica, el filtro va aqui.
        """
        stmt = (
            self._scoped(select(Conversation), Conversation, business_id)
            .where(Conversation.customer_id == customer_id, Conversation.channel == channel)
            .order_by(
                Conversation.last_message_at.desc().nullslast(),
                Conversation.created_at.desc(),
            )
            .limit(1)
        )
        existing: Conversation | None = (
            await self.session.execute(stmt)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        conversation = Conversation(
            business_id=business_id, customer_id=customer_id, channel=channel
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def get(self, business_id: UUID, conversation_id: UUID) -> Conversation | None:
        stmt = self._scoped(select(Conversation), Conversation, business_id).where(
            Conversation.id == conversation_id
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_status(
        self, business_id: UUID, conversation_id: UUID, status: ConversationStatus
    ) -> None:
        """F6: status='human' hace que el agente deje de responder."""
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.business_id == business_id)
            .values(status=status.value)
        )

    async def update_flow_state(
        self, business_id: UUID, conversation_id: UUID, flow_state: dict[str, Any]
    ) -> None:
        """F4/F5: datos parciales del flujo en curso."""
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.business_id == business_id)
            .values(flow_state=flow_state)
        )

    async def set_active_agent(
        self, business_id: UUID, conversation_id: UUID, agent_id: UUID | None
    ) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.business_id == business_id)
            .values(active_agent_id=agent_id)
        )

    async def touch_last_message(
        self, business_id: UUID, conversation_id: UUID, when: dt.datetime | None = None
    ) -> None:
        await self.session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.business_id == business_id)
            .values(last_message_at=when or dt.datetime.now(dt.UTC))
        )


# ======================================================================
# Mensajes (historial; NUNCA alimenta el RAG)
# ======================================================================
class MessageRepo(TenantScopedRepo):
    async def append(
        self,
        business_id: UUID,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
    ) -> Message:
        """RF-16. Persiste un turno del historial."""
        message = Message(
            business_id=business_id,
            conversation_id=conversation_id,
            role=role.value,
            content=content,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def recent(
        self,
        business_id: UUID,
        conversation_id: UUID,
        limit: int = DEFAULT_RECENT_TURNS,
    ) -> list[ConversationTurn]:
        """
        Ultimos N turnos en orden cronologico (el mas antiguo primero), que es
        como los espera la API de mensajes.

        Se ordena por `seq` (orden de insercion), NO por `created_at`: now() es
        constante dentro de una transaccion, asi que los mensajes de un mismo
        turno comparten timestamp y el orden quedaria indefinido.

        Se ordena DESC para que LIMIT tome los ultimos y se invierte en memoria.
        """
        stmt = (
            self._scoped(select(Message), Message, business_id)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            ConversationTurn(
                role=MessageRole(m.role), content=m.content, created_at=m.created_at
            )
            for m in reversed(rows)
        ]


# ======================================================================
# Memoria de cliente (RF-17: actualizacion deliberada, no volcado)
# ======================================================================
class MemoryRepo(TenantScopedRepo):
    async def get(self, business_id: UUID, customer_id: UUID) -> CustomerMemorySchema | None:
        stmt = self._scoped(select(CustomerMemory), CustomerMemory, business_id).where(
            CustomerMemory.customer_id == customer_id
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return CustomerMemorySchema.model_validate(row)

    async def upsert_deliberate(
        self,
        business_id: UUID,
        customer_id: UUID,
        profile: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> CustomerMemorySchema:
        """
        RF-17. Escritura DELIBERADA: el llamador decide que se guarda.

        Nunca volcar `messages` aqui. Los campos en None no se tocan, para poder
        actualizar solo el resumen o solo el perfil sin pisar el otro.
        """
        values: dict[str, Any] = {"business_id": business_id, "customer_id": customer_id}
        updates: dict[str, Any] = {}
        if profile is not None:
            values["profile"] = profile
            updates["profile"] = profile
        if summary is not None:
            values["summary"] = summary
            updates["summary"] = summary

        if not updates:
            # Nada que actualizar: garantiza que la fila exista y la devuelve.
            existing = await self.get(business_id, customer_id)
            if existing is not None:
                return existing
            updates = {"business_id": business_id}

        stmt = (
            pg_insert(CustomerMemory)
            .values(**values)
            .on_conflict_do_update(index_elements=["customer_id"], set_=updates)
            .returning(CustomerMemory)
        )
        row = (await self.session.execute(stmt)).scalar_one()
        return CustomerMemorySchema.model_validate(row)


# ======================================================================
# Agentes (configuracion enrutable)
# ======================================================================
class AgentRepo(TenantScopedRepo):
    async def list_active(self, business_id: UUID) -> list[AgentConfig]:
        """
        RF-06: el orquestador enruta leyendo esto en runtime.

        Orden estable por nombre: el prompt de clasificacion se arma con esta
        lista y un orden variable romperia la cache del prefijo (RNF-02).
        """
        stmt = (
            self._scoped(select(Agent), Agent, business_id)
            .where(Agent.status == "active")
            .order_by(Agent.name, Agent.id)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [AgentConfig.model_validate(a) for a in rows]

    async def get(self, business_id: UUID, agent_id: UUID) -> AgentConfig | None:
        stmt = self._scoped(select(Agent), Agent, business_id).where(Agent.id == agent_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return AgentConfig.model_validate(row) if row is not None else None

    async def create(
        self,
        business_id: UUID,
        name: str,
        routing_description: str,
        instructions_md: str,
        allowed_tools: list[str] | None = None,
        memory_scope: str = "customer",
        response_style: str | None = None,
    ) -> AgentConfig:
        """Agregar un agente = INSERT. Nunca editar codigo para enrutar."""
        agent = Agent(
            business_id=business_id,
            name=name,
            routing_description=routing_description,
            instructions_md=instructions_md,
            allowed_tools=allowed_tools or [],
            memory_scope=memory_scope,
            response_style=response_style,
        )
        self.session.add(agent)
        await self.session.flush()
        return AgentConfig.model_validate(agent)


# ======================================================================
# Negocio (config + conocimiento "base completa")
# ======================================================================
class BusinessRepo(TenantScopedRepo):
    async def get_config(self, business_id: UUID) -> BusinessConfig | None:
        """
        Config del tenant + `knowledge_context` ensamblado.

        El conocimiento va COMPLETO en el prefijo cacheado del prompt (decision
        5 de CLAUDE.md), asi que este string DEBE ser deterministico: mismo
        contenido => mismo texto, o se invalida la cache en cada turno (RNF-02).
        Por eso el orden es explicito (created_at, id) y no hay timestamps ni
        nada dinamico en la salida.
        """
        stmt = select(Business).where(Business.id == business_id)
        business = (await self.session.execute(stmt)).scalar_one_or_none()
        if business is None:
            return None

        config = BusinessConfig.model_validate(business)
        config.knowledge_context = await self.build_knowledge_context(business_id)
        return config

    async def build_knowledge_context(self, business_id: UUID) -> str | None:
        """
        Concatena los documentos del negocio en un solo bloque estable.

        Devuelve None si no hay conocimiento, para no meter una seccion vacia en
        el prefijo del prompt.
        """
        stmt = (
            self._scoped(select(KnowledgeDocument), KnowledgeDocument, business_id)
            .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)
        )
        docs = (await self.session.execute(stmt)).scalars().all()

        bloques = [
            f"## {doc.title}\n\n{doc.raw_content.strip()}"
            for doc in docs
            if doc.raw_content and doc.raw_content.strip()
        ]
        return "\n\n".join(bloques) if bloques else None

    async def get_id_by_whatsapp_number(self, phone_number_id: str) -> UUID | None:
        """
        Resuelve el tenant desde el numero que recibio el mensaje (fase 3).

        Es LA excepcion a la regla de "todo metodo recibe business_id": este es
        el paso que lo averigua, antes de que exista contexto de tenant.

        Consulta la vista `whatsapp_number_map` (migracion 0008), no la tabla:
        `businesses` esta protegida por RLS y sin tenant fijado devolveria cero
        filas siempre. La vista expone unicamente el par
        (business_id, numero) de los negocios activos, que es lo justo que hace
        falta para enrutar el webhook.
        """
        resultado = await self.session.execute(
            text(
                "SELECT business_id FROM whatsapp_number_map "
                "WHERE whatsapp_phone_number_id = :pid"
            ),
            {"pid": phone_number_id},
        )
        fila = resultado.scalar_one_or_none()
        return UUID(str(fila)) if fila is not None else None

    async def get_notify_config(self, business_id: UUID) -> dict[str, Any]:
        """R para la herramienta notify_owner (RF-11)."""
        stmt = select(Business.notify_channel, Business.notify_config).where(
            Business.id == business_id
        )
        row = (await self.session.execute(stmt)).one_or_none()
        if row is None:
            return {}
        return {"channel": row.notify_channel, "config": row.notify_config}


# ======================================================================
# Solicitudes capturadas (F2 / ACT)
# ======================================================================
class RequestRepo(TenantScopedRepo):
    async def create(
        self,
        business_id: UUID,
        customer_id: UUID,
        conversation_id: UUID | None,
        request_type: str,
        structured_data: dict[str, Any],
    ) -> CapturedRequest:
        """RF-10. W: captured_requests."""
        request = CapturedRequest(
            business_id=business_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            request_type=request_type,
            structured_data=structured_data,
        )
        self.session.add(request)
        await self.session.flush()
        return request

    async def list_by_status(
        self, business_id: UUID, status: str | None = None, limit: int = 50
    ) -> list[CapturedRequest]:
        """RF-21: bandeja del panel. Sin status, devuelve todas."""
        stmt = self._scoped(select(CapturedRequest), CapturedRequest, business_id)
        if status is not None:
            stmt = stmt.where(CapturedRequest.status == status)
        stmt = stmt.order_by(CapturedRequest.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_status(self, business_id: UUID, request_id: UUID, status: str) -> None:
        await self.session.execute(
            update(CapturedRequest)
            .where(
                CapturedRequest.id == request_id,
                CapturedRequest.business_id == business_id,
            )
            .values(status=status)
        )


# ======================================================================
# Escalamientos (F6)
# ======================================================================
class EscalationRepo(TenantScopedRepo):
    async def create(
        self,
        business_id: UUID,
        conversation_id: UUID,
        customer_id: UUID,
        reason: str | None = None,
    ) -> Escalation:
        escalation = Escalation(
            business_id=business_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            reason=reason,
        )
        self.session.add(escalation)
        await self.session.flush()
        return escalation

    async def resolve(
        self, business_id: UUID, escalation_id: UUID, resolved_by: UUID | None = None
    ) -> None:
        await self.session.execute(
            update(Escalation)
            .where(Escalation.id == escalation_id, Escalation.business_id == business_id)
            .values(
                status="resolved",
                resolved_at=dt.datetime.now(dt.UTC),
                resolved_by=resolved_by,
            )
        )

    async def list_open(self, business_id: UUID, limit: int = 50) -> list[Escalation]:
        stmt = (
            self._scoped(select(Escalation), Escalation, business_id)
            .where(Escalation.status == "open")
            .order_by(Escalation.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


# ======================================================================
# Log de turnos (RF-15, RNF-02)
# ======================================================================
class TurnLogRepo(TenantScopedRepo):
    async def append(
        self,
        business_id: UUID,
        conversation_id: UUID,
        message_in_id: UUID | None = None,
        message_out_id: UUID | None = None,
        detected_intent: str | None = None,
        routed_agent_id: UUID | None = None,
        tools_used: list[Any] | None = None,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cost_estimate_usd: float | None = None,
        latency_ms: int | None = None,
    ) -> TurnLog:
        """
        RF-15. Se registra SIEMPRE, incluso en turnos fallidos: es la base de la
        metrica de costo por conversacion y del ahorro de cache (RNF-02).
        """
        log = TurnLog(
            business_id=business_id,
            conversation_id=conversation_id,
            message_in_id=message_in_id,
            message_out_id=message_out_id,
            detected_intent=detected_intent,
            routed_agent_id=routed_agent_id,
            tools_used=tools_used or [],
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_estimate_usd=cost_estimate_usd,
            latency_ms=latency_ms,
        )
        self.session.add(log)
        await self.session.flush()
        return log


# ======================================================================
# Eventos entrantes (deduplicacion del webhook, fase 3)
# ======================================================================
class InboundEventRepo(TenantScopedRepo):
    async def claim(
        self, business_id: UUID, external_message_id: str, channel: str = "whatsapp"
    ) -> bool:
        """
        Intenta reservar un evento para procesarlo. Devuelve True si es nuevo.

        Es una operacion atomica: el INSERT con ON CONFLICT DO NOTHING deja que
        Postgres arbitre. Dos entregas simultaneas del mismo mensaje compiten por
        la misma fila y solo una gana, sin necesidad de bloqueos ni de un
        SELECT-then-INSERT (que tendria la carrera clasica).
        """
        stmt = (
            pg_insert(InboundEvent)
            .values(
                business_id=business_id,
                channel=channel,
                external_message_id=external_message_id,
            )
            .on_conflict_do_nothing(index_elements=["channel", "external_message_id"])
            .returning(InboundEvent.id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def mark_done(
        self, business_id: UUID, external_message_id: str, channel: str = "whatsapp"
    ) -> None:
        await self.session.execute(
            update(InboundEvent)
            .where(
                InboundEvent.business_id == business_id,
                InboundEvent.channel == channel,
                InboundEvent.external_message_id == external_message_id,
            )
            .values(status="done", processed_at=dt.datetime.now(dt.UTC))
        )

    async def mark_failed(
        self,
        business_id: UUID,
        external_message_id: str,
        error: str,
        channel: str = "whatsapp",
    ) -> None:
        """
        Marca el evento como fallido. Se conserva la fila (no se borra) para que
        una reentrega de WhatsApp no dispare otro intento sobre algo que ya
        fallo, y para poder diagnosticar despues.
        """
        await self.session.execute(
            update(InboundEvent)
            .where(
                InboundEvent.business_id == business_id,
                InboundEvent.channel == channel,
                InboundEvent.external_message_id == external_message_id,
            )
            .values(status="failed", processed_at=dt.datetime.now(dt.UTC), error=error[:2000])
        )


__all__ = [
    "DEFAULT_RECENT_TURNS",
    "AgentRepo",
    "BusinessRepo",
    "ConversationRepo",
    "CustomerRepo",
    "EscalationRepo",
    "InboundEventRepo",
    "MemoryRepo",
    "MessageRepo",
    "RequestRepo",
    "TurnLogRepo",
]
