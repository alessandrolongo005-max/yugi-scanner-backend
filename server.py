import os
import random
import requests
import base64
import re
import cv2
import numpy as np
import json
from io import BytesIO
from PIL import Image, ImageOps
from fastapi import FastAPI, APIRouter
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
import uvicorn
from google import genai

# --- CONFIGURAZIONE GEMINI (NUOVO SDK) ---
# La chiave viene caricata in modo sicuro dalla "Cassaforte" (Environment Variables)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

# Inizializzazione App
app = FastAPI(title="YuGi DeckBuilder PRO - DEFINITIVO")
api = APIRouter()

YGOPRO_API = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
EXTRA_KEYWORDS = ["Fusion", "Synchro", "XYZ", "Link"]

# --- DATABASE RICETTE PERFETTE ---
PERFECT_DECKS = {
    "neos": {
        "strategy": "Mazzo HERO/Neos Competitivo. Sfrutta Stratos e Vision HERO.",
        "main": {
            "89943723": 2, "10476868": 3, "41517789": 2, "27515504": 3, "42941100": 2, 
            "35897087": 2, "9411399": 2, "83965310": 1, "1516510": 1, "420796": 2, 
            "64180927": 1, "14558127": 3, "8949584": 3, "84139827": 3, "63035430": 3, 
            "48996569": 2, "31557738": 1, "32807846": 1, "24224830": 1, "10045474": 3
        },
        "extra": {
            "56024106": 2, "6061630": 1, "1811613": 1, "60461804": 1, "58481572": 2,
            "81193231": 2, "31872019": 1, "25397306": 1, "96843555": 1, "24641528": 1,
            "40854197": 1, "79915591": 1
        }
    }
}

GENERIC_STAPLES_MAIN = {
    "14558127": 3, "10045474": 3, "97268402": 3, "24224830": 1, "27204311": 3, 
    "48680970": 2, "84211599": 3, "87008374": 1, "18144506": 1, "83764718": 1, 
    "24207889": 2, "59438930": 2
}

GENERIC_STAPLES_EXTRA = {
    "86066372": 1, "65741786": 1, "98127546": 1, "4280258": 1, 
    "90448279": 1, "41999284": 1, "38342335": 1, "30743600": 1
}

class DeckInput(BaseModel):
    mode: str = "Basic"
    tema: str = ""

# NUOVO MODELLO: DECK BUILDER PRO
class ProDeckInput(BaseModel):
    tema: str

class ScannerInput(BaseModel):
    base64_image: str

# MODELLO PER ARRICCHIMENTO CARTE
class EnrichInput(BaseModel):
    cardName: str
    existingData: dict

# NUOVO MODELLO PER PRODOTTI SIGILLATI
class SealedInput(BaseModel):
    query: str

def assegna_destinazione_carta(card_type):
    for keyword in EXTRA_KEYWORDS:
        if keyword in card_type: return "EXTRA"
    return "MAIN"

def get_price(c_data):
    try: return float(c_data.get("card_prices", [{}])[0].get("cardmarket_price", 0.10))
    except: return 0.10

@app.get("/")
async def root_check(): return {"status": "OK", "message": "YuGi DeckBuilder PRO - PONTE ATTIVO"}

@api.get("/")
async def api_check(): return {"status": "OK"}

# --- ROTTA DECK BUILDER CLASSICO ---
@api.post("/deck/generate")
async def generate_deck(inp: DeckInput):
    tema_clean = inp.tema.lower().strip()
    is_comp = inp.mode == "Competitivo"
    for key, recipe in PERFECT_DECKS.items():
        if key in tema_clean or (key == "neos" and "eroe" in tema_clean):
            return await build_perfect_deck(recipe)
    return await build_smart_deck(inp.tema, is_comp)

