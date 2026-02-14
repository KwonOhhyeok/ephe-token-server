import os
import uuid
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone

import google.auth
from google import genai
from google.cloud import storage
from google.auth.transport.requests import Request


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://talky.vivleap.com",
        "http://localhost:5173", # 개발용
    ],
    allow_credentials=False,  # 쿠키 인증 안 쓰면 False
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set")

GCS_BUCKET = os.environ.get("GCS_BUCKET")
if not GCS_BUCKET:
    raise RuntimeError("GCS_BUCKET is not set")


SIGNED_URL_TTL_SECONDS = int(os.environ.get("SIGNED_URL_TTL_SECONDS", "600"))

SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")

client = genai.Client(api_key=GEMINI_API_KEY)
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET)


class SessionCreateRequest(BaseModel):
    modelId: str


class SignUrlRequest(BaseModel):
    path: str
    contentType: str | None = None


def now_iso():
    return datetime.now(tz=timezone.utc).isoformat()


def session_prefix(session_id: str):
    return f"sessions/{session_id}"


def assert_session_path(session_id: str, path: str):
    normalized = path.lstrip("/")
    expected_prefix = f"{session_prefix(session_id)}/"
    if not normalized.startswith(expected_prefix):
        raise HTTPException(status_code=400, detail="path must be inside session prefix")
    if ".." in normalized.split("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    return normalized


def _get_access_token():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(Request())
    return creds.token

def put_signed_url(path: str, content_type: str):
    blob = bucket.blob(path)
    access_token = _get_access_token()

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=SIGNED_URL_TTL_SECONDS),
        method="PUT",
        content_type=content_type,
        service_account_email=SERVICE_ACCOUNT_EMAIL,  # 서명에 사용할 SA 이메일
        access_token=access_token,                   # 방금 갱신한 토큰
    )

def get_signed_url(path: str):
    blob = bucket.blob(path)
    access_token = _get_access_token()

    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(seconds=SIGNED_URL_TTL_SECONDS),
        method="GET",
        service_account_email=SERVICE_ACCOUNT_EMAIL,
        access_token=access_token,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


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


@app.post("/api/session/create")
def create_session(req: SessionCreateRequest):
    try:
        session_id = str(uuid.uuid4())
        prefix = session_prefix(session_id)
        manifest_path = f"{prefix}/manifest.json"
        transcript_index_path = f"{prefix}/transcript/index.json"
        metadata = {
            "sessionId": session_id,
            "modelId": req.modelId,
            "createdAt": now_iso(),
            "bucket": GCS_BUCKET,
            "prefix": prefix,
        }

        # Optional bootstrap files for easier troubleshooting.
        bucket.blob(f"{prefix}/session.json").upload_from_string(
            data=json.dumps(metadata),
            content_type="application/json",
        )
        bucket.blob(transcript_index_path).upload_from_string(
            data='{"items":[]}',
            content_type="application/json",
        )

        return {
            "sessionId": session_id,
            "bucket": GCS_BUCKET,
            "prefix": prefix,
            "manifestPath": manifest_path,
            "manifestUploadUrl": put_signed_url(manifest_path, "application/json"),
            "manifestReadUrl": get_signed_url(manifest_path),
            "uploadUrlEndpoint": f"/api/session/{session_id}/upload-url",
            "readUrlEndpoint": f"/api/session/{session_id}/read-url",
            "createdAt": metadata["createdAt"],
        }
    except HTTPException:
        raise
    except Exception as e:
        print("session/create error:", repr(e))
        raise HTTPException(status_code=500, detail="Failed to create session")


@app.post("/api/session/{session_id}/upload-url")
def sign_upload_url(session_id: str, req: SignUrlRequest):
    try:
        content_type = req.contentType or "application/octet-stream"
        path = assert_session_path(session_id, req.path)
        return {"url": put_signed_url(path, content_type), "path": path}
    except HTTPException:
        raise
    except Exception as e:
        print("upload-url error:", repr(e))
        raise HTTPException(status_code=500, detail="Failed to sign upload URL")


@app.post("/api/session/{session_id}/read-url")
def sign_read_url(session_id: str, req: SignUrlRequest):
    try:
        path = assert_session_path(session_id, req.path)
        return {"url": get_signed_url(path), "path": path}
    except HTTPException:
        raise
    except Exception as e:
        print("read-url error:", repr(e))
        raise HTTPException(status_code=500, detail="Failed to sign read URL")
