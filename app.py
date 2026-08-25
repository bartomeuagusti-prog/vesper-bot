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

    if len(shifts) > 12:
        lines.append(f"\n... i {len(shifts)-12} més.")
    return "\n".join(lines)

def find_team_member(name):
    headers = get_headers()
    r = requests.post(
        f"{SQUARE_BASE}/team-members/search",
        headers=headers,
        json={"query": {"filter": {"status": "ACTIVE"}}}
    )
    if r.status_code != 200:
        return None, None

    name_lower = name.lower().strip()
    for m in r.json().get("team_members", []):
        full = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip().lower()
        if name_lower in full or full in name_lower:
            return m["id"], f"{m.get('given_name', '')} {m.get('family_name', '')}".strip()
    return None, None

def get_date_from_day(day_name):
    """Converteix 'dijous', 'divendres'... a data YYYY-MM-DD de la setmana actual o següent"""
    days_map = {
        "dilluns": 0, "dimarts": 1, "dimecres": 2, "dijous": 3,
        "divendres": 4, "dissabte": 5, "diumenge": 6
    }
    today = datetime.now().date()
    target_weekday = days_map.get(day_name.lower())
    if target_weekday is None:
        return None

    days_ahead = target_weekday - today.weekday()
    if days_ahead < 0:
        days_ahead += 7
    target = today + timedelta(days=days_ahead)
    return target.strftime("%Y-%m-%d")

def propose_change(name, date_str, new_start_hour, new_end_hour):
    team_member_id, real_name = find_team_member(name)
    if not team_member_id:
        return None, f"No he trobat l'empleat/da '{name}'."

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
        return None, f"Error buscant el torn: {r.text[:200]}"

    shifts = r.json().get("scheduled_shifts", [])
    if not shifts:
        return None, f"No he trobat cap torn de {real_name} el dia {date_str}."

    shift = shifts[0]
    details = shift.get("published_shift_details") or shift.get("draft_shift_details") or {}
    old_start = details.get("start_at")
    old_end = details.get("end_at")

    new_start = f"{date_str}T{new_start_hour:02d}:00:00+02:00"
    new_end = f"{date_str}T{new_end_hour:02d}:00:00+02:00"

    change = {
        "shift_id": shift["id"],
        "team_member_id": team_member_id,
        "location_id": details.get("location_id"),
        "job_id": details.get("job_id"),
        "old_start": old_start,
        "new_start": new_start,
        "old_end": old_end,
        "new_end": new_end,
        "version": details.get("version", 1),
        "name": real_name,
        "date": date_str
    }
    return change, None

def apply_and_publish(change):
    headers = get_headers()

    update_body = {
        "scheduled_shift": {
            "draft_shift_details": {
                "team_member_id": change["team_member_id"],
                "location_id": change["location_id"],
                "job_id": change["job_id"],
                "start_at": change["new_start"],
                "end_at": change["new_end"],
                "version": change["version"]
            }
        }
    }

    r = requests.put(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{change['shift_id']}",
        headers=headers,
        json=update_body
    )
    if r.status_code not in [200, 201]:
        return f"Error actualitzant: {r.status_code}\n{r.text[:300]}"

    pub_r = requests.post(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{change['shift_id']}/publish",
        headers=headers,
        json={}
    )
    if pub_r.status_code not in [200, 201]:
        return f"Actualitzat però no s'ha pogut publicar. Error: {pub_r.status_code}"

    return (
        f"✅ Canvi aplicat i publicat.\n\n"
        f"**{change['name']}** – {change['date']}\n"
        f"Abans: {change['old_start'][11:16]} → {change['old_end'][11:16]}\n"
        f"Ara:   {change['new_start'][11:16]} → {change['new_end'][11:16]}\n"
        f"Color del rol conservat."
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

    # Confirmació
    if user_id in pending_changes:
        if any(w in lower for w in ["sí", "si", "confirma", "ok", "d'acord", "aplica", "endavant"]):
            result = apply_and_publish(pending_changes[user_id])
            del pending_changes[user_id]
            say(result)
            return
        if any(w in lower for w in ["no", "cancel", "cancel·la"]):
            del pending_changes[user_id]
            say("Canvi cancel·lat.")
            return

    # Detecció de petició de canvi
    change_words = ["canviar", "canvia", "modificar", "modifica", "avançar", "avança", "posar", "voldria"]
    if any(w in lower for w in change_words):
        # Busquem nom
        name = None
        for possible in ["inma martin", "inma", "ruben", "marc", "anna", "pau"]:
            if possible in lower:
                name = possible
                break

        if not name:
            say("Per canviar un torn necessito saber **de qui** és.\nExemple: «Canvia el torn d'Inma Martin del dijous a 9h-13h»")
            return

        # Busquem dia
        date_str = None
        for day in ["dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"]:
            if day in lower:
                date_str = get_date_from_day(day)
                break

        if not date_str:
            # Prova amb número de dia
            day_match = re.search(r"del?\s*(\d{1,2})", lower)
            if day_match:
                day = int(day_match.group(1))
                date_str = f"2026-08-{day:02d}"

        if not date_str:
            say("No he entès el dia. Digues el dia de la setmana o la data.")
            return

        # Busquem hores (format 9h, 13h, 9:00, etc.)
        hours = re.findall(r"(\d{1,2})\s*h", lower)
        if len(hours) >= 2:
            new_start = int(hours[0])
            new_end = int(hours[1])
        else:
            say("No he entès les hores. Digues-les com «9h a 13h».")
            return

        change, err = propose_change(name, date_str, new_start, new_end)
        if err:
            say(err)
            return

        pending_changes[user_id] = change
        say(
            f"Proposta de canvi:\n\n"
            f"**{change['name']}** – {change['date']}\n"
            f"Abans: {change['old_start'][11:16]} → {change['old_end'][11:16]}\n"
            f"Ara:   {change['new_start'][11:16]} → {change['new_end'][11:16]}\n\n"
            f"Vols que l'apliqui i el publiqui? Contesta **sí** o **no**."
        )
        return

    # Consulta de torns
    if any(w in lower for w in ["torn", "horari", "treballo", "quan treball", "quin dia", "torns"]):
        result = get_square_shifts()
        say(result)
        return

    # Resposta normal
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
        say("Ho sento, he tingut un problema tècnic.")

if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
