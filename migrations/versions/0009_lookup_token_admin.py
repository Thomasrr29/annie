"""Permite resolver el token del panel, sin debilitar RLS.

Mismo patron que la 0008 (mapeo de numero de WhatsApp): autenticar un request
del panel exige averiguar A QUE NEGOCIO pertenece el token, y eso ocurre ANTES de
tener contexto de tenant. Con la politica RLS de `admin_tokens` activa, ese
lookup no ve ninguna fila y **todos los tokens quedan invalidos**: el panel
entero responde 401.

Se expone solo el mapeo necesario a traves de una vista con
`security_invoker = false`:

    token_hash -> (business_id, admin_user_id)

Con eso se puede autenticar y nada mas. La tabla `admin_tokens` sigue protegida
por RLS para cualquier otro acceso (listar tokens, revocarlos, etc.), que ocurre
ya con el tenant resuelto.

`revoked_at` se filtra dentro de la vista: un token revocado ni siquiera es
visible para la autenticacion.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_agente"
TEST_ROLE = "app_agente_test"


def upgrade() -> None:
    op.execute("""
        CREATE VIEW admin_token_lookup
          WITH (security_invoker = false) AS
        SELECT t.token_hash,
               t.id AS token_id,
               t.business_id,
               t.admin_user_id
        FROM admin_tokens t
        JOIN businesses b ON b.id = t.business_id
        WHERE t.revoked_at IS NULL
          AND b.status = 'active'
    """)

    for role in (APP_ROLE, TEST_ROLE):
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT SELECT ON admin_token_lookup TO {role};
              END IF;
            END
            $$
        """)

    # `last_used_at` se actualiza tras autenticar, cuando todavia no hay tenant
    # fijado. Se hace con una funcion SECURITY DEFINER acotada a esa columna, en
    # vez de abrir UPDATE sobre la tabla.
    op.execute("""
        CREATE OR REPLACE FUNCTION touch_admin_token(p_token_id uuid)
        RETURNS void AS $$
          UPDATE admin_tokens SET last_used_at = now() WHERE id = p_token_id;
        $$ LANGUAGE sql SECURITY DEFINER
    """)

    for role in (APP_ROLE, TEST_ROLE):
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT EXECUTE ON FUNCTION touch_admin_token(uuid) TO {role};
              END IF;
            END
            $$
        """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS touch_admin_token(uuid)")
    op.execute("DROP VIEW IF EXISTS admin_token_lookup")
