# Champis

Où et quand cueillir des champignons en France, à l'échelle de la parcelle forestière. Outil personnel, gratuit, construit sur de l'open data (IGN BD Forêt V2, Copernicus DEM, SoilGrids, Open-Meteo) et sur la littérature mycologique.

Score d'un pixel de 25 m pour une espèce et un jour : `S = T × M × Φ` — potentiel terrain (statique, ce dépôt le calcule), indice météo (dynamique, API), fenêtre phénologique. Le modèle complet est dans `species/SCHEMA.md`.

## Phase 1 — faire tourner le pipeline sur le Doubs

Prérequis : Python 3.12+, [uv](https://docs.astral.sh/uv/), ~5 Go de disque pour un département. Variables locales (bucket, clé Open-Meteo) : `cp .env.example .env` et compléter ; le fichier est lu par les CLI et l'API, jamais commité. Pas de GDAL système à installer : rasterio embarque le sien.

```bash
uv sync --group dev
uv run pytest                      # 67 tests, dont une chaîne complète sur un département synthétique
uv run champis-pipeline all --dept 25
```

`all` enchaîne :

1. **download** — BD Forêt V2 du Doubs (31 Mo, flux Géoplateforme IGN), pH SoilGrids (WCS ISRIC, découpe sur l'emprise), dalles Copernicus DEM 30 m (bucket public AWS). Tout va dans `data/raw/`, rien n'est retéléchargé s'il est présent.
2. **grid** — rasters alignés 25 m Lambert-93 dans `data/grid/25/` : `host.tif` (essence dominante), `cover.tif`, `ph.tif`, `alt.tif`, `slope.tif`, `aspect.tif`.
3. **terrain** — `data/T/<espèce>/25-<hash>.tif`, un par espèce (7 au départ). Quelques secondes par espèce.
4. **tiles** — `data/tiles/T-<espèce>-<hash>-p1.0.0-d<hash>.pmtiles`, niveaux 6–15, plus la couche `A-300m-d<hash>.pmtiles` (classe d'altitude du pixel, commune à toutes les espèces, lue par la carte pour appliquer gel et phénologie par pixel). Quelques minutes par espèce pour un département (z15 ≈ 7 000 tuiles).
5. **weather-grid** — `data/weather_grid.json` : les cellules météo 5 km où T > 0, avec leurs classes d'altitude ; c'est l'entrée de l'API.

Plusieurs départements d'un coup : `uv run champis-pipeline batch --depts all --jobs 3` enchaîne download, grid et terrain en parallèle (un processus par département, ~2 Go chacun), garde l'état dans `data/batch_status.json` et reprend là où il s'est arrêté ; `tiles --depts …` et `weather-grid --depts …` prennent ensuite la liste.

Puis `uv run champis-pipeline summary --dept 25` donne la surface par classe (excellent → très faible) par espèce : premier regard sur la crédibilité avant toute carte.

Diagnostic de T : `uv run champis-pipeline explain --dept 25 --species boletus_edulis` écrit `reports/explain-boletus_edulis-25.json` (surface et T moyen par hôte, distribution de chaque facteur, pH et altitude bruts, part « excellent » si un facteur valait 1 ou si l'hôte était plafonné) : c'est l'entrée de toute révision des valeurs d'espèce (skill `species-file`).

Contrôle GBIF (critère de phase 1, AUC > 0,7) : `uv run champis-pipeline check --dept 25` télécharge les occurrences GBIF du département (cache dans `data/raw/gbif/`) et écrit `reports/<espèce>-25.html` : AUC de T aux occurrences contre fond forestier, mois d'observation, quantiles pH pour recalibrer les espèces (skill `validation-gbif`).

Pour regarder le résultat : `python3 -m http.server 8000` à la racine du dépôt, ouvrir <http://localhost:8000/web/> et y glisser les PMTiles (voir `web/README.md`). Sans serveur, [pmtiles.io](https://pmtiles.io) les affiche en gris brut : 0 = noir = T 0, 254 = blanc = T 1.

## Phase 2 — l'API météo

```bash
uv run uvicorn champis_api.app:app --reload     # /species, /weather, /weather/bbox — voir api/README.md
```

## Déploiement

Railway, un seul service (`Dockerfile`, `railway.toml`), volume sur `/data` pour le cache météo, bucket Railway pour les PMTiles (`champis-pipeline publish`, `GET /tiles`), pré-chauffe intégrée, clé Open-Meteo (plan Standard) ; l'historique météo est amorcé une fois depuis ton poste (`champis-api seed`) puis accumulé par le service : voir `api/README.md`.

## Ce qui n'est pas encore là

- `/departements`, `/tiles` (bucket + URL présignées), `/observations`.
- Le recalibrage pH des espèces à partir des quantiles du rapport `check`.

## Licences des données

BD Forêt V2 © IGN, Licence Ouverte 2.0 · Copernicus DEM © ESA/Airbus · SoilGrids © ISRIC, CC-BY 4.0 · Weather data by Open-Meteo.com, CC-BY 4.0 · Observations GBIF, CC-BY / CC-BY-NC selon jeu.
