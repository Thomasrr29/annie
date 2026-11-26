"""
Bandejas: solicitudes capturadas y escalamientos (RF-21, RF-14).

Los escalamientos se responden en la pagina Conversaciones, que muestra el
historial completo: cada tarjeta lleva ahi con la conversacion ya abierta.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from sesion import mostrar_error, requerir_api

ESTADOS = ["new", "seen", "in_progress", "done"]
ETIQUETAS = {
    "new": "Nueva",
    "seen": "Vista",
    "in_progress": "En curso",
    "done": "Resuelta",
}

st.set_page_config(page_title="Bandejas", page_icon="📥", layout="wide")
st.title("Bandejas")

api = requerir_api()

tab_solicitudes, tab_escalamientos = st.tabs(["Solicitudes", "Escalamientos"])

# ----------------------------------------------------------------------
# Solicitudes capturadas
# ----------------------------------------------------------------------
with tab_solicitudes:
    filtro = st.selectbox(
        "Estado",
        options=["(todas)"] + ESTADOS,
        format_func=lambda e: ETIQUETAS.get(e, e),
    )

    try:
        solicitudes = api.list_requests(estado=None if filtro == "(todas)" else filtro)
    except ApiError as exc:
        mostrar_error(exc)
        st.stop()

    if not solicitudes:
        st.info("No hay solicitudes con ese filtro.")
    else:
        st.caption(f"{len(solicitudes)} solicitud(es)")
        for solicitud in solicitudes:
            req_id = str(solicitud.get("id"))
            creado = str(solicitud.get("created_at", ""))[:16].replace("T", " ")
            estado_actual = solicitud.get("status", "new")

            with st.expander(
                f"**{solicitud.get('request_type', 'solicitud')}** · "
                f"{ETIQUETAS.get(estado_actual, estado_actual)} · {creado}"
            ):
                st.json(solicitud.get("structured_data") or {})
                st.caption(f"Conversación: {solicitud.get('conversation_id') or '—'}")

                col1, col2 = st.columns([2, 1])
                nuevo = col1.selectbox(
                    "Cambiar estado",
                    options=ESTADOS,
                    index=ESTADOS.index(estado_actual) if estado_actual in ESTADOS else 0,
                    format_func=lambda e: ETIQUETAS.get(e, e),
                    key=f"estado_{req_id}",
                )
                if col2.button("Actualizar", key=f"upd_{req_id}"):
                    try:
                        api.update_request_status(req_id, nuevo)
                    except ApiError as exc:
                        mostrar_error(exc)
                    else:
                        st.success("Estado actualizado.")
                        st.rerun()

# ----------------------------------------------------------------------
# Escalamientos
# ----------------------------------------------------------------------
with tab_escalamientos:
    try:
        escalamientos = api.list_escalations()
    except ApiError as exc:
        mostrar_error(exc)
        st.stop()

    if not escalamientos:
        st.success("No hay escalamientos abiertos.")
    else:
        st.caption(
            f"{len(escalamientos)} conversación(es) esperando a una persona. "
            "Mientras estén escaladas, el agente no responde."
        )

        for esc in escalamientos:
            esc_id = str(esc.get("id"))
            conv_id = str(esc.get("conversation_id"))
            creado = str(esc.get("created_at", ""))[:16].replace("T", " ")

            titulo = f"**{esc.get('reason') or 'Escalamiento'}** · {creado}"
            with st.expander(titulo, expanded=True):
                st.caption(f"Conversación: {conv_id}")

                # Se responde desde Conversaciones, con el historial completo a
                # la vista: responder a ciegas desde aca era el problema.
                volver_auto = st.checkbox(
                    "Al cerrar, devolver la conversación al agente",
                    key=f"auto_{esc_id}",
                )

                col1, col2 = st.columns(2)
                if col1.button(
                    "💬 Abrir conversación", key=f"abrir_{esc_id}", type="primary"
                ):
                    st.session_state["conversacion_seleccionada"] = conv_id
                    st.switch_page("pages/0_Conversaciones.py")

                if col2.button("Cerrar escalamiento", key=f"cerrar_{esc_id}"):
                    try:
                        api.resolve_escalation(esc_id, volver_auto)
                    except ApiError as exc:
                        mostrar_error(exc)
                    else:
                        st.success(
                            "Escalamiento cerrado."
                            + (" La conversación vuelve al agente." if volver_auto else "")
                        )
                        st.rerun()
