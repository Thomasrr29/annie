# Estructura de Base de Datos — MVP Agente de Soporte

**Versión:** 1.0
**Motor:** PostgreSQL (una sola base) + extensión `pgvector`.
**Complementa:** `MVP_Agente_Soporte_Alcance_Requerimientos.md` (flujos F1–F7, RF-XX, RNF-XX).

---

## 1. Principios de diseño de datos

1. **Una sola base de datos** cubre los cuatro almacenes lógicos. Se modelan como interfaces separadas en el código para poder separarlos físicamente después, pero corren en un solo Postgres (RNF-03).
2. **`business_id` en cada tabla** es la frontera de aislamiento. Todo dato pertenece a un tenant y solo es accesible dentro de ese tenant (RF-24, RNF-04).
3. **Estado de sesión sin Redis.** El estado "dónde vamos en esta conversación" vive en `conversations` (`status`, `active_agent_id`, `flow_state`). Redis se difiere hasta que la latencia lo exija.
4. **El historial no alimenta el RAG.** `messages` y `turn_logs` nunca se indexan como conocimiento. El único almacén vectorial es `knowledge_chunks`.
5. **La memoria de cliente es un lookup, no una búsqueda semántica.** Se recupera por `customer_id`; es un registro estructurado, no un embedding.

### 1.1 Mapa almacén lógico → tablas

| Almacén lógico | Tablas | Acceso |
|----------------|--------|--------|
| Base de conocimiento (RAG) | `knowledge_documents`, `knowledge_chunks` | Búsqueda vectorial |
| Historial de conversación | `conversations`, `messages` | Lookup por clave |
| Memoria de cliente | `customers`, `customer_memory` | Lookup por clave |
| Log de solicitudes/respuestas | `turn_logs`, `captured_requests`, `escalations` | Escritura / analítica |
| Configuración del tenant | `businesses`, `admin_users`, `agents` | Lookup por clave |

---

## 2. DDL completo (PostgreSQL)

