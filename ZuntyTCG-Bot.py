"""
ZuntyTCG-Bot — chasseur de bonnes affaires Pokémon sur Vinted.

Règle d'or : une alerte n'est envoyée que si
  1. c'est une carte ou un produit scellé POKÉMON,
  2. son VRAI prix Cardmarket est connu,
  3. le prix Vinted est au moins DECOTE_MIN % en dessous,
  4. il reste un bénéfice net après protection acheteurs, port et commission,
  5. le vendeur est fiable (avis, note, pays où l'on peut acheter depuis Vinted France),
  6. pour un produit scellé : "scellé" est écrit et ce n'est pas une boîte remplie de vrac.

Usage :
    python ZuntyTCG-Bot.py                         -> un seul passage
    python ZuntyTCG-Bot.py --loop 120 --duree 330  -> en continu (GitHub Actions)
"""
import os
import re
import sys
import json
import time
import html
import random
import argparse
import subprocess
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


# ╔══════════════════════════════════════════════════════════════════╗
# ║                        TES RÉGLAGES                              ║
# ╚══════════════════════════════════════════════════════════════════╝

# Recherches Vinted (français en priorité)
RECHERCHES = [
    "carte pokémon",
    "carte pokemon fr",
    "carte pokémon rare",
    "display pokémon",
    "etb pokémon",
    "coffret pokémon",
    "bundle pokémon",
]

# Règle d'alerte
DECOTE_MIN = 30             # % minimum sous le prix Cardmarket (français / japonais)
ANGLAIS_DECOTE_MIN = 45     # % minimum pour une annonce en anglais
BENEF_MIN = 3.0             # bénéfice net minimum en €

# Watchlist : si le titre contient le mot ET que le prix est <= au max,
# alerte dès qu'il y a un bénéfice (même sous la décote minimale).
WATCHLIST = {
    "dracaufeu": 25,
    "charizard": 25,
    "display": 90,
    "etb": 35,
    "alternative": 30,
}

PRIX_MIN = 2.0              # en dessous : arnaques / cartes sans valeur
PRIX_MAX = 500.0            # au dessus : hors budget

# Coûts réels
PROTECTION_FIXE = 0.70      # protection acheteurs Vinted : 0,70 € + 5 % du prix
PROTECTION_PCT = 0.05
LIVRAISON_FRANCE = 3.50     # port moyen depuis un vendeur français
LIVRAISON_EUROPE = 6.50     # port moyen depuis un autre pays européen
FRAIS_REVENTE_PCT = 0.05    # commission à la revente (Cardmarket ~5 %, Vinted 0 %)

# Anti-arnaque
VENDEUR_AVIS_MIN = 2        # au moins 2 avis...
VENDEUR_NOTE_MIN = 4.0      # ...et une note moyenne d'au moins 4/5
EXIGER_SCELLE = True        # produits scellés : "scellé" doit être écrit
# Pays d'où l'on peut acheter depuis Vinted France (UE, sans douane)
PAYS_AUTORISES = {"FR", "BE", "LU", "NL", "DE", "AT", "ES", "PT", "IT", "PL", "CZ", "SK",
                  "LT", "LV", "EE", "SE", "DK", "FI", "HU", "RO", "HR", "SI", "GR", "IE"}

# Telegram
RESUME_CHAQUE_RUN = True    # bilan + podium même sans bon plan
RESUME_SILENCIEUX = True    # bilans sans son (seules les alertes sonnent)
BILAN_TOUTES_LES_MIN = 30   # en mode continu

# IA (sert à identifier les annonces, jamais à inventer un prix)
IA_PRINCIPALE = os.getenv("IA_PRINCIPALE", "groq")   # "groq" ou "gemini"
GEMINI_ACTIF = True
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

# Clés (dans les Secrets GitHub ou le fichier .env)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Technique
HISTORIQUE_FILE = "historique_vinted.json"
HISTORIQUE_MAX = 5000
TIMEOUT = (5, 15)
PARIS = ZoneInfo("Europe/Paris")


# ╔══════════════════════════════════════════════════════════════════╗
# ║                     FILTRES & VOCABULAIRE                        ║
# ╚══════════════════════════════════════════════════════════════════╝

# Tout ce qui n'est pas une carte / un produit scellé Pokémon (mots entiers)
MOTS_EXCLUS = [
    # faux & fan-made
    "proxy", "fake", "custom", "réplique", "replica", "goldée", "metal", "métal", "fan art",
    "fanart", "orica",
    # annonces de recherche
    "recherche", "cherche",
    # accessoires & goodies
    "classeur", "binder", "portfolio", "sleeve", "sleeves", "protège", "protege", "pochette",
    "toploader", "deck box", "tapis", "playmat", "peluche", "plush", "plushie", "figurine",
    "figurines", "funko", "lego", "mega construx", "puzzle", "mug", "tasse", "poster",
    "t-shirt", "tee-shirt", "sweat", "pull", "pyjama", "casquette", "chaussettes", "sac à dos",
    "trousse", "lampe", "veilleuse", "déguisement", "costume", "manga", "livre", "dvd",
    "sticker only",
    # jeux vidéo
    "jeu vidéo", "switch", "3ds", "nintendo ds", "game boy", "gameboy", "wii",
    # pêche
    "pêche", "peche", "leurre", "leurres", "moulinet", "canne à", "carpe", "hameçon", "hamecon",
    "appât", "appat", "fishing", "lure", "lures", "pêcheur", "pecheur", "silure",
    # autres jeux de cartes
    "one piece", "yu-gi-oh", "yugioh", "lorcana", "magic the gathering", "mtg", "dragon ball",
    "digimon", "naruto", "jujutsu", "demon slayer", "flesh and blood", "star wars", "marvel",
    "disney", "harry potter",
    # sport & célébrités
    "nfl", "nba", "nhl", "mlb", "panini", "topps", "upper deck", "leaf", "football",
    "baseball", "basket", "soccer", "tennis", "rugby", "hockey", "wwe", "ufc", "f1",
    "formule 1", "trump", "biden", "macron", "président", "president", "elon",
]
EXCLUS_REGEX = re.compile(
    r"(?<![a-zà-ÿ])(" + "|".join(re.escape(m) for m in MOTS_EXCLUS) + r")(?![a-zà-ÿ])", re.I)

ONE_PIECE = re.compile(r"(\bop-?\s?\d{1,2}\b|\beb-?\s?\d{1,2}\b|romance dawn|\bluffy\b)", re.I)

# Langues : FRANÇAIS, JAPONAIS et ANGLAIS uniquement
LANGUES_INTERDITES = re.compile(
    r"\b(chinese|chinois|chinoise|korean|coreen|coréen|coréenne|kr|cn|s-chinese|t-chinese|"
    r"german|allemand|allemande|deutsch|italian|italien|italienne|italiano|italiana|ita|"
    r"spanish|espagnol|espagnole|español|espanol|esp|portugues|português|portugais|"
    r"dutch|néerlandais|neerlandais|nederlands|polish|polonais|polski|thai|indonesian|"
    r"indonésien)\b", re.I)
MOTS_AUTRES_LANGUES = re.compile(
    r"\b(carta|cartas|nuovo|nuova|nuevo|nueva|novo|sigillat[oaie]|sellad[oa]s?|selad[oa]|"
    r"bustin[ae]|busta|caja|scatola|colección|coleccion|collezione|karte|karten|neu|"
    r"versiegelt|sammlung|kaart|kaarten|nieuw|karta|karty|nowe|zapakowan[ey]|rzadk[aie])\b",
    re.I)
