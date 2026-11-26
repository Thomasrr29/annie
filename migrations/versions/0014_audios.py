"""Notas de voz: el mensaje de audio entra al flujo con su transcripción.

Antes, un audio se respondía en el webhook con un texto fijo ("solo puedo leer
texto y fotos"), por fuera del orquestador: no se guardaba, no respetaba la
ráfaga ni el límite por cliente, y annie respondía aunque la conversación
estuviera en manos de una persona (modo humano).

Ahora el audio es un mensaje más:
  - `content_type = 'audio'`.
  - `content` = la transcripción (vacío mientras se transcribe).
  - `transcription_status` = pending | done | failed | too_long.

NO se guarda el archivo de audio, solo el texto (Ley 1581: minimización de
datos). La transcripción la hace el proveedor configurado
(app/core/transcription.py), sea cual sea.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_content_type")
    op.execute("""
        ALTER TABLE messages
          ADD CONSTRAINT ck_messages_content_type
          CHECK (content_type IN ('text', 'image', 'audio'))
    """)
    op.execute("""
        ALTER TABLE messages
          ADD COLUMN transcription_status text
          CHECK (transcription_status IN ('pending', 'done', 'failed', 'too_long'))
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS transcription_status")
    op.execute("DELETE FROM messages WHERE content_type = 'audio'")
    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_content_type")
    op.execute("""
        ALTER TABLE messages
          ADD CONSTRAINT ck_messages_content_type
          CHECK (content_type IN ('text', 'image'))
    """)
