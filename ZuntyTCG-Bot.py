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
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
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
# IA utilisée en premier : "groq" (gratuit, gros quota) ou "gemini"
IA_PRINCIPALE = os.getenv("IA_PRINCIPALE", "groq")
GEMINI_ACTIF = True     # mets False pour ne plus du tout utiliser Gemini
TOURS_MAX = 3           # nombre de tours complets sur tous les modèles
GEMINI_ESSAIS_MAX = 4   # modèles Gemini essayés avant de passer à Groq
PAUSE_TOUR = [5, 20, 45]  # attente (s) entre deux tours

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

# ===== ONE PIECE : uniquement les affaires EXTRÊMES, avec prix Cardmarket obligatoire =====
ONE_PIECE_ACTIF = True
RECHERCHES_ONE_PIECE = ["display one piece", "booster box one piece", "one piece card game scellé"]
OP_DECOTE_MIN = 50      # au moins -50 % sous Cardmarket
OP_BENEF_MIN = 25.0     # et au moins +25 € de bénéfice net

# Cartes françaises ET anglaises uniquement : écarte les annonces qui précisent
# une autre langue (japonais, chinois, coréen, allemand...)
CARTES_FR_EN_SEULEMENT = True
LANGUES_ETRANGERES = re.compile(
    r"\b(jap|jp|japan|japanese|japonais|japonaise|chinese|chinois|chinoise|korean|coreen|"
    r"coréen|coréenne|kr|cn|s-chinese|t-chinese|german|allemand|allemande|italian|italien|"
    r"italienne|spanish|espagnol|espagnole|portugues|portugais)\b", re.I)

# Mode continu : bilan Telegram toutes les X minutes (au lieu de chaque passage)
BILAN_TOUTES_LES_MIN = 30

# Watchlist : si le titre contient le mot ET que le prix est <= au max,
# alerte dès qu'il y a un bénéfice (même sous la décote minimale).
# Le prix Cardmarket est toujours vérifié avant.
WATCHLIST = {
    "dracaufeu": 25,
    "charizard": 25,
    "display": 90,
    "etb": 35,
    "psa 10": 60,
    "alternative": 30,
}

# Annonces ignorées directement (faux, recherches, accessoires...)
MOTS_EXCLUS = [
    "proxy", "fake", "custom", "réplique", "replica", "goldée", "metal", "métal",
    "recherche", "cherche", "classeur vide", "sleeve", "protège", "pochette", "peluche",
    "figurine", "jeu vidéo", "switch", "3ds", "fan art", "fanart", "mega construx",
    "lego", "funko", "plush", "plushie", "sticker only",
    # autres jeux / cartes de sport
    "nfl", "nba", "panini", "topps", "upper deck", "football", "baseball", "basket",
    "soccer", "one piece", "yu-gi-oh", "yugioh", "lorcana", "magic the gathering",
    "dragon ball", "digimon", "leaf ",
    # célébrités, autres licences, autres sports
    "trump", "biden", "macron", "président", "president", "elon", "wwe", "ufc", "f1 ",
    "formule 1", "tennis", "rugby", "hockey", "nhl", "mlb", "marvel", "star wars",
    "harry potter", "disney", "naruto", "dragon ball", "jujutsu", "demon slayer",
]
# (les mots "one piece" sont exclus pour Pokémon, mais gérés à part si ONE_PIECE_ACTIF)
POKEMON_INDICES = re.compile(r"(pok[eé]mon|pkmn|\betb\b|elite trainer|coffret dresseur|"
                             r"pok[eé]ball|pikachu|dracaufeu|charizard|evoli|évoli|eevee|mewtwo|"
                             r"\bmew\b|ronflex|lucario|rayquaza|umbreon|noctali|gengar|ectoplasma)", re.I)
ONE_PIECE_INDICES = re.compile(r"(one piece|\bop-?\s?\d{1,2}\b|\beb-?\s?\d{1,2}\b|\bprb-?\s?\d{1,2}\b|"
                               r"romance dawn|luffy)", re.I)
MOTS_EXCLUS_OP_OK = {"one piece"}

PRIX_MIN = 2.0          # en dessous : souvent des arnaques ou des cartes communes
PRIX_MAX = 500.0        # au dessus : hors budget
# ===== RÈGLE D'ALERTE =====
# Alerte si : produit Pokémon + prix au moins DECOTE_MIN % sous le prix Cardmarket
# + bénéfice net (après frais Vinted, port et commission) >= BENEF_MIN
DECOTE_MIN = 30         # % minimum sous le prix Cardmarket
# Un message Telegram à CHAQUE run : bilan + meilleure affaire trouvée,
# même si elle n'atteint pas les seuils. Envoyé en mode silencieux
# (pas de son) pour que seuls les vrais bons plans fassent sonner ton téléphone.
RESUME_CHAQUE_RUN = True
RESUME_SILENCIEUX = True
BENEF_MIN = 3.0         # bénéfice net minimum en €

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