JAPONAIS = re.compile(r"\b(jap|jp|japan|japanese|japonais|japonaise|japonaises)\b", re.I)
MOTS_FR = {"carte", "cartes", "neuf", "neuve", "scelle", "coffret", "francais", "francaise",
           "fr", "vf", "etat", "avec", "pour", "sous", "tres", "bon", "dresseur", "boite",
           "jamais", "ouvert", "de", "du", "des", "le", "la", "les", "et", "vends", "vend"}
MOTS_EN = {"card", "cards", "new", "sealed", "english", "eng", "with", "the", "of", "and",
           "mint", "brand", "box", "pack", "near", "trainer", "graded", "for", "sale",
           "unopened", "complete"}

GRADEE = re.compile(r"\b(psa|cgc|bgs|pca|egc|beckett|collect aura|grad[ée]e?s?)\b", re.I)
MOTS_SCELLES = re.compile(
    r"\b(etb|elite trainer|coffret|display|booster|bundle|collection|upc|tin|pok[eé]box|"
    r"blister|tripack|tri-pack|scell[ée]e?s?|sealed|premium|box)\b", re.I)
MOTS_SCELLE_OK = re.compile(
    r"(scell[ée]e?s?|sealed|sous blister|sous film|film[ée]e?s?|blister d'origine|"
    r"(jamais|non|pas) ouverte?s?|neuf sous|factory|d'usine)", re.I)
MOTS_OUVERT = re.compile(
    r"(\bouverte?s?\b|d[ée]ball[ée]e?s?|\bopened\b|\bempty\b|\bvide\b|sans (les )?boosters?|"
    r"box only|bo[iî]te seule|juste la bo[iî]te|sans cartes|bo[iî]te de rangement|pour ranger)",
    re.I)
MOTS_LOT = re.compile(
    r"(\b\d{2,5}\s*\+?\s*(cartes?|cards?|rares?|holos?|reverses?|communes?|brillantes?|"
    r"pok[eé]mons?)\b|\+\s*\d+\s*(cartes?|cards?)|\blot\b|\bvrac\b|\bbulk\b|\brempli|"
    r"\bavec (des |plein de )?cartes|\bclasseur|\bmystery|\bmyst[eè]re)", re.I)

DRAPEAUX = {"fr": "🇫🇷 ", "en": "🇬🇧 ", "ja": "🇯🇵 "}


# ╔══════════════════════════════════════════════════════════════════╗
# ║                            OUTILS                                ║
# ╚══════════════════════════════════════════════════════════════════╝

def sans_accents(texte):
    return unicodedata.normalize("NFKD", (texte or "").lower()).encode("ascii", "ignore").decode()

def euros(x):
    texte = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return (texte[:-3] if texte.endswith(",00") else texte) + " €"

def heure(ts=None):
    return datetime.fromtimestamp(ts or time.time(), PARIS).strftime("%H:%M")

def echapper(texte):
    return html.escape(str(texte or ""), quote=False)

def plus(dico, cle, n=1):
    dico[cle] = dico.get(cle, 0) + n


# ╔══════════════════════════════════════════════════════════════════╗
# ║                            TELEGRAM                              ║
# ╚══════════════════════════════════════════════════════════════════╝

def lien_cardmarket(recherche):
    return ("https://www.cardmarket.com/fr/Pokemon/Products/Search?searchString="
            + requests.utils.quote(recherche or ""))

def envoyer_telegram(message, photo=None, boutons=None, silencieux=False):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Identifiants Telegram manquants.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    for _ in range(4):
        if photo and len(message) <= 1024:
            methode = "sendPhoto"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": photo, "caption": message,
                       "parse_mode": "HTML"}
        else:
            methode = "sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message[:4096], "parse_mode": "HTML",
                       "disable_web_page_preview": True}
        if boutons:
            payload["reply_markup"] = {"inline_keyboard": boutons}
        if silencieux:
            payload["disable_notification"] = True
        try:
            r = requests.post(f"{base}/{methode}", json=payload, timeout=TIMEOUT)
            if r.status_code == 200:
                return True
            if r.status_code == 429:  # trop de messages : on attend et on renvoie
                attente = (r.json().get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(int(attente) + 1, 60))
                continue
            if methode == "sendPhoto":  # photo refusée : on renvoie en texte
                photo = None
                continue
            print(f"   ⚠️ Telegram {r.status_code} : {r.text[:150]}")
            return False
        except Exception as e:
            print(f"   ⚠️ Telegram : {str(e)[:80]}")
            time.sleep(3)
    return False


# ╔══════════════════════════════════════════════════════════════════╗
# ║                             VINTED                               ║
# ╚══════════════════════════════════════════════════════════════════╝

WWW = "https://www.vinted.fr"
API_VINTED = "https://api.vinted.fr/svc-catalogue/items"
ENTETES = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
SESSION = None

def nouvelle_session():
    """Récupère le jeton anonyme que Vinted donne à tout visiteur."""
    global SESSION
    s = requests.Session()
    s.headers.update(ENTETES)
    r = s.get(f"{WWW}/catalog", timeout=TIMEOUT, headers={"Accept": "text/html"})
    jeton = s.cookies.get("access_token_web")
    anon = r.headers.get("x-anon-id") or s.cookies.get("anon_id")
    csrf = re.search(r'CSRF_TOKEN\\?"\s*:\s*\\?"([^"\\]+)', r.text)
    print(f"🌐 Session Vinted : {r.status_code} | jeton : {'OK' if jeton else 'ABSENT'}")
    s.headers.update({"Accept": "application/json, text/plain, */*",
                      "Origin": WWW, "Referer": f"{WWW}/catalog"})
    if jeton:
        s.headers["Authorization"] = f"Bearer {jeton}"
    if anon:
        s.headers["X-Anon-Id"] = anon
    if csrf:
        s.headers["X-Csrf-Token"] = csrf.group(1)
    SESSION = s
    return s

# --- Devises : Vinted affiche les prix dans la devise du VISITEUR (dollars depuis les
# serveurs GitHub aux États-Unis). La devise ne dit rien du vendeur : on convertit.
TAUX_SECOURS = {"USD": 0.86, "GBP": 1.16, "PLN": 0.235, "CZK": 0.040, "SEK": 0.090,
                "DKK": 0.134, "HUF": 0.0025, "RON": 0.20, "CHF": 1.07, "CAD": 0.62}
_taux = {}

def taux_vers_euro(devise):
    if not devise or devise == "EUR":
        return 1.0
    if devise not in _taux:
        try:
            r = requests.get("https://api.frankfurter.app/latest",
                             params={"from": devise, "to": "EUR"}, timeout=TIMEOUT)
            _taux[devise] = float(r.json()["rates"]["EUR"])
        except Exception:
            _taux[devise] = TAUX_SECOURS.get(devise, 1.0)
        print(f"💱 Prix reçus en {devise} : convertis en euros (1 {devise} = {_taux[devise]:.3f} €)")
    return _taux[devise]

def _prix_brut(item):
    p = item.get("price")
    devise = None
    if isinstance(p, dict):
        devise = (p.get("currency_code") or "").upper() or None
        p = p.get("amount")
    devise = devise or (item.get("currency") or "").upper() or None
    try:
        return float(str(p).replace(",", ".")), devise
    except (TypeError, ValueError):
        return 0.0, devise

def _photo(item):
    photo = item.get("photo") or (item.get("photos") or [None])[0] or item.get("image")
    if isinstance(photo, list):
        photo = photo[0] if photo else None
    if isinstance(photo, dict):
        photo = photo.get("url") or photo.get("full_size_url") or photo.get("contentUrl")
    return photo if isinstance(photo, str) else None

