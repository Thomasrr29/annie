"""
Capa LLM: UNICA puerta de salida hacia la API de Anthropic.

Nadie mas en el codigo llama a Anthropic. Aqui viven el ensamblado del prompt, el
prompt caching, el tiering de modelos y la captura de metricas de tokens.

Estructura del prompt (contrato inmutable, RNF-02):

    [ system: instructions_md + persona + knowledge_context ]  <- PREFIJO ESTABLE
    [ definiciones de herramientas permitidas ]                <- PREFIJO ESTABLE
    --- cache_control (breakpoint) ---
    [ ultimos N turnos + flow_state ]                          <- DINAMICO
    [ mensaje nuevo del cliente ]                              <- DINAMICO

PROHIBIDO en el prefijo: timestamps, ids de sesion, nombres de cliente, cualquier
cosa que cambie por turno. Un solo caracter distinto invalida la cache y el costo
por conversacion se dispara.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final

from anthropic import (
    APIConnectionError,
    APIStatusError,
    AsyncAnthropic,
    RateLimitError,
)

from app.config import settings
from app.core.catalog import prompt_section as catalog_prompt_section
from app.schemas import (
    IMAGE_PLACEHOLDER,
    AgentConfig,
    BusinessConfig,
    ConversationContext,
    Intent,
    MessageRole,
    TurnUsage,
)

logger = logging.getLogger(__name__)

# Precios por millon de tokens (USD). En settings, no hardcodeados en la logica:
# los modelos y sus precios rotan.
# Haiku 4.5 y Sonnet 5.5 (2026-10). cache_write es la escritura de 5 minutos.
PRICING: Final[dict[str, dict[str, float]]] = {
    "haiku": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "sonnet": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50},
}

MAX_TOOL_ITERATIONS: Final = 5
CLASSIFY_MAX_TOKENS: Final = 256
# Sonnet 5.5 piensa por defecto (adaptive) y ese razonamiento cuenta contra
# max_tokens: con 1024 una respuesta podia cortarse a la mitad.
RESPOND_MAX_TOKENS: Final = 4096
# Chat de soporte: respuestas cortas y sensibles a la latencia. `low` mantiene
# el razonamiento breve; subirlo solo si se mide que la calidad lo pide.
RESPOND_EFFORT: Final = "low"
LLM_MAX_ATTEMPTS: Final = 2


class LLMError(RuntimeError):
    """Fallo al hablar con la API del modelo. El orquestador lo degrada (RNF-10)."""


@dataclass
class ClassifyResult:
    """Salida del clasificador (Haiku)."""

    intent: Intent
    agent_id: str | None = None
    usage: TurnUsage = field(default_factory=TurnUsage)


@dataclass
class AgentTurnResult:
    """Salida del turno del agente (Sonnet)."""

    reply_text: str
    tools_used: list[str] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)


def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


# ======================================================================
# Costo
# ======================================================================
def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """
    Costo estimado del turno en USD.

    Los tokens leidos de cache se cobran aparte (mucho mas barato) y NO estan
    incluidos en `input_tokens`, asi que se suman por separado.
    """
    familia = "haiku" if "haiku" in model.lower() else "sonnet"
    precios = PRICING[familia]
    return round(
        (input_tokens * precios["input"]
         + output_tokens * precios["output"]
         + cache_read_tokens * precios["cache_read"]
         + cache_write_tokens * precios["cache_write"])
        / 1_000_000,
        6,
    )


def _usage_from_response(response: Any, model: str) -> TurnUsage:
    """Extrae el consumo de una respuesta de la API."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return TurnUsage(model_calls=1)

    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    return TurnUsage(
        model_calls=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_estimate_usd=estimate_cost(
            model, input_tokens, output_tokens, cache_read, cache_write
        ),
    )


def _merge_usage(*usages: TurnUsage) -> TurnUsage:
    """Suma el consumo de varias llamadas del mismo turno."""
    total = TurnUsage()
    for u in usages:
        total.model_calls += u.model_calls
        total.input_tokens += u.input_tokens
        total.output_tokens += u.output_tokens
        total.cache_read_tokens += u.cache_read_tokens
        if u.cost_estimate_usd:
            total.cost_estimate_usd = (total.cost_estimate_usd or 0.0) + u.cost_estimate_usd
    return total


