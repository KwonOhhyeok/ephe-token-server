import os
from fastapi import FastAPI, HTTPException
from google import genai
from datetime import datetime, timedelta, timezone

app = FastAPI()

# Gemini API Key (Secret Manager → env)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")

# Gemini client (Developer API)
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
                # Live API + ephemeral token은 v1alpha
                "httpOptions": {
                    "apiVersion": "v1alpha"
                }
            }
        )

        # 클라이언트에는 token.name만 전달
        return {
            "token": token.name,
            "expiresInSeconds": 60
        }

    except Exception as e:
        # Cloud Run 로그에 정확한 원인 남기기
        print("Ephemeral token error:", repr(e))
        raise HTTPException(
            status_code=500,
            detail="Failed to create ephemeral token"
        )

