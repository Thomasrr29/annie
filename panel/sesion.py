"""
Sesion del panel: token, conexion y barra lateral compartida.

El token vive UNICAMENTE en `st.session_state` (memoria del proceso, por
pestana). No se escribe a disco, no se loguea y no se muestra en claro: si se
pierde, se emite otro con `scripts/crear_token_admin.py` y se revoca el anterior.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from api_client import AdminAPI, ApiError, base_url

TOKEN_KEY = "admin_token"
URL_KEY = "api_url"
NEGOCIO_KEY = "negocio_cache"


def _cargar_negocio(api: AdminAPI) -> dict[str, Any] | None:
    """
    Valida el token trayendo el negocio, y lo cachea por (url, token).

    Streamlit reejecuta el script entero en cada interaccion; sin cache seria una
    llamada a la API por cada clic en cualquier widget.
    """
    clave = (api.url, api.token)
    cacheado = st.session_state.get(NEGOCIO_KEY)
    if isinstance(cacheado, tuple) and cacheado[0] == clave:
        return cacheado[1]  # type: ignore[no-any-return]

    try:
        negocio = api.get_business()
    except ApiError as exc:
        st.sidebar.error(f"No se pudo conectar: {exc.detail}")
        return None

    st.session_state[NEGOCIO_KEY] = (clave, negocio)
    return negocio


def barra_lateral() -> AdminAPI | None:
    """Dibuja la conexion en la barra lateral y devuelve el cliente si es valida."""
    st.sidebar.title("Panel del negocio")

    url = st.sidebar.text_input(
        "URL de la API",
        value=st.session_state.get(URL_KEY, base_url()),
        help="Dónde corre FastAPI. En local: http://localhost:8000",
    )
    token = st.sidebar.text_input(
        "Token de acceso",
        type="password",
        value=st.session_state.get(TOKEN_KEY, ""),
        help="Se genera con: python -m scripts.crear_token_admin",
    )

    st.session_state[URL_KEY] = url
    st.session_state[TOKEN_KEY] = token

    if not token.strip():
        return None

    api = AdminAPI(token.strip(), url.strip())
    negocio = _cargar_negocio(api)
    if negocio is None:
        return None

    st.sidebar.success(f"Conectado: **{negocio.get('name', 'sin nombre')}**")
    st.sidebar.caption("El token solo queda en memoria de esta sesión.")
    return api


def requerir_api() -> AdminAPI:
    """Igual que `barra_lateral`, pero corta la pagina si no hay conexion."""
    api = barra_lateral()
    if api is None:
        st.info("Ingresá la URL de la API y tu token de acceso en la barra lateral.")
        st.stop()
        # st.stop() no retorna, pero en CI Streamlit no se instala (es el extra
        # [panel]) y para mypy `st` es Any: sin esta línea no sabe que aquí se corta.
        raise RuntimeError("st.stop() no detuvo la página")
    return api


def invalidar_negocio() -> None:
    """Fuerza a releer el negocio tras editarlo."""
    st.session_state.pop(NEGOCIO_KEY, None)


def mostrar_error(exc: ApiError) -> None:
    """Muestra un error de la API con la pista util segun el codigo."""
    if exc.status_code == 401:
        st.error("Token inválido o revocado. Generá uno nuevo y pegalo en la barra lateral.")
    elif exc.status_code == 0:
        st.error(f"{exc.detail}\n\n¿Está corriendo la API? `uvicorn app.main:app --reload`")
    else:
        st.error(f"Error {exc.status_code}: {exc.detail}")


__all__ = [
    "barra_lateral",
    "invalidar_negocio",
    "mostrar_error",
    "requerir_api",
]
