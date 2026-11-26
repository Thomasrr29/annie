"""
Panel de administracion del negocio (Streamlit).

Cliente de la API admin (RF-18..RF-23). Se levanta aparte de la API:

    pip install -e '.[panel]'
    streamlit run panel/app.py

Ojo: Streamlit no es multi-usuario ni tiene sesiones reales. Es la herramienta
del MVP para el onboarding del negocio piloto (fase 7), donde el operador es una
persona con un token. Un panel publico multi-tenant necesita otra cosa.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from sesion import barra_lateral, mostrar_error

st.set_page_config(page_title="Panel del agente", page_icon="🤖", layout="wide")

st.title("Panel del agente de soporte")

api = barra_lateral()

if api is None:
    st.info(
        "Ingresá la URL de la API y tu token de acceso en la barra lateral para empezar."
    )
    st.markdown(
        """
### Primeros pasos

1. Levantá la base: `docker compose up -d`
2. Levantá la API: `uvicorn app.main:app --reload`
3. Generá un token:
   `python -m scripts.crear_token_admin --business-id <uuid> --email vos@negocio.com`
4. Pegalo en la barra lateral.

### Qué hay en cada página

| Página | Para qué |
|---|---|
| **Conversaciones** | Ver el historial completo y responder como persona (texto, emojis, fotos) |
| **Conocimiento** | Cargar lo que el agente sabe del negocio |
| **Sandbox** | Probar al agente sin tocar WhatsApp |
| **Agentes** | Crear y configurar agentes |
| **Bandejas** | Solicitudes capturadas y escalamientos (que se responden en Conversaciones) |
| **Métricas** | Volumen, resolución sin humano y costo |
| **Negocio** | Datos, notificaciones y privacidad |
"""
    )
    st.stop()

# ----------------------------------------------------------------------
# Resumen: lo minimo para saber si el agente esta listo para responder
# ----------------------------------------------------------------------
try:
    negocio = api.get_business()
    agentes = api.list_agents()
    documentos = api.list_knowledge()
    metricas = api.metrics(dias=7)
except ApiError as exc:
    mostrar_error(exc)
    st.stop()

st.subheader(negocio.get("name", "Sin nombre"))

col1, col2, col3, col4 = st.columns(4)
col1.metric("Agentes activos", len(agentes))
col2.metric("Documentos de conocimiento", len(documentos))
col3.metric("Conversaciones (7 días)", metricas.get("conversaciones", 0))

sin_humano = metricas.get("porcentaje_sin_humano")
col4.metric(
    "Resueltas sin humano",
    f"{sin_humano}%" if sin_humano is not None else "—",
)

# Un agente sin conocimiento responde "no sé" a todo: vale avisarlo temprano.
if not agentes:
    st.warning(
        "No hay agentes activos: el negocio todavía no puede responder. "
        "Creá uno en **Agentes**."
    )
if not documentos:
    st.warning(
        "No hay conocimiento cargado: el agente no va a poder responder preguntas del negocio. "
        "Cargalo en **Conocimiento**."
    )

costo = metricas.get("costo_usd_por_conversacion")
if costo:
    st.caption(f"Costo por conversación (7 días): US$ {costo:.4f}")

st.divider()
st.caption("Elegí una página en la barra lateral.")
