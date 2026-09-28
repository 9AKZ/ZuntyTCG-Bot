import os
import json
import time
import re
import requests
from dotenv import load_dotenv
from vinted_scraper import VintedScraper

# Charge automatiquement les clés depuis le fichier .env
load_dotenv()

# ================= CONFIGURATION ZentyTCG-Bot =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

VINTED_QUERY = "booster pokemon"
FICHIER_CACHE = "zenty_tcg_bot_cache.json"
INTERVALLE_SCAN_SECONDES = 60  # Scan toutes les 60 secondes
DISCOUNT_MINIMUM_POURCENT = 35.0  # Réduction minimale (-35% vs Cote)

MOTS_INTERDITS = [
    # Faux / Proxies
    "proxy", "proxies", "custom", "fanart", "fan art", "fanmade", "replica", 
    "reproduction", "fake", "fausse", "faux", "reprint", "aliexpress", "chinois",
    # Emballages vides ou ouverts
    "vide", "vides", "empty", "ouvert", "ouverte", "ouverts", "sans booster", 
    "sans les boosters", "boite seule", "boîte seule", "coffret vide", "display vide",
    # Digital / Codes
    "code", "codes", "online", "tcgl", "jcc live", "ptcgo", "code tcg",
    # Cartes hors scellé
    "carte seule", "graduée", "graduee", "psa", "pca", "graad", "lot de cartes", 
    "vrac", "commune", "unco", "reverse", "bulk"
]