```sql
-- ============================================================
-- Extensiones
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector

-- ============================================================
-- 1. NEGOCIOS (tenant)
-- ============================================================
CREATE TABLE businesses (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name           text        NOT NULL,
  persona_tone   text,                         -- tono/personalidad del agente
  language       text        NOT NULL DEFAULT 'es',
  timezone       text        NOT NULL DEFAULT 'America/Bogota',
  business_hours jsonb       NOT NULL DEFAULT '{}'::jsonb,
  notify_channel text,                         -- 'whatsapp_group' | 'email' | ...
  notify_config  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  status         text        NOT NULL DEFAULT 'active',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 2. USUARIOS ADMIN (operadores del negocio)
-- ============================================================
CREATE TABLE admin_users (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id  uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  email        text        NOT NULL,
  display_name text,
  role         text        NOT NULL DEFAULT 'operator',  -- 'owner' | 'operator'
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (business_id, email)
);

-- ============================================================
-- 3. AGENTES (configuración enrutable = el "cerebro")
-- ============================================================
CREATE TABLE agents (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id         uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  name                text        NOT NULL,
  routing_description text        NOT NULL,          -- lo que lee el orquestador para enrutar
  instructions_md     text        NOT NULL,          -- cuerpo .md (tarea/persona)
  allowed_tools       text[]      NOT NULL DEFAULT '{}',  -- allowlist de herramientas
  memory_scope        text        NOT NULL DEFAULT 'customer', -- 'none' | 'customer'
  response_style      text,
  handler_type        text        NOT NULL DEFAULT 'llm', -- 'llm' | 'flow' (futuro)
  status              text        NOT NULL DEFAULT 'active', -- 'active' | 'inactive'
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);
-- El orquestador solo carga agentes activos del tenant (RF-06)
CREATE INDEX idx_agents_business_active ON agents (business_id) WHERE status = 'active';

-- ============================================================
-- 4. DOCUMENTOS DE CONOCIMIENTO (fuente)
-- ============================================================
CREATE TABLE knowledge_documents (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  title       text        NOT NULL,
  source      text,                              -- 'upload' | 'faq' | 'manual'
  raw_content text,
  status      text        NOT NULL DEFAULT 'pending', -- 'pending' | 'indexed'
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 5. CHUNKS DE CONOCIMIENTO (RAG / vectorial)
-- ============================================================
CREATE TABLE knowledge_chunks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  document_id uuid        NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
  content     text        NOT NULL,
  embedding   vector(1024),                      -- dimensión según modelo de embeddings
  metadata    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);
-- Índice vectorial para búsqueda semántica (F1)
CREATE INDEX idx_chunks_embedding ON knowledge_chunks
  USING hnsw (embedding vector_cosine_ops);
-- Filtro por tenant en la búsqueda
CREATE INDEX idx_chunks_business ON knowledge_chunks (business_id);

-- ============================================================
-- 6. CLIENTES (identidad por canal)
-- ============================================================
CREATE TABLE customers (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id        uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  channel            text        NOT NULL DEFAULT 'whatsapp',
  channel_identifier text        NOT NULL,       -- número de teléfono en WhatsApp
  display_name       text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_at       timestamptz,
  -- Clave de identidad: un cliente por (negocio, canal, identificador)
  UNIQUE (business_id, channel, channel_identifier)
);

-- ============================================================
-- 7. MEMORIA DE CLIENTE (1:1, estructurada, NO vectorial)
-- ============================================================
CREATE TABLE customer_memory (
  customer_id uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  business_id uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  profile     jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- nombre, preferencias
  summary     text,                                       -- resumen deliberado
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- 8. CONVERSACIONES (estado de sesión, sin Redis)
-- ============================================================
CREATE TABLE conversations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  channel         text        NOT NULL DEFAULT 'whatsapp',
  status          text        NOT NULL DEFAULT 'auto',  -- 'auto' | 'human'
  active_agent_id uuid        REFERENCES agents(id),     -- flujo/agente activo
  flow_state      jsonb       NOT NULL DEFAULT '{}'::jsonb, -- datos parciales (CONTINUE)
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_message_at timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_conversations_customer ON conversations (customer_id, last_message_at DESC);

-- ============================================================
-- 9. MENSAJES (historial de conversación)
-- ============================================================
CREATE TABLE messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  role            text        NOT NULL,  -- 'customer' | 'agent' | 'system' | 'human'
  content         text        NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
-- Recuperación de los últimos N turnos de una conversación
CREATE INDEX idx_messages_conversation ON messages (conversation_id, created_at);

-- ============================================================
-- 10. LOG DE TURNOS (auditoría + costo/observabilidad)
-- ============================================================
CREATE TABLE turn_logs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id       uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  conversation_id   uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_in_id     uuid        REFERENCES messages(id),
  message_out_id    uuid        REFERENCES messages(id),
  detected_intent   text,                         -- INFORM | ACT | GREET | ...
  routed_agent_id   uuid        REFERENCES agents(id),
  tools_used        jsonb       NOT NULL DEFAULT '[]'::jsonb,
  model_calls       int         NOT NULL DEFAULT 0,
  input_tokens      int         NOT NULL DEFAULT 0,
  output_tokens     int         NOT NULL DEFAULT 0,
  cache_read_tokens int         NOT NULL DEFAULT 0,  -- para medir ahorro de caché (RNF-02)
  cost_estimate_usd numeric(10,6),
  latency_ms        int,
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_turnlogs_business_time ON turn_logs (business_id, created_at);

-- ============================================================
-- 11. SOLICITUDES CAPTURADAS (F2 / ACT)
-- ============================================================
CREATE TABLE captured_requests (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id uuid        REFERENCES conversations(id) ON DELETE SET NULL,
  request_type    text        NOT NULL,           -- 'agenda' | 'pedido' | 'consulta' ...
  structured_data jsonb       NOT NULL DEFAULT '{}'::jsonb,
  status          text        NOT NULL DEFAULT 'new', -- 'new'|'seen'|'in_progress'|'done'
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_requests_business_status ON captured_requests (business_id, status, created_at DESC);

-- ============================================================
-- 12. ESCALAMIENTOS (F6)
-- ============================================================
CREATE TABLE escalations (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  business_id     uuid        NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
  conversation_id uuid        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  customer_id     uuid        NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  reason          text,
  status          text        NOT NULL DEFAULT 'open',  -- 'open' | 'resolved'
  created_at      timestamptz NOT NULL DEFAULT now(),
  resolved_at     timestamptz,
  resolved_by     uuid        REFERENCES admin_users(id)
);
CREATE INDEX idx_escalations_business_status ON escalations (business_id, status, created_at DESC);
```

---

## 3. Aislamiento multi-tenant (RNF-04)

Dos capas, defensa en profundidad:

**Capa 1 — filtro a nivel de aplicación (obligatorio).** Toda consulta incluye `WHERE business_id = $tenant`. Las herramientas construyen las consultas; el LLM nunca arma SQL.

**Capa 2 — Row-Level Security (respaldo).** Aunque una consulta olvide el filtro, la política de Postgres impide leer filas de otro tenant.

```sql
-- Ejemplo para 'messages'; replicar en toda tabla con business_id
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON messages
  USING (business_id = current_setting('app.current_business_id', true)::uuid);

-- La aplicación fija el tenant por transacción, al inicio de cada request:
--   SET LOCAL app.current_business_id = '<uuid-del-negocio>';
```

> Nota de escalamiento: el índice HNSW de `knowledge_chunks` es global; la búsqueda filtra por `business_id`. A escala de MVP funciona bien. Si un tenant crece mucho, se evalúa particionar o índices por tenant más adelante.

