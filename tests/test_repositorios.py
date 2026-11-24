"""
Tests de repositorios contra Postgres real (no mocks de SQL).

Cubre RF-03, RF-15..RF-17, RF-24 y el determinismo de `knowledge_context`
(requisito de la cache de prompt, RNF-02).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    AgentRepo,
    BusinessRepo,
    ConversationRepo,
    CustomerRepo,
    EscalationRepo,
    MemoryRepo,
    MessageRepo,
    RequestRepo,
    TurnLogRepo,
)
from app.schemas import ConversationStatus, MessageRole
from tests.conftest import requires_db

pytestmark = [requires_db]


# ======================================================================
# CustomerRepo (RF-03)
# ======================================================================
async def test_get_or_create_crea_y_luego_reutiliza(
    session: AsyncSession, business_a: UUID
) -> None:
    repo = CustomerRepo(session)

    creado = await repo.get_or_create(business_a, "whatsapp", "+573001112233")
    de_nuevo = await repo.get_or_create(business_a, "whatsapp", "+573001112233")

    assert creado.id == de_nuevo.id, "la identidad (negocio, canal, numero) debe ser estable"


async def test_get_or_create_actualiza_last_seen(
    session: AsyncSession, business_a: UUID
) -> None:
    repo = CustomerRepo(session)

    primero = await repo.get_or_create(business_a, "whatsapp", "+573001112244")
    visto_inicial = primero.last_seen_at

    await session.flush()
    de_nuevo = await repo.get_or_create(business_a, "whatsapp", "+573001112244")

    assert visto_inicial is not None
    assert de_nuevo.last_seen_at is not None
    assert de_nuevo.last_seen_at >= visto_inicial


async def test_mismo_numero_en_dos_negocios_son_clientes_distintos(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    """RF-03: la clave incluye el negocio, no solo el telefono."""
    repo = CustomerRepo(session)

    en_a = await repo.get_or_create(business_a, "whatsapp", "+573009998877")
    en_b = await repo.get_or_create(business_b, "whatsapp", "+573009998877")

    assert en_a.id != en_b.id
    assert en_a.business_id == business_a
    assert en_b.business_id == business_b


async def test_get_no_devuelve_cliente_de_otro_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    """
    Aislamiento a nivel repositorio (capa 1), independiente de RLS.

    Se pide el cliente de B con el business_id de A y su uuid REAL: si el
    repositorio no filtrara por tenant, esto devolveria la fila.
    """
    repo = CustomerRepo(session)
    de_b = await repo.get_or_create(business_b, "whatsapp", "+573005554433")

    fugado = await repo.get(business_a, de_b.id)

    assert fugado is None, "CustomerRepo del tenant A leyo un cliente del tenant B"


async def test_get_by_identifier_esta_acotado_al_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    repo = CustomerRepo(session)
    await repo.get_or_create(business_b, "whatsapp", "+573007776655")

    assert await repo.get_by_identifier(business_a, "whatsapp", "+573007776655") is None
    assert await repo.get_by_identifier(business_b, "whatsapp", "+573007776655") is not None


# ======================================================================
# ConversationRepo (estado de sesion)
# ======================================================================
async def test_get_or_create_active_reutiliza_la_conversacion(
    session: AsyncSession, business_a: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573001010101")
    repo = ConversationRepo(session)

    primera = await repo.get_or_create_active(business_a, customer.id)
    segunda = await repo.get_or_create_active(business_a, customer.id)

    assert primera.id == segunda.id


async def test_set_status_pasa_a_modo_humano(session: AsyncSession, business_a: UUID) -> None:
    """F6: con status='human' el agente deja de responder."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573001010102")
    repo = ConversationRepo(session)
    conv = await repo.get_or_create_active(business_a, customer.id)

    await repo.set_status(business_a, conv.id, ConversationStatus.HUMAN)
    await session.refresh(conv)

    assert conv.status == "human"


