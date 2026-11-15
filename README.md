# Champis — pipeline terrain et API météo

Champis estime où et quand cueillir des champignons en France, à l'échelle de la parcelle forestière : [champis.app](https://champis.app). Ce dépôt calcule le potentiel terrain de chaque pixel de 25 m (`pipeline/`) et sert l'indice météo du jour (`api/`, sur `api.champis.app`). Le score d'un pixel pour une espèce et un jour est `S = T × M_eff × Φ` : potentiel terrain, indice météo, fenêtre phénologique ; le modèle complet est dans `champis/species/SCHEMA.md`.

Il fait partie de quatre dépôts :

| Dépôt | Contenu |
| --- | --- |
| `adhumi/champis` | le référentiel : fichiers d'espèces et `SCHEMA.md`, table BD Forêt → hôtes, palette, vecteurs de parité, textes du Guide ; ici en sous-module `champis/` |
| `adhumi/champis-api` (ce dépôt) | le pipeline terrain et l'API, avec ses crons de pré-chauffe et de notification |
| `adhumi/champis-web` | le site `champis.app`, service statique |
| `adhumi/champis-ios` | l'app iOS, iPadOS et macOS |

## Prérequis

- git, avec accès au sous-module du référentiel (sans clé SSH chez GitHub : [`CONTRIBUTING.md`](CONTRIBUTING.md) › Cloner sans clé SSH) ;
- Python 3.12 ou plus récent et [uv](https://docs.astral.sh/uv/) ;
- pas de GDAL système à installer : rasterio embarque le sien ;
- pour le pipeline seulement, environ 5 Go de disque par département.

## Installer et lancer en local

```bash
git clone --recurse-submodules https://github.com/adhumi/champis-api
cd champis-api
make setup        # uv sync --group dev, et .env créé depuis .env.example s'il manque
make serve        # API locale avec rechargement : http://localhost:8000/health
```

`make help` liste toutes les commandes (développement, pipeline, publication, météo). Le fichier `.env` est lu par les CLI et l'API, jamais commité ; il peut rester tel quel pour commencer.

### Ce qui marche sans données ni compte

- **Les tests** (`make check`) : sans réseau, sur des données synthétiques.
- **L'API locale.** Le dépôt versionne la grille météo de la France (`data/weather_grid.json`) et un amorçage du Doubs (`data/weather_seed.json`). Sans aucune variable, `make serve` répond donc à `/health`, à `/species` (toutes les espèces du référentiel) et à `/weather` pour un point du Doubs, par exemple `http://localhost:8000/weather?lat=47.0&lon=6.4&species=boletus_edulis`. La météo vient alors de l'API gratuite d'Open-Meteo, sans clé : il faut du réseau. Hors du Doubs, une cellule n'a pas d'amorçage : l'API le dit (« amorçage requis ») au lieu de donner un score.
- Les observations et les coins vont dans une base SQLite locale (`data/champis.db`). Sans bucket, `/tiles` répond `configured: false`. Sans `CHAMPIS_ADMIN_KEY`, le backoffice `/admin` répond 404.
- Le site en local : `make serve` dans `adhumi/champis-web`, qui attend cette API sur le port 8000.

### Ce qui demande des téléchargements ou des comptes

| Pour | Il faut |
| --- | --- |
| Le pipeline terrain d'un département (`make dept DEPT=25`) | Des téléchargements lourds, sans compte : BD Forêt et BD TOPO (IGN), SoilGrids (ISRIC), Copernicus DEM ; environ 5 Go de disque, plusieurs minutes par espèce pour les tuiles |
| Le contrôle GBIF (`make gbif-check`) | Du réseau, sans compte |
| La climatologie du sol (`make soil-clim`) | Un compte Climate Data Store, gratuit (`CDSAPI_URL`, `CDSAPI_KEY`) |
| L'amorçage de la France (`make seed`) | Environ 23 jours sur l'archive gratuite d'Open-Meteo, ou quelques heures avec une clé du plan Professional |
| La publication des tuiles, de l'amorçage et du cache (`make publish`, `make publish-seed`, `make warm-cron`) | Un bucket compatible S3 (variables `AWS_*`) |
| La production | Une clé du plan payant d'Open-Meteo (`CHAMPIS_OPEN_METEO_API_KEY`), Postgres (`DATABASE_URL`), le bucket, et pour les notifications une clé APNs d'un compte développeur Apple (`CHAMPIS_APNS_*`) |

Le détail des étapes du pipeline, de leurs sorties et de leur affichage : [`docs/pipeline.md`](docs/pipeline.md). Les routes de l'API, ses variables, le backoffice, la mesure minimale et Open-Meteo : [`api/README.md`](api/README.md). Pour regarder des tuiles produites en local : la carte de `adhumi/champis-web` en mode développement (son `docs/web.md`).

## Tester

```bash
make check        # exactement la CI : Ruff, tests avec couverture ≥ 90 %, vecteurs de parité, fichiers d'espèces
```

`make test ARGS="-k batch"` filtre les tests ; `make coverage` détaille les lignes non couvertes ; `make fmt` corrige le format. La suite compte environ 500 tests et prend quelques minutes. Ce que chaque fichier de test garantit : [`docs/tests.md`](docs/tests.md).

## Structure du dépôt

```
champis/      sous-module adhumi/champis : species/ (+ SCHEMA.md, validate.py), mapping/, shared/ (palette, vecteurs, textes)
pipeline/     champis_pipeline : download → grid → terrain → tiles ; sorties à committer déposées dans le bucket
api/          champis_api : FastAPI, scoring M/Φ, vecteurs, mesure minimale, backoffice /admin ; README.md : la référence de l'API
data/         téléchargements et rasters, jamais commités (sauf weather_grid.json et weather_seed.json)
docs/         pipeline.md, tests.md, deploiement.md, backoffice-bo6.md
.railway/     railway.ts : les services Railway décrits en code
Dockerfile    l'image commune à l'API, aux deux crons et au service d'exécution
Makefile      toutes les commandes, documentées (make help)
```

Deux paquets dans le workspace uv : `champis-pipeline` et `champis-api`, qui dépend du premier. Trois commandes : `champis-pipeline`, `champis-api`, `champis-vectors`.

## Déploiement

Railway, quatre services construits par le même `Dockerfile` : l'API (`champis`), deux crons (`champis-warm` pour la pré-chauffe météo, `champis-notify` pour la notification « mes coins ») et un service d'exécution (`champis-jobs`) pour les tâches lancées depuis le backoffice. S'y ajoutent un volume pour le cache météo, un bucket pour les tuiles et le cache publié, et Postgres pour les observations et les coins.

Railway ne récupère pas les sous-modules : c'est la CI qui déploie, après des tests verts sur `main`, avec `railway up` (`.github/workflows/ci.yml`). Secrets de la CI, sans valeur dans le dépôt : `RAILWAY_TOKEN`, et `CHAMPIS_REFERENTIEL_TOKEN` tant que le référentiel est privé.

Les variables des services sont listées, avec leur défaut et leur rôle, dans [`api/README.md`](api/README.md) › Déploiement Railway ; `.env.example` en donne la forme. Les valeurs secrètes restent dans Railway, jamais dans le dépôt. Vue d'ensemble des services : [`docs/deploiement.md`](docs/deploiement.md) ; description en code : `.railway/railway.ts`. `make doctor` dit si un environnement est prêt.

## Contribuer, signaler une faille

- Contribuer : [`CONTRIBUTING.md`](CONTRIBUTING.md). Projet personnel : les pull requests sont bienvenues, relues quand l'auteur le peut.
- Signaler une faille : [`SECURITY.md`](SECURITY.md), par courriel à hello@champis.app, pas par une issue.

## Licences

Code de ce dépôt : MIT ([`LICENSE`](LICENSE)). Code et contenus de tiers copiés dans le dépôt (Tabler, htmx, skill `modern-python`) : [`THIRD_PARTY.md`](THIRD_PARTY.md). Le référentiel (`champis/`) porte ses propres licences : `champis/LICENSE` et `champis/LICENSE-DATA.md`.

### Licences des données

Ce que le pipeline et l'API téléchargent, et sous quelles conditions :

- **BD Forêt V2** © IGN, Licence Ouverte 2.0 (Etalab).
- **BD TOPO V3** © IGN, Licence Ouverte 2.0 (Etalab) : zones de végétation hors forêt, tronçons et surfaces hydrographiques (palier 3).
- **Copernicus DEM** (GLO-30) : © DLR e.V. 2010-2014 et © Airbus Defence and Space GmbH 2014-2018, fourni sous licence COPERNICUS par l'Union européenne et l'ESA ; tous droits réservés.
- **SoilGrids** © ISRIC, CC-BY 4.0.
- **Open-Meteo** : Weather data by Open-Meteo.com, CC-BY 4.0.
- **ERA5-Land** (climatologie du sol, `champis-pipeline soil-clim`, et archive de l'amorçage) : Copernicus Climate Change Service ; contient des informations Copernicus modifiées.
- **GBIF** : occurrences utilisées pour un contrôle interne, non commercial, de la crédibilité des cartes (`champis-pipeline check`) ; licences CC0, CC-BY ou CC-BY-NC selon le jeu.
