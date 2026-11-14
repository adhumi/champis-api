# Code et contenus de tiers

Le code de ce dépôt est sous licence MIT (`LICENSE`). Ce qui suit vient d'ailleurs, est copié ici
tel quel et garde sa propre licence. Les dépendances installées par `uv` (`uv.lock`) ne sont pas
redistribuées par le dépôt et ne figurent pas dans cette liste ; les données (IGN, Copernicus,
SoilGrids, Open-Meteo, GBIF) sont décrites dans `README.md` › Licences des données.

## Bibliothèques vendorées (backoffice)

Dans `api/champis_api/admin/static/vendor/`, servies par l'API sous `/admin/static/vendor/`, sans
étape de build ni requête vers un tiers. Provenance et mise à jour : le `README.md` de ce dossier.

| Fichiers | Origine | Licence | Texte |
| --- | --- | --- | --- |
| `tabler-1.7.5.min.css`, `tabler-1.7.5.min.js` | [Tabler](https://github.com/tabler/tabler), paquet npm `@tabler/core` 1.7.5 | MIT, © 2018-2026 The Tabler Authors | `api/champis_api/admin/static/vendor/LICENSE-tabler.txt` |
| embarqué dans `tabler-1.7.5.min.js` | [Popper](https://github.com/floating-ui/floating-ui) (`@popperjs/core` 2.11) | MIT, © 2019 Federico Zivolo | `api/champis_api/admin/static/vendor/LICENSE-popper.txt` |
| embarqué dans `tabler-1.7.5.min.js` | composants JavaScript de [Bootstrap](https://github.com/twbs/bootstrap) 5 | MIT, © The Bootstrap Authors | https://github.com/twbs/bootstrap/blob/main/LICENSE |
| `htmx-2.0.11.min.js` | [htmx](https://github.com/bigskysoftware/htmx), paquet npm `htmx.org` 2.0.11 | 0BSD (Zero-Clause BSD) | `api/champis_api/admin/static/vendor/LICENSE-htmx.txt` |

## Skill `modern-python`

`.claude/skills/modern-python/` (consignes d'outillage Python lues par l'assistant de code, sans
effet sur le service) est le skill « modern-python » de [Trail of Bits](https://github.com/trailofbits/skills),
fondé sur leur gabarit [cookiecutter-python](https://github.com/trailofbits/cookiecutter-python).
Il est distribué sous licence Creative Commons Attribution-ShareAlike 4.0 International
(CC BY-SA 4.0), dont le texte est conservé dans `.claude/skills/modern-python/LICENSE`. Ce dossier
reste sous cette licence, modifications éventuelles comprises (partage dans les mêmes
conditions) ; la licence MIT du dépôt ne s'y applique pas.

## Référentiel

Le sous-module `champis/` (dépôt `adhumi/champis`) porte ses propres licences : `champis/LICENSE`
et `champis/LICENSE-DATA.md`.
