import os
import json
import glob
import secrets
import urllib.parse
import logging
import asyncio
import uvicorn
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from twitchAPI.twitch import Twitch
from twitchAPI.chat import Chat, ChatEvent, ChatMessage, ChatCommand
from twitchAPI.type import AuthScope
from dotenv import load_dotenv

# ——— CONFIG ——————————————————————————————————————————————
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CLIENT_ID       = os.getenv("TWITCH_CLIENT_ID")
CLIENT_SECRET   = os.getenv("TWITCH_SECRET")
CALLBACK_URL    = os.getenv("CALLBACK_URL")  # e.g. https://twitch.cmchale.com/callback
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")
BIT_SPIN_AMOUNT = int(os.getenv("BIT_SPIN_AMOUNT", "555"))
USER_NAME       = os.getenv("USER_NAME")

# Scopes required for both chat listening and EventSub
ALL_SCOPES = [
    AuthScope.CHAT_READ,
    AuthScope.CHAT_EDIT,
    AuthScope.BITS_READ,
    AuthScope.MODERATOR_READ_MODERATORS,
    AuthScope.MODERATOR_READ_CHATTERS,
]

TOKENS_GLOB = f"tokens_{USER_NAME}.json"

# ——— STATE ——————————————————————————————————————————————
clients    = set()            # active WebSocket connections
spin_queue = asyncio.Queue()  # queued spin requests

def _find_token_file() -> str | None:
    files = glob.glob(TOKENS_GLOB)
    return files[0] if files else None

async def _setup_twitch(access_token: str, refresh_token: str, broadcaster_id: str, broadcaster_login: str) -> None:
    """
    Authenticate once with full scopes, subscribe to bits events, and start chat listener.
    """
    twitch = await Twitch(CLIENT_ID, CLIENT_SECRET)
    await twitch.set_user_authentication(access_token, ALL_SCOPES, refresh_token)
    logging.info(f"Twitch authenticated for user {broadcaster_login} (ID {broadcaster_id})")

    # Subscribe to bits via EventSub
    try:
        await twitch.create_eventsub_subscription(
            subscription_type="channel.cheer",
            version="1",
            condition={"broadcaster_user_id": broadcaster_id},
            transport={
                "method":   "webhook",
                "callback": CALLBACK_URL,
                "secret":   WEBHOOK_SECRET,
            },
        )
        logging.info("Subscribed to channel.cheer EventSub")
    except Exception as e:
        logging.error(f"EventSub subscription failed: {e}")

    # Start chat listener
    asyncio.create_task(_start_chat_listener(access_token, refresh_token, broadcaster_login))

async def _start_chat_listener(access_token: str, refresh_token: str, broadcaster_login: str) -> None:
    """
    Listen for '!spin' commands from moderators in chat using the same Twitch auth client.
    """
    twitch = await Twitch(CLIENT_ID, CLIENT_SECRET)
    await twitch.set_user_authentication(access_token, ALL_SCOPES, refresh_token)
    chat = await Chat(twitch)

    async def on_ready(evt) -> None:
        await evt.chat.join_room(broadcaster_login)
        logging.info(f"Chat listener joined room {broadcaster_login}")

    async def on_spin_message(cmd: ChatCommand) -> None:
            if cmd.name == "spin" and (cmd.user.mod or cmd.user.name == cmd.room.name):
                logging.info(f"Moderator {cmd.user.name} requested a spin via chat")
                await spin_queue.put("spin")
            else:
                logging.info(f"User {cmd.user.name} tried to spin but is not a mod")

    chat.register_event(ChatEvent.READY, on_ready)
    chat.register_command('spin', on_spin_message)

    # Start the chat client in a background thread
    import threading
    threading.Thread(target=chat.start, daemon=True).start()

async def spin_processor() -> None:
    """Process queued spins sequentially, each taking 9 seconds."""
    while True:
        await spin_queue.get()
        if clients:
            logging.info("Broadcasting spin to WebSocket clients")
            await asyncio.gather(*[ws.send_text("spin") for ws in clients])
        else:
            logging.info("No WebSocket clients connected; skipping spin")
        await asyncio.sleep(9)
        spin_queue.task_done()
        logging.info("Spin completed")

