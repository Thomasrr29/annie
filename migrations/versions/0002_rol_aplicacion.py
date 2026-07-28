"""Rol de aplicacion sin BYPASSRLS: hace efectivo el aislamiento (RNF-04).

Las politicas RLS de la 0001 no aplican a roles SUPERUSER ni con BYPASSRLS.
Conectarse con el superusuario del contenedor (lo que hace la imagen por
defecto) deja la capa 2 inerte y sin ningun sintoma visible: las consultas
funcionan, simplemente ven todos los tenants.

Esta migracion crea `app_agente`, el rol con el que la aplicacion DEBE
conectarse: puede leer/escribir los datos pero no puede saltarse RLS.

La contrasena se toma de APP_DB_PASSWORD; si no esta, se usa una de desarrollo.
En produccion el rol conviene crearlo por fuera con una contrasena gestionada.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_agente"


def upgrade() -> None:
    password = os.getenv("APP_DB_PASSWORD", "app_password_dev")
    # quote_literal via parametro no aplica a DDL: escapamos la comilla simple.
    safe_password = password.replace("'", "''")

    # NOSUPERUSER + NOBYPASSRLS son el punto de toda la migracion.
    op.execute(f"""
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
            CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{safe_password}';
          END IF;
        END
        $$
    """)
    op.execute(f"ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE")

    op.execute(f"GRANT CONNECT ON DATABASE {_current_database()} TO {APP_ROLE}")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Tablas futuras (migraciones posteriores) heredan los permisos.
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}
    """)
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}
    """)

    # alembic_version es de la herramienta de migracion, no de la app.
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE}
    """)
    op.execute(f"""
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
          REVOKE USAGE, SELECT ON SEQUENCES FROM {APP_ROLE}
    """)
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON DATABASE {_current_database()} FROM {APP_ROLE}")

    # El rol es del cluster, no de la base: si conserva privilegios en OTRA base
    # (p. ej. una de pruebas migrada con el mismo rol), DROP ROLE falla y dejaria
    # el downgrade a medias. Solo se elimina si ya no depende nada de el; si no,
    # se deja creado sin permisos aqui, que es inocuo.
    op.execute(f"""
        DO $$
        BEGIN
          DROP ROLE IF EXISTS {APP_ROLE};
        EXCEPTION WHEN dependent_objects_still_exist THEN
          RAISE NOTICE
            'Rol {APP_ROLE} conservado: mantiene privilegios en otras bases del cluster.';
        END
        $$
    """)


def _current_database() -> str:
    """Nombre de la base actual, entrecomillado para el GRANT CONNECT."""
    bind = op.get_bind()
    name = bind.exec_driver_sql("SELECT current_database()").scalar_one()
    return f'"{name}"'
