# Champis — pipeline terrain et API météo

Cartes de cueillette de champignons (France, 25 m) : pipeline terrain et API météo de champis.app, déployés sur Railway. Pensé d'abord pour Adrien et ses proches, puis ouvert à tous, infrastructure financée par dons (spec, 30 oct. 2026). La spec vit dans le doc « Spec — Champis » ; les skills `.claude/skills/*` disent comment travailler ici — lis `champis-model` avant tout code qui touche un score.

Découpage des dépôts (spec › Produit › Étape C, lot C7, 6 nov. 2026) : `adhumi/champis` est le référentiel (espèces, table BD Forêt, palette, vecteurs, textes), ici en sous-module `champis/` ; `adhumi/champis-web` est le site, `adhumi/champis-ios` l'app.

## Structure

```
champis/      sous-module adhumi/champis : species/ (+ SCHEMA.md, validate.py), mapping/, shared/ (palette, vecteurs, textes)
pipeline/     champis_pipeline : download → grid → terrain → tiles ; artifacts.py (sorties à committer, déposées dans le bucket par le worker)   (skill geo-pipeline)
api/          champis_api : FastAPI, scoring M/Φ, vecteurs, mesure minimale, backoffice /admin (admin/ : pages.py, views.py, templates/, router.py ; jobs.py, tools.py, worker.py, render.py)   (skill open-meteo)
data/         téléchargements et rasters, jamais commités (sauf weather_grid.json et weather_seed.json)
```

## Outillage

- Premier clone : `git clone --recurse-submodules` ; `make referential` amène `champis/` à la dernière version du référentiel (le commit qui déplace le sous-module se fait ici, par PR).
- `make help` liste toutes les commandes avec leur doc ; `make check` = la CI en local ; `make doctor`, `make status` et `make stats` disent où on en est. Les cibles sont la référence : une commande nouvelle ou changée se reflète dans le `Makefile`.
- `uv sync --group dev` ; `uv run pytest` ; `uv run ruff check . && uv run ruff format .` (le sous-module est exclu).
- Affichage des CLI : module `champis_pipeline.console` (étapes `[i/n]`, statut `ok`/`deja fait`/`ECHEC`, couleurs ANSI sur terminal seulement, **pas d'emoji**). Statut sur stderr, données sur stdout, journal dans `data/logs/` (`CHAMPIS_LOG_DIR`). Une nouvelle sous-commande passe par `console.get().steps(n)` et `console.run`, pas par `print`.
- Deux packages du workspace : `champis-pipeline` et `champis-api` (qui dépend du premier).
- Python 3.12+, numpy, rasterio, geopandas ; **aucun outil GDAL en ligne de commande requis**.
- `uv run champis-vectors` régénère `champis/shared/vectors/` **dans le sous-module** : la mise à jour se pousse par PR sur `adhumi/champis`, puis `make referential` ici. `--check` en CI.

## Règles

- Jamais de commit poussé sur `main` sans PR : branche, PR, CI verte, fusion (règle du 30 oct. 2026), ici comme dans le référentiel.
- Jamais de valeur d'espèce dans le code : tout vient de `champis/species/*.json`, qui se modifie dans `adhumi/champis` (skill `species-file`).
- Les clients ne voient que les espèces du **catalogue** (`champis_api.catalog`, décisions du 9 nov. 2026) : complètes dans le référentiel (bloc `about`, photo, planche `shared/illustrations/<id>.jpg` ; `champis_pipeline.species.completeness`) **et** dont un jeu T France est dans le manifeste du bucket (sans bucket, ce critère ne s'applique pas). `/species`, `/weather`, `/departements`, les coins, la mesure et l'indice par département passent par `catalog.served_species()` ; le pipeline, les tuiles et le backoffice voient toutes les espèces (`list_species()`).
- Jointure BD Forêt sur `CODE_TFV` uniquement. `rain_sum`, jamais `precipitation_sum`.
- Météo interpolée entre cellules (spec › Scoring, 13 nov. 2026) : `champis_api.interp` est la référence (Lambert-93 par formule directe, bilinéaire entre centres, poids renormalisés) des portages Swift et JavaScript, validés par `shared/vectors/interp.json` ; les coins l'appliquent, `/weather*` et l'indice par département restent par cellule. Même module pour l'interpolation entre classes d'altitude (`between_classes`, vecteur `alt.json`) ; l'altitude du pixel vient de la couche `E` (`tiles.build_elev`, pas de 20 m, publiée à côté de `A` que les versions installées lisent encore).
- Rien de lourd dans la boucle d'événements (correctifs du 13 nov. 2026) : calcul des scores, lecture du précalcul et encodage d'une grande réponse passent par `offload.run` (groupe de fils réservé, `api/champis_api/offload.py`) ; une grande réponse s'encode cellule par cellule (un seul `json.dumps` de 30 Mo garde le verrou de l'interpréteur). La connexion SQLite du cache ne se partage jamais entre fils sans verrou : toute méthode de `WeatherCache` qui touche `self.db` est `@_locked`, les grandes lectures passent par `_reading`. `species=` vient de `routers/query.py` (dédoublonnée, bornée au catalogue) ; lat, lon et emprises sont des `Query` bornés. Les journaux ne portent jamais d'adresse sortante (`champis_pipeline.redact`).
- Tests sans réseau : fixtures et données synthétiques (`pipeline/tests/test_pipeline_e2e.py` montre comment).
- pytest : `[tool.pytest.ini_options]` à la racine ; `pytest.approx` pour les flottants, `numpy.testing` pour les tableaux.
- FastAPI : `Annotated[...]` pour les dépendances, un `APIRouter` par domaine, `lifespan` pour le cache météo ; tests avec `httpx.ASGITransport(app=app)` et `pytest_asyncio.fixture`.
- Backoffice (lot BO6, `docs/backoffice-bo6.md`) : chaque écran est une page rendue par le serveur (`admin/pages.py`, Jinja2) avec son adresse sous `/admin/`, liens et formulaires (`POST` puis 303, champ `csrf`), tri, filtres et page dans la query string ; htmx seulement par fragments (`templates/partials/`), jamais `hx-boost`, toujours un équivalent sans JS ; Tabler et htmx vendorés, pas de build. Les gabarits ne calculent rien (`admin/views.py`). L'API JSON `/admin/api/*` reste stable pour les scripts. Aucun script ni style en ligne dans un gabarit (CSP de `admin/security.py`, 14 nov. 2026 : `script-src 'self'`, `style-src 'self'` ; un test rend chaque écran) ; session révocable en base (`admin/auth.py`, table `admin_sessions`).
- Le site est sur `champis.app`, l'API sur `api.champis.app` : tout changement de réponse qui sert le site se vérifie aussi dans `adhumi/champis-web` (CORS, en-têtes exposés).
- Toute décision structurante passe par la spec avant le code (skill `spec-sync`) ; en cas de doute, demander à Adrien.
