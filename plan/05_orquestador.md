# Fase 5 — Orquestador: el loop del turno (F1–F7) y herramientas

## Contexto

El corazón del producto. Implementa `handle_inbound_message` completo (8 pasos del scaffold) conectando canal (fase 3), datos (fase 2) y LLM (fase 4). Cubre los 7 flujos conversacionales. Aquí se decide si el producto se siente inteligente o roto.

## Cubre

- RF-04..RF-14 (contexto, clasificación, enrutamiento, allowlist, CLARIFY/CONTINUE, herramientas, escalamiento), RNF-09 (idioma), RNF-10 (degradar con gracia).
- HU-USR-01..HU-USR-07 completas.

## Tareas

1. `app/core/orchestrator.py` — loop del turno:
   1. `resolve_customer_and_conversation` (upsert cliente, conversación activa, guardar entrante, cargar últimos N + memoria).
   2. Si `status == 'human'` → return silencioso (F6 activo).
   3. **Atajo CONTINUE (F5):** si hay `active_agent_id` y `flow_state` no vacío → saltar clasificación, continuar el flujo con el nuevo dato.
   4. `classify_and_route` con Haiku sobre `routing_description` de agentes activos (F1..F7). Sin agente que haga match para INFORM/ACT → FALLBACK.
   5. `run_handler` según intención (tabla abajo).
   6. Persistir: respuesta en `messages`, log en `turn_logs`, efectos (`flow_state`, escalations, captured_requests), actualización deliberada de memoria si aplica.
   7. `send_whatsapp_reply`.
2. `app/core/tools.py`: `capture_request`, `notify_owner` (envío según `notify_config`: correo o mensaje al número del dueño), stub `search_knowledge` (diferida: base completa).
3. Comportamiento por intención:

| Intención | Handler |
|-----------|---------|
| GREET | Sonnet, sin herramientas, respuesta breve en el tono del negocio |
| INFORM | Sonnet; el conocimiento YA está en el prefijo; responder solo con lo que está ahí; si no está → FALLBACK honesto |
| ACT | Sonnet con `capture_request`+`notify_owner`; recolectar campos de a uno vía `flow_state`; confirmar al cliente al completar |
| CLARIFY | Una sola pregunta; guardar lo entendido en `flow_state` |
| CONTINUE | Retomar `active_agent_id` + `flow_state` sin re-clasificar |
| ESCALATE | `status='human'`, INSERT `escalations`, `notify_owner`, avisar al cliente que una persona lo atenderá |
| FALLBACK | Honesto: qué SÍ puede hacer + ofrecer escalar; JAMÁS inventar |

4. Disparadores de ESCALATE además del pedido explícito: enojo evidente, o N fallos consecutivos del agente en la misma conversación (contador en `flow_state`).
5. Resiliencia: cualquier excepción del LLM/herramientas → respuesta honesta de error + ofrecer humano; el turno SIEMPRE se loguea.

## Criterios de aceptación

- Tests de integración por flujo (F1–F7) con LLM mockeado: cada uno verifica las escrituras correctas en DB (tabla↔flujo de `docs/MVP_Estructura_Base_de_Datos.md` §5).
- Test CONTINUE: dos mensajes seguidos completan una solicitud sin re-clasificar el segundo.
- Test modo humano: con `status='human'` no se genera respuesta automática.
- Test allowlist: agente sin `capture_request` no puede capturar solicitudes.
- Prueba E2E manual guiada: guion de 10 conversaciones contra el negocio demo (seeds), cubriendo los 7 flujos, por WhatsApp real.

## Definición de Terminado

Los 7 flujos pasan sus tests; el guion E2E manual se completa sin respuestas inventadas ni errores no manejados.
