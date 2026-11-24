"""
Guardia de precios: invariante I1 (estudio/05_fallas_invariantes.md).

> Todo precio o porcentaje que el agente escribe existe en la base de
> conocimiento Y corresponde a lo que el cliente está viendo.

El prompt dice "nunca inventes precios", pero el prompt no protege al 100%. En
una prueba real el modelo escribió "El tinte completo vale $30.000... perdón,
$90.000": $30.000 existe en el conocimiento, pero es el precio del CORTE. Por
eso la guardia revisa dos cosas, en código y sin llamar a ningún modelo:

1. **Existencia:** cada monto o porcentaje de la respuesta aparece en el
   conocimiento. Atrapa precios y descuentos inventados ("te hago el 50%").
2. **Atribución:** si la frase donde aparece el monto nombra algo que tiene
   precio en el conocimiento ("tinte"), el monto debe estar en una línea que
   nombre eso mismo. Atrapa el precio de otro servicio.

Es una heurística deliberadamente simple (palabras compartidas, no NLP). Sus
límites conocidos están en los tests. Ante la duda NO bloquea: si la frase no
nombra nada con precio, solo se exige la existencia del monto.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from app.core.catalog import strip_references

# Montos con señal de dinero: "$90.000", "$ 90.000", "90.000 pesos", "90 mil",
# "COP 90.000". Un número suelto ("30 gomitas", "9:00", "40 mg") NO es un precio.
_MONTO = re.compile(
    r"""
    (?:\$|cop\s*)\s*(?P<a>\d{1,3}(?:[.,]\d{3})+|\d+)(?![\d.,]*\s*mil\b)  # $90.000 · COP 90.000
    | (?P<b>\d{1,3}(?:[.,]\d{3})+|\d+)\s*(?:pesos|cop)\b                 # 90.000 pesos
    | (?P<c>\d+(?:[.,]\d+)?)\s*mil\b                                     # 90 mil · $90 mil
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PORCENTAJE = re.compile(r"(?P<p>\d+(?:[.,]\d+)?)\s*%")

# Fin de frase: puntuación seguida de espacio (no parte "$90.000") o salto de línea.
# Los dos puntos NO cortan: "Tinte completo: $90.000" es una sola idea.
_FIN_DE_FRASE = re.compile(r"(?<=[.!?;])\s+|\n+")
_PALABRA = re.compile(r"[a-z0-9ñ]+")

# Palabras que no identifican un servicio o producto.
_VACIAS = frozenset(
    """
    a al algo alla ante asi aun bien bolsa cada como con contra cual cuanto cuesta cuestan
    de del desde donde el ella ellos en entre era es esa ese eso esta este esto estos
    hay hasta incluye incluyen iva la las le les lo los mas me mes mi mientras mucho muy
    nada ni no nos nuestro nuestra o otra otro para pero perdon poco por porque precio
    precios que quien se ser si sin sobre solo su sus tambien te tiene tienen todo todos
    tu un una uno unos valor vale valen y ya yo usted ustedes hola gracias claro listo
    pesos cop mil total cada desde hasta aproximadamente
    """.split()  # noqa: SIM905 - más legible que una lista de 100 cadenas
)


@dataclass(frozen=True)
class PriceIssue:
    """Un monto de la respuesta que viola la invariante I1."""

    amount: str  # tal como lo escribió el modelo
    kind: str  # "no_existe" | "otro_item"
    sentence: str  # la frase de la respuesta donde aparece
    knowledge_lines: tuple[str, ...]  # dónde aparece ese monto en el conocimiento

    def describe(self) -> str:
        if self.kind == "no_existe":
            return f"«{self.amount}» no aparece en la información del negocio."
        donde = " | ".join(self.knowledge_lines)
        return (
            f"«{self.amount}» corresponde a «{donde}» en la información del negocio, "
            f"no a lo que dice la frase «{self.sentence.strip()}»."
        )


def _sin_tildes(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )


def _normalizar_monto(match: re.Match[str]) -> str:
    """'$90.000', '90000 pesos' y '90 mil' -> '90000'."""
    if match.group("c") is not None:
        valor = float(match.group("c").replace(",", "."))
        return str(round(valor * 1000))
    crudo = match.group("a") or match.group("b") or ""
    return re.sub(r"[.,]", "", crudo)