if GEMINI_API_KEY and GEMINI_ACTIF:
    client = genai.Client(api_key=GEMINI_API_KEY,
                          http_options=types.HttpOptions(timeout=45000))
    MODELES = decouvrir_modeles()
    print(f"🤖 Gemini actif : {len(MODELES)} modèles -> {', '.join(MODELES[:6])}"
          + (" ..." if len(MODELES) > 6 else ""))
else:
    print("ℹ️ Gemini non utilisé (clé absente ou GEMINI_ACTIF = False).")

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
def lien_cardmarket(recherche):
    return ("https://www.cardmarket.com/fr/Pokemon/Products/Search?searchString="
            + requests.utils.quote(recherche or ""))

def envoyer_telegram(message, photo_url=None, lien=None, recherche_cm=None, silencieux=False,
                     lignes_boutons=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Identifiants Telegram manquants.")
        return False
    base = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    boutons = []
    if lien:
        boutons.append({"text": "🛒 Voir sur Vinted", "url": lien})
    if recherche_cm:
        boutons.append({"text": "📊 Cardmarket", "url": lien_cardmarket(recherche_cm)})
    rangees = ([boutons] if boutons else []) + (lignes_boutons or [])
    clavier = {"inline_keyboard": rangees} if rangees else None

    def post(methode, payload):
        if clavier:
            payload["reply_markup"] = clavier
        if silencieux:
            payload["disable_notification"] = True
        return requests.post(f"{base}/{methode}", json=payload, timeout=TIMEOUT)

    try:
        if photo_url and len(message) <= 1024:
            r = post("sendPhoto", {"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url,
                                   "caption": message, "parse_mode": "HTML"})
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
    for query in RECHERCHES + (RECHERCHES_ONE_PIECE if ONE_PIECE_ACTIF else []):
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
def jeu_de(a):
    """'onepiece' si l'annonce parle de One Piece, sinon 'pokemon'."""
    return "onepiece" if ONE_PIECE_INDICES.search(a["title"]) else "pokemon"

def est_exclue(a):
    titre = a["title"].lower()
    jeu = jeu_de(a)
    if jeu == "onepiece" and not ONE_PIECE_ACTIF:
        return True
    for m in MOTS_EXCLUS:
        if m in titre and not (jeu == "onepiece" and m in MOTS_EXCLUS_OP_OK):
            return True
    if CARTES_FR_EN_SEULEMENT and LANGUES_ETRANGERES.search(titre):
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
    print(f"🦙 Groq actif : {len(GROQ_MODELES)} modèles"
          + (" (IA principale)" if IA_PRINCIPALE == "groq" else " (secours)"))

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
            if r.status_code in (400, 404) and modele in GROQ_MODELES and len(GROQ_MODELES) > 1:
                GROQ_MODELES.remove(modele)
                print(f"   ❌ Groq {modele} indisponible ({r.status_code}), retiré")
            elif r.status_code == 429 and modele in GROQ_MODELES and len(GROQ_MODELES) > 1:
                # quota atteint sur ce modèle : on le met en fin de liste
                GROQ_MODELES.remove(modele)
                GROQ_MODELES.append(modele)
                print(f"   🪫 Groq {modele} : quota atteint -> modèle suivant")
            else:
                print(f"   ⚠️ Groq {modele} : {r.status_code} -> modèle suivant")
        except Exception as e:
            print(f"   ⚠️ Groq {modele} : {str(e)[:80]} -> modèle suivant")
    return None

# ================= ANALYSE IA =================
PROMPT = """Nous sommes le {date}. Tu es un expert du marché français des cartes Pokémon
(Cardmarket, eBay, Vinted).
Analyse cette annonce Vinted :
- Titre : {title}
- Prix : {price} €
- État : {status}

Objectif : ACHAT-REVENTE. Estime le prix auquel cette annonce se REVEND réellement
en France (prix de vente constatés sur Cardmarket et Vinted, pas les prix demandés),
en tenant compte de l'état, de l'édition, de la langue et de la demande.
Sois prudent : en cas de doute, estime bas.

IMPORTANT : tes connaissances s'arrêtent avant aujourd'hui. De nouvelles séries,
extensions, coffrets et promos sortent en permanence (ex : produits du 30e anniversaire
Pokémon en 2026, séries Méga-Évolution, Légendes Z-A / Illumis-Lumiose...).
Ne considère JAMAIS un produit comme faux uniquement parce que tu ne le connais pas.
Pour un produit récent que tu ne connais pas, estime sa valeur d'après son type
(display, ETB, coffret, carte ultra rare...) et mets un score plus bas.

"faux": true UNIQUEMENT si signes clairs : "proxy", "fan art", "custom", "réplique",
carte en métal/dorée non officielle, texte ou visuel visiblement faux sur la photo.
"pokemon": false si ce n'est PAS un produit Pokémon TCG (autre jeu, carte de sport,
jouet, Mega Construx, figurine...).

Identifie aussi le produit le plus précisément possible.
Réponds en JSON :
{{"pokemon": true ou false, "faux": true ou false,
  "type": "carte" ou "gradee" ou "scelle" ou "lot" ou "autre",
  "nom_fr": "nom français de la carte ou du produit", "nom_en": "nom anglais",
  "numero": "numéro de la carte ex 102 (vide si inconnu)",
  "total": "total du set ex 128 (vide si inconnu)",
  "prix_revente": prix Cardmarket FR estimé en euros,
  "raison": "une phrase courte EN FRANÇAIS"}}
Toutes tes réponses texte doivent être en français."""

# ================= PRIX CARDMARKET (via TCGdex, gratuit) =================
TCGDEX = "https://api.tcgdex.net/v2"
_cache_tcgdex = {}
MOTS_VIDES = {
    "carte", "cartes", "pokemon", "pokémon", "holo", "reverse", "rare", "ultra", "secret",
    "full", "art", "alternative", "alt", "promo", "neuf", "mint", "near", "lot", "card",
    "cards", "francaise", "française", "francais", "français", "anglaise", "japonaise",
    "fr", "eng", "en", "jp", "jap", "vf", "etat", "état", "tbe", "be", "tcg", "set", "the",
    "and", "les", "des", "une", "avec", "pour", "edition", "édition", "illustration",
}
GRADEE = re.compile(r"\b(psa|cgc|bgs|pca|egc|beckett|collect aura|grad[ée]e?)\b", re.I)

def tcgdex_get(chemin, params=None):
    cle = (chemin, tuple(sorted((params or {}).items())))
    if cle in _cache_tcgdex:
        return _cache_tcgdex[cle]
    data = None
    try:
        r = requests.get(f"{TCGDEX}/{chemin}", params=params, timeout=TIMEOUT)
        if r.status_code == 200:
            data = r.json()
    except Exception:
        pass
    _cache_tcgdex[cle] = data
    return data

def extraire_numero(titre):
    m = re.search(r"\b([A-Za-z]{0,4}\d{1,3})\s*/\s*([A-Za-z]{0,4}\d{1,3})\b", titre)
    return (m.group(1), m.group(2)) if m else ("", "")

def mots_du_titre(titre):
    mots = re.findall(r"[A-Za-zÀ-ÿ\-']{3,}", titre)
    return [m for m in mots if m.lower() not in MOTS_VIDES][:4]

def prix_cardmarket(noms, numero, total, reverse=False):
    """Cherche la carte sur TCGdex et renvoie le prix Cardmarket (tendance)."""
    numero = (numero or "").strip()
    if not numero:
        return None
    variantes = {numero, numero.lstrip("0") or "0"}
    if numero.isdigit():
        variantes.add(numero.zfill(3))
    filtre_num = "eq:" + "|".join(sorted(variantes))
    total_int = int(re.sub(r"\D", "", total)) if re.sub(r"\D", "", total or "") else None

    for langue in ("fr", "en"):
        for nom in [n for n in noms if n][:5]:
            res = tcgdex_get(f"{langue}/cards", {"name": nom, "localId": filtre_num})
            if not isinstance(res, list):
                continue
            for brief in res[:6]:
                carte = tcgdex_get(f"{langue}/cards/{brief.get('id')}")
                if not isinstance(carte, dict):
                    continue
                nb = ((carte.get("set") or {}).get("cardCount") or {})
                if total_int and total_int not in (nb.get("official"), nb.get("total")):
                    continue
                cm = (carte.get("pricing") or {}).get("cardmarket") or {}
                if reverse:
                    ref = cm.get("trend-holo") or cm.get("avg30-holo") or cm.get("avg-holo")
                else:
                    ref = None
                ref = ref or cm.get("trend") or cm.get("avg30") or cm.get("avg")
                if ref:
                    return {"prix": round(float(ref), 2), "nom": carte.get("name"),
                            "set": (carte.get("set") or {}).get("name", ""),
                            "id": carte.get("id")}
    return None

# ================= PRIX CARDMARKET DES PRODUITS SCELLÉS =================
# Guide de prix officiel publié chaque jour par Cardmarket (gratuit, sans clé)
CM_CATALOGUE = "https://downloads.s3.cardmarket.com/productCatalog"
MOTS_SCELLES = re.compile(r"\b(etb|elite trainer|display|booster box|booster bundle|bundle|"
                          r"coffret|collection|upc|tin|pok[eé]ball tin|blister|tripack|"
                          r"tri-pack|booster|scell[ée]|sealed|premium|box)\b", re.I)
ABREVIATIONS = [
    (r"\betb\b", "elite trainer box"), (r"\bdisplay\b", "booster box"),
    (r"\bupc\b", "ultra premium collection"), (r"\btri-?pack\b", "3 pack blister"),
    (r"\bcoffret dresseur d'?elite\b", "elite trainer box"),
]
MOTS_VIDES_EN = {"pokemon", "tcg", "scelle", "sealed", "neuf", "new", "fr", "en", "eng",
                 "francais", "anglais", "english", "french", "the", "de", "la", "le", "et",
                 "a", "vendre", "pour", "avec", "of", "and", "edition", "version", "set"}
_catalogues = {}          # jeu -> liste de produits
ID_JEU_CARDMARKET = {"pokemon": [6], "onepiece": [18, 19, 20, 21, 22, 17]}
SIGNATURE_JEU = {"onepiece": ("romance dawn", "paramount war", "pillars of strength",
                              "kingdoms of intrigue", "awakening of the new era")}

def jetons(texte):
    t = unicodedata.normalize("NFKD", texte.lower()).encode("ascii", "ignore").decode()
    for motif, remplacement in ABREVIATIONS:
        t = re.sub(motif, remplacement, t)
    t = re.sub(r"\b(op|eb|st|prb)\s*-?\s*(\d{1,2})\b",
               lambda m: f"{m.group(1)}{int(m.group(2)):02d}", t)
    return [w for w in re.findall(r"[a-z0-9]+", t) if w not in MOTS_VIDES_EN]

def charger_catalogue_scelle(jeu="pokemon"):
    """Télécharge (1 fois par run) le guide de prix Cardmarket des produits scellés."""
    if jeu in _catalogues:
        return _catalogues[jeu]
    _catalogues[jeu] = []
    for id_jeu in ID_JEU_CARDMARKET.get(jeu, []):
        try:
            prods = requests.get(f"{CM_CATALOGUE}/productList/products_nonsingles_{id_jeu}.json",
                                 timeout=(5, 60)).json().get("products", [])
            if jeu in SIGNATURE_JEU:
                noms = " ".join(p.get("name", "").lower() for p in prods[:5000])
                if not any(sig in noms for sig in SIGNATURE_JEU[jeu]):
                    continue  # ce n'est pas le bon jeu, on essaie l'identifiant suivant
            guide = requests.get(f"{CM_CATALOGUE}/priceGuide/price_guide_{id_jeu}.json",
                                 timeout=(5, 90)).json().get("priceGuides", [])
            prix = {g.get("idProduct"): g for g in guide}
            for pr in prods:
                g = prix.get(pr.get("idProduct"))
                ref = g and (g.get("trend") or g.get("avg30") or g.get("avg"))
                if ref and pr.get("name"):
                    _catalogues[jeu].append((set(jetons(pr["name"])), pr["name"], float(ref)))
            print(f"   📦 Guide de prix Cardmarket ({jeu}) chargé : {len(_catalogues[jeu])} produits")
            break
        except Exception as e:
            print(f"   ⚠️ Guide de prix Cardmarket ({jeu}) indisponible : {str(e)[:80]}")
    return _catalogues[jeu]

MOTS_TYPE = {"booster", "box", "elite", "trainer", "bundle", "collection", "premium", "ultra",
             "pack", "blister", "tin", "mini", "deck", "starter", "double", "case", "3",
             "build", "battle", "display", "sleeved", "half", "art"}
MOTS_SERIE = {"scarlet", "violet", "sword", "shield", "sun", "moon", "black", "white",
              "xy", "sv", "swsh", "sm", "one", "piece", "card", "game"}

def fr_vers_en(texte):
    """Remplace les noms d'extensions français par leur nom anglais (pour Cardmarket)."""
    t = unicodedata.normalize("NFKD", texte.lower()).encode("ascii", "ignore").decode()
    for en, fr in sorted(sets_en_vers_fr().items(), key=lambda x: -len(x[1])):
        f = unicodedata.normalize("NFKD", fr.lower()).encode("ascii", "ignore").decode()
        if len(f) > 3 and f in t:
            t = t.replace(f, en)
    return t

def prix_scelle(requete, jeu="pokemon"):
    """Trouve le produit scellé Cardmarket correspondant à la requête.
    Le produit doit avoir TOUS ses mots de type (display, ETB...) dans la requête
    et la majorité de ses mots d'extension."""
    if not requete:
        return None
    q = set(jetons(fr_vers_en(requete) if jeu == "pokemon" else requete))
    meilleur, meilleur_score = None, 0
    for toks, nom, ref in charger_catalogue_scelle(jeu):
        types_p = toks & MOTS_TYPE
        set_p = toks - MOTS_TYPE - MOTS_SERIE
        types_requis = types_p - {"booster"} if "bundle" in types_p else types_p
        if not types_p or not set_p or not types_requis <= q:
            continue
        communs = set_p & q
        if not communs or len(communs) / len(set_p) < 0.6:
            continue
        score = len(communs) + len(types_p) - 0.1 * len(set_p - q)
        if score > meilleur_score:
            meilleur, meilleur_score = (nom, ref), score
    if meilleur:
        return {"prix": round(meilleur[1], 2), "nom": meilleur[0], "set": "",
                "nom_fr": traduire_produit(meilleur[0])}
    return None

# ================= TRADUCTION EN FRANÇAIS =================
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
    ("starter deck", "Deck de démarrage"),
    ("double pack", "Double pack"),
    ("collection box", "Coffret"),
    ("collection", "Coffret"),
    ("blister", "Blister"),
    ("booster", "Booster"),
    ("tin", "Pokébox"),
]
_sets_fr = None

