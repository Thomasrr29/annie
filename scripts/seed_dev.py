"""
Seeds de desarrollo: un negocio demo con 2 agentes y conocimiento de ejemplo.

Uso:
    .venv/bin/python -m scripts.seed_dev            # crea (idempotente)
    .venv/bin/python -m scripts.seed_dev --reset    # borra el demo y lo recrea

El negocio demo tiene un id fijo para poder referenciarlo desde pruebas
manuales, el panel o llamadas al webhook sin tener que copiar un uuid nuevo.

Regla de curacion (CLAUDE.md §5): nada entra a la base de conocimiento que no
sea aceptable que un cliente vea. El contenido de ejemplo la respeta.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import NamedTuple
from uuid import UUID

from sqlalchemy import text

from app.db.repositories import AgentRepo
from app.db.session import dispose_engine, set_tenant, system_session

# Id fijo del negocio demo: estable entre corridas.
DEMO_BUSINESS_ID = UUID("00000000-0000-4000-8000-000000000001")
DEMO_NAME = "Peluquería Demo"

DEMO_KNOWLEDGE: list[tuple[str, str]] = [
    (
        "Horarios de atención",
        "Atendemos de lunes a viernes de 8:00 a 18:00 y sábados de 9:00 a 14:00.\n"
        "Domingos y festivos permanecemos cerrados.",
    ),
    (
        "Servicios y precios",
        "Corte de cabello: $30.000\n"
        "Tinte completo: $90.000\n"
        "Peinado para evento: $60.000\n"
        "Los precios incluyen lavado y secado.",
    ),
    (
        "Ubicación y contacto",
        "Estamos en la Calle 45 #12-30, barrio Chapinero, Bogotá.\n"
        "Contamos con parqueadero para clientes.",
    ),
    (
        "Políticas de cita",
        "Las citas se confirman el día anterior por WhatsApp.\n"
        "Si necesitás cancelar, avisanos con al menos 4 horas de anticipación.",
    ),
]

class DemoAgent(NamedTuple):
    """Definicion de un agente demo (tipada, para no perder el chequeo)."""

    name: str
    routing_description: str
    instructions_md: str
    allowed_tools: list[str]
    memory_scope: str
    response_style: str


DEMO_AGENTS: list[DemoAgent] = [
    DemoAgent(
        name="Informador",
        routing_description=(
            "Responde preguntas sobre horarios, servicios, precios, ubicación y "
            "políticas del salón. Usar cuando el cliente PIDE INFORMACIÓN y no "
            "quiere agendar ni dejar una solicitud."
        ),
        instructions_md=(
            "# Rol\n"
            "Sos el asistente informativo de la peluquería.\n\n"
            "# Tarea\n"
            "Respondé con la información del negocio que tenés disponible.\n\n"
            "# Reglas\n"
            "- Si el dato no está en tu conocimiento, decilo con honestidad y "
            "ofrecé pasar la conversación a una persona del salón.\n"
            "- NUNCA inventes precios, horarios ni promociones.\n"
            "- Respuestas breves: el cliente está en WhatsApp.\n"
        ),
        # Sin herramientas: con "base completa" el conocimiento ya va en el prompt.
        allowed_tools=[],
        memory_scope="customer",
        response_style="Cercano y breve, tuteo, español de Colombia.",
    ),
    DemoAgent(
        name="Agendador",
        routing_description=(
            "Captura solicitudes de cita: fecha, hora, servicio y nombre del "
            "cliente. Usar cuando el cliente QUIERE AGENDAR, reprogramar o "
            "cancelar un turno."
        ),
        instructions_md=(
            "# Rol\n"
            "Tomás las solicitudes de cita del salón.\n\n"
            "# Tarea\n"
            "Recolectá estos datos, uno o dos por mensaje:\n"
            "1. Servicio deseado\n"
            "2. Día y franja horaria preferida\n"
            "3. Nombre de la persona\n\n"
            "# Reglas\n"
            "- Cuando tengas los tres datos, registrá la solicitud con "
            "`capture_request` y avisá al dueño con `notify_owner`.\n"
            "- NO confirmes la cita como agendada: aclarale al cliente que el "
            "salón le confirma la disponibilidad.\n"
            "- Si pide un horario fuera de atención, decíselo y ofrecé otro.\n"
        ),
        allowed_tools=["capture_request", "notify_owner"],
        memory_scope="customer",
        response_style="Cordial y concreto, una pregunta a la vez.",
    ),
]


async def seed(reset: bool = False) -> None:
    """
    Crea el negocio demo. Idempotente: se puede correr varias veces.

    Fija `app.current_business_id` al negocio demo antes de escribir: las
    politicas RLS lo exigen tambien para el INSERT (WITH CHECK), y el alta de un
    tenant es justamente el caso donde todavia no hay contexto de tenant.
    """
    async with system_session() as session:
        await set_tenant(session, DEMO_BUSINESS_ID)

        if reset:
            # ON DELETE CASCADE limpia agentes, conocimiento y conversaciones.
            await session.execute(
                text("DELETE FROM businesses WHERE id = :bid"), {"bid": DEMO_BUSINESS_ID}
            )
            print(f"Negocio demo {DEMO_BUSINESS_ID} eliminado.")

        existe = (
            await session.execute(
                text("SELECT 1 FROM businesses WHERE id = :bid"), {"bid": DEMO_BUSINESS_ID}
            )
        ).scalar_one_or_none()

        if existe:
            print(f"El negocio demo ya existe ({DEMO_BUSINESS_ID}). Nada que hacer.")
            print("Usá --reset para recrearlo desde cero.")
            return

        await session.execute(
            text("""
                INSERT INTO businesses
                  (id, name, persona_tone, language, timezone, business_hours,
                   notify_channel, notify_config)
                VALUES
                  (:bid, :name, :tone, 'es', 'America/Bogota', :hours,
                   'email', :notify)
            """),
            {
                "bid": DEMO_BUSINESS_ID,
                "name": DEMO_NAME,
                "tone": "Cercano, amable y breve. Tuteo, español de Colombia.",
                "hours": (
                    '{"lun_vie": "08:00-18:00", "sab": "09:00-14:00", "dom": "cerrado"}'
                ),
                "notify": '{"email": "dueno@peluqueriademo.test"}',
            },
        )

        for titulo, contenido in DEMO_KNOWLEDGE:
            await session.execute(
                text("""
                    INSERT INTO knowledge_documents
                      (business_id, title, source, raw_content, status)
                    VALUES (:bid, :titulo, 'faq', :contenido, 'indexed')
                """),
                {"bid": DEMO_BUSINESS_ID, "titulo": titulo, "contenido": contenido},
            )

        # Los agentes van por el repositorio: es el mismo camino que usa el panel.
        agent_repo = AgentRepo(session)
        for agente in DEMO_AGENTS:
            await agent_repo.create(
                DEMO_BUSINESS_ID,
                name=agente.name,
                routing_description=agente.routing_description,
                instructions_md=agente.instructions_md,
                allowed_tools=list(agente.allowed_tools),
                memory_scope=agente.memory_scope,
                response_style=agente.response_style,
            )

    print(f"Negocio demo creado: {DEMO_NAME} ({DEMO_BUSINESS_ID})")
    print(f"  - {len(DEMO_KNOWLEDGE)} documentos de conocimiento")
    print(f"  - {len(DEMO_AGENTS)} agentes: {', '.join(a.name for a in DEMO_AGENTS)}")


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Seeds de desarrollo")
    parser.add_argument(
        "--reset", action="store_true", help="borra el negocio demo antes de crearlo"
    )
    args = parser.parse_args()

    try:
        await seed(reset=args.reset)
        return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
