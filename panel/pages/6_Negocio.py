"""
Configuracion del negocio y privacidad (RF-18, RNF-05).

`persona_tone` viaja en el prefijo cacheado del prompt: cambiarlo invalida la
cache una vez (el turno siguiente la reconstruye) y afecta a TODOS los agentes
del negocio, no a uno solo.

La seccion de privacidad ejecuta borrados irreversibles (Ley 1581): pide escribir
el identificador dos veces antes de hacer nada.
"""

from __future__ import annotations

import json

import streamlit as st

from api_client import ApiError
from sesion import invalidar_negocio, mostrar_error, requerir_api

st.set_page_config(page_title="Negocio", page_icon="🏪", layout="wide")
st.title("Negocio")

api = requerir_api()

try:
    negocio = api.get_business()
except ApiError as exc:
    mostrar_error(exc)
    st.stop()

tab_config, tab_privacidad = st.tabs(["Configuración", "Privacidad"])

# ----------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------
with tab_config, st.form("form_negocio"):
    nombre = st.text_input("Nombre del negocio", value=negocio.get("name", ""))
    tono = st.text_area(
        "Tono del agente",
        value=negocio.get("persona_tone") or "",
        height=100,
        placeholder="Cercano y cordial, tuteando, sin tecnicismos.",
        help="Aplica a todos los agentes del negocio.",
    )

    col1, col2 = st.columns(2)
    idioma = col1.text_input("Idioma", value=negocio.get("language", "es"))
    zona = col2.text_input("Zona horaria", value=negocio.get("timezone", "America/Bogota"))

    st.markdown("**Notificaciones al dueño**")
    col3, col4 = st.columns(2)
    canal = col3.text_input(
        "Canal", value=negocio.get("notify_channel") or "", placeholder="email"
    )
    config_texto = col4.text_area(
        "Configuración (JSON)",
        value=json.dumps(negocio.get("notify_config") or {}, ensure_ascii=False, indent=2),
        height=100,
        help='Ej: {"email": "dueno@negocio.com"}',
    )

    horarios_texto = st.text_area(
        "Horarios de atención (JSON)",
        value=json.dumps(negocio.get("business_hours") or {}, ensure_ascii=False, indent=2),
        height=140,
        help='Ej: {"lun-vie": "9:00-18:00", "sab": "9:00-13:00"}',
    )

    whatsapp_id = st.text_input(
        "WhatsApp Phone Number ID",
        value=negocio.get("whatsapp_phone_number_id") or "",
        help="Es lo que conecta los mensajes entrantes con este negocio. Cambialo con cuidado.",
    )

    if st.form_submit_button("Guardar", type="primary"):
        errores = []
        try:
            notify_config = json.loads(config_texto or "{}")
        except json.JSONDecodeError as exc:
            errores.append(f"Configuración de notificaciones: JSON inválido ({exc.msg})")
            notify_config = {}
        try:
            horarios = json.loads(horarios_texto or "{}")
        except json.JSONDecodeError as exc:
            errores.append(f"Horarios: JSON inválido ({exc.msg})")
            horarios = {}

        if errores:
            for e in errores:
                st.error(e)
        else:
            cambios = {
                "name": nombre,
                "persona_tone": tono or None,
                "language": idioma,
                "timezone": zona,
                "business_hours": horarios,
                "notify_config": notify_config,
            }
            if canal.strip():
                cambios["notify_channel"] = canal.strip()
            if whatsapp_id.strip():
                cambios["whatsapp_phone_number_id"] = whatsapp_id.strip()

            try:
                api.update_business(cambios)
            except ApiError as exc:
                mostrar_error(exc)
            else:
                invalidar_negocio()
                st.success("Configuración guardada.")
                st.rerun()

# ----------------------------------------------------------------------
# Privacidad
# ----------------------------------------------------------------------
with tab_privacidad:
    st.subheader("Eliminar datos de un cliente")
    st.caption(
        "Borra mensajes y solicitudes del cliente a pedido del titular (Ley 1581). "
        "Queda registro del cumplimiento con el hash del identificador, sin guardar el número."
    )
    st.error("**Esto es irreversible.** Los datos no se pueden recuperar después.")

    identificador = st.text_input(
        "Número o identificador del cliente", placeholder="+573001112233"
    )
    confirmacion = st.text_input(
        "Escribí el mismo identificador otra vez para confirmar",
        placeholder="+573001112233",
    )
    solicitante = st.text_input("Solicitado por (opcional)", placeholder="titular / soporte")

    coincide = bool(identificador.strip()) and identificador.strip() == confirmacion.strip()
    if identificador.strip() and confirmacion.strip() and not coincide:
        st.warning("Los dos identificadores no coinciden.")

    if st.button("Eliminar datos del cliente", type="primary", disabled=not coincide):
        try:
            resultado = api.delete_customer_data(
                identificador.strip(), solicitante.strip() or None
            )
        except ApiError as exc:
            mostrar_error(exc)
        else:
            st.success(
                f"Datos eliminados: {resultado.get('messages_deleted', 0)} mensaje(s), "
                f"{resultado.get('requests_deleted', 0)} solicitud(es)."
            )

    st.divider()
    st.subheader("Registro de eliminaciones")

    try:
        registro = api.deletion_log()
    except ApiError as exc:
        mostrar_error(exc)
    else:
        if not registro:
            st.info("No hay eliminaciones registradas.")
        else:
            st.dataframe(
                [
                    {
                        "Hash": r.get("identifier_hash"),
                        "Mensajes": r.get("messages_deleted"),
                        "Solicitudes": r.get("requests_deleted"),
                        "Solicitado por": r.get("requested_by") or "—",
                        "Fecha": str(r.get("deleted_at", ""))[:16].replace("T", " "),
                    }
                    for r in registro
                ],
                use_container_width=True,
            )
