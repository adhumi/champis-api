# Fichiers vendorés du backoffice

Copiés tels quels depuis npm (pas d'étape de build, aucune requête vers un tiers) ; seule la ligne
`sourceMappingURL` est retirée.

| fichier | paquet | licence |
|---|---|---|
| `tabler-1.7.5.min.css`, `tabler-1.7.5.min.js` | `@tabler/core` 1.7.5 (`dist/css/tabler.min.css`, `dist/js/tabler.min.js`) | MIT |
| `htmx-2.0.11.min.js` | `htmx.org` 2.0.11 (`dist/htmx.min.js`) | 0BSD |

Mise à jour : `npm pack @tabler/core htmx.org`, copier les trois fichiers sous leur nouveau nom et
changer `VENDOR` dans `admin/pages.py`.
