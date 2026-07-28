# Agente de Soporte Inteligente — MVP

## Qué es este proyecto

Plataforma SaaS **multi-tenant** de agentes conversacionales de atención al cliente para **pymes** (LatAm, español primero). El agente atiende por **WhatsApp**: responde preguntas con el conocimiento del negocio y captura solicitudes estructuradas para el dueño, con escalamiento a humano.

**Métrica de éxito:** tiempo ahorrado al negocio y % de conversaciones resueltas sin humano, con **costo por conversación bajo y predecible**.

## Documentos fuente (leer antes de implementar)

- `docs/MVP_Agente_Soporte_Alcance_Requerimientos.md` — flujos F1–F7, RF-01..RF-24, RNF-01..RNF-10, HU-ADM-XX / HU-USR-XX.
- `docs/MVP_Estructura_Base_de_Datos.md` — DDL completo (12 tablas), aislamiento multi-tenant, mapeo tabla↔flujo.
- `app/scaffold_referencia.py` — scaffold de la API con tipados Pydantic, endpoints y firmas del orquestador. Es el contrato de diseño; la implementación real se organiza en módulos (ver estructura abajo).
- `plan/` — plan de implementación por fases, en orden.

## Decisiones de arquitectura (NO reabrir sin causa justificada)

1. **Stack:** Python 3.12+ / FastAPI / Pydantic v2 / PostgreSQL + pgvector / SQLAlchemy async + asyncpg. Todo async; NUNCA drivers síncronos en endpoints async.
2. **Sin frameworks de agentes.** NO usar Claude Agent SDK ni LangGraph en el MVP. El orquestador es un loop delgado propio sobre la API de Anthropic (Messages + tool use). Razones: una sola fuente de verdad de estado (nuestra DB), control exacto de llamadas/caché/costo, sin dependencia que se mueve rápido en el corazón del producto. LangGraph queda reservado para flujos futuros con estado de larga duración (`agents.handler_type='flow'`).
3. **Un motor, N configuraciones.** Un "agente" es un registro en la tabla `agents` (routing_description + instructions_md + allowed_tools + memory_scope), NO un proceso ni un archivo. El orquestador es genérico y agnóstico al tenant; enruta leyendo los agentes activos del negocio en runtime. Agregar un agente = INSERT. Nunca editar código para enrutar.
4. **Modelos:** Haiku para clasificar/enrutar (barato, rápido); Sonnet para componer respuestas y razonar. Registrar tokens y `cache_read_tokens` en `turn_logs` SIEMPRE.
5. **Conocimiento: "base completa" (sin RAG) para el MVP.** El conocimiento estable del negocio va COMPLETO en el prefijo cacheado del system prompt. `search_knowledge`/`knowledge_chunks` existen en el esquema pero quedan diferidos hasta que un tenant crezca. Regla de curación: NADA entra a la base de conocimiento que no sea aceptable que un cliente vea.
6. **Prompt caching obligatorio (RNF-02).** Estructura del prompt: [system + instrucciones + herramientas + knowledge_context] = prefijo estable cacheado → breakpoint `cache_control` → [historial reciente + flow_state + mensaje nuevo] = dinámico. JAMÁS meter contenido dinámico (timestamps, ids de sesión) en el prefijo: invalida la caché.
7. **Webhook con ACK inmediato.** El webhook de WhatsApp responde 200 al instante y procesa en segundo plano. La respuesta al cliente sale por llamada SALIENTE a la API de WhatsApp (ventana de 24h = gratis). Sin plantillas salientes ni mensajes proactivos en el MVP.
8. **Aislamiento multi-tenant en DOS capas (RNF-04):** (a) toda consulta filtra por `business_id` a nivel de aplicación — las herramientas construyen las consultas, el LLM NUNCA arma SQL; (b) Row-Level Security en Postgres como respaldo (`SET LOCAL app.current_business_id`). El allowlist `allowed_tools` se aplica EN CÓDIGO (no pasando la herramienta al modelo), no en el .md.
9. **Estado de sesión en Postgres, sin Redis.** `conversations` (status, active_agent_id, flow_state) es el estado. Redis solo si la latencia lo exige con datos reales.
10. **El historial NUNCA alimenta el RAG/conocimiento.** `messages` y `turn_logs` no se indexan. `customer_memory` se actualiza de forma deliberada (resumen curado), nunca volcado automático.
11. **Fuera de alcance MVP:** pagos, integraciones con APIs externas del cliente (tier 2 concierge), constructor self-service de herramientas (tier 3), multicanal activo (solo WhatsApp; el adaptador de canal queda en la arquitectura), resolución de identidad entre canales, caché semántico de resultados, búsqueda híbrida/reranking.

## Los 7 flujos conversacionales (F1–F7)

INFORM (responder con conocimiento) · ACT (capturar solicitud + notificar) · GREET (social, sin herramientas) · CLARIFY (una pregunta, re-clasificar) · CONTINUE (seguir flujo activo vía `flow_state`, sin re-clasificar) · ESCALATE (status='human', el agente calla, bandeja + notificación) · FALLBACK (honesto, nunca inventar, ofrecer escalar).

## Herramientas integradas del MVP

- `capture_request(business_id, customer_id, conversation_id, data)` → W `captured_requests`
- `notify_owner(business_id, subject, body)` → R `businesses.notify_config`, envío externo
- `search_knowledge(...)` → DIFERIDA (base completa); dejar stub

## Estructura de código objetivo

```
app/
  main.py               # FastAPI app + routers
  schemas.py            # modelos Pydantic (partir del scaffold)
  api/
    webhooks.py         # runtime: POST /webhooks/whatsapp
    admin.py            # panel: /admin/* (agents, knowledge, requests, escalations, metrics)
  core/
    orchestrator.py     # loop del turno (8 pasos, ver scaffold)
    tools.py            # capture_request, notify_owner (+ stub search_knowledge)
    llm.py              # cliente Anthropic: Haiku/Sonnet, prompt assembly, cache_control
    channel_whatsapp.py # adaptador: payload crudo <-> InboundMessage/OutboundMessage
  db/
    models.py           # SQLAlchemy (espejo del DDL en docs/)
    repositories.py     # acceso a datos async, SIEMPRE filtrado por business_id
    session.py          # engine async + SET LOCAL app.current_business_id
migrations/             # Alembic
tests/
```

## Convenciones

- Español para docstrings de dominio y mensajes al usuario; inglés para nombres de código.
- Tipado estricto: Pydantic en fronteras, type hints en todo; `mypy` debe pasar.
- Tests: pytest + pytest-asyncio. Cada flujo F1–F7 tiene al menos un test de integración del loop con el LLM mockeado. El aislamiento multi-tenant tiene tests dedicados (intentar leer datos de otro tenant DEBE fallar).
- Errores: degradar con gracia (RNF-10) — ante fallo de herramienta o LLM, respuesta honesta + ofrecer escalar; nunca romper la conversación ni dejar el webhook sin ACK.
- Secretos por variables de entorno (`ANTHROPIC_API_KEY`, `DATABASE_URL`, credenciales BSP). Nunca en el repo.
- No agregar dependencias sin justificarlas en el PR (principio: lo que el equipo pueda mantener en 6 meses).

## Cómo trabajar el plan

Ejecutar las fases de `plan/` EN ORDEN (00 → 07). Cada fase lista: contexto, requerimientos (RF/RNF) y HU que cubre, tareas, criterios de aceptación y definición de terminado. No empezar una fase sin cumplir la definición de terminado de la anterior. Ante ambigüedad, la fuente de verdad es `docs/` y este archivo.
