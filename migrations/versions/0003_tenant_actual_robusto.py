"""Politicas RLS robustas ante tenant vacio/ausente/malformado.

La 0001 compara con `current_setting('app.current_business_id', true)::uuid`.
Si la variable esta sin fijar devuelve NULL y la comparacion es falsa (correcto,
fail-closed), pero si contiene cadena vacia o basura, el cast lanza
`invalid input syntax for type uuid` y la consulta revienta con un 500 en vez de
devolver cero filas.

Aqui se centraliza la lectura en `app_current_business_id()`, que devuelve NULL
en todos esos casos. El comportamiento de seguridad no cambia -- sin tenant
valido no se ve ninguna fila -- pero deja de ser un error de servidor.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES: tuple[str, ...] = (
    "admin_users",
    "agents",
    "knowledge_documents",
    "knowledge_chunks",
    "customers",
    "customer_memory",
    "conversations",
    "messages",
    "turn_logs",
    "captured_requests",
    "escalations",
)


def upgrade() -> None:
    # STABLE: mismo resultado dentro de la consulta, asi el planner la evalua una
    # sola vez por fila-scan en vez de por fila.
    op.execute("""
        CREATE OR REPLACE FUNCTION app_current_business_id() RETURNS uuid AS $$
        DECLARE
          raw text := current_setting('app.current_business_id', true);
        BEGIN
          IF raw IS NULL OR raw = '' THEN
            RETURN NULL;
          END IF;
          RETURN raw::uuid;
        EXCEPTION WHEN invalid_text_representation THEN
          -- Valor malformado: se trata como "sin tenant", no como error.
          RETURN NULL;
        END;
        $$ LANGUAGE plpgsql STABLE
    """)

    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (business_id = app_current_business_id())
              WITH CHECK (business_id = app_current_business_id())
        """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON businesses")
    op.execute("""
        CREATE POLICY tenant_isolation ON businesses
          USING (id = app_current_business_id())
          WITH CHECK (id = app_current_business_id())
    """)


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (business_id = current_setting('app.current_business_id', true)::uuid)
              WITH CHECK (business_id = current_setting('app.current_business_id', true)::uuid)
        """)

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON businesses")
    op.execute("""
        CREATE POLICY tenant_isolation ON businesses
          USING (id = current_setting('app.current_business_id', true)::uuid)
          WITH CHECK (id = current_setting('app.current_business_id', true)::uuid)
    """)
    op.execute("DROP FUNCTION IF EXISTS app_current_business_id()")
