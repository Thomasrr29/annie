"""
Agentes del negocio (RF-20, RF-06).

Un agente es CONFIGURACION, no codigo: darlo de alta es un INSERT y el
orquestador lo enruta en el mensaje siguiente, sin redeploy. `routing_description`
es lo que lee el clasificador para decidir a quien mandarle cada mensaje, asi que
es el campo que mas conviene escribir bien.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from sesion import mostrar_error, requerir_api

st.set_page_config(page_title="Agentes", page_icon="🧭", layout="wide")
st.title("Agentes")

api = requerir_api()

try:
    # El catalogo viene de la API: es la misma lista contra la que valida.
    herramientas = api.list_tools()
    incluir_inactivos = st.toggle("Mostrar también los inactivos", value=False)
    agentes = api.list_agents(incluir_inactivos=incluir_inactivos)
except ApiError as exc:
    mostrar_error(exc)
    st.stop()

st.divider()

# ----------------------------------------------------------------------
# Agentes existentes
# ----------------------------------------------------------------------
if not agentes:
    st.warning("No hay agentes. Sin al menos uno activo, el negocio no puede responder.")
else:
    for agente in agentes:
        agent_id = str(agente.get("id"))
        estado = agente.get("status", "active")
        etiqueta = f"**{agente.get('name')}**"
        if estado != "active":
            etiqueta += f" · _{estado}_"

        with st.expander(etiqueta), st.form(f"form_{agent_id}"):
            nombre = st.text_input("Nombre", value=agente.get("name", ""))
            routing = st.text_area(
                "Cuándo se usa este agente",
                value=agente.get("routing_description", ""),
                height=90,
                help=(
                    "Lo lee el clasificador para enrutar. Describí los temas que "
                    "atiende, no cómo responde."
                ),
            )
            instrucciones = st.text_area(
                "Instrucciones (markdown)",
                value=agente.get("instructions_md", ""),
                height=260,
                help="Va al prefijo cacheado del prompt. Es el comportamiento del agente.",
            )
            estilo = st.text_input(
                "Estilo de respuesta",
                value=agente.get("response_style") or "",
                placeholder="Breve, cordial, tuteando",
            )

            col1, col2, col3 = st.columns(3)
            permitidas = col1.multiselect(
                "Herramientas permitidas",
                options=herramientas,
                default=[t for t in agente.get("allowed_tools", []) if t in herramientas],
            )
            memoria = col2.selectbox(
                "Memoria",
                options=["customer", "none"],
                index=0 if agente.get("memory_scope", "customer") == "customer" else 1,
            )
            nuevo_estado = col3.selectbox(
                "Estado",
                options=["active", "inactive"],
                index=0 if estado == "active" else 1,
            )

            if st.form_submit_button("Guardar cambios", type="primary"):
                try:
                    api.update_agent(
                        agent_id,
                        {
                            "name": nombre,
                            "routing_description": routing,
                            "instructions_md": instrucciones,
                            "response_style": estilo or None,
                            "allowed_tools": permitidas,
                            "memory_scope": memoria,
                            "status": nuevo_estado,
                        },
                    )
                except ApiError as exc:
                    mostrar_error(exc)
                else:
                    st.success("Agente actualizado. Aplica desde el próximo mensaje.")
                    st.rerun()

st.divider()

# ----------------------------------------------------------------------
# Nuevo agente
# ----------------------------------------------------------------------
st.header("Crear agente")

with st.form("form_nuevo_agente"):
    nombre_nuevo = st.text_input("Nombre", placeholder="Reservas")
    routing_nuevo = st.text_area(
        "Cuándo se usa este agente",
        height=90,
        placeholder="Cuando el cliente quiere reservar, reprogramar o cancelar un turno.",
    )
    instrucciones_nuevas = st.text_area(
        "Instrucciones (markdown)",
        height=240,
        placeholder=(
            "Sos quien gestiona las reservas.\n\n"
            "- Pedí fecha, hora y nombre antes de registrar nada.\n"
            "- Si falta un dato, preguntá por uno solo a la vez.\n"
            "- Cuando tengas todo, registrá la solicitud y confirmá al cliente."
        ),
    )
    estilo_nuevo = st.text_input("Estilo de respuesta", placeholder="Breve y cordial")

    c1, c2 = st.columns(2)
    herramientas_nuevas = c1.multiselect("Herramientas permitidas", options=herramientas)
    memoria_nueva = c2.selectbox("Memoria", options=["customer", "none"])

    if st.form_submit_button("Crear agente", type="primary"):
        obligatorios = (nombre_nuevo, routing_nuevo, instrucciones_nuevas)
        if not all(campo.strip() for campo in obligatorios):
            st.error("Nombre, descripción de enrutamiento e instrucciones son obligatorios.")
        else:
            try:
                api.create_agent(
                    {
                        "name": nombre_nuevo.strip(),
                        "routing_description": routing_nuevo.strip(),
                        "instructions_md": instrucciones_nuevas.strip(),
                        "allowed_tools": herramientas_nuevas,
                        "memory_scope": memoria_nueva,
                        "response_style": estilo_nuevo or None,
                    }
                )
            except ApiError as exc:
                mostrar_error(exc)
            else:
                st.success(f"Agente «{nombre_nuevo}» creado.")
                st.rerun()
