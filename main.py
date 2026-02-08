import os
import requests
from fastapi import FastAPI, HTTPException

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")

app = FastAPI()

@app.post("/api/ephe-token")
def create_ephemeral_token():
    """
    Cloud Run /token endpoint
    """

    url = "https://generativelanguage.googleapis.com/v1beta/authTokens"

    headers = {
        "Authorization": f"Bearer {GEMINI_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        # 새 Live 세션 시작 가능 시간 (초 단위 개념)
        "newSessionExpireTime": "60s",
        # Live 연결 내 메시지 유지 시간
        "expireTime": "1800s",
    }

    r = requests.post(url, headers=headers, json=payload)

    if r.status_code != 200:
        raise HTTPException(
            status_code=500,
            detail=f"Gemini error: {r.text}"
        )

    data = r.json()

    # 클라이언트에는 token.name만 내려줌
    return {
        "token": data["name"],
        "expiresIn": 60
    }

