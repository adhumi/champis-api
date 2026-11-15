# Le pipeline terrain

Détail des étapes de `champis-pipeline`, de leurs sorties et de leur affichage. Ce texte vient du `README.md` d'avant sa refonte (15 nov. 2026) ; les paragraphes gardent leur date, et une décision plus récente l'emporte sur une plus ancienne. Pour installer et lancer : [`README.md`](../README.md). Conventions d'écriture d'une étape : skill `geo-pipeline` (`.claude/skills/`).

Un département demande environ 5 Go de disque et des téléchargements depuis la Géoplateforme de l'IGN, l'ISRIC et le bucket public de Copernicus, sans compte ni clé.

```bash
make dept DEPT=25                  # = uv run champis-pipeline all --dept 25
```

## Étapes

`make help` liste toutes les commandes (développement, pipeline, publication, météo) avec leur documentation ; les commandes `uv run …` ci-dessous restent valables telles quelles. Avant de publier : `make doctor` (l'environnement est-il prêt : grille météo, bucket, base, clé APNs…), `make status` (où en est chaque département sur disque et dans le bucket), `make publish-dry` (ce que `publish --prune` téléverserait et effacerait, sans rien écrire).

`all` enchaîne :

1. **download** — BD Forêt V2 du Doubs (31 Mo, flux Géoplateforme IGN), pH SoilGrids (WCS ISRIC, découpe sur l'emprise), dalles Copernicus DEM 30 m (bucket public AWS). Tout va dans `data/raw/`, rien n'est retéléchargé s'il est présent.
2. **grid** — rasters alignés 25 m Lambert-93 dans `data/grid/25/` : `host.tif` (essence dominante), `cover.tif`, `ph.tif`, `alt.tif`, `slope.tif`, `aspect.tif`.
   Depuis le 12 nov. 2026 (palier 3), `download` et `grid` lisent aussi **BD TOPO V3** par le WFS de la Géoplateforme (zones de végétation hors forêt fermée, tronçons et surfaces hydrographiques, ≈ 250 Mo par département) : `grid/<dept>/host_topo.tif`, `cover_topo.tif` (hôtes hors forêt là où BD Forêt ne dit rien) et `water.tif` (distance à l'eau, plafond 1 km). Une espèce ne les emploie que si son fichier a `terrain.hosts_topo` ou `terrain.water_m` (morilles) ; les espèces forestières gardent leurs rasters.
3. **terrain** — `data/T/<espèce>/25-<hash>.tif`, un par espèce. Quelques secondes par espèce.
4. **tiles** — `data/tiles/T-<espèce>-<hash>-p1.0.0-d<hash>.pmtiles`, niveaux 6–15 (`d<hash>` = empreinte des départements *et* de la version du rendu `tiles.RENDER_VERSION` : un correctif du rendu donne un nouveau jeu sous un nouveau nom, l'ancien part avec `publish --prune` et aucun cache, relais, navigateur ou app, ne peut servir d'anciens octets ; décision du 5 nov. 2026), plus la couche `A-300m-d<hash>.pmtiles` (classe d'altitude du pixel, commune à toutes les espèces, lue par la carte pour appliquer gel et phénologie par pixel). Depuis le 13 nov. 2026 s'y ajoute la couche `E` (`E-<pas>m-d<hash>.pmtiles`, `tiles.build_elev` : altitude du pixel par pas de 20 m, pour interpoler la météo entre deux classes d'altitude), publiée à côté de `A`, que les versions installées de l'app lisent encore. Quelques minutes par espèce pour un département (z15 ≈ 7 000 tuiles). Avec `--per-dept`, un jeu par département (hash du département seul) est extrait du MBTiles de travail du jeu complet sans rien re-rendre : c'est ce que l'app télécharge pour le hors ligne (décision du 31 oct. 2026) ; `publish --per-dept` les téléverse et le manifeste marque ces entrées d'un champ `dept`. Avec `--groups`, un PMTiles par groupe d'affichage `G-<groupe>-<hash>-p1.0.0-d<hash>.pmtiles` (onze groupes depuis le 8 nov. 2026 : `bolets_bais`, `cepes`, `chanterelles`, `girolles`, `lactaires`, `nonnettes`, `petits_gris`, `pieds_bleus`, `pieds_de_mouton`, `russules`, `trompettes` ; `<hash>` = empreinte des hashes d'espèces dans l'ordre des canaux) est empilé depuis les MBTiles de travail des T, sans rien re-rendre : PNG RGBA, canal i = T × 254 de la i-ème espèce du groupe par ordre alphabétique, 255 = sans donnée pour ce canal, l'alpha est une donnée (pas une transparence) ; au plus quatre espèces par fichier, les canaux inutilisés tout à 255, et une seconde partie `-2` au-delà de quatre espèces (les lactaires, cinq espèces) (décision du 4 nov. 2026, spec › Points ouverts › PMTiles par groupe : deux requêtes par tuile côté clients au lieu de cinq ; `champis/shared/palette.json` › `group_tile_encoding`). `publish --groups` les téléverse ; leurs entrées du manifeste portent `kind: "G"`, `group` et `species` (ordre des canaux). `--jobs` vaut pour le niveau 15 des T comme pour l'empilement des G.
5. **weather-grid** — `data/weather_grid.json` (version 2) : les cellules météo 5 km où T > 0, avec leurs classes d'altitude et, par département touché, classe d'altitude et espèce, la somme des T des pixels (poids de l'indice par département) ; c'est l'entrée de l'API.

Plusieurs départements d'un coup : `uv run champis-pipeline batch --depts all --jobs 3` enchaîne download, grid et terrain en parallèle (un processus par département, ~2 Go chacun), garde l'état dans `data/batch_status.json` et reprend là où il s'est arrêté ; `tiles --depts …` et `weather-grid --depts …` prennent ensuite la liste. `batch --clean-raw` efface `data/raw/<dept>` dès que les T d'un département sont écrits (volume borné du service d'exécution) ; `weather-grid --to-bucket` dépose en plus le fichier dans le bucket sous `artifacts/` pour le committer depuis un poste. Depuis le backoffice (`api/README.md` › Backoffice, lot BO4), ces étapes tournent sur le service `champis-jobs` par `champis-api pipeline <étape>`, inscrites dans le journal des tâches.

Puis `uv run champis-pipeline summary --dept 25` donne la surface par classe (excellent → très faible) par espèce : premier regard sur la crédibilité avant toute carte.

Diagnostic de T : `uv run champis-pipeline explain --dept 25 --species boletus_edulis` écrit `reports/explain-boletus_edulis-25.json` (surface et T moyen par hôte, distribution de chaque facteur, pH et altitude bruts, part « excellent » si un facteur valait 1 ou si l'hôte était plafonné) : c'est l'entrée de toute révision des valeurs d'espèce (skill `species-file`).

Contrôle GBIF (critère de phase 1, AUC > 0,7) : `uv run champis-pipeline check --dept 25` télécharge les occurrences GBIF du département (cache dans `data/raw/gbif/`) et écrit `reports/<espèce>-25.html` : AUC de T aux occurrences contre fond forestier, mois d'observation, quantiles pH pour recalibrer les espèces (skill `validation-gbif`). Depuis le 12 nov. 2026, aussi depuis le backoffice (outil `check`, fiche de l'espèce), **sur plusieurs départements réunis** (`--depts 12 65 …` ou `all` : occurrences dédoublonnées, chacune sur le T de son département, fond au prorata des forêts, un seul rapport) et avec une **variante lisière** (`--edge min,opt_low,opt_high,max --edge-weight w`) : un second T calculé en mémoire avec le bloc candidat, dans `data/T-variants/`, évalué sur les mêmes occurrences, comparé à la référence avec le verdict du critère d'entrée du facteur ; `--to-bucket` dépose les rapports sous `reports/`, listés dans la fiche. Le fichier espèce n'est jamais écrit.

Pour regarder le résultat : la carte du dépôt `adhumi/champis-web` en mode développement (`carte.html?dev=1`), où l'on glisse les PMTiles (voir son `docs/web.md` › Mode développement). Sans serveur, [pmtiles.io](https://pmtiles.io) les affiche en gris brut : 0 = noir = T 0, 254 = blanc = T 1.

## Affichage et journaux

Chaque commande affiche ses étapes `[i/n]` avec un statut (`ok`, `deja fait`, `ECHEC`) et sa durée, des barres de progression pour les tâches longues, et un résumé final. Le statut va sur stderr, les données (`summary`) sur stdout. Couleurs sur un terminal seulement (`NO_COLOR` les coupe). Tout est aussi écrit, sans couleur, dans `data/logs/<commande>-<date>.log` (50 derniers gardés ; `make log` affiche le dernier ; `CHAMPIS_LOG_DIR` change le dossier, vide pour ne rien écrire). `-v` rétablit les journaux bruts et les traces complètes ; `-q` ne garde que les avertissements et le résultat. Les secrets connus (clé Open-Meteo `apikey=`, signature des adresses présignées du bucket, jeton et JWT d'APNs) sont masqués dans tout ce que la console écrit, à l'écran, dans le fichier et dans le journal des tâches, `-v` compris (`champis_pipeline.redact`, 13 nov. 2026).

Exemple de sortie (relevé du 31 oct. 2026, quand le référentiel comptait sept espèces) :

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

## Ce qui n'est pas encore là

- Le recalibrage pH des espèces à partir des quantiles du rapport `check`.
