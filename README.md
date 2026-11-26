# Agente de Soporte Inteligente — MVP

Plataforma SaaS multi-tenant de agentes conversacionales de atención al cliente
para pymes, por WhatsApp. Ver `CLAUDE.md` para las decisiones de arquitectura y
`docs/` para alcance y modelo de datos.

## Requisitos

- Python 3.12+
- Docker (para la base de datos local)

## Puesta en marcha

```bash
# 1. Configuración
cp .env.example .env        # completar ANTHROPIC_API_KEY y credenciales de WhatsApp

# 2. Base de datos (Postgres 16 + pgvector)
docker compose up -d

# 3. Entorno de Python (versiones exactas: ver "Versiones de dependencias")
python3 -m venv .venv
.venv/bin/pip install -c constraints.txt -e ".[dev]"

# 4. Migraciones
.venv/bin/alembic upgrade head

# 5. Datos de ejemplo: negocio demo con 2 agentes y conocimiento
.venv/bin/python -m scripts.seed_dev          # --reset para recrearlo

# 6. Token del panel para ese negocio
.venv/bin/python -m scripts.crear_token_admin \
  --business-id 00000000-0000-4000-8000-000000000001 --email dueno@demo.test

# 7. Levantar la API
.venv/bin/uvicorn app.main:app --reload
```

Comprobación: `curl localhost:8000/health` → `200` con la DB arriba, `503` si
está caída. La documentación interactiva queda en `/docs`.

### Probar el agente sin teléfono

```bash
curl -X POST localhost:8000/admin/sandbox/message \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"text": "¿qué horarios tienen?"}'
```

El sandbox corre el **mismo** orquestador con un canal falso y devuelve además la
intención detectada, el agente enrutado, las herramientas usadas y el costo.

### Panel de administración (Streamlit)

```bash
.venv/bin/pip install -c constraints.txt -e ".[panel]"
.venv/bin/streamlit run panel/app.py        # http://localhost:8501
```

Pegar el token del paso 6 en la barra lateral. El panel es un **cliente HTTP de
`/admin/*`**: no importa `app.*` ni toca la base, así que el aislamiento sigue
siendo el de la API (token → `business_id` → RLS), sin una segunda puerta a los
datos.

| Página | Para qué |
|--------|----------|
| Conversaciones | Bandeja: lista, historial completo con fotos, responder con texto/emojis/imágenes, tomar o devolver al agente, ventana de 24 h |
| Conocimiento | Cargar/revisar/borrar lo que el agente sabe (`.txt`, `.md`, `.pdf`, `.docx` o pegado) |
| Sandbox | Conversar con el agente sin WhatsApp, con la traza del turno |
| Agentes | Alta y edición: enrutamiento, instrucciones, herramientas |
| Bandejas | Solicitudes capturadas y escalamientos (llevan a Conversaciones para responder) |
| Métricas | Volumen, % sin humano, costo y salud de la caché |
| Negocio | Datos, notificaciones y borrado de datos (Ley 1581) |

Es la herramienta del **piloto**: Streamlit no tiene sesiones reales ni
multiusuario. Un panel público multi-tenant necesita otra cosa.

Dos cosas que la página de conocimiento hace a propósito:

- **El texto extraído siempre se muestra editable antes de guardar.** Un PDF con
  tablas o columnas produce texto desordenado, y ese desorden llega tal cual al
  cliente porque el agente puede citarlo textualmente.
- **`confirmed_public` no viene marcado.** Es el único control entre un documento
  interno y un cliente leyéndolo por WhatsApp.

### Conectar WhatsApp

1. En Meta for Developers, apuntar el webhook a `https://<host>/webhooks/whatsapp`
   con el `WHATSAPP_VERIFY_TOKEN` del `.env`.
2. Asociar el número al negocio:
   `PATCH /admin/business {"whatsapp_phone_number_id": "<id de Meta>"}`.

El `phone_number_id` es lo único del payload que identifica al tenant.