---

## 4. Cómo se usa la base en el loop del agente

Secuencia base que ocurre en **todo turno**, con lectura (R) / escritura (W):

| Paso del loop | Tablas | R/W |
|---------------|--------|-----|
| 1. Recepción del mensaje | `customers` (upsert por identidad), `conversations` (busca/crea activa), `messages` (mensaje entrante) | W |
| 2. Cargar contexto | `messages` (últimos N por conversación), `customer_memory` | R |
| 3. Clasificar intención (Haiku) | `agents` (lista activa del tenant) | R |
| 4. Enrutar al agente | `agents` (match por `routing_description`), `conversations.active_agent_id` | R/W |
| 5. Ejecutar handler | *(depende del flujo — ver §5)* | — |
| 6. Componer respuesta (Sonnet) | `messages` (mensaje del agente); `customer_memory` (actualización deliberada, opcional) | W |
| 7. Persistir | `turn_logs` (intención, herramientas, tokens, costo, latencia) | W |

---

## 5. Mapeo tabla ↔ flujo (F1–F7)

Cada flujo agrega, sobre el loop base, sus lecturas/escrituras específicas.

### F1 — Consulta de información (INFORM)
- **R** `knowledge_chunks` — búsqueda vectorial filtrada por `business_id` (herramienta `search_knowledge`).
- Si no hay match suficiente → cae a F7.

### F2 — Ejecución/captura de acción (ACT)
- **W** `captured_requests` — inserta la solicitud estructurada (herramienta `capture_request`).
- **R** `businesses.notify_config` — para saber a dónde notificar (herramienta `notify_owner`).
- Los datos parciales que se van recolectando viven en `conversations.flow_state` hasta completar.

### F3 — Saludo / social (GREET)
- Solo el loop base. Sin herramientas ni acceso a conocimiento.

### F4 — Aclaración (CLARIFY)
- **W** `conversations.flow_state` — guarda lo entendido hasta ahora.
- Vuelve al paso 3 (clasificar) con la respuesta del cliente.

### F5 — Continuación dentro de un flujo (CONTINUE)
- **R/W** `conversations.flow_state` — lee el estado parcial, agrega el nuevo dato, continúa el flujo sin re-clasificar desde cero.
- **R** `conversations.active_agent_id` — para retomar el agente correcto.

### F6 — Escalamiento a humano (ESCALATE)
- **W** `conversations.status = 'human'` — el agente deja de responder.
- **W** `escalations` — registra motivo e historia.
- **R** `businesses.notify_config` + notificación al operador (`notify_owner`).

### F7 — Fallback / fuera de alcance
- Solo el loop base. Respuesta honesta; puede ofrecer escalar (deriva a F6).

---

## 6. Herramientas integradas y las tablas que tocan (RF-09 a RF-11)

| Herramienta | Lee | Escribe |
|-------------|-----|---------|
| `search_knowledge` | `knowledge_chunks` (filtrado por tenant) | — |
| `capture_request` | — | `captured_requests` |
| `notify_owner` | `businesses.notify_config` | *(envío externo; opcional registrar en `turn_logs`)* |

Cada herramienta valida `business_id` internamente. Un agente solo puede invocar las herramientas listadas en su `allowed_tools` (RF-07): esto se aplica **en código**, no en el `.md`.

---

## 7. Diferido (fuera del MVP)

- **Redis** como caché de sesiones activas: se agrega cuando la latencia lo justifique; `conversations` ya cubre el estado por ahora.
- **`handler_type = 'flow'`**: agentes con pasos deterministas/estado largo (LangGraph). El campo ya existe en `agents`; el runtime del MVP solo ejecuta `'llm'`.
- **Resolución de identidad entre canales**: unir `customers` de distintos canales requiere señal verificada; hoy cada canal es un `customer` independiente.
- **Memoria semántica de largo plazo** (embeddings de conversaciones resumidas): si se agrega, sería una tabla vectorial aparte y **curada**, nunca volcado automático de `messages`.
- **Retención / borrado (RNF-05)**: definir políticas de retención de `messages` y `turn_logs`, y mecanismo de eliminación por solicitud del titular (Ley 1581).

---

## 8. Notas de implementación

- **Embeddings:** `pgvector` guarda el vector, pero el modelo que lo genera es una decisión aparte (p. ej. Voyage AI, recomendado con Claude, u otro). La dimensión `vector(1024)` debe ajustarse al modelo elegido.
- **`updated_at`:** conviene un trigger que lo actualice automáticamente en `UPDATE`.
- **Índices:** los definidos cubren los accesos del loop; revisar con datos reales antes de agregar más.
- **Costo (RNF-02):** `turn_logs` guarda `input_tokens`, `output_tokens` y `cache_read_tokens` justamente para medir el ahorro de prompt caching y el costo por conversación.
