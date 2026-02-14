import os
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from datetime import datetime, timedelta, timezone

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://talky.vivleap.com",
        # 개발용이 필요하면 추가:
        "http://localhost:5173",
    ],
    allow_credentials=False,  # 쿠키 인증 안 쓰면 False
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")

client = genai.Client(api_key=GEMINI_API_KEY)

@app.post("/api/ephemeral-token")
def create_ephemeral_token():
    try:
        expire_time = datetime.now(tz=timezone.utc) + timedelta(minutes=30)
        new_session_expire_time = datetime.now(tz=timezone.utc) + timedelta(minutes=1)

        token = client.auth_tokens.create(
            config={
                "uses": 1,
                "expireTime": expire_time.isoformat(),
                "newSessionExpireTime": new_session_expire_time.isoformat(),
                "httpOptions": {"apiVersion": "v1alpha"},
            }
        )
        return {"token": token.name, "expiresInSeconds": 60}

    except Exception as e:
        print("Ephemeral token error:", repr(e))
        raise HTTPException(status_code=500, detail="Failed to create ephemeral token")

