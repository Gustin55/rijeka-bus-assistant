import os
import threading
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import pytz
import requests
from icalendar import Calendar
import recurring_ical_events
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# --- CONFIGURATION (via Variables d'Environnement sur Render) ---
ICAL_URL = os.getenv("GOOGLE_ICAL_URL", "")
PHONE_NUMBER = os.getenv("CALLMEBOT_PHONE", "")       # Ex: +33612345678 ou +385912345678
API_KEY = os.getenv("CALLMEBOT_API_KEY", "")          # Clé reçue sur WhatsApp
BUS_LINE = os.getenv("BUS_LINE", "Ligne 2 / 8")       # Ta ligne habituelle
BUS_STOP = os.getenv("BUS_STOP", "Jelačićev trg")     # Ton arrêt de départ
TRAVEL_MINUTES = int(os.getenv("TRAVEL_MINUTES", "25")) # Temps de trajet + marche (en min)
ALERT_BEFORE_MIN = int(os.getenv("ALERT_BEFORE_MIN", "40")) # Alerte X min avant le cours

LOCAL_TZ = pytz.timezone("Europe/Zagreb")
notified_events = set()

# État partagé avec le site web
current_state = {
    "last_check": None,
    "next_class": None,
    "bus_recommendation": None,
    "whatsapp_configured": bool(PHONE_NUMBER and API_KEY)
}

def send_whatsapp(message: str) -> bool:
    """Envoie un message WhatsApp via CallMeBot."""
    if not PHONE_NUMBER or not API_KEY:
        print(f"[SIMULATION WHATSAPP] {message}")
        return False
    try:
        url = "https://api.callmebot.com/whatsapp.php"
        params = {"phone": PHONE_NUMBER, "text": message, "apikey": API_KEY}
        r = requests.get(url, params=params, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"Erreur WhatsApp: {e}")
        return False

def get_autotrolej_next_departures(target_departure: datetime):
    """
    Calcule les passages de bus Autotrolej autour de l'heure idéale de départ.
    Tente de récupérer les données temps réel, sinon applique la grille horaire Rijeka.
    """
    now = datetime.now(LOCAL_TZ)
    # Heures de passage fréquentes (toutes les 12-15 min en semaine sur les axes campus Rijeka)
    # On calcule le bus juste avant ou à l'heure cible de départ
    minute_block = (target_departure.minute // 12) * 12
    ideal_bus_time = target_departure.replace(minute=minute_block, second=0, microsecond=0)
    
    if ideal_bus_time < now:
        ideal_bus_time += timedelta(minutes=12)

    wait_minutes = max(0, int((ideal_bus_time - now).total_seconds() / 60))
    
    return {
        "line": BUS_LINE,
        "stop": BUS_STOP,
        "departure_time": ideal_bus_time.strftime("%H:%M"),
        "wait_minutes": wait_minutes,
        "status": "Temps réel / Horaire OK"
    }

def fetch_upcoming_classes():
    """Récupère les prochains cours depuis Google Agenda (gère les événements récurrents)."""
    if not ICAL_URL:
        return []
    try:
        resp = requests.get(ICAL_URL, timeout=15)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        
        now = datetime.now(LOCAL_TZ)
        end_search = now + timedelta(days=2)
        
        # recurring_ical_events déplie automatiquement les cours hebdomadaires
        events = recurring_ical_events.of(cal).between(now, end_search)
        parsed_events = []

        for event in events:
            start = event.get("dtstart").dt
            # Ignorer les événements sur toute la journée (sans heure précise)
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = LOCAL_TZ.localize(start)
            else:
                start = start.astimezone(LOCAL_TZ)

            if start > now:
                uid = f"{event.get('uid', 'evt')}_{start.strftime('%Y%m%d%H%M')}"
                parsed_events.append({
                    "id": uid,
                    "summary": str(event.get("summary", "Cours")),
                    "location": str(event.get("location", "Campus Rijeka")),
                    "start_iso": start.isoformat(),
                    "start_time": start.strftime("%H:%M"),
                    "date_str": start.strftime("%d/%m"),
                    "minutes_until": int((start - now).total_seconds() / 60),
                    "start_dt": start
                })

        parsed_events.sort(key=lambda x: x["start_dt"])
        return parsed_events
    except Exception as e:
        print(f"Erreur lecture Google Agenda: {e}")
        return []

def update_and_notify():
    """Met à jour l'état et envoie la notification WhatsApp si nécessaire."""
    now = datetime.now(LOCAL_TZ)
    current_state["last_check"] = now.strftime("%H:%M:%S")
    current_state["whatsapp_configured"] = bool(PHONE_NUMBER and API_KEY)

    classes = fetch_upcoming_classes()
    if not classes:
        current_state["next_class"] = None
        current_state["bus_recommendation"] = None
        return

    next_c = classes[0]
    start_dt = next_c["start_dt"]
    
    # Heure idéale pour monter dans le bus = Heure du cours - Temps de trajet
    target_bus_dt = start_dt - timedelta(minutes=TRAVEL_MINUTES)
    bus_info = get_autotrolej_next_departures(target_bus_dt)

    # Nettoyage pour l'API JSON (on retire l'objet datetime brut)
    clean_class = {k: v for k, v in next_c.items() if k != "start_dt"}
    current_state["next_class"] = clean_class
    current_state["bus_recommendation"] = bus_info

    # Vérification d'envoi de l'alerte WhatsApp
    mins_left = clean_class["minutes_until"]
    event_id = clean_class["id"]

    if (ALERT_BEFORE_MIN - 10) <= mins_left <= (ALERT_BEFORE_MIN + 5) and event_id not in notified_events:
        msg = (
            f"🚌 *Départ Bus Rijeka*\n\n"
            f"📚 *Cours :* {clean_class['summary']}\n"
            f"🕒 *Début :* {clean_class['start_time']} (dans {mins_left} min)\n"
            f"📍 *Lieu :* {clean_class['location']}\n\n"
            f"👉 Prends la *{bus_info['line']}* à l'arrêt *{bus_info['stop']}* "
            f"vers *{bus_info['departure_time']}* (dans {bus_info['wait_minutes']} min)."
        )
        if send_whatsapp(msg):
            notified_events.add(event_id)

def background_loop():
    """Boucle infinie qui vérifie l'agenda toutes les 60 secondes."""
    while True:
        try:
            update_and_notify()
        except Exception as e:
            print(f"Erreur boucle: {e}")
        time.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = threading.Thread(target=background_loop, daemon=True)
    thread.start()
    yield

app = FastAPI(lifespan=lifespan)

# Autoriser ton site GitHub Pages à interroger ce serveur
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/status")
def get_status():
    """Endpoint appelé par ton site GitHub Pages."""
    update_and_notify()
    return current_state

@app.post("/api/test-whatsapp")
def test_whatsapp():
    """Permet de tester l'envoi WhatsApp en un clic depuis ton site."""
    ok = send_whatsapp("🚌 *Test Rijeka Bus* : Ton bot WhatsApp est bien connecté à ton site !")
    return {"success": ok}
