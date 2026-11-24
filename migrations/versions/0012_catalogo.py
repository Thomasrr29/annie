"""Catálogo: lo que vende el negocio (productos o servicios) y su precio.

Por qué existe (estudio/experimento_tinte.md): con los precios escritos en el
texto del conocimiento, el modelo los REESCRIBE al responder, y a veces se le
cruza el renglón ("el tinte vale $30.000", que era el corte). Tener la
información en el prompt no garantiza copiarla bien.

Con esta tabla el modelo ya no escribe números: elige el ítem con una
referencia ({{precio:tinte-completo}}) y el sistema pone el precio desde aquí
(`app/core/catalog.py`). "Lo que ve la persona sale del sistema, no del modelo".

Básico a propósito: nombre + precio. Variantes, stock o categorías son otra
decisión, con su propia migración.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE catalog_items (
          id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id  uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          name         text        NOT NULL,
          -- Pesos enteros: el COP no usa centavos en la práctica.
          price        bigint      NOT NULL CHECK (price >= 0),
          currency     text        NOT NULL DEFAULT 'COP',
          status       text        NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'inactive')),
          created_at   timestamptz NOT NULL DEFAULT now(),
          updated_at   timestamptz NOT NULL DEFAULT now(),
          -- Dos ítems con el mismo nombre harían ambigua la referencia del modelo.
          UNIQUE (business_id, name)
        )
    """)
    op.execute("CREATE INDEX idx_catalog_business ON catalog_items (business_id, status)")

    op.execute("ALTER TABLE catalog_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE catalog_items FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON catalog_items
          USING (business_id = app_current_business_id())
          WITH CHECK (business_id = app_current_business_id())
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalog_items CASCADE")