def normaliser(item):
    url = item.get("url") or ""
    item_id = item.get("id")
    if not item_id:
        m = re.search(r"/items/(\d+)", url)
        item_id = m.group(1) if m else None
    if not item_id:
        return None
    prix, devise = _prix_brut(item)
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    statut = item.get("status") or item.get("item_condition") or ""
    return {
        "id": f"vinted_{item_id}",
        "title": (item.get("title") or item.get("name") or "").strip(),
        "price": round(prix * taux_vers_euro(devise), 2),
        "url": f"{WWW}/items/{item_id}",      # toujours sur vinted.fr
        "photo": _photo(item),
        "status": statut if isinstance(statut, str) else "",
        "vendeur": user.get("login", ""),
        "_user": user,
    }

def _chercher_page(s, query):
    """Secours : annonces lues dans la page catalogue (JSON-LD)."""
    r = s.get(f"{WWW}/catalog", timeout=TIMEOUT, headers={"Accept": "text/html"},
              params={"search_text": query, "order": "newest_first"})
    if r.status_code != 200:
        return []
    items = []
    for bloc in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', r.text, re.S):
        try:
            data = json.loads(bloc)
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict):
                for el in d.get("itemListElement", []):
                    items.append(el.get("item", el))
    return items

def recuperer_annonces():
    try:
        s = nouvelle_session()
    except Exception as e:
        print(f"⚠️ Vinted injoignable : {str(e)[:80]}")
        return []
    annonces, vus = [], set()
    for query in RECHERCHES:
        items = []
        for essai in range(2):
            try:
                r = s.get(API_VINTED, timeout=TIMEOUT, params={
                    "search_text": query, "order": "newest_first", "page": 1, "per_page": 30,
                    "time": int(time.time()), "currency": "EUR"})
            except Exception as e:
                print(f"   ⚠️ '{query}' : {str(e)[:60]}")
                break
            if r.status_code == 200:
                data = r.json()
                items = data.get("items") or data.get("catalog_items") or []
                break
            if r.status_code == 401 and essai == 0:     # jeton expiré : on le renouvelle
                s = nouvelle_session()
                continue
            if r.status_code in (403, 429):
                print(f"   ⛔ Vinted limite les requêtes ({r.status_code}), pause de ce passage")
                return annonces
            print(f"   ⚠️ API '{query}' -> {r.status_code}, secours page catalogue")
            try:
                items = _chercher_page(s, query)
            except Exception:
                items = []
            break
        print(f"📡 '{query}' : {len(items)} annonces")
        for item in items:
            a = normaliser(item)
            if a and a["title"] and a["id"] not in vus:
                vus.add(a["id"])
                annonces.append(a)
        time.sleep(1.2 + random.random())
    return annonces

# --- Détails d'une annonce : description + avis et pays du vendeur
_details = {}

def _nombre(motif, texte, conv=float):
    m = re.search(motif, texte)
    try:
        return conv(m.group(1)) if m else None
    except ValueError:
        return None

def details_annonce(a):
    if a["id"] in _details:
        return _details[a["id"]]
    u = a.get("_user") or {}
    d = {"description": "", "avis": u.get("feedback_count"), "rep": u.get("feedback_reputation"),
         "pays": u.get("country_iso_code") or u.get("country_code"), "lu": False}
    s = SESSION or requests.Session()
    try:
        r = s.get(a["url"], timeout=TIMEOUT, headers={"Accept": "text/html,application/xhtml+xml"})
        if r.status_code == 200:
            d["lu"] = True
            t = r.text
            m = (re.search(r'property="og:description"[^>]*content="([^"]*)"', t) or
                 re.search(r'content="([^"]*)"[^>]*property="og:description"', t))
            d["description"] = html.unescape(m.group(1)) if m else ""
            if not d["description"]:
                m = re.search(r'\\?"description\\?":\s*\\?"(.{0,1500}?)\\?"', t)
                d["description"] = m.group(1) if m else ""
            if d["avis"] is None:
                d["avis"] = _nombre(r'\\?"feedback_count\\?":\s*(\d+)', t, int)
            if d["rep"] is None:
                d["rep"] = _nombre(r'\\?"feedback_reputation\\?":\s*([\d.]+)', t)
            if not d["pays"]:
                m = re.search(r'\\?"country_iso_code\\?":\s*\\?"([A-Za-z]{2})', t)
                d["pays"] = m.group(1) if m else None
    except Exception:
        pass
    if (d["avis"] is None or not d["pays"]) and u.get("id"):
        try:
            r = s.get(f"{WWW}/api/v2/users/{u['id']}", timeout=TIMEOUT)
            if r.status_code == 200:
                uu = r.json().get("user", {})
                d["avis"] = d["avis"] if d["avis"] is not None else uu.get("feedback_count")
                d["rep"] = d["rep"] if d["rep"] is not None else uu.get("feedback_reputation")
                d["pays"] = d["pays"] or uu.get("country_iso_code")
                d["lu"] = True
        except Exception:
            pass
    rep = d["rep"]
    d["note"] = None if rep is None else (round(rep * 5, 1) if rep <= 1 else round(rep, 1))
    d["pays"] = (d["pays"] or "").upper() or None
    _details[a["id"]] = d
    return d


# ╔══════════════════════════════════════════════════════════════════╗
# ║                       FILTRES D'ANNONCES                         ║
# ╚══════════════════════════════════════════════════════════════════╝

def langue_annonce(a):
    """'ja', 'fr', 'en' ou '?' d'après le titre."""
    titre = a["title"]
    if JAPONAIS.search(titre) or re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", titre):
        return "ja"
    if re.search(r"[éèêàçùûôî]", titre.lower()):
        return "fr"
    mots = set(re.findall(r"[a-z]+", sans_accents(titre)))
    fr, en = len(mots & MOTS_FR), len(mots & MOTS_EN)
    return "fr" if fr > en else "en" if en > fr else "?"

def raison_exclusion(a):
    """Raison pour laquelle l'annonce est écartée d'office, ou None."""
    titre = a["title"]
    if EXCLUS_REGEX.search(titre) or ONE_PIECE.search(titre):
        return "hors sujet"
    if LANGUES_INTERDITES.search(titre) or MOTS_AUTRES_LANGUES.search(titre):
        return "langue"
    if not (PRIX_MIN <= a["price"] <= PRIX_MAX):
        return "prix"
    if GRADEE.search(titre):
        return "gradée (pas de prix Cardmarket)"
    return None

def facteur_etat(statut):
    """Décote selon l'état (le prix Cardmarket correspond à une carte parfaite)."""
    s = sans_accents(statut)
    if not s or "neuf" in s or "new" in s:
        return 1.0
    if "tres bon" in s or "very good" in s:
        return 0.95
    if "bon" in s or "good" in s:
        return 0.80
    if "satisfaisant" in s or "satisfactory" in s:
        return 0.50
    return 1.0

def match_watchlist(a):
    titre = a["title"].lower()
    for mot, prix_max in WATCHLIST.items():
        if re.search(r"\b" + re.escape(mot) + r"\b", titre) and a["price"] <= prix_max:
            return mot, prix_max
    return None


# ╔══════════════════════════════════════════════════════════════════╗
# ║               PRIX CARDMARKET — CARTES (TCGdex)                  ║
# ╚══════════════════════════════════════════════════════════════════╝

TCGDEX = "https://api.tcgdex.net/v2"
_cache_tcgdex = {}
MOTS_VIDES = {
    "carte", "cartes", "pokemon", "holo", "reverse", "rare", "ultra", "secret", "full", "art",
    "alternative", "alt", "promo", "neuf", "neuve", "mint", "near", "lot", "card", "cards",
    "francaise", "francais", "anglaise", "japonaise", "etat", "tbe", "tcg", "set", "the",
    "and", "les", "des", "une", "avec", "pour", "edition", "illustration", "vends", "vend",
    "brillante", "officielle", "originale", "authentique",
}

