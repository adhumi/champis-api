# Champis — monorepo

Cartes de cueillette de champignons (France, 25 m) : pipeline terrain, API météo, site champis.adhumi.fr. Usage perso et proches, pas commercial. La spec vit dans le doc « Spec — Champis » ; les skills `.claude/skills/*` disent comment travailler ici — lis `champis-model` avant tout code qui touche un score.

## Structure

```
species/      fichiers espèces JSON + SCHEMA.md + validate.py   (skill species-file)
mapping/      bdforet_to_hosts.json : 32 codes TFV → hôtes
shared/       palette.json, vectors/ (contrat de parité JS/Swift)   (skill scoring-parity)
pipeline/     champis_pipeline : download → grid → terrain → tiles   (skill geo-pipeline)
api/          champis_api : scoring M/Φ, vecteurs, [FastAPI à venir] (skill open-meteo)
web/          [site à venir]
data/         téléchargements et rasters, jamais commités
```

## Outillage

- `uv sync --group dev` ; `uv run pytest` ; `uv run ruff check . && uv run ruff format .`
- Deux packages du workspace : `champis-pipeline` et `champis-api` (qui dépend du premier).
- Python 3.12+, numpy, rasterio, geopandas ; **aucun outil GDAL en ligne de commande requis** (tout passe par rasterio).
- `uv run champis-vectors` régénère `shared/vectors/` ; `--check` en CI. Si tu modifies le scoring, régénère.
- Ne pas migrer ou supprimer l'outillage existant (skill `modern-python` donne les conventions, pas des ordres de migration) ; `ty` est optionnel.

## Commandes du pipeline

```
uv run champis-pipeline download --dept 25
uv run champis-pipeline grid     --dept 25
uv run champis-pipeline terrain  --dept 25 [--species boletus_edulis]
uv run champis-pipeline summary  --dept 25
uv run champis-pipeline tiles    --depts 25 [--species …]
uv run champis-pipeline all      --dept 25
uv run champis-pipeline batch    --depts all --jobs 3   # download+grid+terrain, parallèle, reprise
uv run champis-pipeline check    --dept 25              # contrôle GBIF
uv run champis-pipeline explain  --dept 25 --species boletus_edulis
uv run champis-pipeline publish  --depts 25            # PMTiles → bucket Railway + manifeste
```

## Règles

- Jamais de valeur d'espèce dans le code : tout vient de `species/*.json`.
- Jointure BD Forêt sur `CODE_TFV` uniquement. `rain_sum`, jamais `precipitation_sum`.
- Tests sans réseau : fixtures et données synthétiques (`pipeline/tests/test_pipeline_e2e.py` montre comment).
- pytest : `[tool.pytest.ini_options]` à la racine ; `pytest.approx` pour les flottants, `numpy.testing` pour les tableaux.
- FastAPI (à venir) : `Annotated[...]` pour les dépendances, un `APIRouter` par domaine, `lifespan` pour le cache météo ; tests avec `httpx.ASGITransport(app=app)` et `pytest_asyncio.fixture`.
- Toute décision structurante passe par la spec avant le code (skill `spec-sync`) ; en cas de doute, demander à Adrien.
