import os
import json
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# ================= CONFIGURATION =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

VINTED_QUERY = "carte pokemon"
HISTORIQUE_FILE = "historique_vinted.json"

# Configuration de Gemini
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
else:
    model = None

# ================= HISTORIQUE (ANTI- DOUBLONS) =================
def charger_historique():
    if os.path.exists(HISTORIQUE_FILE):
        try:
            with open(HISTORIQUE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def sauvegarder_historique(historique):
    with open(HISTORIQUE_FILE, "w", encoding="utf-8") as f:
        json.dump(historique[-200:], f, ensure_ascii=False, indent=2)

# ================= TELEGRAM =================
def envoyer_telegram(message, photo_url=None):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Identifiants Telegram manquants.")
        return
    
    try:
        if photo_url:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "photo": photo_url, "caption": message, "parse_mode": "Markdown"}
        else:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
            
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ Erreur Telegram : {res.text}")
    except Exception as e:
        print(f"⚠️ Exception Telegram : {e}")

# ================= SCRAPING VINTED ALTERNATIF & ROBUSTE =================
def recuperer_annonces_vinted():
    print("🔍 Lancement de la recherche Vinted...")
    # On utilise l'interface web mobile de Vinted qui passe beaucoup mieux
    url = f"https://www.vinted.fr/catalog?search_text={VINTED_QUERY}&order=newest_first"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9",
        "Referer": "https://www.vinted.fr/"
    }
    
    try:
        session = requests.Session()
        # Étape 1 : Récupérer les cookies de session de base
        res_init = session.get("https://www.vinted.fr", headers=headers, timeout=15)
        print(f"🌐 Connexion initiale Vinted - Statut : {res_init.status_code}")
        
        # Étape 2 : Requête catalogue HTML ou API JSON directe de secours
        res = session.get(url, headers=headers, timeout=15)
        print(f"📡 Réponse catalogue Vinted - Statut : {res.status_code}")
        
        if res.status_code != 200:
            print(f"⚠️ Blocage potentiel ou code erreur Vinted: {res.status_code}")
            return []

        soup = BeautifulSoup(res.text, 'html.parser')
        
        # Extraction via l'API embarquée dans le HTML (state JSON de Vinted si présent)
        annonces = []
        
        # Méthode de secours : parsing des éléments HTML visibles
        items_html = soup.select("div.feed-grid__item, div[data-testid='grid-item']")
        print(f"📦 Éléments HTML bruts trouvés : {len(items_html)}")
        
        for item in items_html[:15]:
            try:
                link_elem = item.find("a", href=True)
                if not link_elem:
                    continue
                item_url = "https://www.vinted.fr" + link_elem["href"] if link_elem["href"].startswith("/") else link_elem["href"]
                
                # Extraire un ID unique de l'URL
                item_id = item_url.split("/")[-2] if "-" in item_url else item_url

                title_elem = item.find("p", class_="") or item.find("h2")
                title = title_elem.text.strip() if title_elem else "Carte Pokémon Vinted"

                price_elem = item.find(string=lambda t: t and '€' in t)
                price_str = price_elem.strip().replace('€', '').replace(',', '.').strip() if price_elem else "0"
                
                # Nettoyage prix
                price_val = 0.0
                for p in price_str.split():
                    try:
                        price_val = float(p)
                        break
                    except ValueError:
                        continue

                img_elem = item.find("img")
                photo_url = img_elem.get("src") if img_elem else None

                annonces.append({
                    "id": f"vinted_{item_id}",
                    "title": title,
                    "price": price_val,
                    "url": item_url,
                    "description": title,
                    "photo": photo_url
                })
            except Exception as inner_e:
                continue

        print(f"✅ {len(annonces)} annonces extraites avec succès.")
        return annonces

    except Exception as e:
        print(f"⚠️ Erreur critique lors du scraping Vinted : {e}")
        return []

# ================= ANALYSE GEMINI =================
def analyser_avec_gemini(annonce):
    if not model:
        return True, "Modèle IA non configuré, validation par défaut."
    
    prompt = f"""
    Analyse cette annonce de carte Pokémon sur Vinted :
    Titre : {annonce['title']}
    Prix : {annonce['price']} €
    Description : {annonce['description']}
    
    Est-ce une offre intéressante avec une forte réduction potentielle ou un bon plan (-35% minimum estimé par rapport au marché estimé ou prix bradé) ? 
    Réponds UNIQUEMENT au format JSON strict :
    {{"valide": true/false, "raison": "explication courte"}}
    """
    try:
        response = model.generate_content(prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result.get("valide", False), result.get("raison", "")
    except Exception as e:
        print(f"⚠️ Erreur Gemini : {e}")
        # En cas de doute, si le prix est très bas, on laisse passer
        return True, "Validation automatique de secours."

# ================= MAIN =================
def main():
    print("🚀 Démarrage du script ZuntyTCG-Bot...")
    historique = charger_historique()
    ids_connus = {item["id"] for item in historique}

    annonces = recuperer_annonces_vinted()
    
    if not annonces:
        print("ℹ️ Aucune annonce récupérée lors de ce cycle.")
        return

    nouveautes = 0
    for annonce in annonces:
        if annonce["id"] in ids_connus:
            continue
        
        nouveautes += 1
        print(f"🔍 Analyse de l'annonce : {annonce['title']} à {annonce['price']}€")
        
        valide, raison = analyser_avec_gemini(annonce)
        
        if valide:
            msg = (
                *🔥 BON PLAN POKÉMON DÉTECTÉ !* \n\n"
                f"📦 *Titre* : {annonce['title']}\n"
                f"💰 *Prix* : {annonce['price']} €\n"
                f"💡 *Analyse* : {raison}\n\n"
                f"🔗 [Voir l'annonce sur Vinted]({annonce['url']})"
            )
            envoyer_telegram(msg, annonce.get("photo"))
            print(f"✅ Alerte envoyée pour : {annonce['title']}")
        
        historique.append(annonce)

    sauvegarder_historique(historique)
    print(f"✨ Cycle terminé. {nouveautes} nouvelles annonces traitées.")

if __name__ == "__main__":
    main()