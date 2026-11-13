---
name: geo-pipeline
description: Conventions du pipeline SIG offline (pipeline/) qui produit le potentiel terrain T en PMTiles à partir de BD Forêt V2, BD ALTI, SoilGrids et Corine. À lire avant d'écrire ou modifier une étape du pipeline, un raster ou une jointure.
---

# Pipeline terrain

Python 3.12, `rasterio`, `geopandas`, `numpy`, `pyogrio`, `mercantile`, `pillow`, `pmtiles`. **Aucun outil GDAL en ligne de commande** : tout passe par rasterio (rasterize, reproject, merge) et numpy (pente/exposition par Horn). Ne pas réintroduire de `subprocess` GDAL. Outillage : `uv`, `ruff`, `pytest` (skill `modern-python`).

## Grille de référence

- **Calcul en Lambert-93, EPSG:2154**, résolution **25 m**, grille France alignée sur l'origine `(x0, y0) = (99 975, 6 049 975)` pour que tous les départements s'emboîtent sans rééchantillonnage. Toute étape commence par `align_to_grid(bbox)`.
- **Tuilage en Web Mercator, EPSG:3857**, une seule reprojection, à la fin, sur la mosaïque France.
- Rasters intermédiaires : GeoTIFF **COG**, `uint8`, `nodata = 255`, compression `DEFLATE`, `PREDICTOR=2`, tuilé 512. Les valeurs 0–254 encodent 0–1 (`v / 254`). Un flottant intermédiaire (pente, exposition) reste en `float32` avec `nodata = -9999`.

## Étapes (une fonction, un fichier de sortie, idempotent)

```
data/raw/<source>/<dept>/…          téléchargements, jamais modifiés, .md5 vérifié
data/grid/<dept>/host.tif            uint8, code hôte (index dans hosts.json)
data/grid/<dept>/cover.tif           uint8, code couvert
data/grid/<dept>/edge.tif            uint8, distance à la lisière en pixels de 25 m (plafond 40 = 1 km), 0 hors forêt
data/grid/<dept>/ph.tif              uint8, pH × 10 (255 = inconnu)
data/grid/<dept>/alt.tif             uint16, m
data/grid/<dept>/slope.tif           uint8, degrés
data/grid/<dept>/aspect.tif          uint8, 0–7 = N,NE,E,SE,S,SW,W,NW ; 8 = flat (pente < 3°)
data/T/<species>/<dept>.tif          uint8, T × 254
data/T/<species>/france.vrt          mosaïque
data/tiles/T-<species>-<hash>-….pmtiles  livrable par espèce
data/tiles/A-300m-d<hash>.pmtiles    classes d'altitude, commune
data/tiles/G-<groupe>-<hash>-….pmtiles   un fichier par groupe d'affichage (RGBA, un canal par espèce)
```

Chaque étape vérifie si sa sortie existe et est plus récente que ses entrées ; `--force` pour recalculer. Une étape ne lit jamais la sortie d'une étape postérieure.

## Sources et pièges

