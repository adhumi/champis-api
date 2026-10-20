---
name: validation-gbif
description: Contrôler la crédibilité du potentiel terrain T avec les occurrences GBIF (AUC contre fond forestier, quantiles pH, mois d'observation) via `champis-pipeline check`. À lire avant de juger un département, de recalibrer une plage pH ou de modifier pipeline/champis_pipeline/check/.
---

# Validation GBIF

GBIF ne fournit que des **positives** (quelqu'un a vu l'espèce là, ce jour-là). On ne mesure donc pas une précision mais une **discrimination** : `T` doit être nettement plus haut aux occurrences que sur des pixels forestiers tirés au hasard. Métrique : **AUC** (Mann-Whitney, ex æquo à 0,5), critère de phase 1 **AUC > 0,7** par espèce et département.

## Lancer

```bash
uv run champis-pipeline check --dept 25 [--species boletus_edulis] [--force]
```

Sortie : `reports/<espèce>-<dept>.html` (lisible) et `.json` (chiffres), jamais commités. Réseau : `api.gbif.org` ; les occurrences sont mises en cache dans `data/raw/gbif/<espèce>_<bbox>.json`, `--force` retélécharge.

## Ce que fait `check`

1. **taxonKey** par `species/match` sur `names.la` (règne Fungi) ; nom inconnu = erreur, pas de repli.
2. **Occurrences** `occurrence/search` : France, coordonnées présentes sans problème géospatial, emprise = bornes du raster T en WGS84, 300 par page jusqu'à `endOfRecords` (plafond 100 000 de l'API search : au-delà, passer à l'API download). **Précision triée côté client** : incertitude déclarée ≤ 100 m, ou incertitude non renseignée mais coordonnées à ≥ 4 décimales (≈ 10 m). Ne jamais remettre le paramètre `coordinateUncertaintyInMeters` dans la requête : il exclut aussi les enregistrements sans incertitude, c'est-à-dire la plupart des jeux naturalistes français (Doubs : 0 occurrence avec le filtre API). Écartés et comptés dans le rapport : sans date, incertitude > 100 m, coordonnées arrondies (centre de commune).
3. **T aux occurrences** (`sample_raster`) : hors emprise ou hors forêt BD Forêt → ignoré (compté dans « occurrences » mais pas dans « en forêt »).
4. **Fond** : `max(2 000, 10 × positives)` pixels forestiers (`T ≠ nodata`) tirés avec remise, graine fixe (`seed`).
5. **pH SoilGrids aux occurrences** : quantiles 1/10/90/99 % (≥ 20 points), c'est l'entrée du recalibrage de `terrain.ph` (skill `species-file`, SCHEMA.md « à faire en phase 1 »).
6. **Mois** des observations : à comparer à `phenology` ; un pic GBIF un mois avant `peak.start` est un signal, pas une preuve (les observateurs sortent quand il fait beau).
7. **Carte miniature** : T colorisé par `shared/palette.json`, occurrences en rouge, pour voir où le modèle se trompe.

## Lire un rapport

- **AUC ≥ 0,7 et T moyen aux occurrences ≥ 0,55** : crédible. Entre 0,6 et 0,7 : regarder la carte ; souvent une classe d'hôte sous-pondérée (les points rouges tombent dans une couleur orange) ou une plage d'altitude trop étroite.
- **Peu de points en forêt** (< 30) : la métrique est bruitée ; dire « non évalué », ne pas conclure.
- **Biais connus** : échantillonnage GBIF concentré près des routes et des villes, sur-déclaration des espèces faciles, coordonnées parfois arrondies au centre de commune (le filtre ≤ 100 m en élimine la plupart). Ne jamais ajuster une plage pour coller à un point isolé.

## Recalibrer le pH

Les plages `terrain.ph` actuelles sont la littérature élargie de +0,6 (SoilGrids surestime les humus forestiers). Avec les quantiles : `min ≈ q1`, `opt_low ≈ q10`, `opt_high ≈ q90`, `max ≈ q99`, arrondis à 0,1, **sans resserrer sous la littérature** et en gardant le plancher 0,15 du facteur pH. Procédure et spec : skill `species-file` (référence `gbif2026` à mettre à jour avec la date et le département).

## Modifier le code

`pipeline/champis_pipeline/check/gbif.py` (API, cache) et `report.py` (mesures, HTML). Tests sans réseau : `pipeline/tests/test_pipeline_e2e.py::test_gbif_check_report` (client HTTP factice, occurrences posées dans la hêtraie synthétique). Aucune valeur d'espèce dans ce code.
