# ¿Dónde y cómo se guardan los datos de un negocio?

Todo vive en **una sola base PostgreSQL**. Cada tabla de negocio tiene la columna `business_id`, y Row-Level Security garantiza que cada negocio solo ve sus filas (ver `arquitectura_multitenant.md`).

## Las tablas, en tres grupos

```
CONFIGURACIÓN (lo que el dueño define: "quién es annie para este negocio")
  businesses ─────────── 1 fila por negocio: nombre, tono, idioma, horario,
  │                      número de WhatsApp, canal de aviso, tope diario
  ├── agents ─────────── N por negocio: cuándo atiende (routing_description),
  │                      instrucciones, herramientas permitidas, estilo
  ├── knowledge_documents  N por negocio: título + contenido (lo que annie sabe)
  │     └── knowledge_chunks  (diferida: para RAG cuando un negocio crezca)
  └── admin_users ─────── quién entra al panel

OPERACIÓN (lo que pasa en cada conversación)
  customers ──────────── 1 por número de WhatsApp por negocio
  ├── customer_memory ── resumen del cliente (hoy se lee, nunca se escribe)
  └── conversations ──── estado: auto/humano, agente activo, flow_state
        └── messages ─── cada mensaje, en orden
              └── message_media  imágenes (bytes en Postgres)

RESULTADOS (lo que el dueño y nosotros medimos)
  captured_requests ──── leads/solicitudes para el asesor
  turn_logs ──────────── 1 por turno: intención, agente, herramientas,
                         tokens, caché, costo, latencia
```

Definición completa: `app/db/models.py` y el DDL en `docs/MVP_Estructura_Base_de_Datos.md`.

## El camino del conocimiento: del archivo al prompt

```
datos_demo/nott/conocimiento.md
        │  (cargar: panel → página Conocimiento, o un script)
        ▼
knowledge_documents  ── una fila por sección "##"
   título: "Envíos"            contenido: "El envío es 100% gratis…"
   título: "Productos y precios" contenido: "Nott Tired — $39.900…"
        │
        │  en cada turno: build_knowledge_context()  (app/db/repositories.py:543)
        │  une todos los documentos del negocio, siempre en el mismo orden
        ▼
"## Envíos\n\nEl envío es 100% gratis…\n\n## Productos y precios\n\n…"
        │
        ▼
prefijo del system prompt (con cache_control)  ← igual en cada turno = caché barata
```

- **Siempre el mismo orden** (`created_at`, `id`): si el orden cambiara, el texto cambiaría y la caché se rompería.
- **Editar un documento** cambia el prefijo: la siguiente llamada vuelve a escribir la caché (cuesta un poco más una vez) y después vuelve a ser barata.
- **Detalle encontrado:** `knowledge_documents.status` existe (`pending` por defecto), pero `build_knowledge_context` **no lo filtra**: un documento sin revisar también entra al prompt. Si la curación exige aprobar antes de publicar, ahí hay un hueco.

## Nott en la base de datos

| Tabla | Filas | Contenido |
|---|---|---|
| `businesses` | 1 | Nott · tono: "asesora muy comprometida con la marca" · `whatsapp_phone_number_id` del número de prueba · aviso por correo (SES) |
| `agents` | 2 a 3 | Informador (responde, sin herramientas) · Capturador de leads (embajadores, mayoristas, suscripción) |
| `knowledge_documents` | 10 | Una por sección de `conocimiento.md` |
| `admin_users` | 1 | El asesor que entra al panel |

Todo lo demás (clientes, conversaciones, mensajes, leads, logs) se crea solo cuando la gente escribe.
