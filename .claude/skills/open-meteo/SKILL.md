---
name: open-meteo
description: Comment Champis consomme Open-Meteo (variables, deux API, correction d'altitude, cache, quotas) et calcule l'indice météo M. À lire avant tout code qui appelle Open-Meteo ou touche api/champis_api/weather*.
---

# Open-Meteo dans Champis

Un seul module client : `api/champis_api/weather_client.py`. Personne d'autre n'appelle Open-Meteo. **Plan payant avec clé API** (décision du 17 oct. 2026, après des 429 depuis l'IP partagée de Railway) : `CHAMPIS_OPEN_METEO_API_KEY` bascule le client sur `customer-api.open-meteo.com` / `customer-archive-api.open-meteo.com` (`apikey=`), quota dédié ; sans clé, API gratuite (~10 000 appels pondérés/jour) pour le dev local. Mention « Weather data by Open-Meteo.com » dans l'« à propos » des clients.

## Deux API, deux rôles — « amorcer, puis accumuler » (décision du 17 oct. 2026)

| API | URL | Sert à | Quand |
| --- | --- | --- | --- |
| Prévision (plan payant, clé) | `https://customer-api.open-meteo.com/v1/forecast` avec `past_days=<jours manquants dans l'historique, 1 à 92>&forecast_days=8` | J−n → J+7 : temp, rain, trigger, humidité du sol, frost, water_balance, heat, persistance. Ses jours passés sont **accumulés** dans `history` (SQLite) : au bout de quelques jours l'historique couvre J−200 avec le même modèle que la prévision | En service, Railway, un appel par cellule et par seau de 12 h |
| Archive (ERA5-Land) | `https://customer-archive-api.open-meteo.com/v1/archive` avec la clé (plan **Professional** requis, pris le temps de l'amorçage France ; sans clé, `archive-api` gratuite pour le développement), journalier (`soil_moisture_0_to_7cm_mean` inclus) | **Amorçage seulement** (`champis-api seed`, hors Railway) : historique initial J−200 → J−1 (lots de 50, ≈ 1 appel pondéré par cellule) ; la climatologie du sol (10 ans → 11 quantiles/mois) vient du pipeline (`champis-pipeline soil-clim` : jeu CDS `derived-era5-land-daily-statistics`, `volumetric_soil_water_layer_1` 0–7 cm, moyenne journalière, 0,1°, une requête par année sur l'emprise France, quantiles par mois et par pixel, cellule → pixel valide le plus proche ; identifiants `CDSAPI_URL`/`CDSAPI_KEY` ; option a du 25 oct. 2026 ; `seed --climatology` reste possible pour un petit secteur) → base SQLite `data/weather_seed.db` (schéma du cache), publiée dans le bucket par `champis-api publish-seed` (`weather/weather_seed.db` + fiche JSON) ; l'API la télécharge et l'importe par ATTACH au démarrage quand la date `generated` change (table `meta`) | Une fois par grille |

Pourquoi : l'API historique n'est pas dans le plan Standard (seulement Professional), et l'IP de sortie Railway est bridée (429) sur l'API gratuite. En service, l'API ne parle donc **qu'à la prévision**. Fusion par variable et par jour (`weather_service.build_series`) : **prévision si présente, sinon historique** ; une valeur absente des deux → 502 nommant la variable et les dates.

## Variables (`daily=`)

`temperature_2m_mean,temperature_2m_min,temperature_2m_max,rain_sum,snowfall_sum,et0_fao_evapotranspiration`. Humidité du sol : en archive, `daily=soil_moisture_0_to_7cm_mean` (journalier, jamais horaire : trop lourd pour le quota) ; en prévision il n'y a que l'horaire par couche `soil_moisture_0_to_1cm` / `1_to_3cm` / `3_to_9cm` → moyenne journalière pondérée 1/2/6 (≈ 0–9 cm). `timezone=Europe/Paris`. Toujours lire `elevation` de la réponse : c'est l'altitude de la cellule, nécessaire à la correction.

**`rain_sum`, jamais `precipitation_sum`.** Un jour avec `snowfall_sum > 0` n'est pas un jour de pluie déclenchante.

## Grille et cache

- L'historique (`history`, source `forecast` écrase, `seed` ne comble que les trous ; jours complets seulement ; purge au-delà de 400 j) et la climatologie (`soil_clim`) vivent dans le même SQLite que le cache. Une cellule sans climatologie amorcée est `unavailable` (« amorçage requis »).
- Les cellules sont les points de `data/weather_grid.json` (étape `weather-grid` du pipeline : carrés de 5 km alignés Lambert-93 où `T > 0` pour au moins une espèce ; `{version: 1, cell_m: 5000, cells: [{id, lat, lon, alt_classes, elevation_m}]}`, `id` = coin SO en km, `alt_classes` = centres des classes de 300 m sous la forêt). ~25 000 cellules France. Lu par `champis_api.grid`.
- Cache des réponses brutes Open-Meteo **12 h par cellule** (prévision ; archive 1 jour ; climatologie permanente), clé `cell_id:kind:bucket`, stockage SQLite (`weather_cache.db` sur le volume Railway). Le scoring se recalcule à partir du cache, jamais de nouvel appel pour changer d'espèce ou de jour. Pas de temps réel : décision du 17 oct. 2026.
- **Quotas** : Open-Meteo pondère chaque requête : appels = points × jours/14 × variables/10, fractions comptées (page tarifs, 25 oct. 2026 ; `call_weight()` dans le client, cumul `client.weight`, journalisé par passage de pré-chauffe). Gratuit ≈ 600 appels pondérés/min, 5 000/h, 10 000/j ; Standard 1 M/mois ; Professional 5 M/mois. **`past_days` dynamique** (`past_days_needed`) : seulement les jours absents de l'historique SQLite, borné à [1, 92] ; en régime établi ≈ 0,6 appel par cellule et par passage (France : ≈ 0,8 M/mois pour deux passages par jour, dans le plan Standard) contre ≈ 5 avec `past_days=60`. Amorçage France : historique ≈ 1 appel par cellule (2–3 jours sur l'archive gratuite) ; la climatologie (26 par cellule) vient du CDS, pas d'Open-Meteo. Historique trop court pour la fenêtre mémoire (130 j pour B. edulis) → cellule `unavailable` avec sa date de complétude (`HistoryTooShortError`), jamais une fenêtre tronquée ; l'accumulation comble en ≈ 41 j après le premier passage. Règles : jamais d'horaire hors prévision ; lots de 100 (prévision) / 50 (archive) / 10 (climatologie) points ; 250 ms minimum entre deux requêtes ; `/weather/bbox` sert la météo **précalculée** à chaque pré-chauffe (table `cell_weather`, remplie par la passe de l'indice par département ; décision du 2 nov. 2026) et ne calcule à la demande que 40 cellules absentes par requête, les autres sont `unavailable` ; une emprise de plus de 5 000 cellules, qui n'est plus celle d'un écran, reste un 422 « zoomez » ; un 429 à courte attente (`Retry-After` ≤ 10 s) est réessayé **une** fois, sinon c'est une indisponibilité (cache périmé, sinon cellule `unavailable` dans `/weather/bbox`, 503 + `Retry-After` sur `/weather`) ; une cellule indisponible ne bloque jamais les autres. Le passage France entière passe par le plan payant (clé API), décidé le 17 oct. 2026.
- **Pré-chauffe** `uv run champis-api warm` (service cron Railway `champis-warm` défini dans `.railway/railway.ts`, 2 h et 14 h UTC : un passage par seau de 12 h ; décision du 4 nov. 2026, hors de l'API qui n'a plus de tâche de fond) : parcourt toute la grille au rythme d'une requête toutes les 2 s, remplit le cache, puis calcule pour chaque cellule et chaque espèce les 11 jours de `M`, `M_eff`, `Φ` par classe d'altitude (≈ 14 ms par cellule et par espèce) : l'indice par département en découle, et les lignes sont rangées dans `cell_weather` pour `/weather/bbox`. Le cron n'a pas de volume : `--pull` repart de l'instantané du cache publié dans le bucket (`weather/weather_cache.db`, `champis_api.snapshot`), `--publish` en dépose un nouveau que le worker meneur de l'API importe (`CHAMPIS_CACHE_POLL_MINUTES`). Sort en erreur si des cellules restent indisponibles. En journée, un appel `/weather` sur une cellule froide déclenche encore un fetch à la demande (filet de sécurité).
- Si Open-Meteo est injoignable : servir le cache périmé avec `"stale": true` dans la réponse, jamais une erreur 500 côté client.