def sets_en_vers_fr():
    """Noms des extensions Pokémon anglais -> français (via TCGdex)."""
    global _sets_fr
    if _sets_fr is None:
        _sets_fr = {}
        en = tcgdex_get("en/sets") or []
        fr = {x.get("id"): x.get("name") for x in (tcgdex_get("fr/sets") or [])}
        for x in en:
            if x.get("name") and fr.get(x.get("id")):
                _sets_fr[x["name"].lower()] = fr[x["id"]]
    return _sets_fr

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
        if en and en in reste.lower():
            i = reste.lower().find(en)
            reste = reste[:i] + fr + reste[i + len(en):]
            break
    return f"{type_fr} {reste}".strip() if type_fr else reste

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

GEMINI_ECHECS = 0
GEMINI_ECHECS_MAX = 3   # après 3 échecs complets, Gemini est ignoré jusqu'au run suivant

def analyser(a):
    """Essaie tous les modèles, plusieurs tours. Renvoie (resultat, ok).
    ok=False seulement si TOUT a échoué -> l'annonce sera retentée au run suivant."""
    if not (client and MODELES) and not GROQ_MODELES:
        return None, False
    prompt = PROMPT.format(date=time.strftime("%d/%m/%Y"), **a)
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

    global GEMINI_ECHECS
    gemini_ok = bool(client and MODELES) and GEMINI_ECHECS < GEMINI_ECHECS_MAX
    for tour in range(TOURS_MAX):
        if IA_PRINCIPALE == "groq" and GROQ_MODELES:
            res = analyser_groq(prompt)
            if isinstance(res, dict):
                return res, True
        essais = 0
        for modele in (list(MODELES) if gemini_ok else []):
            if essais >= GEMINI_ESSAIS_MAX:
                break
            essais += 1
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
                GEMINI_ECHECS = 0
                return res, True
            except json.JSONDecodeError:
                print(f"   ⚠️ {modele} : réponse illisible, modèle suivant")
            except Exception as e:
                msg = str(e)
                code = msg[:3]
                if code in ("404", "400") and "API key" not in msg:
                    print(f"   ❌ {modele} indisponible ({code}), retiré")
                    if modele in MODELES and len(MODELES) > 1:
                        MODELES.remove(modele)
                elif code == "429":
                    # Quota épuisé : inutile de le réessayer pendant ce run
                    print(f"   🪫 {modele} : quota épuisé, retiré pour ce run")
                    if modele in MODELES:
                        MODELES.remove(modele)
                    if not MODELES:
                        print("   🪫 Plus aucun modèle Gemini disponible pour ce run")
                        gemini_ok = False
                        break
                elif "API key" in msg or code in ("401", "403"):
                    print(f"   ⛔ Clé Gemini refusée : {msg[:150]}")
                    gemini_ok = False
                    break
                else:
                    # Surcharge (503/504...) : on le passe en fin de liste
                    print(f"   ⚠️ {modele} : {code} surcharge -> modèle suivant")
                    if modele in MODELES and len(MODELES) > 1:
                        MODELES.remove(modele)
                        MODELES.append(modele)
        if gemini_ok:
            GEMINI_ECHECS += 1
            if GEMINI_ECHECS >= GEMINI_ECHECS_MAX:
                print("   🪫 Gemini échoue en boucle : désactivé pour ce run")
                gemini_ok = False
        if IA_PRINCIPALE != "groq" and GROQ_MODELES:
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
SEP = "━━━━━━━━━━━━━━━"

