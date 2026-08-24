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
És només d'ús intern. És un agent professional i tracta assumptes formals de feina. No emojis ni sobre-exclamacions.
Només respones preguntes sobre horaris, torns, vacances o canvis de torns.
Respon sempre en català, de forma concisa, clara i amable. 
Quan et demanin torns, utilitza la informació de Square.
No inventis dades i respecta la privacitat dels treballadors.
Davant de faltes de respecte o comentaris inapropiats, adverteix que el missatge serà notificat al responsable. 

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

user_history = {}
pending_changes = {}  # Guardem canvis pendents de confirmació per usuari

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

    if len(shifts) > 12:
        lines.append(f"\n... i {len(shifts)-12} més.")
    return "\n".join(lines)

def find_team_member(name):
    headers = get_headers()
    r = requests.post(f"{SQUARE_BASE}/team-members/search", headers=headers, json={"query": {"filter": {"status": "ACTIVE"}}})
    if r.status_code != 200:
        return None, f"Error buscant empleats: {r.text[:200]}"

    name_lower = name.lower()
    for m in r.json().get("team_members", []):
        full = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip().lower()
        if name_lower in full:
            return m["id"], None
    return None, f"No he trobat l'empleat/da '{name}'"

def propose_shift_change(team_member_name, date_str, minutes_delta):
    """Prepara un canvi i el guarda pendent de confirmació. Conserva job_id (color)."""
    team_member_id, err = find_team_member(team_member_name)
    if err:
        return err

    headers = get_headers()
    body = {
        "query": {
            "filter": {
                "team_member_ids": [team_member_id],
                "start": {
                    "start_at": f"{date_str}T00:00:00+02:00",
                    "end_at": f"{date_str}T23:59:59+02:00"
                }
            }
        },
        "limit": 5
    }

    r = requests.post(f"{SQUARE_BASE}/labor/scheduled-shifts/search", headers=headers, json=body)
    if r.status_code != 200:
        return f"Error buscant el torn: {r.text[:200]}"

    shifts = r.json().get("scheduled_shifts", [])
    if not shifts:
        return f"No he trobat cap torn de {team_member_name} el dia {date_str}."

    shift = shifts[0]
    details = shift.get("published_shift_details") or shift.get("draft_shift_details") or {}
    old_start = details.get("start_at")
    old_end = details.get("end_at")

    if not old_start:
        return "El torn no té hora d'inici."

    old_dt = datetime.fromisoformat(old_start)
    new_dt = old_dt + timedelta(minutes=minutes_delta)
    new_start = new_dt.isoformat()

    # Guardem el canvi pendent (conservem job_id i location_id → color intacte)
    return {
        "shift_id": shift["id"],
        "team_member_id": team_member_id,
        "location_id": details.get("location_id"),
        "job_id": details.get("job_id"),  # Important: conservem el job_id = color
        "old_start": old_start,
        "new_start": new_start,
        "old_end": old_end,
        "version": details.get("version", 1),
        "name": team_member_name,
        "date": date_str
    }

def apply_pending_change(change):
    """Aplica el canvi conservant el job_id (color)"""
    headers = get_headers()
    update_body = {
        "scheduled_shift": {
            "draft_shift_details": {
                "team_member_id": change["team_member_id"],
                "location_id": change["location_id"],
                "job_id": change["job_id"],  # ← color es manté
                "start_at": change["new_start"],
                "end_at": change["old_end"],
                "version": change["version"]
            }
        }
    }

    r = requests.put(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{change['shift_id']}",
        headers=headers,
        json=update_body
    )

    if r.status_code in [200, 201]:
        return (
            f"✅ Canvi aplicat correctament.\n"
            f"{change['name']} – {change['date']}\n"
            f"Abans: {change['old_start'][11:16]}\n"
            f"Ara:   {change['new_start'][11:16]}\n"
            f"(El color del rol s'ha mantingut)"
        )
    else:
        return f"Error aplicant el canvi: {r.status_code}\n{r.text[:300]}"

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

    # Confirmació de canvi pendent
    if user_id in pending_changes and any(w in lower for w in ["sí", "si", "confirma", "d'acord", "ok", "aplica"]):
        result = apply_pending_change(pending_changes[user_id])
        del pending_changes[user_id]
        say(result)
        return

    if user_id in pending_changes and any(w in lower for w in ["no", "cancel·la", "cancela"]):
        del pending_changes[user_id]
        say("Canvi cancel·lat.")
        return

    # Detecció simple de petició de canvi (exemples: "avança 30 minuts el torn d'Inma del 26")
    if any(w in lower for w in ["avançar", "avança", "canviar", "modificar", "endreçar"]) and any(c.isdigit() for c in text):
        # De moment deixem que Grok interpreti i proposi
        pass

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
