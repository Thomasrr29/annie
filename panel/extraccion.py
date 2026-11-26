"""
Extraccion de texto de los archivos que sube el dueno del negocio.

Lo que sale de aca va COMPLETO al prefijo cacheado del prompt y el agente puede
citarlo TEXTUALMENTE al cliente (decision 5 de CLAUDE.md). De ahi dos reglas que
el panel respeta encima de este modulo:

1. El texto extraido SIEMPRE se muestra editable antes de guardar. Un PDF con
   columnas, tablas o encabezados repetidos produce texto desordenado, y ese
   desorden llega tal cual al cliente.
2. La normalizacion es deterministica: mismo archivo => mismo texto. El
   `knowledge_context` se ensambla concatenando documentos y viaja en el prefijo
   cacheado; si el texto cambiara entre cargas, se invalidaria la cache (RNF-02).
"""

from __future__ import annotations

import io
import re

EXTENSIONES_SOPORTADAS = (".txt", ".md", ".pdf", ".docx")


class ExtraccionError(RuntimeError):
    """No se pudo sacar texto del archivo. El panel lo muestra tal cual."""


def normalizar(texto: str) -> str:
    """
    Limpieza minima y deterministica.

    Unifica saltos de linea, quita espacios al final de cada linea y colapsa las
    lineas en blanco de sobra (los PDFs generan muchas). No reescribe contenido:
    solo quita ruido que gastaria tokens en cada turno.
    """
    plano = texto.replace("\r\n", "\n").replace("\r", "\n")
    lineas = [linea.rstrip() for linea in plano.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas)).strip()


def _extraer_texto_plano(datos: bytes) -> str:
    """Los .txt/.md se asumen UTF-8; si no, se cae a latin-1 antes de fallar."""
    try:
        return datos.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return datos.decode("latin-1")
        except UnicodeDecodeError as exc:
            raise ExtraccionError(
                "No se pudo leer el archivo como texto. Guardalo en UTF-8 y reintentá."
            ) from exc


def _extraer_pdf(datos: bytes) -> str:
    """
    Texto de un PDF, pagina por pagina.

    Un PDF escaneado (imagen) devuelve vacio: no hay OCR en el MVP. Se avisa en
    vez de guardar un documento sin contenido, que dejaria al agente creyendo que
    tiene informacion que en realidad no tiene.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depende del extra 'panel'
        raise ExtraccionError(
            "Falta la dependencia pypdf. Instalá el extra: pip install -e '.[panel]'"
        ) from exc

    try:
        lector = PdfReader(io.BytesIO(datos))
        if lector.is_encrypted:
            # Los PDFs "protegidos" sin clave real se abren con clave vacia.
            try:
                lector.decrypt("")
            except Exception as exc:  # noqa: BLE001 - cualquier fallo es lo mismo aca
                raise ExtraccionError(
                    "El PDF está protegido con contraseña. Quitale la protección y reintentá."
                ) from exc
        paginas = [(pagina.extract_text() or "").strip() for pagina in lector.pages]
    except ExtraccionError:
        raise
    except Exception as exc:  # noqa: BLE001 - pypdf levanta de todo con PDFs rotos
        raise ExtraccionError(f"No se pudo leer el PDF: {exc}") from exc

    texto = "\n\n".join(p for p in paginas if p)
    if not texto.strip():
        raise ExtraccionError(
            "El PDF no tiene texto extraíble (suele pasar con documentos escaneados). "
            "Copiá y pegá el contenido a mano."
        )
    return texto


def _extraer_docx(datos: bytes) -> str:
    """Parrafos y tablas de un .docx, en orden de lectura."""
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - depende del extra 'panel'
        raise ExtraccionError(
            "Falta la dependencia python-docx. Instalá el extra: pip install -e '.[panel]'"
        ) from exc

    try:
        documento = docx.Document(io.BytesIO(datos))
    except Exception as exc:  # noqa: BLE001
        raise ExtraccionError(f"No se pudo leer el .docx: {exc}") from exc

    partes = [p.text.strip() for p in documento.paragraphs if p.text.strip()]

    for tabla in documento.tables:
        for fila in tabla.rows:
            celdas = [celda.text.strip() for celda in fila.cells]
            if any(celdas):
                # Separador simple: legible para el modelo sin inventar markdown.
                partes.append(" | ".join(celdas))

    texto = "\n\n".join(partes)
    if not texto.strip():
        raise ExtraccionError("El documento está vacío o no tiene texto extraíble.")
    return texto


def extraer_texto(nombre_archivo: str, datos: bytes) -> str:
    """
    Devuelve el texto normalizado del archivo, segun su extension.

    `nombre_archivo` solo se usa para elegir el extractor; el contenido nunca se
    infiere del nombre.
    """
    nombre = nombre_archivo.lower()

    if nombre.endswith((".txt", ".md")):
        crudo = _extraer_texto_plano(datos)
    elif nombre.endswith(".pdf"):
        crudo = _extraer_pdf(datos)
    elif nombre.endswith(".docx"):
        crudo = _extraer_docx(datos)
    elif nombre.endswith(".doc"):
        raise ExtraccionError(
            "El formato .doc (Word viejo) no está soportado. Guardalo como .docx o .pdf."
        )
    else:
        raise ExtraccionError(
            f"Formato no soportado. Aceptados: {', '.join(EXTENSIONES_SOPORTADAS)}"
        )

    return normalizar(crudo)


def estimar_tokens(texto: str) -> int:
    """
    Estimacion gruesa de tokens (~4 caracteres por token en espanol).

    Es orientativa, para que el dueno vea cuanto pesa su base de conocimiento en
    el prompt. El numero real lo reporta la API en `turn_logs`.
    """
    return len(texto) // 4


__all__ = [
    "EXTENSIONES_SOPORTADAS",
    "ExtraccionError",
    "estimar_tokens",
    "extraer_texto",
    "normalizar",
]
