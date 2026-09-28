"""
ZuntyTCG-Bot — détecteur de bons plans Pokémon sur Vinted.

Usage :
    python ZuntyTCG-Bot.py            -> un seul passage (GitHub Actions)
    python ZuntyTCG-Bot.py --loop 90  -> tourne en continu, 1 passage toutes les 90 s (PC / Raspberry)
"""
import os
import sys
import json
import time
import html
import random
import argparse
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import types

sys.stdout.reconfigure(line_buffering=True)

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

HISTORIQUE_FILE = "historique_vinted.json"
TIMEOUT = (5, 15)

# Recherches lancées à chaque passage
RECHERCHES = [
    "carte pokemon",
    "pokemon display",
    "pokemon ETB",
    "carte pokemon gradée PSA",
    "lot cartes pokemon",
]

# Alerte immédiate (sans IA) si le titre contient le mot ET que le prix est <= au max
WATCHLIST = {
    "dracaufeu": 25,
    "charizard": 25,
    "display": 90,
    "etb": 35,
    "psa 10": 60,
    "alternative": 30,
    "gold": 20,
}

# Annonces ignorées directement (faux, recherches, accessoires...)
MOTS_EXCLUS = [
    "proxy", "fake", "custom", "réplique", "replica", "goldée", "metal", "métal",
    "recherche", "cherche", "classeur vide", "sleeve", "protège", "pochette", "peluche",
    "figurine", "jeu vidéo", "switch", "3ds",
]

PRIX_MIN = 2.0          # en dessous : souvent des arnaques ou des cartes communes
PRIX_MAX = 500.0        # au dessus : hors budget
SCORE_ALERTE = 7        # note Gemini minimale (sur 10) pour alerter
REDUCTION_MIN = 35      # % de réduction estimée minimale
PAUSE_GEMINI = 4        # secondes entre deux appels (quota gratuit)
ANALYSE_PHOTO = True    # envoie la photo à Gemini pour repérer les faux

# ================= GEMINI =================
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=45000))
    print(f"🤖 Gemini actif ({GEMINI_MODEL})")
else:
    print("⚠️ GEMINI_API_KEY absente : seule la WATCHLIST déclenchera des alertes.")

# ================= HISTORIQUE =================
def charger_historique():
    try:
        with open(HISTORIQUE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [d["id"] if isinstance(d, dict) else d for d in data]
    except Exception:
        return []

def sauvegarder_historique(ids):
    with open(HISTORIQUE_FILE, "w", encoding="utf-8") as f:
        json.dump(ids[-2000:], f, indent=2)

# ================= TELEGRAM =================
def envoyer_telegram(message, photo_url=None, lien=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Identifiants Telegram manquants.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    clavier = {"inline_keyboard": [[{"text": "🛒 Ouvrir sur Vinted", "url": lien}]]} if lien else None

    def post(methode, payload):
        if clavier:
            payload["reply_markup"] = clavier
        return requests.post(f"{base}/{methode}", json=payload, timeout=TIMEOUT)

    try:
        if photo_url:
            r = post("sendPhoto", {"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url,
                                   "caption": message[:1024], "parse_mode": "HTML"})
            if r.status_code == 200:
                return True
            print(f"   ⚠️ sendPhoto échoué ({r.status_code}), envoi en texte.")
        r = post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                 "parse_mode": "HTML", "disable_web_page_preview": False})
        if r.status_code != 200:
            print(f"   ⚠️ Erreur Telegram : {r.text}")
        return r.status_code == 200
    except Exception as e:
        print(f"   ⚠️ Exception Telegram : {e}")
        return False

# ================= VINTED =================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

def nouvelle_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get("https://www.vinted.fr/", timeout=TIMEOUT)
    print(f"🌐 Session Vinted : {r.status_code}")
    return s

