# champis-api

Service FastAPI : scoring météo `M × Φ` par cellule, indice par département pour la vue France, fichiers espèces, palette ; observations terrain et « mes coins » de l'app dans Postgres, avec la notification récapitulative du matin. Aucun calcul géographique (tout est dans les tuiles). Spec › « Backend sur Railway ».

```bash
uv run champis-pipeline weather-grid --depts 25        # produit data/weather_grid.json
uv run uvicorn champis_api.app:app --reload            # http://127.0.0.1:8000/docs
```

Les commandes `champis-api` partagent l'affichage du pipeline (étapes, statut, journal dans `data/logs/`, `-v`/`-q`) ; `make warm`, `make seed`, `make publish-seed`, `make notify` les lancent.

**Pré-chauffe** (pas de temps réel : décision du 17 oct. 2026) — `uv run champis-api warm` parcourt toute la grille à une requête toutes les 2 s et remplit le cache ; sort en code 1 si des cellules restent indisponibles. À lancer par cron une fois par seau de 12 h, par exemple :

```
0 3,15 * * *  cd /chemin/champis && uv run champis-api warm >> data/warm.log 2>&1
```

## Déploiement Railway

Un seul service, `Dockerfile` et `railway.toml` à la racine du dépôt (spec › Backend sur Railway).