def euros(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",").replace(",00", "") + " €"

def jauge(decote):
    """Barre visuelle de la décote : 10 cases = 100 %."""
    pleines = max(0, min(10, round(decote / 10)))
    couleur = "🟩" if decote >= DECOTE_MIN else ("🟨" if decote >= 10 else "🟥")
    return couleur * pleines + "⬜" * (10 - pleines)

def niveau(decote, benef):
    if decote >= 50 and benef >= 20:
        return "💎 PÉPITE"
    if decote >= 40:
        return "🚀 GROS COUP"
    return "🔥 BON PLAN"

def formater(a, titre, infos=None):
    """Alerte bon plan."""
    e = html.escape
    lignes = [f"<b>{titre}</b>", SEP, f"📦 <b>{e(a['title'][:90])}</b>"]
    if infos:
        lignes += [
            f"💰 Vinted <b>{euros(a['price'])}</b>  ➜  📊 Cardmarket <b>{euros(infos['ref'])}</b>",
            f"{jauge(infos['decote'])} <b>{'-' if infos['decote'] >= 0 else '+'}{abs(infos['decote'])} %</b>",
            f"💵 <b>Bénéfice net : {'+' if infos['benef'] >= 0 else ''}{euros(infos['benef'])}</b>"
            f"  (rentabilité {infos['roi']} %)",
            f"🧾 Coût réel (protection + port) : {euros(infos['cout'])}",
        ]
        if infos.get("carte"):
            lignes.append(f"🃏 {e(str(infos['carte'])[:80])}")
        if infos.get("source", "").startswith("Estimation"):
            lignes.append("🤖 Prix Cardmarket estimé par l'IA (produit non trouvé dans le guide)")
    details = " · ".join(x for x in [
        f"🏷️ {e(a['status'])}" if a.get("status") else "",
        f"👤 {e(a['vendeur'])}" if a.get("vendeur") else ""] if x)
    if details:
        lignes.append(details)
    lignes += [SEP, "⚡ Vérifie Cardmarket en 1 clic et fonce 👇"]
    return "\n".join(lignes)

