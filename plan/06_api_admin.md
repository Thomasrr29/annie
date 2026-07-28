# Fase 6 — API Admin: configuración, bandejas y sandbox

## Contexto

"Onboarding = llenar un formulario" es el producto tanto como el agente. Esta fase construye la API que consumirá el panel: configuración del negocio, agentes, conocimiento, bandejas de trabajo y el sandbox de prueba. (El frontend del panel es proyecto aparte; aquí se entrega la API completa y documentada por OpenAPI.)

## Cubre

- RF-18..RF-23 (perfil, conocimiento, agentes, bandejas, métricas, sandbox), RF-06 (alta de agente = INSERT, cero cambios de código).
- HU-ADM-01..HU-ADM-08.

## Tareas

1. Autenticación del panel: tokens por `admin_users` (magic link o password simple para MVP), y dependencia `current_business` que fija el contexto RLS.
2. `app/api/admin.py`:
   - Negocio: `GET/PATCH /admin/business` (nombre, tono, horarios, idioma, notify_config).
   - Agentes: `GET/POST/PATCH /admin/agents`, activar/desactivar. Validar `allowed_tools` contra el catálogo real de herramientas.
   - Conocimiento: `GET/POST/DELETE /admin/knowledge` (texto/FAQ). Al cambiar, invalidar el `knowledge_context` ensamblado (y con ello, naturalmente, la caché de prompt se renueva).
   - Solicitudes: `GET /admin/requests?status=` + `PATCH /admin/requests/{id}` (seen/in_progress/done).
   - Escalamientos: `GET /admin/escalations` + `POST /admin/escalations/{id}/resolve` (+ opcional: devolver conversación a `status='auto'`, RF-14).
   - Responder como humano: `POST /admin/conversations/{id}/reply` (envía por WhatsApp con rol `human`).
   - Métricas: `GET /admin/metrics?from=&to=` sobre `turn_logs` (volumen, % sin humano, escalamientos, costo, latencia media).
3. **Sandbox (HU-ADM-05):** `POST /admin/sandbox/message` — ejecuta el MISMO orquestador con un canal falso "sandbox" y un cliente de prueba, sin tocar WhatsApp; devuelve la respuesta + intención detectada + herramientas invocadas (transparencia para depurar).
4. Validación de curación al subir conocimiento: recordatorio/confirmación en la API ("¿este contenido puede verlo cualquier cliente?") — campo `confirmed_public: true` requerido.

## Criterios de aceptación

- Crear un agente por API y, sin redeploy, el orquestador lo enruta en el siguiente mensaje (test de integración).
- Editar conocimiento cambia las respuestas del sandbox inmediatamente.
- Un admin del tenant A recibe 404/403 en recursos del tenant B (tests).
- El sandbox reproduce los 7 flujos sin canal real.

## Definición de Terminado

OpenAPI completo y probado; tests de autorización multi-tenant verdes; sandbox usable para el guion E2E de la fase 5 sin teléfono.