## Arquitectura

```
Meta Cloud API → POST /webhooks/whatsapp → [firma → tenant → dedup] → ACK 200
                                                                      ↓ (async)
                                        orquestador → clasificar (Haiku)
                                                    → responder (Sonnet + tools)
                                                    → persistir → enviar
```

| Módulo | Responsabilidad |
|--------|-----------------|
| `app/api/webhooks.py` | Webhook con ACK inmediato, firma HMAC y deduplicación |
| `app/api/admin.py` | API del panel: config, agentes, conocimiento, bandejas, sandbox |
| `app/core/orchestrator.py` | Loop del turno, 7 flujos F1–F7 |
| `app/core/llm.py` | Única salida a Anthropic: prompt caching, tiering, tool use |
| `app/core/tools.py` | `capture_request`, `notify_owner` (+ stub `search_knowledge`) |
| `app/core/channel_whatsapp.py` | Única pieza que conoce el formato de WhatsApp |
| `app/core/limits.py` | Rate limit, tope diario de costo, privacidad (Ley 1581) |
| `app/db/repositories.py` | Único acceso a datos, siempre filtrado por `business_id` |
| `panel/` | Panel Streamlit: cliente HTTP de `/admin/*`, proceso aparte |

## Roles de base de datos (importante)

El aislamiento multi-tenant (RNF-04) tiene dos capas: el filtro por `business_id`
en la aplicación y **Row-Level Security** en Postgres como respaldo.

RLS **no aplica a roles superusuario ni con `BYPASSRLS`**. Si la aplicación se
conecta con el rol administrador, las políticas quedan inertes y todos los
tenants se ven entre sí, sin ningún error visible. Por eso hay tres roles:

| Rol | Uso | Variable |
|-----|-----|----------|
| `user` (admin) | Solo migraciones: crea extensiones, roles y vistas | `ADMIN_DATABASE_URL` |
| `app_agente` | La aplicación y los tests de RLS | `DATABASE_URL` |
| `app_agente_test` | Solo tests de repositorio (`BYPASSRLS`) | derivado de `DATABASE_URL` |

`app_agente_test` omite RLS a propósito: los tests de repositorio verifican el
filtrado por `business_id` en el código (capa 1). Con RLS activo, la capa 2
taparía un repositorio que olvidara filtrar y el test pasaría sin probar nada.
La migración `0004` no lo crea si `ENV=production`.

### Los dos lookups que preceden al tenant

Resolver el número de WhatsApp → negocio, y el token del panel → negocio, ocurren
**antes** de saber el tenant, así que corren sin `app.current_business_id`. Con
RLS activo esas consultas devolverían cero filas siempre — ningún mensaje se
enrutaría y ningún token sería válido.

La solución no debilita RLS: expone solo el mapeo mínimo en dos vistas
(`whatsapp_number_map`, `admin_token_lookup`) con `security_invoker = false`.
Todo lo demás sigue protegido.

## Desarrollo

```bash
.venv/bin/ruff check .        # lint (incluye panel/)
.venv/bin/mypy app scripts    # tipos
.venv/bin/pytest -q           # tests

# El panel se chequea aparte: sus módulos se importan por nombre plano porque
# Streamlit pone el directorio del script en sys.path. `panel/pages/` queda
# afuera (los nombres empiezan con dígito para fijar el orden del menú).
MYPYPATH=panel .venv/bin/mypy panel/api_client.py panel/extraccion.py panel/sesion.py
```

Los tests que necesitan Postgres se saltan si no hay `DATABASE_URL`, para no
bloquear el trabajo local. En CI (`CI=true`) esa configuración faltante es un
error: saltarse los tests de aislamiento equivale a no probarlo.

## Migraciones

```bash
.venv/bin/alembic upgrade head       # aplicar
.venv/bin/alembic downgrade base     # revertir todo
.venv/bin/alembic current            # estado
```