def formater_bilan(stats, top):
    """Bilan d'un passage sans bon plan : stats + podium des meilleures affaires."""
    e = html.escape
    heure = datetime.now(ZoneInfo("Europe/Paris")).strftime("%H:%M")
    periode = stats.get("periode") or f"PASSAGE DE {heure}"
    etat = (f"✅ {stats['alertes']} bon(s) plan(s) envoyé(s)" if stats.get("alertes")
            else "pas de bon plan")
    lignes = [f"📋 <b>{periode}</b> · {etat}",
              f"🆕 {stats['nouvelles']} nouvelles · 📊 {stats['comparees']} comparées"
              f" · 🚫 {stats['ecartees']} écartées"
              + (f" · ⏳ {stats['attente']} en attente" if stats['attente'] else "")]
    if top:
        lignes += [SEP, "🏆 <b>PODIUM</b>"]
        for medaille, (a, inf) in zip(["🥇", "🥈", "🥉"], top):
            signe = "-" if inf["decote"] >= 0 else "+"
            b = f"{'+' if inf['benef'] >= 0 else ''}{euros(inf['benef'])}"
            lignes.append(f"{medaille} <b>{e(a['title'][:45])}</b>")
            lignes.append(f"      {euros(a['price'])} au lieu de {euros(inf['ref'])} sur Cardmarket · "
                          f"{signe}{abs(inf['decote'])} % · {b}")
        a, inf = top[0]
        manque = []
        if inf["decote"] < DECOTE_MIN:
            manque.append(f"{DECOTE_MIN - inf['decote']} pts de décote")
        if inf["benef"] < BENEF_MIN:
            manque.append(f"{euros(BENEF_MIN - inf['benef'])} de bénéfice")
        if manque:
            lignes.append(f"📏 Il manque {' et '.join(manque)} au 🥇 pour une alerte")
    else:
        lignes += [SEP, "😴 Aucune nouvelle annonce comparable sur cette période."]
    lignes += [SEP, f"🎯 Tes seuils : -{DECOTE_MIN} % et +{euros(BENEF_MIN)} de bénéfice"]
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
        return {"nouvelles": len(nouvelles), "comparees": 0, "ecartees": 0,
                "attente": 0, "alertes": 0, "candidats": []}

    alertes = 0
    a_reessayer = 0
    ecartees = 0
    comparees = 0
    candidats = []  # (annonce, infos) de toutes les annonces comparées
    ia_en_panne = False
    ia_dispo = bool(client or GROQ_MODELES)
    for a in nouvelles:
        if est_exclue(a):
            ids.append(a["id"])
            continue

        jeu = jeu_de(a)
        wl = match_watchlist(a) if jeu == "pokemon" else None
        print(f"🔍 [{'One Piece' if jeu == 'onepiece' else 'Pokémon'}] {a['title']} — {a['price']} €")
        est_gradee = bool(GRADEE.search(a["title"]))
        num_titre, total_titre = extraire_numero(a["title"])
        res, ok, cm = {}, False, None

        # 1) Carte Pokémon avec numéro -> prix Cardmarket direct, SANS IA
        if jeu == "pokemon" and num_titre and not est_gradee:
            cm = prix_cardmarket(mots_du_titre(a["title"]), num_titre, total_titre,
                                 "reverse" in a["title"].lower())
            if cm:
                print("   ⚡ Trouvée sur Cardmarket sans IA")

        # 1 bis) Produit scellé -> guide de prix Cardmarket, SANS IA
        if not cm and not est_gradee and (MOTS_SCELLES.search(a["title"]) or jeu == "onepiece"):
            cm = prix_scelle(a["title"], jeu)
            if cm:
                print(f"   ⚡ Trouvé dans le guide Cardmarket sans IA : {cm['nom']}")

        # 2) Sinon : IA pour identifier (vrai produit ? faux ? lequel ?)
        if not cm and ia_dispo and not ia_en_panne:
            r, ok = analyser(a)
            time.sleep(PAUSE_GEMINI)
            res = r if isinstance(r, dict) else {}
            if not ok:
                ia_en_panne = True
                print("   💤 IA indisponible : les annonces sans prix Cardmarket attendront")
            elif res.get("faux") or res.get("suspect") or (jeu == "pokemon" and res.get("pokemon") is not True):
                print(f"   🚫 écartée : {res.get('raison', '')}")
                ecartees += 1
                ids.append(a["id"])
                continue
            elif jeu == "pokemon" and res.get("numero") and not est_gradee \
                    and res.get("type") not in ("gradee", "scelle"):
                noms = [res.get("nom_fr"), res.get("nom_en")] + mots_du_titre(a["title"])
                cm = prix_cardmarket(noms, str(res.get("numero")),
                                     str(res.get("total") or total_titre or ""),
                                     "reverse" in a["title"].lower())
            if ok and not cm and res.get("type") in ("scelle", "lot", "autre", None):
                cm = prix_scelle(res.get("nom_en") or "", jeu)
                if cm:
                    print(f"   📦 Trouvé dans le guide Cardmarket : {cm['nom']}")

        numero = str(res.get("numero") or num_titre or "")
        if cm:
            ref, source = cm["prix"], "Prix Cardmarket"
            nom_aff = cm.get("nom_fr") or cm["nom"]
            carte = (f"{nom_aff} {numero} – {cm['set']}" if cm.get("set") else nom_aff).strip()
        elif ok and jeu == "pokemon":
            # Estimation IA acceptée seulement si c'est clairement du Pokémon
            nom_ia = res.get("nom_fr") or ""
            if not (POKEMON_INDICES.search(a["title"]) or
                    (nom_ia and tcgdex_get("fr/cards", {"name": nom_ia.split()[0]}))):
                print("   🚫 écartée : rien ne prouve que c'est du Pokémon")
                ecartees += 1
                ids.append(a["id"])
                continue
            try:
                ref = float(res.get("prix_revente") or 0)
            except (TypeError, ValueError):
                ref = 0
            source, carte = "Estimation IA", nom_ia
        elif ok:
            # One Piece sans prix Cardmarket : on ignore (prix Cardmarket obligatoire)
            print("   ⏭️ One Piece : pas de prix Cardmarket, ignoré")
            ids.append(a["id"])
            continue
        else:
            a_reessayer += 1  # ni IA ni Cardmarket : on réessaiera au prochain run
            continue

        ids.append(a["id"])
        if ref <= 0:
            continue

        decote = round((1 - a["price"] / ref) * 100)
        cout, benef, roi = calcul_rentabilite(a["price"], ref)
        ecart = f"-{decote} %" if decote >= 0 else f"+{-decote} % plus cher"
        print(f"   📊 {source} : {ref} € | {ecart} | bénéfice {benef} € | rentabilité {roi} %")

        comparees += 1
        recherche = (cm or {}).get("nom") or res.get("nom_en") or res.get("nom_fr") or a["title"]
        infos = {"source": source, "ref": ref, "carte": carte, "decote": decote, "cout": cout,
                 "benef": benef, "roi": roi, "raison": res.get("raison"), "recherche": recherche}
        candidats.append((a, infos))

        if jeu == "onepiece":
            alerte = decote >= OP_DECOTE_MIN and benef >= OP_BENEF_MIN
            titre = f"🏴‍☠️ ONE PIECE EXTRÊME · -{decote} % SOUS CARDMARKET"
        elif decote >= DECOTE_MIN and benef >= BENEF_MIN:
            alerte, titre = True, f"{niveau(decote, benef)} · -{decote} % SOUS CARDMARKET"
        elif wl and a["price"] <= wl[1] and benef >= BENEF_MIN:
            alerte, titre = True, f"🎯 WATCHLIST « {wl[0]} » · -{decote} % SOUS CARDMARKET"
        else:
            alerte = False

        if alerte and envoyer_telegram(formater(a, titre, infos), a["photo"], a["url"], recherche):
            alertes += 1
            print("   ✅ Alerte envoyée")

    sauvegarder_historique(ids)
    if a_reessayer:
        print(f"🔁 {a_reessayer} annonce(s) seront réessayées au prochain run.")
    print(f"✨ Cycle terminé : {alertes} alerte(s).")

    return {"nouvelles": len(nouvelles), "comparees": comparees, "ecartees": ecartees,
            "attente": a_reessayer, "alertes": alertes, "candidats": candidats}

