"""
Transcripción de audio, independiente del proveedor.

El resto del sistema solo conoce esto:

    resultado = await transcribe(AudioFile(data=..., mime_type="audio/ogg"))
    resultado.status   # done | failed | too_long
    resultado.text     # la transcripción (si status == done)

Cambiar de herramienta (Amazon Transcribe hoy; Whisper, Deepgram o un modelo
local mañana) es escribir una clase que cumpla `Transcriber` y registrarla en
`_PROVEEDORES`. Se elige con la variable `TRANSCRIPTION_PROVIDER`; ni el
orquestador ni el canal cambian.

Reglas que valen para cualquier proveedor (las aplica `transcribe`, no cada clase):
  - Tope de duración: un audio más largo que `MAX_AUDIO_SECONDS` no se transcribe
    (costo y latencia acotados). Las notas de voz de WhatsApp son Ogg/Opus y su
    duración se lee del propio archivo, sin decodificarlo.
  - Nunca lanza: cualquier error termina en `status=failed` y el turno sigue.
  - No se guarda el audio en ningún lado: solo la transcripción (Ley 1581,
    minimización de datos).
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Final, Protocol

from app.config import settings

logger = logging.getLogger(__name__)

# Tope de duración (decisión de producto, 2026-11-25): las notas de voz rara vez
# pasan de 2 minutos; más largo, se le pide al cliente que lo resuma por escrito.
MAX_AUDIO_SECONDS: Final = 120.0

# Si no se puede leer la duración (formato distinto de Ogg), se acota por tamaño.
# ~2 minutos de voz comprimida rara vez pasan de 1 MB.
MAX_AUDIO_BYTES_SIN_DURACION: Final = 1_000_000

# mime -> extensión. Algunos proveedores deciden el formato por la extensión del
# archivo (Thomas: "con cambiar la extensión a .ogg puede funcionar"): por eso
# `AudioFile` la expone, y para Ogg siempre es ".ogg" aunque el códec sea Opus.
_EXTENSIONES: Final[dict[str, str]] = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",  # Opus suelto se entrega en contenedor Ogg
    "audio/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/amr": "amr",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


class TranscriptionStatus(str, Enum):
    PENDING = "pending"  # se está transcribiendo (estado del mensaje, no del resultado)
    DONE = "done"
    FAILED = "failed"
    TOO_LONG = "too_long"


@dataclass(frozen=True)
class AudioFile:
    """El archivo tal como llegó del canal. Ningún proveedor ve nada más."""

    data: bytes
    mime_type: str  # puede traer parámetros: "audio/ogg; codecs=opus"

    @property
    def base_mime(self) -> str:
        return self.mime_type.split(";")[0].strip().lower()

    @property
    def extension(self) -> str:
        return _EXTENSIONES.get(self.base_mime, "bin")

    @property
    def filename(self) -> str:
        return f"audio.{self.extension}"


@dataclass(frozen=True)
class TranscriptionResult:
    status: TranscriptionStatus
    text: str = ""
    provider: str = ""
    detail: str = ""  # motivo de la falla, para los logs (nunca se le muestra al cliente)
    duration_seconds: float | None = None


class TranscriptionError(RuntimeError):
    """Error de un proveedor. `transcribe` lo convierte en status=failed."""


class Transcriber(Protocol):
    """Lo único que tiene que cumplir un proveedor de transcripción."""

    name: str

    async def transcribe(self, audio: AudioFile, language: str) -> str:
        """Devuelve el texto. Lanza `TranscriptionError` si no puede."""
        ...


# ======================================================================
# Proveedores
# ======================================================================
class AmazonTranscribeSync:
    """
    Amazon Transcribe, API síncrona `TranscribeAudio` (anunciada el 2026-11-20):
    el audio va en la petición y el texto vuelve en la misma respuesta, sin
    trabajos en cola ni S3. $0,007 USD por minuto, cobrado por segundo.
    Acepta Ogg (Opus), WebM, MP3, M4A, MP4, WAV, FLAC y AMR; hasta 25 MB.
    Docs: https://docs.aws.amazon.com/transcribe/latest/APIReference/API_TranscribeAudio.html

    boto3 es síncrono: la llamada corre en un hilo para no bloquear el loop.
    Credenciales: rol de la EC2 en AWS; perfil de la AWS CLI en local.
    """

    name = "amazon"

    @staticmethod
    @lru_cache(maxsize=1)
    def _client() -> Any:
        import boto3  # type: ignore[import-untyped]

        return boto3.client("transcribe", region_name=settings.aws_region)

    def _llamar(self, audio: AudioFile, language: str) -> str:
        respuesta = self._client().transcribe_audio(
            Media={"MediaBytes": audio.data},
            Language=language,
        )
        resultados = (respuesta.get("TranscriptResponse") or {}).get("Results") or {}
        return str(resultados.get("Transcript") or "")

    async def transcribe(self, audio: AudioFile, language: str) -> str:
        try:
            return await asyncio.to_thread(self._llamar, audio, language)
        except Exception as exc:  # noqa: BLE001 - cualquier error del SDK es una falla del proveedor
            raise TranscriptionError(f"Amazon Transcribe: {exc}") from exc


class DisabledTranscriber:
    """Sin proveedor configurado: todo audio queda como 'no se pudo escuchar'."""

    name = "none"

    async def transcribe(self, audio: AudioFile, language: str) -> str:
        raise TranscriptionError("transcripción desactivada (TRANSCRIPTION_PROVIDER=none)")


_PROVEEDORES: Final[dict[str, type[Any]]] = {
    "amazon": AmazonTranscribeSync,
    "none": DisabledTranscriber,
}


def get_transcriber() -> Transcriber:
    """El proveedor configurado en TRANSCRIPTION_PROVIDER."""
    clase = _PROVEEDORES.get(settings.transcription_provider)
    if clase is None:
        logger.error(
            "TRANSCRIPTION_PROVIDER=%r no existe; se desactiva la transcripción",
            settings.transcription_provider,
        )
        clase = DisabledTranscriber
    instancia: Transcriber = clase()
    return instancia


# ======================================================================
# Punto de entrada único
# ======================================================================
def ogg_duration_seconds(data: bytes) -> float | None:
    """
    Duración de un Ogg/Opus sin decodificarlo: el "granule position" de la
    última página Ogg cuenta muestras a 48 kHz (fijo en Opus). None si no es Ogg.
    """
    if not data.startswith(b"OggS"):
        return None
    ultima = data.rfind(b"OggS")
    if ultima < 0 or ultima + 14 > len(data):
        return None
    (granule,) = struct.unpack_from("<q", data, ultima + 6)
    if granule <= 0:
        return None
    return float(granule) / 48_000.0


async def transcribe(
    audio: AudioFile, transcriber: Transcriber | None = None
) -> TranscriptionResult:
    """
    Transcribe con el proveedor configurado y aplica las reglas comunes. Nunca lanza.
    """
    proveedor = transcriber or get_transcriber()

    if not audio.data:
        return TranscriptionResult(TranscriptionStatus.FAILED, provider=proveedor.name,
                                   detail="audio vacío")

    duracion = ogg_duration_seconds(audio.data)
    if duracion is not None and duracion > MAX_AUDIO_SECONDS:
        return TranscriptionResult(TranscriptionStatus.TOO_LONG, provider=proveedor.name,
                                   duration_seconds=duracion)
    if duracion is None and len(audio.data) > MAX_AUDIO_BYTES_SIN_DURACION:
        return TranscriptionResult(TranscriptionStatus.TOO_LONG, provider=proveedor.name,
                                   detail=f"{len(audio.data)} bytes sin duración legible")

    try:
        texto = (await proveedor.transcribe(audio, settings.transcription_language)).strip()
    except TranscriptionError as exc:
        logger.warning("no se pudo transcribir el audio (%s): %s", proveedor.name, exc)
        return TranscriptionResult(TranscriptionStatus.FAILED, provider=proveedor.name,
                                   detail=str(exc), duration_seconds=duracion)

    if not texto:
        # Silencio o ruido: para el cliente es lo mismo que no haberlo escuchado.
        return TranscriptionResult(TranscriptionStatus.FAILED, provider=proveedor.name,
                                   detail="transcripción vacía", duration_seconds=duracion)
    return TranscriptionResult(TranscriptionStatus.DONE, text=texto, provider=proveedor.name,
                               duration_seconds=duracion)


__all__ = [
    "MAX_AUDIO_SECONDS",
    "AmazonTranscribeSync",
    "AudioFile",
    "DisabledTranscriber",
    "TranscriptionError",
    "TranscriptionResult",
    "TranscriptionStatus",
    "Transcriber",
    "get_transcriber",
    "ogg_duration_seconds",
    "transcribe",
]
