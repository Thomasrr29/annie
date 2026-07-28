"""
Los seeds de desarrollo deben producir un tenant coherente y utilizable.

Se valida el CONTENIDO, no solo que el script no reviente: un demo con agentes
sin routing_description o con herramientas inexistentes no sirve para probar el
orquestador en la fase 5.
"""

from __future__ import annotations

from app.schemas import MemoryScope
from scripts.seed_dev import DEMO_AGENTS, DEMO_BUSINESS_ID, DEMO_KNOWLEDGE

# Herramientas integradas del MVP (CLAUDE.md). search_knowledge esta diferida.
HERRAMIENTAS_CONOCIDAS = {"capture_request", "notify_owner", "search_knowledge"}


def test_el_id_del_negocio_demo_es_fijo() -> None:
    """Un id estable permite referenciarlo sin copiar uuids nuevos cada vez."""
    assert str(DEMO_BUSINESS_ID) == "00000000-0000-4000-8000-000000000001"


def test_hay_al_menos_dos_agentes() -> None:
    """La fase 2 pide un demo con 2 agentes para ejercitar el enrutamiento."""
    assert len(DEMO_AGENTS) >= 2


def test_los_agentes_tienen_routing_description_util() -> None:
    """
    El orquestador enruta leyendo `routing_description`: si es vaga o vacia, la
    clasificacion no puede funcionar.
    """
    for agente in DEMO_AGENTS:
        assert agente.routing_description.strip(), f"{agente.name} sin routing_description"
        assert len(agente.routing_description) > 40, (
            f"{agente.name}: routing_description demasiado corta para enrutar"
        )


def test_los_agentes_solo_declaran_herramientas_existentes() -> None:
    """Una herramienta inventada en allowed_tools falla recien en runtime."""
    for agente in DEMO_AGENTS:
        desconocidas = set(agente.allowed_tools) - HERRAMIENTAS_CONOCIDAS
        assert not desconocidas, f"{agente.name} declara herramientas inexistentes: {desconocidas}"


def test_los_nombres_de_agente_no_se_repiten() -> None:
    nombres = [a.name for a in DEMO_AGENTS]
    assert len(nombres) == len(set(nombres))


def test_memory_scope_es_valido() -> None:
    validos = {m.value for m in MemoryScope}
    for agente in DEMO_AGENTS:
        assert agente.memory_scope in validos, f"{agente.name}: memory_scope invalido"


def test_el_conocimiento_demo_no_esta_vacio() -> None:
    assert DEMO_KNOWLEDGE
    for titulo, contenido in DEMO_KNOWLEDGE:
        assert titulo.strip(), "documento sin titulo"
        assert contenido.strip(), f"documento '{titulo}' sin contenido"


def test_los_titulos_de_conocimiento_no_se_repiten() -> None:
    """Titulos duplicados confunden al modelo al leer el bloque concatenado."""
    titulos = [t for t, _ in DEMO_KNOWLEDGE]
    assert len(titulos) == len(set(titulos))
