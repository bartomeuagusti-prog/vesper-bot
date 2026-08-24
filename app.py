import os
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from openai import OpenAI

app = App(token=os.environ.get("SLACK_BOT_TOKEN"))

client = OpenAI(
    api_key=os.environ.get("XAI_API_KEY"),
    base_url="https://api.x.ai/v1"
)

SYSTEM_PROMPT = """
Ets Vesper, l'assistent intern del restaurant.
Només respones preguntes sobre:
- Horaris i torns
- Vacances i permisos
- Nòmines
- Contractes i condicions laborals

Respon sempre en català, de forma clara, amable i professional.
Si et pregunten alguna cosa fora d'aquests temes, digues educadament que només pots ajudar amb temes de personal i horaris.
No inventis informació. Si no tens la dada exacta, digues-ho clarament.
Mantén el context de la conversa.
"""

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
    user_history[user_id] = user_history[user_id][-8:]

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
