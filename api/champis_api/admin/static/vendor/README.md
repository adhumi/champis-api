# Fichiers vendorés du backoffice

Copiés tels quels depuis npm (pas d'étape de build, aucune requête vers un tiers) ; seule la ligne
`sourceMappingURL` est retirée.

| fichier | paquet | licence | texte |
|---|---|---|---|
| `tabler-1.7.5.min.css`, `tabler-1.7.5.min.js` | `@tabler/core` 1.7.5 (`dist/css/tabler.min.css`, `dist/js/tabler.min.js`) | MIT, © 2018-2026 The Tabler Authors | `LICENSE-tabler.txt` (le `LICENSE` du dépôt `tabler/tabler` au tag `@tabler/core@1.7.5` ; le paquet npm n'en embarque pas) |
| `htmx-2.0.11.min.js` | `htmx.org` 2.0.11 (`dist/htmx.min.js`) | 0BSD | `LICENSE-htmx.txt` (le `LICENSE` du paquet npm) |

`tabler-1.7.5.min.js` embarque le code qu'il importe : les composants JavaScript de Bootstrap 5
(MIT, © The Bootstrap Authors, https://github.com/twbs/bootstrap/blob/main/LICENSE) et Popper
(`@popperjs/core` ^2.11.8, MIT, © 2019 Federico Zivolo : `LICENSE-popper.txt`, le `LICENSE.md` du
paquet npm 2.11.8).

Les textes de licence ne sont pas servis (`STATIC_FILES` de `admin/pages.py` ne les liste pas) ;
ils accompagnent les fichiers dans le dépôt et dans l'image. Inventaire de tout ce qui vient de
tiers : `THIRD_PARTY.md` à la racine.

Mise à jour : `npm pack @tabler/core htmx.org`, copier les trois fichiers sous leur nouveau nom,
changer `VENDOR` dans `admin/pages.py` et reprendre les textes de licence de la nouvelle version.
