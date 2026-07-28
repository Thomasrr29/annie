"""
Crea un token de acceso al panel para un negocio.

Uso:
    .venv/bin/python -m scripts.crear_token_admin --business-id <uuid> --email dueno@negocio.com

El token se muestra UNA sola vez: en la base solo queda su hash. Si se pierde,
se crea otro y se revoca el anterior.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import text

from app.api.admin import generate_token
from app.db.session import dispose_engine, set_tenant, system_session


async def crear_token(business_id: UUID, email: str, label: str | None = None) -> str:
    """Crea (o reutiliza) el admin_user y le emite un token nuevo."""
    async with system_session() as session:
        await set_tenant(session, business_id)

        existe = (
            await session.execute(
                text("SELECT 1 FROM businesses WHERE id = :bid"), {"bid": business_id}
            )
        ).scalar_one_or_none()
        if not existe:
            raise SystemExit(f"No existe el negocio {business_id}")

        admin_id = (
            await session.execute(
                text("""
                    INSERT INTO admin_users (business_id, email, role)
                    VALUES (:bid, :email, 'owner')
                    ON CONFLICT (business_id, email) DO UPDATE SET email = EXCLUDED.email
                    RETURNING id
                """),
                {"bid": business_id, "email": email},
            )
        ).scalar_one()

        token, token_hash, prefijo = generate_token()

        await session.execute(
            text("""
                INSERT INTO admin_tokens
                  (business_id, admin_user_id, token_hash, token_prefix, label)
                VALUES (:bid, :aid, :hash, :prefix, :label)
            """),
            {
                "bid": business_id,
                "aid": admin_id,
                "hash": token_hash,
                "prefix": prefijo,
                "label": label or "cli",
            },
        )

    return token


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Crea un token del panel admin")
    parser.add_argument("--business-id", required=True, help="uuid del negocio")
    parser.add_argument("--email", required=True, help="email del operador")
    parser.add_argument("--label", default=None, help="etiqueta del token")
    args = parser.parse_args()

    try:
        token = await crear_token(UUID(args.business_id), args.email, args.label)
        print("\nToken creado (guardalo ahora, no se vuelve a mostrar):\n")
        print(f"  {token}\n")
        print("Uso:")
        print(f'  curl -H "Authorization: Bearer {token}" localhost:8000/admin/business\n')
        return 0
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
