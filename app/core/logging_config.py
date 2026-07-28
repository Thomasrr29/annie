"""
Logging estructurado en JSON (RNF-07).

Regla dura: **el contenido de los mensajes NUNCA va al log de aplicacion**. Ya
vive en la base, protegido por RLS y sujeto a la politica de retencion; copiarlo
a los logs lo saca de ese perimetro y complica el cumplimiento de la Ley 1581.

Lo que si va: `business_id`, `conversation_id` y metricas. Suficiente para
diagnosticar sin exponer datos personales.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

# Atributos internos de LogRecord: todo lo demas que traiga el record es
# contexto que puso el llamador con `extra=`.
_ATRIBUTOS_ESTANDAR = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

# Claves que no deben aparecer en el log ni por accidente.
_CLAVES_PROHIBIDAS = frozenset({"text", "content", "body", "message_text", "reply_text"})


class JsonFormatter(logging.Formatter):
    """Formatea cada registro como una linea JSON."""

    def format(self, record: logging.LogRecord) -> str:
        salida: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for clave, valor in record.__dict__.items():
            if clave in _ATRIBUTOS_ESTANDAR or clave.startswith("_"):
                continue
            if clave in _CLAVES_PROHIBIDAS:
                # Defensa en profundidad: si alguien loguea contenido por
                # descuido, se marca en vez de escribirlo.
                salida[clave] = "[omitido]"
                continue
            try:
                json.dumps(valor)
                salida[clave] = valor
            except (TypeError, ValueError):
                salida[clave] = str(valor)

        if record.exc_info:
            salida["exc"] = self.formatException(record.exc_info)

        return json.dumps(salida, ensure_ascii=False)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """
    Configura el logging del proceso.

    En desarrollo conviene `json_output=False`, que es mas legible a ojo.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(levelname)-7s %(name)s: %(message)s")
        )
    root.addHandler(handler)

    # SQLAlchemy en INFO vuelca todo el SQL, con parametros incluidos: son datos
    # de clientes yendo al log.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


__all__ = ["JsonFormatter", "configure_logging"]
