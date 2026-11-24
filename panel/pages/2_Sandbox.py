"""
Sandbox: probar el agente sin tocar WhatsApp (RF-23, HU-ADM-05).

Corre el MISMO orquestador que produccion sobre el canal 'sandbox', asi que lo
que se ve aca es lo que va a pasar de verdad. Como es el mismo loop, la prueba
ESCRIBE datos reales: crea cliente, conversacion, mensajes y `turn_logs`, y por
lo tanto cuenta en las metricas y consume tokens de la API.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import ApiError
from sesion import mostrar_error, requerir_api

st.set_page_config(page_title="Sandbox", page_icon="🧪", layout="wide")
st.title("Probar el agente")

api = requerir_api()

st.caption(
    "Conversación real contra el mismo orquestador, sin enviar nada por WhatsApp. "
    "Los turnos se registran y suman a las métricas y al costo."
)

identificador = st.text_input(
    "Cliente de prueba",
    value="sandbox-tester",
    help="Identificador ficticio. Cambialo para arrancar una conversación desde cero.",
)

if "sandbox_historial" not in st.session_state:
    st.session_state["sandbox_historial"] = []

historial: list[dict[str, Any]] = st.session_state["sandbox_historial"]

col_a, col_b = st.columns([1, 5])
if col_a.button("Limpiar pantalla"):
    st.session_state["sandbox_historial"] = []
    st.rerun()
col_b.caption(
    "Limpiar solo borra lo que ves. La conversación sigue viva en la base: "
    "para empezar de nuevo de verdad, cambiá el identificador del cliente."
)

st.divider()

for turno in historial:
    with st.chat_message("user"):
        st.write(turno["pregunta"])
    with st.chat_message("assistant"):
        traza = turno["traza"]
        if traza.get("replied", True):
            st.write(turno["respuesta"])
        else:
            # El agente calló a propósito (p. ej. modo humano): no es un error.
            st.warning(
                "El agente no respondió. "
                + (traza.get("no_reply_reason") or "")
            )
        detalle = [
            f"intención: **{traza.get('detected_intent') or '—'}**",
            f"agente: **{traza.get('routed_agent') or '—'}**",
        ]
        if traza.get("tools_used"):
            detalle.append(f"herramientas: **{', '.join(traza['tools_used'])}**")
        st.caption(" · ".join(detalle))

        with st.expander("Detalle del turno"):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Tokens entrada", traza.get("input_tokens", 0))
            c2.metric("Tokens salida", traza.get("output_tokens", 0))
            c3.metric("Leídos de caché", traza.get("cache_read_tokens", 0))
            c4.metric(
                "Latencia",
                f"{traza['latency_ms']} ms" if traza.get("latency_ms") else "—",
            )
            costo = traza.get("cost_estimate_usd")
            if costo:
                st.caption(f"Costo estimado del turno: US$ {costo:.6f}")

            entrada = traza.get("input_tokens", 0) + traza.get("cache_read_tokens", 0)
            if entrada:
                tasa = traza.get("cache_read_tokens", 0) / entrada
                st.caption(f"Caché: {tasa:.0%} de los tokens de entrada")

mensaje = st.chat_input("Escribí como si fueras un cliente...")

if mensaje:
    with st.spinner("El agente está pensando..."):
        try:
            respuesta = api.sandbox_message(mensaje, identificador.strip() or "sandbox-tester")
        except ApiError as exc:
            mostrar_error(exc)
        else:
            historial.append(
                {
                    "pregunta": mensaje,
                    "respuesta": respuesta.get("reply_text") or "(sin respuesta)",
                    "traza": respuesta,
                }
            )
            st.session_state["sandbox_historial"] = historial
            st.rerun()

if not historial:
    st.info(
        "Probá con preguntas reales de tus clientes: horarios, precios, "
        "«quiero agendar un turno», «necesito hablar con alguien»."
    )
