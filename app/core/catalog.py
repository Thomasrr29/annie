"""
Precios por referencia: el modelo elige el ítem, el sistema pone el número.

    modelo escribe:   "El tinte completo vale {{precio:tinte-completo}}."
    sistema entrega:  "El tinte completo vale $90.000."

El modelo nunca ve una cifra que pueda copiar mal, porque el prompt le muestra
el catálogo sin precios, solo con referencias. Tampoco puede inventar una:
la guardia rechaza cualquier precio escrito a mano (`app/core/guards.py`).

"Lo que ve la persona sale del sistema, no del modelo" (notas de la clase).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.schemas import CatalogEntry

# Tolerante a espacios y mayúsculas: {{ Precio : Tinte-Completo }}.
REFERENCIA = re.compile(r"\{\{\s*precio\s*:\s*([^}]+?)\s*\}\}", re.IGNORECASE)


def price_key(name: str) -> str:
    """'Tinte completo' -> 'tinte-completo'. Sin tildes ni símbolos."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", name.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", "-", sin_tildes).strip("-")


def keys_for(catalog: list[CatalogEntry]) -> dict[str, CatalogEntry]:
    """Clave estable por ítem. Si dos nombres dan la misma clave, se numeran."""
    claves: dict[str, CatalogEntry] = {}
    for item in catalog:
        base = price_key(item.name) or "item"
        clave, n = base, 2
        while clave in claves:
            clave, n = f"{base}-{n}", n + 1
        claves[clave] = item
    return claves


def format_price(price: int, currency: str = "COP") -> str:
    """90000 -> '$90.000' (formato colombiano)."""
    cifra = f"{price:,}".replace(",", ".")
    return f"${cifra}" if currency == "COP" else f"{cifra} {currency}"


def prompt_section(catalog: list[CatalogEntry]) -> str:
    """Lo que ve el modelo: nombres y referencias, SIN precios."""
    lineas = [f"- {item.name}: {{{{precio:{clave}}}}}" for clave, item in keys_for(catalog).items()]
    return "\n".join(lineas)


def verification_lines(catalog: list[CatalogEntry]) -> str:
    """'Nombre: $precio' por ítem: la fuente de verdad para la guardia."""
    return "\n".join(f"{item.name}: {format_price(item.price, item.currency)}" for item in catalog)


@dataclass(frozen=True)
class Rendered:
    text: str
    unknown_refs: tuple[str, ...]


def render_prices(text: str, catalog: list[CatalogEntry]) -> Rendered:
    """Reemplaza cada referencia por su precio. Las desconocidas se reportan."""
    claves = keys_for(catalog)
    desconocidas: list[str] = []

    def _reemplazar(m: re.Match[str]) -> str:
        clave = price_key(m.group(1))
        item = claves.get(clave)
        if item is None:
            desconocidas.append(m.group(1))
            return m.group(0)
        return format_price(item.price, item.currency)

    return Rendered(REFERENCIA.sub(_reemplazar, text), tuple(desconocidas))


def strip_references(text: str) -> str:
    """El texto sin referencias: lo que queda es lo que el modelo escribió a mano."""
    return REFERENCIA.sub(" ", text)


__all__ = [
    "REFERENCIA",
    "Rendered",
    "format_price",
    "keys_for",
    "price_key",
    "prompt_section",
    "render_prices",
    "strip_references",
    "verification_lines",
]
