"""Limites de proteccion y privacidad (fase 7).

Tres cosas:

1. `businesses.daily_turn_limit`: tope diario de turnos por tenant. Es un
   circuit breaker de COSTO: sin esto, un bucle o un abuso puede generar una
   factura enorme antes de que nadie lo note (riesgo #1 del documento de
   alcance).

2. `customers.privacy_notice_sent_at`: el aviso de tratamiento de datos
   (Ley 1581) se manda una sola vez por cliente, en el primer contacto. Sin
   registrar el envio, se repetiria en cada mensaje.

3. `data_deletion_log`: evidencia de cumplimiento. Cuando un titular pide que se
   borren sus datos, la fila del cliente desaparece — y con ella cualquier
   rastro de que se cumplio. Por eso el registro guarda solo el hash del
   identificador: sirve como prueba sin conservar el dato personal.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Circuit breaker de costo por tenant.
    op.execute("ALTER TABLE businesses ADD COLUMN daily_turn_limit int")

    # 2. Aviso de tratamiento de datos (RNF-05).
    op.execute("ALTER TABLE customers ADD COLUMN privacy_notice_sent_at timestamptz")

    # 3. Evidencia de eliminacion (Ley 1581).
    op.execute("""
        CREATE TABLE data_deletion_log (
          id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id        uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          -- Hash del identificador, NO el telefono: guardar el dato personal
          -- aqui vaciaria de sentido el borrado.
          identifier_hash    text        NOT NULL,
          messages_deleted   int         NOT NULL DEFAULT 0,
          requests_deleted   int         NOT NULL DEFAULT 0,
          requested_by       text,
          deleted_at         timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_deletion_log_business ON data_deletion_log (business_id, deleted_at DESC)"
    )

    op.execute("ALTER TABLE data_deletion_log ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_deletion_log FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON data_deletion_log
          USING (business_id = app_current_business_id())
          WITH CHECK (business_id = app_current_business_id())
    """)

    # Conteo de turnos del dia por tenant: es la consulta del circuit breaker y
    # corre en CADA turno, asi que necesita indice propio.
    op.execute("""
        CREATE INDEX idx_turnlogs_business_day
          ON turn_logs (business_id, created_at DESC)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_turnlogs_business_day")
    op.execute("DROP TABLE IF EXISTS data_deletion_log CASCADE")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS privacy_notice_sent_at")
    op.execute("ALTER TABLE businesses DROP COLUMN IF EXISTS daily_turn_limit")
