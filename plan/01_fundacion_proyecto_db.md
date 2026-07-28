# Fase 1 — Fundación del proyecto y base de datos

## Contexto

Todo lo demás depende de dos cimientos: un esqueleto FastAPI async correcto y la base de datos con el DDL de `docs/MVP_Estructura_Base_de_Datos.md` aplicado, incluido el aislamiento multi-tenant por RLS. Errores aquí (driver síncrono, RLS ausente) son carísimos de corregir después.

## Cubre

- RNF-03 (stateless, config como datos), RNF-04 (aislamiento, capa RLS), base de RF-24.

## Tareas

1. Inicializar proyecto: estructura de `CLAUDE.md` §"Estructura de código objetivo", `pyproject.toml` (fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, alembic, pydantic v2, anthropic, httpx, pytest, pytest-asyncio, mypy, ruff).
2. Docker Compose local: Postgres 16 con `pgvector` instalado.
3. Alembic configurado en modo async. Migración inicial = DDL completo de `docs/` (12 tablas + índices + extensiones). La dimensión `vector(1024)` queda como está; ajustar solo cuando se elija modelo de embeddings (post-MVP, conocimiento va por base completa).
4. Habilitar RLS: política `tenant_isolation` en TODA tabla con `business_id`, usando `current_setting('app.current_business_id')`. Incluir en la migración.
5. `app/db/session.py`: engine async + dependencia de sesión que ejecuta `SET LOCAL app.current_business_id` por transacción.
6. `app/main.py` con healthcheck `GET /health` (verifica conexión a DB).
7. CI mínimo: ruff + mypy + pytest en cada push.

## Criterios de aceptación

- `docker compose up` levanta la DB; `alembic upgrade head` aplica todo sin errores.
- `GET /health` responde 200 con la DB arriba y 503 con la DB caída.
- Test de RLS: con `app.current_business_id` fijado al tenant A, un `SELECT` sobre filas del tenant B devuelve 0 filas aunque la consulta no filtre por `business_id`.

## Definición de Terminado

CI verde (lint + tipos + tests), migración reproducible desde cero, test de RLS pasando.
