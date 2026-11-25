"""
Superficie ADMIN: la API que consume el panel (RF-18..RF-23).

Autenticacion: `Authorization: Bearer <token>`. El token resuelve el tenant, y
TODA consulta posterior corre dentro de `tenant_session(business_id)`, o sea con
RLS fijado. Un admin del negocio A no puede alcanzar datos del negocio B ni
conociendo los uuid: los repositorios filtran (capa 1) y RLS respalda (capa 2).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import logging
import secrets
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_whatsapp import (
    InvalidMedia,
    WhatsAppError,
    check_image,
    send_whatsapp_image,
    send_whatsapp_reply,
)
from app.core.limits import eliminar_datos_cliente
from app.core.tools import TOOL_SCHEMAS
from app.db.models import Agent, Business, KnowledgeDocument, TurnLog
from app.db.repositories import (
    AgentRepo,
    BusinessRepo,
    CatalogRepo,
    ConversationRepo,
    CustomerRepo,
    EscalationRepo,
    MediaRepo,
    MessageRepo,
    RequestRepo,
)
from app.db.session import system_session, tenant_session
from app.schemas import (
    AgentConfig,
    AgentCreate,
    ContentType,
    ConversationStatus,
    MessageRole,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

# Catalogo real de herramientas: `allowed_tools` se valida contra esto para que
# no se guarde una herramienta inexistente que falle recien en runtime.
HERRAMIENTAS_VALIDAS = set(TOOL_SCHEMAS) | {"search_knowledge"}


# ======================================================================
# Autenticacion
# ======================================================================
def hash_token(token: str) -> str:
    """SHA-256 del token. Nunca se guarda el token en claro."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Genera (token, hash, prefijo) para un token nuevo."""
    token = f"asa_{secrets.token_urlsafe(32)}"
    return token, hash_token(token), token[:12]


