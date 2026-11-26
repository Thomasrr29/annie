"""
Sandbox: probar el agente sin tocar WhatsApp (RF-23, HU-ADM-05).

Corre el MISMO orquestador que produccion sobre el canal 'sandbox', asi que lo
que se ve aca es lo que va a pasar de verdad. Como es el mismo loop, la prueba
ESCRIBE datos reales: crea cliente, conversacion, mensajes y `turn_logs`, y por
lo tanto cuenta en las metricas y consume tokens de la API.

La pagina funciona como el CELULAR DEL CLIENTE: muestra el hilo real de la
conversacion guardada (cliente, agente y equipo), no solo lo que se escribio
aqui. Asi, si alguien del equipo responde desde Conversaciones, la respuesta
aparece aca, igual que le llegaria al cliente por WhatsApp.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import AdminAPI, ApiError
from sesion import mostrar_error, requerir_api

st.set_page_config(page_title="Sandbox", page_icon="🧪", layout="wide")
st.title("Probar el agente")

api = requerir_api()

# El hilo se vuelve a leer solo cada pocos segundos: asi aparecen las
# respuestas del equipo sin tener que hacer nada.
REFRESCO_HILO = 5

TRAZAS_KEY = "sandbox_trazas"  # id del mensaje del agente -> traza del turno
CONVERSACIONES_KEY = "sandbox_conversaciones"  # identificador -> conversation_id
AVISO_KEY = "sandbox_aviso"  # el último "el agente no respondió" (se muestra una vez)

AVATARES = {"customer": "🙂", "agent": "🤖", "human": "🧑‍💼", "system": "⚙️"}
AUTORES = {"customer": "Vos (cliente)", "agent": "annie", "human": "Equipo", "system": "Sistema"}

st.caption(
    "Conversación real contra el mismo orquestador, sin enviar nada por WhatsApp. "
    "Esta pantalla es el celular del cliente: muestra también lo que responda el "
    "equipo desde Conversaciones. Los turnos suman a las métricas y al costo."
)

identificador = (
    st.text_input(
        "Cliente de prueba",
        value="sandbox-tester",
        help="Identificador ficticio. Cambialo para arrancar una conversación desde cero.",
    ).strip()
    or "sandbox-tester"
)

trazas: dict[str, dict[str, Any]] = st.session_state.setdefault(TRAZAS_KEY, {})
conversaciones: dict[str, str] = st.session_state.setdefault(CONVERSACIONES_KEY, {})


def _conversacion_de(cliente_api: AdminAPI, ident: str) -> str | None:
    """La conversación de este cliente de prueba, si ya existe en la base."""
    if ident in conversaciones:
        return conversaciones[ident]
    try:
        encontradas = cliente_api.list_conversations(busqueda=ident)
    except ApiError:
        return None
    for conv in encontradas:
        cliente = conv.get("customer") or {}
        if conv.get("channel") == "sandbox" and cliente.get("channel_identifier") == ident:
            conversaciones[ident] = str(conv["id"])
            return conversaciones[ident]
    return None


def _traza(traza: dict[str, Any]) -> None:
    """Intención, agente, herramientas y costo del turno que produjo este mensaje."""
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
        c4.metric("Latencia", f"{traza['latency_ms']} ms" if traza.get("latency_ms") else "—")
        costo = traza.get("cost_estimate_usd")
        if costo:
            st.caption(f"Costo estimado del turno: US$ {costo:.6f}")
        entrada = traza.get("input_tokens", 0) + traza.get("cache_read_tokens", 0)
        if entrada:
            st.caption(f"Caché: {traza.get('cache_read_tokens', 0) / entrada:.0%} de la entrada")


@st.fragment(run_every=REFRESCO_HILO)
def hilo(cliente_api: AdminAPI, ident: str) -> None:
    conversation_id = _conversacion_de(cliente_api, ident)
    if conversation_id is None:
        st.info(
            "Probá con preguntas reales de tus clientes: horarios, precios, "
            "«quiero agendar un turno», «necesito hablar con alguien»."
        )
        return

    try:
        detalle = cliente_api.get_conversation(conversation_id)
    except ApiError as exc:
        mostrar_error(exc)
        return

    if detalle.get("status") == "human":
        st.info(
            "🧑‍💼 Esta conversación la atiende el **equipo**: annie no responde hasta "
            "que la devuelvan al agente desde **Conversaciones**."
        )

    for m in detalle.get("messages") or []:
        rol = str(m.get("role"))
        with st.chat_message("user" if rol == "customer" else "assistant",
                             avatar=AVATARES.get(rol, "💬")):
            st.caption(f"**{AUTORES.get(rol, rol)}**")
            if m.get("content_type") == "audio":
                st.caption("🎤 Nota de voz")
            if m.get("content"):
                st.write(m["content"])
            traza = trazas.get(str(m.get("id")))
            if traza is not None:
                _traza(traza)


hilo(api, identificador)

aviso = st.session_state.pop(AVISO_KEY, None)
if aviso:
    st.warning(f"El agente no respondió este mensaje. {aviso}")

mensaje = st.chat_input("Escribí como si fueras un cliente...")

if mensaje:
    with st.spinner("annie está leyendo… (espera unos 10 s, por si seguís escribiendo)"):
        try:
            respuesta = api.sandbox_message(mensaje, identificador)
        except ApiError as exc:
            mostrar_error(exc)
        else:
            conversaciones[identificador] = str(respuesta["conversation_id"])
            if respuesta.get("reply_message_id"):
                trazas[str(respuesta["reply_message_id"])] = respuesta
            if not respuesta.get("replied", True):
                st.session_state[AVISO_KEY] = respuesta.get("no_reply_reason") or ""
            st.rerun()
