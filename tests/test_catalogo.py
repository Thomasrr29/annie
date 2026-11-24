"""
Precios por referencia (app/core/catalog.py): el modelo elige el ítem y el
sistema pone el número. Pura lógica: sin base ni modelo.
"""

from __future__ import annotations

import re
from uuid import uuid4

from app.core.catalog import (
    format_price,
    keys_for,
    price_key,
    prompt_section,
    render_prices,
    verification_lines,
)
from app.schemas import CatalogEntry


def _item(nombre: str, precio: int) -> CatalogEntry:
    return CatalogEntry(id=uuid4(), name=nombre, price=precio)


PELUQUERIA = [
    _item("Corte de cabello", 30_000),
    _item("Peinado para evento", 60_000),
    _item("Tinte completo", 90_000),
]


def test_la_clave_es_estable_y_sin_tildes() -> None:
    assert price_key("Tinte completo") == "tinte-completo"
    assert price_key("Plan Nott Stopping (mensual)") == "plan-nott-stopping-mensual"
    assert price_key("Depilación") == "depilacion"


def test_claves_repetidas_se_numeran() -> None:
    claves = keys_for([_item("Combo", 1), _item("combo", 2)])
    assert list(claves) == ["combo", "combo-2"]


def test_formato_colombiano() -> None:
    assert format_price(90_000) == "$90.000"
    assert format_price(109_900) == "$109.900"
    assert format_price(1_250_000) == "$1.250.000"


def test_el_prompt_no_le_muestra_cifras_al_modelo() -> None:
    """Si el modelo no ve un número, no lo puede copiar mal."""
    seccion = prompt_section(PELUQUERIA)
    assert "{{precio:tinte-completo}}" in seccion
    assert "90" not in seccion and "$" not in seccion
    assert not re.search(r"\d", seccion)


def test_reemplaza_cada_referencia_por_su_precio() -> None:
    r = render_prices(
        "El tinte vale {{precio:tinte-completo}} y el corte {{ Precio : Corte-de-cabello }}.",
        PELUQUERIA,
    )
    assert r.text == "El tinte vale $90.000 y el corte $30.000."
    assert r.unknown_refs == ()


def test_una_referencia_inventada_se_reporta_y_no_se_reemplaza() -> None:
    r = render_prices("La keratina vale {{precio:keratina}}.", PELUQUERIA)
    assert r.unknown_refs == ("keratina",)
    assert "{{precio:keratina}}" in r.text


def test_lineas_de_verificacion() -> None:
    assert verification_lines(PELUQUERIA).splitlines() == [
        "Corte de cabello: $30.000",
        "Peinado para evento: $60.000",
        "Tinte completo: $90.000",
    ]


def test_el_prompt_del_agente_lista_el_catalogo_sin_cifras() -> None:
    from app.core.llm import build_system_prefix
    from app.schemas import AgentConfig, BusinessConfig

    bid = uuid4()
    agente = AgentConfig(
        id=uuid4(), business_id=bid, name="Informador",
        routing_description="responde precios", instructions_md="# Rol",
    )
    negocio = BusinessConfig(id=bid, name="Peluquería", catalog=PELUQUERIA)

    prefijo = build_system_prefix(agente, negocio)[0]["text"]

    assert "{{precio:tinte-completo}}" in prefijo
    assert "90.000" not in prefijo and "90000" not in prefijo
    assert "NUNCA escribas un precio con números" in prefijo
