# Fase 3 — Canal WhatsApp: webhook, adaptador y envío

## Contexto

WhatsApp exige ACK rápido del webhook (reintenta si no). Patrón: el webhook valida, normaliza a `InboundMessage`, dispara el procesamiento en segundo plano y responde 200 de inmediato. La respuesta al cliente sale por llamada SALIENTE a la API del BSP, dentro de la ventana de servicio de 24h (gratis). El adaptador es la única pieza que conoce el formato de WhatsApp: el resto del sistema es agnóstico al canal (preparado para multicanal futuro).

## Cubre

- RF-01 (ACK inmediato + async), RF-02 (formato común), RNF-01 (latencia), RNF-10 (resiliencia).

## Decisiones a cerrar en esta fase

- **BSP:** elegir (360dialog / Wati / otro) y crear cuenta sandbox.
- **Mapeo número→tenant:** el número de WhatsApp destino identifica al `business_id`. Implementar tabla o campo de mapeo (p. ej. `businesses.whatsapp_number`) y resolverlo en el adaptador.

## Tareas

1. `app/core/channel_whatsapp.py`:
   - `adapt_whatsapp_payload(payload) -> InboundMessage` (texto; ignorar tipos no soportados con respuesta amable "por ahora solo puedo leer texto").
   - `send_whatsapp_reply(OutboundMessage)` vía API del BSP (httpx async, reintentos con backoff).
   - Verificación de firma/token del webhook del BSP.
2. `app/api/webhooks.py`: `POST /webhooks/whatsapp` con `BackgroundTasks` → `handle_inbound_message` (stub hasta fase 5: por ahora, eco simple para probar el circuito completo).
3. Idempotencia: deduplicar por `external_message_id` (WhatsApp puede reentregar). Guardarlo en `messages` o tabla de deduplicación corta.
4. Manejo de errores: si el procesamiento falla, el webhook YA respondió 200; loguear y (si aplica) responder al cliente con mensaje honesto de error.

## Criterios de aceptación

- Test: el webhook responde 200 en <500 ms aunque el handler tarde (handler simulado lento).
- Test: payload duplicado (mismo `external_message_id`) no genera doble procesamiento.
- Prueba manual E2E en sandbox del BSP: mensaje real de WhatsApp → eco de vuelta al teléfono.

## Definición de Terminado

Circuito completo probado con un teléfono real contra el sandbox del BSP; deduplicación y verificación de firma con tests.