def tcgdex_get(chemin, params=None):
    cle = (chemin, tuple(sorted((params or {}).items())))
    if cle not in _cache_tcgdex:
        data = None
        try:
            r = requests.get(f"{TCGDEX}/{chemin}", params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
        except Exception:
            pass
        _cache_tcgdex[cle] = data
    return _cache_tcgdex[cle]

def extraire_numero(titre):
    m = re.search(r"\b([A-Za-z]{0,4}\d{1,3})\s*/\s*([A-Za-z]{0,4}\d{1,3})\b", titre)
    return (m.group(1), m.group(2)) if m else ("", "")

def mots_du_titre(titre):
    mots = re.findall(r"[A-Za-zÀ-ÿ\-']{3,}", titre)
    return [m for m in mots if sans_accents(m) not in MOTS_VIDES][:3]

def prix_carte(noms, numero, total, reverse=False):
    """Carte exacte (nom + numéro + total du set) -> prix tendance Cardmarket."""
    numero = (numero or "").strip()
    if not numero:
        return None
    variantes = {numero, numero.lstrip("0") or "0"}
    if numero.isdigit():
        variantes.add(numero.zfill(3))
    filtre = "eq:" + "|".join(sorted(variantes))
    chiffres = re.sub(r"\D", "", total or "")
    total_int = int(chiffres) if chiffres else None
    deja = set()
    for langue in ("fr", "en"):
        for nom in [n for n in noms if n][:4]:
            mot = sans_accents(nom)
            if (langue, mot) in deja:
                continue
            deja.add((langue, mot))
            res = tcgdex_get(f"{langue}/cards", {"name": nom, "localId": filtre})
            if not isinstance(res, list):
                continue
            for brief in res[:5]:
                nom_carte = sans_accents(brief.get("name"))
                if mot not in re.findall(r"[a-z0-9]+", nom_carte) and mot != nom_carte:
                    continue   # on veut le mot exact dans le nom de la carte
                carte = tcgdex_get(f"{langue}/cards/{brief.get('id')}")
                if not isinstance(carte, dict):
                    continue
                nb = (carte.get("set") or {}).get("cardCount") or {}
                if total_int and total_int not in (nb.get("official"), nb.get("total")):
                    continue
                cm = (carte.get("pricing") or {}).get("cardmarket") or {}
                ref = (cm.get("trend-holo") or cm.get("avg30-holo")) if reverse else None
                ref = ref or cm.get("trend") or cm.get("avg30") or cm.get("avg")
                if not ref:
                    continue
                en = tcgdex_get(f"en/cards/{carte.get('id')}") if langue == "fr" else carte
                return {"prix": round(float(ref), 2), "nom": carte.get("name"),
                        "nom_en": (en or {}).get("name") or carte.get("name"),
                        "set": (carte.get("set") or {}).get("name", ""), "scelle": False}
    return None


# ╔══════════════════════════════════════════════════════════════════╗
# ║        PRIX CARDMARKET — PRODUITS SCELLÉS (guide officiel)       ║
# ╚══════════════════════════════════════════════════════════════════╝

CM_CATALOGUE = "https://downloads.s3.cardmarket.com/productCatalog"
ABREVIATIONS = [
    (r"\bcoffret dresseur d'?\s?elite\b", "elite trainer box"),
    (r"\betb\b", "elite trainer box"),
    (r"\bdisplay\b", "booster box"),
    (r"\bupc\b", "ultra premium collection"),
    (r"\bcoffret ultra premium\b", "ultra premium collection"),
    (r"\bcoffret premium\b", "premium collection"),
    (r"\btri-?pack\b", "3 pack blister"),
    (r"\bmini pokebox\b", "mini tin"),
    (r"\bpokebox\b", "tin"),
]
MOTS_VIDES_EN = {"pokemon", "tcg", "scelle", "scellee", "sealed", "neuf", "neuve", "new", "fr",
                 "en", "eng", "francais", "francaise", "anglais", "english", "french", "the",
                 "de", "la", "le", "et", "a", "vendre", "pour", "avec", "of", "and", "edition",
                 "version", "set", "jap", "jp", "japonais", "japonaise"}
MOTS_TYPE = {"booster", "box", "elite", "trainer", "bundle", "collection", "premium", "ultra",
             "pack", "blister", "tin", "mini", "deck", "starter", "double", "case", "3",
             "build", "battle", "display", "sleeved", "half"}
MOTS_SERIE = {"scarlet", "violet", "sword", "shield", "sun", "moon", "black", "white", "xy",
              "sv", "swsh", "sm", "mega", "evolution"}
LANGUES_PRODUIT = {"japanese": "ja", "korean": "ko", "chinese": "zh", "thai": "th",
                   "indonesian": "id", "german": "de", "french": "fr", "italian": "it",
                   "spanish": "es", "portuguese": "pt"}
_catalogue = None
_sets_fr = None

def jetons(texte):
    t = sans_accents(texte)
    for motif, remplacement in ABREVIATIONS:
        t = re.sub(motif, remplacement, t)
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in MOTS_VIDES_EN]

def sets_en_vers_fr():
    """Noms d'extensions anglais -> français (via TCGdex)."""
    global _sets_fr
    if _sets_fr is None:
        _sets_fr = {}
        fr = {x.get("id"): x.get("name") for x in (tcgdex_get("fr/sets") or []) if isinstance(x, dict)}
        for x in tcgdex_get("en/sets") or []:
            if isinstance(x, dict) and x.get("name") and fr.get(x.get("id")):
                _sets_fr[x["name"].lower()] = fr[x["id"]]
    return _sets_fr

def fr_vers_en(texte):
    """Remplace les noms d'extensions français par les noms anglais de Cardmarket."""
    t = sans_accents(texte)
    for en, fr in sorted(sets_en_vers_fr().items(), key=lambda x: -len(x[1])):
        f = sans_accents(fr)
        if len(f) > 3 and f in t:
            t = t.replace(f, en)
    return t

def charger_catalogue():
    """Guide de prix Cardmarket des produits scellés Pokémon (1 fois par session)."""
    global _catalogue
    if _catalogue is not None:
        return _catalogue
    _catalogue = []
    try:
        produits = requests.get(f"{CM_CATALOGUE}/productList/products_nonsingles_6.json",
                                timeout=(5, 60)).json().get("products", [])
        guide = requests.get(f"{CM_CATALOGUE}/priceGuide/price_guide_6.json",
                             timeout=(5, 90)).json().get("priceGuides", [])
        prix = {g.get("idProduct"): g for g in guide}
        for p in produits:
            g = prix.get(p.get("idProduct"))
            ref = g and (g.get("trend") or g.get("avg30") or g.get("avg"))
            if ref and p.get("name"):
                toks = set(jetons(p["name"]))
                langue = next((v for k, v in LANGUES_PRODUIT.items() if k in toks), "")
                _catalogue.append((toks, p["name"], float(ref), langue))
        print(f"   📦 Guide de prix Cardmarket chargé : {len(_catalogue)} produits scellés")
    except Exception as e:
        print(f"   ⚠️ Guide de prix Cardmarket indisponible : {str(e)[:80]}")
    return _catalogue

