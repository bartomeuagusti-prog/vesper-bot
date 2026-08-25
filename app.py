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
No inventis dades i respecta la privacitat dels treballadors.
Davant de faltes de respecte, adverteix que el missatge serà notificat al responsable.

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}

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

    lines = [f"He trobat {len(shifts)} torns els propers {days} dies:\n"]
    for s in shifts[:12]:
        details = s.get("published_shift_details") or s.get("draft_shift_details") or {}
        start_at = details.get("start_at", "")[:16].replace("T", " ")
        end_at = details.get("end_at", "")[:16].replace("T", " ")
        lines.append(f"• {start_at} → {end_at}")

    if len(shifts) > 12:
        lines.append(f"\n... i {len(shifts)-12} més.")

    return "\n".join(lines)

def update_inma_shift():
    """Avança 30 minuts l'entrada del torn d'Inma Martin del 26/08/2026"""
    if not SQUARE_TOKEN:
        return "No tinc el token de Square configurat."

    headers = get_headers()

    # 1. Buscar Inma Martin
    search_body = {"query": {"filter": {"status": "ACTIVE"}}}
    r = requests.post(f"{SQUARE_BASE}/team-members/search", headers=headers, json=search_body)
    if r.status_code != 200:
        return f"Error buscant empleats: {r.text[:250]}"

    team_member_id = None
    for m in r.json().get("team_members", []):
        full_name = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip().lower()
        if "inma" in full_name and "martin" in full_name:
            team_member_id = m["id"]
            break

    if not team_member_id:
        return "No he trobat l'empleada Inma Martin."

    # 2. Buscar el torn del 26/08
    body = {
        "query": {
            "filter": {
                "team_member_ids": [team_member_id],
                "start": {
                    "start_at": "2026-08-26T00:00:00+02:00",
                    "end_at": "2026-08-26T23:59:59+02:00"
                }
            }
        },
        "limit": 10
    }

    r = requests.post(f"{SQUARE_BASE}/labor/scheduled-shifts/search", headers=headers, json=body)
    if r.status_code != 200:
        return f"Error buscant el torn: {r.text[:250]}"

    shifts = r.json().get("scheduled_shifts", [])
    if not shifts:
        return "No he trobat cap torn d'Inma Martin el dia 26/08/2026."

    shift = shifts[0]
    shift_id = shift["id"]
    details = shift.get("published_shift_details") or shift.get("draft_shift_details") or {}

    old_start = details.get("start_at")
    old_end = details.get("end_at")
    version = details.get("version", 1)

    if not old_start:
        return "El torn no té hora d'inici."

    # 3. Avançar 30 minuts
    old_dt = datetime.fromisoformat(old_start)
    new_dt = old_dt - timedelta(minutes=30)
    new_start = new_dt.isoformat()

    # 4. Actualitzar (conservem job_id → color)
    update_body = {
        "scheduled_shift": {
            "draft_shift_details": {
                "team_member_id": team_member_id,
                "location_id": details.get("location_id"),
                "job_id": details.get("job_id"),
                "start_at": new_start,
                "end_at": old_end,
                "version": version
            }
        }
    }

    r = requests.put(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{shift_id}",
        headers=headers,
        json=update_body
    )

    if r.status_code not in [200, 201]:
        return f"Error actualitzant el torn: {r.status_code}\n{r.text[:300]}"

    # 5. Publicar
    pub_r = requests.post(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{shift_id}/publish",
        headers=headers,
        json={}
    )

    if pub_r.status_code not in [200, 201]:
        return (
            f"He actualitzat el torn però no he pogut publicar-lo.\n"
            f"Error: {pub_r.status_code}"
        )

    return (
        f"Fet! He avançat 30 minuts l'entrada d'Inma Martin el 26/08 i l'he publicat.\n"
        f"Abans: {old_start}\n"
        f"Ara:   {new_start}"
    )

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

    # Ordre específica d'Inma (prioritat màxima)
    if "inma" in lower:
        result = update_inma_shift()
        say(result)
        return

    # Consulta de torns
    if any(w in lower for w in ["torn", "horari", "treballo", "quan treball", "quin dia", "torns"]):
        result = get_square_shifts()
        say(result)
        return

    # Resposta normal amb Grok
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
        say("Ho sento, he tingut un problema tècnic. Prova-ho de nou d'aquí uns minuts.")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
