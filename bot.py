"""Bot de veille boursière : il lit l'actualité, demande une analyse à Claude,
estime une fourchette, envoie des alertes et dessine dashboard.html.
Il ne passe JAMAIS d'ordre : c'est toi qui décides, dans ton appli.
"""
import json
import math
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import yfinance as yf

DOSSIER = Path(__file__).parent
DONNEES = DOSSIER / "data"
DONNEES.mkdir(exist_ok=True)
AVERTISSEMENT = "Estimation, pas une garantie. Ce n'est pas un conseil en investissement."


def charger_cle_api():
    """Lit la clé dans cle_api.txt (une seule ligne) si elle n'est pas déjà définie."""
    fichier = DOSSIER / "cle_api.txt"
    if not os.environ.get("ANTHROPIC_API_KEY") and fichier.exists():
        os.environ["ANTHROPIC_API_KEY"] = fichier.read_text(encoding="utf-8").strip()


charger_cle_api()


def lire_json(chemin, defaut):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except Exception:
        return defaut


def ecrire_json(chemin, contenu):
    Path(chemin).write_text(json.dumps(contenu, ensure_ascii=False, indent=2), encoding="utf-8")


def lire_favoris():
    lignes = (DOSSIER / "watchlist.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip().upper() for l in lignes if l.strip() and not l.strip().startswith("#")]


# ---------- 1. Données de marché ----------
def donnees_marche(symbole):
    """Cours, volatilité du mois, objectifs et recommandation des analystes."""
    t = yf.Ticker(symbole)
    hist = t.history(period="1y")["Close"].dropna()
    if len(hist) < 30:
        raise ValueError("pas assez d'historique")
    cours = float(hist.iloc[-1])
    veille = float(hist.iloc[-2])
    rendements = hist.pct_change().dropna()
    # volatilité sur ~1 mois de bourse (21 jours) à partir des variations quotidiennes
    vol_mois = float(rendements.std()) * math.sqrt(21)
    try:
        info = t.info or {}
    except Exception:
        info = {}
    return {
        "nom": info.get("shortName") or symbole,
        "devise": info.get("currency", ""),
        "capitalisation": info.get("marketCap"),
        "croissance_ca": info.get("revenueGrowth"),
        "cours": round(cours, 2),
        "variation_jour": round((cours / veille - 1) * 100, 2),
        "vol_mois": round(vol_mois * 100, 1),
        "objectif_moyen": info.get("targetMeanPrice"),
        "objectif_bas": info.get("targetLowPrice"),
        "objectif_haut": info.get("targetHighPrice"),
        "recommandation": info.get("recommendationKey"),
        "nb_analystes": info.get("numberOfAnalystOpinions"),
    }


def fourchette(m):
    """Ordre de grandeur sur ~1 mois, basé sur la volatilité et l'objectif moyen."""
    vol = m["vol_mois"]
    potentiel = None
    if m.get("objectif_moyen"):
        potentiel = round((m["objectif_moyen"] / m["cours"] - 1) * 100, 1)
    gain_haut = max(vol, potentiel or 0)
    return {
        "ecart_objectif": potentiel,
        "gain": [round(vol * 0.5, 1), round(gain_haut, 1)],
        "perte": [round(-vol * 0.5, 1), round(-vol, 1)],
    }


# ---------- 1b. Découverte de petites entreprises ----------
def decouvrir(config, deja):
    """Cherche des petites entreprises qui remplissent des critères chiffrés (config.json)."""
    d = config.get("decouverte", {})
    if not d.get("actif"):
        return []
    from yfinance import EquityQuery as EQ
    try:
        q = EQ("and", [
            EQ("is-in", ["exchange"] + d.get("places", ["NMS", "NYQ", "PAR"])),
            EQ("btwn", ["intradaymarketcap", d.get("cap_min", 3e8), d.get("cap_max", 3e9)]),
            EQ("btwn", ["quarterlyrevenuegrowth.quarterly", d.get("croissance_min", 20), d.get("croissance_max", 200)]),
        ])
        res = yf.screen(q, size=d.get("nombre", 10) + len(deja),
                        sortField="quarterlyrevenuegrowth.quarterly", sortAsc=False)
        symboles = [x["symbol"] for x in res["quotes"] if x["symbol"] not in deja]
        return symboles[: d.get("nombre", 10)]
    except Exception as e:
        print(f"  ! découverte impossible ({e})")
        return []


# ---------- 2. Actualités ----------
def articles(symbole, nom, combien):
    """Articles récents via le flux RSS de Google Actualités."""
    requete = urllib.parse.quote(f"{nom} stock OR action bourse")
    url = f"https://news.google.com/rss/search?q={requete}&hl=fr&gl=FR&ceid=FR:fr"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        racine = ET.fromstring(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        print(f"  ! actualités indisponibles pour {symbole} ({e})")
        return []
    res = []
    for item in racine.iter("item"):
        res.append({
            "titre": (item.findtext("title") or "").strip(),
            "lien": (item.findtext("link") or "").strip(),
            "source": (item.findtext("source") or "").strip(),
            "date": (item.findtext("pubDate") or "").strip(),
        })
        if len(res) >= combien:
            break
    return res


# ---------- 3. Analyse par Claude ----------
def analyser(symbole, m, arts, config):
    """Demande à Claude un avis prudent. Sans clé API, renvoie une analyse vide."""
    vide = {"sentiment": "inconnu", "importance": 0, "resume": "Analyse IA non activée (clé API manquante).", "risques": ""}
    if not os.environ.get("ANTHROPIC_API_KEY") or not arts:
        return vide
    import anthropic
    titres = "\n".join(f"- {a['titre']} ({a['source']})" for a in arts)
    consigne = f"""Tu aides un particulier à suivre l'action {m['nom']} ({symbole}).
Voici les titres d'actualité récents :
{titres}

Données : cours {m['cours']} {m['devise']}, objectif moyen des analystes {m['objectif_moyen']}, recommandation {m['recommandation']}.

Réponds UNIQUEMENT par un JSON : {{"sentiment": "positif|neutre|negatif", "importance": 1 à 5, "resume": "2 phrases simples en français", "risques": "1 phrase sur ce qui pourrait mal tourner"}}.
Sois prudent : si les titres ne disent rien de concret, mets importance 1. N'invente aucun fait absent des titres."""
    try:
        rep = anthropic.Anthropic().messages.create(
            model=config["model"], max_tokens=400,
            messages=[{"role": "user", "content": consigne}])
        texte = rep.content[0].text
        return json.loads(texte[texte.index("{"): texte.rindex("}") + 1])
    except Exception as e:
        vide["resume"] = f"Analyse IA impossible ({e})"
        return vide


# ---------- 4. Alertes ----------
def alerter(config, titre, message, lien=None):
    print(f"\n🔔 {titre}\n{message}\n")
    sujet = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    if not sujet:
        return
    en_tetes = {"Title": titre.encode("utf-8")}
    if lien:
        en_tetes["Click"] = lien
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{sujet}", data=message.encode("utf-8"), headers=en_tetes)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"  ! envoi ntfy impossible ({e})")


# ---------- 5. Tableau de bord ----------
def dessiner_dashboard(fiches, journal):
    modele = (DOSSIER / "dashboard_modele.html").read_text(encoding="utf-8")
    donnees = {"maj": datetime.now().strftime("%d/%m/%Y %H:%M"), "fiches": fiches,
               "journal": journal[-100:][::-1], "avertissement": AVERTISSEMENT}
    html = modele.replace("/*DONNEES*/null", json.dumps(donnees, ensure_ascii=False).replace("</", "<\\/"))
    (DOSSIER / "dashboard.html").write_text(html, encoding="utf-8")


def score_potentiel(f):
    """Score simple : actualité positive + écart vers l'objectif des analystes."""
    pts = {"positif": 2, "neutre": 0, "negatif": -2}.get(f["analyse"]["sentiment"], 0)
    pts += f["analyse"].get("importance", 0) * 0.5
    ecart = f["fourchette"]["ecart_objectif"]
    if ecart is not None:
        pts += max(min(ecart / 10, 2), -2)
    return round(pts, 1)


def main():
    config = lire_json(DOSSIER / "config.json", {})
    etat = lire_json(DONNEES / "etat.json", {"vus": []})
    journal = lire_json(DONNEES / "journal.json", [])
    fiches = []

    favoris = lire_favoris()
    decouverts = decouvrir(config, favoris)
    print(f"{len(favoris)} favoris, {len(decouverts)} entreprises découvertes")

    for symbole in favoris + decouverts:
        print(f"→ {symbole}")
        try:
            m = donnees_marche(symbole)
        except Exception as e:
            print(f"  ! données indisponibles ({e})")
            continue
        arts = articles(symbole, m["nom"], config.get("articles_par_action", 5))
        analyse = analyser(symbole, m, arts, config)
        f = {"symbole": symbole, "decouverte": symbole in decouverts, **m, "fourchette": fourchette(m), "articles": arts, "analyse": analyse}
        f["score"] = score_potentiel(f)
        fiches.append(f)

        # Alerte seulement pour une actu importante et nouvelle
        nouveaux = [a for a in arts if a["lien"] not in etat["vus"]]
        if nouveaux and analyse.get("importance", 0) >= config.get("alerte_importance_min", 4):
            fo = f["fourchette"]
            msg = (f"{analyse['resume']}\nCours : {m['cours']} {m['devise']}\n"
                   f"Fourchette ~1 mois : gain {fo['gain'][0]} à {fo['gain'][1]} % / perte {fo['perte'][1]} à {fo['perte'][0]} %\n"
                   f"{AVERTISSEMENT}")
            alerter(config, f"{m['nom']} : actu {analyse['sentiment']}", msg, nouveaux[0]["lien"])
            journal.append({"date": datetime.now().strftime("%d/%m/%Y %H:%M"), "symbole": symbole,
                            "sentiment": analyse["sentiment"], "cours": m["cours"],
                            "gain": fo["gain"], "perte": fo["perte"], "resume": analyse["resume"]})
        etat["vus"] = (etat["vus"] + [a["lien"] for a in arts])[-500:]

    ecrire_json(DONNEES / "etat.json", etat)
    ecrire_json(DONNEES / "journal.json", journal)
    # le journal garde le cours du signal : le tableau compare avec le cours actuel
    actuel = {f["symbole"]: f["cours"] for f in fiches}
    for j in journal:
        j["cours_actuel"] = actuel.get(j["symbole"])
    dessiner_dashboard(fiches, journal)
    print("\nTerminé. Ouvre dashboard.html dans ton navigateur.")


if __name__ == "__main__":
    main()