def extraire_prix(item):
    p = item.get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(str(p).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0

def chercher(session, query, essais=3):
    for essai in range(1, essais + 1):
        try:
            r = session.get("https://www.vinted.fr/api/v2/catalog/items", timeout=TIMEOUT, params={
                "search_text": query, "order": "newest_first", "per_page": 30})
            if r.status_code == 200:
                return r.json().get("items", [])
            if r.status_code == 401:  # cookie expiré -> on renouvelle
                session.cookies.clear()
                session.get("https://www.vinted.fr/", timeout=TIMEOUT)
            elif r.status_code == 403:
                print("   ⛔ 403 : Vinted bloque cette IP (fréquent sur GitHub Actions).")
                return []
            print(f"   ⚠️ '{query}' -> {r.status_code} (essai {essai}/{essais})")
        except Exception as e:
            print(f"   ⚠️ '{query}' -> {e} (essai {essai}/{essais})")
        time.sleep(2 * essai + random.random())
    return []

def recuperer_annonces():
    try:
        session = nouvelle_session()
    except Exception as e:
        print(f"⚠️ Impossible de joindre Vinted : {e}")
        return []

    annonces, vus = [], set()
    for query in RECHERCHES:
        items = chercher(session, query)
        print(f"📡 '{query}' : {len(items)} annonces")
        for item in items:
            item_id = item.get("id")
            if not item_id or item_id in vus:
                continue
            vus.add(item_id)
            annonces.append({
                "id": f"vinted_{item_id}",
                "title": item.get("title", "Sans titre"),
                "price": extraire_prix(item),
                "url": item.get("url") or f"https://www.vinted.fr/items/{item_id}",
                "photo": (item.get("photo") or {}).get("url"),
                "status": item.get("status", ""),
                "vendeur": (item.get("user") or {}).get("login", ""),
            })
        time.sleep(1.5 + random.random())
    return annonces

# ================= FILTRES =================
def est_exclue(a):
    titre = a["title"].lower()
    if any(m in titre for m in MOTS_EXCLUS):
        return True
    return not (PRIX_MIN <= a["price"] <= PRIX_MAX)

def match_watchlist(a):
    titre = a["title"].lower()
    for mot, prix_max in WATCHLIST.items():
        if mot in titre and a["price"] <= prix_max:
            return mot, prix_max
    return None

# ================= ANALYSE IA =================
PROMPT = """Tu es un expert du marché français des cartes Pokémon (Cardmarket, eBay, Vinted).
Analyse cette annonce Vinted :
- Titre : {title}
- Prix : {price} €
- État : {status}

Estime le prix de marché réel en France, puis juge si c'est une affaire.
Méfie-toi des faux, proxys, cartes abîmées et titres trompeurs (photo fournie si disponible).
Réponds en JSON :
{{"prix_marche": nombre en euros, "reduction_pct": nombre, "score": entier de 0 à 10,
  "suspect": true ou false, "raison": "une phrase courte"}}"""

def telecharger_image(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and len(r.content) < 4_000_000:
            return r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    except Exception:
        pass
    return None, None

def analyser(a):
    if not client:
        return None
    contenu = [PROMPT.format(**a)]
    if ANALYSE_PHOTO and a.get("photo"):
        data, mime = telecharger_image(a["photo"])
        if data:
            contenu.append(types.Part.from_bytes(data=data, mime_type=mime))
    try:
        rep = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=contenu,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               temperature=0.2),
        )
        return json.loads(rep.text)
    except Exception as e:
        print(f"   ⚠️ Erreur Gemini : {e}")
        return None

# ================= MESSAGE =================
def formater(a, source, analyse=None):
    e = html.escape
    lignes = [f"<b>🔥 {source}</b>", "",
              f"📦 <b>{e(a['title'])}</b>",
              f"💰 <b>{a['price']:.2f} €</b>"]
    if analyse:
        lignes.append(f"📊 Marché estimé : ~{analyse.get('prix_marche', '?')} € "
                      f"(-{analyse.get('reduction_pct', '?')}%)")
        lignes.append(f"⭐ Score : {analyse.get('score', '?')}/10")
        lignes.append(f"💡 {e(str(analyse.get('raison', '')))}")
    if a.get("status"):
        lignes.append(f"🏷️ État : {e(a['status'])}")
    if a.get("vendeur"):
        lignes.append(f"👤 {e(a['vendeur'])}")
    return "\n".join(lignes)

# ================= CYCLE =================
def cycle():
    ids = charger_historique()
    connus = set(ids)
    premier_lancement = not ids

    annonces = recuperer_annonces()
    nouvelles = [a for a in annonces if a["id"] not in connus]
    print(f"🆕 {len(nouvelles)} nouvelles annonces sur {len(annonces)}")

    if premier_lancement and nouvelles:
        # Évite d'envoyer 100 alertes au tout premier lancement
        print("ℹ️ Premier lancement : annonces mémorisées sans alerte.")
        sauvegarder_historique([a["id"] for a in nouvelles])
        return

    alertes = 0
    for a in nouvelles:
        ids.append(a["id"])
        if est_exclue(a):
            continue

        wl = match_watchlist(a)
        if wl:
            print(f"🎯 WATCHLIST '{wl[0]}' : {a['title']} — {a['price']} €")
            if envoyer_telegram(formater(a, f"WATCHLIST : {wl[0]} ≤ {wl[1]} €"), a["photo"], a["url"]):
                alertes += 1
            continue

        if not client:
            continue
        print(f"🔍 IA : {a['title']} — {a['price']} €")
        res = analyser(a)
        time.sleep(PAUSE_GEMINI)
        if not res or res.get("suspect"):
            continue
        try:
            score = int(res.get("score", 0))
            reduc = float(res.get("reduction_pct", 0))
        except (TypeError, ValueError):
            continue
        if score >= SCORE_ALERTE and reduc >= REDUCTION_MIN:
            if envoyer_telegram(formater(a, "BON PLAN DÉTECTÉ PAR L'IA", res), a["photo"], a["url"]):
                alertes += 1
                print(f"   ✅ Alerte envoyée (score {score}, -{reduc}%)")

    sauvegarder_historique(ids)
    print(f"✨ Cycle terminé : {alertes} alerte(s).")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="intervalle en secondes pour tourner en continu")
    args = parser.parse_args()

    print("🚀 ZuntyTCG-Bot démarré")
    if not args.loop:
        cycle()
        return
    while True:
        try:
            cycle()
        except Exception as e:
            print(f"💥 Erreur inattendue : {e}")
        attente = args.loop + random.randint(0, 20)
        print(f"⏳ Prochain passage dans {attente} s\n")
        time.sleep(attente)

if __name__ == "__main__":
    main()
