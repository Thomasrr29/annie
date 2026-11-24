"""
Conversaciones: la bandeja para atender clientes como persona (RF-14).

Tres columnas, como una bandeja de WhatsApp: lista de conversaciones, el chat
con el historial completo y la ficha del cliente.

Lo que hay que saber al usarla:

- Lo que se escribe aca le llega al cliente por WhatsApp de verdad. Escribir es
  TOMAR la conversacion: el agente deja de responder hasta que se la devuelva.
- WhatsApp solo deja escribir dentro de las 24h desde el ultimo mensaje del
  cliente. Pasado eso el envio se bloquea (la API devuelve 409) y la ficha lo
  avisa.
- Streamlit no tiene tiempo real: la lista, el chat y la ficha se refrescan solos
  cada pocos segundos (`st.fragment(run_every=...)`). Es la herramienta del
  piloto; una bandeja con push real necesita otro frontend sobre la misma API.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import streamlit as st

from api_client import AdminAPI, ApiError
from sesion import mostrar_error, requerir_api

SELECCION_KEY = "conversacion_seleccionada"
MEDIA_CACHE_KEY = "media_cache"
ULTIMO_LEIDO_KEY = "ultimo_seq_leido"

REFRESCO_LISTA = "6s"
REFRESCO_CHAT = "4s"
REFRESCO_FICHA = "8s"

FORMATOS_IMAGEN = ["jpg", "jpeg", "png", "webp"]
# Tope de WhatsApp para imagenes; la API lo valida igual.
MAX_IMAGEN_MB = 5

# `after_seq` imposible: trae el detalle de la conversacion sin mensajes. La
# ficha y el estado del composer no necesitan el historial.
SIN_MENSAJES = 2**62

FILTROS = {"Todas": None, "Esperando persona": "human", "Con el agente": "auto"}

# `escalations.reason` guarda la intencion que disparo el escalamiento
# (`intencion=<valor>`, ver orchestrator._persist_turn). Esto la traduce para el
# dueño del negocio.
MOTIVOS = {
    "intencion=escalate": "Pidió hablar con una persona o el agente no pudo seguir",
    "intencion=fallback": "Consulta fuera de lo que el agente puede resolver",
}

AVATARES = {"customer": "👤", "agent": "🤖", "human": "🧑‍💼", "system": "ℹ️"}
AUTORES = {"customer": "Cliente", "agent": "Agente", "human": "Equipo", "system": "Sistema"}

st.set_page_config(page_title="Conversaciones", page_icon="💬", layout="wide")

# Estilos de la bandeja. Streamlit no tiene un componente de "lista de chats":
# cada item es un contenedor con `key` (Streamlit le pone la clase
# `st-key-<key>`) y desde aca se le da forma de fila. Solo tonos neutros: el
# color queda para estados (badges) y la accion principal (Enviar).
st.html(
    """
    <style>
      /* Los fragmentos se refrescan solos cada pocos segundos; sin esto
         Streamlit atenua la pagina en cada refresco y parece que parpadea. */
      [data-stale="true"] { opacity: 1 !important; transition: none !important; }

      .etiqueta {
        font-size: 0.72rem; font-weight: 600; letter-spacing: 0.06em;
        text-transform: uppercase; opacity: 0.55; margin: 0 0 4px 0;
      }

      /* Fila de la lista: nombre + estado + vista previa como una sola unidad. */
      [class*="st-key-item-"] {
        gap: 2px; padding: 10px 12px; border-radius: 10px;
        transition: background-color 120ms ease-out;
      }
      [class*="st-key-item-"]:hover { background: rgba(128, 128, 128, 0.08); }
      [class*="st-key-item-sel-"] {
        background: rgba(128, 128, 128, 0.16);
        box-shadow: inset 3px 0 0 rgba(128, 128, 128, 0.7);
      }
      [class*="st-key-item-"] button {
        justify-content: flex-start; padding: 0; min-height: 0; width: 100%;
      }
      [class*="st-key-item-"] button > div { justify-content: flex-start; }
      [class*="st-key-item-"] button p { font-weight: 600; text-align: left; }
      [class*="st-key-item-"] [data-testid="stCaptionContainer"] p {
        margin: 0; line-height: 1.45;
      }
    </style>
    """
)

api = requerir_api()

# La seleccion vive en session_state (la pueden fijar otras paginas, como
# Bandejas) y se refleja en la URL para que recargar no la pierda.
if "c" in st.query_params and SELECCION_KEY not in st.session_state:
    st.session_state[SELECCION_KEY] = st.query_params["c"]


# ----------------------------------------------------------------------
# Utilidades de formato
# ----------------------------------------------------------------------
def _fecha(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _hace(iso: str | None) -> str:
    """'hace 5 min', 'hace 2 h', o la fecha si es de otro dia."""
    momento = _fecha(iso)
    if momento is None:
        return ""
    segundos = (datetime.now(UTC) - momento).total_seconds()
    if segundos < 60:
        return "ahora"
    if segundos < 3600:
        return f"hace {int(segundos // 60)} min"
    if segundos < 86400:
        return f"hace {int(segundos // 3600)} h"
    return momento.astimezone().strftime("%d/%m %H:%M")


def _hora(iso: str | None) -> str:
    momento = _fecha(iso)
    return momento.astimezone().strftime("%d/%m %H:%M") if momento else ""


_ESPECIALES_MARKDOWN = set("\\`*_{}[]()#+-.!|<>~$")


def _texto_literal(texto: str) -> str:
    """
    Texto del cliente para `st.markdown`, sin que se interprete como markdown:
    un `*` o un `[link](...)` en un mensaje no debe cambiar el formato ni crear
    enlaces. Se conservan los saltos de linea.
    """
    escapado = "".join(f"\\{c}" if c in _ESPECIALES_MARKDOWN else c for c in texto)
    return escapado.replace("\n", "  \n")


def _nombre(cliente: dict[str, Any]) -> str:
    return str(cliente.get("display_name") or cliente.get("channel_identifier") or "Cliente")


def _vista_previa(ultimo: dict[str, Any] | None) -> str:
    """Ultimo mensaje en una linea, ya escapado para markdown."""
    if not ultimo:
        return "Sin mensajes"
    prefijo = {"agent": "Agente: ", "human": "Vos: "}.get(str(ultimo.get("role")), "")
    if ultimo.get("content_type") == "image":
        cuerpo = "📷 Foto" + (f" · {ultimo['content']}" if ultimo.get("content") else "")
    else:
        cuerpo = str(ultimo.get("content") or "")
    cuerpo = cuerpo.replace("\n", " ")
    cuerpo = cuerpo[:48] + "…" if len(cuerpo) > 48 else cuerpo
    return _texto_literal(prefijo + cuerpo)


def _etiqueta(texto: str) -> None:
    """Titulo de seccion: chico y atenuado, para no competir con el contenido."""
    st.markdown(f'<p class="etiqueta">{texto}</p>', unsafe_allow_html=True)


def _badge_estado(conv: dict[str, Any]) -> str:
    """Un solo badge por conversacion: el estado que mas importa."""
    if conv.get("open_escalation"):
        return ":red-badge[Requiere persona]"
    if conv.get("status") == "human":
        return ":orange-badge[Atiende el equipo]"
    return ":gray-badge[Agente]"


def _imagen(cliente_api: AdminAPI, media_id: str) -> bytes | None:
    """
    Bytes de una imagen, cacheados en la sesion: el chat se redibuja cada pocos
    segundos y sin cache bajaria todas las fotos en cada refresco.
    """
    cache: dict[str, bytes] = st.session_state.setdefault(MEDIA_CACHE_KEY, {})
    if media_id not in cache:
        try:
            cache[media_id] = cliente_api.get_media(media_id)
        except ApiError:
            return None
    return cache[media_id]


def _seleccionar(conversation_id: str) -> None:
    st.session_state[SELECCION_KEY] = conversation_id
    st.query_params["c"] = conversation_id


# ----------------------------------------------------------------------
# Columna 1: lista de conversaciones
# ----------------------------------------------------------------------
@st.fragment(run_every=REFRESCO_LISTA)
def lista_de_conversaciones() -> None:
    filtro = st.selectbox(
        "Mostrar",
        list(FILTROS),
        key="filtro_conversaciones",
        label_visibility="collapsed",
    )
    busqueda = st.text_input(
        "Buscar",
        key="busqueda_conversaciones",
        placeholder="Buscar por nombre o teléfono",
        label_visibility="collapsed",
        icon=":material/search:",
    )

    try:
        conversaciones = api.list_conversations(FILTROS.get(filtro or "Todas"), busqueda)
    except ApiError as exc:
        mostrar_error(exc)
        return

    if not conversaciones:
        st.caption("No hay conversaciones con este filtro.")
        return

    seleccionada = st.session_state.get(SELECCION_KEY)
    with st.container(height=600, border=False, gap="small"):
        for conv in conversaciones:
            conv_id = str(conv["id"])
            cliente = conv.get("customer") or {}
            no_leidos = int(conv.get("unread") or 0)

            # La seleccion se marca con fondo, no con el color de acento.
            clave = f"item-sel-{conv_id}" if conv_id == seleccionada else f"item-{conv_id}"
            with st.container(key=clave):
                nombre = _texto_literal(_nombre(cliente))
                if conv.get("channel") == "sandbox":
                    nombre += " · prueba"
                if st.button(nombre, key=f"conv_{conv_id}", type="tertiary"):
                    _seleccionar(conv_id)
                    st.rerun()

                meta = [_badge_estado(conv)]
                if no_leidos:
                    meta.append(f":blue-badge[{no_leidos} sin leer]")
                meta.append(_hace(conv.get("last_message_at")))
                st.caption(" ".join(meta) + "  \n" + _vista_previa(conv.get("last_message")))


# ----------------------------------------------------------------------
# Columna 2: chat
# ----------------------------------------------------------------------
@st.fragment(run_every=REFRESCO_CHAT)
def chat(conversation_id: str) -> None:
    try:
        detalle = api.get_conversation(conversation_id)
    except ApiError as exc:
        mostrar_error(exc)
        return

    cliente = detalle.get("customer") or {}
    humano = detalle.get("status") == "human"
    estado = ":orange-badge[Atiende el equipo]" if humano else ":gray-badge[Atiende el agente]"
    st.markdown(f"#### {_texto_literal(_nombre(cliente))}")
    st.caption(f"{_texto_literal(str(cliente.get('channel_identifier', '')))}  {estado}")

    mensajes: list[dict[str, Any]] = detalle.get("messages") or []
    with st.container(height=520, border=True, autoscroll=True):
        if not mensajes:
            st.caption("Todavía no hay mensajes.")
        for m in mensajes:
            rol = str(m.get("role"))
            with st.chat_message(rol, avatar=AVATARES.get(rol, "💬")):
                st.caption(f"**{AUTORES.get(rol, rol)}** · {_hora(m.get('created_at'))}")
                if m.get("content_type") == "image":
                    datos = _imagen(api, str(m["media_id"])) if m.get("media_id") else None
                    if datos is not None:
                        st.image(datos, width=280)
                    else:
                        st.caption("📷 Imagen no disponible")
                if m.get("content"):
                    st.markdown(_texto_literal(str(m["content"])))

    # Al ver mensajes nuevos se marcan como leidos (una llamada, no una por
    # refresco: solo cuando aparece algo nuevo).
    ultimo_seq = max((int(m.get("seq") or 0) for m in mensajes), default=0)
    leidos: dict[str, int] = st.session_state.setdefault(ULTIMO_LEIDO_KEY, {})
    if ultimo_seq > leidos.get(conversation_id, -1):
        try:
            api.mark_read(conversation_id)
            leidos[conversation_id] = ultimo_seq
        except ApiError:
            pass


def composer(conversation_id: str, detalle: dict[str, Any]) -> None:
    """Caja de respuesta. Fuera del fragmento: un refresco no debe borrar lo escrito."""
    ventana = detalle.get("window") or {}
    cerrada = bool(ventana.get("applies")) and not ventana.get("open")

    volver = st.toggle(
        "Devolver al agente después de enviar",
        key=f"volver_{conversation_id}",
        help="Si no, la conversación queda a tu cargo y el agente no responde.",
    )
    envio = st.chat_input(
        "Ventana de 24 h cerrada: el cliente tiene que escribir primero"
        if cerrada
        else "Escribí tu respuesta… (emojis: Win + .)",
        accept_file="multiple",
        file_type=FORMATOS_IMAGEN,
        max_upload_size=MAX_IMAGEN_MB,
        disabled=cerrada,
        key=f"composer_{conversation_id}",
    )
    if not envio:
        return

    texto = (envio.text or "").strip()
    archivos = list(envio.files or [])
    try:
        if archivos:
            # El texto va como pie de la primera foto, como en WhatsApp.
            for i, archivo in enumerate(archivos):
                api.reply_image(
                    conversation_id,
                    archivo.getvalue(),
                    archivo.type or "image/jpeg",
                    pie=texto if i == 0 else "",
                    volver_a_auto=volver and i == len(archivos) - 1,
                )
        elif texto:
            api.human_reply(conversation_id, texto, volver)
        else:
            return
    except ApiError as exc:
        mostrar_error(exc)
        return
    st.rerun()


# ----------------------------------------------------------------------
# Columna 3: ficha del cliente
# ----------------------------------------------------------------------
@st.fragment(run_every=REFRESCO_FICHA)
def ficha(conversation_id: str) -> None:
    try:
        detalle = api.get_conversation(conversation_id, despues_de_seq=SIN_MENSAJES)
    except ApiError as exc:
        mostrar_error(exc)
        return

    cliente = detalle.get("customer") or {}
    _etiqueta("Cliente")
    st.markdown(f"**{_texto_literal(_nombre(cliente))}**")
    canal = "WhatsApp" if detalle.get("channel") == "whatsapp" else "Prueba (sandbox)"
    st.caption(
        f"{_texto_literal(str(cliente.get('channel_identifier', '')))} · {canal}  \n"
        f"Cliente desde {_hora(cliente.get('created_at'))}"
    )

    st.space("small")
    _etiqueta("Atención")
    escalamiento = detalle.get("open_escalation")
    if escalamiento:
        razon = str(escalamiento.get("reason") or "")
        motivo = MOTIVOS.get(razon, "El agente derivó la conversación a una persona")
        st.markdown(f":red-badge[Requiere persona] {_hace(escalamiento.get('created_at'))}")
        st.caption(motivo)
    if detalle.get("status") == "human":
        if not escalamiento:
            st.markdown(":orange-badge[Atiende el equipo]")
        st.caption("El agente no responde mientras la conversación esté a tu cargo.")
        if st.button("Devolver al agente", key="devolver", width="stretch"):
            _cambiar_estado(conversation_id, "auto")
    else:
        st.markdown(":gray-badge[Atiende el agente]")
        if st.button("Tomar conversación", key="tomar", width="stretch"):
            _cambiar_estado(conversation_id, "human")

    ventana = detalle.get("window") or {}
    if ventana.get("applies"):
        st.space("small")
        _etiqueta("Ventana de WhatsApp")
        vence = _fecha(ventana.get("expires_at"))
        if ventana.get("open") and vence is not None:
            restante = vence - datetime.now(UTC)
            horas, resto = divmod(int(restante.total_seconds()), 3600)
            color = "orange" if horas < 2 else "green"
            st.markdown(f":{color}-badge[Abierta] quedan {horas} h {resto // 60} min")
        else:
            st.markdown(":red-badge[Cerrada]")
            st.caption("Pasaron más de 24 h: el cliente tiene que escribir primero.")


def _cambiar_estado(conversation_id: str, estado: str) -> None:
    try:
        api.set_conversation_status(conversation_id, estado)
    except ApiError as exc:
        mostrar_error(exc)
        return
    st.rerun()


# ----------------------------------------------------------------------
# Pagina
# ----------------------------------------------------------------------
col_lista, col_chat, col_ficha = st.columns([1.2, 2.6, 1.1], gap="large")

with col_lista:
    _etiqueta("Conversaciones")
    lista_de_conversaciones()

seleccion = st.session_state.get(SELECCION_KEY)
if not seleccion:
    col_chat.info("Elegí una conversación de la lista.")
    st.stop()

try:
    detalle_inicial = api.get_conversation(str(seleccion), despues_de_seq=SIN_MENSAJES)
except ApiError as exc:
    if exc.status_code == 404:
        # Conversacion borrada (Ley 1581) o de otro negocio: se olvida.
        st.session_state.pop(SELECCION_KEY, None)
        st.query_params.clear()
        col_chat.info("Esa conversación ya no existe. Elegí otra de la lista.")
    else:
        with col_chat:
            mostrar_error(exc)
    st.stop()

with col_chat:
    chat(str(seleccion))
    composer(str(seleccion), detalle_inicial)

with col_ficha:
    ficha(str(seleccion))
