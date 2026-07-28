"""Orden estable del historial de mensajes.

`created_at` usa `now()`, que en Postgres es la hora de INICIO DE LA TRANSACCION:
todos los mensajes escritos en un mismo turno (el del cliente y el del agente)
quedan con el MISMO timestamp. Ordenar solo por `created_at` deja el desempate
al azar, y el id es un uuid v4 (aleatorio), asi que tampoco sirve de criterio.

Efecto real: el historial que se le pasa al modelo puede ir desordenado, con la
respuesta del agente antes de la pregunta del cliente.

Se agrega `seq`, un bigint monotono por secuencia: es el orden de insercion real
y no depende del reloj.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS messages_seq_seq AS bigint")
    op.execute("""
        ALTER TABLE messages
          ADD COLUMN seq bigint NOT NULL DEFAULT nextval('messages_seq_seq')
    """)
    op.execute("ALTER SEQUENCE messages_seq_seq OWNED BY messages.seq")

    # Backfill determinista para filas previas: respeta el tiempo y desempata
    # por id para que el resultado sea reproducible.
    op.execute("""
        WITH ordenados AS (
          SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
          FROM messages
        )
        UPDATE messages m SET seq = o.rn FROM ordenados o WHERE m.id = o.id
    """)
    op.execute(
        "SELECT setval('messages_seq_seq', GREATEST((SELECT max(seq) FROM messages), 1))"
    )

    # El acceso del loop es "ultimos N de esta conversacion".
    op.execute("DROP INDEX IF EXISTS idx_messages_conversation")
    op.execute("CREATE INDEX idx_messages_conversation ON messages (conversation_id, seq)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_messages_conversation")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS seq")
    op.execute("DROP SEQUENCE IF EXISTS messages_seq_seq")
    op.execute("CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at)")
