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
data/grid/<dept>/ph.tif              uint8, pH × 10 (255 = inconnu)
data/grid/<dept>/alt.tif             uint16, m
data/grid/<dept>/slope.tif           uint8, degrés
data/grid/<dept>/aspect.tif          uint8, 0–7 = N,NE,E,SE,S,SW,W,NW ; 8 = flat (pente < 3°)
data/T/<species>/<dept>.tif          uint8, T × 254
data/T/<species>/france.vrt          mosaïque
data/tiles/T-<species>-<hash>-….pmtiles  livrable par espèce
data/tiles/A-300m-d<hash>.pmtiles    classes d'altitude, commune
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

## Calcul de `T`

`pipeline/champis_pipeline/scoring.py` : `trapezoid()` et `terrain_potential(species, host, cover, ph, alt, slope, aspect) -> float32` vectorisés numpy sur des blocs de 2048 × 2048 (jamais le département en mémoire d'un coup). Voir skill `champis-model` pour les règles. Les tables de correspondance (poids par hôte) sont converties une fois en tableaux numpy indexés par code, pas de dictionnaire dans la boucle.

## Tuilage

Mosaïque Lambert-93 (`rasterio.merge` sur disque), puis pour chaque tuile XYZ du niveau **15** : lecture de la fenêtre source (bords arrondis vers l'extérieur + 1 px de marge, `tiles.WINDOW_MARGIN` : un arrondi au plus proche perd un pixel 25 m au bord et laisse une bande nodata à chaque couture, test `test_tiles_have_no_seams`) + `reproject` nearest en 256×256 ; niveaux **14 à 6** par réduction 2×2 ; PNG **LA** (luminance = `T × 254`, alpha 0 pour nodata), écrit en MBTiles (schéma TMS, ligne inversée) puis `pmtiles.convert.mbtiles_to_pmtiles` (qui remet en XYZ : pas de double flip, vérifié par test). Commits par lots de 2 000 tuiles. Nom `T-<species>-<hash espèce>-p<version>-d<hash des départements>.pmtiles`, immuable. Une seconde couche `A-<largeur>m-d<hash des départements>.pmtiles` (classes d'altitude, `build_alt`) suit la même chaîne depuis `alt.tif` : gris = indice k de la classe `[300k, 300k+300[` m (centre `300k+150`, celui de `weather_grid.alt_classes` et de l'API), alpha 0 = altitude inconnue, emprise entière ; la largeur vient de `shared/palette.json` (`alt_encoding.class_m`), jamais en dur. Ordres de grandeur : Doubs ≈ 35 k tuiles, quelques minutes/espèce ; France ≈ 2,3 M tuiles, de l'ordre de 2–4 h/espèce avec 4 processus (à mesurer).

## Diagnostic (`champis_pipeline/explain.py`)

`champis-pipeline explain --dept 25 --species <id>` → `reports/explain-<id>-<dept>.json` : par hôte (ha, poids, T moyen, part excellent), distribution de chaque facteur (`scoring.terrain_factors`), pH et altitude bruts, « et si » (part excellent si un facteur valait 1, si l'hôte était plafonné à 0,9/0,8/0,7). À lancer avant de toucher une valeur d'espèce quand une carte sature (Doubs : 90 % excellent pour B. edulis) ; la décision reste dans la spec et le skill species-file.

## Contrôle (`champis_pipeline/check/`)

- `champis_pipeline/check/` (`champis-pipeline check --dept 25`) produit `reports/<species>-<dept>.html` + `.json` : **AUC** GBIF positives vs pixels forestiers aléatoires, histogrammes de `T`, mois d'observation, quantiles pH aux occurrences, surface par classe, carte miniature (skill `validation-gbif`). Critère de phase 1 : AUC > 0,7.
- (à écrire) Un jeu de **pixels de contrôle** `pipeline/tests/control_pixels.json` (hêtraie du Ventoux, cédraie, vigne de Carpentras, chênaie du Luberon…) avec le `T` attendu par espèce à ± 0,1 ; `pytest` les rejoue sur le Vaucluse.

## Performance

Ordres de grandeur : France 25 m ≈ 1,3 Gpixels par espèce, GeoTIFF ~1–2 Go, PMTiles ~2–4 Go. Parallélisme par département : `champis-pipeline batch --depts all --jobs N` (`batch.py`, `ProcessPoolExecutor`, 1 process par département, mémoire ~2 Go chacun, `data/batch_status.json` pour reprendre, un échec n'arrête pas le lot ; `CHAMPIS_QUIET` coupe les barres tqdm dans les processus ; les téléchargements partagés comme les dalles DEM utilisent un `.part` par pid). Tuilage : mosaïque écrite sur disque (`merge(dst_path=…, mem_limit=…)`, jamais en RAM), niveau 15 rendu par carrés de 64 × 64 tuiles dans `--jobs` processus (`spawn`, un carré vide est écarté par une seule lecture de sa fenêtre ; la barre avance par carré, ~450 pour la France), niveaux 14 → 6 par réduction 2×2 des tuiles déjà en base (`reduce_t` moyenne des valides, `reduce_nearest` pour A) sans relire la mosaïque. `_work/` n'est pas nettoyé automatiquement. Ne pas lancer la France sur un portable sans vérifier l'espace disque (~60 Go de travail).

Climatologie du sol (`soil_clim.py`, `champis-pipeline soil-clim`) : ERA5-Land journalier depuis le Climate Data Store (compte CDS, `cdsapi`), NetCDF par année dans `data/era5land/` (repris s'ils existent), quantiles 0–1 par mois et par pixel 0,1° (`xarray`, sans dask), échantillonnage au pixel valide le plus proche pour chaque cellule de `data/weather_grid.json`, écriture dans `data/weather_seed.db` (schéma `weather_store.py`, partagé avec le cache de l'API). Tests : NetCDF synthétique écrit par xarray, faux client CDS (`pipeline/tests/test_soil_clim.py`).
