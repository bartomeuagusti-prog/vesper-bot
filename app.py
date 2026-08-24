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
SQUARE_BASE = "https://connect.squareupsandbox.com/v2"

KNOWLEDGE_BASE = """
INFORMACIÓ IMPORTANT DE LA TERRASSA DE L'ULTONIA:
- Vacances: 31 dies naturals a l'any. S'han de sol·licitar amb un mínim de 15 dies d'antelació.
- Canvi de torn: Cal avisar amb 48 hores i ha d'estar aprovat pel responsable.
- Nòmines: Es paguen el dia 1 de cada mes.
- Contractes: Tots els contractes són indefinits llevat que s'indiqui el contrari.
"""

SYSTEM_PROMPT = f"""
Ets Vesper, l'assistent intern del restaurant.
Només respones preguntes sobre horaris, torns, vacances, nòmines i contractes.
Respon sempre en català, de forma clara i amable.
Quan et demanin torns, utilitza la informació de Square que et passo.
No inventis dades.

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}

def get_square_shifts(days=7):
    if not SQUARE_TOKEN:
        return "ERROR: No tinc el token de Square."

    headers = {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Content-Type": "application/json",
        "Square-Version": "2025-05-21"
    }

    # 1. Locations
    loc_r = requests.get(f"{SQUARE_BASE}/locations", headers=headers)
    if loc_r.status_code != 200:
        return f"Error locations ({loc_r.status_code}): {loc_r.text[:250]}"

    locations = loc_r.json().get("locations", [])
    if not locations:
        return "No hi ha locations a Square Sandbox."

    location_ids = [loc["id"] for loc in locations]

    # 2. Torns
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
        return f"Error torns ({r.status_code}): {r.text[:400]}"

    data = r.json()
    shifts = data.get("scheduled_shifts", [])
    
    return f"Square ha respost OK. He trobat {len(shifts)} torns. Resposta parcial: {str(data)[:300]}"

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
    square_info = ""
    if any(w in lower for w in ["torn", "horari", "treballo", "quan treball", "quin dia"]):
        square_info = "\n\n[DADES DE SQUARE]:\n" + get_square_shifts()

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT + square_info}] + user_history[user_id]

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
        say("Ho sento, he tingut un problema tècnic. Prova-ho de nou d'aquí uns minuts.")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