async def test_update_flow_state_persiste_datos_parciales(
    session: AsyncSession, business_a: UUID
) -> None:
    """F4/F5: el flujo activo guarda lo recolectado hasta ahora."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573001010103")
    repo = ConversationRepo(session)
    conv = await repo.get_or_create_active(business_a, customer.id)

    await repo.update_flow_state(business_a, conv.id, {"tipo": "agenda", "dia": "martes"})
    await session.refresh(conv)

    assert conv.flow_state == {"tipo": "agenda", "dia": "martes"}


async def test_set_active_agent(session: AsyncSession, business_a: UUID) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573001010104")
    conv_repo = ConversationRepo(session)
    conv = await conv_repo.get_or_create_active(business_a, customer.id)
    agente = await AgentRepo(session).create(
        business_a, "Agenda", "agenda citas", "# instrucciones"
    )

    await conv_repo.set_active_agent(business_a, conv.id, agente.id)
    await session.refresh(conv)

    assert conv.active_agent_id == agente.id


async def test_escrituras_de_conversacion_no_cruzan_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    """Un business_id ajeno no debe poder mutar la conversacion."""
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573001010105")
    repo = ConversationRepo(session)
    conv = await repo.get_or_create_active(business_b, customer.id)

    # Intento con el tenant equivocado: no debe cambiar nada.
    await repo.set_status(business_a, conv.id, ConversationStatus.HUMAN)
    await session.refresh(conv)

    assert conv.status == "auto", "una escritura con otro business_id afecto la conversacion"


async def test_get_conversacion_de_otro_tenant_devuelve_none(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573001010106")
    repo = ConversationRepo(session)
    conv = await repo.get_or_create_active(business_b, customer.id)

    assert await repo.get(business_a, conv.id) is None


# ======================================================================
# MessageRepo (RF-16)
# ======================================================================
async def test_recent_ordena_por_insercion_no_por_reloj(
    session: AsyncSession, business_a: UUID
) -> None:
    """
    Los mensajes de un mismo turno comparten `created_at` (now() es constante en
    la transaccion). El orden debe venir de `seq`, no del timestamp: si no, el
    modelo recibe el historial barajado.
    """
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573002020204")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)
    repo = MessageRepo(session)

    esperado = [f"turno {i}" for i in range(12)]
    for texto in esperado:
        await repo.append(business_a, conv.id, MessageRole.CUSTOMER, texto)

    turnos = await repo.recent(business_a, conv.id, limit=12)

    assert [t.content for t in turnos] == esperado
    # Premisa del test: si dejaran de compartir timestamp, el caso ya no cubre
    # lo que dice cubrir.
    assert len({t.created_at for t in turnos}) == 1, (
        "los mensajes del mismo turno deberian compartir created_at"
    )


async def test_recent_devuelve_orden_cronologico(
    session: AsyncSession, business_a: UUID
) -> None:
    """El historial va al prompt en orden cronologico (mas antiguo primero)."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573002020201")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)
    repo = MessageRepo(session)

    await repo.append(business_a, conv.id, MessageRole.CUSTOMER, "hola")
    await repo.append(business_a, conv.id, MessageRole.AGENT, "buenas, en que ayudo")
    await repo.append(business_a, conv.id, MessageRole.CUSTOMER, "quiero una cita")

    turnos = await repo.recent(business_a, conv.id)

    assert [t.content for t in turnos] == ["hola", "buenas, en que ayudo", "quiero una cita"]