1. Versionner la grille météo : `uv run champis-pipeline weather-grid --depts all`, puis `git add data/weather_grid.json` (exception à `data/` ignoré). L'amorçage, lui, va dans le bucket : `uv run champis-api seed` (base SQLite `data/weather_seed.db`, reprise possible) puis `uv run champis-api publish-seed` ; l'API le télécharge et l'importe au démarrage quand sa date change. L'ancien `data/weather_seed.json` du Doubs reste un repli local tant qu'aucun amorçage n'est publié.
2. Railway → New Project → Deploy from GitHub repo `adhumi/champis`, branche `main`. Le build utilise le `Dockerfile`.
3. Ajouter un **volume** monté sur `/data` (le cache SQLite `weather_cache.db` y vit ; sans volume, chaque déploiement repart à froid). Ajouter un service **Postgres** et le connecter au service API : Railway injecte `DATABASE_URL`, les tables se créent au démarrage.
4. Variables déjà fixées dans l'image : `CHAMPIS_WEATHER_GRID`, `CHAMPIS_CACHE_DB=/data/weather_cache.db`, `CHAMPIS_WARM_HOURS=3,15` (pré-chauffe intégrée à 3 h et 15 h Paris, pas de second service). **À ajouter : `CHAMPIS_OPEN_METEO_API_KEY`** (plan payant Open-Meteo, en variable secrète Railway : l'IP de sortie Railway est partagée et bridée sur l'API gratuite). Optionnel : `CHAMPIS_WARM_ON_START=1` pour chauffer au premier déploiement (puis à retirer : chaque redéploiement relancerait un passage complet), `CHAMPIS_CORS_ORIGINS` pour restreindre.
5. Health check : `/health`. La carte est servie par le même service sur `/web/` (l'URL de l'API y est pré-remplie avec l'origine de la page).

Les PMTiles ne sont pas encore servis par le service (`/tiles` et le bucket viennent plus tard) : sur la carte déployée, on les glisse depuis son disque comme en local.

| Variable | Défaut | Rôle |
| --- | --- | --- |
| `CHAMPIS_WEATHER_GRID` | `data/weather_grid.json` | Cellules météo (pipeline, étape `weather-grid`) |
| `CHAMPIS_WEATHER_SEED` | `data/weather_seed.json` | Amorçage local de repli (`.db` ou ancien `.json`), importé une fois si le bucket n'en publie pas |
| `CHAMPIS_WEATHER_SEED_DB` | `data/weather_seed.db` | Sortie de `champis-api seed`, source de `publish-seed` |
| `CHAMPIS_CACHE_DB` | `data/weather_cache.db` | Cache SQLite des réponses Open-Meteo |
| `CHAMPIS_OPEN_METEO_API_KEY` | *(vide)* | Clé du plan payant Open-Meteo (Standard) : prévision sur `customer-api` avec quota dédié ; vide = API gratuite |
| `AWS_ENDPOINT_URL`, `AWS_S3_BUCKET_NAME`, `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | *(vides)* | Bucket Railway des PMTiles, tels que les injecte « Connect Service to Bucket » avec le préréglage **AWS SDK (Generic)** ; vides = `/tiles` répond `configured: false`. `CHAMPIS_S3_ENDPOINT` / `CHAMPIS_S3_BUCKET` / `CHAMPIS_S3_REGION` priment s'ils sont posés |
| `CHAMPIS_TILES_MODE` | `presign` | `presign` : URL présignées lues par le navigateur ; `proxy` : relais `/tiles/{name}` (si le bucket n'envoie pas les en-têtes CORS) |
| `CHAMPIS_TILES_URL_HOURS` | `24` | Durée des URL présignées |
| `CHAMPIS_TILE_CACHE_DIR` | `<dossier de CHAMPIS_CACHE_DB>/tiles` (`/data/tiles` sur Railway) | Cache disque du relais (mode `proxy`) : une entrée par plage `Range` exacte relayée, servie ensuite sans repasser par le bucket |
| `CHAMPIS_TILE_CACHE_MB` | `2048` | Taille totale du cache disque en Mo, éviction des plages les moins lues au-delà ; `0` = désactivé |
| `CHAMPIS_CORS_ORIGINS` | `*` | Origines autorisées, séparées par des virgules |
| `CHAMPIS_WARM_HOURS` | *(vide)* | Heures Paris de la pré-chauffe intégrée, ex. `3,15` ; vide = cron externe (`champis-api warm`) |
| `CHAMPIS_WARM_ON_START` | `0` | `1` : pré-chauffe au démarrage |
| `CHAMPIS_SERVE_WEB` | `1` | Servir `web/`, `shared/`, `species/` en statique |
| `DATABASE_URL` (ou `CHAMPIS_DATABASE_URL`) | `sqlite:///data/champis.db` | Observations et coins : Postgres Railway (variable injectée par le service Postgres, « Connect ») ; en local sans Postgres, SQLite. Vide = `/observations` et `/coins` en 503 |
| `CHAMPIS_APNS_KEY`, `CHAMPIS_APNS_KEY_ID`, `CHAMPIS_APNS_TEAM_ID` | *(vides)* | Clé APNs `.p8` (contenu PEM en variable secrète, ou chemin), son identifiant, l'identifiant d'équipe Apple. Sans eux, aucune notification n'est envoyée |
| `CHAMPIS_APNS_TOPIC` | `fr.adhumi.champis` | Sujet APNs = identifiant de bundle de l'app |
| `CHAMPIS_NOTIFY_HOURS` | `7` | Heure(s) Paris de la notification « mes coins », après la pré-chauffe de 3 h ; vide = jamais |
| `CHAMPIS_RATE_READ`, `CHAMPIS_RATE_WRITE` | `120`, `20` | Requêtes par minute et par IP, lectures et écritures (`POST`/`PUT`/`DELETE`) ; au-delà 429 + `Retry-After` |
| `CHAMPIS_MAX_BODY` | `262144` | Taille maximale d'un corps d'écriture (octets) ; au-delà 413 |

## PMTiles : bucket et `/tiles`

Les tuiles ne tiennent ni dans l'image ni dans le dépôt (France ≈ 2–4 Go par espèce) : elles vont dans un **Railway Storage Bucket** (S3-compatible, privé, sortie gratuite par URL présignées). Procédure :

1. Créer le bucket dans le projet Railway, « Connect Service to Bucket » → service API, préréglage **AWS SDK (Generic)** ; sur ton poste, les cinq mêmes variables dans `.env` à la racine (`cp .env.example .env`).
2. Après `champis-pipeline tiles --depts …` : `uv run champis-pipeline publish --depts … [--species …]` téléverse les PMTiles manquants et réécrit `tiles/manifest.json` ; `--prune` supprime du bucket les PMTiles des autres jeux de départements (la carte n'affiche qu'un jeu, le plus couvrant) (`{version, generated, depts_by_hash, tiles:[{kind, species, hash, scoring_version, depts_hash, name, key, bytes}]}`).
3. La carte appelle `GET /tiles` au chargement et ouvre chaque fichier par son URL ; le glisser-déposer reste disponible. Si le navigateur refuse les URL présignées (CORS absent sur le bucket : le statut de la carte affiche « Load failed » / « Failed to fetch » par fichier et `0/N chargés`), passer `CHAMPIS_TILES_MODE=proxy` : l'API relaie les requêtes `Range` via `/tiles/{name}` (sortie service facturée, à éviter si `presign` marche).
4. En mode `proxy`, le relais garde sur le volume chaque plage `Range: bytes=a-b` exacte et bornée (≤ 4 Mo) qu'il a relayée (`CHAMPIS_TILE_CACHE_DIR`, `CHAMPIS_TILE_CACHE_MB` ; spec › Points ouverts › Tuiles hors du processus Python, 4 nov. 2026) : en-tête, répertoires et tuiles chaudes ne sautent plus vers le bucket à chaque visiteur. La réponse est la même qu'en relais (206, `content-range`, `etag` mémorisé, `cache-control` immutable) avec `x-cache: hit` ou `miss` ; les PMTiles étant immuables, rien n'expire. Une requête sans `Range` (fichier entier), ouverte ou en suffixe reste un relais en streaming, jamais mise en cache. Écriture atomique (temporaire puis `rename`) et lecture sans verrou : plusieurs workers uvicorn partagent le même dossier. Au-delà de la borne, les plages dont le mtime est le plus ancien sont supprimées hors du chemin des requêtes (nettoyage après écriture, 5 s au plus) ; `champis-api doctor` affiche le nombre de plages et la taille.

## Endpoints

- `GET /health` — `{"status": "ok", "cells": n, "seeded": n, "dept_index_at": "…"}` : `seeded` = cellules amorcées (climatologie du sol présente) ; doit valoir `cells` après l'import de l'amorçage ; `dept_index_at` = date du dernier calcul de l'indice par département (`null` avant la première pré-chauffe).
- `GET /departements[?species=<id>…][&day=<date>]` — indice par département pour la vue France (spec › Scoring) : `I = Σ T × M_eff × Φ / Σ T` sur les pixels forestiers du département, par cellule météo et classe d'altitude, pour chaque espèce et chaque jour J−3 → J+7 (`day` pour un seul jour) ; `coverage` = part de Σ T dont la météo était disponible ; par groupe, le maximum des espèces et l'espèce `leading` (règle 9). Calculé à la fin de chaque pré-chauffe sur le cache (aucun appel Open-Meteo), 503 tant qu'il ne l'a jamais été. Les poids Σ T viennent de `weather_grid.json` version 2 (`champis-pipeline weather-grid`) ; avec une grille version 1 l'indice n'est pas calculé.
- `GET /tiles` — `{configured, mode, generated, depts_by_hash, tiles:[{…, url}]}` ; `GET /tiles/{name}` — relais Range (mode `proxy` seulement).
- `GET /species` — `scoring_version`, `species_hash`, `palette` (`shared/palette.json`) et chaque `species/<id>.json` tel quel : les clients n'embarquent aucune valeur.
- `/web/` (statique, avec `SERVE_WEB`) — le site : `index.html` (Aujourd'hui), `carte.html`, `comprendre.html`, `soutenir.html` ; `/` redirige vers `/web/`.
- `/shared/texts/` (statique, avec `SERVE_WEB`) — les articles de Comprendre (`index.json` + Markdown), les mêmes mots pour l'app (copiés à la compilation) et le site ; les fiches d'espèces sont le bloc `about` de `GET /species`.
- `GET /weather?cell=<id>` ou `?lat=&lon=` (cellule la plus proche), `&species=<id>` répétable (défaut : toutes). Pour chaque jour de **J−3 à J+7** et chaque **classe d'altitude** de la cellule (centres 150, 450, 750… m) : `M`, `M_eff`, `phi` et `factors` (`temp`, `rain`, `trigger`, `soil_moisture`, `frost`, `water_balance`, `heat`, `memory`). Lot 2 de la refonte UX (1er nov. 2026) : chaque ligne porte aussi `trigger_days` (âge en jours de la pluie déclenchante retenue, `null` sans événement) et `summary`, deux à huit phrases courtes en français déduites des facteurs avec des seuils génériques (`champis_api/explain.py`) ; les clients les affichent telles quelles et ajoutent seuls la phrase de terrain depuis le libellé de palette de T. Plus `stale` (cache périmé servi faute d'Open-Meteo), `cell_elevation_m`, `scoring_version`, `species_hash`.
- `GET /weather/bbox?min_lon&min_lat&max_lon&max_lat[&species][&day]` — même chose pour toutes les cellules de l'emprise (chaque `cell` avec `id`, `lat`, `lon`, `alt_classes`, `elevation_m`), sans `factors`, pour peindre la carte d'un coup. Servi depuis la **météo précalculée** à la dernière pré-chauffe (table `cell_weather`, remplie par la même passe que l'indice par département ; `computed_at` l'horodate) : aucun recalcul ni appel Open-Meteo, une emprise d'écran entière tient dans une réponse, jusqu'à 5 000 cellules (au-delà, 422 « zoomez » : ce n'est plus un écran). Une cellule que la pré-chauffe n'a pas est calculée à la demande, 40 par requête au plus, et rangée pour la suivante ; les autres sont listées dans `unavailable`, comme les cellules sans météo (Open-Meteo injoignable, rien en cache), avec `retry_after` quand Open-Meteo l'a indiqué, au lieu de faire échouer la réponse. Entre minuit et la pré-chauffe de 3 h, les lignes de la veille couvrent J−4 → J+6 : J+7 manque. Réponses JSON compressées (gzip) au-delà de 1 Kio.

Le client choisit la classe d'altitude de son pixel ; le score affiché est `T × M_eff × phi`.

### Retour terrain et mes coins (app seulement)

Identité : en-tête `X-Champis-User`, un UUID généré par l'app au premier lancement et gardé dans le trousseau iCloud. Pas de compte. Tout ce qui suit est privé à cet identifiant ; `DELETE /observations` efface tout (observations, coins, jeton) : droit RGPD.

- `POST /observations` — `{"observations": [{id, species, lat, lon, accuracy_m?, date, found, quantity?}]}` (≤ 200), 201 → `{"inserted", "duplicates"}`. Idempotent par `id` : renvoyer un lot après une coupure réseau ne crée rien. `quantity` 1–3 obligatoire si `found`, interdite sinon ; pas de photo, pas de texte libre.
- `GET /observations` — les miennes, les plus récentes d'abord.
- `DELETE /observations` — `{"deleted": n}`.
- `PUT /coins` — `{"apns_token"?, "sandbox"?, "coins": [{id, name, lat, lon, target, alt_class, t}]}` (≤ 50) : remplace la liste complète (le serveur ne fusionne pas) et le jeton de notification. `target` = espèce ou groupe affiché ; `alt_class` = centre de la classe d'altitude du pixel (couche A) ; `t` = `{espèce: T}` lu par l'app dans les tuiles au moment où le coin est créé — le backend n'a pas les tuiles.
- `GET /coins` — mes coins avec `score` du jour (max sur les espèces de `T_e × M_eff_e × Φ_e`, cellule la plus proche, classe d'altitude du coin), `leading`, et `best_day`/`best` si un jour de J+1 à J+7 fait mieux.

**Notification** (`champis_api.coins.notify_all`, tâche intégrée à `CHAMPIS_NOTIFY_HOURS`, ou `champis-api notify` à la main) : après la pré-chauffe du matin, une seule notification par appareil, récapitulative, envoyée seulement si au moins un coin est « bon » ou « excellent » (seuil lu dans `shared/palette.json`). Un jeton refusé par APNs (`Unregistered`, `BadDeviceToken`) est retiré, on n'insiste pas. L'app ne fait aucun rafraîchissement en arrière-plan.

**Protection** (service public) : seau à jetons par IP (`X-Forwarded-For` derrière Railway), 120 lectures et 20 écritures par minute par défaut, corps d'écriture ≤ 256 Ko, lots bornés, validation stricte des champs (espèces connues, France métropolitaine, dates non futures).

## Open-Meteo (skill `open-meteo`)

`weather_client.py` est le seul module qui appelle Open-Meteo. Décision du 17 oct. 2026, **« amorcer, puis accumuler »** :

| Donnée | Source | Quand |
| --- | --- | --- |
| Prévision J−n → J+7 (températures, `rain_sum`, neige, ET0, humidité du sol 0–9 cm), n = jours manquants dans l'historique, 1 à 92 | API prévision, **plan payant** (`customer-api`, clé `CHAMPIS_OPEN_METEO_API_KEY`) | En service, un appel par cellule et par seau de 12 h |
| Historique J−200 → J−n | Les jours passés de chaque prévision, **accumulés** dans SQLite (`history`) | Automatique, à chaque prévision fraîche |
| Climatologie du sol (10 ans → 11 quantiles/mois) | **Pipeline** `champis-pipeline soil-clim` : ERA5-Land 0–7 cm via le Climate Data Store (gratuit, compte CDS), quantiles par pixel 0,1° | Une fois → `data/weather_seed.db` (`soil_clim`), publié dans le bucket (`publish-seed`), importé au démarrage |
| Historique initial J−200 → J−1 | **Amorçage** `champis-api seed` sur l'archive ERA5-Land gratuite, hors Railway, 10 appels pondérés par cellule (200 jours × 7 variables) : France ≈ 227 000 appels, soit ≈ 23 jours sur l'archive gratuite (10 000 par jour) ou quelques heures avec une clé Professional ; en attendant, `past_days=92` puis accumulation (cellule `unavailable` avec sa date de complétude tant que la fenêtre mémoire n'est pas couverte, ≈ 41 j) | Une fois par grille → même base, mêmes publication et import |

L'API historique n'est pas dans le plan Standard (Professional seulement, pris le temps de l'amorçage France) et l'IP de sortie Railway est bridée sur l'API gratuite : en service, Railway ne parle donc **qu'à la prévision**, en ne demandant que les jours de passé absents de l'historique (`past_days` 1 à 92 ; poids Open-Meteo = points × jours/14 × variables/10, journalisé à chaque pré-chauffe). Les dates d'amorçage sont remplacées par la prévision dès qu'elle les couvre (même modèle avant et après J). Une valeur absente de la prévision et de l'historique → 502 nommant la variable et les dates, jamais une approximation.

```bash
uv run champis-pipeline soil-clim           # climatologie ERA5-Land (CDS) → data/weather_seed.db, une fois
uv run champis-api publish-seed             # → bucket weather/weather_seed.db + weather/weather_seed.json ; l'API l'importe au prochain démarrage
uv run champis-api seed                     # historique J−200 depuis l'archive gratuite, lots de 50, 1 s d'intervalle ; France ≈ 23 jours sur l'archive gratuite (quota 10 000/j), quelques heures en Professional
uv run champis-api publish-seed             # republier : l'API réimporte (ATTACH, sans rien charger en mémoire ; l'historique amorcé ne remplace jamais la prévision)
```

`seed --climatology` demande aussi la climatologie à Open-Meteo pour les cellules qui n'en ont pas (26 appels pondérés par cellule : un petit secteur, ou avec la clé sur `customer-archive-api`, plan Professional). Un 429 (quota) ne fait pas échouer la commande : elle attend (`Retry-After`, sinon 1 → 15 min) et reprend le même lot ; laisse-la tourner. Reprise possible (les cellules déjà dans la base ne sont pas refaites). Après un `weather-grid` qui ajoute des cellules, relancer `soil-clim`, `seed` puis `publish-seed` : seules les nouvelles sont faites.

**Quotas** : lissage 250 ms entre requêtes (2 s en pré-chauffe, 1 s en amorçage), lots de 100 points, `/weather/bbox` servi depuis le précalcul et limité à 40 cellules calculées à la demande par requête (5 000 cellules par emprise, sinon 422 « zoomez »), 429 court réessayé une fois sinon indisponibilité (cache périmé, cellule `unavailable`, 503 + `Retry-After`). Une cellule sans climatologie amorcée est `unavailable` avec la raison « amorçage requis ».

## Tests

`uv run pytest api/tests` — sans réseau : `tests/fixtures/open_meteo.py` fabrique des réponses Open-Meteo dans la forme exacte de l'API à partir d'un `Scenario` (pluie, neige, gel, panne…). Scénarios couverts : grosse pluie puis beau temps, petite pluie récente après grosse pluie ancienne, épisode neigeux, gel en altitude seulement, panne (cache périmé puis 503), cache par seau de 12 h, trou de série, pré-chauffe.
