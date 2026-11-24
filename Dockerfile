# Imagen de annie. Dos destinos:
#   docker build --target api   -t annie-api   .   -> API FastAPI (webhook + /admin)
#   docker build --target panel -t annie-panel .   -> panel Streamlit (cliente HTTP de /admin)
#
# Una sola imagen base para los dos: el panel es un extra opcional del mismo
# paquete (pyproject: [panel]) y no importa `app.*`.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/annie

# Primero solo lo que define dependencias, para aprovechar la caché de capas.
COPY pyproject.toml README.md ./
COPY app ./app
COPY scripts ./scripts

RUN useradd --system --uid 10001 --no-create-home annie


FROM base AS api

RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations
COPY datos_demo ./datos_demo

USER annie
EXPOSE 8000

# UN solo worker a propósito:
#  - el rate limit por cliente vive en memoria del proceso (app/core/limits.py);
#  - el turno corre en segundo plano con asyncio.create_task después del ACK.
# Con varios workers, cada uno tendría su propio contador.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]


FROM base AS panel

RUN pip install ".[panel]"

COPY panel ./panel

USER annie
EXPOSE 8501

# baseUrlPath=panel: Caddy publica el panel en https://<dominio>/panel
CMD ["streamlit", "run", "panel/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.baseUrlPath=panel", "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