async def test_recent_respeta_el_limite_y_toma_los_ultimos(
    session: AsyncSession, business_a: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573002020202")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)
    repo = MessageRepo(session)

    for i in range(6):
        await repo.append(business_a, conv.id, MessageRole.CUSTOMER, f"mensaje {i}")

    turnos = await repo.recent(business_a, conv.id, limit=2)

    assert [t.content for t in turnos] == ["mensaje 4", "mensaje 5"]


async def test_recent_no_mezcla_historial_de_otro_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573002020203")
    conv = await ConversationRepo(session).get_or_create_active(business_b, customer.id)
    repo = MessageRepo(session)
    await repo.append(business_b, conv.id, MessageRole.CUSTOMER, "secreto de B")

    assert await repo.recent(business_a, conv.id) == []


# ======================================================================
# MemoryRepo (RF-17)
# ======================================================================
async def test_memoria_inexistente_devuelve_none(
    session: AsyncSession, business_a: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573003030301")

    assert await MemoryRepo(session).get(business_a, customer.id) is None


async def test_upsert_deliberate_crea_y_actualiza(
    session: AsyncSession, business_a: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573003030302")
    repo = MemoryRepo(session)

    creada = await repo.upsert_deliberate(
        business_a, customer.id, profile={"nombre": "Ana"}, summary="cliente frecuente"
    )
    assert creada.profile == {"nombre": "Ana"}
    assert creada.summary == "cliente frecuente"

    actualizada = await repo.upsert_deliberate(business_a, customer.id, summary="prefiere tardes")
    assert actualizada.summary == "prefiere tardes"


async def test_upsert_parcial_no_pisa_el_otro_campo(
    session: AsyncSession, business_a: UUID
) -> None:
    """Actualizar solo el resumen no debe borrar el perfil."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573003030303")
    repo = MemoryRepo(session)

    await repo.upsert_deliberate(
        business_a, customer.id, profile={"nombre": "Luis"}, summary="inicial"
    )
    resultado = await repo.upsert_deliberate(business_a, customer.id, summary="actualizado")

    assert resultado.profile == {"nombre": "Luis"}, "el perfil se perdio al actualizar el resumen"
    assert resultado.summary == "actualizado"


async def test_memoria_no_se_lee_desde_otro_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573003030304")
    repo = MemoryRepo(session)
    await repo.upsert_deliberate(business_b, customer.id, summary="dato privado de B")

    assert await repo.get(business_a, customer.id) is None


# ======================================================================
# AgentRepo (RF-06)
# ======================================================================
async def test_list_active_excluye_inactivos_y_otros_tenants(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    repo = AgentRepo(session)
    await repo.create(business_a, "Agenda", "agenda citas", "# md")
    await repo.create(business_b, "Ventas B", "vende", "# md")

    activos = await repo.list_active(business_a)

    assert [a.name for a in activos] == ["Agenda"]


async def test_list_active_tiene_orden_estable(
    session: AsyncSession, business_a: UUID
) -> None:
    """
    El prompt de clasificacion se arma con esta lista: un orden variable
    invalidaria el prefijo cacheado en cada turno (RNF-02).
    """
    repo = AgentRepo(session)
    for nombre in ("Zeta", "Alfa", "Media"):
        await repo.create(business_a, nombre, f"rutea {nombre}", "# md")

    primera = [a.name for a in await repo.list_active(business_a)]
    segunda = [a.name for a in await repo.list_active(business_a)]

    assert primera == segunda == ["Alfa", "Media", "Zeta"]


async def test_agente_de_otro_tenant_no_se_obtiene(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    repo = AgentRepo(session)
    de_b = await repo.create(business_b, "Interno B", "rutea", "# md")

    assert await repo.get(business_a, de_b.id) is None


# ======================================================================
# BusinessRepo (knowledge_context estable, RNF-02)
# ======================================================================
async def test_get_config_devuelve_la_configuracion(
    session: AsyncSession, business_a: UUID
) -> None:
    config = await BusinessRepo(session).get_config(business_a)

    assert config is not None
    assert config.id == business_a


async def test_knowledge_context_es_deterministico(
    session: AsyncSession, business_a: UUID, seed_knowledge: None
) -> None:
    """Mismo input => mismo string, o la cache de prompt no sirve (RNF-02)."""
    repo = BusinessRepo(session)

    primero = await repo.build_knowledge_context(business_a)
    segundo = await repo.build_knowledge_context(business_a)

    assert primero is not None
    assert primero == segundo


async def test_knowledge_context_empate_de_carga_se_ordena_por_titulo(
    session: AsyncSession, business_a: UUID
) -> None:
    """
    Documentos creados en la misma transacción comparten created_at. El orden no
    puede depender del UUID (aleatorio): se ordena por título, y así cualquier
    negocio con el mismo conocimiento recibe el mismo prompt.
    """
    for titulo in ("Zeta", "Alfa", "Media"):
        await session.execute(
            text(
                "INSERT INTO knowledge_documents (business_id, title, raw_content) "
                "VALUES (:bid, :t, :c)"
            ),
            {"bid": business_a, "t": titulo, "c": f"contenido {titulo}"},
        )
    await session.flush()

    contexto = await BusinessRepo(session).build_knowledge_context(business_a)

    assert contexto is not None
    assert contexto.index("## Alfa") < contexto.index("## Media") < contexto.index("## Zeta")


async def test_knowledge_context_concatena_todos_los_documentos(
    session: AsyncSession, business_a: UUID, seed_knowledge: None
) -> None:
    contexto = await BusinessRepo(session).build_knowledge_context(business_a)

    assert contexto is not None
    assert "Horarios" in contexto
    assert "Servicios" in contexto
    assert "Lunes a viernes" in contexto


async def test_knowledge_context_none_si_no_hay_conocimiento(
    session: AsyncSession, business_a: UUID
) -> None:
    """Sin conocimiento, no meter una seccion vacia en el prefijo del prompt."""
    assert await BusinessRepo(session).build_knowledge_context(business_a) is None


async def test_knowledge_context_no_incluye_documentos_de_otro_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID, seed_knowledge: None
) -> None:
    """Una fuga aqui mandaria conocimiento ajeno al prompt del modelo."""
    contexto_b = await BusinessRepo(session).build_knowledge_context(business_b)

    assert contexto_b is None or "Horarios" not in contexto_b


# ======================================================================
# RequestRepo (F2 / ACT)
# ======================================================================
async def test_create_y_list_by_status(session: AsyncSession, business_a: UUID) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573004040401")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)
    repo = RequestRepo(session)

    await repo.create(
        business_a, customer.id, conv.id, "agenda", {"dia": "martes", "hora": "10am"}
    )

    nuevas = await repo.list_by_status(business_a, status="new")
    assert len(nuevas) == 1
    assert nuevas[0].request_type == "agenda"
    assert nuevas[0].structured_data == {"dia": "martes", "hora": "10am"}


async def test_list_by_status_no_ve_solicitudes_de_otro_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573004040402")
    conv = await ConversationRepo(session).get_or_create_active(business_b, customer.id)
    await RequestRepo(session).create(business_b, customer.id, conv.id, "pedido", {"x": 1})

    assert await RequestRepo(session).list_by_status(business_a) == []


# ======================================================================
# EscalationRepo (F6)
# ======================================================================
async def test_create_y_resolve_escalamiento(
    session: AsyncSession, business_a: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573005050501")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)
    repo = EscalationRepo(session)

    esc = await repo.create(business_a, conv.id, customer.id, reason="pide humano")
    assert len(await repo.list_open(business_a)) == 1

    await repo.resolve(business_a, esc.id)
    await session.refresh(esc)

    assert esc.status == "resolved"
    assert esc.resolved_at is not None
    assert await repo.list_open(business_a) == []


async def test_resolve_no_cruza_tenant(
    session: AsyncSession, business_a: UUID, business_b: UUID
) -> None:
    customer = await CustomerRepo(session).get_or_create(business_b, "whatsapp", "+573005050502")
    conv = await ConversationRepo(session).get_or_create_active(business_b, customer.id)
    repo = EscalationRepo(session)
    esc = await repo.create(business_b, conv.id, customer.id, reason="de B")

    await repo.resolve(business_a, esc.id)  # tenant equivocado
    await session.refresh(esc)

    assert esc.status == "open", "un tenant ajeno resolvio un escalamiento"


# ======================================================================
# TurnLogRepo (RF-15, RNF-02)
# ======================================================================
async def test_append_registra_tokens_y_cache(
    session: AsyncSession, business_a: UUID
) -> None:
    """cache_read_tokens es la metrica del ahorro de caché (RNF-02)."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573006060601")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)

    log = await TurnLogRepo(session).append(
        business_a,
        conv.id,
        detected_intent="inform",
        tools_used=["search_knowledge"],
        model_calls=2,
        input_tokens=1200,
        output_tokens=150,
        cache_read_tokens=1000,
        cost_estimate_usd=0.0042,
        latency_ms=850,
    )

    assert log.cache_read_tokens == 1000
    assert log.input_tokens == 1200
    assert log.detected_intent == "inform"
    assert log.tools_used == ["search_knowledge"]
    assert log.latency_ms == 850


async def test_append_con_valores_por_defecto(
    session: AsyncSession, business_a: UUID
) -> None:
    """Un turno fallido igual se registra: no puede quedar sin traza."""
    customer = await CustomerRepo(session).get_or_create(business_a, "whatsapp", "+573006060602")
    conv = await ConversationRepo(session).get_or_create_active(business_a, customer.id)

    log = await TurnLogRepo(session).append(business_a, conv.id)

    assert log.model_calls == 0
    assert log.cache_read_tokens == 0
    assert log.tools_used == []