# ——— LIFESPAN: startup & shutdown ——————————————————————————————
@asynccontextmanager
async def lifespan(app: FastAPI):
    token_file = _find_token_file()
    if token_file:
        data  = json.load(open(token_file))
        creds = data["tokens"]
        uid   = data["user"]["id"]
        login = data["user"]["login"]
        await _setup_twitch(creds["access_token"], creds.get("refresh_token"), uid, login)
    asyncio.create_task(spin_processor())
    yield

app = FastAPI(lifespan=lifespan)

# ——— 1) /auth ——————————————————————————————————————————————
@app.get("/auth", response_class=HTMLResponse)
async def auth(response: Response) -> RedirectResponse:
    state = secrets.token_urlsafe(16)
    response.set_cookie("oauth_state", state, httponly=True, secure=True, samesite="lax")
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  CALLBACK_URL,
        "response_type": "code",
        "scope":         " ".join([s.value for s in ALL_SCOPES]),
        "state":         state,
        "force_verify":  "true",
    }
    query = urllib.parse.urlencode(params, safe=":+")
    auth_url = f"https://id.twitch.tv/oauth2/authorize?{query}"
    return RedirectResponse(auth_url)

# ——— 2) /callback ——————————————————————————————————————————————
@app.get("/callback", response_class=HTMLResponse)
async def callback(request: Request) -> HTMLResponse:
    code           = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    stored_state   = request.cookies.get("oauth_state")
    if not code or returned_state != stored_state:
        raise HTTPException(400, "Invalid OAuth callback (bad state or missing code)")

    async with httpx.AsyncClient() as client:
        tr = await client.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": CALLBACK_URL,
        })
        tr.raise_for_status()
        tokens = tr.json()
        ur = await client.get("https://api.twitch.tv/helix/users", headers={
            "Client-ID": CLIENT_ID,
            "Authorization": f"Bearer {tokens['access_token']}"
        })
        ur.raise_for_status()
        user = ur.json()["data"][0]

    record = {
        "state": returned_state,
        "user": {"id": user["id"], "login": user["login"], "display_name": user["display_name"]},
        "tokens": tokens
    }
    filename = f"tokens_{user['login']}.json"
    with open(filename, "w") as f:
        json.dump(record, f, indent=2)
    logging.info(f"Saved OAuth record to {filename}")

    await _setup_twitch(tokens["access_token"], tokens.get("refresh_token"), user["id"], user["login"])

    return f"""
<html><body style="text-align:center; font-family:sans-serif; padding-top:50px;">
  <h1>✅ Authorized as {user['display_name']}</h1>
  <p>Tokens saved to <code>{filename}</code>. You may close this window.</p>
</body></html>
"""

# ——— 3) /eventsub ——————————————————————————————————————————————
@app.post("/eventsub")
async def eventsub(webhook_payload: dict) -> dict:
    if webhook_payload.get("challenge"):
        return JSONResponse({"challenge": webhook_payload["challenge"]})
    stype = webhook_payload.get("subscription", {}).get("type")
    if stype == "channel.cheer":
        bits = webhook_payload["event"]["bits"]
        user = webhook_payload["event"]["user_name"]
        logging.info(f"{user} cheered {bits} bits")
        if bits == BIT_SPIN_AMOUNT:
            await spin_queue.put("spin")
    return {"status": "ok"}

# ——— 4) WebSocket endpoint ——————————————————————————————————————
@app.websocket("/ws/spin")
async def ws_spin(ws: WebSocket) -> None:
    await ws.accept()
    clients.add(ws)
    logging.info("WebSocket client connected")
    try:
        while True:
            msg = await ws.receive_text()
            logging.info(f"WS recv: {msg}")
            await ws.send_text("ack")
    except WebSocketDisconnect:
        logging.info("WebSocket client disconnected")
    finally:
        clients.remove(ws)

# ——— RUN ———————————————————————————————————————————————————————
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8070, reload=True)
