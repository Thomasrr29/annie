"""Marca de mensajes ya atendidos por conversación (ráfagas, invariante I2).

Por qué (prueba de punta a punta, 2026-11-25): el cliente escribió "¿hacen
keratina?" mientras el agente todavía respondía su pregunta anterior. Los dos
turnos corrieron a la vez; el segundo leyó el historial sin la primera
respuesta y contestó todo de nuevo ("¡Hola! … los precios …").

La gente escribe en varios mensajes seguidos. El orquestador ahora espera unos
segundos, agrupa lo que llegó y responde UNA vez (app/core/orchestrator.py).
Para eso necesita saber qué mensajes del cliente ya se atendieron:

  `conversations.last_processed_seq` = el `messages.seq` del último mensaje del
  cliente que ya entró a un turno. Pendientes = mensajes del cliente con un
  `seq` mayor.

Se usa una marca por conversación y no "el último mensaje del agente", porque
`seq` es el orden de inserción: un mensaje que llegó mientras el agente
respondía queda ANTES de esa respuesta y parecería atendido.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE conversations ADD COLUMN last_processed_seq bigint NOT NULL DEFAULT 0"
    )
    # Todo lo que ya existe se da por atendido: si no, al próximo mensaje de una
    # conversación vieja el agente respondería su historial completo.
    op.execute("""
        UPDATE conversations c
           SET last_processed_seq = COALESCE(
                 (SELECT max(m.seq) FROM messages m WHERE m.conversation_id = c.id), 0)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE conversations DROP COLUMN IF EXISTS last_processed_seq")
