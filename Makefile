# Champis — toutes les commandes du dépôt, documentées. `make` ou `make help` les liste.
#
# Variables (surcharge : `make dept DEPT=39`, `make batch DEPTS="25 39" JOBS=2`) :
#   DEPT     un département (download, grid, terrain, dept, summary, explain, gbif-check)
#   DEPTS    une liste ou `all` (batch, tiles, weather-grid, publish, release)
#   SPECIES  ids d'espèces séparés par des espaces ; vide = toutes
#   JOBS     processus en parallèle (batch : départements, ~2 Go chacun ; tiles : niveau 15)
#   FLAGS    options passées aux CLI, ex. `FLAGS=-v` (journaux bruts, traces) ou `FLAGS=-q`
#
# Chaque commande écrit son journal dans data/logs/<commande>-<date>.log (`make log` : le dernier).

.DEFAULT_GOAL := help
SHELL := /bin/bash
# .ONESHELL et .SHELLFLAGS demandent GNU Make ≥ 3.82 ; le make 3.81 de macOS (Xcode) les ignore et
# lance chaque ligne dans son propre shell : une recette ne doit donc pas faire passer une variable
# shell d'une ligne à l'autre (voir publish).
.ONESHELL:
# Sans ça, une recette .ONESHELL ne rend que le code de sa dernière ligne : un échec au milieu passe inaperçu.
.SHELLFLAGS := -eu -o pipefail -c

DEPT    ?= 25
DEPTS   ?= all
SPECIES ?=
JOBS    ?= 3
FLAGS   ?=

UV       := uv run
PIPELINE := $(UV) champis-pipeline $(FLAGS)
API      := $(UV) champis-api $(FLAGS)
SP       := $(foreach s,$(SPECIES),--species $(s))

# ------------------------------------------------------------------ Développement

.PHONY: help
help: ## Cette aide
	@awk 'BEGIN { FS = ":.*## " } \
	  /^# ---+ / { sub(/^# -+ /, ""); printf "\n\033[1m%s\033[0m\n", $$0 } \
	  /^[a-zA-Z_-]+:.*## / { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo
	@echo "Variables : DEPT=$(DEPT) DEPTS=$(DEPTS) SPECIES='$(SPECIES)' JOBS=$(JOBS) FLAGS='$(FLAGS)'"

.PHONY: setup
setup: ## Installe l'environnement (uv sync --group dev) et crée .env depuis .env.example s'il manque
	uv sync --group dev
	if [ ! -f .env ]; then cp .env.example .env && echo "  .env créé : complète le bucket et les clés"; fi

.PHONY: test
test: ## Tests Python (pipeline, api, web) : `make test ARGS="-k batch"` pour filtrer
	$(UV) pytest $(ARGS)

.PHONY: coverage
coverage: ## Tests avec le détail des lignes non couvertes, par fichier
	$(UV) pytest --cov=champis_pipeline --cov=champis_api --cov-report=term-missing

.PHONY: lint
lint: ## Ruff : lint + vérification du format, sans rien modifier
	$(UV) ruff check .
	$(UV) ruff format --check .

.PHONY: fmt
fmt: ## Ruff : corrige ce qui peut l'être et formate
	$(UV) ruff check --fix .
	$(UV) ruff format .

.PHONY: vectors
vectors: ## Régénère shared/vectors/ (contrat de parité Python/Swift) ; à relancer si le scoring change
	$(UV) champis-vectors

.PHONY: check
check: ## Exactement la CI : lint, format, tests avec couverture ≥ 90 %, vecteurs à jour, fichiers espèces valides
	$(MAKE) --no-print-directory lint
	$(UV) pytest --cov=champis_pipeline --cov=champis_api --cov-report=term --cov-fail-under=90
	$(UV) champis-vectors --check
	$(UV) python species/validate.py

.PHONY: serve
serve: ## API locale avec rechargement : http://localhost:8000 (/species, /weather, /tiles, /web/)
	$(UV) uvicorn champis_api.app:app --reload

