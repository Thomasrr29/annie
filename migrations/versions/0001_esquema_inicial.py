"""Esquema inicial: 12 tablas, indices, extensiones y RLS multi-tenant.

DDL espejo de docs/MVP_Estructura_Base_de_Datos.md §2 y §3.

Revision ID: 0001
Revises:
Create Date: 2026-07-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Toda tabla con business_id lleva la politica tenant_isolation (RNF-04).
# customer_memory tiene business_id ademas de la PK customer_id, asi que entra.
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
    # ------------------------------------------------------------------
    # Extensiones
    # ------------------------------------------------------------------
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ------------------------------------------------------------------
    # 1. NEGOCIOS (tenant)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE businesses (
          id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          name           text        NOT NULL,
          persona_tone   text,
          language       text        NOT NULL DEFAULT 'es',
          timezone       text        NOT NULL DEFAULT 'America/Bogota',
          business_hours jsonb       NOT NULL DEFAULT '{}'::jsonb,
          notify_channel text,
          notify_config  jsonb       NOT NULL DEFAULT '{}'::jsonb,
          status         text        NOT NULL DEFAULT 'active',
          created_at     timestamptz NOT NULL DEFAULT now(),
          updated_at     timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------
    # 2. USUARIOS ADMIN
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE admin_users (
          id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id  uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          email        text        NOT NULL,
          display_name text,
          role         text        NOT NULL DEFAULT 'operator',
          created_at   timestamptz NOT NULL DEFAULT now(),
          UNIQUE (business_id, email)
        )
    """)

    # ------------------------------------------------------------------
    # 3. AGENTES (configuracion enrutable)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE agents (
          id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id         uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          name                text        NOT NULL,
          routing_description text        NOT NULL,
          instructions_md     text        NOT NULL,
          allowed_tools       text[]      NOT NULL DEFAULT '{}',
          memory_scope        text        NOT NULL DEFAULT 'customer',
          response_style      text,
          handler_type        text        NOT NULL DEFAULT 'llm',
          status              text        NOT NULL DEFAULT 'active',
          created_at          timestamptz NOT NULL DEFAULT now(),
          updated_at          timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_agents_business_active ON agents (business_id) WHERE status = 'active'"
    )

    # ------------------------------------------------------------------
    # 4. DOCUMENTOS DE CONOCIMIENTO
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE knowledge_documents (
          id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          title       text        NOT NULL,
          source      text,
          raw_content text,
          status      text        NOT NULL DEFAULT 'pending',
          created_at  timestamptz NOT NULL DEFAULT now(),
          updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------
    # 5. CHUNKS DE CONOCIMIENTO (RAG diferido; el esquema ya existe)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE knowledge_chunks (
          id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          document_id uuid        NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
          content     text        NOT NULL,
          embedding   vector(1024),
          metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
          created_at  timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_chunks_embedding ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX idx_chunks_business ON knowledge_chunks (business_id)")

    # ------------------------------------------------------------------
    # 6. CLIENTES
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE customers (
          id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id        uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          channel            text        NOT NULL DEFAULT 'whatsapp',
          channel_identifier text        NOT NULL,
          display_name       text,
          created_at         timestamptz NOT NULL DEFAULT now(),
          last_seen_at       timestamptz,
          UNIQUE (business_id, channel, channel_identifier)
        )
    """)

    # ------------------------------------------------------------------
    # 7. MEMORIA DE CLIENTE
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE customer_memory (
          customer_id uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
          business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          profile     jsonb       NOT NULL DEFAULT '{}'::jsonb,
          summary     text,
          updated_at  timestamptz NOT NULL DEFAULT now()
        )
    """)

    # ------------------------------------------------------------------
    # 8. CONVERSACIONES (estado de sesion, sin Redis)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE conversations (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
          channel         text        NOT NULL DEFAULT 'whatsapp',
          status          text        NOT NULL DEFAULT 'auto',
          active_agent_id uuid        REFERENCES agents(id),
          flow_state      jsonb       NOT NULL DEFAULT '{}'::jsonb,
          created_at      timestamptz NOT NULL DEFAULT now(),
          last_message_at timestamptz,
          updated_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_conversations_customer "
        "ON conversations (customer_id, last_message_at DESC)"
    )

    # ------------------------------------------------------------------
    # 9. MENSAJES
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE messages (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          conversation_id uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          role            text        NOT NULL,
          content         text        NOT NULL,
          created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at)")

    # ------------------------------------------------------------------
    # 10. LOG DE TURNOS (costo/observabilidad, RNF-02)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE turn_logs (
          id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id       uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          conversation_id   uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          message_in_id     uuid        REFERENCES messages(id),
          message_out_id    uuid        REFERENCES messages(id),
          detected_intent   text,
          routed_agent_id   uuid        REFERENCES agents(id),
          tools_used        jsonb       NOT NULL DEFAULT '[]'::jsonb,
          model_calls       int         NOT NULL DEFAULT 0,
          input_tokens      int         NOT NULL DEFAULT 0,
          output_tokens     int         NOT NULL DEFAULT 0,
          cache_read_tokens int         NOT NULL DEFAULT 0,
          cost_estimate_usd numeric(10,6),
          latency_ms        int,
          created_at        timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_turnlogs_business_time ON turn_logs (business_id, created_at)")

    # ------------------------------------------------------------------
    # 11. SOLICITUDES CAPTURADAS (F2 / ACT)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE captured_requests (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
          conversation_id uuid        REFERENCES conversations(id) ON DELETE SET NULL,
          request_type    text        NOT NULL,
          structured_data jsonb       NOT NULL DEFAULT '{}'::jsonb,
          status          text        NOT NULL DEFAULT 'new',
          created_at      timestamptz NOT NULL DEFAULT now(),
          updated_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute(
        "CREATE INDEX idx_requests_business_status "
        "ON captured_requests (business_id, status, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # 12. ESCALAMIENTOS (F6)
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE escalations (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
          conversation_id uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
          reason          text,
          status          text        NOT NULL DEFAULT 'open',
          created_at      timestamptz NOT NULL DEFAULT now(),
          resolved_at     timestamptz,
          resolved_by     uuid        REFERENCES admin_users(id)
        )
    """)
    op.execute(
        "CREATE INDEX idx_escalations_business_status "
        "ON escalations (business_id, status, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # updated_at automatico
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
        BEGIN
          NEW.updated_at = now();
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in (
        "businesses",
        "agents",
        "knowledge_documents",
        "customer_memory",
        "conversations",
        "captured_requests",
    ):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
              BEFORE UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION set_updated_at()
        """)

    # ------------------------------------------------------------------
    # Row-Level Security (RNF-04, capa 2)
    # ------------------------------------------------------------------
    # FORCE es imprescindible: sin el, el dueno de la tabla (el rol con el que
    # corre la app en local) omite las politicas y el aislamiento no aplica.
    #
    # OJO: ni ENABLE ni FORCE aplican a un rol SUPERUSER o con BYPASSRLS. Si la
    # app se conecta con un rol asi, esta capa queda INERTE sin ningun error
    # visible. Por eso la migracion 0002 crea el rol de aplicacion sin esos
    # atributos; DATABASE_URL debe apuntar a ese rol, nunca al superusuario.
    #
    # La politica usa el modo permisivo de current_setting (segundo arg true):
    # si la variable no esta fijada devuelve NULL y la comparacion es falsa,
    # es decir, sin tenant fijado no se ve ninguna fila.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (business_id = current_setting('app.current_business_id', true)::uuid)
              WITH CHECK (business_id = current_setting('app.current_business_id', true)::uuid)
        """)

    # businesses se filtra por su propia PK (no tiene business_id).
    op.execute("ALTER TABLE businesses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE businesses FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON businesses
          USING (id = current_setting('app.current_business_id', true)::uuid)
          WITH CHECK (id = current_setting('app.current_business_id', true)::uuid)
    """)


def downgrade() -> None:
    for table in (
        "escalations",
        "captured_requests",
        "turn_logs",
        "messages",
        "conversations",
        "customer_memory",
        "customers",
        "knowledge_chunks",
        "knowledge_documents",
        "agents",
        "admin_users",
        "businesses",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS set_updated_at()")