# --- ROTTA DECK BUILDER PRO (TOURNAMENT READY) ---
@api.post("/deck/generate-pro")
async def generate_pro_deck(inp: ProDeckInput):
    try:
        # 1. Chiediamo a Gemini la lista da torneo in JSON
        prompt = f"""
        Sei un Campione del Mondo di Yu-Gi-Oh! TCG. 
        Crea la decklist competitiva perfetta (Tier 1/Tournament Ready) aggiornata al meta attuale per l'archetipo "{inp.tema}".
        Includi le migliori staple e handtraps del formato (es. Ash Blossom, Nibiru, ecc. a seconda della sinergia).
        Rispondi SOLO in formato JSON puro, nessuna formattazione markdown, nessun testo fuori dal JSON.
        I nomi delle carte DEVONO essere i nomi ESATTI in inglese.
        Struttura obbligatoria:
        {{
            "strategy": "Spiega in 3 righe come si gioca il mazzo nel meta attuale.",
            "main": {{"Nome Esatto Carta": quantita, "Altra Carta": quantita}},
            "extra": {{"Nome Esatto Carta": quantita}}
        }}
        IMPORTANTE: Il campo "strategy" deve contenere una spiegazione tattica dettagliata del mazzo, ma DEVE essere scritta ESCLUSIVAMENTE in lingua ITALIANA. Scrivi tutto il testo su una SINGOLA RIGA continua, senza MAI andare a capo.
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        
        text = response.text.replace('```json', '').replace('```', '').strip()
        text = text.replace('\n', ' ').replace('\r', '')
        deck_data = json.loads(text, strict=False)
        
        main_deck = []
        extra_deck = []
        missing_list = []
        costo = 0.0

        def fetch_and_append(card_name, qty, is_extra):
            nonlocal costo
            try:
                res = requests.get(YGOPRO_API, params={"name": card_name})
                if res.status_code != 200:
                    res = requests.get(YGOPRO_API, params={"fname": card_name})
                
                if res.status_code == 200 and "data" in res.json():
                    c = res.json()["data"][0]
                    prezzo = get_price(c)
                    card_obj = {
                        "passcode": str(c["id"]),
                        "nome_carta": c["name"],
                        "immagine": c.get("card_images", [{}])[0].get("image_url", ""),
                        "quantita": qty,
                        "posseduta": 0,
                        "prezzo_unitario": prezzo
                    }
                    if is_extra:
                        extra_deck.append(card_obj)
                    else:
                        main_deck.append(card_obj)
                    
                    costo += round(qty * prezzo, 2)
                    missing_list.append({**card_obj, "mancanti": qty, "subtotale": round(qty * prezzo, 2)})
            except Exception as e:
                print(f"Errore recupero carta {card_name}: {e}")

        for nome, q in deck_data.get("main", {}).items():
            fetch_and_append(nome, q, False)
            
        for nome, q in deck_data.get("extra", {}).items():
            fetch_and_append(nome, q, True)

        return {
            "main_deck": main_deck, 
            "extra_deck": extra_deck, 
            "missing": missing_list, 
            "costo_totale_stimato": round(costo, 2), 
            "strategy": deck_data.get("strategy", "Nessuna strategia fornita."), 
            "ok": True
        }

    except Exception as e:
        return {"ok": False, "strategy": f"Errore AI: {str(e)}", "main_deck": [], "extra_deck": [], "missing": [], "costo_totale_stimato": 0}

# --- NUOVA ROTTA: PRODOTTI SIGILLATI (CON IMMAGINI AUTOMATICHE DA YUGIPEDIA) ---
@api.post("/sealed/search")
async def search_sealed(inp: SealedInput):
    try:
        # 1. Chiediamo a Gemini di normalizzare il nome per le API di Yugipedia e stimare il prezzo
        prompt = f"""
        Analizza questo prodotto sigillato di Yu-Gi-Oh! (box, tin, structure deck, ecc.): "{inp.query}".
        Restituisci SOLO un JSON puro con:
        - "name": Nome ufficiale in inglese (es. "Strike of Neos", "Legend of Blue Eyes White Dragon").
        - "estimated_price": Prezzo stimato attuale in EUR sul mercato dei collezionisti (solo il numero).
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        text = response.text.replace('```json', '').replace('```', '').strip()
        text = text.replace('\n', ' ').replace('\r', '')
        data = json.loads(text, strict=False)
        
        official_name = data.get("name", inp.query)
        
        # 2. Peschiamo l'immagine ufficiale direttamente da Yugipedia in background
        image_url = ""
        try:
            yugi_api = "https://yugipedia.com/api.php"
            params = {
                "action": "query",
                "prop": "pageimages",
                "titles": official_name,
                "format": "json",
                "pithumbsize": 500
            }
            res = requests.get(yugi_api, params=params).json()
            pages = res.get("query", {}).get("pages", {})
            for page_id, page_data in pages.items():
                if "thumbnail" in page_data:
                    image_url = page_data["thumbnail"]["source"]
                    break
        except Exception:
            pass
            
        return {
            "found": True,
            "name": official_name,
            "estimated_price": data.get("estimated_price", 0),
            "image_url": image_url,
            "type": "Sealed"
        }
    except Exception as e:
        return {"found": False, "message": str(e)}

