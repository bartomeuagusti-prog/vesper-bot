import os
from datetime import datetime, timedelta
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
SQUARE_BASE = "https://connect.squareupsandbox.com/v2"  # Sandbox

KNOWLEDGE_BASE = """
INFORMACIÓ IMPORTANT DE LA TERRASSA DE L'ULTONIA:
- Vacances: 31 dies naturals a l'any. S'han de sol·licitar amb un mínim de 15 dies d'antelació.
- Canvi de torn: Cal avisar amb 48 hores i ha d'estar aprovat pel responsable.
- Nòmines: Es paguen el dia 1 de cada mes.
- Contractes: Tots els contractes són indefinits llevat que s'indiqui el contrari.
"""

SYSTEM_PROMPT = f"""
Ets Vesper, l'assistent a l'equip de la terrassa de l'Ultonia. 
Només respones preguntes sobre horaris, torns, vacances i registre laboral. No sobre augments de sou, cobraments o augments (si ho fan dirigeix-los al manager). 
Respon sempre en català, de forma concisa, clara i amable.
Si et pregunten el seu torn personal, demana el nom complet del treballador i després consulta Square.
No inventis dades.

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}

def get_square_shifts(team_member_name=None, days=7):
    """Consulta torns a Square (versió simple)"""
    if not SQUARE_TOKEN:
        return "No tinc connexió amb Square configurada."

    headers = {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Content-Type": "application/json",
        "Square-Version": "2025-05-21"
    }

    # Primer busquem el team member per nom (si ens el donen)
    team_member_id = None
    if team_member_name:
        search_body = {
            "query": {
                "filter": {
                    "status": "ACTIVE"
                }
            }
        }
        r = requests.post(f"{SQUARE_BASE}/team-members/search", headers=headers, json=search_body)
        if r.status_code == 200:
            members = r.json().get("team_members", [])
            for m in members:
                full_name = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip().lower()
                if team_member_name.lower() in full_name:
                    team_member_id = m["id"]
                    break

    # Busquem torns dels propers dies
    start = datetime.utcnow().isoformat() + "Z"
    end = (datetime.utcnow() + timedelta(days=days)).isoformat() + "Z"

    body = {
        "query": {
            "filter": {
                "start": {
                    "start_at": start,
                    "end_at": end
                },
                "scheduled_shift_statuses": ["PUBLISHED"]
            }
        },
        "limit": 50
    }

    if team_member_id:
        body["query"]["filter"]["team_member_ids"] = [team_member_id]

    r = requests.post(f"{SQUARE_BASE}/labor/scheduled-shifts/search", headers=headers, json=body)

    if r.status_code != 200:
        return f"Error consultant Square: {r.text[:200]}"

    shifts = r.json().get("scheduled_shifts", [])
    if not shifts:
        return "No he trobat torns publicats per als propers dies."

    result = []
    for s in shifts:
        details = s.get("published_shift_details") or s.get("draft_shift_details") or {}
        start_at = details.get("start_at", "")[:16].replace("T", " ")
        end_at = details.get("end_at", "")[:16].replace("T", " ")
        result.append(f"- {start_at} → {end_at}")

    return "Torns trobats:\n" + "\n".join(result[:10])

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

    # Si parla de torns personals, intentem consultar Square
    lower = text.lower()
    square_info = ""
    if any(word in lower for word in ["torn", "horari", "quin dia treballo", "quan treballo"]):
        # Busquem si ha dit un nom
        square_info = "\n\n[Informació de Square]:\n" + get_square_shifts()

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT + square_info}] + user_history[user_id]

        response = client.chat.completions.create(
            model="grok-4.6",
            messages=messages,
            temperature=0.3
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