## Correction d'altitude

Pour chaque classe d'altitude `a` de la cellule (centres 150, 450, 750… m) : `t_corr = t_cell − 0,006 × (a − elevation_cell)` appliqué à mean, min et max. L'API renvoie `M` **par classe d'altitude** ; le client choisit la classe du pixel. Sans cela, le gel n'est jamais détecté en montagne.

## Calcul de `M` (`api/champis_api/scoring.py`)

Règles dans `species/SCHEMA.md` et skill `champis-model`. Rappels d'implémentation :
- Fenêtres glissantes calculées pour chaque J de J−`persistence.days` à J+7 (la persistance a besoin des M passés).
- `trigger` : parcourir tous les événements (`≥ event_mm` sur `event_days` jours consécutifs, hors neige) des `lag_days.max` derniers jours ; facteur = max des trapèzes.
- `soil_moisture_pct` : percentile de la valeur du jour (prévision, 0–9 cm) dans la distribution ERA5-Land 0–7 cm de la cellule pour le même mois sur 10 ans : 11 quantiles par mois, **amorcés une fois** par `champis-api seed` (archive, hors Railway) et importés dans SQLite (`soil_clim`), interpolation linéaire. Deux modèles différents : seul le percentile est utilisé, jamais la valeur absolue (décision du 17 oct. 2026).
- `water_balance` : `Σ(rain_sum − et0)` sur `window_days`.
- `memory` : cumul `rain_sum` sur `[J−118, J−28]` (archive).
- Réponse `/weather` : pour chaque jour, `M`, `Φ`, `M_eff` par classe d'altitude **et le détail des facteurs** (`temp`, `rain`, `trigger`, `soil_moisture`, `frost`, `water_balance`, `heat`, `memory`) : la fiche « pourquoi c'est vert » en dépend.

## Tests

- Fixtures JSON de réponses Open-Meteo enregistrées (`tests/fixtures/open_meteo/*.json`), jamais d'appel réseau dans `pytest`.
- Scénarios obligatoires : grosse pluie puis beau temps (trigger optimal, water_balance neutre), petite pluie récente après grosse pluie ancienne (le max gagne), épisode neigeux (pas de trigger), gel en altitude seulement (correction), Open-Meteo en panne (stale).
- Vecteurs de parité : skill `scoring-parity`.