# --- ROTTA ARRICCHIMENTO GEMINI ---
@api.post("/gemini/enrich")
async def enrich_card(inp: EnrichInput):
    try:
        prompt = f"""
        Sei un esperto del gioco di carte Yu-Gi-Oh!. 
        Analizza questa carta: "{inp.cardName}". 
        Dati parziali API: {str(inp.existingData)}.
        Restituisci un oggetto JSON con: 'effect', 'rarity', 'description'.
        Rispondi SOLO con il JSON puro.
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(text)
    except Exception as e:
        return {"error": str(e)}

async def build_perfect_deck(recipe):
    tutti_gli_id = list(recipe["main"].keys()) + list(recipe["extra"].keys())
    res = requests.get(YGOPRO_API, params={"id": ",".join(tutti_gli_id)})
    data = res.json().get("data", [])
    dizionario_carte = {str(c["id"]): c for c in data}
    main_deck, extra_deck, missing_list, costo = [], [], [], 0.0

    def process_card(card_id, qty):
        nonlocal costo
        if card_id in dizionario_carte:
            c = dizionario_carte[card_id]
            prezzo = get_price(c)
            card_obj = {"passcode": card_id, "nome_carta": c["name"], "immagine": c.get("card_images", [{}])[0].get("image_url", ""), "quantita": qty, "posseduta": 0, "prezzo_unitario": prezzo}
            (extra_deck if assegna_destinazione_carta(c["type"]) == "EXTRA" else main_deck).append(card_obj)
            costo += round(qty * prezzo, 2)
            missing_list.append({**card_obj, "mancanti": qty, "subtotale": round(qty * prezzo, 2)})

    for cid, qty in recipe["main"].items(): process_card(cid, qty)
    for cid, qty in recipe["extra"].items(): process_card(cid, qty)
    return {"main_deck": main_deck, "extra_deck": extra_deck, "missing": missing_list, "costo_totale_stimato": round(costo, 2), "strategy": recipe["strategy"], "ok": True}

async def build_smart_deck(tema, is_comp):
    res = requests.get(YGOPRO_API, params={"archetype": tema})
    data = res.json()
    if "data" not in data:
        res = requests.get(YGOPRO_API, params={"fname": tema})
        data = res.json()
        if "data" not in data:
            res = requests.get(YGOPRO_API, params={"fname": tema, "language": "it"})
            data = res.json()
    if "data" not in data: return {"main_deck": [], "extra_deck": [], "missing": [], "costo_totale_stimato": 0, "strategy": f"Nessun risultato per '{tema}'.", "ok": False}

    pool = data["data"]
    main_deck, extra_deck, carte_nel_main, carte_nel_extra = [], [], 0, 0

    def append_card(c_data, quantita, is_extra):
        nonlocal carte_nel_main, carte_nel_extra
        deck = extra_deck if is_extra else main_deck
        max_limit = 15 if is_extra else 40
        current_count = carte_nel_extra if is_extra else carte_nel_main
        
        if current_count >= max_limit: return
        for carta in deck:
            if carta["passcode"] == str(c_data["id"]):
                spazio = min(quantita, 3 - carta["quantita"], max_limit - current_count)
                if spazio > 0:
                    carta["quantita"] += spazio
                    if is_extra: carte_nel_extra += spazio 
                    else: carte_nel_main += spazio
                return
        spazio = min(quantita, 3, max_limit - current_count)
        if spazio > 0:
            deck.append({"passcode": str(c_data["id"]), "nome_carta": c_data["name"], "immagine": c_data.get("card_images", [{}])[0].get("image_url", ""), "quantita": spazio, "posseduta": 0, "prezzo_unitario": get_price(c_data)})
            if is_extra: carte_nel_extra += spazio 
            else: carte_nel_main += spazio

    for c in pool: append_card(c, 2 if is_comp else 1, True) if assegna_destinazione_carta(c["type"]) == "EXTRA" else append_card(c, 3 if is_comp else 2, False)

    if carte_nel_main < 40:
        try:
            staples = {str(c["id"]): c for c in requests.get(YGOPRO_API, params={"id": ",".join(GENERIC_STAPLES_MAIN.keys())}).json().get("data", [])}
            for sid, sqty in GENERIC_STAPLES_MAIN.items():
                if sid in staples: append_card(staples[sid], sqty, False)
        except: pass

    if carte_nel_extra < 15:
        try:
            staples_ex = {str(c["id"]): c for c in requests.get(YGOPRO_API, params={"id": ",".join(GENERIC_STAPLES_EXTRA.keys())}).json().get("data", [])}
            for eid, eqty in GENERIC_STAPLES_EXTRA.items():
                if eid in staples_ex: append_card(staples_ex[eid], eqty, True)
        except: pass

    if carte_nel_main < 40 and main_deck:
        for carta in main_deck:
            aggiunta = min(3 - carta["quantita"], 40 - carte_nel_main)
            if aggiunta > 0: carta["quantita"] += aggiunta; carte_nel_main += aggiunta;

    missing_list, costo = [], 0.0
    for carta in main_deck + extra_deck:
        mancanti = max(0, carta["quantita"] - carta["posseduta"])
        if mancanti > 0: costo += round(mancanti * carta["prezzo_unitario"], 2); missing_list.append({**carta, "mancanti": mancanti, "subtotale": round(mancanti * carta["prezzo_unitario"], 2)})

    return {"main_deck": main_deck, "extra_deck": extra_deck, "missing": missing_list, "costo_totale_stimato": round(costo, 2), "strategy": f"Mazzo generato. Main: {carte_nel_main}, Extra: {carte_nel_extra}.", "ok": True}

# --- ROTTA SCANNER DEFINITIVA: INTEGRAZIONE GEMINI VISION + SET CODE + IMMAGINI CORRETTE ---
@api.post("/scanner/recognize")
async def recognize_card(inp: ScannerInput):
    try:
        image_data = base64.b64decode(inp.base64_image)
        img_pil = Image.open(BytesIO(image_data))
        img_pil = ImageOps.exif_transpose(img_pil)
        
        prompt_scanner = """
        Sei un esperto classificatore di carte Yu-Gi-Oh!.
        Analizza l'immagine della carta fornita e individua DUE informazioni fondamentali:
        1. "passcode": Il codice numerico a 8 cifre stampato nell'angolo in basso a sinistra.
        2. "set_code": Il codice dell'espansione alfanumerico stampato appena sotto l'immagine della carta a destra (es. LOB-EN001, SDY-046, CT13-IT008). Se non riesci a leggerlo, lascia il campo vuoto "".

        Rispondi SOLO in formato JSON puro, nessuna formattazione markdown. Esempio esatto:
        {"passcode": "89631139", "set_code": "LOB-EN001"}
        """
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[prompt_scanner, img_pil]
        )
        
        text = response.text.replace('```json', '').replace('```', '').strip()
        text = text.replace('\n', ' ').replace('\r', '')
        
        try:
            ai_data = json.loads(text, strict=False)
            raw_passcode = str(ai_data.get("passcode", ""))
            set_code_ai = str(ai_data.get("set_code", "")).upper().strip()
        except Exception:
            raw_passcode = response.text.strip()
            set_code_ai = ""

        passcode = re.sub(r'[^0-9]', '', raw_passcode)
        
        if len(passcode) < 7:
            return {"found": False, "message": f"Gemini non ha trovato un codice valido."}
            
        res = requests.get(YGOPRO_API, params={"id": passcode, "language": "it"})
        data = res.json()
        if "data" not in data:
            data = requests.get(YGOPRO_API, params={"id": passcode}).json()
            
        if "data" in data:
            c = data["data"][0]
            
            # Match per l'espansione specifica
            best_set = None
            if set_code_ai and "card_sets" in c:
                for s in c["card_sets"]:
                    if set_code_ai in str(s.get("set_code", "")).upper():
                        best_set = s
                        break
            
            if not best_set and "card_sets" in c:
                best_set = c["card_sets"][0]
            elif "card_sets" not in c:
                best_set = {}
                
            rarita = best_set.get("set_rarity", "Comune")
            edizione = best_set.get("set_name", "N/A")
            
            set_price = best_set.get("set_price")
            if set_price and float(set_price) > 0:
                prezzo_finale = float(set_price)
            else:
                prezzo_finale = get_price(c)

            # Match esatto dell'immagine dell'artwork corretto (utile per Alt Art)
            target_image_url = ""
            for img in c.get("card_images", []):
                if str(img.get("id")) == str(passcode):
                    target_image_url = img.get("image_url", "")
                    break
            
            # Fallback alla prima immagine disponibile se non troviamo l'ID esatto
            if not target_image_url and c.get("card_images"):
                target_image_url = c["card_images"][0].get("image_url", "")

            return {
                "found": True, 
                "passcode": passcode, 
                "name": c["name"],
                "price": prezzo_finale, 
                "image": target_image_url,
                "rarita": rarita,
                "edizione": edizione,
                "archetipo": c.get("archetype", "N/A")
            }
        else:
            return {"found": False, "message": f"Codice {passcode} inesistente nel database YGOPRO."}
            
    except Exception as e:
        return {"found": False, "message": f"Errore server: {str(e)}"}

app.include_router(api, prefix="/api")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
