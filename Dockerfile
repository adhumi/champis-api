# champis-api sur Railway : FastAPI + cache SQLite sur volume (/data) + carte web statique.
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
# Optionnel : le motif [n] rend le COPY tolérant si l'amorçage n'est pas encore versionné
# (l'API démarre alors en avertissant, toutes les cellules sont indisponibles).
COPY data/weather_seed.jso[n] data/
RUN uv sync

ENV CHAMPIS_WEATHER_GRID=/app/data/weather_grid.json \
    CHAMPIS_WEATHER_SEED=/app/data/weather_seed.json \
    CHAMPIS_CACHE_DB=/data/weather_cache.db \
    CHAMPIS_WARM_HOURS=3,15 \
    PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run uvicorn champis_api.app:app --host 0.0.0.0 --port ${PORT}"]