**BD Forêt V2** (IGN, shapefile par département, Lambert-93, licence ouverte) — flux `https://data.geopf.fr/telechargement/resource/BDFORET`, archive `BDFORET_2-0__SHP_LAMB93_D0XX_AAAA-MM-JJ.7z`, couche `FORMATION_VEGETALE`.
- **Jointure sur `CODE_TFV` uniquement**, jamais sur `TFV` (apostrophes droites ou typographiques selon l'édition). `FF1-00` ≠ `FF1-00-00` : comparaison exacte.
- 32 codes → `mapping/bdforet_to_hosts.json` ; un code absent de la table est une erreur bloquante, pas un `default`.
- Rasteriser avec `rasterio.features.rasterize` sur la grille alignée, `fill=255`, en priorité aux polygones les plus petits (trier par surface décroissante avant rasterisation pour que les petits gagnent). Codes TFV `strip()`és, shapefile sans `.prj` → Lambert-93 supposé avec avertissement.
- « Pur » = ≥ 75 % du couvert dominant ; le sous-étage n'est pas décrit. Ne pas essayer de le déduire.
- Hors polygone BD Forêt → repli Corine Land Cover 2018 niveau 3 (`clc_2018_fallback`) ; tout code hors table → `herbacee` (= 0).

**MNT** — aujourd'hui Copernicus DEM GLO-30 (bucket public AWS, dalles 1°, 404 en mer = ignorées). Attention : c'est un **DSM** (canopée incluse), ce qui crée des pentes fictives en lisière ; décision en attente de passer à BD ALTI / RGE ALTI (IGN, DTM) ou de lisser. Pente et exposition par Horn (numpy, float32) **sur la mosaïque** reprojetée, pas par dalle. Exposition : 0 = N, sens horaire, classes centrées (N = [337,5°, 22,5°[) ; pente < 3° → `flat`.

**SoilGrids 2.0** (ISRIC, pH H2O 0–5 cm, 250 m, valeur × 10, CC-BY 4.0) — WCS `maps.isric.org` en 4326 sur l'emprise ; le fichier téléchargé est ouvert avec rasterio avant mise en cache (un WCS renvoie des erreurs XML en HTTP 200). Reprojeter en 2154 en **bilinéaire** ; `null` fréquent en plaine agricole et en zone urbaine → 255. Ne jamais remplacer par une moyenne.

**Corine Land Cover 2018** — UMC 25 ha, shapefile métropole ; sert uniquement de repli.

**BD TOPO** (palier 3 du 12 nov. 2026, `download.bdtopo`, `rasterize.topo`, `rasterize.water`) — flux WFS de la Géoplateforme (`BDTOPO_V3:zone_de_vegetation` natures Peupleraie, Bois, Verger, Haie, Forêt ouverte, Lande ligneuse ; `troncon_hydrographique` non fictif ; `surface_hydrographique`), filtre CQL sur l'emprise Lambert-93 du département, pages de 5 000 objets en GeoJSON réduit (`nature,geometrie`), un fichier par couche dans `data/raw/<dept>/bdtopo/` (≈ 250 Mo par département). `host_topo.tif` + `cover_topo.tif` : les zones de végétation **là où host.tif est à nodata** (BD Forêt fait foi), codées dans le vocabulaire à part `topo_hosts` du mapping (section `bdtopo_v3` : nature → hôte, couvert) ; `water.tif` : distance à l'eau en pixels de 25 m, même érosion octogonale que la lisière, plafond `WATER_CAP_PX` = 40, 0 dans l'eau, jamais de nodata. `terrain.compute` ne lit `host_topo`/`cover_topo` que pour une espèce qui a `terrain.hosts_topo` (hôtes nommés, jamais de `default` : pixel non nommé = NaN → nodata) et `water.tif` que pour `terrain.water_m` ; les 27 espèces forestières gardent leurs rasters à l'octet près. `grid_ready` compte dix rasters.

**Lisière** (`rasterize.edge`, palier 2 du 9 nov. 2026) — dérivée de `host.tif` et `cover.tif`, sans nouvelle source : forêt = polygone BD Forêt dont l'hôte n'est pas `herbacee` et le couvert ni `sans_couvert` ni `non_foret` (une coupe rase fait une lisière) ; distance au pixel non forestier le plus proche par érosions successives du masque, voisinage de 4 puis de 8 en alternance (distance octogonale, à moins de 8 % de l'euclidienne, sans scipy), plafonnée à `EDGE_CAP_PX` = 40 px (1 km) ; le bord du raster est traité comme de la forêt (la limite d'un département ne fait pas de fausse lisière). Le raster se calcule sur le département entier en mémoire (quelques dizaines de Mo). Lu par `terrain.compute` et `explain` **seulement si l'espèce a le bloc optionnel `terrain.edge_m`** (`scoring.terrain_factors`, facteur `edge`, trapèze sur les mètres, poids `weights.edge`) ; une espèce sans le bloc ne change pas. Avant d'ajouter le bloc à une espèce : AUC GBIF sur un département de référence avec et sans (`check --edge min,opt_low,opt_high,max --edge-weight w`, aussi depuis le backoffice : T variant en mémoire dans `data/T-variants/`, comparaison et verdict du critère d'entrée dans le rapport, skill `validation-gbif`), et l'empreinte de l'espèce change (tuiles T à rerendre).

## Calcul de `T`

`pipeline/champis_pipeline/scoring.py` : `trapezoid()` et `terrain_potential(species, host, cover, ph, alt, slope, aspect, edge_px=None) -> float32` vectorisés numpy sur des blocs de 2048 × 2048 (jamais le département en mémoire d'un coup). Voir skill `champis-model` pour les règles. Les tables de correspondance (poids par hôte) sont converties une fois en tableaux numpy indexés par code, pas de dictionnaire dans la boucle.

## Tuilage

Mosaïque Lambert-93 (`rasterio.merge` sur disque), puis pour chaque tuile XYZ du niveau **15** : lecture de la fenêtre source (bords arrondis vers l'extérieur + 1 px de marge, `tiles.WINDOW_MARGIN` : un arrondi au plus proche perd un pixel 25 m au bord et laisse une bande nodata à chaque couture, test `test_interior_tiles_have_full_edges`) + `reproject` nearest en 256×256 ; niveaux **14 à 6** par réduction 2×2 ; PNG **LA** (luminance = `T × 254`, alpha 0 pour nodata), écrit en MBTiles (schéma TMS, ligne inversée) puis `pmtiles.convert.mbtiles_to_pmtiles` (qui remet en XYZ : pas de double flip, vérifié par test). Commits par lots de 2 000 tuiles. Nom `T-<species>-<hash espèce>-p<version>-d<hash des départements>.pmtiles`, immuable : `d<…>` = sha256 (6 hex) des départements triés **et** de `tiles.RENDER_VERSION` (`_depts_hash`), que chaque PMTiles porte aussi en métadonnée `champis_render`. Un correctif du rendu incrémente `RENDER_VERSION` : nouveau nom donc nouveau jeu (`_done` ne saute rien, `publish --prune` écarte l'ancien, les caches du relais, des navigateurs et de l'app, tous par nom, ne servent jamais d'anciens octets) ; `status` liste les anciens jeux encore sur disque. Le défaut en « L » aux coins des tuiles (nov. 2026) venait d'un jeu rendu avant le correctif de la fenêtre source et jamais refait sous son nom inchangé. Une seconde couche `A-<largeur>m-d<hash des départements>.pmtiles` (classes d'altitude, `build_alt`) suit la même chaîne depuis `alt.tif` : gris = indice k de la classe `[300k, 300k+300[` m (centre `300k+150`, celui de `weather_grid.alt_classes` et de l'API), alpha 0 = altitude inconnue, emprise entière ; la largeur vient de `shared/palette.json` (`alt_encoding.class_m`), jamais en dur. Une troisième couche `E-<pas>m-d<hash des départements>.pmtiles` (altitude fine, `build_elev`, 13 nov. 2026) sort du même `alt.tif` par la même chaîne, avec le pas de `elev_encoding.step_m` (20 m, 254 tranches = 5 080 m) : les clients y lisent l'altitude du pixel pour interpoler `M_eff × Φ` entre deux classes de 300 m ; `A` reste publiée pour les versions installées, `publish` attend les deux. Ordres de grandeur : Doubs ≈ 35 k tuiles, quelques minutes/espèce ; France ≈ 2,3 M tuiles, de l'ordre de 2–4 h/espèce avec 4 processus (à mesurer).

**Un PMTiles par groupe d'affichage** (`tiles --groups`, `build_group`, décision du 4 nov. 2026, spec › Points ouverts › PMTiles par groupe) : `G-<groupe>-<hash>-p<version>-d<hash des départements>.pmtiles`, `<groupe>` = `group.id` des fichiers d'espèces, `<hash>` = sha256 (8 hex) des `species_hash` concaténés dans l'ordre des canaux (`species.group_hash`). Même grille et mêmes niveaux 6–15 ; PNG **RGBA** 256 px 8 bits non entrelacé, canal i = T × 254 de la i-ème espèce du groupe par ordre alphabétique d'identifiant (`species.group_sets`), 255 = nodata pour ce canal (pixel hors forêt, ou tuile absente du jeu de l'espèce) ; **l'alpha est un canal de données**, jamais une transparence. Au plus quatre espèces par fichier (`GROUP_CHANNELS`) ; au-delà, un second fichier suffixé `-2` avant `.pmtiles` (pas nécessaire : cèpes = 4, girolles = 3 avec le 4e canal tout à 255). Construit **depuis les MBTiles de travail des jeux T** (`full_set_source`, reconstruit depuis le PMTiles si besoin) : union des (z, x, y) des espèces du groupe (sqlite, `UNION` sur les jeux attachés en lecture seule), empilement des canaux par lots de `GROUP_BATCH` tuiles dans `--jobs` processus `spawn` (au plus 2 × jobs lots en vol), aucune reprojection ni réduction (les T sont déjà réduits à tous les niveaux) ; MBTiles → `_write_pmtiles` atomique, `_done` pour l'idempotence. Un G porte toujours son groupe entier : avec `--species` partiel, pas de G pour ce groupe (avertissement). Jeu par département : `build_group_dept` = `extract` du MBTiles de travail du G complet. Clients : `shared/palette.json` › `group_tile_encoding` (`channels: "rgba"`, `scale: 254`, `nodata: 255`) et `species` du manifeste pour l'ordre des canaux, rien en dur. Coût : PIL décode 4 LA + encode 1 RGBA ≈ 3–5 ms par tuile, France ≈ 1 M de tuiles par groupe → de l'ordre de 1 h par groupe avec 4 processus (à mesurer).

## Diagnostic (`champis_pipeline/explain.py`)

`champis-pipeline explain --dept 25 --species <id>` → `reports/explain-<id>-<dept>.json` : par hôte (ha, poids, T moyen, part excellent), distribution de chaque facteur (`scoring.terrain_factors`), pH et altitude bruts, « et si » (part excellent si un facteur valait 1, si l'hôte était plafonné à 0,9/0,8/0,7). À lancer avant de toucher une valeur d'espèce quand une carte sature (Doubs : 90 % excellent pour B. edulis) ; la décision reste dans la spec et le skill species-file.

## Contrôle (`champis_pipeline/check/`)

- `champis_pipeline/check/` (`champis-pipeline check --dept 25`) produit `reports/<species>-<dept>.html` + `.json` : **AUC** GBIF positives vs pixels forestiers aléatoires, histogrammes de `T`, mois d'observation, quantiles pH aux occurrences, surface par classe, carte miniature (skill `validation-gbif`). Critère de phase 1 : AUC > 0,7.
- (à écrire) Un jeu de **pixels de contrôle** `pipeline/tests/control_pixels.json` (hêtraie du Ventoux, cédraie, vigne de Carpentras, chênaie du Luberon…) avec le `T` attendu par espèce à ± 0,1 ; `pytest` les rejoue sur le Vaucluse.

## Performance

Ordres de grandeur : France 25 m ≈ 1,3 Gpixels par espèce, GeoTIFF ~1–2 Go, PMTiles ~2–4 Go. Parallélisme par département : `champis-pipeline batch --depts all --jobs N` (`batch.py`, `ProcessPoolExecutor`, 1 process par département, mémoire ~2 Go chacun, `data/batch_status.json` pour reprendre (un département « ok » dont un T manque sous le hash courant, fichier espèce modifié, est repris : si sa grille est complète et que `grid/<dept>/inputs.json` — édition BD Forêt, empreinte de la table TFV, écrit par `rasterize.write_grid_stamp` à la fin de la grille — correspond aux entrées courantes, seul le terrain des espèces manquantes est recalculé, sans retélécharger BD Forêt ni rasteriser, même après `--clean-raw` ; sinon la chaîne complète), un échec n'arrête pas le lot ; `CHAMPIS_QUIET` coupe les barres tqdm dans les processus ; les téléchargements partagés comme les dalles DEM utilisent un `.part` par pid). Tuilage : mosaïque écrite sur disque (`merge(dst_path=…, mem_limit=…)`, jamais en RAM), niveau 15 rendu par carrés de 64 × 64 tuiles dans `--jobs` processus (`spawn`, un carré vide est écarté par une seule lecture de sa fenêtre ; la barre avance par carré, ~450 pour la France), niveaux 14 → 6 par réduction 2×2 des tuiles déjà en base (`reduce_t` moyenne des valides, `reduce_nearest` pour A) sans relire la mosaïque. `_work/` n'est pas nettoyé automatiquement. Ne pas lancer la France sur un portable sans vérifier l'espace disque (~60 Go de travail).

Climatologie du sol (`soil_clim.py`, `champis-pipeline soil-clim`) : ERA5-Land journalier depuis le Climate Data Store (compte CDS, `cdsapi`), NetCDF par année dans `data/era5land/` (repris s'ils existent), quantiles 0–1 par mois et par pixel 0,1° (`xarray`, sans dask), échantillonnage au pixel valide le plus proche pour chaque cellule de `data/weather_grid.json`, écriture dans `data/weather_seed.db` (schéma `weather_store.py`, partagé avec le cache de l'API). Tests : NetCDF synthétique écrit par xarray, faux client CDS (`pipeline/tests/test_soil_clim.py`).
