# Backoffice, lot BO6 : pages rendues par le serveur

Note à reporter dans la spec (« Spec — Champis » › Backoffice), tenue ici à la demande d'Adrien
(13 nov. 2026) : la refonte ajoute une dépendance et change les adresses du backoffice.

## Décision

Le backoffice n'est plus une page unique pilotée par JavaScript. Chaque écran est une page HTML
rendue par l'API (Jinja2) avec sa propre adresse ; la navigation se fait par des liens et des
formulaires (`POST`, puis redirection 303) ; tri, filtres et page vivent dans la query string.
htmx n'intervient qu'en amélioration progressive, par fragments ciblés, jamais en `hx-boost`
global : chaque interaction a son équivalent sans JavaScript. Pas d'étape de build.

Pourquoi : dans l'ancienne page, seul l'onglet était dans l'adresse. L'espèce ouverte, la tâche
suivie et les filtres se perdaient au rechargement, le bouton retour ne revenait pas d'un détail,
aucun lien ne se partageait, et lancer un outil ne menait pas à la tâche créée.

## Ce qui change

| Sujet | Avant | Après |
|---|---|---|
| Pages | `/admin` (une page, onglets en `#`) | `/admin/health`, `/usage`, `/species`, `/species/<id>`, `/species/<id>/file`, `/jobs`, `/jobs/<id>`, `/tools`, `/tools/<sorte>`, `/renders`, `/reports`, `/artifacts`, `/search` |
| Adresses | — | sous `/admin/` et non à la racine : `GET /species` est l'API publique, et le cookie est limité à `/admin` |
| Dépendances | aucune | `jinja2` côté serveur ; Tabler 1.7.5 (MIT) et htmx 2.0.11 (0BSD) vendorés dans `admin/static/vendor/`, aucune requête vers un tiers |
| Connexion | JSON et en-tête `X-Champis-Admin` | formulaire (303 vers la page demandée) ; la forme JSON reste pour les scripts |
| Actions | en-tête `X-Champis-Admin: 1` | pages : champ caché `csrf` (HMAC du cookie de session, rien de stocké) en plus du contrôle d'`Origin` ; API JSON : inchangé |
| Lancer un outil | carte-formulaire, puis chercher la tâche dans un autre onglet | formulaire pré-rempli par la query string, erreurs sous chaque champ, puis redirection vers la tâche |
| Confirmations | `confirm()` du navigateur | page de confirmation servie par le serveur ; une publication réelle demande toujours confirmation, avec ou sans `--prune` |
| Liens vers le bucket | URL signée d'une heure dans la page | `…/open?key=` redirige vers une URL signée fraîche |
| Journal des tâches | 100 dernières, filtre par sorte | recherche, sorte, état, origine, tri, pages de 50 |

## Ce qui ne change pas

- L'API JSON `/admin/api/*` (Bearer pour les scripts), ses réponses et ses tests.
- La clé unique, le cookie signé (HttpOnly, SameSite=Strict, `Path=/admin`, 30 jours), la limite
  de 5 essais par minute, le 404 sans `CHAMPIS_ADMIN_KEY`, `X-Robots-Tag: noindex`.
- Le catalogue des outils (`tools.py`) : rien d'autre ne se lance depuis le backoffice.
- « Une étape à la fois » sur la page d'une espèce : pas d'enchaînement automatique.
- Les fichiers espèces restent en lecture seule (ils se modifient par PR dans `adhumi/champis`).
- Les retours terrain restent privés : aucun écran, seuls les compteurs de la page Usage.

## Écarté

- Édition des fichiers espèces depuis le backoffice : hors périmètre (décision du 13 nov. 2026).
- Vue agrégée des observations : non, elle toucherait la vie privée.
- Pastille d'alertes de santé dans le menu : la page Santé interroge le bucket et le disque, trop
  cher à refaire sur chaque page. Le menu porte seulement le nombre de tâches en file ou en cours.
