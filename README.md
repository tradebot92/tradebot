# Bot Trade

Bot de **veille** : il lit l'actualité de tes actions favorites, estime une fourchette de gain/perte, t'envoie des alertes et remplit un tableau de bord. Il ne passe **aucun ordre** : tu décides dans ton appli.

## Les fichiers
- `watchlist.txt` : tes actions favorites (un symbole par ligne).
- `config.json` : réglages (canal de notifications, seuil d'alerte).
- `bot.py` : le programme.
- `dashboard_modele.html` : le modèle du tableau de bord (ne pas ouvrir directement).
- `dashboard.html` : le tableau de bord généré (3 onglets : Favoris, Potentiel, Journal).
- `data/` : mémoire du bot (articles déjà vus, journal des signaux).

## Lancer
```bash
cd "/Users/matt/Downloads/bot trade"
.venv/bin/python bot.py
open dashboard.html
```

## Activer l'analyse par Claude
Sans clé, le bot affiche quand même cours, fourchettes et articles, mais pas l'analyse.
```bash
export ANTHROPIC_API_KEY="ta_clé"
```

## Activer les notifications sur ton téléphone
1. Installe l'appli **ntfy** (gratuite).
2. Choisis un nom de canal long et secret, par exemple `bot-trade-matt-48213`.
3. Abonne-toi à ce canal dans l'appli et écris le même nom dans `config.json` (`ntfy_topic`).

## Comment lire la fourchette
Elle vient de la volatilité du dernier mois et de l'objectif moyen des analystes. C'est un ordre de grandeur, jamais une promesse : le cours peut sortir de la fourchette.
