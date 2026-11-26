"""
Invariante I8: annie no afirma haber pasado el caso, avisado, registrado o
agendado si no lo hizo. Esto prueba el detector (lógica pura, sin base ni modelo);
el respaldo con herramientas se prueba en test_orquestador.py.
"""

from __future__ import annotations

import pytest

from app.core.guards import unbacked_promises


@pytest.mark.parametrize(
    "respuesta",
    [
        # Las dos frases reales de la prueba del 2026-11-25 (el Informador, sin herramientas):
        "¡Perfecto! 🙌 Le paso tu solicitud a una persona del salón: 3 personas, el sábado.",
        "Te paso con una persona del salón para que te confirme los cupos.",
        "Listo, le avisé a una persona del negocio para que siga tu caso.",
        "Dejé registrada tu solicitud de tinte completo.",
        "Ya agendé tu cita para el viernes.",
        "Tu cita quedó agendada.",
    ],
)
def test_afirmaciones_de_una_accion_hecha(respuesta: str) -> None:
    assert unbacked_promises(respuesta), f"no detectó la promesa: {respuesta!r}"


@pytest.mark.parametrize(
    "respuesta",
    [
        # Ofrecer no es prometer: todavía no se hizo nada.
        "¿Quieres que te pase con una persona del salón?",
        "Si quieres, le paso tu solicitud a una persona.",
        "Puedo pasarte con una persona del equipo.",
        # Información, no acciones.
        "Las citas se confirman el día anterior por WhatsApp.",
        "El corte de cabello cuesta $30.000.",
        "Atendemos de lunes a viernes de 8:00 a 18:00.",
    ],
)
def test_ofertas_e_informacion_no_son_promesas(respuesta: str) -> None:
    assert unbacked_promises(respuesta) == []


def test_devuelve_solo_la_frase_que_promete() -> None:
    texto = "Los sábados atendemos de 9 a 14. Le paso tu solicitud al equipo. ¿Algo más?"
    assert unbacked_promises(texto) == ["Le paso tu solicitud al equipo."]
