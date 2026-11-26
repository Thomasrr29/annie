"""
Transcripción intercambiable (app/core/transcription.py). Sin base ni AWS:
los proveedores se reemplazan por falsos.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest

from app.core import transcription as tr
from app.core.transcription import (
    AudioFile,
    TranscriptionError,
    TranscriptionStatus,
    ogg_duration_seconds,
    transcribe,
)


def _ogg(segundos: float) -> bytes:
    """Un Ogg/Opus mínimo: lo que importa es el granule position de la última página."""
    granule = int(segundos * 48_000)
    pagina = b"OggS" + b"\x00\x00" + struct.pack("<q", granule) + b"\x00" * 16
    return pagina + b"datos-de-audio" + pagina


class ProveedorFalso:
    name = "falso"

    def __init__(self, texto: str = "quería saber el precio", error: str | None = None) -> None:
        self.texto = texto
        self.error = error
        self.llamadas: list[tuple[AudioFile, str]] = []

    async def transcribe(self, audio: AudioFile, language: str) -> str:
        self.llamadas.append((audio, language))
        if self.error:
            raise TranscriptionError(self.error)
        return self.texto


# ----------------------------------------------------------------------
# El archivo: el mismo para cualquier proveedor
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mime", "extension"),
    [
        ("audio/ogg; codecs=opus", "ogg"),  # nota de voz de WhatsApp: Opus dentro de Ogg
        ("audio/opus", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("audio/desconocido", "bin"),
    ],
)
def test_la_extension_sale_del_mime(mime: str, extension: str) -> None:
    audio = AudioFile(data=b"x", mime_type=mime)
    assert audio.extension == extension
    assert audio.filename == f"audio.{extension}"


def test_duracion_de_un_ogg_sin_decodificarlo() -> None:
    assert ogg_duration_seconds(_ogg(30)) == pytest.approx(30.0)
    assert ogg_duration_seconds(b"ID3 no es ogg") is None


# ----------------------------------------------------------------------
# Las reglas comunes, sin importar el proveedor
# ----------------------------------------------------------------------
async def test_transcribe_con_el_proveedor_que_se_le_pase() -> None:
    proveedor = ProveedorFalso("¿cuánto vale el tinte?")

    resultado = await transcribe(AudioFile(_ogg(8), "audio/ogg; codecs=opus"), proveedor)

    assert resultado.status == TranscriptionStatus.DONE
    assert resultado.text == "¿cuánto vale el tinte?"
    assert resultado.provider == "falso"
    assert resultado.duration_seconds == pytest.approx(8.0)
    assert proveedor.llamadas[0][1] == "es"


async def test_un_audio_de_mas_de_2_minutos_no_se_manda_a_transcribir() -> None:
    proveedor = ProveedorFalso()

    resultado = await transcribe(AudioFile(_ogg(150), "audio/ogg"), proveedor)

    assert resultado.status == TranscriptionStatus.TOO_LONG
    assert proveedor.llamadas == [], "no se paga por transcribir lo que se va a descartar"


async def test_sin_duracion_legible_se_acota_por_tamano() -> None:
    grande = AudioFile(b"x" * (tr.MAX_AUDIO_BYTES_SIN_DURACION + 1), "audio/mpeg")
    assert (await transcribe(grande, ProveedorFalso())).status == TranscriptionStatus.TOO_LONG


@pytest.mark.parametrize(
    ("proveedor", "detalle"),
    [
        (ProveedorFalso(error="unsupportedAudioFormat"), "unsupportedAudioFormat"),
        (ProveedorFalso(texto="   "), "transcripción vacía"),  # silencio o ruido
    ],
)
async def test_las_fallas_terminan_en_failed_y_nunca_lanzan(
    proveedor: ProveedorFalso, detalle: str
) -> None:
    resultado = await transcribe(AudioFile(_ogg(5), "audio/ogg"), proveedor)

    assert resultado.status == TranscriptionStatus.FAILED
    assert detalle in resultado.detail
    assert resultado.text == ""


async def test_audio_vacio() -> None:
    resultado = await transcribe(AudioFile(b"", "audio/ogg"), ProveedorFalso())
    assert resultado.status == TranscriptionStatus.FAILED


# ----------------------------------------------------------------------
# Cambiar de proveedor es cambiar una variable
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("configurado", "esperado"),
    [("amazon", "amazon"), ("none", "none"), ("no-existe", "none")],
)
def test_el_proveedor_se_elige_por_configuracion(
    monkeypatch: pytest.MonkeyPatch, configurado: str, esperado: str
) -> None:
    monkeypatch.setattr(tr.settings, "transcription_provider", configurado)
    assert tr.get_transcriber().name == esperado


async def test_desactivado_deja_el_audio_como_no_escuchado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tr.settings, "transcription_provider", "none")
    resultado = await transcribe(AudioFile(_ogg(5), "audio/ogg"))
    assert resultado.status == TranscriptionStatus.FAILED


# ----------------------------------------------------------------------
# Amazon Transcribe (cliente de AWS simulado)
# ----------------------------------------------------------------------
async def test_amazon_manda_el_audio_en_la_peticion_y_lee_el_texto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pedidos: list[dict[str, Any]] = []

    class ClienteFalso:
        def transcribe_audio(self, **kwargs: Any) -> dict[str, Any]:
            pedidos.append(kwargs)
            return {"TranscriptResponse": {"Results": {"Transcript": "hola, ¿tienen cupo?"}}}

    monkeypatch.setattr(tr.AmazonTranscribeSync, "_client", staticmethod(lambda: ClienteFalso()))
    audio = AudioFile(_ogg(4), "audio/ogg; codecs=opus")

    texto = await tr.AmazonTranscribeSync().transcribe(audio, "es")

    assert texto == "hola, ¿tienen cupo?"
    assert pedidos == [{"Media": {"MediaBytes": audio.data}, "Language": "es"}]


async def test_un_error_de_aws_se_vuelve_transcription_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClienteQueFalla:
        def transcribe_audio(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("BadRequestException: unsupportedAudioFormat")

    monkeypatch.setattr(
        tr.AmazonTranscribeSync, "_client", staticmethod(lambda: ClienteQueFalla())
    )

    with pytest.raises(TranscriptionError, match="unsupportedAudioFormat"):
        await tr.AmazonTranscribeSync().transcribe(AudioFile(_ogg(4), "audio/ogg"), "es")


def test_por_defecto_no_hay_proveedor() -> None:
    """Amazon Transcribe no está en el plan gratuito de AWS (decisión 2026-11-25)."""
    from app.config import Settings

    assert Settings.model_fields["transcription_provider"].default == "none"
