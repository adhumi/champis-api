# Déploiement sur Railway

Vue d'ensemble des services. Ce texte vient du `README.md` d'avant sa refonte (15 nov. 2026). La procédure pas à pas, les variables, les crons et la circulation du cache par le bucket sont dans [`api/README.md`](../api/README.md) › Déploiement Railway ; la description des services en code dans `.railway/railway.ts` (`.railway/README.md`).

Le déploiement lui-même est fait par la CI (`.github/workflows/ci.yml`) : après des tests verts sur `main`, elle envoie l'arbre de travail, sous-module compris, à chacun des quatre services avec `railway up` (secret `RAILWAY_TOKEN`). Railway ne récupère pas les sous-modules ; il ne construit donc pas l'image depuis GitHub.

Railway, quatre services construits par le même `Dockerfile` (plus le site, `champis-web`, dans le même projet) (décision du 4 nov. 2026, spec › Points ouverts › Tuiles hors du processus Python) :

| Service (`.railway/railway.ts`) | Commande | Rôle |
| --- | --- | --- |
| `champis` (API) | `uvicorn` (CMD du `Dockerfile`) | FastAPI, plusieurs workers uvicorn (`CHAMPIS_WORKERS`, 2), volume sur `/data` (cache météo SQLite), relais de tuiles avec cache disque des plages sur le volume (`CHAMPIS_TILE_CACHE_MB`), backoffice privé `/admin` (`CHAMPIS_ADMIN_KEY` ; `api/README.md` › Backoffice) |
| `champis-warm` (cron) | `champis-api warm --pull --publish` | À 2 h et 14 h UTC (3 h et 15 h Paris en hiver, une heure plus tard en été) : pré-chauffe Open-Meteo, indice par département, météo précalculée, puis instantané du cache déposé dans le bucket |
| `champis-notify` (cron) | `champis-api notify --pull` | À 6 h UTC (7 h Paris en hiver) : notification « mes coins » |
| `champis-jobs` (worker) | `champis-api worker` | Service d'exécution du backoffice (spec › Backoffice, lots BO3 et BO4) : prend les tâches mises en file depuis `/admin` (pré-chauffe à la demande, notification, publications, amorçage, climatologie, cartes HD, puis `batch`, `tiles`, `publish`, `weather-grid` du pipeline), une à la fois, sur son propre volume de 100 Go |

Le bucket Railway porte les PMTiles (`champis-pipeline publish`, `GET /tiles`), l'amorçage météo (`champis-api seed` puis `publish-seed`, une fois par grille) et l'instantané du cache publié par le cron (`weather/weather_cache.db`), que l'API importe au démarrage puis toutes les `CHAMPIS_CACHE_POLL_MINUTES` (10). L'API n'a plus de tâche de fond : `CHAMPIS_WARM_HOURS` et `CHAMPIS_NOTIFY_HOURS` restent vides (repli sans cron : `3,15` et `7`, la passe bloque alors un worker ≈ 36 min). Postgres Railway pour les observations et les coins, clé Open-Meteo (plan Standard), clé APNs. Création des services cron, variables, migration et limites (un volume ne se monte que sur un service) : `api/README.md` › Déploiement Railway. `make doctor` dit si la pré-chauffe est intégrée ou déléguée, et la date du dernier cache publié.
