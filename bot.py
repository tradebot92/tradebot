"""Bot de veille boursière : il lit l'actualité, demande une analyse à Claude,
estime une fourchette, envoie des alertes et dessine dashboard.html.
Il ne passe JAMAIS d'ordre : c'est toi qui décides, dans ton appli.
"""
import hashlib
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
try:
    from zoneinfo import ZoneInfo
    FUSEAU = ZoneInfo("Europe/Paris")
except Exception:
    FUSEAU = None


def maintenant():
    """Date et heure de Paris (pour que l'heure affichée soit la tienne)."""
    return datetime.now(FUSEAU) if FUSEAU else datetime.now()


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
    try:
        cal = t.calendar or {}
        dates = cal.get("Earnings Date") or []
        resultats = str(dates[0]) if dates else None
    except Exception:
        resultats = None
    six_mois = hist.iloc[-126:]
    return {
        "serie": [round(float(x), 2) for x in six_mois.iloc[::2]],
        "perf_6m": round((float(six_mois.iloc[-1]) / float(six_mois.iloc[0]) - 1) * 100, 1),
        "plus_haut_52": info.get("fiftyTwoWeekHigh"),
        "plus_bas_52": info.get("fiftyTwoWeekLow"),
        "secteur": info.get("sector"),
        "industrie": info.get("industry"),
        "pays": info.get("country"),
        "activite": (info.get("longBusinessSummary") or "")[:600],
        "prochains_resultats": resultats,
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
    """Cherche des petites entreprises avec 3 filtres différents (croissance, volume, hausse).
    Renvoie une liste de {"symbole", "raisons"}, avec les raisons qui l'ont fait remonter."""
    d = config.get("decouverte", {})
    if not d.get("actif"):
        return []
    from yfinance import EquityQuery as EQ
    base = [EQ("is-in", ["exchange"] + d.get("places", ["NMS", "NYQ", "PAR"])),
            EQ("btwn", ["intradaymarketcap", d.get("cap_min", 3e8), d.get("cap_max", 3e9)])]
    filtres = {
        "croissance": (base + [EQ("btwn", ["quarterlyrevenuegrowth.quarterly", d.get("croissance_min", 20), d.get("croissance_max", 200)])],
                       "quarterlyrevenuegrowth.quarterly"),
        "volume": (base, "dayvolume"),
        "hausse": (base, "percentchange"),
    }
    taille = d.get("vivier_par_filtre", 15)
    candidats = {}
    for nom, (criteres, tri) in filtres.items():
        try:
            res = yf.screen(EQ("and", criteres), size=taille, sortField=tri, sortAsc=False)
        except Exception as e:
            print(f"  ! filtre {nom} impossible ({e})")
            continue
        for x in res.get("quotes", []):
            sym = x["symbol"]
            if sym in deja:
                continue
            c = candidats.setdefault(sym, {"symbole": sym, "raisons": [], "points": 0.0})
            vol, moy = x.get("regularMarketVolume"), x.get("averageDailyVolume3Month")
            ratio = vol / moy if vol and moy else 0
            var = x.get("regularMarketChangePercent") or 0
            c.setdefault("filtres", set()).add(nom)
            if nom == "croissance":
                c["raisons"].append("Forte croissance du chiffre d'affaires (+20 % ou plus sur le dernier trimestre)")
                c["points"] += 1
            elif nom == "volume" and ratio >= d.get("volume_ratio_min", 2):
                c["raisons"].append(f"Volume d'échanges {ratio:.1f} fois supérieur à la normale")
                c["points"] += 1 + min(ratio, 10) / 10
            elif nom == "hausse" and var >= d.get("hausse_min", 8):
                c["raisons"].append(f"En hausse de {var:.1f} % aujourd'hui")
                c["points"] += 1 + min(var, 30) / 100
    classes = sorted((c for c in candidats.values() if c["raisons"]), key=lambda c: c["points"], reverse=True)
    # on réserve des places pour chaque filtre, sinon celui du jour écrase les autres
    nombre = d.get("nombre", 10)
    choisis = []
    quota = {"croissance": max(1, nombre // 3), "volume": max(1, nombre // 3), "hausse": max(1, nombre // 3)}
    for nom_filtre, n in quota.items():
        for c in classes:
            if n <= 0:
                break
            if nom_filtre in c["filtres"] and c["raisons"] and c not in choisis:
                choisis.append(c)
                n -= 1
    for c in classes:
        if len(choisis) >= nombre:
            break
        if c not in choisis:
            choisis.append(c)
    return sorted(choisis, key=lambda c: c["points"], reverse=True)[:nombre]


def raisons_en_plus(m, f_ecart):
    """Raisons ajoutées une fois les données de marché récupérées."""
    r = []
    s = m.get("serie") or []
    if len(s) >= 11 and s[-11] > 0 and (s[-1] / s[-11] - 1) * 100 >= 15:
        r.append(f"Forte progression sur ~1 mois ({(s[-1] / s[-11] - 1) * 100:+.0f} %)")
    if f_ecart is not None and f_ecart >= 25 and (m.get("nb_analystes") or 0) >= 3:
        r.append(f"Cours {f_ecart:.0f} % sous l'objectif moyen des analystes")
    return r


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
def appeler_claude(config, consigne, max_tokens):
    import anthropic
    rep = anthropic.Anthropic().messages.create(
        model=config["model"], max_tokens=max_tokens,
        messages=[{"role": "user", "content": consigne}])
    texte = rep.content[0].text
    return json.loads(texte[texte.index("{"): texte.rindex("}") + 1])


def signature(*morceaux):
    return hashlib.sha1("|".join(str(x) for x in morceaux).encode("utf-8")).hexdigest()[:16]


def analyser(symbole, m, arts, config, cache):
    """Analyse détaillée par Claude. Réutilise l'ancienne analyse si rien de nouveau (économise l'API)."""
    vide = {"sentiment": "inconnu", "importance": 0, "confiance": "", "points_forts": [], "risques": [],
            "catalyseurs": [], "a_surveiller": "",
            "resume": "Analyse IA non activée (clé API manquante)."}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return vide
    if not arts:
        vide["resume"] = "Aucun article récent trouvé : rien à analyser pour l'instant."
        return vide
    sig = signature(*[a["lien"] for a in arts], m.get("objectif_moyen"), m.get("prochains_resultats"))
    if cache.get(symbole, {}).get("sig") == sig:
        return cache[symbole]["analyse"]
    titres = "\n".join(f"- {a['titre']} ({a['source']})" for a in arts)
    consigne = f"""Tu aides un particulier à suivre l'action {m['nom']} ({symbole}), sans jamais lui dire d'acheter ou de vendre.

ACTIVITÉ : {m.get('activite') or 'inconnue'}
Secteur : {m.get('secteur')} / {m.get('industrie')} ({m.get('pays')}). Capitalisation : {m.get('capitalisation')}.
COURS : {m['cours']} {m['devise']} (6 mois : {m['perf_6m']} %, plus haut 52 sem. {m.get('plus_haut_52')}, plus bas {m.get('plus_bas_52')}).
CROISSANCE du chiffre d'affaires : {m.get('croissance_ca')}.
ANALYSTES : objectif bas/moyen/haut {m.get('objectif_bas')} / {m.get('objectif_moyen')} / {m.get('objectif_haut')}, avis {m.get('recommandation')} ({m.get('nb_analystes')} analystes).
PROCHAINS RÉSULTATS : {m.get('prochains_resultats')}.
TITRES D'ACTUALITÉ RÉCENTS :
{titres}

Réponds UNIQUEMENT par un JSON :
{{"sentiment": "positif|neutre|negatif", "importance": 1 à 5, "confiance": "faible|moyenne|forte",
"resume": "2 à 3 phrases simples en français : de quoi parle l'actu et pourquoi ça compte",
"points_forts": ["max 3 courts"], "risques": ["max 3 courts"],
"catalyseurs": ["max 2 événements à venir qui pourraient faire bouger le cours"],
"a_surveiller": "1 phrase"}}
Règles : appuie-toi uniquement sur les données ci-dessus, n'invente aucun fait. Si les titres ne disent rien de concret, mets importance 1 et confiance faible. Sois prudent sur les petites entreprises très volatiles."""
    try:
        analyse = appeler_claude(config, consigne, 900)
        cache[symbole] = {"sig": sig, "analyse": analyse}
        return analyse
    except Exception as e:
        vide["resume"] = f"Analyse IA impossible ({e})"
        return vide


def synthese(fiches, config, cache):
    """Résumé du jour : les 3 entreprises qui ressortent le plus."""
    haut = sorted(fiches, key=lambda f: f["score"], reverse=True)[:3]
    base = {"points": [{"symbole": f["symbole"], "nom": f["nom"],
                        "pourquoi": f["analyse"].get("resume", ""),
                        "a_surveiller": f["analyse"].get("a_surveiller", "")} for f in haut],
            "prudence": "Ce sont des pistes à étudier, pas des recommandations."}
    if not haut or not os.environ.get("ANTHROPIC_API_KEY") or haut[0]["analyse"]["sentiment"] == "inconnu":
        return base
    resume = "\n".join(f"- {f['symbole']} ({f['nom']}) : score {f['score']}, actu {f['analyse']['sentiment']}, "
                       f"{f['analyse'].get('resume')} Risques : {f['analyse'].get('risques')}" for f in haut)
    sig = signature(resume)
    if cache.get("_synthese", {}).get("sig") == sig:
        return cache["_synthese"]["contenu"]
    consigne = f"""Voici les 3 entreprises qui ressortent aujourd'hui sur la veille d'un particulier :
{resume}
Écris UNIQUEMENT un JSON : {{"points": [{{"symbole": "...", "pourquoi": "1 phrase simple", "a_surveiller": "1 phrase simple"}}], "prudence": "1 phrase rappelant le risque"}}.
Ton neutre, en français. Interdit : dire d'acheter, de vendre, ou promettre un gain. Reste fidèle au texte ci-dessus."""
    try:
        contenu = appeler_claude(config, consigne, 600)
        for p in contenu.get("points", []):
            p["nom"] = next((f["nom"] for f in haut if f["symbole"] == p.get("symbole")), "")
        cache["_synthese"] = {"sig": sig, "contenu": contenu}
        return contenu
    except Exception:
        return base


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
def dessiner_dashboard(fiches, journal, resume_jour, config):
    modele = (DOSSIER / "dashboard_modele.html").read_text(encoding="utf-8")
    donnees = {"maj": maintenant().strftime("%d/%m/%Y %H:%M"), "fiches": fiches,
               "journal": journal[-100:][::-1], "avertissement": AVERTISSEMENT,
               "synthese": resume_jour, "depot": config.get("depot", ""),
               "sorties": lire_json(DONNEES / "decouvertes.json", {}).get("sorties", [])[::-1]}
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
    cache = lire_json(DONNEES / "analyses.json", {})
    premiere_fois = not etat.get("init")   # au tout premier lancement, pas d'alertes en rafale
    fiches = []

    favoris = lire_favoris()
    decouverts_info = {c["symbole"]: c for c in decouvrir(config, favoris)}
    decouverts = list(decouverts_info)
    print(f"{len(favoris)} favoris, {len(decouverts)} entreprises découvertes")

    # historique des découvertes : nouveautés et sorties
    histo = lire_json(DONNEES / "decouvertes.json", {"actuel": {}, "sorties": []})
    maintenant_iso = maintenant().isoformat(timespec="minutes")
    for sym in list(histo["actuel"]):
        if sym not in decouverts_info:
            histo["sorties"].append({"symbole": sym, "date": maintenant().strftime("%d/%m/%Y"),
                                     "nom": histo["actuel"][sym].get("nom", "")})
            del histo["actuel"][sym]
    histo["sorties"] = histo["sorties"][-30:]

    for symbole in favoris + decouverts:
        print(f"→ {symbole}")
        try:
            m = donnees_marche(symbole)
        except Exception as e:
            print(f"  ! données indisponibles ({e})")
            continue
        arts = articles(symbole, m["nom"], config.get("articles_par_action", 5))
        analyse = analyser(symbole, m, arts, config, cache)
        f = {"symbole": symbole, "decouverte": symbole in decouverts, **m, "fourchette": fourchette(m), "articles": arts, "analyse": analyse}
        f["score"] = score_potentiel(f)
        if f["decouverte"]:
            f["raisons"] = decouverts_info[symbole]["raisons"] + raisons_en_plus(m, f["fourchette"]["ecart_objectif"])
            fiche_histo = histo["actuel"].setdefault(symbole, {"premiere": maintenant_iso})
            fiche_histo["nom"] = m["nom"]
            try:
                age = maintenant() - datetime.fromisoformat(fiche_histo["premiere"])
                f["nouveau"] = age.total_seconds() < 48 * 3600
            except Exception:
                f["nouveau"] = False
        fiches.append(f)

        # Alerte seulement pour une actu importante et nouvelle
        nouveaux = [a for a in arts if a["lien"] not in etat["vus"]]
        if nouveaux and not premiere_fois and analyse.get("importance", 0) >= config.get("alerte_importance_min", 4):
            fo = f["fourchette"]
            msg = (f"{analyse['resume']}\nCours : {m['cours']} {m['devise']}\n"
                   f"Fourchette ~1 mois : gain {fo['gain'][0]} à {fo['gain'][1]} % / perte {fo['perte'][1]} à {fo['perte'][0]} %\n"
                   f"{AVERTISSEMENT}")
            alerter(config, f"{m['nom']} : actu {analyse['sentiment']}", msg, nouveaux[0]["lien"])
            journal.append({"date": maintenant().strftime("%d/%m/%Y %H:%M"), "symbole": symbole,
                            "sentiment": analyse["sentiment"], "cours": m["cours"],
                            "gain": fo["gain"], "perte": fo["perte"], "resume": analyse["resume"]})
        etat["vus"] = (etat["vus"] + [a["lien"] for a in arts])[-500:]

    etat["init"] = True
    ecrire_json(DONNEES / "etat.json", etat)
    ecrire_json(DONNEES / "journal.json", journal)
    ecrire_json(DONNEES / "decouvertes.json", histo)
    resume_jour = synthese(fiches, config, cache)
    ecrire_json(DONNEES / "analyses.json", cache)
    # le journal garde le cours du signal : le tableau compare avec le cours actuel
    actuel = {f["symbole"]: f["cours"] for f in fiches}
    for j in journal:
        j["cours_actuel"] = actuel.get(j["symbole"])
    dessiner_dashboard(fiches, journal, resume_jour, config)
    print("\nTerminé. Ouvre dashboard.html dans ton navigateur.")


if __name__ == "__main__":
    main()
