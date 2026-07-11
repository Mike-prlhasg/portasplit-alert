# Alerte Midea PortaSplit

Surveillance automatique de ManoMano, Optimea (neuf, déstockage, seconde vie et catégorie), Boulanger, Leroy Merlin, Castorama, Amazon France, Darty et Bricorama.

## Test manuel

Ouvre **Actions** → **Vérifier le stock PortaSplit** → **Run workflow**.

La première exécution initialise l’état. Une notification ntfy est envoyée lorsqu’une boutique passe ensuite vers **DISPONIBLE** ou **À VÉRIFIER**.

## Tableau web

Après la première exécution, active GitHub Pages dans **Settings → Pages**, avec :

- Source : **Deploy from a branch**
- Branch : **main**
- Folder : **/docs**

GitHub affichera ensuite l’adresse publique du tableau.

## Remarques

- GitHub accepte un cron de 5 minutes, mais une exécution planifiée peut être retardée.
- « À vérifier » signifie que le code postal, le magasin, le vendeur ou le prix doit être confirmé manuellement.
- Le script ne commande jamais automatiquement.
