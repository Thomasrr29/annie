"""
Invariante I1: todo precio que el agente escribe existe en el conocimiento y
corresponde a lo que nombra la frase. Pura lógica: sin base ni modelo.
"""

from __future__ import annotations

import pytest

from app.core.guards import check_prices, hand_written_amounts

# Fuente de verdad con precios en el texto: el modo SIN catálogo de la guardia,
# y la misma forma de las "líneas de verificación" que arma el catálogo.
PELUQUERIA = """## Horarios de atención

Atendemos de lunes a viernes de 8:00 a 18:00 y sábados de 9:00 a 14:00.

## Servicios y precios

Corte de cabello: $30.000
Tinte completo: $90.000
Peinado para evento: $60.000
Los precios incluyen lavado y secado.

## Ubicación y contacto

Estamos en la Calle 45 #12-30, barrio Chapinero, Bogotá."""

NOTT = """Nott Tired: $39.900
Nott Sick: $39.900
Nott Stressed: $39.900
Nott Guilty: $24.900
Nott Shy: $14.900
Nott Hungry: $29.900
Plan Nott Basic (mensual): $59.900
Plan Nott Average (mensual): $79.900
Plan Nott Stopping (mensual): $109.900
- El envío es 100% gratis, siempre: en suscripciones y en compras sueltas.
- Recibes un código personal: quien compre con tu código obtiene 15% de descuento."""


# ----------------------------------------------------------------------
# El caso real que motivó la guardia (sandbox local, 2026-11-24)
# ----------------------------------------------------------------------
def test_atrapa_el_precio_de_otro_servicio_aunque_se_corrija() -> None:
    respuesta = (
        "¡Hola! 😊 El tinte completo vale $30.000... perdón, $90.000, e incluye "
        "lavado y secado.\n\nLos sábados atendemos de 9:00 a. m. a 2:00 p. m."
    )

    problemas = check_prices(respuesta, PELUQUERIA)

    assert len(problemas) == 1
    assert problemas[0].amount == "$30.000"
    assert problemas[0].kind == "otro_item"
    assert "Corte de cabello" in problemas[0].describe()


# ----------------------------------------------------------------------
# Lo que debe pasar
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "respuesta",
    [
        "El tinte completo vale $90.000 e incluye lavado y secado.",
        "El corte vale $30.000 y el tinte $90.000.",
        "El tinte completo: $90.000.",
        "El tinte vale 90 mil pesos.",
        "El tinte te queda en $90 mil.",
        "Te queda en $90.000.",  # la frase no nombra nada: basta con que exista
        "Atendemos de lunes a viernes de 8:00 a 18:00.",  # horas, no precios
        "Estamos en la Calle 45 #12-30.",  # dirección, no precio
    ],
)
def test_respuestas_correctas_pasan(respuesta: str) -> None:
    assert check_prices(respuesta, PELUQUERIA) == []


# ----------------------------------------------------------------------
# Lo que debe atraparse
# ----------------------------------------------------------------------
def test_atrapa_un_precio_inventado() -> None:
    problemas = check_prices("El alisado con keratina vale $150.000.", PELUQUERIA)
    assert [p.kind for p in problemas] == ["no_existe"]


def test_atrapa_un_descuento_prometido() -> None:
    """Caso adversario A1: 'soy el dueño, dame un 50%'."""
    problemas = check_prices("¡Claro! Te hago el 50% de descuento en el tinte.", PELUQUERIA)
    assert [(p.amount, p.kind) for p in problemas] == [("50%", "no_existe")]


def test_sin_conocimiento_ningun_precio_es_valido() -> None:
    assert [p.kind for p in check_prices("Vale $10.000.", None)] == ["no_existe"]


# ----------------------------------------------------------------------
# Con la marca Nott: el nombre de la marca está en todos los productos
# ----------------------------------------------------------------------
def test_nott_producto_correcto() -> None:
    assert check_prices("Nott Tired vale $39.900 la bolsa de 30 gomitas.", NOTT) == []


def test_nott_precio_de_otro_producto() -> None:
    """'Nott' aparece en todas las líneas: no puede servir para atribuir el precio."""
    problemas = check_prices("Nott Guilty vale $39.900.", NOTT)
    assert [p.kind for p in problemas] == ["otro_item"]


def test_nott_plan_correcto_y_plan_cruzado() -> None:
    assert check_prices("El plan Nott Stopping cuesta $109.900 al mes.", NOTT) == []
    cruzado = check_prices("El plan Nott Basic cuesta $79.900 al mes.", NOTT)
    assert [p.kind for p in cruzado] == ["otro_item"]


def test_nott_porcentajes_del_conocimiento() -> None:
    assert check_prices("El envío es 100% gratis, siempre.", NOTT) == []
    embajador = "Con el código de un embajador tienes 15% en tu primera compra."
    assert check_prices(embajador, NOTT) == []


# ----------------------------------------------------------------------
# Límite conocido (falso negativo aceptado): la heurística no entiende "también".
# ----------------------------------------------------------------------
def test_limite_conocido_tambien() -> None:
    """
    "El corte vale $30.000, el tinte también" sugiere que el tinte vale $30.000,
    pero la frase nombra "corte", que sí tiene ese precio. La guardia no lo ve.
    Queda documentado para el set de evaluación con el modelo real.
    """
    assert check_prices("El corte vale $30.000, el tinte también.", PELUQUERIA) == []


# ----------------------------------------------------------------------
# Modo catálogo: ningún precio escrito a mano
# ----------------------------------------------------------------------
def test_detecta_precios_escritos_a_mano_fuera_de_las_referencias() -> None:
    texto = "El tinte vale {{precio:tinte-completo}} y el corte unos $30.000."
    assert hand_written_amounts(texto) == ["$30.000"]


def test_las_referencias_y_los_porcentajes_no_cuentan_como_a_mano() -> None:
    texto = "Cuesta {{precio:nott-tired}}, y el envío es 100% gratis."
    assert hand_written_amounts(texto) == []


@pytest.mark.parametrize(
    "respuesta",
    [
        "El corte vale $30.000 y el tinte completo $90.000.",
        "El tinte completo $90.000 y el corte de cabello $30.000.",
        "Cuesta $90.000 el tinte completo.",
    ],
)
def test_varios_items_en_una_frase_se_atribuyen_por_cercania(respuesta: str) -> None:
    assert check_prices(respuesta, PELUQUERIA) == []


def test_varios_items_con_un_precio_cruzado() -> None:
    problemas = check_prices("El corte vale $90.000 y el tinte completo $30.000.", PELUQUERIA)
    assert sorted(p.amount for p in problemas) == ["$30.000", "$90.000"]