def prix_scelle(requete, langue_annonce_):
    """Produit scellé Cardmarket correspondant : tous ses mots de type (display, ETB...)
    et la majorité de ses mots d'extension doivent être dans la requête."""
    if not requete:
        return None
    q = set(jetons(fr_vers_en(requete)))
    langue_voulue = "ja" if langue_annonce_ == "ja" else ""
    meilleur, score_max = None, 0
    for toks, nom, ref, langue in charger_catalogue():
        if langue != langue_voulue and not (langue == "fr" and langue_voulue == ""):
            continue   # un display japonais n'a pas le prix d'un display français
        types_p = toks & MOTS_TYPE
        set_p = toks - MOTS_TYPE - MOTS_SERIE - set(LANGUES_PRODUIT)
        requis = types_p - {"booster"} if "bundle" in types_p else types_p
        if not types_p or not set_p or not requis <= q:
            continue
        communs = set_p & q
        if not communs or len(communs) / len(set_p) < 0.6:
            continue
        score = len(communs) + len(types_p) - 0.1 * len(set_p - q)
        if score > score_max:
            meilleur, score_max = (nom, ref), score
    if not meilleur:
        return None
    return {"prix": round(meilleur[1], 2), "nom": traduire_produit(meilleur[0]),
            "nom_en": meilleur[0], "set": "", "scelle": True}

TYPES_FR = [
    ("ultra premium collection", "Coffret Ultra Premium"),
    ("elite trainer box case", "Carton de Coffrets Dresseur d'Élite"),
    ("elite trainer box", "Coffret Dresseur d'Élite (ETB)"),
    ("booster box case", "Carton de Displays"),
    ("booster box", "Display"),
    ("booster bundle", "Bundle de boosters"),
    ("build & battle box", "Kit Avant-Première"),
    ("premium collection", "Coffret Premium"),
    ("3 pack blister", "Tripack"),
    ("mini tin", "Mini Pokébox"),
    ("collection box", "Coffret"),
    ("collection", "Coffret"),
    ("blister", "Blister"),
    ("booster", "Booster"),
    ("tin", "Pokébox"),
]

def traduire_produit(nom):
    """'Prismatic Evolutions Booster Box' -> 'Display Évolutions Prismatiques'."""
    nom = re.sub(r"^(scarlet & violet|sword & shield|sun & moon|mega evolution|xy|"
                 r"black & white)\s*[:\-–]\s*", "", nom, flags=re.I)
    bas = nom.lower()
    type_fr, reste = "", nom
    for en, fr in TYPES_FR:
        m = re.search(r"\b" + re.escape(en) + r"\b", bas)
        if m:
            type_fr = fr
            reste = (nom[:m.start()] + nom[m.end():]).strip(" -:")
            break
    for en, fr in sorted(sets_en_vers_fr().items(), key=lambda x: -len(x[0])):
        i = reste.lower().find(en)
        if en and i >= 0:
            reste = reste[:i] + fr + reste[i + len(en):]
            break
    return f"{type_fr} {reste}".strip() if type_fr else reste


# ╔══════════════════════════════════════════════════════════════════╗
# ║           IA : IDENTIFIER L'ANNONCE (Groq puis Gemini)           ║
# ╚══════════════════════════════════════════════════════════════════╝

class IA:
    """Identifie une annonce (Pokémon ? faux ? quelle carte / quel produit ?).
    Essaie tous les modèles Groq puis Gemini, et ne donne JAMAIS de prix."""
    GROQ_URL = "https://api.groq.com/openai/v1"
    GROQ_PREFERES = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                     "llama-3.1-8b-instant"]
    GROQ_EXCLUS = ("whisper", "tts", "guard", "playai", "orpheus", "safeguard", "compound")
    GEMINI_PREFERES = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                       "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest",
                       "gemini-flash-lite-latest"]
    GEMINI_EXCLUS = ("tts", "image", "embedding", "live", "audio", "veo", "imagen", "banana",
                     "aqa", "robotics", "computer-use", "native", "lyria", "transcribe")

    def __init__(self):
        self.groq_init = self._modeles_groq()
        self.client = None
        self.gemini_init = []
        if GEMINI_ACTIF and GEMINI_API_KEY and genai:
            try:
                self.client = genai.Client(api_key=GEMINI_API_KEY,
                                           http_options=genai_types.HttpOptions(timeout=45000))
                self.gemini_init = self._modeles_gemini()
            except Exception as e:
                print(f"⚠️ Gemini indisponible : {str(e)[:80]}")
        self.reinitialiser()
        print(f"🦙 Groq : {len(self.groq_init)} modèles | 🤖 Gemini : {len(self.gemini_init)} modèles"
              f" | IA principale : {IA_PRINCIPALE}")

    def reinitialiser(self):
        """Recharge les quotas (appelé toutes les 30 min)."""
        self.groq = list(self.groq_init)
        self.gemini = list(self.gemini_init)
        self.en_panne = False

    @property
    def dispo(self):
        return bool(self.groq or self.gemini)

    def _modeles_groq(self):
        if not GROQ_API_KEY:
            return []
        try:
            r = requests.get(f"{self.GROQ_URL}/models", timeout=TIMEOUT,
                             headers={"Authorization": f"Bearer {GROQ_API_KEY}"})
            dispo = [m["id"] for m in r.json().get("data", [])
                     if m.get("active", True) and not any(x in m["id"] for x in self.GROQ_EXCLUS)]
            if dispo:
                return [m for m in self.GROQ_PREFERES if m in dispo] + sorted(
                    m for m in dispo if m not in self.GROQ_PREFERES)
        except Exception:
            pass
        return list(self.GROQ_PREFERES)

    def _modeles_gemini(self):
        ordre = [GEMINI_MODEL] + [m for m in self.GEMINI_PREFERES if m != GEMINI_MODEL]
        try:
            dispo = []
            for m in self.client.models.list():
                nom = (m.name or "").replace("models/", "")
                actions = getattr(m, "supported_actions", None) or []
                if "gemini" in nom and "flash" in nom and not any(x in nom for x in self.GEMINI_EXCLUS) \
                        and (not actions or "generateContent" in actions):
                    dispo.append(nom)
            if dispo:
                ordre = [m for m in ordre if m in dispo] + sorted(m for m in dispo if m not in ordre)
        except Exception:
            pass
        return list(dict.fromkeys(ordre))

    @staticmethod
    def prompt(a):
        return (
            f"Nous sommes le {time.strftime('%d/%m/%Y')}. Tu es expert des cartes Pokémon (TCG).\n"
            f"Annonce Vinted :\n- Titre : {a['title']}\n- Prix : {a['price']} €\n"
            f"- État : {a.get('status') or 'inconnu'}\n\n"
            "Tes connaissances s'arrêtent avant aujourd'hui : de nouvelles séries sortent en "
            "permanence (Méga-Évolution, 30e anniversaire en 2026...). Ne juge JAMAIS un produit "
            "faux simplement parce que tu ne le connais pas.\n\n"
            "Réponds uniquement en JSON avec ces clés :\n"
            '"pokemon": true si c\'est une carte ou un produit scellé Pokémon TCG, false sinon '
            "(autre jeu, sport, jouet, peluche, figurine, vêtement, pêche...) ;\n"
            '"faux": true UNIQUEMENT si signes clairs de contrefaçon (proxy, fan art, custom, '
            "carte en métal/dorée non officielle) ;\n"
            '"type": "carte", "scelle", "lot" ou "autre" ;\n'
            '"nom_fr": nom français de la carte ou du produit ;\n'
            '"nom_en": nom anglais (pour un produit scellé : extension + type, '
            'ex "Prismatic Evolutions Booster Box") ;\n'
            '"numero": numéro de la carte (ex "102", vide si inconnu) ;\n'
            '"total": total du set (ex "128", vide si inconnu) ;\n'
            '"raison": une phrase courte en français.'
        )

    @staticmethod
    def _json(texte):
        texte = (texte or "").replace("```json", "").replace("```", "").strip()
        m = re.search(r"\{.*\}", texte, re.S)
        res = json.loads(m.group(0) if m else texte)
        return res[0] if isinstance(res, list) and res else res

    def _groq(self, prompt):
        for modele in list(self.groq):
            try:
                r = requests.post(f"{self.GROQ_URL}/chat/completions", timeout=(5, 40),
                                  headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                                  json={"model": modele, "temperature": 0.1,
                                        "response_format": {"type": "json_object"},
                                        "messages": [{"role": "user", "content": prompt}]})
                if r.status_code == 200:
                    return self._json(r.json()["choices"][0]["message"]["content"])
                if r.status_code in (400, 404, 429) and modele in self.groq:
                    self.groq.remove(modele)   # indisponible ou quota atteint jusqu'au bilan
                    print(f"   🪫 Groq {modele} : {r.status_code}, mis de côté")
            except Exception as e:
                print(f"   ⚠️ Groq {modele} : {str(e)[:60]}")
        return None

    def _gemini(self, prompt, photo):
        if not self.client:
            return None
        contenu = [prompt]
        if photo:
            try:
                r = requests.get(photo, timeout=TIMEOUT)
                if r.status_code == 200 and len(r.content) < 4_000_000:
                    contenu.append(genai_types.Part.from_bytes(
                        data=r.content, mime_type=r.headers.get("Content-Type", "image/jpeg").split(";")[0]))
            except Exception:
                pass
        config = genai_types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0.1,
            automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True))
        for modele in list(self.gemini)[:4]:
            try:
                rep = self.client.models.generate_content(model=modele, contents=contenu, config=config)
                return self._json(rep.text)
            except Exception as e:
                code = str(e)[:3]
                if code in ("400", "404", "429", "401", "403") and modele in self.gemini:
                    self.gemini.remove(modele)
                    print(f"   🪫 Gemini {modele} : {code}, mis de côté")
                elif modele in self.gemini:
                    self.gemini.remove(modele)
                    self.gemini.append(modele)   # surchargé : en fin de liste
        return None

    def identifier(self, a):
        """Renvoie (résultat, ok)."""
        if self.en_panne or not self.dispo:
            return None, False
        prompt = self.prompt(a)
        ordre = ["groq", "gemini"] if IA_PRINCIPALE == "groq" else ["gemini", "groq"]
        for tour in range(2):
            for fournisseur in ordre:
                try:
                    res = self._groq(prompt) if fournisseur == "groq" else self._gemini(prompt, a.get("photo"))
                except Exception:
                    res = None
                if isinstance(res, dict):
                    time.sleep(1.5)   # respecte les limites par minute
                    return res, True
            if tour == 0 and self.dispo:
                time.sleep(5)
        self.en_panne = True
        print("   💤 IA indisponible : les annonces à identifier attendront le prochain passage")
        return None, False


