# Fase 7 — Observabilidad, hardening y piloto

## Contexto

El MVP se valida con un negocio real y números reales. Esta fase cierra lo transversal: métricas de costo visibles, límites de protección, privacidad, y el piloto de una semana.

## Cubre

- RNF-05 (privacidad/Ley 1581), RNF-06 (disponibilidad), RNF-07 (observabilidad), riesgo #1 (costos) del documento de alcance. Métricas de éxito §10.

## Tareas

1. **Dashboard de costo interno:** consulta sobre `turn_logs` por tenant/día: turnos, tokens, % cache hit (`cache_read/(input+cache_read)`), costo estimado, latencia p50/p95. Alertas simples: costo diario por tenant > umbral; % cache hit < umbral (indica prefijo roto = bug caro).
2. **Límites de protección:** rate limit por cliente final (mensajes/minuto) y tope diario de turnos por tenant (circuit breaker de costo). Mensaje amable al exceder.
3. **Privacidad (Ley 1581):** texto de aviso de tratamiento de datos en el primer contacto con cada cliente nuevo; endpoint interno de eliminación de datos de un cliente por solicitud (borra messages/memory/requests del cliente); política de retención definida y documentada. *(Validar textos con asesoría legal antes del piloto.)*
4. **Logging estructurado** (JSON) con `business_id`/`conversation_id` en cada evento; sin contenido de mensajes en logs de aplicación (el contenido ya vive en la DB).
5. **Despliegue:** entorno productivo pequeño (una instancia + Postgres gestionado), backups automáticos de la DB, healthcheck monitoreado.
6. **Piloto:** onboarding de 1 negocio real por formulario. Una semana de operación. Recolectar: % resuelto sin humano, escalamientos y motivos, costo/conversación, feedback del dueño.

## Criterios de aceptación

- El dashboard muestra el costo real del piloto por día y el % de cache hit es > 60% sostenido (si no, hay bug de prefijo).
- Simulación de flood: el rate limit contiene el costo sin tumbar el servicio.
- El endpoint de eliminación borra los datos de un cliente y queda evidencia del cumplimiento.
- Piloto completado con métricas de la semana documentadas.

## Definición de Terminado (= MVP terminado)

Piloto operado una semana con números recolectados y una decisión escrita: iterar qué, escalar a más negocios, o ajustar alcance. Los RF/RNF del documento de alcance están todos cubiertos o marcados como diferidos con razón.
