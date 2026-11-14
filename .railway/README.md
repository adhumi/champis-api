# Railway — Infrastructure as Code

`railway.ts` décrit les cinq services du projet `champis` (API, cron `champis-warm`, cron
`champis-notify`, service d'exécution `champis-jobs`, et le site `champis-web`, déployé par son
propre dépôt), les deux volumes, le bucket et Postgres. Railway ne lit pas ce fichier au déploiement :
il s'applique depuis un poste avec la CLI (`npm install railway` à la racine pour que la CLI évalue
le fichier, puis `railway login`, `railway link`, `railway config plan`, `railway config apply`).
Le détail, la migration depuis `railway.toml` et les variables : `api/README.md` › Déploiement Railway.