# ╔══════════════════════════════════════════════════════════════════╗
# ║                   RENTABILITÉ & ANTI-ARNAQUE                     ║
# ╚══════════════════════════════════════════════════════════════════╝

def rentabilite(prix, ref, livraison):
    cout = prix + PROTECTION_FIXE + prix * PROTECTION_PCT + livraison
    benef = ref * (1 - FRAIS_REVENTE_PCT) - cout
    roi = benef / cout * 100 if cout else 0
    return round(cout, 2), round(benef, 2), round(roi)

def verifier(a, infos, exiger_rentable=True):
    """Vérifie vendeur, pays, scellé et rentabilité réelle. Renvoie (ok, raison)."""
    d = details_annonce(a)
    infos["details"] = d
    texte = f"{a['title']} {d['description']}"
    if infos["scelle"] and EXIGER_SCELLE:
        if MOTS_OUVERT.search(re.sub(r"(jamais|non|pas) ouverte?s?", "", texte, flags=re.I)):
            return False, "produit ouvert ou boîte vide"
        if MOTS_LOT.search(texte):
            return False, "boîte remplie de vrac"
        if not MOTS_SCELLE_OK.search(texte):
            return False, "« scellé » non indiqué"
    if d["avis"] is None or d["note"] is None:
        return False, "avis du vendeur illisibles"
    if d["avis"] < VENDEUR_AVIS_MIN:
        return False, f"vendeur avec moins de {VENDEUR_AVIS_MIN} avis"
    if d["note"] < VENDEUR_NOTE_MIN:
        return False, f"vendeur noté sous {VENDEUR_NOTE_MIN:g}/5"
    if d["pays"] and d["pays"] not in PAYS_AUTORISES:
        return False, "pays où on ne peut pas acheter"
    if not d["pays"] and infos["langue"] != "fr":
        return False, "pays du vendeur inconnu"
    # Port réel : plus cher si le vendeur n'est pas en France
    if d["pays"] and d["pays"] != "FR":
        cout, benef, roi = rentabilite(a["price"], infos["ref"], LIVRAISON_EUROPE)
        infos.update(cout=cout, benef=benef, roi=roi, port="Europe")
        if exiger_rentable and benef < BENEF_MIN:
            return False, "plus rentable avec le port depuis l'étranger"
    return True, ""


# ╔══════════════════════════════════════════════════════════════════╗
# ║                            MESSAGES                              ║
# ╚══════════════════════════════════════════════════════════════════╝

SEP = "━━━━━━━━━━━━━━━"

def jauge(decote):
    pleines = max(0, min(10, round(decote / 10)))
    couleur = "🟩" if decote >= DECOTE_MIN else ("🟨" if decote >= 10 else "🟥")
    return couleur * pleines + "⬜" * (10 - pleines)

def niveau(decote, benef):
    if decote >= 50 and benef >= 20:
        return "💎 PÉPITE"
    if decote >= 40:
        return "🚀 GROS COUP"
    return "🔥 BON PLAN"

def ligne_vendeur(a, d):
    morceaux = []
    if a.get("vendeur"):
        morceaux.append(f"👤 {echapper(a['vendeur'])}")
    if d and d.get("note") is not None:
        morceaux.append(f"⭐ {str(d['note']).replace('.', ',')}/5 ({d['avis']} avis)")
    if d and d.get("pays"):
        morceaux.append(f"📍 {d['pays']}")
    return " · ".join(morceaux)

def boutons(a, infos):
    return [[{"text": "🛒 Voir sur Vinted", "url": a["url"]},
             {"text": "📊 Cardmarket", "url": lien_cardmarket(infos["recherche"])}]]

def message_alerte(a, titre, infos):
    e = echapper
    signe_b = "+" if infos["benef"] >= 0 else ""
    lignes = [
        f"<b>{titre}</b>", SEP,
        f"📦 {DRAPEAUX.get(infos['langue'], '')}<b>{e(a['title'][:90])}</b>",
        f"💰 Vinted <b>{euros(a['price'])}</b>  ➜  📊 Cardmarket <b>{euros(infos['ref'])}</b>",
        f"{jauge(infos['decote'])} <b>-{infos['decote']} %</b>",
        f"💵 <b>Bénéfice net : {signe_b}{euros(infos['benef'])}</b>  (rentabilité {infos['roi']} %)",
        f"🧾 Coût réel (protection + port {'Europe' if infos.get('port') == 'Europe' else 'France'})"
        f" : {euros(infos['cout'])}",
        f"🃏 {e(infos['carte'][:80])}",
    ]
    if infos.get("facteur", 1) < 1:
        lignes.append(f"🩹 État « {e(a['status'])} » : prix Cardmarket ajusté "
                      f"(-{round((1 - infos['facteur']) * 100)} %)")
    elif a.get("status"):
        lignes.append(f"🏷️ {e(a['status'])}")
    vendeur = ligne_vendeur(a, infos.get("details"))
    if vendeur:
        lignes.append(vendeur)
    lignes.append("🛡️ Vendeur vérifié" + (" · Scellé confirmé ✅" if infos["scelle"] else ""))
    lignes += [SEP, "⚡ Vérifie Cardmarket en 1 clic et fonce 👇"]
    return "\n".join(lignes)

