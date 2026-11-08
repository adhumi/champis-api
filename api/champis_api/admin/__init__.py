"""Backoffice (spec › Backoffice, 7 nov. 2026, tranché le 8 nov.) : page privée `/admin` de
l'API, ouverte par une clé unique, pour voir la santé du service, son usage et le journal des
tâches. Lot BO1 : lecture seule ; les outils lancés depuis la page et la carte HD viennent avec le
service d'exécution (lot BO3). Rien ici n'est public : sans `CHAMPIS_ADMIN_KEY`, tout répond 404."""
