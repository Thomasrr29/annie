# Fase 4 — Capa LLM: cliente Anthropic, caché y tiering

## Contexto

El costo por conversación es requisito de producto (RNF-02), no optimización posterior. Esta capa centraliza TODA llamada a Anthropic: ensamblado del prompt con prefijo cacheable, tiering Haiku/Sonnet, tool use, y captura de métricas de tokens. Nadie más en el código llama a la API directamente.

## Cubre

- RNF-02 (caché + tiering + mínimo de llamadas), RNF-07 (métricas de tokens/costo), base de RF-05 y RF-07.

## Estructura del prompt (contrato inmutable)

```
[ system: instructions_md del agente + persona del negocio + knowledge_context ]  <- PREFIJO ESTABLE
[ definiciones de herramientas permitidas (allowed_tools) ]                        <- PREFIJO ESTABLE
--- cache_control (breakpoint) ---
[ últimos N turnos + flow_state ]                                                  <- DINÁMICO
[ mensaje nuevo del cliente ]                                                      <- DINÁMICO
```

Prohibido en el prefijo: timestamps, ids de sesión, cualquier contenido que cambie por turno (invalida la caché). El `knowledge_context` debe ser un string estable (garantizado por fase 2).

## Tareas

1. `app/core/llm.py`:
   - `classify(message, ctx, agents) -> Intent + agent_id | None` — con **Haiku**, prompt corto, salida estructurada (JSON validado con Pydantic; reintento único si el JSON es inválido).
   - `run_agent_turn(agent_config, business_config, ctx, message, tools) -> HandlerResult` — con **Sonnet**, tool use loop: si el modelo pide una herramienta permitida, ejecutarla y continuar; si pide una NO permitida, bloquear y loguear (RF-07 se aplica no incluyéndola en el request, este chequeo es cinturón extra).
   - Marcado de `cache_control` en el bloque correcto del prefijo.
   - Captura de `usage` de cada respuesta: input, output, cache_read, cache_creation → devolver junto al resultado para `turn_logs`.
2. Cálculo de costo estimado por turno (precios por modelo en config, no hardcodeados).
3. Manejo de errores: timeout/overload de la API → excepción tipada que el orquestador convierte en respuesta honesta + oferta de escalar (RNF-10). Reintento único con backoff para errores transitorios.
4. Config por entorno: nombres de modelo en variables/settings (los modelos rotan; no hardcodear en el código).

## Criterios de aceptación

- Tests unitarios con el cliente Anthropic mockeado: ensamblado del prompt coloca el breakpoint tras el prefijo; el prefijo es byte-idéntico entre dos turnos del mismo negocio.
- Test: herramienta no permitida nunca aparece en el request de la API.
- Test de integración real (opcional, marcado `@slow`): dos llamadas seguidas al mismo negocio muestran `cache_read_tokens > 0` en la segunda.

## Definición de Terminado

Toda llamada a Anthropic pasa por este módulo; métricas de tokens fluyen hacia el llamador; tests verdes.
