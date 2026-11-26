"""
Extraccion de texto del panel.

Es la unica logica real del panel (el resto es UI sobre la API admin), y lo que
sale de aca termina en el prefijo cacheado del prompt, asi que importa que sea
deterministico: mismo archivo => mismo texto, o se invalida la cache (RNF-02).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "panel"))

from extraccion import (  # noqa: E402
    ExtraccionError,
    estimar_tokens,
    extraer_texto,
    normalizar,
)


class TestNormalizar:
    def test_unifica_saltos_de_linea(self) -> None:
        assert normalizar("a\r\nb\rc") == "a\nb\nc"

    def test_quita_espacios_al_final_de_linea(self) -> None:
        assert normalizar("hola   \nchau\t\t") == "hola\nchau"

    def test_colapsa_lineas_en_blanco_de_sobra(self) -> None:
        assert normalizar("a\n\n\n\n\nb") == "a\n\nb"

    def test_conserva_un_solo_parrafo_en_blanco(self) -> None:
        assert normalizar("a\n\nb") == "a\n\nb"

    def test_es_idempotente(self) -> None:
        """Normalizar dos veces no puede dar algo distinto: eso rompería la caché."""
        texto = "Horarios  \n\n\n\nLun a vie 9-18\r\n\r\nSábados 9-13   "
        una_vez = normalizar(texto)
        assert normalizar(una_vez) == una_vez


class TestExtraerTexto:
    def test_txt_utf8(self) -> None:
        datos = "Atendemos de 9 a 18. Ñandú, café.".encode()
        assert extraer_texto("horarios.txt", datos) == "Atendemos de 9 a 18. Ñandú, café."

    def test_markdown(self) -> None:
        assert extraer_texto("faq.md", b"# FAQ\n\n- Envios: 3 dias") == "# FAQ\n\n- Envios: 3 dias"

    def test_txt_latin1_no_falla(self) -> None:
        """Un .txt exportado de Windows no debe romper la carga."""
        assert "Ma" in extraer_texto("x.txt", "Mañana".encode("latin-1"))

    def test_normaliza_al_extraer(self) -> None:
        assert extraer_texto("x.txt", b"a   \n\n\n\nb") == "a\n\nb"

    def test_formato_no_soportado(self) -> None:
        with pytest.raises(ExtraccionError, match="no soportado"):
            extraer_texto("hoja.xlsx", b"lo que sea")

    def test_doc_viejo_da_mensaje_util(self) -> None:
        with pytest.raises(ExtraccionError, match=".docx"):
            extraer_texto("viejo.doc", b"\xd0\xcf\x11\xe0")

    def test_pdf_invalido_no_propaga_excepcion_cruda(self) -> None:
        """Un archivo roto tiene que llegar como ExtraccionError, no como error de pypdf."""
        pytest.importorskip("pypdf")
        with pytest.raises(ExtraccionError):
            extraer_texto("roto.pdf", b"esto no es un pdf")

    def test_extension_en_mayusculas(self) -> None:
        assert extraer_texto("HORARIOS.TXT", b"abierto") == "abierto"


class TestEstimarTokens:
    def test_aproxima_cuatro_caracteres_por_token(self) -> None:
        assert estimar_tokens("a" * 400) == 100

    def test_texto_vacio(self) -> None:
        assert estimar_tokens("") == 0
