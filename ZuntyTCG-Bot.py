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
import re
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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # secours gratuit : console.groq.com/keys

# Ordre de préférence. Les autres modèles disponibles sur ton compte
# sont ajoutés automatiquement à la suite au démarrage.
MODELES_SECOURS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]
TOURS_MAX = 3           # nombre de tours complets sur tous les modèles
PAUSE_TOUR = [5, 20, 45]  # attente (s) entre deux tours
# Modèles essayés dans l'ordre si le principal est saturé (erreur 503/429)
MODELES_SECOURS = ["gemini-3.5-flash-lite", "gemini-3.6-flash"]

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
SCORE_ALERTE = 7        # note de revendabilité minimale (sur 10)
BENEF_MIN = 10.0        # bénéfice net minimum en € pour alerter
ROI_MIN = 30            # rentabilité minimale en % (bénéfice / coût d'achat)

# Coûts réels d'un achat Vinted (à ajuster si besoin)
PROTECTION_FIXE = 0.70  # protection acheteurs : 0,70 € + 5 % du prix
PROTECTION_PCT = 0.05
LIVRAISON = 3.50        # frais de port moyens à l'achat
FRAIS_REVENTE_PCT = 0.05  # commission à la revente (Cardmarket ~5 %, Vinted 0 %)
PAUSE_GEMINI = 4        # secondes entre deux appels (quota gratuit)
ANALYSE_PHOTO = True    # envoie la photo à Gemini pour repérer les faux

# ================= GEMINI =================
client = None
MODELES = []
EXCLUS_MODELES = ("tts", "image", "embedding", "live", "audio", "veo", "imagen",
                  "banana", "aqa", "robotics", "computer-use", "native", "lyria")

def decouvrir_modeles():
    """Liste de modèles à essayer : préférés d'abord, puis tous ceux du compte."""
    ordre = [GEMINI_MODEL] + [m for m in MODELES_SECOURS if m != GEMINI_MODEL]
    try:
        dispo = []
        for m in client.models.list():
            nom = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if "gemini" not in nom or any(x in nom for x in EXCLUS_MODELES):
                continue
            if actions and "generateContent" not in actions:
                continue
            dispo.append(nom)
        if dispo:
            ordre = [m for m in ordre if m in dispo] + sorted(
                (m for m in dispo if m not in ordre),
                key=lambda n: ("flash" not in n, "preview" in n or "exp" in n, n))
    except Exception as e:
        print(f"⚠️ Liste des modèles indisponible ({e}), liste par défaut utilisée.")
    return list(dict.fromkeys(ordre))

if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=45000))
    MODELES = decouvrir_modeles()
    print(f"🤖 Gemini actif : {len(MODELES)} modèles -> {', '.join(MODELES[:6])}"
          + (" ..." if len(MODELES) > 6 else ""))
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

# ================= VINTED (nouvelle API sept. 2026) =================
WWW = "https://www.vinted.fr"
API = "https://api.vinted.fr/svc-catalogue/items"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

def nouvelle_session():
    """Récupère le jeton anonyme que Vinted donne à tout visiteur."""
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(f"{WWW}/catalog", timeout=TIMEOUT,
              headers={"Accept": "text/html,application/xhtml+xml"})
    token = s.cookies.get("access_token_web")
    anon_id = r.headers.get("x-anon-id") or s.cookies.get("anon_id")
    csrf = re.search(r'CSRF_TOKEN\\?"\s*:\s*\\?"([^"\\]+)', r.text)
    print(f"🌐 Session Vinted : {r.status_code} | jeton : {'OK' if token else 'ABSENT'}")

    s.headers.update({"Accept": "application/json, text/plain, */*",
                      "Origin": WWW, "Referer": f"{WWW}/catalog"})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    if anon_id:
        s.headers["X-Anon-Id"] = anon_id
    if csrf:
        s.headers["X-Csrf-Token"] = csrf.group(1)
    return s