# ======================================================================
# Ensamblado del prompt (el corazon del caching)
# ======================================================================
def build_system_prefix(
    agent: AgentConfig, business: BusinessConfig
) -> list[dict[str, Any]]:
    """
    Arma el prefijo ESTABLE del system prompt y le pone el breakpoint de cache.

    Todo lo que entra aca depende solo de (agente, negocio): dos turnos del mismo
    negocio producen bytes identicos. El `cache_control` va en el ULTIMO bloque,
    que es lo que marca el final del prefijo cacheable.
    """
    partes: list[str] = [
        f"Sos el asistente de atención al cliente de {business.name}.",
    ]

    if business.persona_tone:
        partes.append(f"\n## Tono\n{business.persona_tone}")

    if agent.response_style:
        partes.append(f"\n## Estilo de respuesta\n{agent.response_style}")

    partes.append(f"\n## Tus instrucciones\n{agent.instructions_md}")

    if business.knowledge_context:
        # "Base completa": el conocimiento entero va en el prefijo cacheado.
        partes.append(
            "\n## Información del negocio\n"
            "Esta es toda la información disponible. Si algo no está acá, no lo "
            "inventes: decilo con honestidad y ofrecé pasar con una persona.\n\n"
            f"{business.knowledge_context}"
        )

    if business.catalog:
        # Precios por referencia: el modelo NO ve cifras, solo referencias. El
        # sistema las reemplaza por el precio real (app/core/catalog.py).
        partes.append(
            "\n## Productos y servicios (precios)\n"
            "Esto es lo que vende el negocio. Para dar un precio escribí EXACTAMENTE "
            "la referencia del ítem, por ejemplo «cuesta {{precio:clave}}»; el "
            "sistema la reemplaza por el valor real antes de enviar el mensaje.\n"
            "- NUNCA escribas un precio con números: siempre la referencia.\n"
            "- Usá la referencia del ítem por el que pregunta el cliente, no otra.\n"
            "- Si lo que pide no está en esta lista, no tiene precio: decilo y "
            "ofrecé pasar con una persona.\n\n"
            f"{catalog_prompt_section(business.catalog)}"
        )

    partes.append(
        "\n## Reglas generales\n"
        f"- Respondé en {business.language}, salvo que el cliente escriba en otro idioma.\n"
        "- Estás en WhatsApp: mensajes breves, sin markdown pesado.\n"
        "- Nunca inventes precios, horarios, disponibilidad ni promociones.\n"
        "- Si no podés resolver algo, ofrecé escalar a una persona del negocio.\n"
        f"- Si ves «{IMAGE_PLACEHOLDER}», no podés ver la foto: no "
        "describas ni supongas lo que muestra. Pedile que te cuente qué busca, u "
        "ofrecé pasarlo con una persona del negocio que sí puede verla."
    )

    return [
        {
            "type": "text",
            "text": "\n".join(partes),
            # Breakpoint: todo lo anterior es cacheable, lo que sigue es dinamico.
            "cache_control": {"type": "ephemeral"},
        }
    ]


def build_messages(
    ctx: ConversationContext, new_message: str
) -> list[dict[str, Any]]:
    """
    Parte DINAMICA: historial reciente + flow_state + mensaje nuevo.

    Nunca se cachea. El flow_state se inyecta como contexto del sistema dentro
    del turno del usuario, no en el prefijo, porque cambia en cada paso (F5).
    """
    mensajes: list[dict[str, Any]] = []

    for turno in ctx.recent_turns:
        # 'system' y 'human' (operador) se mapean a assistant: para el modelo son
        # mensajes que ya se le dijeron al cliente.
        rol = "user" if turno.role == MessageRole.CUSTOMER else "assistant"
        if mensajes and mensajes[-1]["role"] == rol:
            # La API rechaza dos turnos seguidos del mismo rol.
            mensajes[-1]["content"] += f"\n{turno.content}"
        else:
            mensajes.append({"role": rol, "content": turno.content})

    contenido_nuevo = new_message
    if ctx.flow_state:
        contenido_nuevo = (
            f"[Datos recolectados hasta ahora: {json.dumps(ctx.flow_state, ensure_ascii=False)}]\n"
            f"{new_message}"
        )

    if mensajes and mensajes[-1]["role"] == "user":
        mensajes[-1]["content"] += f"\n{contenido_nuevo}"
    else:
        mensajes.append({"role": "user", "content": contenido_nuevo})

    # La API exige que el primer mensaje sea del usuario.
    while mensajes and mensajes[0]["role"] != "user":
        mensajes.pop(0)

    return mensajes