def message_bilan(st, top, titre_periode):
    e = echapper
    etat = f"✅ {st.alertes} bon(s) plan(s) envoyé(s)" if st.alertes else "pas de bon plan"
    lignes = [f"📋 <b>{titre_periode}</b> · {etat}",
              f"🆕 {st.nouvelles} nouvelles · 📊 {st.comparees} comparées à Cardmarket"
              + (f" · ⏳ {st.attente} en attente" if st.attente else "")]
    if st.filtres:
        lignes.append("🔎 Filtrées : " + " · ".join(
            f"{k} {v}" for k, v in sorted(st.filtres.items(), key=lambda x: -x[1])))
    if st.refus:
        lignes.append("🛡️ Bloquées : " + " · ".join(
            f"{k} {v}" for k, v in sorted(st.refus.items(), key=lambda x: -x[1])))
    # Alarmes automatiques
    if st.recues == 0:
        lignes.append("⚠️ <b>Anomalie</b> : Vinted n'a renvoyé aucune annonce sur la période.")
    elif st.nouvelles >= 20 and st.comparees == 0 and st.filtres:
        pire = max(st.filtres.items(), key=lambda x: x[1])
        lignes.append(f"⚠️ <b>Anomalie</b> : aucune annonce comparée, « {pire[0]} » en bloque "
                      f"{round(pire[1] * 100 / st.nouvelles)} %.")
    if st.refus.get("avis du vendeur illisibles", 0) >= 3 and not st.alertes:
        lignes.append("⚠️ <b>Anomalie</b> : les fiches vendeurs sont illisibles, "
                      "les alertes sont bloquées par sécurité.")
    if top:
        lignes += [SEP, "🏆 <b>PODIUM</b>"]
        for medaille, (a, inf) in zip(["🥇", "🥈", "🥉"], top):
            signe = "-" if inf["decote"] >= 0 else "+"
            b = f"{'+' if inf['benef'] >= 0 else ''}{euros(inf['benef'])}"
            lignes.append(f"{medaille} {DRAPEAUX.get(inf['langue'], '')}<b>{e(a['title'][:45])}</b>")
            lignes.append(f"      {euros(a['price'])} au lieu de {euros(inf['ref'])} · "
                          f"{signe}{abs(inf['decote'])} % · {b}")
        a, inf = top[0]
        manque = []
        seuil = ANGLAIS_DECOTE_MIN if inf["langue"] == "en" else DECOTE_MIN
        if inf["decote"] < seuil:
            manque.append(f"{seuil - inf['decote']} pts de décote")
        if inf["benef"] < BENEF_MIN:
            manque.append(f"{euros(BENEF_MIN - inf['benef'])} de bénéfice")
        if manque:
            lignes.append(f"📏 Il manque {' et '.join(manque)} au 🥇 pour une alerte")
    else:
        lignes += [SEP, "😴 Aucune annonce comparable et fiable sur cette période."]
    lignes += [SEP, f"🎯 Tes seuils : -{DECOTE_MIN} % (🇬🇧 -{ANGLAIS_DECOTE_MIN} %) et "
                    f"+{euros(BENEF_MIN)} de bénéfice"]
    return "\n".join(lignes)


# ╔══════════════════════════════════════════════════════════════════╗
# ║                     HISTORIQUE & STATISTIQUES                    ║
# ╚══════════════════════════════════════════════════════════════════╝