| Revisión | Contenido |
|----------|-----------|
| `0001` | Esquema inicial: 12 tablas, índices, extensiones, RLS + triggers `updated_at` |
| `0002` | Rol `app_agente` sin `BYPASSRLS`, que hace efectivo el aislamiento |
| `0003` | Políticas robustas ante tenant vacío/ausente/malformado (fail-closed sin error) |
| `0004` | Rol `app_agente_test` con `BYPASSRLS`, solo para tests de repositorio |
| `0005` | `messages.seq`: orden de historial por inserción, no por reloj |
| `0006` | Mapeo número de WhatsApp → tenant, y `inbound_events` (deduplicación) |
| `0007` | Tokens del panel admin (se guarda el hash, nunca el token) |
| `0008` | Vista `whatsapp_number_map`: resolver el tenant sin romper RLS |
| `0009` | Vista `admin_token_lookup`: autenticar el panel sin romper RLS |
| `0010` | Tope diario de costo, aviso de privacidad y evidencia de borrado |

## Notas de diseño que conviene no olvidar

- **`messages` se ordena por `seq`, no por `created_at`.** `now()` es constante
  dentro de una transacción, así que los mensajes de un mismo turno comparten
  timestamp y ordenar por él deja el historial al azar.
- **El prefijo del prompt debe ser byte-idéntico entre turnos.** Cualquier cosa
  dinámica ahí (timestamps, ids) invalida la caché y dispara el costo (RNF-02).
  `GET /admin/metrics/costs` alerta si el cache hit baja del 60%.
- **El webhook usa `asyncio.create_task`, no `BackgroundTasks`.** Estas últimas
  corren antes de cerrar el request, así que un turno lento mantendría la
  conexión abierta y Meta reintentaría la entrega.
- **El contenido de los mensajes nunca va a los logs.** Vive en la base, bajo RLS
  y sujeto a retención; copiarlo al log lo saca de ese perímetro (Ley 1581).

## Estado

Fases 1–7 implementadas y con tests. Pendiente del cierre del MVP (fase 7):
**operar el piloto con un negocio real durante una semana** y documentar los
números (% resuelto sin humano, escalamientos, costo/conversación).

## Versiones de dependencias

Dos archivos, con papeles distintos:

| Archivo | Qué dice |
|---|---|
| `pyproject.toml` | Los **rangos** que acepta el proyecto: desde la versión más vieja probada hasta antes del siguiente *major* (por ejemplo `anthropic>=0.120,<3`). |
| `constraints.txt` | La versión **exacta** de cada paquete, incluidas las dependencias de las dependencias (82 paquetes). |

Todo se instala con `-c constraints.txt`: el PC, el CI (`.github/workflows/ci.yml`) y la imagen Docker (`Dockerfile`). Así los tres corren exactamente el mismo software. Sin este archivo, cada instalación tomaba "lo último" y el CI terminó probando SQLAlchemy 2.1 mientras en local había 2.0.

**Actualizar versiones** (cuando se quiera, no en automático):

1. Resolver lo último dentro de los rangos, en Linux (como producción):

   ```bash
   docker run --rm -v "$PWD:/src:ro" python:3.12-slim sh -c \
     "mkdir /tmp/p && cp -r /src/pyproject.toml /src/README.md /src/app /src/scripts /tmp/p && \
      pip install -q --root-user-action=ignore '/tmp/p[dev,panel]' && pip freeze --exclude agente-soporte-mvp"
   ```

2. Reemplazar las líneas `paquete==versión` de `constraints.txt` con esa salida. Mantener `colorama` y `tzdata`, que solo se instalan en Windows y no salen en la resolución de Linux.
3. Reinstalar con `-c constraints.txt` y correr lo mismo que el CI: `ruff check .`, `mypy app scripts`, el mypy del panel y `pytest`.
4. Si algo se rompe, ajustar el código o bajar el tope en `pyproject.toml`. Hacer commit de los dos archivos juntos.
