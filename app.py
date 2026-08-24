import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

# === FONS DE CONEIXEMENT DEL RESTAURANT ===
# Aquí pots anar afegint tota la informació important
KNOWLEDGE_BASE = """
INFORMACIÓ IMPORTANT DEL RESTAURANT:

- Vacances: Els treballadors tenen dret a 30 dies naturals de vacances a l'any.
- Canvi de torn: Cal avisar amb un mínim de 48 hores d'antelació i ha d'estar aprovat pel responsable.
- Nòmines: Es paguen el dia 1 de cada mes.
- Contractes: Tots els contractes són indefinits llevat que s'indiqui el contrari.
- Horaris: Els torns normals són de 10:00 a 16:00 i de 16:00 a 22:00.

(Pots anar afegint més informació aquí quan vulguis)
"""

SYSTEM_PROMPT = f"""
Ets Vesper, l'assistent intern del restaurant.
Només respones preguntes sobre:
- Horaris i torns
- Vacances i permisos
- Nòmines
- Contractes i condicions laborals

Respon sempre en català, de forma clara, amable i professional.
Si et pregunten alguna cosa fora d'aquests temes, digues educadament que només pots ajudar amb temes de personal i horaris.
No inventis informació. Si no tens la dada exacta, digues-ho clarament.
Utilitza sempre la informació del fons de coneixement quan sigui rellevant.

FONS DE CONEIXEMENT:
{KNOWLEDGE_BASE}
"""

# Memòria per usuari (ara més llarga)
user_history = {}

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
    # Guarda els últims 12 missatges (més memòria)
    user_history[user_id] = user_history[user_id][-12:]

    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}] + user_history[user_id]

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