class Historique:
    def __init__(self):
        try:
            with open(HISTORIQUE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.ids = [d["id"] if isinstance(d, dict) else d for d in data]
        except Exception:
            self.ids = []
        self.set = set(self.ids)

    def __contains__(self, i):
        return i in self.set

    def ajouter(self, i):
        if i not in self.set:
            self.ids.append(i)
            self.set.add(i)

    def sauver(self):
        self.ids = self.ids[-HISTORIQUE_MAX:]
        self.set = set(self.ids)
        with open(HISTORIQUE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.ids, f, indent=0)

class Stats:
    def __init__(self):
        self.recues = self.nouvelles = self.comparees = self.attente = self.alertes = 0
        self.filtres, self.refus = {}, {}
        self.candidats = []

    def fusion(self, o):
        self.recues += o.recues
        self.nouvelles += o.nouvelles
        self.comparees += o.comparees
        self.alertes += o.alertes
        self.attente = o.attente
        for k, v in o.filtres.items():
            plus(self.filtres, k, v)
        for k, v in o.refus.items():
            plus(self.refus, k, v)
        self.candidats += o.candidats


# ╔══════════════════════════════════════════════════════════════════╗
# ║                        TRAITEMENT D'ANNONCE                      ║
# ╚══════════════════════════════════════════════════════════════════╝

TENTATIVES = {}   # annonces que l'IA n'a pas encore pu identifier

def trouver_prix(a, langue, ia, st):
    """Renvoie (prix Cardmarket, statut) — statut : 'ok', 'attente', ou une raison."""
    titre = a["title"]
    reverse = "reverse" in titre.lower()
    num, total = extraire_numero(titre)

    # 1) Carte avec numéro -> TCGdex, sans IA (pas pour le japonais : numérotation différente)
    if num and langue != "ja":
        cm = prix_carte(mots_du_titre(titre), num, total, reverse)
        if cm:
            print("   ⚡ Carte trouvée sur Cardmarket sans IA")
            return cm, "ok"
    # 2) Produit scellé -> guide officiel Cardmarket, sans IA
    if MOTS_SCELLES.search(titre):
        cm = prix_scelle(titre, langue)
        if cm:
            print(f"   ⚡ Produit trouvé sur Cardmarket sans IA : {cm['nom_en']}")
            return cm, "ok"
    # 3) Sinon l'IA identifie, puis on cherche le VRAI prix Cardmarket
    if not (ia.groq_init or ia.gemini_init):      # aucune IA configurée
        return None, "pas de prix Cardmarket"
    res, ok = ia.identifier(a)
    if not ok:
        return None, "attente"
    if res.get("pokemon") is not True:
        return None, "pas du Pokémon (IA)"
    if res.get("faux"):
        return None, "contrefaçon (IA)"
    typ = res.get("type")
    if typ == "carte" and res.get("numero") and langue != "ja":
        noms = [res.get("nom_fr"), res.get("nom_en")] + mots_du_titre(titre)
        cm = prix_carte(noms, str(res.get("numero")), str(res.get("total") or total), reverse)
        if cm:
            return cm, "ok"
    if typ in ("scelle", "lot"):
        for requete in (res.get("nom_en"), res.get("nom_fr")):
            cm = prix_scelle(requete or "", langue)
            if cm:
                return cm, "ok"
    return None, "pas de prix Cardmarket"

def traiter(a, ia, st, histo):
    raison = raison_exclusion(a)
    if raison:
        plus(st.filtres, raison)
        histo.ajouter(a["id"])
        return

    langue = langue_annonce(a)
    print(f"🔍 {DRAPEAUX.get(langue, '')}{a['title'][:70]} — {euros(a['price'])}")
    cm, statut = trouver_prix(a, langue, ia, st)
    if statut == "attente":
        plus(TENTATIVES, a["id"])
        if TENTATIVES[a["id"]] >= 3:
            plus(st.filtres, "non identifiable")
            histo.ajouter(a["id"])
        else:
            st.attente += 1
        return
    histo.ajouter(a["id"])
    if not cm:
        plus(st.filtres, statut)
        print(f"   ⏭️ {statut}")
        return
    if cm["scelle"] and MOTS_LOT.search(a["title"]):
        plus(st.filtres, "boîte remplie de vrac")
        print("   🚫 boîte remplie de cartes en vrac")
        return

    facteur = 1.0 if cm["scelle"] else facteur_etat(a.get("status"))
    ref = round(cm["prix"] * facteur, 2)
    decote = round((1 - a["price"] / ref) * 100) if ref > 0 else -999
    cout, benef, roi = rentabilite(a["price"], ref, LIVRAISON_FRANCE)
    carte = f"{cm['nom']} – {cm['set']}" if cm.get("set") else cm["nom"]
    infos = {"ref": ref, "decote": decote, "cout": cout, "benef": benef, "roi": roi,
             "carte": carte or "", "scelle": cm["scelle"], "langue": langue, "facteur": facteur,
             "recherche": cm.get("nom_en") or cm["nom"]}
    st.comparees += 1
    ecart = f"-{decote} %" if decote >= 0 else f"+{-decote} % plus cher"
    print(f"   📊 Cardmarket {euros(ref)} | {ecart} | bénéfice {euros(benef)}")
    if decote < -100:
        return
    st.candidats.append((a, infos))

    seuil = ANGLAIS_DECOTE_MIN if langue == "en" else DECOTE_MIN
    wl = match_watchlist(a)
    if decote >= seuil and benef >= BENEF_MIN:
        titre = f"{niveau(decote, benef)} · -{decote} % SOUS CARDMARKET"
    elif wl and benef >= BENEF_MIN and decote > 0:
        titre = f"🎯 WATCHLIST « {wl[0]} » · -{decote} % SOUS CARDMARKET"
    else:
        return

    ok, refus = verifier(a, infos)
    if not ok:
        plus(st.refus, refus)
        st.candidats.remove((a, infos))
        print(f"   🛡️ Alerte bloquée : {refus}")
        return
    if envoyer_telegram(message_alerte(a, titre, infos), a.get("photo"), boutons(a, infos)):
        st.alertes += 1
        print("   ✅ ALERTE ENVOYÉE")

def cycle(ia, histo):
    st = Stats()
    annonces = recuperer_annonces()
    st.recues = len(annonces)
    nouvelles = [a for a in annonces if a["id"] not in histo]
    st.nouvelles = len(nouvelles)
    print(f"🆕 {len(nouvelles)} nouvelles annonces sur {len(annonces)}")

    if not histo.ids and nouvelles:
        print("ℹ️ Premier lancement : annonces mémorisées sans alerte.")
        for a in nouvelles:
            histo.ajouter(a["id"])
        histo.sauver()
        return st

    ia.en_panne = False
    for a in nouvelles:
        try:
            traiter(a, ia, st, histo)
        except Exception as e:   # une annonce bizarre ne doit jamais bloquer les autres
            print(f"   💥 Erreur sur « {a.get('title', '')[:40]} » : {str(e)[:80]}")
            plus(st.filtres, "erreur")
            histo.ajouter(a["id"])
    histo.sauver()
    if st.filtres:
        print("🔎 Filtrées : " + " · ".join(f"{k} {v}" for k, v in st.filtres.items()))
    if st.refus:
        print("🛡️ Bloquées : " + " · ".join(f"{k} {v}" for k, v in st.refus.items()))
    print(f"✨ Passage terminé : {st.alertes} alerte(s)")
    return st


# ╔══════════════════════════════════════════════════════════════════╗
# ║                        BILAN & SAUVEGARDE                        ║
# ╚══════════════════════════════════════════════════════════════════╝

def envoyer_bilan(st, titre_periode):
    top = []
    tries = sorted(st.candidats, key=lambda c: (c[1]["langue"] != "en", c[1]["benef"],
                                                c[1]["decote"]), reverse=True)
    for a, inf in tries[:8]:           # podium : uniquement des annonces fiables
        ok, refus = verifier(a, inf, exiger_rentable=False)
        if ok:
            top.append((a, inf))
            if len(top) == 3:
                break
    message = message_bilan(st, top, titre_periode)
    if top:
        a1, i1 = top[0]
        lignes_boutons = boutons(a1, i1)
        autres = [{"text": f"{m} Vinted", "url": a["url"]}
                  for m, (a, _) in zip(["🥈", "🥉"], top[1:])]
        if autres:
            lignes_boutons.append(autres)
        envoyer_telegram(message, a1.get("photo"), lignes_boutons, silencieux=RESUME_SILENCIEUX)
    else:
        envoyer_telegram(message, silencieux=RESUME_SILENCIEUX)
    print("   📨 Bilan envoyé sur Telegram")

def sauvegarde_github():
    """Sur GitHub Actions : enregistre l'historique dans le dépôt (anti-doublons)."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    for c in (["git", "config", "user.name", "zuntytcg-bot"],
              ["git", "config", "user.email", "bot@users.noreply.github.com"],
              ["git", "add", HISTORIQUE_FILE],
              ["git", "commit", "-m", "maj historique [skip ci]"],
              ["git", "pull", "--rebase", "--autostash"],
              ["git", "push"]):
        try:
            subprocess.run(c, capture_output=True, timeout=60)
        except Exception:
            pass
    print("   💾 Historique sauvegardé sur GitHub")


# ╔══════════════════════════════════════════════════════════════════╗
# ║                              MAIN                                ║
# ╚══════════════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0, help="secondes entre deux passages")
    parser.add_argument("--duree", type=int, default=0, help="durée max en minutes (0 = sans fin)")
    args = parser.parse_args()

    print("🚀 ZuntyTCG-Bot démarré")
    ia = IA()
    histo = Historique()

    if not args.loop:
        st = cycle(ia, histo)
        if RESUME_CHAQUE_RUN:
            envoyer_bilan(st, f"PASSAGE DE {heure()}")
        return

    fin = time.time() + args.duree * 60 if args.duree else None
    envoyer_telegram(
        "🟢 <b>ZuntyTCG-Bot en ligne</b>\n"
        f"Vinted surveillé toutes les ~{max(1, args.loop // 60)} min"
        + (f" pendant {args.duree // 60} h {args.duree % 60:02d}" if args.duree else "")
        + f".\nAlertes en direct 🔥 · bilan toutes les {BILAN_TOUTES_LES_MIN} min 📋",
        silencieux=RESUME_SILENCIEUX)
    periode, debut_periode, premier = Stats(), time.time(), True
    while True:
        try:
            periode.fusion(cycle(ia, histo))
        except Exception as e:
            print(f"💥 Erreur inattendue : {e}")
        maintenant = time.time()
        if premier or maintenant - debut_periode >= BILAN_TOUTES_LES_MIN * 60:
            try:
                if RESUME_CHAQUE_RUN:
                    envoyer_bilan(periode, f"BILAN {heure(debut_periode)} → {heure(maintenant)}")
                sauvegarde_github()
            except Exception as e:
                print(f"💥 Erreur bilan : {e}")
            ia.reinitialiser()      # quotas IA rechargés
            _details.clear()
            periode, debut_periode, premier = Stats(), maintenant, False
        attente = args.loop + random.randint(0, 20)
        if fin and maintenant + attente >= fin:
            print("🏁 Fin de session : la relance automatique prend le relais")
            break
        print(f"⏳ Prochain passage dans {attente} s\n")
        time.sleep(attente)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("👋 Arrêt demandé")