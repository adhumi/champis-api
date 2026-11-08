# Champis — pipeline terrain et API météo

Cartes de cueillette de champignons (France, 25 m) : pipeline terrain et API météo de champis.app, déployés sur Railway. Pensé d'abord pour Adrien et ses proches, puis ouvert à tous, infrastructure financée par dons (spec, 30 oct. 2026). La spec vit dans le doc « Spec — Champis » ; les skills `.claude/skills/*` disent comment travailler ici — lis `champis-model` avant tout code qui touche un score.

Découpage des dépôts (spec › Produit › Étape C, lot C7, 6 nov. 2026) : `adhumi/champis` est le référentiel (espèces, table BD Forêt, palette, vecteurs, textes), ici en sous-module `champis/` ; `adhumi/champis-web` est le site, `adhumi/champis-ios` l'app.

## Structure

```
champis/      sous-module adhumi/champis : species/ (+ SCHEMA.md, validate.py), mapping/, shared/ (palette, vecteurs, textes)
pipeline/     champis_pipeline : download → grid → terrain → tiles ; artifacts.py (sorties à committer, déposées dans le bucket par le worker)   (skill geo-pipeline)
api/          champis_api : FastAPI, scoring M/Φ, vecteurs, mesure minimale, backoffice /admin (admin/, jobs.py, tools.py, worker.py, render.py)   (skill open-meteo)
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
- Jointure BD Forêt sur `CODE_TFV` uniquement. `rain_sum`, jamais `precipitation_sum`.
- Tests sans réseau : fixtures et données synthétiques (`pipeline/tests/test_pipeline_e2e.py` montre comment).
- pytest : `[tool.pytest.ini_options]` à la racine ; `pytest.approx` pour les flottants, `numpy.testing` pour les tableaux.
- FastAPI : `Annotated[...]` pour les dépendances, un `APIRouter` par domaine, `lifespan` pour le cache météo ; tests avec `httpx.ASGITransport(app=app)` et `pytest_asyncio.fixture`.
- Le site est sur `champis.app`, l'API sur `api.champis.app` : tout changement de réponse qui sert le site se vérifie aussi dans `adhumi/champis-web` (CORS, en-têtes exposés).
- Toute décision structurante passe par la spec avant le code (skill `spec-sync`) ; en cas de doute, demander à Adrien.
