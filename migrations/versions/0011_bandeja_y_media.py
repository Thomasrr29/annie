"""Bandeja de conversaciones e imagenes en los mensajes.

Tres cosas que necesita la bandeja del panel (responder como persona con el
historial completo a la vista):

1. `messages.content_type`: un mensaje puede ser texto o imagen. Para una
   imagen, `content` guarda el pie de foto (puede ser vacio) y el archivo vive
   en `message_media`. El CHECK cierra la lista: un tipo nuevo (audio,
   documento) es una decision explicita con su propia migracion.

2. `message_media`: los bytes de la imagen, 1:1 con el mensaje. Se guardan en
   Postgres (bytea) y no en un bucket a proposito: para el piloto evita sumar
   un servicio y una dependencia, y el borrado por Ley 1581 sigue funcionando
   solo por cascada (customer -> conversation -> message -> media). El tamaño
   lo acota la aplicacion (5 MB, el maximo de WhatsApp para imagenes). Si el
   volumen crece, se mueve a almacenamiento de objetos y esta tabla queda con
   la referencia.

   `wa_media_id` es el id de Meta. Sirve para diagnosticar, no para volver a
   bajar el archivo: Meta lo expira a los pocos dias.

3. `conversations.owner_last_read_at`: hasta donde leyo el negocio. Los
   mensajes del cliente posteriores son los "no leidos" de la bandeja.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Tipo de contenido del mensaje.
    op.execute("ALTER TABLE messages ADD COLUMN content_type text NOT NULL DEFAULT 'text'")
    op.execute("""
        ALTER TABLE messages
          ADD CONSTRAINT ck_messages_content_type
          CHECK (content_type IN ('text', 'image'))
    """)

    # 2. Archivos adjuntos.
    op.execute("""
        CREATE TABLE message_media (
          id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id  uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          message_id   uuid        NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
          mime_type    text        NOT NULL,
          size_bytes   int         NOT NULL,
          data         bytea       NOT NULL,
          wa_media_id  text,
          created_at   timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("ALTER TABLE message_media ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE message_media FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON message_media
          USING (business_id = app_current_business_id())
          WITH CHECK (business_id = app_current_business_id())
    """)

    # 3. Marca de lectura del negocio.
    op.execute("ALTER TABLE conversations ADD COLUMN owner_last_read_at timestamptz")

    # La bandeja lista conversaciones por actividad reciente: corre cada pocos
    # segundos mientras el panel esta abierto.
    op.execute("""
        CREATE INDEX idx_conversations_business_activity
          ON conversations (business_id, last_message_at DESC NULLS LAST)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_conversations_business_activity")
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS owner_last_read_at")
    op.execute("DROP TABLE IF EXISTS message_media CASCADE")
    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_content_type")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS content_type")