async def current_business(
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """
    Resuelve el negocio autenticado desde el token del panel.

    El lookup usa `system_session()` porque todavia no se sabe el tenant: es
    justamente lo que se esta averiguando. A partir de aca, todo va con
    `tenant_session()`.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta el token de acceso",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_hash = hash_token(authorization.removeprefix("Bearer ").strip())

    async with system_session() as session:
        # Se consulta la vista `admin_token_lookup` (migracion 0009), no la
        # tabla: `admin_tokens` esta protegida por RLS y sin tenant fijado no
        # devolveria ninguna fila, con lo cual TODOS los tokens serian invalidos.
        # La vista expone unicamente el mapeo token -> negocio, y ya filtra los
        # tokens revocados y los negocios inactivos.
        row = (
            await session.execute(
                text(
                    "SELECT business_id, token_id FROM admin_token_lookup "
                    "WHERE token_hash = :h"
                ),
                {"h": token_hash},
            )
        ).one_or_none()

        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido"
            )

        await session.execute(
            text("SELECT touch_admin_token(:t)"), {"t": row.token_id}
        )

    return UUID(str(row.business_id))


BusinessId = Annotated[UUID, Depends(current_business)]


# ======================================================================
# Modelos de request/response
# ======================================================================
class BusinessUpdate(BaseModel):
    name: str | None = None
    persona_tone: str | None = None
    language: str | None = None
    timezone: str | None = None
    business_hours: dict[str, Any] | None = None
    notify_channel: str | None = None
    notify_config: dict[str, Any] | None = None
    whatsapp_phone_number_id: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    routing_description: str | None = None
    instructions_md: str | None = None
    allowed_tools: list[str] | None = None
    memory_scope: str | None = None
    response_style: str | None = None
    status: str | None = None


class KnowledgeCreate(BaseModel):
    title: str
    raw_content: str
    source: str = "upload"
    # Regla de curacion (CLAUDE.md §5): nada entra al conocimiento que no sea
    # aceptable que un cliente vea. El agente lo lee COMPLETO del prefijo, asi
    # que un dato interno aca es un dato interno filtrado.
    confirmed_public: bool = Field(
        ...,
        description=(
            "Confirmación explícita de que este contenido puede verlo cualquier "
            "cliente. El agente puede citarlo textualmente."
        ),
    )


class RequestStatusUpdate(BaseModel):
    status: str


class HumanReply(BaseModel):
    text: str
    return_to_auto: bool = False


class ImageReply(BaseModel):
    image_base64: str
    mime_type: str
    caption: str = ""
    return_to_auto: bool = False


class ConversationStatusChange(BaseModel):
    status: ConversationStatus


class SandboxMessage(BaseModel):
    text: str
    customer_identifier: str = "sandbox-tester"


class SandboxResponse(BaseModel):
    """
    Respuesta del sandbox, con detalle para depurar (HU-ADM-05).

    `replied=False` significa que el agente NO contestó (por ejemplo, la
    conversación está en modo humano): `reply_text` queda vacío y
    `no_reply_reason` explica por qué. La traza (intención, tokens, costo) es la
    de ESTE mensaje; si no se registró un turno, queda vacía en vez de mostrar
    la del turno anterior.
    """

    reply_text: str
    replied: bool = True
    outcome: str = "respondido"
    no_reply_reason: str | None = None
    detected_intent: str | None = None
    routed_agent: str | None = None
    tools_used: list[str] = Field(default_factory=list)
    conversation_id: UUID
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_estimate_usd: float | None = None
    latency_ms: int | None = None


# ======================================================================
# Negocio (RF-18)
# ======================================================================
@router.get("/business")
async def get_business(business_id: BusinessId) -> dict[str, Any]:
    async with tenant_session(business_id) as session:
        config = await BusinessRepo(session).get_config(business_id)
        if config is None:
            raise HTTPException(status_code=404, detail="Negocio no encontrado")
        return config.model_dump(mode="json")


@router.patch("/business")
async def update_business(body: BusinessUpdate, business_id: BusinessId) -> dict[str, Any]:
    cambios = body.model_dump(exclude_none=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="Nada que actualizar")

    async with tenant_session(business_id) as session:
        await session.execute(
            update(Business).where(Business.id == business_id).values(**cambios)
        )
        config = await BusinessRepo(session).get_config(business_id)

    if config is None:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return config.model_dump(mode="json")


# ======================================================================
# Agentes (RF-20, RF-06)
# ======================================================================
def _validar_herramientas(allowed_tools: list[str]) -> None:
    desconocidas = set(allowed_tools) - HERRAMIENTAS_VALIDAS
    if desconocidas:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Herramientas inexistentes: {sorted(desconocidas)}. "
                f"Disponibles: {sorted(HERRAMIENTAS_VALIDAS)}"
            ),
        )


@router.get("/tools")
async def list_tools(business_id: BusinessId) -> list[str]:
    """
    Catalogo de herramientas validas para `allowed_tools`.

    Existe para que el panel ofrezca exactamente lo que `_validar_herramientas`
    acepta: si el panel tuviera su propia lista, las dos se separarian y el
    dueno guardaria una herramienta que la API rechaza (o peor, dejaria de ver
    una nueva).
    """
    return sorted(HERRAMIENTAS_VALIDAS)


@router.get("/agents")
async def list_agents(
    business_id: BusinessId, include_inactive: bool = False
) -> list[dict[str, Any]]:
    async with tenant_session(business_id) as session:
        if include_inactive:
            rows = (
                await session.execute(
                    select(Agent).where(Agent.business_id == business_id).order_by(Agent.name)
                )
            ).scalars().all()
            agentes = [AgentConfig.model_validate(a) for a in rows]
        else:
            agentes = await AgentRepo(session).list_active(business_id)
    return [a.model_dump(mode="json") for a in agentes]


@router.post("/agents", status_code=201)
async def create_agent(body: AgentCreate, business_id: BusinessId) -> dict[str, Any]:
    """
    Alta de agente = INSERT (RF-06). El orquestador lo enruta en el siguiente
    mensaje, sin redeploy ni cambios de codigo.
    """
    _validar_herramientas(body.allowed_tools)

    async with tenant_session(business_id) as session:
        agente = await AgentRepo(session).create(
            business_id,
            name=body.name,
            routing_description=body.routing_description,
            instructions_md=body.instructions_md,
            allowed_tools=body.allowed_tools,
            memory_scope=body.memory_scope.value,
            response_style=body.response_style,
        )
    return agente.model_dump(mode="json")


@router.patch("/agents/{agent_id}")
async def update_agent(
    agent_id: UUID, body: AgentUpdate, business_id: BusinessId
) -> dict[str, Any]:
    cambios = body.model_dump(exclude_none=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    if "allowed_tools" in cambios:
        _validar_herramientas(cambios["allowed_tools"])

    async with tenant_session(business_id) as session:
        # El filtro por business_id es lo que impide editar agentes de otro tenant.
        resultado = await session.execute(
            update(Agent)
            .where(Agent.id == agent_id, Agent.business_id == business_id)
            .values(**cambios)
            .returning(Agent)
        )
        fila = resultado.scalar_one_or_none()
        if fila is None:
            raise HTTPException(status_code=404, detail="Agente no encontrado")
        agente = AgentConfig.model_validate(fila)

    return agente.model_dump(mode="json")


# ======================================================================
# Catálogo: lo que vende el negocio (migración 0012)
# ======================================================================
class CatalogItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    price: int = Field(ge=0, description="Precio en pesos, sin decimales")
    currency: str = "COP"


class CatalogItemUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    price: int | None = Field(default=None, ge=0)
    currency: str | None = None
    status: Literal["active", "inactive"] | None = None


def _item_json(item: Any) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "name": item.name,
        "price": item.price,
        "currency": item.currency,
        "status": item.status,
    }


@router.get("/catalog")
async def list_catalog(business_id: BusinessId) -> list[dict[str, Any]]:
    """Todo el catálogo, activo e inactivo. El agente solo ve los activos."""
    async with tenant_session(business_id) as session:
        items = await CatalogRepo(session).list_all(business_id)
        return [_item_json(i) for i in items]


@router.post("/catalog", status_code=201)
async def create_catalog_item(body: CatalogItemCreate, business_id: BusinessId) -> dict[str, Any]:
    """Alta de un producto o servicio. Desde el siguiente mensaje el agente lo puede citar."""
    try:
        async with tenant_session(business_id) as session:
            item = await CatalogRepo(session).create(
                business_id, body.name.strip(), body.price, body.currency
            )
            return _item_json(item)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Ya existe un ítem con ese nombre") from exc


@router.patch("/catalog/{item_id}")
async def update_catalog_item(
    item_id: UUID, body: CatalogItemUpdate, business_id: BusinessId
) -> dict[str, Any]:
    cambios = body.model_dump(exclude_none=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="Nada que actualizar")
    try:
        async with tenant_session(business_id) as session:
            item = await CatalogRepo(session).update(business_id, item_id, cambios)
            if item is None:
                raise HTTPException(status_code=404, detail="Ítem no encontrado")
            return _item_json(item)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Ya existe un ítem con ese nombre") from exc


@router.delete("/catalog/{item_id}", status_code=204)
async def delete_catalog_item(item_id: UUID, business_id: BusinessId) -> Response:
    async with tenant_session(business_id) as session:
        borrado = await CatalogRepo(session).delete(business_id, item_id)
    if not borrado:
        raise HTTPException(status_code=404, detail="Ítem no encontrado")
    return Response(status_code=204)


# ======================================================================
# Conocimiento (RF-19)
# ======================================================================
@router.get("/knowledge")
async def list_knowledge(business_id: BusinessId) -> list[dict[str, Any]]:
    async with tenant_session(business_id) as session:
        rows = (
            await session.execute(
                select(KnowledgeDocument)
                .where(KnowledgeDocument.business_id == business_id)
                .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)
            )
        ).scalars().all()

    return [
        {
            "id": str(d.id),
            "title": d.title,
            "source": d.source,
            "raw_content": d.raw_content,
            "status": d.status,
            "created_at": d.created_at.isoformat(),
        }
        for d in rows
    ]


@router.post("/knowledge", status_code=201)
async def create_knowledge(body: KnowledgeCreate, business_id: BusinessId) -> dict[str, Any]:
    """
    Carga conocimiento. Exige `confirmed_public=true`: el agente lo lee completo
    del prefijo del prompt y puede citarlo textualmente al cliente.
    """
    if not body.confirmed_public:
        raise HTTPException(
            status_code=422,
            detail=(
                "Confirmá que este contenido puede verlo cualquier cliente "
                "(confirmed_public=true). El agente puede citarlo textualmente."
            ),
        )

    async with tenant_session(business_id) as session:
        doc = KnowledgeDocument(
            business_id=business_id,
            title=body.title,
            source=body.source,
            raw_content=body.raw_content,
            status="indexed",  # "base completa": no hay pipeline de embeddings
        )
        session.add(doc)
        await session.flush()
        resultado = {"id": str(doc.id), "title": doc.title, "status": doc.status}

    return resultado


@router.delete("/knowledge/{document_id}", status_code=204)
async def delete_knowledge(document_id: UUID, business_id: BusinessId) -> None:
    async with tenant_session(business_id) as session:
        borrado = (
            await session.execute(
                text(
                    "DELETE FROM knowledge_documents "
                    "WHERE id = :id AND business_id = :bid RETURNING id"
                ),
                {"id": document_id, "bid": business_id},
            )
        ).scalar_one_or_none()
        if borrado is None:
            raise HTTPException(status_code=404, detail="Documento no encontrado")


# ======================================================================
# Bandejas (RF-21)
# ======================================================================
@router.get("/requests")
async def list_requests(
    business_id: BusinessId,
    request_status: Annotated[str | None, Query(alias="status")] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    async with tenant_session(business_id) as session:
        solicitudes = await RequestRepo(session).list_by_status(
            business_id, status=request_status, limit=limit
        )
        return [
            {
                "id": str(r.id),
                "customer_id": str(r.customer_id),
                "conversation_id": str(r.conversation_id) if r.conversation_id else None,
                "request_type": r.request_type,
                "structured_data": r.structured_data,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in solicitudes
        ]


@router.patch("/requests/{request_id}")
async def update_request_status(
    request_id: UUID, body: RequestStatusUpdate, business_id: BusinessId
) -> dict[str, str]:
    validos = {"new", "seen", "in_progress", "done"}
    if body.status not in validos:
        raise HTTPException(status_code=422, detail=f"Estado inválido. Válidos: {sorted(validos)}")

    async with tenant_session(business_id) as session:
        await RequestRepo(session).set_status(business_id, request_id, body.status)

    return {"id": str(request_id), "status": body.status}


@router.get("/escalations")
async def list_escalations(business_id: BusinessId, limit: int = 50) -> list[dict[str, Any]]:
    async with tenant_session(business_id) as session:
        escalamientos = await EscalationRepo(session).list_open(business_id, limit=limit)
        return [
            {
                "id": str(e.id),
                "conversation_id": str(e.conversation_id),
                "customer_id": str(e.customer_id),
                "reason": e.reason,
                "status": e.status,
                "created_at": e.created_at.isoformat(),
            }
            for e in escalamientos
        ]


@router.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(
    escalation_id: UUID, business_id: BusinessId, return_to_auto: bool = False
) -> dict[str, Any]:
    """
    Cierra el escalamiento. Con `return_to_auto`, la conversacion vuelve a modo
    automatico (RF-14).
    """
    async with tenant_session(business_id) as session:
        repo = EscalationRepo(session)
        abiertos = await repo.list_open(business_id, limit=200)
        objetivo = next((e for e in abiertos if e.id == escalation_id), None)
        if objetivo is None:
            raise HTTPException(status_code=404, detail="Escalamiento no encontrado")

        await repo.resolve(business_id, escalation_id)

        if return_to_auto:
            await ConversationRepo(session).set_status(
                business_id, objetivo.conversation_id, ConversationStatus.AUTO
            )

    return {"id": str(escalation_id), "status": "resolved", "returned_to_auto": return_to_auto}


# ======================================================================
# Bandeja de conversaciones (RF-14): historial completo y respuesta humana
# ======================================================================
# Ventana de servicio de WhatsApp: pasadas 24h desde el ultimo mensaje del
# cliente, solo se le puede escribir con plantillas pagas (fuera del MVP).
VENTANA_WHATSAPP = dt.timedelta(hours=24)


def _ventana(canal: str, ultimo_del_cliente: dt.datetime | None) -> dict[str, Any]:
    """
    Estado de la ventana de 24h. Solo aplica a WhatsApp: el sandbox no tiene
    restriccion porque no envia nada afuera.
    """
    if canal != "whatsapp":
        return {"applies": False, "open": True, "expires_at": None}
    if ultimo_del_cliente is None:
        return {"applies": True, "open": False, "expires_at": None}
    vence = ultimo_del_cliente + VENTANA_WHATSAPP
    return {
        "applies": True,
        "open": dt.datetime.now(dt.UTC) < vence,
        "expires_at": vence.isoformat(),
    }


def _iso(valor: dt.datetime | None) -> str | None:
    return valor.isoformat() if valor is not None else None


class _Destino(BaseModel):
    """A quien y por donde sale un mensaje del operador."""

    channel: str
    channel_identifier: str
    phone_number_id: str | None


async def _validar_envio(business_id: UUID, conversation_id: UUID) -> _Destino:
    """
    Comprueba que se le pueda escribir al cliente ANTES de enviar nada.

    Se valida la ventana de 24h aca y no se deja fallar a Meta: su error llega
    como un 400 generico, y el operador necesita saber por que no salio.
    """
    async with tenant_session(business_id) as session:
        conv_repo = ConversationRepo(session)
        conv = await conv_repo.get(business_id, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")

        cliente = await CustomerRepo(session).get(business_id, conv.customer_id)
        if cliente is None:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        ultimo = await conv_repo.last_customer_message_at(business_id, conversation_id)
        numero = (
            await session.execute(
                select(Business.whatsapp_phone_number_id).where(Business.id == business_id)
            )
        ).scalar_one_or_none()

    if not _ventana(cliente.channel, ultimo)["open"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "Pasaron más de 24 horas desde el último mensaje del cliente: "
                "WhatsApp no permite escribirle hasta que vuelva a escribir."
            ),
        )

    return _Destino(
        channel=cliente.channel,
        channel_identifier=cliente.channel_identifier,
        # El numero del negocio, no el global de la config: cada tenant responde
        # desde su propio numero.
        phone_number_id=numero,
    )


async def _registrar_envio(
    business_id: UUID,
    conversation_id: UUID,
    texto: str,
    return_to_auto: bool,
    imagen: tuple[bytes, str] | None = None,
) -> None:
    """
    Guarda lo que el operador ya envio y actualiza quien atiende.

    Escribir al cliente desde la bandeja es tomar la conversacion: queda en modo
    humano y el agente deja de responder, salvo que se pida devolverla. Sin esto,
    el agente le contestaria al cliente encima de la persona.
    """
    async with tenant_session(business_id) as session:
        if imagen is not None:
            mensaje = await MessageRepo(session).append(
                business_id,
                conversation_id,
                MessageRole.HUMAN,
                texto,
                content_type=ContentType.IMAGE,
            )
            datos, mime_type = imagen
            await MediaRepo(session).create(business_id, mensaje.id, mime_type, datos)
        else:
            await MessageRepo(session).append(
                business_id, conversation_id, MessageRole.HUMAN, texto
            )

        await _cambiar_estado(
            session,
            business_id,
            conversation_id,
            ConversationStatus.AUTO if return_to_auto else ConversationStatus.HUMAN,
        )


async def _cambiar_estado(
    session: AsyncSession, business_id: UUID, conversation_id: UUID, nuevo: ConversationStatus
) -> None:
    """Devolver al agente tambien cierra el escalamiento abierto: el caso terminó."""
    await ConversationRepo(session).set_status(business_id, conversation_id, nuevo)
    if nuevo == ConversationStatus.AUTO:
        await EscalationRepo(session).resolve_open_for_conversation(
            business_id, conversation_id
        )


@router.get("/conversations")
async def list_conversations(
    business_id: BusinessId,
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    search: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Bandeja: conversaciones por actividad reciente, con no leídos y ventana."""
    async with tenant_session(business_id) as session:
        filas = await ConversationRepo(session).list_inbox(
            business_id, status=status_filter, search=search, limit=limit
        )

    return [
        {
            "id": str(f["id"]),
            "status": f["status"],
            "channel": f["channel"],
            "last_message_at": _iso(f["last_message_at"] or f["created_at"]),
            "customer": {
                "id": str(f["customer_id"]),
                "display_name": f["display_name"],
                "channel_identifier": f["channel_identifier"],
            },
            "last_message": (
                {
                    "role": f["last_role"],
                    "content_type": f["last_content_type"],
                    "content": f["last_content"],
                }
                if f["last_role"] is not None
                else None
            ),
            "unread": int(f["unread"] or 0),
            "open_escalation": (
                {"id": str(f["open_escalation_id"]), "reason": f["open_escalation_reason"]}
                if f["open_escalation_id"] is not None
                else None
            ),
            "window": _ventana(f["channel"], f["last_customer_message_at"]),
        }
        for f in filas
    ]


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: UUID,
    business_id: BusinessId,
    after_seq: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """
    Detalle para la bandeja: cliente, estado, ventana de 24h e historial.

    Con `after_seq` devuelve solo los mensajes nuevos: es lo que pide el panel
    en cada refresco. No marca como leído (GET sin efectos): eso es `/read`.
    """
    async with tenant_session(business_id) as session:
        conv_repo = ConversationRepo(session)
        conv = await conv_repo.get(business_id, conversation_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")

        cliente = await CustomerRepo(session).get(business_id, conv.customer_id)
        if cliente is None:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        mensajes = await MessageRepo(session).list_for_inbox(
            business_id, conversation_id, after_seq=after_seq, limit=limit
        )
        ultimo = await conv_repo.last_customer_message_at(business_id, conversation_id)
        escalamiento = await EscalationRepo(session).open_for_conversation(
            business_id, conversation_id
        )

    return {
        "id": str(conv.id),
        "status": conv.status,
        "channel": conv.channel,
        "created_at": _iso(conv.created_at),
        "last_message_at": _iso(conv.last_message_at),
        "customer": {
            "id": str(cliente.id),
            "display_name": cliente.display_name,
            "channel_identifier": cliente.channel_identifier,
            "created_at": _iso(cliente.created_at),
            "last_seen_at": _iso(cliente.last_seen_at),
        },
        "open_escalation": (
            {
                "id": str(escalamiento.id),
                "reason": escalamiento.reason,
                "created_at": _iso(escalamiento.created_at),
            }
            if escalamiento is not None
            else None
        ),
        "window": _ventana(conv.channel, ultimo),
        "messages": [
            {
                "id": str(m["id"]),
                "seq": m["seq"],
                "role": m["role"],
                "content_type": m["content_type"],
                "content": m["content"],
                "created_at": _iso(m["created_at"]),
                "media_id": str(m["media_id"]) if m["media_id"] else None,
                "media_mime_type": m["media_mime_type"],
            }
            for m in mensajes
        ],
    }


@router.post("/conversations/{conversation_id}/read", status_code=204)
async def mark_conversation_read(conversation_id: UUID, business_id: BusinessId) -> None:
    async with tenant_session(business_id) as session:
        conv_repo = ConversationRepo(session)
        if await conv_repo.get(business_id, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")
        await conv_repo.mark_read(business_id, conversation_id)


@router.post("/conversations/{conversation_id}/status")
async def change_conversation_status(
    conversation_id: UUID, body: ConversationStatusChange, business_id: BusinessId
) -> dict[str, Any]:
    """
    Tomar la conversación (`human`: el agente calla) o devolverla al agente
    (`auto`: además cierra el escalamiento abierto).
    """
    async with tenant_session(business_id) as session:
        if await ConversationRepo(session).get(business_id, conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")
        await _cambiar_estado(session, business_id, conversation_id, body.status)

    return {"id": str(conversation_id), "status": body.status.value}


@router.post("/conversations/{conversation_id}/reply")
async def human_reply(
    conversation_id: UUID, body: HumanReply, business_id: BusinessId
) -> dict[str, Any]:
    """
    Envía un mensaje del operador al cliente y lo guarda con rol 'human'.

    Primero se envía y después se guarda: si WhatsApp lo rechaza, el historial no
    muestra un mensaje que el cliente nunca recibió.
    """
    texto = body.text.strip()
    if not texto:
        raise HTTPException(status_code=422, detail="El mensaje está vacío")

    destino = await _validar_envio(business_id, conversation_id)

    if destino.channel == "whatsapp":
        try:
            await send_whatsapp_reply(
                OutboundMessage(
                    business_id=business_id,
                    channel=destino.channel,
                    channel_identifier=destino.channel_identifier,
                    text=texto,
                ),
                phone_number_id=destino.phone_number_id,
            )
        except WhatsAppError as exc:
            raise HTTPException(status_code=502, detail=f"No se pudo enviar: {exc}") from exc

    await _registrar_envio(business_id, conversation_id, texto, body.return_to_auto)
    return {"sent": True, "returned_to_auto": body.return_to_auto}


@router.post("/conversations/{conversation_id}/reply-image")
async def human_reply_image(
    conversation_id: UUID, body: ImageReply, business_id: BusinessId
) -> dict[str, Any]:
    """
    Envía una imagen (con pie de foto opcional) del operador al cliente.

    La imagen viaja en base64 dentro del JSON en vez de multipart: evita sumar
    `python-multipart` como dependencia para un único endpoint, y con el tope de
    5 MB el sobrecosto de base64 no importa.
    """
    try:
        datos = base64.b64decode(body.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="La imagen no es base64 válido") from exc

    try:
        check_image(datos, body.mime_type)
    except InvalidMedia as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    pie = body.caption.strip()
    destino = await _validar_envio(business_id, conversation_id)

    if destino.channel == "whatsapp":
        try:
            await send_whatsapp_image(
                destino.channel_identifier,
                datos,
                body.mime_type,
                caption=pie,
                phone_number_id=destino.phone_number_id,
            )
        except WhatsAppError as exc:
            raise HTTPException(status_code=502, detail=f"No se pudo enviar: {exc}") from exc

    await _registrar_envio(
        business_id, conversation_id, pie, body.return_to_auto, imagen=(datos, body.mime_type)
    )
    return {"sent": True, "returned_to_auto": body.return_to_auto}


@router.get("/media/{media_id}")
async def get_media(media_id: UUID, business_id: BusinessId) -> Response:
    """El archivo de un mensaje. Mismo aislamiento que todo: token -> tenant -> RLS."""
    async with tenant_session(business_id) as session:
        media = await MediaRepo(session).get(business_id, media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="Archivo no encontrado")
        datos, mime_type = bytes(media.data), media.mime_type

    return Response(
        content=datos,
        media_type=mime_type,
        # Privado: son fotos de clientes, ningun proxy intermedio debe guardarlas.
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ======================================================================
# Metricas (RF-22)
# ======================================================================
@router.get("/metrics")
async def get_metrics(
    business_id: BusinessId,
    days: int = Query(default=7, ge=1, le=90),
) -> dict[str, Any]:
    """
    Metricas del periodo sobre `turn_logs`: volumen, % resuelto sin humano,
    costo y latencia. `cache_read_tokens` mide el ahorro de cache (RNF-02).
    """
    desde = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

    async with tenant_session(business_id) as session:
        fila = (
            await session.execute(
                select(
                    func.count(TurnLog.id).label("turnos"),
                    func.count(func.distinct(TurnLog.conversation_id)).label("conversaciones"),
                    func.coalesce(func.sum(TurnLog.input_tokens), 0).label("input_tokens"),
                    func.coalesce(func.sum(TurnLog.output_tokens), 0).label("output_tokens"),
                    func.coalesce(func.sum(TurnLog.cache_read_tokens), 0).label("cache_read"),
                    func.coalesce(func.sum(TurnLog.cost_estimate_usd), 0).label("costo"),
                    func.avg(TurnLog.latency_ms).label("latencia_media"),
                ).where(TurnLog.business_id == business_id, TurnLog.created_at >= desde)
            )
        ).one()

        escalados = (
            await session.execute(
                select(func.count(func.distinct(TurnLog.conversation_id))).where(
                    TurnLog.business_id == business_id,
                    TurnLog.created_at >= desde,
                    TurnLog.detected_intent == "escalate",
                )
            )
        ).scalar_one()

        por_intencion = (
            await session.execute(
                select(TurnLog.detected_intent, func.count(TurnLog.id))
                .where(TurnLog.business_id == business_id, TurnLog.created_at >= desde)
                .group_by(TurnLog.detected_intent)
            )
        ).all()

    conversaciones = int(fila.conversaciones or 0)
    sin_humano = conversaciones - int(escalados or 0)

    return {
        "periodo_dias": days,
        "turnos": int(fila.turnos or 0),
        "conversaciones": conversaciones,
        "conversaciones_escaladas": int(escalados or 0),
        "porcentaje_sin_humano": (
            round(100.0 * sin_humano / conversaciones, 1) if conversaciones else None
        ),
        "tokens": {
            "input": int(fila.input_tokens or 0),
            "output": int(fila.output_tokens or 0),
            "cache_read": int(fila.cache_read or 0),
        },
        "costo_usd_total": float(fila.costo or 0),
        "costo_usd_por_conversacion": (
            round(float(fila.costo or 0) / conversaciones, 6) if conversaciones else None
        ),
        "latencia_ms_media": int(fila.latencia_media) if fila.latencia_media else None,
        "turnos_por_intencion": {str(i): int(c) for i, c in por_intencion if i},
    }


# ======================================================================
# Costos y salud del prefijo cacheado (fase 7)
# ======================================================================
@router.get("/metrics/costs")
async def get_cost_dashboard(
    business_id: BusinessId, days: int = Query(default=7, ge=1, le=90)
) -> dict[str, Any]:
    """
    Costo diario y salud de la caché.

    `cache_hit_rate` es la métrica de diagnóstico clave: si baja del 60% de
    forma sostenida, casi seguro se coló algo dinámico en el prefijo del prompt
    y cada turno se está pagando completo (RNF-02).
    """
    desde = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)

    async with tenant_session(business_id) as session:
        filas = (
            await session.execute(
                text("""
                    SELECT date_trunc('day', created_at) AS dia,
                           count(*)                       AS turnos,
                           count(DISTINCT conversation_id) AS conversaciones,
                           coalesce(sum(input_tokens), 0)  AS input_tokens,
                           coalesce(sum(output_tokens), 0) AS output_tokens,
                           coalesce(sum(cache_read_tokens), 0) AS cache_read,
                           coalesce(sum(cost_estimate_usd), 0) AS costo,
                           percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                           percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
                    FROM turn_logs
                    WHERE business_id = :b AND created_at >= :d
                    GROUP BY 1 ORDER BY 1 DESC
                """),
                {"b": business_id, "d": desde},
            )
        ).all()

    dias = []
    for f in filas:
        entrada_total = int(f.input_tokens) + int(f.cache_read)
        cache_hit = round(int(f.cache_read) / entrada_total, 3) if entrada_total else None
        dias.append({
            "dia": f.dia.date().isoformat(),
            "turnos": int(f.turnos),
            "conversaciones": int(f.conversaciones),
            "input_tokens": int(f.input_tokens),
            "output_tokens": int(f.output_tokens),
            "cache_read_tokens": int(f.cache_read),
            "cache_hit_rate": cache_hit,
            "costo_usd": float(f.costo),
            "costo_usd_por_conversacion": (
                round(float(f.costo) / int(f.conversaciones), 6) if f.conversaciones else None
            ),
            "latencia_p50_ms": int(f.p50) if f.p50 else None,
            "latencia_p95_ms": int(f.p95) if f.p95 else None,
        })

    alertas = [
        f"{d['dia']}: cache hit {d['cache_hit_rate']:.0%} (<60%): revisar el prefijo del prompt"
        for d in dias
        if d["cache_hit_rate"] is not None and d["cache_hit_rate"] < 0.60 and d["turnos"] >= 10
    ]

    return {"periodo_dias": days, "dias": dias, "alertas": alertas}


# ======================================================================
# Privacidad: eliminación de datos (RNF-05, Ley 1581)
# ======================================================================
class DataDeletionRequest(BaseModel):
    channel_identifier: str
    requested_by: str | None = None


@router.post("/privacy/delete-customer")
async def delete_customer_data(
    body: DataDeletionRequest, business_id: BusinessId
) -> dict[str, Any]:
    """
    Borra los datos de un cliente a solicitud del titular (Ley 1581).

    Deja evidencia en `data_deletion_log` con el hash del identificador: prueba
    del cumplimiento sin conservar el dato personal.
    """
    resultado = await eliminar_datos_cliente(
        business_id, body.channel_identifier, body.requested_by
    )

    if not resultado["found"]:
        raise HTTPException(status_code=404, detail="No hay datos de ese cliente")

    return {
        "deleted": True,
        "messages_deleted": resultado["messages_deleted"],
        "requests_deleted": resultado["requests_deleted"],
    }


@router.get("/privacy/deletion-log")
async def list_deletion_log(business_id: BusinessId, limit: int = 50) -> list[dict[str, Any]]:
    """Evidencia de cumplimiento de las solicitudes de eliminación."""
    async with tenant_session(business_id) as session:
        filas = (
            await session.execute(
                text("""
                    SELECT identifier_hash, messages_deleted, requests_deleted,
                           requested_by, deleted_at
                    FROM data_deletion_log
                    WHERE business_id = :b
                    ORDER BY deleted_at DESC LIMIT :l
                """),
                {"b": business_id, "l": limit},
            )
        ).all()

    return [
        {
            # Prefijo del hash: identifica el registro sin exponer el número.
            "identifier_hash": f.identifier_hash[:16],
            "messages_deleted": f.messages_deleted,
            "requests_deleted": f.requests_deleted,
            "requested_by": f.requested_by,
            "deleted_at": f.deleted_at.isoformat(),
        }
        for f in filas
    ]


# ======================================================================
# Sandbox (RF-23, HU-ADM-05)
# ======================================================================
@router.post("/sandbox/message")
async def sandbox_message(body: SandboxMessage, business_id: BusinessId) -> SandboxResponse:
    """
    Prueba el agente SIN tocar WhatsApp.

    Corre el MISMO orquestador sobre un canal 'sandbox' con un cliente de prueba,
    asi lo que se ve aca es lo que va a pasar en produccion. Devuelve ademas la
    intencion detectada y las herramientas usadas, para poder depurar.
    """
    from app.core.orchestrator import TurnOutcomeKind, handle_inbound_message
    from app.schemas import InboundMessage

    inbound = InboundMessage(
        business_id=business_id,
        channel="sandbox",  # el envio saliente se saltea para este canal
        channel_identifier=body.customer_identifier,
        text=body.text,
        external_message_id=None,
        received_at=dt.datetime.now(dt.UTC),
    )

    # El orquestador dice cómo terminó el turno: no se adivina leyendo "el último
    # mensaje", que es el del propio cliente cuando el agente calla.
    resultado = await handle_inbound_message(inbound)

    motivos_sin_respuesta = {
        TurnOutcomeKind.GROUPED: (
            "El cliente siguió escribiendo: este mensaje se responde junto con los "
            "siguientes, en una sola respuesta."
        ),
        TurnOutcomeKind.HUMAN_MODE: (
            "La conversación está en modo humano: el agente no responde hasta que "
            "una persona la devuelva al agente desde la bandeja."
        ),
    }

    async with tenant_session(business_id) as session:
        cliente = await CustomerRepo(session).get_by_identifier(
            business_id, "sandbox", body.customer_identifier
        )
        if cliente is None:
            raise HTTPException(status_code=500, detail="No se creó el cliente de prueba")

        conv = await ConversationRepo(session).get_or_create_active(
            business_id, cliente.id, "sandbox"
        )

        # La traza de ESTE mensaje, no la del último turno de la conversación.
        log = None
        if resultado.message_in_id is not None:
            log = (
                await session.execute(
                    select(TurnLog)
                    .where(
                        TurnLog.business_id == business_id,
                        TurnLog.message_in_id == resultado.message_in_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        agente_nombre = None
        if log is not None and log.routed_agent_id:
            agente = await AgentRepo(session).get(business_id, log.routed_agent_id)
            agente_nombre = agente.name if agente else None

    return SandboxResponse(
        reply_text=resultado.reply_text or "",
        replied=resultado.replied,
        outcome=resultado.kind.value,
        no_reply_reason=None if resultado.replied else motivos_sin_respuesta.get(
            resultado.kind, "El agente no respondió este mensaje."
        ),
        detected_intent=log.detected_intent if log else None,
        routed_agent=agente_nombre,
        tools_used=list(log.tools_used) if log and log.tools_used else [],
        conversation_id=conv.id,
        input_tokens=log.input_tokens if log else 0,
        output_tokens=log.output_tokens if log else 0,
        cache_read_tokens=log.cache_read_tokens if log else 0,
        cost_estimate_usd=float(log.cost_estimate_usd) if log and log.cost_estimate_usd else None,
        latency_ms=log.latency_ms if log else None,
    )


__all__ = ["current_business", "generate_token", "hash_token", "router"]