# ================= GESTION DU CACHE =================
def charger_cache():
    if os.path.exists(FICHIER_CACHE):
        try:
            with open(FICHIER_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []

def sauvegarder_cache(cache):
    try:
        with open(FICHIER_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache[-500:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ ZentyTCG-Bot Erreur Cache : {e}")

# ================= ENVOI TELEGRAM =================
def envoyer_alerte_telegram(texte, photo_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ ZentyTCG-Bot : Identifiants Telegram manquants.")
        return

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    if photo_url:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        payload["photo"] = photo_url
        payload["caption"] = texte
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload["text"] = texte

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ ZentyTCG-Bot Erreur Telegram ({res.status_code}) : {res.text}")
    except Exception as e:
        print(f"⚠️ ZentyTCG-Bot Erreur de connexion Telegram : {e}")

# ================= MOTEUR D'ANALYSE IA (GEMINI) =================
def analyser_annonce_avec_gemini(titre, prix, description):
    if not GEMINI_API_KEY:
        print("⚠️ GEMINI_API_KEY manquante.")
        return False, {}

    prompt = f"""
Tu es l'expert numéro 1 du marché Pokémon TCG en France pour ZentyTCG-Bot.
Ton rôle est d'analyser une annonce Vinted pour déterminer si c'est une EXCELLENTE affaire de revente ou de collection.

Règles d'évaluation :
1. Identifie le produit scellé/TCG précis (Display, ETB/Coffret, Booster, Tripack, Blister, etc.).
2. Estime sa COTE RÉELLE sur le marché français actuel (Cardmarket FR / eBay FR en €).
3. Vérifie l'état : Le produit doit être NEUF, SCELLÉ ou EN PARFAIT ÉTAT sans défaut (pas d'ouverture, pas de déchirure du plastique d'origine).
4. Calcule la réduction % : ((Cote - Prix_Vinted) / Cote) * 100.
5. C'est une 'bonne affaire' SEULEMENT SI :
   - La réduction est de -35% MINIMUM par rapport à la cote.
   - Le produit est 100% authentique, parfait/scellé.
   - Ce n'est NI un produit ouvert, NI du vrac, NI une arnaque (ex: Display scellée proposée à 15€).

Annonce Vinted à analyser :
- Titre : {titre}
- Prix demandé : {prix} €
- Description : {description}

Réponds STRICTEMENT sous forme d'objet JSON respectant ce schéma exact (sans markdown, sans balises ```json autour, juste le JSON brut) :
{{
  "est_bonne_affaire": true,
  "nom_produit": "Nom précis de l'item",
  "cote_estimee": 100.0,
  "reduction_pourcentage": 40.5,
  "etat_scelle_parfait": true,
  "analyse_detaillee": "Explication courte mais complète sur l'état, la série, le prix et le potentiel de gain."
}}
"""

    url = f"[https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=){GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=15)
        if res.status_code == 200:
            data = res.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
            donnees_ia = json.loads(raw_text)
            return donnees_ia.get("est_bonne_affaire", False), donnees_ia
        else:
            print(f"⚠️ Erreur API Gemini ({res.status_code}) : {res.text}")
            return False, {}
    except Exception as e:
        print(f"⚠️ ZentyTCG-Bot : Erreur lors de l'appel Gemini : {e}")
        return False, {}

# ================= FILTRAGE ET EXECUTION =================
def filtrage_rapide(titre, description):
    texte = f"{titre} {description}".lower()
    for mot in MOTS_INTERDITS:
        pattern = rf"(?:\b|_){re.escape(mot)}(?:\b|_)"
        if re.search(pattern, texte):
            return False, f"Mot exclu : '{mot}'"
    return True, "OK"

def executer_cycle():
    print(f"\n[{time.strftime('%H:%M:%S')}] 🔍 ZentyTCG-Bot : Scan Vinted en cours...")
    cache = charger_cache()
    nouveaux_ids = []

    try:
        vinted = VintedScraper()
        items = vinted.search(VINTED_QUERY, params={"per_page": 20})

        for item in items:
            item_id = f"vinted_{item.get('id')}"
            if item_id in cache:
                continue

            nouveaux_ids.append(item_id)
            titre = item.get('title', 'Sans titre')
            prix = float(item.get('price', 0))
            description = item.get('description', '')
            url = item.get('url', '')

            photo_url = None
            if item.get('photo'):
                photo_url = item.get('photo', {}).get('url')

            # 1. Filtre local rapide
            valide_preliminaire, raison_preliminaire = filtrage_rapide(titre, description)
            if not valide_preliminaire:
                print(f"   ↳ Ignoré [{titre} - {prix}€] : {raison_preliminaire}")
                continue

            # 2. Analyse Gemini
            print(f"   🧠 ZentyTCG-Bot analyse avec Gemini : '{titre}' ({prix}€)...")
            est_affaire, analyse = analyser_annonce_avec_gemini(titre, prix, description)

            if est_affaire:
                nom_produit = analyse.get("nom_produit", titre)
                cote = analyse.get("cote_estimee", 0)
                reduction = analyse.get("reduction_pourcentage", 0)
                details = analyse.get("analyse_detaillee", "Pas de détails.")

                if reduction >= DISCOUNT_MINIMUM_POURCENT:
                    msg = (
                        f"🚨 <b>ZentyTCG-Bot : PÉPITE POKÉMON (-{reduction:.1f}%)</b> 🚨\n\n"
                        f"📌 <b>Item :</b> {nom_produit}\n"
                        f"💰 <b>Prix Vinted :</b> {prix} €\n"
                        f"📈 <b>Cote estimée :</b> {cote} €\n"
                        f"⚡ <b>Réduction :</b> -{reduction:.1f}%\n\n"
                        f"🔍 <b>Analyse ZentyTCG-Bot (Gemini) :</b>\n{details}\n\n"
                        f"🔗 <a href='{url}'>Acheter immédiatement sur Vinted</a>"
                    )
                    print(f"   ✨ ALERTE ENVOYÉE : {nom_produit} ({prix}€ vs Cote {cote}€)")
                    envoyer_alerte_telegram(msg, photo_url=photo_url)
                else:
                    print(f"   ↳ Ignoré : Réduction insuffisante (-{reduction:.1f}% vs -{DISCOUNT_MINIMUM_POURCENT}% exigé)")
            else:
                print(f"   ↳ Ignoré par Gemini : Non conforme ou hors critères (-35%/Scellé/Cote).")

    except Exception as e:
        print(f"⚠️ ZentyTCG-Bot Erreur durant le scan : {e}")

    # Mise à jour du cache
    for aid in nouveaux_ids:
        if aid not in cache:
            cache.append(aid)
    sauvegarder_cache(cache)

# ================= BOUCLE PRINCIPALE =================
if __name__ == "__main__":
    print("==================================================")
    print("🤖 ZentyTCG-Bot actif et connecté à Gemini !")
    print(f"🎯 Chat ID Telegram : {TELEGRAM_CHAT_ID}")
    print(f"🎯 Critère d'alerte : Réduction >= {DISCOUNT_MINIMUM_POURCENT}% vs Cote.")
    print("==================================================")

    while True:
        executer_cycle()
        time.sleep(INTERVALLE_SCAN_SECONDES)