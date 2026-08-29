import os
import re
import json
import uuid
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

SYSTEM_PROMPT = """
Ets Vesper, l'assistent intern de torns de La Terrassa de l'Ultonia
(cocteleria rooftop de l'Hotel Ultonia, Girona).

Parles només per DM privat de Slack. Respon en català, curt, clar i humà.
Una idea per missatge. Hores en format 17:30 / 02:30.

Pots: consultar horaris reals de Square, proposar canvis de torn i
escalar a Ruben. Mai inventis hores. Mai aplicis un canvi sense un
«sí» explícit a la proposta d'aquest fil.

Coneixement fix:
- Vacances: 31 dies naturals, preavís 15 dies.
- Canvi de torn: 48 hores + aprovació del responsable.
- Nòmines: dia 1.
- Contractes: indefinits si no es diu el contrari.
- El color visual de Square no el controla l'API; no el prometis.

Si surt del tema (sou, contracte, baixa, conflicte): escala a Ruben.
"""

CLASSIFIER_PROMPT = """
Classifica el missatge d'un treballador de restaurant sobre torns.
Respon NOMÉS un JSON vàlid, sense markdown ni text extra.

Esquema:
{
  "intent": "consulta_llista" | "consulta_persona" | "proposta" | "confirmar" | "cancelar" | "escalar" | "xerrada",
  "persona": string o null,
  "dia_setmana": "dilluns"|"dimarts"|"dimecres"|"dijous"|"divendres"|"dissabte"|"diumenge"|null,
  "data": "YYYY-MM-DD" o null,
  "accio": "posar_hores" | "avancar" | "retardar" | null,
  "minuts": number o null,
  "hora_inici": "HH:MM" o null,
  "hora_fi": "HH:MM" o null,
  "resposta": string
}

Regles:
- "Quins torns hi ha", "horaris de la setmana", "qui treballa" sense persona → consulta_llista
- "Quin torn tinc/té X", "a quina hora entra/plega X", un dia concret d'algú → consulta_persona
- Canviar, avançar, retardar, posar de Xh a Yh, moure entrada/sortida → proposta
- sí, si, ok, confirma, d'acord, aplica, endavant → confirmar
- no, cancel·la, cancela → cancelar
- sou, nòmina detallada, contracte, baixa, conflicte, vacances llargues → escalar
- salutació o xerrada → xerrada
- persona: nom i cognom si es pot. "jo/em/meu" → persona="JO"
- Si diu un número de dia (el 26, del 1) i som a l'agost/setembre 2026, omple data.
- hora_inici/hora_fi en HH:MM (9h → 09:00, 13h → 13:00, 2:30 → 02:30).
- avançar 30 minuts l'entrada → accio=avancar, minuts=30
- resposta: frase curta en català per xerrada/escalar. Per la resta pot ser "".
"""

user_history = {}
pending_changes = {}
DAYS = {
    "dilluns": 0, "dimarts": 1, "dimecres": 2, "dijous": 3,
    "divendres": 4, "dissabte": 5, "diumenge": 6
}


def get_headers():
    return {
        "Authorization": f"Bearer {SQUARE_TOKEN}",
        "Content-Type": "application/json",
        "Square-Version": "2025-05-21"
    }


def hhmm(value):
    if not value:
        return ""
    return str(value)[11:16] if "T" in str(value) else str(value)[:5]


def get_team_map():
    headers = get_headers()
    r = requests.post(
        f"{SQUARE_BASE}/team-members/search",
        headers=headers,
        json={"query": {"filter": {"status": "ACTIVE"}}, "limit": 200}
    )
    names = {}
    if r.status_code == 200:
        for m in r.json().get("team_members", []):
            full = f"{m.get('given_name', '')} {m.get('family_name', '')}".strip()
            names[m["id"]] = full or m["id"]
    return names


def resolve_name(query, team_names):
    if not query:
        return None, None
    q = query.strip().lower()
    if q in ("jo", "meu", "meva", "mi"):
        return None, "JO"
    items = list(team_names.items())
    for tid, full in items:
        if q == full.lower():
            return tid, full
    matches = [(tid, full) for tid, full in items if q in full.lower() or full.lower() in q]
    if len(matches) == 1:
        return matches[0]
    parts = [p for p in q.split() if len(p) > 2]
    if parts:
        part_matches = []
        for tid, full in items:
            fl = full.lower()
            if all(p in fl for p in parts):
                part_matches.append((tid, full))
        if len(part_matches) == 1:
            return part_matches[0]
        if len(matches) == 0:
            matches = part_matches
    if len(matches) > 1:
        opcions = ", ".join(sorted({n for _, n in matches})[:5])
        return None, f"VARIS:{opcions}"
    return None, None


def date_from_day(day_name):
    if not day_name or day_name not in DAYS:
        return None
    today = datetime.now().date()
    ahead = DAYS[day_name] - today.weekday()
    if ahead < 0:
        ahead += 7
    return (today + timedelta(days=ahead)).strftime("%Y-%m-%d")


