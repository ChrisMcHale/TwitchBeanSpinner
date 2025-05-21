import os, uvicorn
import secrets
import urllib.parse

from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse

app = FastAPI()

CLIENT_ID    = os.getenv("TWITCH_CLIENT_ID")
REDIRECT_URI = os.getenv("CALLBACK_URL")  # https://twitch.cmchale.com/callback

# the scopes you need
SCOPES = [
    "chat:read",
    "bits:read",
    "moderator:read:moderators",
    "moderator:read:chatters",
]

@app.get("/auth", response_class=HTMLResponse)
async def auth(response: Response):
    # 1) generate & store state
    state = secrets.token_urlsafe(16)
    response.set_cookie(
        key="oauth_state",
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
    )

    # 2) build the auth URL
    scope_str = "+".join(SCOPES)
    params = {
        "client_id":     CLIENT_ID,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
        "scope":         scope_str,
        "state":         state,
    }
    query = urllib.parse.urlencode(params, safe=":+")  # keep colons and pluses
    auth_url = f"https://id.twitch.tv/oauth2/authorize?{query}"

    # 3) serve a simple page
    html = f"""
    <html>
      <head><title>Log in with Twitch</title></head>
      <body style="font-family:Arial,sans-serif; text-align:center; padding-top:50px;">
        <a href="{auth_url}"
           style="display:inline-block; padding:12px 24px; background-color:#6441a5; color:white; 
                  text-decoration:none; border-radius:4px; font-size:16px;">
          Log in with Twitch
        </a>
      </body>
    </html>
    """
    return HTMLResponse(html)
if __name__ == "__main__":
    uvicorn.run(
        "main:app",      # or "your_module_name:app"
        host="0.0.0.0",
        port=8070,
        reload=True      # omit in production
    )