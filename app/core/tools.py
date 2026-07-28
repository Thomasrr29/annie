"""
Herramientas integradas del MVP (RF-09..RF-11).

Dos garantias de seguridad, ambas en CODIGO y no en el prompt:

1. **Allowlist (RF-07).** Una herramienta que el agente no tiene permitida no se
   incluye en el request a la API: el modelo ni sabe que existe. El chequeo en
   `execute_tool` es cinturon extra por si alguna vez se arma el request mal.

2. **Aislamiento (RNF-04).** El `business_id` lo inyecta el ejecutor desde el
   contexto del turno, NUNCA lo elige el modelo. Los esquemas que ve el modelo no
   tienen `business_id`: aunque alucine uno, no hay donde ponerlo.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from uuid import UUID

from app.db.repositories import BusinessRepo, RequestRepo
from app.db.session import tenant_session
from app.schemas import KnowledgeHit

logger = logging.getLogger(__name__)

CAPTURE_REQUEST: Final = "capture_request"
NOTIFY_OWNER: Final = "notify_owner"
SEARCH_KNOWLEDGE: Final = "search_knowledge"

# Esquemas que ve el modelo. Sin business_id/customer_id/conversation_id: esos
# los pone el ejecutor desde el contexto del turno.
TOOL_SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    CAPTURE_REQUEST: {
        "name": CAPTURE_REQUEST,
        "description": (
            "Registra una solicitud del cliente para que el negocio la atienda "
            "(agendar, encargar, reclamar). Usala cuando tengas los datos "
            "necesarios completos. No confirmes la solicitud como aceptada: el "
            "negocio la revisa después."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request_type": {
                    "type": "string",
                    "description": "Tipo de solicitud: agenda, pedido, reclamo, consulta.",
                },
                "structured_data": {
                    "type": "object",
                    "description": (
                        "Datos recolectados (fecha, hora, servicio, nombre, "
                        "detalle). Solo lo que el cliente dijo."
                    ),
                },
            },
            "required": ["request_type", "structured_data"],
        },
    },
    NOTIFY_OWNER: {
        "name": NOTIFY_OWNER,
        "description": (
            "Avisa al dueño del negocio por el medio que tenga configurado. "
            "Usala tras registrar una solicitud importante o cuando el cliente "
            "necesite atención de una persona."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Asunto breve."},
                "body": {"type": "string", "description": "Cuerpo del aviso."},
            },
            "required": ["subject", "body"],
        },
    },
}


class ToolNotAllowed(RuntimeError):
    """El agente intento usar una herramienta fuera de su allowlist (RF-07)."""


def tool_schemas_for(allowed_tools: list[str]) -> list[dict[str, Any]]:
    """
    Esquemas de las herramientas permitidas, en orden estable.

    El orden importa: estas definiciones van en el prefijo cacheado del prompt y
    reordenarlas invalidaria la cache (RNF-02).
    """
    return [TOOL_SCHEMAS[n] for n in sorted(allowed_tools) if n in TOOL_SCHEMAS]


# ======================================================================
# Implementaciones
# ======================================================================
async def capture_request(
    business_id: UUID,
    customer_id: UUID,
    conversation_id: UUID | None,
    request_type: str,
    structured_data: dict[str, Any],
) -> str:
    """RF-10. W: captured_requests."""
    async with tenant_session(business_id) as session:
        solicitud = await RequestRepo(session).create(
            business_id=business_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            request_type=request_type,
            structured_data=structured_data,
        )
        solicitud_id = solicitud.id

    logger.info("solicitud %s capturada (%s)", solicitud_id, request_type)
    return f"Solicitud registrada con el número {str(solicitud_id)[:8]}."


async def notify_owner(business_id: UUID, subject: str, body: str) -> str:
    """
    RF-11. R: businesses.notify_config; envio por el medio configurado.

    El envio real (email/WhatsApp del dueño) queda para la fase 7: hoy se
    registra la intencion, que es lo que necesita el circuito. Devuelve texto
    para el modelo, no lanza: un aviso fallido no debe romper la conversacion.
    """
    async with tenant_session(business_id) as session:
        config = await BusinessRepo(session).get_notify_config(business_id)

    canal = config.get("channel")
    if not canal:
        logger.warning("negocio %s sin canal de notificacion configurado", business_id)
        return "No hay un canal de aviso configurado; la solicitud quedó registrada igual."

    # TODO(fase 7): envio real por email/WhatsApp segun `canal`.
    logger.info(
        "aviso al dueño (%s) del negocio %s: %s | %s", canal, business_id, subject, body[:200]
    )
    return "Listo, le avisé al negocio."


async def search_knowledge(
    business_id: UUID, query: str, top_k: int = 5
) -> list[KnowledgeHit]:
    """
    RF-09. DIFERIDA en el MVP: el conocimiento va completo en el prefijo del
    prompt ("base completa"), asi que el modelo no necesita buscarlo.

    Se deja el stub porque el esquema (`knowledge_chunks`) ya existe y esta es la
    puerta por donde entrara el RAG cuando un tenant crezca.
    """
    logger.info("search_knowledge esta diferida en el MVP (negocio %s)", business_id)
    return []


# ======================================================================
# Ejecutor
# ======================================================================
async def execute_tool(
    name: str,
    tool_input: dict[str, Any],
    *,
    business_id: UUID,
    customer_id: UUID,
    conversation_id: UUID | None,
    allowed_tools: list[str],
) -> str:
    """
    Ejecuta una herramienta validando el allowlist.

    Los identificadores de tenant/cliente vienen del contexto del turno, nunca
    del modelo: `tool_input` solo aporta los campos del esquema publico.
    """
    if name not in allowed_tools:
        # No deberia pasar: la herramienta no se incluyo en el request. Si pasa,
        # es senal de un bug al armar el prompt.
        logger.error(
            "el agente pidio %s sin tenerla permitida (allowlist: %s)", name, allowed_tools
        )
        raise ToolNotAllowed(f"herramienta no permitida: {name}")

    if name == CAPTURE_REQUEST:
        return await capture_request(
            business_id=business_id,
            customer_id=customer_id,
            conversation_id=conversation_id,
            request_type=str(tool_input.get("request_type", "consulta")),
            structured_data=dict(tool_input.get("structured_data") or {}),
        )

    if name == NOTIFY_OWNER:
        return await notify_owner(
            business_id=business_id,
            subject=str(tool_input.get("subject", "Nueva solicitud")),
            body=str(tool_input.get("body", "")),
        )

    if name == SEARCH_KNOWLEDGE:
        await search_knowledge(business_id, str(tool_input.get("query", "")))
        return "La búsqueda no está disponible; usá la información que ya tenés."

    raise ToolNotAllowed(f"herramienta desconocida: {name}")


__all__ = [
    "CAPTURE_REQUEST",
    "NOTIFY_OWNER",
    "SEARCH_KNOWLEDGE",
    "TOOL_SCHEMAS",
    "ToolNotAllowed",
    "capture_request",
    "execute_tool",
    "notify_owner",
    "search_knowledge",
    "tool_schemas_for",
]