def envoyer_bilan(stats):
    top = sorted(stats["candidats"], key=lambda c: (c[1]["benef"], c[1]["decote"]),
                 reverse=True)[:3]
    message = formater_bilan(stats, top)
    if top:
        a1, i1 = top[0]
        autres = [{"text": f"{m} Vinted", "url": a["url"]}
                  for m, (a, _) in zip(["🥈", "🥉"], top[1:])]
        envoyer_telegram(message, a1.get("photo"), a1["url"], i1["recherche"],
                         silencieux=RESUME_SILENCIEUX, lignes_boutons=[autres] if autres else None)
    else:
        envoyer_telegram(message, silencieux=RESUME_SILENCIEUX)
    print("   📨 Bilan envoyé sur Telegram")

def sauvegarde_github():
    """Sur GitHub Actions : enregistre l'historique dans le dépôt (anti-doublons)."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    import subprocess
    cmds = [["git", "config", "user.name", "zuntytcg-bot"],
            ["git", "config", "user.email", "bot@users.noreply.github.com"],
            ["git", "add", HISTORIQUE_FILE],
            ["git", "commit", "-m", "maj historique [skip ci]"],
            ["git", "pull", "--rebase", "--autostash"],
            ["git", "push"]]
    for c in cmds:
        subprocess.run(c, capture_output=True, timeout=60)
    print("   💾 Historique sauvegardé sur GitHub")

def fusionner(total, st):
    for k in ("nouvelles", "comparees", "ecartees", "alertes"):
        total[k] += st[k]
    total["attente"] = st["attente"]
    total["candidats"] += st["candidats"]

def stats_vides():
    return {"nouvelles": 0, "comparees": 0, "ecartees": 0, "attente": 0,
            "alertes": 0, "candidats": []}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                        help="intervalle en secondes pour tourner en continu")
    parser.add_argument("--duree", type=int, default=0,
                        help="durée max en minutes (0 = sans fin)")
    args = parser.parse_args()

    print("🚀 ZuntyTCG-Bot démarré")
    if not args.loop:
        st = cycle()
        if RESUME_CHAQUE_RUN and st:
            envoyer_bilan(st)
        return

    debut = time.time()
    fin = debut + args.duree * 60 if args.duree else None
    envoyer_telegram("🟢 <b>ZuntyTCG-Bot en ligne</b>\n"
                     f"Surveillance de Vinted toutes les ~{args.loop // 60 or 1} min"
                     + (f" pendant {args.duree // 60} h {args.duree % 60:02d}" if args.duree else "")
                     + f".\nBilan toutes les {BILAN_TOUTES_LES_MIN} min, alertes en direct 🔥",
                     silencieux=RESUME_SILENCIEUX)
    periode, debut_periode, premier = stats_vides(), time.time(), True
    while True:
        try:
            st = cycle()
            if st:
                fusionner(periode, st)
        except Exception as e:
            print(f"💥 Erreur inattendue : {e}")
        maintenant = time.time()
        if RESUME_CHAQUE_RUN and (premier or maintenant - debut_periode >= BILAN_TOUTES_LES_MIN * 60):
            heure_debut = datetime.fromtimestamp(debut_periode, ZoneInfo("Europe/Paris")).strftime("%H:%M")
            heure_fin = datetime.fromtimestamp(maintenant, ZoneInfo("Europe/Paris")).strftime("%H:%M")
            periode["periode"] = f"BILAN {heure_debut} → {heure_fin}"
            try:
                envoyer_bilan(periode)
                sauvegarde_github()
            except Exception as e:
                print(f"💥 Erreur bilan : {e}")
            periode, debut_periode, premier = stats_vides(), maintenant, False
        attente = args.loop + random.randint(0, 20)
        if fin and maintenant + attente >= fin:
            print("🏁 Durée maximale atteinte : fin de la session (relance automatique)")
            break
        print(f"⏳ Prochain passage dans {attente} s\n")
        time.sleep(attente)

if __name__ == "__main__":
    main()