def extraire_prix(item):
    p = item.get("price") or item.get("offers", {}).get("price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(str(p).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0

def extraire_photo(item):
    photo = item.get("photo")
    if not photo and item.get("photos"):
        photo = item["photos"][0]
    if not photo:
        photo = item.get("image")
    if isinstance(photo, list):
        photo = photo[0] if photo else None
    if isinstance(photo, dict):
        photo = photo.get("url") or photo.get("full_size_url") or photo.get("contentUrl")
    return photo

def normaliser(item):
    url = item.get("url") or ""
    if url.startswith("/"):
        url = WWW + url
    item_id = item.get("id")
    if not item_id:
        m = re.search(r"/items/(\d+)", url)
        item_id = m.group(1) if m else None
    if not item_id:
        return None
    user = item.get("user") or {}
    return {
        "id": f"vinted_{item_id}",
        "title": item.get("title") or item.get("name") or "Sans titre",
        "price": extraire_prix(item),
        "url": url or f"{WWW}/items/{item_id}",
        "photo": extraire_photo(item),
        "status": item.get("status") or item.get("item_condition") or "",
        "vendeur": user.get("login", "") if isinstance(user, dict) else "",
    }

def chercher_api(session, query):
    r = session.get(API, timeout=TIMEOUT, params={
        "search_text": query, "order": "newest_first", "page": 1,
        "per_page": 30, "time": int(time.time()), "currency": "EUR"})
    if r.status_code != 200:
        print(f"   ⚠️ API '{query}' -> {r.status_code} {r.text[:150]}")
        return None
    data = r.json()
    return data.get("items") or data.get("catalog_items") or []

def chercher_page(session, query):
    """Secours : lit les annonces dans le JSON-LD de la page catalogue."""
    r = session.get(f"{WWW}/catalog", timeout=TIMEOUT,
                    params={"search_text": query, "order": "newest_first"},
                    headers={"Accept": "text/html"})
    if r.status_code != 200:
        print(f"   ⚠️ Page '{query}' -> {r.status_code}")
        return []
    items = []
    for bloc in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(bloc)
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            for el in d.get("itemListElement", []) if isinstance(d, dict) else []:
                items.append(el.get("item", el))
    return items

def recuperer_annonces():
    try:
        session = nouvelle_session()
    except Exception as e:
        print(f"⚠️ Impossible de joindre Vinted : {e}")
        return []

    annonces, vus = [], set()
    for query in RECHERCHES:
        items = None
        try:
            items = chercher_api(session, query)
            if items is None:
                items = chercher_page(session, query)
                print(f"   ↪️ secours page catalogue : {len(items)} annonces")
        except Exception as e:
            print(f"   ⚠️ '{query}' -> {e}")
            items = []
        print(f"📡 '{query}' : {len(items)} annonces")
        for item in items:
            a = normaliser(item)
            if a and a["id"] not in vus:
                vus.add(a["id"])
                annonces.append(a)
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

# ================= GROQ (secours gratuit) =================
GROQ_URL = "https://api.groq.com/openai/v1"
GROQ_PREFERES = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b",
                 "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
GROQ_EXCLUS = ("whisper", "tts", "guard", "playai", "orpheus", "safeguard", "compound")
GROQ_MODELES = []

def decouvrir_groq():
    if not GROQ_API_KEY:
        return []
    try:
        r = requests.get(f"{GROQ_URL}/models", timeout=TIMEOUT,
                         headers={"Authorization": f"Bearer {GROQ_API_KEY}"})
        dispo = [m["id"] for m in r.json().get("data", [])
                 if m.get("active", True) and not any(x in m["id"] for x in GROQ_EXCLUS)]
        if dispo:
            return [m for m in GROQ_PREFERES if m in dispo] + sorted(
                m for m in dispo if m not in GROQ_PREFERES)
        print(f"⚠️ Groq : liste vide ({r.status_code}), liste par défaut utilisée.")
    except Exception as e:
        print(f"⚠️ Groq : liste indisponible ({e}), liste par défaut utilisée.")
    return list(GROQ_PREFERES)

GROQ_MODELES = decouvrir_groq()
if GROQ_MODELES:
    print(f"🦙 Secours Groq actif : {len(GROQ_MODELES)} modèles")

def analyser_groq(prompt):
    for modele in list(GROQ_MODELES):
        try:
            r = requests.post(f"{GROQ_URL}/chat/completions", timeout=(5, 40),
                              headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                              json={"model": modele, "temperature": 0.2,
                                    "response_format": {"type": "json_object"},
                                    "messages": [{"role": "user", "content": prompt}]})
            if r.status_code == 200:
                texte = r.json()["choices"][0]["message"]["content"]
                texte = texte.replace("```json", "").replace("```", "").strip()
                res = json.loads(texte)
                print(f"   🦙 Analysé par Groq ({modele})")
                return res
            if r.status_code == 404 and modele in GROQ_MODELES and len(GROQ_MODELES) > 1:
                GROQ_MODELES.remove(modele)
            print(f"   ⚠️ Groq {modele} : {r.status_code} -> modèle suivant")
        except Exception as e:
            print(f"   ⚠️ Groq {modele} : {str(e)[:80]} -> modèle suivant")
    return None

# ================= ANALYSE IA =================
PROMPT = """Tu es un expert du marché français des cartes Pokémon (Cardmarket, eBay, Vinted).
Analyse cette annonce Vinted :
- Titre : {title}
- Prix : {price} €
- État : {status}

Objectif : ACHAT-REVENTE. Estime le prix auquel cette annonce se REVEND réellement
en France (prix de vente constatés sur Cardmarket et Vinted, pas les prix demandés),
en tenant compte de l'état, de l'édition, de la langue et de la demande.
Sois prudent : en cas de doute, estime bas.
Méfie-toi des faux, proxys, cartes abîmées et titres trompeurs (photo fournie si disponible).
Si ce n'est PAS un produit Pokémon TCG (autre jeu, carte de sport, figurine...), mets "suspect": true.
"score" = facilité de revente de 0 à 10 (10 = part en quelques jours).
Réponds en JSON :
{{"prix_revente": nombre en euros, "score": entier de 0 à 10,
  "suspect": true ou false, "raison": "une phrase courte"}}"""

def calcul_rentabilite(prix, prix_revente):
    cout = prix + PROTECTION_FIXE + prix * PROTECTION_PCT + LIVRAISON
    net_revente = prix_revente * (1 - FRAIS_REVENTE_PCT)
    benef = net_revente - cout
    roi = benef / cout * 100 if cout else 0
    return round(cout, 2), round(benef, 2), round(roi)

def telecharger_image(url):
    try:
        r = requests.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and len(r.content) < 4_000_000:
            return r.content, r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    except Exception:
        pass
    return None, None

def analyser(a):
    """Essaie tous les modèles, plusieurs tours. Renvoie (resultat, ok).
    ok=False seulement si TOUT a échoué -> l'annonce sera retentée au run suivant."""
    if not (client and MODELES) and not GROQ_MODELES:
        return None, False
    prompt = PROMPT.format(**a)
    contenu = [prompt]
    if ANALYSE_PHOTO and a.get("photo"):
        data, mime = telecharger_image(a["photo"])
        if data:
            contenu.append(types.Part.from_bytes(data=data, mime_type=mime))
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.2,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    gemini_ok = bool(client and MODELES)
    for tour in range(TOURS_MAX):
        for modele in (list(MODELES) if gemini_ok else []):
            try:
                rep = client.models.generate_content(model=modele, contents=contenu, config=config)
                texte = (rep.text or "").replace("```json", "").replace("```", "").strip()
                res = json.loads(texte)
                if isinstance(res, list):
                    res = res[0] if res else {}
                if modele != MODELES[0]:
                    # On garde en premier le modèle qui marche
                    MODELES.remove(modele)
                    MODELES.insert(0, modele)
                    print(f"   🔁 Bascule sur {modele}")
                return res, True
            except json.JSONDecodeError:
                print(f"   ⚠️ {modele} : réponse illisible, modèle suivant")
            except Exception as e:
                msg = str(e)
                code = msg[:3]
                if code in ("404", "400") and "API key" not in msg:
                    # Modèle inexistant ou non autorisé : on le retire pour de bon
                    print(f"   ❌ {modele} indisponible ({code}), retiré de la liste")
                    if modele in MODELES and len(MODELES) > 1:
                        MODELES.remove(modele)
                elif "API key" in msg or code in ("401", "403"):
                    print(f"   ⛔ Clé Gemini refusée : {msg[:150]}")
                    gemini_ok = False
                    break
                else:
                    print(f"   ⚠️ {modele} : {msg[:90]} -> modèle suivant")
        if GROQ_MODELES:
            res = analyser_groq(prompt)
            if isinstance(res, dict):
                return res, True
        if tour < TOURS_MAX - 1:
            attente = PAUSE_TOUR[min(tour, len(PAUSE_TOUR) - 1)]
            print(f"   ⏳ Tous les modèles ont échoué, nouvel essai dans {attente} s")
            time.sleep(attente)
    print("   ⚠️ Analyse impossible pour l'instant, annonce gardée pour le prochain run")
    return None, False

# ================= MESSAGE =================
def formater(a, source, analyse=None):
    e = html.escape
    lignes = [f"<b>🔥 {source}</b>", "",
              f"📦 <b>{e(a['title'])}</b>",
              f"💰 <b>{a['price']:.2f} €</b>"]
    if analyse:
        lignes.append(f"🧾 Coût total (protection + port) : {analyse['cout']} €")
        lignes.append(f"📈 Revente estimée : ~{analyse.get('prix_revente', '?')} €")
        lignes.append(f"💵 <b>Bénéfice net : +{analyse['benef']} € (ROI {analyse['roi']} %)</b>")
        lignes.append(f"⭐ Revendabilité : {analyse.get('score', '?')}/10")
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
    a_reessayer = 0
    ia_en_panne = False
    for a in nouvelles:
        if est_exclue(a):
            ids.append(a["id"])
            continue

        wl = match_watchlist(a)
        if wl:
            print(f"🎯 WATCHLIST '{wl[0]}' : {a['title']} — {a['price']} €")
            if envoyer_telegram(formater(a, f"WATCHLIST : {wl[0]} ≤ {wl[1]} €"), a["photo"], a["url"]):
                alertes += 1
            ids.append(a["id"])
            continue

        if not client and not GROQ_MODELES:
            ids.append(a["id"])
            continue
        if ia_en_panne:
            a_reessayer += 1  # gardée pour le prochain run
            continue
        print(f"🔍 IA : {a['title']} — {a['price']} €")
        res, ok = analyser(a)
        time.sleep(PAUSE_GEMINI)
        if not ok or not isinstance(res, dict):
            a_reessayer += 1  # pas mémorisée -> réanalysée au prochain run
            ia_en_panne = True
            print("   💤 IA indisponible : les annonces restantes seront analysées au prochain run")
            continue
        ids.append(a["id"])
        if res.get("suspect"):
            print(f"   🚫 écartée : {res.get('raison', '')}")
            continue
        try:
            score = int(res.get("score", 0))
            revente = float(res.get("prix_revente", 0))
        except (TypeError, ValueError):
            continue
        cout, benef, roi = calcul_rentabilite(a["price"], revente)
        res.update(cout=cout, benef=benef, roi=roi)
        print(f"   💶 revente ~{revente} € | bénéfice {benef} € | ROI {roi} % | score {score}")
        if score >= SCORE_ALERTE and benef >= BENEF_MIN and roi >= ROI_MIN:
            if envoyer_telegram(formater(a, "ACHAT-REVENTE RENTABLE", res), a["photo"], a["url"]):
                alertes += 1
                print("   ✅ Alerte envoyée")

    sauvegarder_historique(ids)
    if a_reessayer:
        print(f"🔁 {a_reessayer} annonce(s) seront réessayées au prochain run.")
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