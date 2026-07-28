# Fase 2 — Capa de datos: modelos y repositorios

## Contexto

El orquestador y la API admin no tocan SQL directamente: usan repositorios async que SIEMPRE filtran por `business_id` (capa 1 del aislamiento; RLS es la capa 2). Aquí se implementa el mapa tabla↔flujo de `docs/MVP_Estructura_Base_de_Datos.md` §4–5.

## Cubre

- RF-03 (identidad por negocio+teléfono), RF-15..RF-17 (persistencia), RF-24 (aislamiento), RNF-04.

## Tareas

1. `app/db/models.py`: modelos SQLAlchemy espejo del DDL (12 tablas).
2. `app/schemas.py`: portar los modelos Pydantic del scaffold (`app/scaffold_referencia.py`) — InboundMessage, ConversationContext, AgentConfig, BusinessConfig, HandlerResult, etc.
3. `app/db/repositories.py` con repositorios por agregado. Métodos mínimos:
   - `CustomerRepo.get_or_create(business_id, channel, channel_identifier)`
   - `ConversationRepo.get_or_create_active(customer_id)` / `set_status()` / `update_flow_state()` / `set_active_agent()`
   - `MessageRepo.append(conversation_id, role, content)` / `recent(conversation_id, n)`
   - `MemoryRepo.get(customer_id)` / `upsert_deliberate(customer_id, profile, summary)`
   - `AgentRepo.list_active(business_id)`
   - `BusinessRepo.get_config(business_id)` (incluye ensamblar `knowledge_context` desde `knowledge_documents`)
   - `RequestRepo.create(...)` / `list_by_status(...)`
   - `EscalationRepo.create(...)` / `resolve(...)`
   - `TurnLogRepo.append(...)` (tokens, cache_read_tokens, costo, latencia, intención, herramientas)
4. Regla de oro en revisión: ningún método acepta consultas sin `business_id` resuelto; el LLM jamás construye SQL.
5. Seeds de desarrollo: script que crea un negocio demo + 2 agentes + conocimiento de ejemplo.

## Criterios de aceptación

- Tests de repositorio por método (DB real en Docker, no mocks de SQL).
- Test explícito de aislamiento a nivel repositorio: `CustomerRepo` del tenant A no puede leer clientes del tenant B (además del test RLS de fase 1).
- `BusinessRepo.get_config` concatena el conocimiento en un solo `knowledge_context` estable (mismo input ⇒ mismo string, requisito para caché de prompt).

## Definición de Terminado

Cobertura de tests sobre repositorios, seeds funcionando, mypy verde.
