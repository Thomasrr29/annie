"""
Metricas de uso y costo (RF-22, RNF-02).

La metrica que hay que mirar cuando algo se pone caro es `cache_hit_rate`: si
cae sostenidamente por debajo del 60%, casi seguro se colo algo dinamico en el
prefijo del prompt y cada turno se esta pagando entero.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from api_client import ApiError
from sesion import mostrar_error, requerir_api

st.set_page_config(page_title="Métricas", page_icon="📊", layout="wide")
st.title("Métricas")

api = requerir_api()

dias = st.selectbox("Período", options=[7, 14, 30, 90], format_func=lambda d: f"Últimos {d} días")

try:
    metricas = api.metrics(dias=dias)
    costos = api.cost_dashboard(dias=dias)
except ApiError as exc:
    mostrar_error(exc)
    st.stop()

# ----------------------------------------------------------------------
# Resumen
# ----------------------------------------------------------------------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Conversaciones", metricas.get("conversaciones", 0))
col2.metric("Turnos", metricas.get("turnos", 0))

sin_humano = metricas.get("porcentaje_sin_humano")
col3.metric("Resueltas sin humano", f"{sin_humano}%" if sin_humano is not None else "—")

por_conv = metricas.get("costo_usd_por_conversacion")
col4.metric("Costo por conversación", f"US$ {por_conv:.4f}" if por_conv else "—")

col5, col6, col7 = st.columns(3)
col5.metric("Escaladas a humano", metricas.get("conversaciones_escaladas", 0))
col6.metric("Costo total", f"US$ {metricas.get('costo_usd_total', 0):.4f}")
latencia = metricas.get("latencia_ms_media")
col7.metric("Latencia media", f"{latencia} ms" if latencia else "—")

# ----------------------------------------------------------------------
# Alertas de caché
# ----------------------------------------------------------------------
for alerta in costos.get("alertas", []):
    st.warning(alerta)

st.divider()

# ----------------------------------------------------------------------
# Turnos por intención (los 7 flujos)
# ----------------------------------------------------------------------
por_intencion = metricas.get("turnos_por_intencion") or {}
if por_intencion:
    st.subheader("Turnos por intención")
    st.bar_chart(pd.Series(por_intencion, name="turnos"))

# ----------------------------------------------------------------------
# Evolución diaria
# ----------------------------------------------------------------------
dias_datos = costos.get("dias") or []

if not dias_datos:
    st.info("Todavía no hay turnos registrados en este período.")
    st.stop()

tabla = pd.DataFrame(dias_datos).set_index("dia").sort_index()

st.subheader("Costo diario")
st.line_chart(tabla[["costo_usd"]])

st.subheader("Aprovechamiento de caché")
st.caption("Proporción de tokens de entrada que se leyeron de caché en vez de pagarse completos.")
st.line_chart(tabla[["cache_hit_rate"]])

st.subheader("Latencia (p50 / p95)")
st.line_chart(tabla[["latencia_p50_ms", "latencia_p95_ms"]])

with st.expander("Detalle por día"):
    st.dataframe(
        tabla.rename(
            columns={
                "turnos": "Turnos",
                "conversaciones": "Conversaciones",
                "input_tokens": "Tokens entrada",
                "output_tokens": "Tokens salida",
                "cache_read_tokens": "Tokens de caché",
                "cache_hit_rate": "Tasa de caché",
                "costo_usd": "Costo US$",
                "costo_usd_por_conversacion": "US$/conversación",
                "latencia_p50_ms": "p50 ms",
                "latencia_p95_ms": "p95 ms",
            }
        ),
        use_container_width=True,
    )