.PHONY: log
log: ## Affiche le dernier journal de data/logs/
	f=$$(ls -t data/logs/*.log 2>/dev/null | head -1 || true)
	if [ -n "$$f" ]; then cat "$$f"; else echo "aucun journal dans data/logs/" >&2; fi

.PHONY: doctor
doctor: ## L'environnement est-il prêt ? .env, disque, grille météo, amorçage, bucket, base, APNs, CDS, vecteurs
	$(API) doctor

.PHONY: status
status: ## Où en est chaque département (raw, grid, T, batch, tuiles) et le jeu DEPTS (local, bucket)
	$(PIPELINE) status --depts $(DEPTS) $(SP)

# ------------------------------------------------------------------ Pipeline terrain (un département : DEPT)

.PHONY: download
download: ## BD Forêt, SoilGrids et DEM du département dans data/raw/ (rien n'est retéléchargé)
	$(PIPELINE) download --dept $(DEPT)

.PHONY: grid
grid: ## Rasters alignés 25 m (host, cover, ph, alt, slope, aspect) dans data/grid/$(DEPT)/
	$(PIPELINE) grid --dept $(DEPT)

.PHONY: terrain
terrain: ## Potentiel terrain T par espèce : data/T/<espèce>/$(DEPT)-<hash>.tif
	$(PIPELINE) terrain --dept $(DEPT) $(SP)

.PHONY: dept
dept: ## Tout un département : download, grid, terrain, tiles, weather-grid
	$(PIPELINE) all --dept $(DEPT) $(SP)

.PHONY: summary
summary: ## Surface par classe (excellent -> très faible) et par espèce, JSON sur stdout
	$(PIPELINE) summary --dept $(DEPT) $(SP)

.PHONY: explain
explain: ## Part de chaque facteur dans T : reports/explain-<espèce>-$(DEPT).json
	$(PIPELINE) explain --dept $(DEPT) $(SP)

.PHONY: gbif-check
gbif-check: ## Contrôle GBIF (AUC de T aux occurrences) : reports/<espèce>-$(DEPT).html ; réseau
	$(PIPELINE) check --dept $(DEPT) $(SP)

# ------------------------------------------------------------------ Pipeline terrain (plusieurs départements : DEPTS)

.PHONY: batch
batch: ## download + grid + terrain en parallèle (JOBS départements à la fois), reprise automatique
	$(PIPELINE) batch --depts $(DEPTS) --jobs $(JOBS) $(SP)

.PHONY: tiles
tiles: ## PMTiles T par espèce et A (altitude) du jeu DEPTS, plus un jeu par département (hors ligne)
	$(PIPELINE) tiles --depts $(DEPTS) --jobs $(JOBS) --per-dept $(SP)

.PHONY: weather-grid
weather-grid: ## Cellules météo 5 km avec poids Σ T : data/weather_grid.json (versionné, à commiter)
	$(PIPELINE) weather-grid --depts $(DEPTS) $(SP)

# ------------------------------------------------------------------ Publication (bucket Railway, variables de .env)

.PHONY: publish
publish: ## PMTiles de data/tiles -> bucket + manifeste, jeux par département compris ; --prune efface les autres jeux
	@# question et test sur une même ligne : le make 3.81 de macOS (Xcode) ignore .ONESHELL et lance
	@# chaque ligne dans son propre shell, où `r` serait vide
	@read -r -p "Publier le jeu [$(DEPTS)] et effacer les autres jeux du bucket (--prune) ? [o/N] " r; \
	  [[ "$$r" == [oOyY] ]] || { echo "annulé"; exit 1; }
	$(PIPELINE) publish --depts $(DEPTS) --per-dept --prune $(SP)

.PHONY: publish-dry
publish-dry: ## Montre ce que publish téléverserait et ce que --prune supprimerait, sans rien écrire
	$(PIPELINE) publish --depts $(DEPTS) --per-dept --prune --dry-run $(SP)

.PHONY: publish-keep
publish-keep: ## Comme publish, sans effacer les jeux déjà dans le bucket
	$(PIPELINE) publish --depts $(DEPTS) --per-dept $(SP)

.PHONY: release
release: ## Séquence complète : tiles (jeu DEPTS + par département), publish-dry, puis publish
	$(MAKE) --no-print-directory tiles
	$(MAKE) --no-print-directory publish-dry
	$(MAKE) --no-print-directory publish

# ------------------------------------------------------------------ Météo (API ; grille versionnée, amorçage dans le bucket)

.PHONY: soil-clim
soil-clim: ## Climatologie du sol ERA5-Land (compte CDS) -> data/weather_seed.db ; une fois par grille
	$(PIPELINE) soil-clim

.PHONY: seed
seed: ## Historique J-200 -> data/weather_seed.db ; France ≈ 227 000 appels pondérés : ≈ 23 j gratuit, quelques heures en Professional ; reprise possible
	$(API) seed

.PHONY: publish-seed
publish-seed: ## Dépose data/weather_seed.db dans le bucket ; l'API l'importe à son prochain démarrage
	$(API) publish-seed

.PHONY: warm
warm: ## Pré-chauffe du cache météo local puis indice par département (le cron Railway fait `warm --pull --publish`)
	$(API) warm

.PHONY: warm-cron
warm-cron: ## Exactement la passe du service cron Railway : reprend le cache du bucket, pré-chauffe, republie
	$(API) warm --pull --publish

.PHONY: publish-cache
publish-cache: ## Dépose un instantané du cache météo local dans le bucket (migration : l'API l'importe)
	$(API) publish-cache

.PHONY: notify
notify: ## Envoie une fois la notification « mes coins » (APNs configuré dans .env) ; le cron fait `notify --pull`
	$(API) notify
