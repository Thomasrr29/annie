"""Tokens de acceso al panel de administracion.

Autenticacion simple para el MVP: un token opaco por `admin_user`, que la API
recibe en `Authorization: Bearer <token>` y usa para resolver el tenant.

Se guarda el HASH del token, nunca el token en claro: si alguien lee la tabla no
obtiene credenciales usables. El token completo se muestra una sola vez, al
crearlo.

`token_prefix` guarda los primeros caracteres para poder identificar un token en
la UI ("termina en ...") sin poder reconstruirlo.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE admin_tokens (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id   uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          admin_user_id uuid        NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
          token_hash    text        NOT NULL UNIQUE,
          token_prefix  text        NOT NULL,
          label         text,
          created_at    timestamptz NOT NULL DEFAULT now(),
          last_used_at  timestamptz,
          revoked_at    timestamptz
        )
    """)
    # El lookup de cada request del panel es por hash.
    op.execute("CREATE INDEX idx_admin_tokens_hash ON admin_tokens (token_hash)")
    op.execute("CREATE INDEX idx_admin_tokens_business ON admin_tokens (business_id)")

    # RLS como toda tabla con business_id. Ojo: la resolucion del token ocurre
    # ANTES de saber el tenant, asi que ese lookup usa `system_session()`.
    op.execute("ALTER TABLE admin_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE admin_tokens FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON admin_tokens
          USING (business_id = app_current_business_id())
          WITH CHECK (business_id = app_current_business_id())
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS admin_tokens CASCADE")
