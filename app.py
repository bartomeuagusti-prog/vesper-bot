import os
import re
from datetime import datetime, timedelta, timezone
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI
import requests

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

SQUARE_TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN")
SQUARE_BASE = "https://connect.squareup.com/v2"

KNOWLEDGE_BASE = """
INFORMACIÓ IMPORTANT DE LA TERRASSA DE L'ULTONIA:
- Vacances: 31 dies naturals a l'any. S'han de sol·licitar amb un mínim de 15 dies d'antelació.
- Canvi de torn: Cal avisar amb 48 hores i ha d'estar aprovat pel responsable.
- Nòmines: Es paguen el dia 1 de cada mes.
- Contractes: Tots els contractes són indefinits llevat que s'indiqui el contrari.
"""

SYSTEM_PROMPT = f"""
Ets Vesper, l'assistent de l'equip de la terrassa de l'Ultonia.
Només respones preguntes sobre horaris, torns, vacances o canvis de torns.
Respon sempre en català, de forma concisa, clara i amable.
No inventis dades i respecta la privacitat dels treballadors.
Davant de faltes de respecte, adverteix que el missatge serà notificat al responsable.

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}
pending_changes = {}

def get_headers():
    return {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Content-Type": "application/json",
        "Square-Version": "2025-05-21"
    }

def get_square_shifts(days=7):
    if not SQUARE_TOKEN:
        return "No tinc el token de Square configurat."

    headers = get_headers()
    loc_r = requests.get(f"{SQUARE_BASE}/locations", headers=headers)
    if loc_r.status_code != 200:
        return f"Error locations: {loc_r.text[:200]}"

    locations = loc_r.json().get("locations", [])
    if not locations:
        return "No he trobat ubicacions."

    location_ids = [loc["id"] for loc in locations]
    now = datetime.now(timezone.utc)
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    body = {
        "query": {
            "filter": {
                "location_ids": location_ids,
                "start": {"start_at": start, "end_at": end}
            }
        },
        "limit": 50
    }

    r = requests.post(f"{SQUARE_BASE}/labor/scheduled-shifts/search", headers=headers, json=body)
    if r.status_code != 200:
        return f"Error consultant torns: {r.status_code}"

    shifts = r.json().get("scheduled_shifts", [])
    if not shifts:
        return "No he trobat torns programats per als propers dies."

    lines = [f"He trobat {len(shifts)} torns els propers {days} dies:\n"]
    for s in shifts[:12]:
        details = s.get("published_shift_details") or s.get("draft_shift_details") or {}
        start_at = details.get("start_at", "")[:16].replace("T", " ")
        end_at = details.get("end_at", "")[:16].replace("T", " ")
        lines.append(f"• {start_at} → {end_at}")