def resolve_date(data, dia_setmana):
    if data and re.match(r"\d{4}-\d{2}-\d{2}", data):
        return data
    return date_from_day(dia_setmana)


def search_shifts(start_iso, end_iso, team_member_id=None):
    headers = get_headers()
    loc_r = requests.get(f"{SQUARE_BASE}/locations", headers=headers)
    if loc_r.status_code != 200:
        return None, f"Error consultant ubicacions: {loc_r.status_code}"
    locations = loc_r.json().get("locations", [])
    if not locations:
        return None, "No he trobat ubicacions a Square."
    filt = {
        "location_ids": [loc["id"] for loc in locations],
        "start": {"start_at": start_iso, "end_at": end_iso}
    }
    if team_member_id:
        filt["team_member_ids"] = [team_member_id]
    body = {"query": {"filter": filt}, "limit": 50}
    r = requests.post(f"{SQUARE_BASE}/labor/scheduled-shifts/search", headers=headers, json=body)
    if r.status_code != 200:
        return None, f"Error consultant torns: {r.status_code}"
    return r.json().get("scheduled_shifts", []), None


def shift_details(shift):
    return shift.get("published_shift_details") or shift.get("draft_shift_details") or {}


def get_square_shifts(days=7):
    if not SQUARE_TOKEN:
        return "No tinc el token de Square configurat."
    now = datetime.now(timezone.utc)
    start = now.isoformat().replace("+00:00", "Z")
    end = (now + timedelta(days=days)).isoformat().replace("+00:00", "Z")
    shifts, err = search_shifts(start, end)
    if err:
        return err
    if not shifts:
        return "No he trobat torns programats per als propers dies."
    team_names = get_team_map()
    lines = [f"He trobat {len(shifts)} torns els propers {days} dies:\n"]
    for s in shifts[:15]:
        d = shift_details(s)
        name = team_names.get(d.get("team_member_id"), "Sense assignar")
        lines.append(f"• {name}: {hhmm(d.get('start_at'))} → {hhmm(d.get('end_at'))} ({str(d.get('start_at', ''))[:10]})")
    if len(shifts) > 15:
        lines.append(f"\n... i {len(shifts) - 15} més.")
    return "\n".join(lines)


def get_person_shifts(team_id, name, date_str):
    start = f"{date_str}T00:00:00+02:00"
    end = f"{date_str}T23:59:59+02:00"
    shifts, err = search_shifts(start, end, team_id)
    if err:
        return err
    if not shifts:
        return f"No he trobat cap torn de {name} el {date_str}."
    lines = [f"Torns de {name} el {date_str}:"]
    for s in shifts:
        d = shift_details(s)
        lines.append(f"• {hhmm(d.get('start_at'))} → {hhmm(d.get('end_at'))}")
    return "\n".join(lines)


def build_new_times(details, accio, minuts, hora_inici, hora_fi):
    old_start = details.get("start_at")
    old_end = details.get("end_at")
    if not old_start or not old_end:
        return None, None, "Aquest torn no té hores completes."

    start_dt = datetime.fromisoformat(old_start)
    end_dt = datetime.fromisoformat(old_end)

    if accio in ("avancar", "retardar"):
        delta = int(minuts or 30)
        if accio == "avancar":
            start_dt = start_dt - timedelta(minutes=delta)
        else:
            start_dt = start_dt + timedelta(minutes=delta)
        return start_dt.isoformat(), end_dt.isoformat(), None

    if hora_inici:
        h, m = [int(x) for x in hora_inici.split(":")]
        start_dt = start_dt.replace(hour=h, minute=m, second=0)
    if hora_fi:
        h, m = [int(x) for x in hora_fi.split(":")]
        end_dt = start_dt.replace(hour=h, minute=m, second=0)
        if end_dt <= start_dt:
            end_dt = end_dt + timedelta(days=1)
    return start_dt.isoformat(), end_dt.isoformat(), None


def propose_change(team_id, name, date_str, accio, minuts, hora_inici, hora_fi):
    start = f"{date_str}T00:00:00+02:00"
    end = f"{date_str}T23:59:59+02:00"
    shifts, err = search_shifts(start, end, team_id)
    if err:
        return None, err
    if not shifts:
        return None, f"No he trobat cap torn de {name} el {date_str}."

    shift = shifts[0]
    details = shift_details(shift)
    new_start, new_end, terr = build_new_times(details, accio, minuts, hora_inici, hora_fi)
    if terr:
        return None, terr

    if not details.get("job_id"):
        return None, "Aquest torn no té rol (job_id). No el modifico per no perdre el sentit del torn. Ho passo a Ruben."

    return {
        "shift_id": shift["id"],
        "team_member_id": team_id,
        "location_id": details.get("location_id"),
        "job_id": details.get("job_id"),
        "old_start": details.get("start_at"),
        "new_start": new_start,
        "old_end": details.get("end_at"),
        "new_end": new_end,
        "version": shift.get("version", 1),
        "name": name,
        "date": date_str
    }, None