def _montos_con_posicion(texto: str) -> list[tuple[str, str, int, int]]:
    """(normalizado, como_aparece, inicio, fin) de cada monto y porcentaje, en orden."""
    encontrados = [
        (_normalizar_monto(m), m.group(0).strip(), m.start(), m.end())
        for m in _MONTO.finditer(texto)
    ]
    encontrados += [
        (m.group("p").replace(",", ".") + "%", m.group(0).strip(), m.start(), m.end())
        for m in _PORCENTAJE.finditer(texto)
    ]
    return sorted(encontrados, key=lambda x: x[2])


def _montos(texto: str) -> list[tuple[str, str]]:
    """(normalizado, como_aparece) de cada monto y porcentaje del texto."""
    return [(n, crudo) for n, crudo, _, _ in _montos_con_posicion(texto)]


def _raices(texto: str) -> set[str]:
    """Raíces de las palabras con significado (5 letras: 'tintes' y 'tinte' coinciden)."""
    return {
        palabra[:5]
        for palabra in _PALABRA.findall(_sin_tildes(texto))
        if len(palabra) >= 4 and palabra not in _VACIAS and not palabra.isdigit()
    }


def check_prices(reply: str, knowledge: str | None) -> list[PriceIssue]:
    """Revisa la invariante I1 sobre una respuesta. Lista vacía = la respuesta pasa."""
    montos_respuesta = _montos(reply)
    if not montos_respuesta:
        return []

    lineas = [linea.strip() for linea in (knowledge or "").splitlines() if linea.strip()]

    # Monto normalizado -> líneas del conocimiento donde aparece.
    lineas_por_monto: dict[str, list[str]] = {}
    for linea in lineas:
        for normalizado, _ in _montos(linea):
            lineas_por_monto.setdefault(normalizado, []).append(linea)

    # "Ítems con precio": palabras de las líneas que tienen montos. Se descartan
    # las que están en más de la mitad de esas líneas (p. ej. el nombre de la
    # marca en todos los productos): no distinguen un ítem de otro.
    lineas_con_precio = {linea for grupo in lineas_por_monto.values() for linea in grupo}
    frecuencia = Counter(r for linea in lineas_con_precio for r in _raices(linea))
    umbral = max(1, len(lineas_con_precio) // 2) if len(lineas_con_precio) >= 3 else None
    items = {r for r, n in frecuencia.items() if umbral is None or n <= umbral}

    raices_por_linea = {linea: _raices(linea) & items for linea in lineas_con_precio}

    def _puntaje(mencionados: set[str], linea: str) -> float:
        # Una palabra que está en un solo renglón ("basic") pesa más que una que
        # está en varios ("plan"): así gana el ítem más específico que se nombra.
        return sum(1 / frecuencia[r] for r in mencionados & raices_por_linea[linea])

    problemas: list[PriceIssue] = []
    for frase in _FIN_DE_FRASE.split(reply):
        montos = _montos_con_posicion(frase)
        for i, (normalizado, como_aparece, inicio, fin) in enumerate(montos):
            propias = lineas_por_monto.get(normalizado)
            if not propias:
                problemas.append(PriceIssue(como_aparece, "no_existe", frase, ()))
                continue
            # A quién se le atribuye el monto: lo que se nombra justo ANTES de él
            # (desde el monto anterior). "El corte vale $30.000 y el tinte $90.000"
            # atribuye cada precio a su servicio. Si antes no se nombra nada
            # ("cuesta $90.000 el tinte"), se mira lo que viene después.
            antes = frase[montos[i - 1][3] if i else 0 : inicio]
            despues = frase[fin : montos[i + 1][2] if i + 1 < len(montos) else len(frase)]
            mencionados = (_raices(antes) & items) or (_raices(despues) & items)
            if not mencionados:
                continue  # no nombra nada con precio: no hay a quién atribuirlo
            puntajes = {linea: _puntaje(mencionados, linea) for linea in lineas_con_precio}
            mejor = max(puntajes.values())
            if mejor == 0:
                continue  # nombra cosas, pero ninguna con precio
            mas_especificas = {linea for linea, p in puntajes.items() if p == mejor}
            if not mas_especificas & set(propias):
                problemas.append(PriceIssue(como_aparece, "otro_item", frase, tuple(propias)))
    return problemas


def hand_written_amounts(text: str) -> list[str]:
    """
    Montos de dinero escritos a mano (fuera de las referencias {{precio:...}}).

    Con catálogo, el modelo no debe escribir ninguno: todo precio sale del
    sistema. Los porcentajes no cuentan aquí (se validan contra el conocimiento).
    """
    return [m.group(0).strip() for m in _MONTO.finditer(strip_references(text))]


__all__ = ["PriceIssue", "check_prices", "hand_written_amounts"]
