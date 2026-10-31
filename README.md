# Champis

Où et quand cueillir des champignons en France, à l'échelle de la parcelle forestière. Outil personnel, gratuit, construit sur de l'open data (IGN BD Forêt V2, Copernicus DEM, SoilGrids, Open-Meteo) et sur la littérature mycologique.

Score d'un pixel de 25 m pour une espèce et un jour : `S = T × M × Φ` — potentiel terrain (statique, ce dépôt le calcule), indice météo (dynamique, API), fenêtre phénologique. Le modèle complet est dans `species/SCHEMA.md`.

## Phase 1 — faire tourner le pipeline sur le Doubs

Prérequis : Python 3.12+, [uv](https://docs.astral.sh/uv/), ~5 Go de disque pour un département. Variables locales (bucket, clé Open-Meteo) : `cp .env.example .env` et compléter ; le fichier est lu par les CLI et l'API, jamais commité. Pas de GDAL système à installer : rasterio embarque le sien.

```bash
make setup                         # uv sync --group dev, .env depuis .env.example
make test                          # 142 tests, dont une chaîne complète sur un département synthétique
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

## Phase 2 — l'API météo

```bash
uv run uvicorn champis_api.app:app --reload     # /species, /weather, /weather/bbox, /departements — voir api/README.md
```

## Déploiement

Railway, un seul service (`Dockerfile`, `railway.toml`), volume sur `/data` pour le cache météo, bucket Railway pour les PMTiles (`champis-pipeline publish`, `GET /tiles`), Postgres Railway pour les observations et les coins de l'app, pré-chauffe intégrée, clé Open-Meteo (plan Standard), clé APNs pour les notifications « mes coins » ; l'historique météo est amorcé une fois depuis ton poste (`champis-api seed`) puis accumulé par le service : voir `api/README.md`.

## Ce qui n'est pas encore là

- Le recalibrage pH des espèces à partir des quantiles du rapport `check`.

## Licences des données

BD Forêt V2 © IGN, Licence Ouverte 2.0 · Copernicus DEM © ESA/Airbus · SoilGrids © ISRIC, CC-BY 4.0 · Weather data by Open-Meteo.com, CC-BY 4.0 · Observations GBIF, CC-BY / CC-BY-NC selon jeu.
