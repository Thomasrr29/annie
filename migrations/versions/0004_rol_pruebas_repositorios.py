"""Rol de pruebas con BYPASSRLS para testear la capa 1 del aislamiento.

El aislamiento tiene dos capas: el filtro por `business_id` en los repositorios
(capa 1) y RLS en Postgres (capa 2). Si los tests de repositorio corren con RLS
activo, la capa 2 tapa cualquier repositorio que olvide filtrar y el test pasa
sin probar lo que dice probar.

`app_agente_test` existe para eso: omite RLS, de modo que una fuga en un
repositorio se manifieste como fuga. Los tests de RLS (`test_rls_aislamiento.py`)
siguen corriendo con `app_agente`, sin bypass.

NO usar este rol para la aplicacion. Se crea solo si ENV != 'production'.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEST_ROLE = "app_agente_test"


def upgrade() -> None:
    # En produccion no tiene sentido un rol que se salta el aislamiento.
    if os.getenv("ENV", "development").lower() == "production":
        return

    password = os.getenv("TEST_DB_PASSWORD", "test_password_dev").replace("'", "''")

    op.execute(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{TEST_ROLE}') THEN
            CREATE ROLE {TEST_ROLE} LOGIN PASSWORD '{password}';
          END IF;
        END
        $$
    """)
    # BYPASSRLS es justamente el punto: deja al descubierto la capa 1.
    op.execute(f"ALTER ROLE {TEST_ROLE} NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE")

    op.execute(f"GRANT USAGE ON SCHEMA public TO {TEST_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {TEST_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {TEST_ROLE}")
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {TEST_ROLE}
    """)
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO {TEST_ROLE}
    """)


def downgrade() -> None:
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {TEST_ROLE}
    """)
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          REVOKE USAGE, SELECT ON SEQUENCES FROM {TEST_ROLE}
    """)
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {TEST_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {TEST_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {TEST_ROLE}")
    op.execute(f"""
        DO $$
        BEGIN
          DROP ROLE IF EXISTS {TEST_ROLE};
        EXCEPTION WHEN dependent_objects_still_exist THEN
          RAISE NOTICE
            'Rol {TEST_ROLE} conservado: mantiene privilegios en otras bases del cluster.';
        END
        $$
    """)
