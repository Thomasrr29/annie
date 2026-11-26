"""
Aislamiento multi-tenant por Row-Level Security (RNF-04, capa 2).

Criterio de aceptacion de la fase 1: con app.current_business_id fijado al
tenant A, un SELECT sobre filas del tenant B devuelve 0 filas AUNQUE la consulta
no filtre por business_id.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql import text

from tests.conftest import requires_db

pytestmark = [requires_db]

# Tablas con business_id que deben quedar cubiertas por la politica.
TENANT_TABLES = [
    "admin_users",
    "agents",
    "knowledge_documents",
    "knowledge_chunks",
    "catalog_items",
    "customers",
    "customer_memory",
    "conversations",
    "messages",
    "message_media",
    "turn_logs",
    "captured_requests",
    "escalations",
]


async def _set_tenant(conn: AsyncConnection, business_id: UUID) -> None:
    await conn.execute(
        text("SELECT set_config('app.current_business_id', :bid, true)"),
        {"bid": str(business_id)},
    )


async def test_select_sin_filtro_solo_ve_el_tenant_activo(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    """El SELECT no filtra por business_id; RLS debe hacerlo igual."""
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_a)
    rows = (await conn.execute(text("SELECT business_id FROM messages"))).scalars().all()

    assert rows, "el tenant A debe ver sus propios mensajes"
    assert all(bid == business_a for bid in rows)
    assert business_b not in rows


async def test_no_se_pueden_leer_filas_de_otro_tenant_ni_apuntando_al_id(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    """Aun pidiendo explicitamente el business_id ajeno, el resultado es vacio."""
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_a)
    result = await conn.execute(
        text("SELECT count(*) FROM messages WHERE business_id = :bid"),
        {"bid": business_b},
    )
    assert result.scalar_one() == 0


async def test_conversaciones_y_clientes_tambien_estan_aislados(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_b)
    customers = (await conn.execute(text("SELECT business_id FROM customers"))).scalars().all()
    conversations = (
        (await conn.execute(text("SELECT business_id FROM conversations"))).scalars().all()
    )

    assert customers and all(bid == business_b for bid in customers)
    assert conversations and all(bid == business_b for bid in conversations)


async def test_businesses_solo_expone_el_tenant_activo(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    """businesses se filtra por su PK, no por una columna business_id."""
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_a)
    ids = (await conn.execute(text("SELECT id FROM businesses"))).scalars().all()

    assert ids == [business_a]


@pytest.mark.parametrize("valor", ["", "no-es-un-uuid", "   "])
async def test_sin_tenant_valido_no_se_ve_nada(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID], valor: str
) -> None:
    """
    Sin tenant valido no se ve ninguna fila: fail-closed, no fail-open.

    Cubre vacio y malformado, que con el cast directo a uuid de la 0001 hacian
    reventar la consulta en vez de devolver cero filas (arreglado en la 0003).
    """
    await conn.execute(
        text("SELECT set_config('app.current_business_id', :v, true)"), {"v": valor}
    )

    for table in ("messages", "customers", "conversations", "businesses"):
        count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0, f"{table} expuso filas con tenant invalido ({valor!r})"


async def test_tenant_nunca_fijado_no_se_ve_nada(conn: AsyncConnection) -> None:
    """La variable ni siquiera existe en la sesion: tampoco debe verse nada."""
    for table in ("messages", "customers", "conversations", "businesses"):
        count = (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()
        assert count == 0, f"{table} expuso filas sin tenant fijado"


async def test_no_se_puede_escribir_en_otro_tenant(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    """WITH CHECK impide insertar filas atribuidas a otro negocio."""
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_a)
    conversation_id = (
        await conn.execute(text("SELECT id FROM conversations LIMIT 1"))
    ).scalar_one()

    with pytest.raises(Exception) as exc:
        await conn.execute(
            text("""
                INSERT INTO messages (conversation_id, business_id, role, content)
                VALUES (:conv, :bid, 'customer', 'intruso')
            """),
            {"conv": conversation_id, "bid": business_b},
        )
    assert "row-level security" in str(exc.value).lower()


@pytest.mark.parametrize("table", TENANT_TABLES)
async def test_toda_tabla_con_business_id_tiene_rls_forzado(
    conn: AsyncConnection, table: str
) -> None:
    """
    RLS habilitado Y forzado. Sin FORCE, el dueno de la tabla omite la politica
    y el aislamiento no aplicaria a la app corriendo con ese rol.
    """
    row = (
        await conn.execute(
            text("""
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname = :t AND relkind = 'r'
            """),
            {"t": table},
        )
    ).one()

    assert row.relrowsecurity, f"{table} no tiene RLS habilitado"
    assert row.relforcerowsecurity, f"{table} no tiene RLS forzado (FORCE)"

    policies = (
        await conn.execute(
            text("SELECT policyname FROM pg_policies WHERE tablename = :t"),
            {"t": table},
        )
    ).scalars().all()
    assert "tenant_isolation" in policies, f"{table} no tiene la politica tenant_isolation"


async def test_el_tenant_no_se_filtra_entre_transacciones(
    conn: AsyncConnection, two_tenants: tuple[UUID, UUID]
) -> None:
    """
    SET LOCAL vive hasta el fin de la transaccion. Cambiar de tenant dentro de la
    misma conexion cambia lo visible, sin arrastrar el anterior.
    """
    business_a, business_b = two_tenants

    await _set_tenant(conn, business_a)
    visto_a = (await conn.execute(text("SELECT business_id FROM messages"))).scalars().all()

    await _set_tenant(conn, business_b)
    visto_b = (await conn.execute(text("SELECT business_id FROM messages"))).scalars().all()

    assert set(visto_a) == {business_a}
    assert set(visto_b) == {business_b}


async def test_tenant_inexistente_no_ve_nada(conn: AsyncConnection) -> None:
    await _set_tenant(conn, uuid4())
    count = (await conn.execute(text("SELECT count(*) FROM messages"))).scalar_one()
    assert count == 0


async def test_el_rol_de_la_app_no_puede_saltarse_rls(conn: AsyncConnection) -> None:
    """
    El rol con el que corren los tests (y la app) no debe ser SUPERUSER ni tener
    BYPASSRLS: con cualquiera de los dos, TODAS las politicas quedan inertes y
    el resto de los tests de este archivo pasarian sin probar nada.
    """
    row = (
        await conn.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        )
    ).one()

    assert not row.rolsuper, (
        "la conexion usa un rol SUPERUSER: RLS no aplica y el aislamiento es ficticio. "
        "DATABASE_URL debe apuntar al rol de aplicacion (migracion 0002)."
    )
    assert not row.rolbypassrls, (
        "la conexion usa un rol con BYPASSRLS: RLS no aplica. "
        "DATABASE_URL debe apuntar al rol de aplicacion (migracion 0002)."
    )
