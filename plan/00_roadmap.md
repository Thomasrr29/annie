# Plan de Implementación — Roadmap

## Orden de fases y dependencias

| Fase | Archivo | Entrega | Depende de |
|------|---------|---------|------------|
| 1 | `01_fundacion_proyecto_db.md` | Proyecto FastAPI corriendo + DB migrada + RLS | — |
| 2 | `02_capa_datos.md` | Modelos SQLAlchemy + repositorios async con aislamiento probado | 1 |
| 3 | `03_canal_whatsapp.md` | Webhook con ACK inmediato + adaptador + envío saliente | 2 |
| 4 | `04_capa_llm.md` | Cliente Anthropic con caché de prompt + tiering Haiku/Sonnet | 2 |
| 5 | `05_orquestador.md` | Loop completo del turno (F1–F7) con herramientas | 3, 4 |
| 6 | `06_api_admin.md` | CRUD de configuración + bandejas + sandbox de prueba | 2, 5 |
| 7 | `07_observabilidad_cierre.md` | Métricas, costos, hardening, piloto | 5, 6 |

## Regla de avance

Una fase está terminada solo cuando cumple su **Definición de Terminado** (tests incluidos). No avanzar con deuda de la fase anterior.

## Hito de validación del MVP

Al cerrar la fase 7: onboarding de **un negocio piloto real** (configuración por formulario, conocimiento cargado, número de WhatsApp conectado) y medición de una semana: % resuelto sin humano, escalamientos, costo por conversación (con `cache_read_tokens`).

## Trazabilidad

Cada fase referencia los RF/RNF/HU de `docs/MVP_Agente_Soporte_Alcance_Requerimientos.md` que cubre. Al terminar el plan completo, todos los RF-01..RF-24 y RNF-01..RNF-10 deben estar cubiertos o explícitamente diferidos.

## Decisiones abiertas (resolver durante las fases, no antes)

- **Fase 3:** estrategia de mapeo número de WhatsApp → `business_id` (número propio por negocio vía BSP vs. número compartido). Resolver con el BSP elegido.
- **Fase 3:** elección del BSP (360dialog / Wati / otro). Criterio: velocidad de verificación + webhook limpio + costo.
- **Fase 4:** umbral de tokens por tenant para alertar "este negocio ya debería migrar a RAG" (solo alerta; la migración es post-MVP).
- **Fase 7:** política de retención de `messages`/`turn_logs` (Ley 1581) antes del piloto.
