# Contribuer

Champis est un projet personnel, tenu par une seule personne sur son temps libre. Les pull requests sont bienvenues ; elles sont relues quand l'auteur le peut, parfois après plusieurs semaines. Une fonctionnalité qui contredit la spec du projet sera refusée, même bien écrite : mieux vaut en parler avant.

## Avant de contribuer

- Pour un changement de fond (nouvelle route, nouvelle étape du pipeline, changement du scoring, dépendance de plus), ouvrez d'abord une issue pour en discuter.
- Pour une correction de bug, une faute ou un test manquant, une pull request directe suffit.
- Pour une faille de sécurité, pas d'issue : voir [`SECURITY.md`](SECURITY.md).

## La spec

Les décisions structurantes du projet (le modèle, les sources de données, le découpage des dépôts, la vie privée) sont consignées dans un document de spécification tenu par l'auteur. Le code et les commentaires y renvoient sous la forme « spec › … ». Une copie de ce document est destinée à être versionnée dans le référentiel `adhumi/champis` : voir le référentiel. Une décision de ce niveau se discute dans une issue et s'inscrit dans la spec avant d'arriver dans le code.

## Ce qui ne se modifie pas ici

Les fichiers d'espèces, la table BD Forêt, la palette, les textes du Guide et les vecteurs de parité vivent dans le référentiel `adhumi/champis`, ici en sous-module `champis/`. Une espèce ou un texte se modifie par une pull request là-bas. Ce dépôt ne fait qu'avancer le sous-module (`make referential`, puis une pull request ici).

Si un changement du scoring modifie les vecteurs, `uv run champis-vectors` les régénère dans le sous-module ; ils se poussent par une pull request sur le référentiel, jamais à la main.

## Cloner sans clé SSH

Le sous-module est déclaré en SSH (`git@github.com:adhumi/champis.git`). Sans clé SSH chez GitHub, faites-le passer en HTTPS avant de cloner :

```bash
git config --global url."https://github.com/".insteadOf "git@github.com:"
git clone --recurse-submodules https://github.com/adhumi/champis-api
```

Ou, dans un clone déjà fait, sans toucher à la configuration globale :

```bash
git -c url."https://github.com/".insteadOf="git@github.com:" submodule update --init
```

## Règles du dépôt

- Une branche, une pull request, CI verte, puis fusion. Jamais de commit poussé directement sur `main`.
- `make check` lance exactement ce que lance la CI : Ruff (lint et format), les tests avec une couverture d'au moins 90 %, la vérification des vecteurs de parité et la validation des fichiers d'espèces. `make fmt` corrige le format.
- Aucune valeur d'espèce dans le code : tout vient de `champis/species/*.json`.
- Les tests tournent sans réseau, avec des fixtures et des données synthétiques (`pipeline/tests/test_pipeline_e2e.py` montre comment). Un test qui appelle un service réel sera refusé.
- La jointure avec la BD Forêt se fait sur `CODE_TFV` uniquement. La pluie est `rain_sum`, jamais `precipitation_sum`.
- Une commande nouvelle ou changée se reflète dans le `Makefile` (`make help` les liste). Une sous-commande affiche ses étapes par `champis_pipeline.console`, pas par `print`, et sans emoji.
- Rien de lourd dans la boucle d'événements de l'API : le calcul des scores et l'encodage d'une grande réponse passent par `offload.run`.
- Backoffice : aucun script ni style en ligne dans un gabarit, et toujours un équivalent sans JavaScript.
- Un changement de réponse de l'API qui sert le site se vérifie aussi dans `adhumi/champis-web` (CORS, en-têtes exposés).
- Le dépôt est écrit en français : commentaires, messages de commit, documentation.

Le détail de ces conventions, avec leurs raisons et leurs dates, est dans `CLAUDE.md`.

## Licence des contributions

Le dépôt est sous licence MIT (`LICENSE`). En proposant une contribution, vous acceptez qu'elle soit publiée sous la même licence.
