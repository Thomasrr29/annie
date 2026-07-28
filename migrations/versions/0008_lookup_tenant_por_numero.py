"""Permite resolver el tenant desde el numero de WhatsApp, sin romper RLS.

El problema: para atender un webhook hay que averiguar A QUE NEGOCIO pertenece el
numero que recibio el mensaje. Ese lookup ocurre ANTES de saber el tenant, asi
que corre sin `app.current_business_id` fijado. Pero la politica RLS de
`businesses` exige `id = app_current_business_id()`, que sin tenant es NULL: la
consulta devuelve cero filas SIEMPRE.

Efecto: ningun mensaje entrante se puede enrutar. El webhook responde 200 y
descarta todo con "phone_number_id sin negocio activo asociado".

La solucion NO es debilitar la politica de `businesses` (seguiria siendo la que
protege el resto de los datos del negocio), sino exponer solo el mapeo
necesario:

  - `whatsapp_number_map`: vista con (business_id, whatsapp_phone_number_id) de
    los negocios activos. Es `security_invoker = false`, o sea que corre con los
    permisos del creador (el rol de migraciones) y por eso no la frena RLS.
  - Solo se le da SELECT al rol de la aplicacion.

Lo unico que se puede sacar de ahi es "este numero pertenece a este negocio",
que es exactamente lo que el webhook necesita y nada mas. Ningun dato del
negocio, ninguna conversacion, ningun cliente.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "app_agente"
TEST_ROLE = "app_agente_test"


def upgrade() -> None:
    # security_invoker=false: la vista se evalua con los permisos de su creador,
    # asi que RLS de businesses no la bloquea. Es deliberado y acotado: expone
    # dos columnas y nada mas.
    op.execute("""
        CREATE VIEW whatsapp_number_map
          WITH (security_invoker = false) AS
        SELECT id AS business_id, whatsapp_phone_number_id
        FROM businesses
        WHERE whatsapp_phone_number_id IS NOT NULL
          AND status = 'active'
    """)

    for role in (APP_ROLE, TEST_ROLE):
        op.execute(f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT SELECT ON whatsapp_number_map TO {role};
              END IF;
            END
            $$
        """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS whatsapp_number_map")