def apply_and_publish(change):
    headers = get_headers()
    if not change.get("job_id"):
        return "No puc aplicar el canvi: falta el rol del torn. Escalo a Ruben."

    update_body = {
        "scheduled_shift": {
            "version": change["version"],
            "draft_shift_details": {
                "team_member_id": change["team_member_id"],
                "location_id": change["location_id"],
                "job_id": change["job_id"],
                "start_at": change["new_start"],
                "end_at": change["new_end"]
            }
        }
    }
    r = requests.put(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{change['shift_id']}",
        headers=headers,
        json=update_body
    )
    if r.status_code not in [200, 201]:
        return f"Error actualitzant el torn: {r.status_code}\n{r.text[:300]}"

    new_version = r.json().get("scheduled_shift", {}).get("version", change["version"])
    pub_r = requests.post(
        f"{SQUARE_BASE}/labor/scheduled-shifts/{change['shift_id']}/publish",
        headers=headers,
        json={
            "idempotency_key": str(uuid.uuid4()),
            "version": new_version,
            "scheduled_shift_notification_audience": "NONE"
        }
    )
    if pub_r.status_code not in [200, 201]:
        return (
            f"He actualitzat el torn però no l'he pogut publicar (error {pub_r.status_code}).\n"
            f"{pub_r.text[:300]}"
        )
    return (
        f"Canvi aplicat i publicat.\n\n"
        f"{change['name']} – {change['date']}\n"
        f"Abans: {hhmm(change['old_start'])} → {hhmm(change['old_end'])}\n"
        f"Ara:   {hhmm(change['new_start'])} → {hhmm(change['new_end'])}"
    )


def extract_json(text):
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def classify(text):
    try:
        response = client.chat.completions.create(
            model="grok-4.6",
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0
        )
        return extract_json(response.choices[0].message.content) or {}
    except Exception:
        return {}


def chat_reply(history):
    try:
        response = client.chat.completions.create(
            model="grok-4.6",
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
            temperature=0.3
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Ho sento, he tingut un problema tècnic. ({e})"


def handle_intent(user_id, text, data):
    intent = (data.get("intent") or "").lower()
    lower = text.lower()

    if user_id in pending_changes:
        yes = intent == "confirmar" or lower in ("sí", "si", "ok") or any(
            w in lower for w in ["confirma", "d'acord", "aplica", "endavant"]
        )
        no = intent == "cancelar" or lower in ("no",) or any(
            w in lower for w in ["cancel", "cancel·la", "cancela"]
        )
        if yes and not no:
            result = apply_and_publish(pending_changes[user_id])
            del pending_changes[user_id]
            return result
        if no:
            del pending_changes[user_id]
            return "Canvi cancel·lat."

    if intent == "consulta_llista":
        return get_square_shifts()

    team_names = get_team_map()
    persona = data.get("persona")
    date_str = resolve_date(data.get("data"), data.get("dia_setmana"))

    if intent in ("consulta_persona", "proposta"):
        tid, resolved = resolve_name(persona, team_names)
        if resolved == "JO":
            return "Encara no tinc vinculat el teu usuari de Slack amb la plantilla. Digues el teu nom i ho miro."
        if resolved and str(resolved).startswith("VARIS:"):
            return f"He trobat més d'una persona. Qui exactament? {resolved[6:]}"
        if not tid:
            if intent == "consulta_persona":
                return "De qui vols consultar el torn? Digues el nom."
            return "Per canviar un torn necessito el nom. Exemple: «Canvia el torn d'Inma Martin del dimarts a 15:00-19:00»"
        if not date_str:
            return "Quin dia? Digues el dia de la setmana o la data."
        if intent == "consulta_persona":
            return get_person_shifts(tid, resolved, date_str)

        accio = data.get("accio") or "posar_hores"
        change, err = propose_change(
            tid, resolved, date_str, accio,
            data.get("minuts"), data.get("hora_inici"), data.get("hora_fi")
        )
        if err:
            return err
        pending_changes[user_id] = change
        return (
            f"Proposta de canvi:\n\n"
            f"{change['name']} – {change['date']}\n"
            f"Abans: {hhmm(change['old_start'])} → {hhmm(change['old_end'])}\n"
            f"Ara:   {hhmm(change['new_start'])} → {hhmm(change['new_end'])}\n\n"
            f"Ho aplico i ho publico a Square? Digues sí o no."
        )

    if intent == "confirmar":
        return "No tinc cap canvi pendent de confirmar."
    if intent == "cancelar":
        return "No hi havia cap canvi pendent."
    if intent == "escalar":
        return data.get("resposta") or "Això ho ha de decidir Ruben. Li ho pots dir o t'ho passo jo si m'ho confirmes."

    return data.get("resposta") or None


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

    data = classify(text)
    answer = handle_intent(user_id, text, data)
    if not answer:
        answer = chat_reply(user_history[user_id])

    user_history[user_id].append({"role": "assistant", "content": answer})
    say(answer)


if __name__ == "__main__":
    handler = SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN"))
    handler.start()
