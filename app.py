import os
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
Quan et demanin torns, utilitza la informació de Square.
No inventis dades i sobretot respecta la privacitat de qualsevol treballador. 
Davant de faltes de respecte o abusos, adverteix que el missatge serà notificat al responsable. 

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}

def get_square_shifts(days=7):
    if not SQUARE_TOKEN:
        return "No tinc el token de Square configurat."

    headers = {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Content-Type": "application/json",
        "Square-Version": "2025-05-21"
    }

    # Locations
    loc_r = requests.get(f"{SQUARE_BASE}/locations", headers=headers)
    if loc_r.status_code != 200:
        return f"Error locations: {loc_r.text[:200]}"

    locations = loc_r.json().get("locations", [])
    if not locations:
        return "No he trobat ubicacions."

    location_ids = [loc["id"] for loc in locations]

    # Torns
    now = datetime.now(timezone.utc)
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")

    body = {
        "query": {
            "filter": {
                "location_ids": location_ids,
                "start": {
                    "start_at": start,
                    "end_at": end
                }
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

    # Format net
    lines = [f"He trobat {len(shifts)} torns els propers {days} dies:\n"]
    for s in shifts[:12]:  # mostrem només els 12 primers
        details = s.get("published_shift_details") or s.get("draft_shift_details") or {}
        start_at = details.get("start_at", "")[:16].replace("T", " ")
        end_at = details.get("end_at", "")[:16].replace("T", " ")
        lines.append(f"• {start_at} → {end_at}")

    if len(shifts) > 12:
        lines.append(f"\n... i {len(shifts)-12} més.")

    return "\n".join(lines)
@app.event("message")
def handle_dm(event, say, logger):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return

    user_id = event["user"]
    text = event.get("text", "").strip()
    if not text:
        return

    if user_id not in user_history:
        user_history[user_id] = []
    user_history[user_id].append({"role": "user", "content": text})
    user_history[user_id] = user_history[user_id][-12:]

    lower = text.lower()
    if any(w in lower for w in ["torn", "horari", "treballo", "quan treball", "quin dia", "torns"]):
        # Resposta directa temporal per veure què retorna Square
        result = get_square_shifts()
        say(result)
        return

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + user_history[user_id]

        response = client.chat.completions.create(
            model="grok-4.6",
            messages=messages,
            temperature=0.2
        )

        answer = response.choices[0].message.content
        user_history[user_id].append({"role": "assistant", "content": answer})
        say(answer)

    except Exception as e:
        logger.error(f"Error: {e}")
        say("Ho sento jefe, he tingut un problema tècnic. Prova-ho de nou d'aquí uns minuts.")
        
if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
