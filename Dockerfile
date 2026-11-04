# champis-api sur Railway : FastAPI + cache SQLite sur volume (/data) + carte web statique.
# La même image sert aux services cron (railway.warm.toml, railway.notify.toml) : seul le
# `startCommand` change ; la pré-chauffe n'est plus dans l'API (décision du 4 nov. 2026).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_FROZEN=1

# dépendances d'abord (cache Docker), code ensuite
COPY pyproject.toml uv.lock ./
COPY pipeline/pyproject.toml pipeline/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
RUN uv sync --no-install-workspace

COPY pipeline pipeline
COPY api api
COPY species species
COPY mapping mapping
COPY shared shared
COPY web web
COPY data/weather_grid.json data/weather_grid.json
# Repli local (Doubs) tant qu'aucun amorçage n'est publié dans le bucket (weather/weather_seed.db) ;
# le motif [n] rend le COPY tolérant si le fichier n'est pas versionné.
COPY data/weather_seed.jso[n] data/
RUN uv sync

# CHAMPIS_WARM_HOURS et CHAMPIS_NOTIFY_HOURS vides : l'API n'a plus de tâche de fond, les
# services cron pré-chauffent et notifient. Repli sans cron : `3,15` et `7` dans les variables
# du service (la passe bloque alors le worker meneur ≈ 36 min pour la France).
ENV CHAMPIS_WEATHER_GRID=/app/data/weather_grid.json \
    CHAMPIS_WEATHER_SEED=/app/data/weather_seed.json \
    CHAMPIS_CACHE_DB=/data/weather_cache.db \
    CHAMPIS_WARM_HOURS= \
    CHAMPIS_NOTIFY_HOURS= \
    PORT=8000
EXPOSE 8000
# Plusieurs workers (CHAMPIS_WORKERS, 2 par défaut) : limiteur de débit et cache du manifeste par
# processus, SQLite en WAL, tâches de fond tenues par un seul worker (verrou sur /data).
CMD ["sh", "-c", "uv run uvicorn champis_api.app:app --host 0.0.0.0 --port ${PORT} --workers ${CHAMPIS_WORKERS:-2}"]
