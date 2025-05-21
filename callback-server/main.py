import os
import json
import secrets
import urllib.parse
import logging
import uvicorn

import httpx
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse

# ——— CONFIG ————————————————————————————————————————————————————
CLIENT_ID     = "bc2zp0pggthm56x4e9n5bd2a4byf7h"
CLIENT_SECRET = "k9ooa3p44dyd46c7ramlbabvsc1iwm"
REDIRECT_URI  = "https://twitch.cmchale.com/callback"

SCOPES        = [
    "chat:read",
    "bits:read",
    "moderator:read:moderators",
    "moderator:read:chatters",
]
logging.basicConfig(level=logging.INFO)

app = FastAPI()


# ——— STEP 1: /auth — generate & store `state`, build Twitch URL ————————
@app.get("/auth", response_class=HTMLResponse)
async def auth(response: Response):
    state = secrets.token_urlsafe(16)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
    )

    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         " ".join(SCOPES),
        "state":         state,
    }
    # allow spaces and colons in scope
    query   = urllib.parse.urlencode(params, safe=":+ ")
    auth_url = f"https://id.twitch.tv/oauth2/authorize?{query}"

    return f"""
    <html>
      <body style="text-align:center; font-family:sans-serif; padding-top:50px;">
        <a href="{auth_url}"
           style="background:#6441a5;color:#fff;padding:12px 24px;
                  text-decoration:none;border-radius:4px;font-size:16px;">
          Log in with Twitch
        </a>
              <p align="left">
              This will authorise the BeanSpinner app with the following scopes:<br>
             "chat:read" - To read messages in chat and respond to commands from Mods/VIPs<br>
             "bits:read" - To read Bit donation events and trigger the wheel<br>
             "moderator:read:moderators" - To identify Moderators in the chat<br>
             "moderator:read:chatters" - To identify VIPs in the chat<br>
      </body>
    </html>
    """


# ——— STEP 2: /callback — verify `state`, swap code for tokens, fetch user, save —————
@app.get("/callback", response_class=HTMLResponse)
async def callback(request: Request):
    # 1) extract & verify state
    code           = request.query_params.get("code")
    returned_state = request.query_params.get("state")
    stored_state   = request.cookies.get("oauth_state")

    if not code or not returned_state or returned_state != stored_state:
        raise HTTPException(400, "Invalid OAuth callback (missing code or bad state)")

    logging.info("State verified, exchanging code for tokens…")

    # 2) exchange code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code":          code,
                "grant_type":    "authorization_code",
                "redirect_uri":  REDIRECT_URI,
            },
        )
        if token_resp.status_code != 200:
            body = await token_resp.text()
            logging.error(f"Twitch token error {token_resp.status_code}: {body}")
            raise HTTPException(token_resp.status_code, f"Twitch token error: {body}")
        tokens = token_resp.json()

        # 3) fetch authenticated user info
        user_resp = await client.get(
            "https://api.twitch.tv/helix/users",
            headers={
                "Client-ID":     CLIENT_ID,
                "Authorization": f"Bearer {tokens['access_token']}"
            }
        )
        user_resp.raise_for_status()
        user_data = user_resp.json()["data"][0]

    # 4) assemble output
    out = {
        "state":   returned_state,
        "user": {
            "id":           user_data["id"],
            "login":        user_data["login"],
            "display_name": user_data["display_name"]
        },
        "tokens": tokens
    }

    # 5) write to tokens_<login>.json
    filename = f"tokens_{user_data['id']}.json"
    with open(filename, "w") as f:
        json.dump(out, f, indent=2)
    logging.info(f"Saved tokens for {user_data['login']} → {filename}")

    # 6) confirm to user
    return f"""
      <html>
        <body style="text-align:center; font-family:sans-serif; padding-top:50px;">
          <h1>✅ Authorized as {user_data['display_name']}</h1>
          <p>You may now close this window.</p>
        </body>
      </html>
    """


# ——— Optional: load existing tokens on startup —————————————————————————
@app.on_event("startup")
def load_tokens():
    # scans cwd for any tokens_*.json and logs them
    for fname in os.listdir("."):
        if fname.startswith("tokens_") and fname.endswith(".json"):
            logging.info(f"Found existing token file: {fname}")

if __name__ == "__main__":
    uvicorn.run(
        "main:app",      # or "your_module_name:app"
        host="0.0.0.0",
        port=8070,
        reload=True      # omit in production
    )
