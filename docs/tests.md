# Les tests

Ce que la suite garantit, fichier par fichier. Ce tableau vient du `README.md` d'avant sa refonte (15 nov. 2026). Les scénarios météo des tests de l'API sont décrits dans `api/README.md` › Tests.

`make test` (ou `uv run pytest`) tourne sans réseau : fixtures synthétiques (département de 10 km × 10 km, faux Open-Meteo, faux bucket). La suite est écrite à partir de la spec et de `champis/species/SCHEMA.md` ; chaque fichier de test cite la règle qu'il protège.

| Fichiers | Ce qu'ils garantissent |
| --- | --- |
| `pipeline/tests/test_spec_contracts.py` | Palette et seuils de la spec, fichiers espèces et table TFV valides, aucune valeur d'espèce dans le code, noms de tuiles et manifeste, forme de `weather_grid.json` |
| `pipeline/tests/test_terrain.py`, `test_pipeline_e2e.py` | T = hôte × Σ poids × facteur, règles dures, chaîne grid → terrain → tuiles → grille météo, extraction par département, GBIF, explain, publication |
| `pipeline/tests/test_rasterize.py`, `test_download.py` | Jointure `CODE_TFV` exacte, petits polygones gagnants, pH nodata jamais moyenné, pente et exposition ; téléchargements idempotents, md5, WCS en erreur, dalles DEM absentes |
| `pipeline/tests/test_cli.py`, `test_console.py`, `test_status.py`, `test_storage_bucket.py` | Les commandes, leur affichage et leurs codes de retour ; `status`, `publish --dry-run` ; le bucket S3 via botocore |
| `pipeline/tests/test_group_tiles.py` | Les PMTiles G : empilement des canaux depuis des jeux T synthétiques, 255 pour une espèce sans tuile, 4e canal des girolles, idempotence, jeu par département, nom, manifeste, `--groups` |
| `api/tests/test_scoring.py`, `test_vectors.py`, `test_spec_rules.py` | M, Φ, M_eff, trigger « meilleur événement », correction d'altitude, gel, chaleur, mémoire, persistance, groupes ; vecteurs de parité avec l'app |
| `api/tests/test_weather_api.py`, `test_departements.py`, `test_observations.py`, `test_coins.py`, `test_ratelimit.py` | Contrats des endpoints, cache 12 h, pannes et quotas Open-Meteo, amorçage, indice par département, retour terrain, coins et notifications, limitation de débit |
| `api/tests/test_app_lifespan.py`, `test_api_cli.py`, `test_doctor.py` | Démarrage réel de l'API en mode dégradé, planificateurs, commandes `champis-api` de bout en bout |
| `api/tests/test_admin_pages.py` | Pages du backoffice (lot BO6) : une adresse par écran, connexion et retour à la page demandée, jeton `csrf`, POST puis redirection, tri, filtres et page dans la query string, fragments htmx, erreurs par champ, confirmations, états vides, sans base ni bucket |
| `api/tests/test_admin_security.py`, `test_store_migration.py` | Correctifs du 14 nov. 2026 : session révocable du backoffice, `Bearer` refusés comptés, en-têtes de sécurité et gabarits sans script ni style en ligne ; clé primaire `(user_id, id)` et migration d'une base à l'ancien schéma (SQLite, et Postgres avec `CHAMPIS_TEST_POSTGRES_URL`) |
| `api/tests/test_admin.py`, `test_jobs.py` | Backoffice `/admin` : fermé sans clé, session, Bearer, origine et essais limités, pages Santé, Usage et Tâches ; journal des tâches, puits de la console, inscription des commandes, compteurs 429 et 5xx |
| `api/tests/test_species.py` | Onglet Espèces : état par espèce depuis le fichier, le manifeste et l'inventaire du worker, tâches qui la concernent, inventaire du volume, champ espèces des outils, `CHAMPIS_DATA_DIR` |
| `api/tests/test_measure.py`, `test_worker.py`, `test_render.py`, `pipeline/tests/test_artifacts.py` | Appareils actifs par empreinte et parts d'espèces ; catalogue d'outils, service d'exécution (file, battement, annulation, sortes exclusives, garde du disque), passerelle `champis-api pipeline`, routes de lancement et sorties à committer ; carte HD rendue depuis des PMTiles synthétiques lus par plages ; `batch --clean-raw`, dépôt de `weather_grid.json` dans le bucket |

La CI exige une couverture d'au moins 90 % du pipeline et de l'API (`make check`, `make coverage` pour le détail). Ce qui n'est pas couvert et assumé : les appels réseau réels (Géoplateforme, ISRIC, AWS, Open-Meteo, APNs, CDS) ; le site a ses propres tests dans `adhumi/champis-web`.
