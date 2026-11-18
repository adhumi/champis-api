# champis-api sur Railway : FastAPI + cache SQLite sur volume (/data). Le site est un service à part
# (adhumi/champis-web) depuis le découpage des dépôts (6 nov. 2026).
# La même image sert aux services cron (railway.warm.toml, railway.notify.toml) : seul le
# `startCommand` change ; la pré-chauffe n'est plus dans l'API (décision du 4 nov. 2026).
# Images de base épinglées par condensé (14 nov. 2026) : un tag se déplace, pas un condensé. Pour
# les relever (index multi-architecture) : `docker buildx imagetools inspect <image>:<tag>`.
#
# L'image tourne encore en root, sciemment : un volume Railway est monté en root et la doc
# (docs.railway.com/volumes › Permissions, lue le 14 nov. 2026) n'offre à une image non-root que
# `RAILWAY_RUN_UID=0`, c'est-à-dire root. L'API et `champis-jobs` écrivent sur leur volume `/data`
# (cache météo SQLite, cache des tuiles, verrous, rasters), les crons dans `/data` et
# `/app/data/logs` du conteneur, `uv run` dans `/app/.venv` : passer à un utilisateur sans
# privilège demande un point d'entrée qui rend ces chemins à cet utilisateur avant de lâcher root,
# à essayer sur un service Railway, pas à l'aveugle (un cron qui n'écrit plus échoue sans bruit).
FROM python:3.15-slim-bookworm@sha256:96599df822d0e1c6bb700014a05c7f3c8cabc6e0f7ff3c4f9f69cee033b4b5d9

# rasterio (pipeline : climatologie du sol et, lot BO4, terrain et tuiles sur le service
# champis-jobs) embarque son GDAL mais pas libexpat ; l'API, elle, n'importe jamais rasterio.
# DejaVu : police du cartouche des cartes HD (`render.py`), la police par défaut de Pillow n'a pas
# les lettres accentuées (« Cèpe »).
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_FROZEN=1

# dépendances d'abord (cache Docker), code ensuite
COPY pyproject.toml uv.lock ./
COPY pipeline/pyproject.toml pipeline/pyproject.toml
COPY api/pyproject.toml api/pyproject.toml
RUN uv sync --no-install-workspace

COPY pipeline pipeline
COPY api api
# Référentiel (sous-module champis/ : espèces, table BD Forêt, palette, textes, vecteurs)
COPY champis/species champis/species
COPY champis/mapping champis/mapping
COPY champis/shared champis/shared
COPY data/weather_grid.json data/weather_grid.json
# Repli local (Doubs) tant qu'aucun amorçage n'est publié dans le bucket (weather/weather_seed.db) ;
# le motif [n] rend le COPY tolérant si le fichier n'est pas versionné.
COPY data/weather_seed.jso[n] data/
# Empreinte du déploiement écrite par la CI (sha, date), affichée par le backoffice ; absente en local
COPY BUIL[D] ./
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
