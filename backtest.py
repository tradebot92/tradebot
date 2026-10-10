"""Test gratuit : les fourchettes du bot auraient-elles été justes dans le passé ?

Principe : on remonte dans le temps. À chaque date, on calcule la volatilité comme le bot
le fait (sur les données connues À CETTE DATE), puis on regarde si le cours un mois plus
tard est resté dans la fourchette [-vol ; +vol].
Si la fourchette est bien calibrée, environ 68 % des cas doivent rester dedans.
(La partie "objectif des analystes" ne peut pas être testée : l'historique n'est pas gratuit.)
"""
import math
import sys

import yfinance as yf

from bot import lire_favoris, lire_json, DOSSIER, decouvrir

JOURS_MOIS = 21


def tester(symbole):
    cours = yf.Ticker(symbole).history(period="3y")["Close"].dropna()
    if len(cours) < 300:
        return []
    rend = cours.pct_change()
    lignes = []
    for i in range(252, len(cours) - JOURS_MOIS, JOURS_MOIS):
        vol = float(rend.iloc[i - 252:i].std()) * math.sqrt(JOURS_MOIS)
        futur = float(cours.iloc[i + JOURS_MOIS] / cours.iloc[i] - 1)
        lignes.append((vol, futur))
    return lignes


def main():
    config = lire_json(DOSSIER / "config.json", {})
    favoris = lire_favoris()
    symboles = favoris + decouvrir(config, favoris)
    if len(sys.argv) > 1:
        symboles = sys.argv[1:]
    toutes = []
    for s in symboles:
        try:
            r = tester(s)
        except Exception as e:
            print(f"{s}: ignoré ({e})")
            continue
        if r:
            dans = sum(1 for v, f in r if -v <= f <= v)
            print(f"{s:10} {len(r):3} tests · dans la fourchette : {dans / len(r) * 100:3.0f} %")
            toutes += r
    if not toutes:
        print("Aucun résultat.")
        return
    n = len(toutes)
    dans = sum(1 for v, f in toutes if -v <= f <= v) / n * 100
    pire = sum(1 for v, f in toutes if f < -v) / n * 100
    mieux = sum(1 for v, f in toutes if f > v) / n * 100
    moy_vol = sum(v for v, f in toutes) / n * 100
    print(f"\nTOTAL : {n} tests sur {len(symboles)} actions (fourchette moyenne ±{moy_vol:.1f} %)")
    print(f"  dans la fourchette : {dans:.0f} %  (attendu ≈ 68 %)")
    print(f"  pire que la perte annoncée : {pire:.0f} %  (attendu ≈ 16 %)")
    print(f"  mieux que le gain annoncé : {mieux:.0f} %  (attendu ≈ 16 %)")


if __name__ == "__main__":
    main()