# ======================================================================
# Clasificacion / enrutamiento (Haiku)
# ======================================================================
CLASSIFY_SYSTEM: Final = """\
Clasificás mensajes de clientes de un negocio para enrutarlos.

Intenciones:
- inform: pide información que el negocio puede tener (horarios, precios, servicios).
- act: quiere hacer o pedir algo concreto (agendar, encargar, reclamar).
- greet: saludo o charla social, sin pedido concreto.
- clarify: el mensaje es ambiguo y hace falta una pregunta para entenderlo.
- escalate: pide hablar con una persona, o está molesto/con un problema serio.
- fallback: fuera de lo que el negocio hace.

Respondé SOLO con un objeto JSON, sin texto alrededor:
{"intent": "<intención>", "agent_id": "<id del agente o null>"}

Elegí el agente cuya descripción mejor cubra el mensaje. Si ninguno encaja, null.\
"""


async def classify(
    message: str, ctx: ConversationContext, agents: list[AgentConfig]
) -> ClassifyResult:
    """
    Clasifica la intencion y elige agente. Usa Haiku: es barato y la tarea es
    simple (RNF-02).

    Si hay un flujo activo, se le dice al modelo, pero la decision de CONTINUE la
    toma el orquestador antes de llegar aca (F5 evita re-clasificar).
    """
    catalogo = "\n".join(f"- {a.id}: {a.name} — {a.routing_description}" for a in agents)
    contexto = f"Agentes disponibles:\n{catalogo}" if catalogo else "No hay agentes configurados."

    if ctx.recent_turns:
        ultimos = ctx.recent_turns[-3:]
        historial = "\n".join(f"{t.role.value}: {t.content}" for t in ultimos)
        contexto += f"\n\nÚltimos turnos:\n{historial}"

    model = settings.anthropic_model_classify
    response = await _call_with_retry(
        model=model,
        max_tokens=CLASSIFY_MAX_TOKENS,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": f"{contexto}\n\nMensaje nuevo: {message}"}],
    )

    usage = _usage_from_response(response, model)
    texto = _first_text(response).strip()

    intent, agent_id = _parse_classification(texto, agents)
    return ClassifyResult(intent=intent, agent_id=agent_id, usage=usage)


def _parse_classification(
    texto: str, agents: list[AgentConfig]
) -> tuple[Intent, str | None]:
    """
    Interpreta la salida del clasificador.

    Ante cualquier duda cae a CLARIFY, que es lo unico seguro: preguntar. Nunca
    inventa un agente que el tenant no tiene.
    """
    payload: dict[str, Any] | None = None
    try:
        payload = json.loads(texto)
    except json.JSONDecodeError:
        # A veces el modelo envuelve el JSON en prosa o en un bloque de codigo.
        inicio, fin = texto.find("{"), texto.rfind("}")
        if inicio >= 0 and fin > inicio:
            try:
                payload = json.loads(texto[inicio : fin + 1])
            except json.JSONDecodeError:
                payload = None

    if not isinstance(payload, dict):
        logger.warning("clasificacion no parseable: %r", texto[:200])
        return Intent.CLARIFY, None

    try:
        intent = Intent(str(payload.get("intent", "")).lower().strip())
    except ValueError:
        logger.warning("intencion desconocida: %r", payload.get("intent"))
        return Intent.CLARIFY, None

    agent_id = payload.get("agent_id")
    if agent_id in (None, "null", ""):
        return intent, None

    # Solo se acepta un agente que exista y sea de este tenant.
    validos = {str(a.id) for a in agents}
    if str(agent_id) not in validos:
        logger.warning("el clasificador eligio un agente inexistente: %r", agent_id)
        return intent, None

    return intent, str(agent_id)


