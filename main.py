import os
import logging
import uuid
import json
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone

import google.auth
from google import genai
from google.genai import types
from google.cloud import storage
from google.auth.transport.requests import Request


logger = logging.getLogger("talky.api")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

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


class GenerateLessonRequest(BaseModel):
    interest: str = Field(..., min_length=2, max_length=120)

def _build_lesson_prompt(interest: str) -> str:
    return f"""
You are a news researcher. Find the most recent and trending news or issue
about "{interest}" and write a lesson material in English, approximately
300 characters.

Format:
- First line: title only
- Second line onward: concise summary with key facts and different perspectives
- Keep it discussion-friendly for an English conversation class
- Plain text only (no markdown)
""".strip()

def _extract_text_from_response(resp) -> str:
    chunks: list[str] = []
    candidate_count = 0
    part_count = 0

    for cand in (getattr(resp, "candidates", None) or []):
        candidate_count += 1
        content = getattr(cand, "content", None)
        parts = getattr(content, "parts", None) or []
        part_count += len(parts)

        for part in parts:
            t = getattr(part, "text", None)
            if isinstance(t, str) and t.strip():
                chunks.append(t.strip())

    if chunks:
        merged = "\n".join(chunks).strip()
        line_count = len([ln for ln in merged.splitlines() if ln.strip()])
        logger.info(
            "[generate-lesson] extracted from parts candidates=%d parts=%d chars=%d lines=%d",
            candidate_count, part_count, len(merged), line_count
        )
        return merged

    # parts가 없을 때만 fallback
    fallback = getattr(resp, "text", None)
    if isinstance(fallback, str) and fallback.strip():
        text = fallback.strip()
        line_count = len([ln for ln in text.splitlines() if ln.strip()])
        logger.warning(
            "[generate-lesson] fallback to response.text chars=%d lines=%d preview=%r",
            len(text), line_count, text[:120]
        )
        return text

    logger.warning(
        "[generate-lesson] empty Gemini response candidates=%d",
        candidate_count
    )
    return ""


# 필요하면 상수로 분리
LESSON_GEN_CONFIG = types.GenerateContentConfig(
    temperature=0.6,
    max_output_tokens=22000,  # curl과 동일. (운영에선 1024~4096로 낮추는 것 권장)
    response_mime_type="text/plain",
    thinking_config=types.ThinkingConfig(thinking_budget=10),
)

@app.post("/api/generate-lesson")
def generate_lesson(req: GenerateLessonRequest):
    try:
        interest = req.interest.strip()
        if not interest:
            raise HTTPException(status_code=400, detail="interest is required")

        prompt = _build_lesson_prompt(interest)

        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=LESSON_GEN_CONFIG,
        )

        # 디버깅 로그: 끊김 원인 추적용
        cand0 = (getattr(resp, "candidates", None) or [None])[0]
        finish_reason = getattr(cand0, "finish_reason", None) or getattr(cand0, "finishReason", None)
        if hasattr(finish_reason, "name"):
            finish_reason = finish_reason.name

        usage = getattr(resp, "usage_metadata", None) or getattr(resp, "usageMetadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", None) or getattr(usage, "promptTokenCount", None)
        candidate_tokens = getattr(usage, "candidates_token_count", None) or getattr(usage, "candidatesTokenCount", None)
        thoughts_tokens = getattr(usage, "thoughts_token_count", None) or getattr(usage, "thoughtsTokenCount", None)
        total_tokens = getattr(usage, "total_token_count", None) or getattr(usage, "totalTokenCount", None)

        logger.info(
            "[generate-lesson] finish_reason=%s prompt=%s candidate=%s thoughts=%s total=%s",
            finish_reason, prompt_tokens, candidate_tokens, thoughts_tokens, total_tokens
        )

        lesson_material = _extract_text_from_response(resp)
        if not lesson_material:
            raise HTTPException(status_code=502, detail="Empty response from Gemini")

        return {"lessonMaterial": lesson_material}

    except HTTPException:
        raise
    except Exception:
        logger.exception("Generate lesson error")
        raise HTTPException(status_code=500, detail="Failed to generate lesson")

