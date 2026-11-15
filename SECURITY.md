# Signaler une faille de sécurité

Champis est un projet personnel, tenu par une seule personne sur son temps libre. Un signalement est toujours le bienvenu.

## Comment signaler

Par courriel à **hello@champis.app**. Pas d'issue publique ni de pull request pour une faille : elles la rendraient visible avant sa correction.

À joindre, autant que possible :

- ce qui est touché : une route de l'API, une commande, un fichier du dépôt, avec le commit ou la date de l'essai ;
- les étapes pour reproduire, ou une requête d'exemple (`curl`) ;
- ce qu'un attaquant obtiendrait : lecture ou modification de données, déni de service, exécution de code ;
- le nom à citer dans le correctif, si vous souhaitez être crédité.

## À quoi s'attendre

- Un accusé de réception sous quelques jours.
- Un correctif dès que possible, selon la gravité et le temps disponible. Aucun délai n'est garanti.
- Pas de prime.
- Merci d'attendre le correctif avant d'en parler publiquement.

## Périmètre

- Le code de ce dépôt : l'API (`api/`), le pipeline terrain (`pipeline/`), le `Dockerfile` et la CI.
- Le service qu'il fait tourner : `https://api.champis.app`, backoffice `/admin` compris.

Le site `champis.app` relève de `adhumi/champis-web`, l'app de `adhumi/champis-ios`, les fichiers d'espèces et les textes de `adhumi/champis`. La même adresse reçoit les signalements pour les quatre dépôts. Une faille dans une dépendance se signale d'abord à son auteur ; écrivez ici si Champis l'expose d'une façon particulière.

Seule la branche `main`, celle qui est déployée, reçoit des correctifs.

## Ce qui n'est pas une faille

Ces points sont des choix du modèle, décrits dans `api/README.md` :

- **L'identifiant d'appareil vaut mot de passe.** Il n'y a pas de compte : l'app tire un identifiant au hasard et l'envoie dans l'en-tête `X-Champis-User`. Qui connaît cet identifiant lit et efface les observations et les coins qui lui sont liés. Deviner l'identifiant d'un autre appareil, ou lire des données sans le connaître, serait en revanche une faille.
- **Les routes de lecture sont ouvertes** (`/species`, `/weather`, `/tiles`, `/departements`), sans clé, avec un CORS ouvert. Elles ne servent que des données publiques.
- **La limitation de débit est comptée par worker.** Un client peut obtenir quelques fois la limite affichée.
- **Pas de documentation interactive** : `/docs`, `/redoc` et `/openapi.json` répondent 404, à dessein.
- Le backoffice `/admin` répond 404 tant qu'aucune clé n'est configurée.

## Ce qu'il ne faut pas faire

- Ne testez pas la production en charge : pas d'outil de balayage à haut débit, pas d'essai de déni de service. Le service tourne sur une petite infrastructure financée par des dons. Une API locale suffit le plus souvent (`README.md` › Installer et lancer en local).
- Ne touchez pas aux données d'autrui : n'utilisez que des identifiants que vous avez créés vous-même. Si vous tombez sur des données qui ne sont pas les vôtres, arrêtez-vous et écrivez.
- N'essayez pas de forcer la clé du backoffice.