# ======================================================================
# Turno del agente (Sonnet + tool use)
# ======================================================================
async def run_agent_turn(
    agent: AgentConfig,
    business: BusinessConfig,
    ctx: ConversationContext,
    message: str,
    tools: list[dict[str, Any]] | None = None,
    tool_executor: Any = None,
) -> AgentTurnResult:
    """
    Ejecuta el turno con Sonnet, resolviendo tool use si hace falta.

    `tools` ya viene filtrado por el allowlist del agente (RF-07): la herramienta
    no permitida ni siquiera se le ofrece al modelo. El chequeo adicional en el
    ejecutor es cinturon extra.
    """
    system = build_system_prefix(agent, business)
    messages = build_messages(ctx, message)
    model = settings.anthropic_model_respond

    usages: list[TurnUsage] = []
    tools_used: list[str] = []
    tool_results: list[dict[str, Any]] = []

    # Un agente sin herramientas no manda el campo: `tools: null` es un 400.
    extra: dict[str, Any] = {"tools": tools} if tools else {}

    for iteracion in range(MAX_TOOL_ITERATIONS):
        response = await _call_with_retry(
            model=model,
            max_tokens=RESPOND_MAX_TOKENS,
            output_config={"effort": RESPOND_EFFORT},
            system=system,
            messages=messages,
            **extra,
        )
        usages.append(_usage_from_response(response, model))

        if getattr(response, "stop_reason", None) != "tool_use":
            return AgentTurnResult(
                reply_text=_first_text(response).strip(),
                tools_used=tools_used,
                tool_results=tool_results,
                usage=_merge_usage(*usages),
            )

        if tool_executor is None:
            logger.error("el modelo pidio una herramienta pero no hay ejecutor")
            break

        # Se acumula el turno del assistant TAL CUAL llego (bloques de thinking
        # incluidos): Sonnet 5.5 rechaza o descarta el razonamiento si el turno se
        # reenvia editado.
        messages.append({"role": "assistant", "content": response.content})
        bloques_resultado: list[dict[str, Any]] = []

        for bloque in _tool_use_blocks(response):
            nombre = bloque["name"]
            tools_used.append(nombre)
            try:
                resultado = await tool_executor(nombre, bloque.get("input") or {})
                es_error = False
            except Exception as exc:  # noqa: BLE001 - se le devuelve al modelo
                logger.exception("fallo la herramienta %s", nombre)
                resultado = f"La herramienta falló: {exc}"
                es_error = True

            tool_results.append({"tool": nombre, "result": resultado, "error": es_error})
            bloques_resultado.append(
                {
                    "type": "tool_result",
                    "tool_use_id": bloque["id"],
                    "content": str(resultado),
                    "is_error": es_error,
                }
            )

        messages.append({"role": "user", "content": bloques_resultado})

        if iteracion == MAX_TOOL_ITERATIONS - 1:
            logger.warning("se alcanzo el maximo de iteraciones de herramientas")

    return AgentTurnResult(
        reply_text=(
            "Perdoná, tuve un problema procesando eso. "
            "¿Querés que le pase tu mensaje a una persona del negocio?"
        ),
        tools_used=tools_used,
        tool_results=tool_results,
        usage=_merge_usage(*usages),
    )


# ======================================================================
# Utilidades de la API
# ======================================================================
def _first_text(response: Any) -> str:
    """Primer bloque de texto de la respuesta."""
    for bloque in getattr(response, "content", []) or []:
        if getattr(bloque, "type", None) == "text":
            return str(getattr(bloque, "text", ""))
    return ""


def _content_blocks(response: Any) -> list[dict[str, Any]]:
    """Bloques de texto y tool_use como dicts (para leerlos, no para reenviarlos)."""
    bloques: list[dict[str, Any]] = []
    for bloque in getattr(response, "content", []) or []:
        tipo = getattr(bloque, "type", None)
        if tipo == "text":
            bloques.append({"type": "text", "text": getattr(bloque, "text", "")})
        elif tipo == "tool_use":
            bloques.append(
                {
                    "type": "tool_use",
                    "id": getattr(bloque, "id", ""),
                    "name": getattr(bloque, "name", ""),
                    "input": getattr(bloque, "input", {}) or {},
                }
            )
    return bloques


def _tool_use_blocks(response: Any) -> list[dict[str, Any]]:
    return [b for b in _content_blocks(response) if b.get("type") == "tool_use"]


async def _call_with_retry(**kwargs: Any) -> Any:
    """
    Llama a la API con un reintento ante errores transitorios.

    Sobrecarga (529), rate limit (429) y cortes de red se reintentan una vez con
    backoff. Los errores de request (4xx) no: no mejoran solos. Todo termina como
    `LLMError`, que el orquestador convierte en respuesta honesta (RNF-10).
    """
    client = _client()
    ultimo: Exception | None = None

    for intento in range(LLM_MAX_ATTEMPTS):
        try:
            return await client.messages.create(**kwargs)
        except (RateLimitError, APIConnectionError) as exc:
            ultimo = exc
        except APIStatusError as exc:
            if exc.status_code < 500 and exc.status_code != 429:
                raise LLMError(f"la API del modelo rechazo la llamada: {exc}") from exc
            ultimo = exc
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"fallo inesperado llamando al modelo: {exc}") from exc

        if intento < LLM_MAX_ATTEMPTS - 1:
            await asyncio.sleep(1.0)
            logger.warning("reintentando llamada al modelo tras error transitorio")

    raise LLMError(f"la API del modelo no respondio tras {LLM_MAX_ATTEMPTS} intentos: {ultimo}")


__all__ = [
    "AgentTurnResult",
    "ClassifyResult",
    "LLMError",
    "build_messages",
    "build_system_prefix",
    "classify",
    "estimate_cost",
    "run_agent_turn",
]
