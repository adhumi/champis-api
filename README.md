# Champis

Où et quand cueillir des champignons en France, à l'échelle de la parcelle forestière. Outil personnel, gratuit, construit sur de l'open data (IGN BD Forêt V2, Copernicus DEM, SoilGrids, Open-Meteo) et sur la littérature mycologique.

Score d'un pixel de 25 m pour une espèce et un jour : `S = T × M × Φ` — potentiel terrain (statique, ce dépôt le calcule), indice météo (dynamique, API), fenêtre phénologique. Le modèle complet est dans `species/SCHEMA.md`.

## Phase 1 — faire tourner le pipeline sur le Doubs

Prérequis : Python 3.12+, [uv](https://docs.astral.sh/uv/), ~5 Go de disque pour un département. Variables locales (bucket, clé Open-Meteo) : `cp .env.example .env` et compléter ; le fichier est lu par les CLI et l'API, jamais commité. Pas de GDAL système à installer : rasterio embarque le sien.

```bash
make setup                         # uv sync --group dev, .env depuis .env.example
make test                          # ~210 tests, dont une chaîne complète sur un département synthétique
make dept DEPT=25                  # = uv run champis-pipeline all --dept 25
```

`make help` liste toutes les commandes (développement, pipeline, publication, météo) avec leur documentation ; les commandes `uv run …` ci-dessous restent valables telles quelles. Avant de publier : `make doctor` (l'environnement est-il prêt : grille météo, bucket, base, clé APNs…), `make status` (où en est chaque département sur disque et dans le bucket), `make publish-dry` (ce que `publish --prune` téléverserait et effacerait, sans rien écrire).

`all` enchaîne :

1. **download** — BD Forêt V2 du Doubs (31 Mo, flux Géoplateforme IGN), pH SoilGrids (WCS ISRIC, découpe sur l'emprise), dalles Copernicus DEM 30 m (bucket public AWS). Tout va dans `data/raw/`, rien n'est retéléchargé s'il est présent.
2. **grid** — rasters alignés 25 m Lambert-93 dans `data/grid/25/` : `host.tif` (essence dominante), `cover.tif`, `ph.tif`, `alt.tif`, `slope.tif`, `aspect.tif`.
3. **terrain** — `data/T/<espèce>/25-<hash>.tif`, un par espèce (7 au départ). Quelques secondes par espèce.
4. **tiles** — `data/tiles/T-<espèce>-<hash>-p1.0.0-d<hash>.pmtiles`, niveaux 6–15, plus la couche `A-300m-d<hash>.pmtiles` (classe d'altitude du pixel, commune à toutes les espèces, lue par la carte pour appliquer gel et phénologie par pixel). Quelques minutes par espèce pour un département (z15 ≈ 7 000 tuiles). Avec `--per-dept`, un jeu par département (hash du département seul) est extrait du MBTiles de travail du jeu complet sans rien re-rendre : c'est ce que l'app télécharge pour le hors ligne (décision du 31 oct. 2026) ; `publish --per-dept` les téléverse et le manifeste marque ces entrées d'un champ `dept`.
5. **weather-grid** — `data/weather_grid.json` (version 2) : les cellules météo 5 km où T > 0, avec leurs classes d'altitude et, par département touché, classe d'altitude et espèce, la somme des T des pixels (poids de l'indice par département) ; c'est l'entrée de l'API.

Plusieurs départements d'un coup : `uv run champis-pipeline batch --depts all --jobs 3` enchaîne download, grid et terrain en parallèle (un processus par département, ~2 Go chacun), garde l'état dans `data/batch_status.json` et reprend là où il s'est arrêté ; `tiles --depts …` et `weather-grid --depts …` prennent ensuite la liste.

Puis `uv run champis-pipeline summary --dept 25` donne la surface par classe (excellent → très faible) par espèce : premier regard sur la crédibilité avant toute carte.

Diagnostic de T : `uv run champis-pipeline explain --dept 25 --species boletus_edulis` écrit `reports/explain-boletus_edulis-25.json` (surface et T moyen par hôte, distribution de chaque facteur, pH et altitude bruts, part « excellent » si un facteur valait 1 ou si l'hôte était plafonné) : c'est l'entrée de toute révision des valeurs d'espèce (skill `species-file`).

Contrôle GBIF (critère de phase 1, AUC > 0,7) : `uv run champis-pipeline check --dept 25` télécharge les occurrences GBIF du département (cache dans `data/raw/gbif/`) et écrit `reports/<espèce>-25.html` : AUC de T aux occurrences contre fond forestier, mois d'observation, quantiles pH pour recalibrer les espèces (skill `validation-gbif`).

Pour regarder le résultat : `python3 -m http.server 8000` à la racine du dépôt, ouvrir <http://localhost:8000/web/> et y glisser les PMTiles (voir `web/README.md`). Sans serveur, [pmtiles.io](https://pmtiles.io) les affiche en gris brut : 0 = noir = T 0, 254 = blanc = T 1.

### Affichage et journaux

Chaque commande affiche ses étapes `[i/n]` avec un statut (`ok`, `deja fait`, `ECHEC`) et sa durée, des barres de progression pour les tâches longues, et un résumé final. Le statut va sur stderr, les données (`summary`) sur stdout. Couleurs sur un terminal seulement (`NO_COLOR` les coupe). Tout est aussi écrit, sans couleur, dans `data/logs/<commande>-<date>.log` (50 derniers gardés ; `make log` affiche le dernier ; `CHAMPIS_LOG_DIR` change le dossier, vide pour ne rien écrire). `-v` rétablit les journaux bruts et les traces complètes ; `-q` ne garde que les avertissements et le résultat.

```
champis-pipeline all --dept 25

all --dept 25 · 7 espèce(s)
[1/17] download 25
      - présent : BDFORET_2-0__SHP_LAMB93_D025_2016-02-16.7z
      ok              3 s  BD Forêt FORMATION_VEGETALE.shp · SoilGrids · DEM 4 dalle(s) · grille 3812x4620 px
[2/17] grid 25
      deja fait       0.4 s  host, cover, ph, alt, slope, aspect · 3812x4620 px
[3/17] terrain boletus_edulis
      ok             12 s  data/T/boletus_edulis/25-af34c16e.tif
…
25 prêt en 14 min 02 s · journal : data/logs/all-20261031-150102.log
```

### Tests

`make test` (ou `uv run pytest`) tourne sans réseau : fixtures synthétiques (département de 10 km × 10 km, faux Open-Meteo, faux bucket). La suite est écrite à partir de la spec et de `species/SCHEMA.md` ; chaque fichier de test cite la règle qu'il protège.

| Fichiers | Ce qu'ils garantissent |
| --- | --- |
| `pipeline/tests/test_spec_contracts.py` | Palette et seuils de la spec, fichiers espèces et table TFV valides, aucune valeur d'espèce dans le code, noms de tuiles et manifeste, forme de `weather_grid.json` |
| `pipeline/tests/test_terrain.py`, `test_pipeline_e2e.py` | T = hôte × Σ poids × facteur, règles dures, chaîne grid → terrain → tuiles → grille météo, extraction par département, GBIF, explain, publication |
| `pipeline/tests/test_rasterize.py`, `test_download.py` | Jointure `CODE_TFV` exacte, petits polygones gagnants, pH nodata jamais moyenné, pente et exposition ; téléchargements idempotents, md5, WCS en erreur, dalles DEM absentes |
| `pipeline/tests/test_cli.py`, `test_console.py`, `test_status.py`, `test_storage_bucket.py` | Les commandes, leur affichage et leurs codes de retour ; `status`, `publish --dry-run` ; le bucket S3 via botocore |
| `api/tests/test_scoring.py`, `test_vectors.py`, `test_spec_rules.py` | M, Φ, M_eff, trigger « meilleur événement », correction d'altitude, gel, chaleur, mémoire, persistance, groupes ; vecteurs de parité avec l'app |
| `api/tests/test_weather_api.py`, `test_departements.py`, `test_observations.py`, `test_coins.py`, `test_ratelimit.py` | Contrats des endpoints, cache 12 h, pannes et quotas Open-Meteo, amorçage, indice par département, retour terrain, coins et notifications, limitation de débit |
| `api/tests/test_app_lifespan.py`, `test_api_cli.py`, `test_doctor.py` | Démarrage réel de l'API en mode dégradé, planificateurs, commandes `champis-api` de bout en bout |
| `web/tests/test_png_decoder.py` | Le décodeur PNG du site lit les tuiles du pipeline exactement |

La CI exige une couverture d'au moins 90 % du pipeline et de l'API (`make check`, `make coverage` pour le détail). Ce qui n'est pas couvert et assumé : les appels réseau réels (Géoplateforme, ISRIC, AWS, Open-Meteo, APNs, CDS) et le code du site (`web/app.js`), vérifié à la main dans le navigateur.

## Phase 2 — l'API météo

```bash
uv run uvicorn champis_api.app:app --reload     # /species, /weather, /weather/bbox, /departements — voir api/README.md
```

## Déploiement

Railway, trois services construits par le même `Dockerfile` (décision du 4 nov. 2026, spec › Points ouverts › Tuiles hors du processus Python) :

| Service (`.railway/railway.ts`) | Commande | Rôle |
| --- | --- | --- |
| `champis` (API) | `uvicorn` (CMD du `Dockerfile`) | FastAPI, plusieurs workers uvicorn (`CHAMPIS_WORKERS`, 2), volume sur `/data` (cache météo SQLite), site statique, relais de tuiles avec cache disque des plages sur le volume (`CHAMPIS_TILE_CACHE_MB`) |
| `champis-warm` (cron) | `champis-api warm --pull --publish` | À 2 h et 14 h UTC (3 h et 15 h Paris en hiver, une heure plus tard en été) : pré-chauffe Open-Meteo, indice par département, météo précalculée, puis instantané du cache déposé dans le bucket |
| `champis-notify` (cron) | `champis-api notify --pull` | À 6 h UTC (7 h Paris en hiver) : notification « mes coins » |

Le bucket Railway porte les PMTiles (`champis-pipeline publish`, `GET /tiles`), l'amorçage météo (`champis-api seed` puis `publish-seed`, une fois par grille) et l'instantané du cache publié par le cron (`weather/weather_cache.db`), que l'API importe au démarrage puis toutes les `CHAMPIS_CACHE_POLL_MINUTES` (10). L'API n'a plus de tâche de fond : `CHAMPIS_WARM_HOURS` et `CHAMPIS_NOTIFY_HOURS` restent vides (repli sans cron : `3,15` et `7`, la passe bloque alors un worker ≈ 36 min). Postgres Railway pour les observations et les coins, clé Open-Meteo (plan Standard), clé APNs. Création des services cron, variables, migration et limites (un volume ne se monte que sur un service) : `api/README.md` › Déploiement Railway. `make doctor` dit si la pré-chauffe est intégrée ou déléguée, et la date du dernier cache publié.

## Ce qui n'est pas encore là

- Le recalibrage pH des espèces à partir des quantiles du rapport `check`.

## Licences des données

BD Forêt V2 © IGN, Licence Ouverte 2.0 · Copernicus DEM © ESA/Airbus · SoilGrids © ISRIC, CC-BY 4.0 · Weather data by Open-Meteo.com, CC-BY 4.0 · Observations GBIF, CC-BY / CC-BY-NC selon jeu.
