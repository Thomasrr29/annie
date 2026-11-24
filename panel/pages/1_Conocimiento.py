"""
Conocimiento del negocio (RF-19).

Todo lo que se carga aca se concatena y viaja COMPLETO en el prefijo cacheado
del prompt: no hay RAG ni busqueda vectorial en el MVP (decision 5 de CLAUDE.md).
Dos consecuencias que la pagina hace visibles:

- El agente puede citar cualquier parte TEXTUALMENTE. Por eso la confirmacion de
  "esto lo puede ver un cliente" es explicita y nunca viene marcada por defecto.
- Todo suma tokens en cada turno. Por eso se muestra el peso acumulado.
"""

from __future__ import annotations

import streamlit as st

from api_client import ApiError
from extraccion import (
    EXTENSIONES_SOPORTADAS,
    ExtraccionError,
    estimar_tokens,
    extraer_texto,
    normalizar,
)
from sesion import mostrar_error, requerir_api

# Umbral orientativo: por encima de esto conviene revisar si todo el material
# tiene que estar en el prompt (decision abierta de la fase 4).
TOKENS_ALERTA = 40_000

st.set_page_config(page_title="Conocimiento", page_icon="📚", layout="wide")
st.title("Conocimiento del negocio")

api = requerir_api()

try:
    documentos = api.list_knowledge()
except ApiError as exc:
    mostrar_error(exc)
    st.stop()

# ----------------------------------------------------------------------
# Peso de la base: es lo que se manda al modelo en cada turno
# ----------------------------------------------------------------------
texto_total = "\n\n".join(d.get("raw_content") or "" for d in documentos)
tokens = estimar_tokens(texto_total)

col1, col2, col3 = st.columns(3)
col1.metric("Documentos", len(documentos))
col2.metric("Caracteres", f"{len(texto_total):,}".replace(",", "."))
col3.metric("Tokens estimados", f"{tokens:,}".replace(",", "."))

if tokens > TOKENS_ALERTA:
    st.warning(
        f"La base supera los {TOKENS_ALERTA:,} tokens estimados. ".replace(",", ".")
        + "Todo esto entra en el prompt de cada conversación. Con caché el costo se "
        "mantiene bajo, pero revisá si todo el material es realmente necesario."
    )

st.divider()

# ----------------------------------------------------------------------
# Cargar documento
# ----------------------------------------------------------------------
st.header("Cargar documento")

modo = st.radio(
    "Origen del contenido",
    ["Subir archivo", "Escribir o pegar texto"],
    horizontal=True,
)

texto_extraido = ""
titulo_sugerido = ""
origen = "upload"

if modo == "Subir archivo":
    archivo = st.file_uploader(
        f"Archivo ({', '.join(EXTENSIONES_SOPORTADAS)})",
        type=[e.lstrip(".") for e in EXTENSIONES_SOPORTADAS],
    )
    if archivo is not None:
        try:
            texto_extraido = extraer_texto(archivo.name, archivo.getvalue())
            titulo_sugerido = archivo.name.rsplit(".", 1)[0].replace("_", " ").strip()
        except ExtraccionError as exc:
            st.error(str(exc))
        else:
            st.success(f"Texto extraído de **{archivo.name}**. Revisalo antes de guardar.")
else:
    origen = "manual"
    texto_extraido = st.text_area(
        "Contenido",
        height=260,
        placeholder="Horarios, servicios, precios, políticas de devolución...",
    )

if texto_extraido.strip():
    st.subheader("Revisá el contenido antes de guardar")
    st.caption(
        "El agente lee esto tal cual y puede citarlo palabra por palabra. "
        "Corregí acá lo que haya quedado desordenado (frecuente en PDFs con tablas o columnas)."
    )

    contenido = st.text_area(
        "Texto que se va a guardar",
        value=texto_extraido,
        height=340,
        key="contenido_revisado",
    )

    titulo = st.text_input(
        "Título",
        value=titulo_sugerido,
        placeholder="Horarios y ubicación",
        help="Se usa como encabezado del bloque dentro del prompt del agente.",
    )

    st.caption(f"~{estimar_tokens(contenido):,} tokens estimados".replace(",", "."))

    st.warning(
        "**Antes de confirmar:** el agente puede mostrarle este contenido a cualquier "
        "cliente por WhatsApp. No cargues costos internos, márgenes, datos de "
        "empleados, ni nada que no publicarías."
    )
    confirmado = st.checkbox(
        "Confirmo que cualquier cliente puede ver este contenido",
        value=False,
        key="confirmacion_publica",
    )

    if st.button("Guardar documento", type="primary", disabled=not confirmado):
        if not titulo.strip():
            st.error("Poné un título.")
        elif not contenido.strip():
            st.error("El contenido está vacío.")
        else:
            try:
                api.create_knowledge(
                    titulo.strip(), normalizar(contenido), origen, confirmado
                )
            except ApiError as exc:
                mostrar_error(exc)
            else:
                st.success(f"Guardado: {titulo}")
                st.rerun()

st.divider()

# ----------------------------------------------------------------------
# Documentos cargados
# ----------------------------------------------------------------------
st.header("Documentos cargados")

if not documentos:
    st.info(
        "Todavía no hay conocimiento cargado. "
        "El agente no va a poder responder preguntas del negocio."
    )
else:
    for doc in documentos:
        contenido_doc = doc.get("raw_content") or ""
        with st.expander(
            f"**{doc.get('title', 'Sin título')}** · "
            f"{len(contenido_doc):,} caracteres".replace(",", ".")
        ):
            st.caption(
                f"Origen: {doc.get('source', '—')} · "
                f"Cargado: {str(doc.get('created_at', ''))[:16].replace('T', ' ')}"
            )
            st.text(contenido_doc[:4000])
            if len(contenido_doc) > 4000:
                st.caption(f"... {len(contenido_doc) - 4000:,} caracteres más".replace(",", "."))

            doc_id = str(doc.get("id"))
            confirmar = st.checkbox(
                "Confirmar borrado", key=f"conf_borrar_{doc_id}", value=False
            )
            if st.button(
                "Eliminar documento", key=f"borrar_{doc_id}", disabled=not confirmar
            ):
                try:
                    api.delete_knowledge(doc_id)
                except ApiError as exc:
                    mostrar_error(exc)
                else:
                    st.success("Documento eliminado.")
                    st.rerun()
