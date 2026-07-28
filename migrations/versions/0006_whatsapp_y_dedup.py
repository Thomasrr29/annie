"""Mapeo numero de WhatsApp -> tenant, y deduplicacion de mensajes entrantes.

Dos cosas que la fase 3 necesita:

1. `businesses.whatsapp_phone_number_id`: el payload de Meta trae el
   `phone_number_id` del numero que RECIBIO el mensaje. Ese es el unico dato del
   webhook que identifica al tenant, asi que el adaptador lo resuelve contra
   esta columna. Es UNIQUE: un numero pertenece a un solo negocio.

2. `inbound_events`: WhatsApp reentrega un mensaje si el ACK tarda o falla, y el
   mismo `external_message_id` puede llegar varias veces. Sin deduplicacion, el
   cliente recibe la respuesta repetida y se paga el turno dos veces.

   La dedup NO puede vivir solo en `messages`: si el turno falla despues de
   escribir el mensaje entrante, se pierde el rastro. Y `messages` se purga por
   retencion (RNF-05), lo que reabriria la ventana de duplicados.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Mapeo numero -> tenant
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE businesses ADD COLUMN whatsapp_phone_number_id text")
    op.execute("""
        ALTER TABLE businesses
          ADD CONSTRAINT uq_businesses_whatsapp_phone_number_id
          UNIQUE (whatsapp_phone_number_id)
    """)
    # Resolver el tenant es lo PRIMERO de cada webhook: indice dedicado.
    op.execute("""
        CREATE INDEX idx_businesses_whatsapp_number
          ON businesses (whatsapp_phone_number_id)
          WHERE whatsapp_phone_number_id IS NOT NULL
    """)

    # ------------------------------------------------------------------
    # Deduplicacion de eventos entrantes
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE inbound_events (
          id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id         uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          channel             text        NOT NULL DEFAULT 'whatsapp',
          external_message_id text        NOT NULL,
          status              text        NOT NULL DEFAULT 'processing',
          received_at         timestamptz NOT NULL DEFAULT now(),
          processed_at        timestamptz,
          error               text
        )
    """)
    # La clave de la dedup: el INSERT del segundo evento con el mismo id falla
    # (o no hace nada con ON CONFLICT), y ahi se corta el reproceso.
    op.execute("""
        ALTER TABLE inbound_events
          ADD CONSTRAINT uq_inbound_events_channel_message
          UNIQUE (channel, external_message_id)
    """)
    op.execute(
        "CREATE INDEX idx_inbound_events_business_time "
        "ON inbound_events (business_id, received_at DESC)"
    )

    # RLS, igual que toda tabla con business_id (RNF-04).
    op.execute("ALTER TABLE inbound_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE inbound_events FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON inbound_events
          USING (business_id = app_current_business_id())
          WITH CHECK (business_id = app_current_business_id())
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS inbound_events CASCADE")
    op.execute("DROP INDEX IF EXISTS idx_businesses_whatsapp_number")
    op.execute("""
        ALTER TABLE businesses
          DROP CONSTRAINT IF EXISTS uq_businesses_whatsapp_phone_number_id
    """)
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS whatsapp_phone_number_id")
