import os
import time
import uuid
import hmac
import logging
from contextvars import ContextVar
from pathlib import Path

from datetime import datetime, timedelta
from typing import Literal

from io import BytesIO
from openpyxl import Workbook

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi import Depends, Form
from pydantic import BaseModel, Field
from groq import Groq
from fastapi.responses import FileResponse
from elevenlabs.client import ElevenLabs
import tempfile
from starlette.background import BackgroundTask
from urllib.parse import quote
import json
import base64
import re
from fastapi.responses import StreamingResponse



# Load API keys from the project root
PROJECT_ROOT = Path(__file__).resolve().parent

# Make the src/ phase modules importable under uvicorn as well as tests.
import sys as _sys

_src_dir = str(PROJECT_ROOT / "src")
if _src_dir not in _sys.path:
    _sys.path.append(_src_dir)
load_dotenv(PROJECT_ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from the project .env file")

groq_client = Groq(api_key=GROQ_API_KEY)

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

if not ELEVENLABS_API_KEY:
    raise RuntimeError(
        "ELEVENLABS_API_KEY is missing from the project .env file"
    )

elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)


# Outbound calling mode. "offline" means no telephony integration is
# configured; "mock" allows safe simulated flow testing; "live" is a
# Phase 6 feature that must never be enabled without operator approval.
OUTBOUND_CALL_MODE = os.getenv("OUTBOUND_CALL_MODE", "offline").lower()

if OUTBOUND_CALL_MODE not in ("offline", "mock", "live"):
    raise RuntimeError(
        "OUTBOUND_CALL_MODE must be offline, mock, or live. "
    )

if OUTBOUND_CALL_MODE == "live":
    raise RuntimeError(
        "Live outbound calling requires Phase 6 telephony integration, "
        "a configured provider, and explicit operator approval. "
        "Set OUTBOUND_CALL_MODE=offline or mock instead."
    )


# --------------------------------------------------
# Structured logging with request IDs
# --------------------------------------------------

logger = logging.getLogger("truck_dispatcher")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def log_event(event: str, **fields):
    """Emit one structured JSON log line, never containing secrets."""
    logger.info(
        json.dumps(
            {
                "ts": datetime.now().astimezone().isoformat(),
                "event": event,
                "request_id": request_id_var.get(),
                **fields,
            },
            default=str,
            ensure_ascii=False,
        )
    )


def get_db_connection():
    """Connect to the TruckDispatchAgent PostgreSQL database."""
    required = [
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "DB_USER",
        "DB_PASSWORD",
    ]

    missing = [
        key for key in required
        if not os.getenv(key)
    ]

    if missing:
        raise RuntimeError(
            f"Missing database settings: {', '.join(missing)}"
        )

    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT")),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=5,
    )


app = FastAPI(
    title="TruckDispatchAgent API",
    description="Backend for the AI truck dispatch voice agent",
    version="0.3.0",
)


# --------------------------------------------------
# Operator authentication for admin/data endpoints
# --------------------------------------------------

# Shared operator token from the environment. Never hardcode it.
ADMIN_API_TOKEN = os.getenv("ADMIN_API_TOKEN")


def require_admin(request: Request):
    """FastAPI dependency protecting admin/data endpoints.

    Accepted form: `Authorization: Bearer <ADMIN_API_TOKEN>`.
    If no token is configured in the environment, access is refused
    (fail-closed) until the operator sets one. Wrong or missing tokens
    are logged without revealing their value.
    """
    supplied = request.headers.get("Authorization", "")

    if not ADMIN_API_TOKEN:
        log_event("admin_access_denied", reason="token_not_configured")
        raise HTTPException(
            status_code=503,
            detail=(
                "Admin endpoints are locked: set ADMIN_API_TOKEN in the "
                "server environment first."
            ),
        )

    if not supplied.startswith("Bearer "):
        log_event("admin_access_denied", reason="no_bearer_token")
        raise HTTPException(
            status_code=401,
            detail="Bearer token required. "
        )

    token = supplied[len("Bearer "):]

    if not hmac.compare_digest(token, ADMIN_API_TOKEN):
        log_event("admin_access_denied", reason="invalid_token")
        raise HTTPException(
            status_code=401,
            detail="Invalid admin token.",
        )


@app.middleware("http")
async def attach_request_id(request, call_next):
    """Assign a request/session ID and log one line per HTTP request."""
    request_id = (
        request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
    )
    request_id_var.set(request_id)

    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    log_event(
        "http_request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
    )
    return response


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class StreamVoiceRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(min_length=1, max_length=100)


class CallAttemptRequest(BaseModel):
    usdot_number: str = Field(min_length=1, max_length=30)

    call_status: Literal[
        "not_started",
        "initiated",
        "answered",
        "no_answer",
        "busy",
        "failed",
        "completed",
        "cancelled",
    ] = "not_started"

    outcome: str | None = Field(default=None, max_length=100)
    interested: bool | None = None

    consent_status: Literal[
        "unknown", "granted", "denied", "revoked"
    ] = "unknown"

    do_not_call: bool = False
    follow_up_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)


# Temporary conversation memory.
# Each conversation ID stores its own messages and last-active time.
HISTORY_MAX_MESSAGES = 20
HISTORY_TTL_SECONDS = 60 * 60  # 1 hour of inactivity expires a conversation.

conversation_histories: dict[str, dict] = {}

pending_carrier_reviews: dict[str, dict] = {}

# Which MC (USDOT) each conversation is currently working on. Memory is
# scoped to one carrier: when the conversation moves on to another MC,
# everything remembered about the previous one is deleted.
conversation_carriers: dict[str, str] = {}


def clear_conversation_memory(
    conversation_id: str,
    reason: str = "carrier_finished",
) -> bool:
    """Forget a conversation entirely (it finished working with its MC).

    Removes the message history, any pending review, and the carrier
    marker, so the next MC starts with a clean slate. Returns True if
    anything was actually removed.
    """
    removed = False
    for store in (
        conversation_histories,
        pending_carrier_reviews,
        conversation_carriers,
    ):
        if store.pop(conversation_id, None) is not None:
            removed = True

    if removed:
        log_event(
            "conversation_memory_cleared",
            conversation_id=conversation_id,
            reason=reason,
        )

    return removed


def get_history(conversation_id: str) -> list[dict[str, str]]:
    """Return a valid, recently active history list for this conversation."""
    entry = conversation_histories.get(conversation_id)
    now = time.monotonic()

    if (
        entry is None
        or now - entry["last_active"] > HISTORY_TTL_SECONDS
    ):
        if entry is not None:
            # The conversation expired: its pending review is stale.
            pending_carrier_reviews.pop(conversation_id, None)
            conversation_carriers.pop(conversation_id, None)

        entry = {"messages": [], "last_active": now}
        conversation_histories[conversation_id] = entry

        # Opportunistically prune fully expired conversations.
        expired = [
            key
            for key, item in conversation_histories.items()
            if now - item["last_active"] > HISTORY_TTL_SECONDS
        ]
        for key in expired:
            conversation_histories.pop(key, None)
            pending_carrier_reviews.pop(key, None)
            conversation_carriers.pop(key, None)

    entry["last_active"] = now
    return entry["messages"]


def save_history(
    conversation_id: str,
    messages: list[dict[str, str]],
):
    """Persist bounded history for this conversation."""
    entry = conversation_histories.get(conversation_id)

    if entry is None:
        entry = {"messages": [], "last_active": time.monotonic()}
        conversation_histories[conversation_id] = entry

    entry["messages"] = messages[-HISTORY_MAX_MESSAGES:]
    entry["last_active"] = time.monotonic()


class MockCallRequest(BaseModel):
    """Request to simulate one call outcome (dry-run calling workflow)."""

    usdot_number: str = Field(min_length=1, max_length=30)

    simulated_outcome: Literal[
        "interested",
        "not_interested",
        "no_answer",
        "invalid_number",
        "failed",
    ]

    notes: str | None = Field(default=None, max_length=4000)


MOCK_OUTCOME_TO_ATTEMPT = {
    "interested": {
        "call_status": "completed",
        "interested": True,
    },
    "not_interested": {
        "call_status": "completed",
        "interested": False,
    },
    "no_answer": {
        "call_status": "no_answer",
        "interested": None,
    },
    "invalid_number": {
        "call_status": "failed",
        "interested": None,
    },
    "failed": {
        "call_status": "failed",
        "interested": None,
    },
}


def record_audit(
    cur,
    action: str,
    usdot_number: str | None,
    details: dict | None = None,
):
    """Append one audited operator/system action inside the open transaction."""
    cur.execute(
        """
        INSERT INTO public.operator_audit_log (
            action, usdot_number, request_id, details
        )
        VALUES (%s, %s, %s, %s)
        """,
        (
            action,
            usdot_number,
            request_id_var.get() or None,
            Jsonb(details or {}),
        ),
    )


class CarrierReviewRequest(BaseModel):
    usdot_number: str = Field(min_length=1, max_length=30)

    review_status: Literal[
        "interested",
        "not_interested",
        "callback_requested",
        "do_not_contact",
    ]

    follow_up_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=4000)


class ChatResponse(BaseModel):
    reply: str


@app.get("/")
def home():
    return {
        "status": "running",
        "project": "TruckDispatchAgent",
    }


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/metrics", dependencies=[Depends(require_admin)])
def dashboard_metrics():
    """Read-only operational counts for the operator dashboard."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        COUNT(*) AS total_leads,
                        COUNT(*) FILTER (
                            WHERE basic_qualification_passed IS TRUE
                        ) AS qualified_leads,
                        COUNT(*) FILTER (
                            WHERE priority = 'high'
                        ) AS high_priority_leads,
                        COUNT(*) FILTER (
                            WHERE phone IS NOT NULL AND BTRIM(phone) <> ''
                        ) AS leads_with_phone
                    FROM public.carrier_leads
                """)
                lead_counts = dict(zip(
                    [
                        "total_leads",
                        "qualified_leads",
                        "high_priority_leads",
                        "leads_with_phone",
                    ],
                    cur.fetchone(),
                ))

                cur.execute("""
                    SELECT call_status, COUNT(*)
                    FROM public.carrier_call_attempts
                    GROUP BY call_status
                """)
                attempt_counts = {
                    status: count
                    for status, count in cur.fetchall()
                }

                cur.execute("""
                    SELECT review_status, COUNT(*)
                    FROM public.carrier_reviews
                    GROUP BY review_status
                """)
                review_counts = {
                    status: count
                    for status, count in cur.fetchall()
                }

                cur.execute("""
                    SELECT COUNT(*)
                    FROM public.carrier_leads
                    WHERE review_reserved_until > CURRENT_TIMESTAMP
                """)
                active_reservations = cur.fetchone()[0]

        return {
            "generated_at": datetime.now().astimezone().isoformat(),
            "outbound_call_mode": OUTBOUND_CALL_MODE,
            "leads": lead_counts,
            "call_attempts_by_status": attempt_counts,
            "reviews_by_status": review_counts,
            "active_reservations": active_reservations,
        }

    except Exception as exc:
        print(f"ERROR in /metrics: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not collect metrics. Check server terminal.",
        ) from exc


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    try:
        result = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Adam, a friendly AI truck dispatch assistant. "
                        "Keep responses concise and conversational. "
                        "Ask one question at a time. Never invent carrier "
                        "details, truck availability, or load information. "
                        "Do not claim that a real call or booking occurred."
                    ),
                },
                {"role": "user", "content": request.message},
            ],
            temperature=0.3,
        )

        reply = result.choices[0].message.content

        if not reply:
            raise HTTPException(
                status_code=502,
                detail="The AI returned an empty response.",
            )

        return ChatResponse(reply=reply.strip())

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="AI request failed. Check the API key, model access, "
                   "network connection, and provider limits.",
        )


@app.post("/voice")
def voice(request: ChatRequest):
    # First generate Adam's text response
    try:
        result = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Adam, a friendly AI truck dispatch "
                        "assistant. Keep responses concise and natural. "
                        "Never invent carrier or load information."
                    ),
                },
                {"role": "user", "content": request.message},
            ],
            temperature=0.3,
        )

        reply = result.choices[0].message.content
        if not reply:
            raise HTTPException(
                status_code=502,
                detail="The AI returned an empty response.",
            )

        reply = reply.strip()

        # Convert Adam's reply into speech
        audio = elevenlabs_client.text_to_speech.convert(
            text=reply,
            voice_id="JBFqnCBsd6RMkjVDRZzb",
            model_id="eleven_flash_v2_5",
            output_format="mp3_44100_128",
        )

        # Save the MP3 to a temporary file
        with tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False
        ) as temp_file:
            for chunk in audio:
                temp_file.write(chunk)
            audio_path = temp_file.name

        return FileResponse(
            audio_path,
            media_type="audio/mpeg",
            filename="adam_response.mp3",
            background=BackgroundTask(os.remove, audio_path),
        )

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=502,
            detail="Voice generation failed. Check your API keys, "
                   "provider limits, and network connection.",
        )
        

@app.post("/voice-chat")
async def voice_chat(audio_file: UploadFile = File(...)):
    audio_path = None

    try:
        # 1. Read the uploaded recording
        audio_bytes = await audio_file.read()

        if not audio_bytes:
            raise HTTPException(
                status_code=400,
                detail="The uploaded audio file is empty.",
            )

        # Basic upload-size limit: 15 MB
        if len(audio_bytes) > 15 * 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="Audio file must be 15 MB or smaller.",
            )

        filename = audio_file.filename or "recording.webm"

        # 2. Convert speech into text using Groq Whisper
        transcription = groq_client.audio.transcriptions.create(
            file=(filename, audio_bytes),
            model="whisper-large-v3-turbo",
            response_format="text",
        )
        
        transcript = str(transcription).strip()
        
        print("STEP 1 OK: Whisper transcription:", transcript)

        

        if not transcript:
            raise HTTPException(
                status_code=400,
                detail="No speech could be detected in the recording.",
            )

        # 3. Generate Adam's response
        result = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Adam, a friendly AI truck dispatch "
                        "assistant. Keep responses concise and natural. "
                        "Ask one question at a time. Never invent carrier "
                        "details, truck availability, or load information. "
                        "Never claim a call or booking actually happened."
                    ),
                },
                {"role": "user", "content": transcript},
            ],
            temperature=0.3,
        )

        reply = result.choices[0].message.content

        if not reply:
            raise HTTPException(
                status_code=502,
                detail="The AI returned an empty response.",
            )

        reply = reply.strip()
        print("STEP 2 OK: Groq generated reply:", reply)

        # 4. Convert Adam's response into speech
        print("STEP 3: Calling ElevenLabs...")
        audio = elevenlabs_client.text_to_speech.convert(
            text=reply,
            voice_id="JBFqnCBsd6RMkjVDRZzb",
            model_id="eleven_flash_v2_5",
            output_format="mp3_44100_128",
        )

        with tempfile.NamedTemporaryFile(
            suffix=".mp3", delete=False
        ) as temp_file:
            for chunk in audio:
                temp_file.write(chunk)

            audio_path = temp_file.name
            
        print("STEP 4 OK: ElevenLabs returned audio.")

        # 5. Return the MP3 plus the recognized text and reply
        return FileResponse(
            audio_path,
            media_type="audio/mpeg",
            filename="adam_voice_chat.mp3",
            headers={
                "X-Transcript": quote(transcript, safe=""),
                "X-Assistant-Reply": quote(reply, safe=""),
            },
            background=BackgroundTask(os.remove, audio_path),
        )

    except HTTPException:
        raise

    except Exception as exc:
        print(f"Voice chat error: {exc}")
        raise HTTPException(
            status_code=502,
            detail=(
                "Voice chat failed. Check the terminal for the "
                "underlying error and verify provider access and limits."
            ),
        )

    finally:
        await audio_file.close()

@app.get("/ui", include_in_schema=False)
def voice_interface():
    ui_path = PROJECT_ROOT / "static" / "voice.html"

    if not ui_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Voice interface file was not found.",
        )

    return FileResponse(ui_path, media_type="text/html")


@app.get("/dashboard", include_in_schema=False)
def operator_dashboard():
    dashboard_path = PROJECT_ROOT / "static" / "dashboard.html"

    if not dashboard_path.is_file():
        raise HTTPException(
            status_code=404,
            detail="Dashboard file was not found.",
        )

    return FileResponse(dashboard_path, media_type="text/html")


# --------------------------------------------------
# Carrier-pipeline UI (upload PDF, run all phases, live progress)
# --------------------------------------------------

PIPELINE_UPLOAD_DIR = PROJECT_ROOT / "data" / "uploads"
PIPELINE_MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB
PIPELINE_MAX_RECORDS = 200


class PipelineUploadRequest:
    """Parsed options for a pipeline run (filled from form fields)."""


@app.get("/pipeline", include_in_schema=False)
def pipeline_page():
    pipeline_html = PROJECT_ROOT / "static" / "pipeline.html"

    if not pipeline_html.is_file():
        raise HTTPException(
            status_code=404,
            detail="Pipeline page file was not found.",
        )

    return FileResponse(pipeline_html, media_type="text/html")


@app.post(
    "/pipeline/upload",
    dependencies=[Depends(require_admin)],
)
async def pipeline_upload(
    file: UploadFile = File(...),
    import_to_db: bool = Form(False),
    max_records: int = Form(25),
):
    """Start a pipeline run on an uploaded USDOT-carrier PDF.

    Admin-token protected. The run is executed in a background thread;
    poll `/pipeline/status/{job_id}` for live progress. Database import
    is opt-in (dry run by default).
    """
    import pipeline_service

    if not (1 <= max_records <= PIPELINE_MAX_RECORDS):
        raise HTTPException(
            status_code=400,
            detail=f"max_records must be between 1 and "
                   f"{PIPELINE_MAX_RECORDS}.",
        )

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted.",
        )

    PIPELINE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    # Sanitize the stored filename; never trust the client's path.
    safe_stem = "".join(
        char if char.isalnum() or char in "._- " else "_"
        for char in Path(file.filename).stem
    ).strip() or "register"
    destination = PIPELINE_UPLOAD_DIR / (
        f"{uuid.uuid4().hex[:8]}_{safe_stem[:80]}.pdf"
    )

    size = 0
    try:
        with destination.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > PIPELINE_MAX_PDF_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="PDF exceeds the 25 MB limit.",
                    )
                out.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"Could not save the uploaded PDF: {error}",
        )

    job = pipeline_service.start_job(
        pdf_path=destination,
        original_filename=file.filename,
        import_to_db=import_to_db,
        max_records=max_records,
    )

    log_event(
        "pipeline_job_started",
        job_id=job.job_id,
        filename=file.filename,
        size_bytes=size,
        import_to_db=import_to_db,
        max_records=max_records,
    )

    return {
        "job_id": job.job_id,
        "status": job.status,
        "status_url": f"/pipeline/status/{job.job_id}",
        "size_bytes": size,
        "import_to_db": import_to_db,
        "max_records": max_records,
    }

@app.get(
    "/pipeline/leads/{job_id}",
    dependencies=[Depends(require_admin)],
)
def pipeline_leads(job_id: str):
    """Qualified leads produced by one pipeline job (admin-token protected)."""
    import pipeline_service

    job = pipeline_service.registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown pipeline job id.",
        )

    leads_file = pipeline_service.RUNS_ROOT / job_id / "04_qualified.json"
    if not leads_file.is_file():
        raise HTTPException(
            status_code=404,
            detail="This job has no qualified leads yet.",
        )

    try:
        return json.loads(leads_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read job results: {error}",
        )


@app.get(
    "/pipeline/status/{job_id}",
    dependencies=[Depends(require_admin)],
)
def pipeline_status(job_id: str):
    """Live progress of one pipeline job (admin-token protected)."""
    import pipeline_service

    job = pipeline_service.registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Unknown pipeline job id.",
        )

    return job.to_dict()


# --------------------------------------------------
# Operator assistant (chatbot that runs real actions)
# --------------------------------------------------

class AssistantRequest(BaseModel):
    """One operator instruction to the assistant."""

    message: str = Field(min_length=1, max_length=1000)
    conversation_id: str = Field(
        default="operator-assistant", min_length=1, max_length=100
    )


# Actions the assistant may execute. Everything here is an existing,
# already-audited application operation. There are no delete/drop/reset
# actions, no live-calling action, and no spending action by design:
# "every permission" stops at anything destructive or that could cost
# money / contact a carrier for real.
ASSISTANT_ACTIONS = {
    "help": "Explain what the assistant can do.",
    "metrics": "Show lead/call/review counts (dashboard metrics).",
    "search_carriers": "Search leads. args: {q: text, limit: 1-100}",
    "next_carrier": "Reserve the next eligible carrier for review.",
    "save_review": (
        "Save a carrier review. args: {usdot_number, review_status "
        "(interested|not_interested|callback_requested|do_not_contact), "
        "follow_up_at (ISO-8601 datetime, required for callback_requested), "
        "notes (optional)}"
    ),
    "callbacks_due": (
        "List scheduled callback reviews. args: {due_only: bool "
        "(default true)}"
    ),
    "pipeline_status": (
        "Show pipeline job status. args: {job_id (optional; latest if "
        "omitted)}"
    ),
    "pipeline_leads": (
        "List qualified leads of a pipeline job. args: {job_id, limit}"
    ),
    "export_excel": (
        "Give the Excel export link. args: {review_status (optional: "
        "interested|not_interested|callback_requested|do_not_contact; "
        "omit for all leads)}"
    ),
}

ASSISTANT_REFUSAL_KEYWORDS = (
    "delete", "drop ", "truncate", "wipe", "erase", "remove all",
    "reset all", "purge", "rm -rf", "buy", "purchase", "pay ", "charge",
    "subscribe", "send email", "send sms", "dial", "real call",
    "place a call", "make the call", "go live", "call the carrier",
    "call this carrier", "call a carrier", "phone the carrier",
)

# Phrasing variants that are still "make a real phone call". Kept as
# patterns so "place a real call" is refused while ordinary questions
# such as "what is the outbound calling mode" are still allowed.
ASSISTANT_REFUSAL_PATTERNS = (
    re.compile(r"\b(place|make|start|begin|initiate)\b.{0,25}\bcall\b"),
    re.compile(r"\b(dial|phone)\b.{0,25}\b(carrier|lead|number|them|him|her)\b"),
    re.compile(r"\bcall\b.{0,25}\b(carrier|lead|them|him|her)\b"),
)


def _assistant_refusal_reason(message: str) -> str | None:
    """Return the reason a request must be refused, or None if allowed."""
    lowered = message.lower()

    for word in ASSISTANT_REFUSAL_KEYWORDS:
        if word in lowered:
            return word.strip()

    for pattern in ASSISTANT_REFUSAL_PATTERNS:
        if pattern.search(lowered):
            return "real phone call"

    return None

ASSISTANT_SYSTEM_PROMPT = (
    "You are the TruckDispatchAgent operator assistant. Convert the "
    "operator's request into ONE JSON object and output JSON only.\n"
    "Allowed actions and args:\n"
    + "\n".join(f"- {name}: {desc}" for name, desc in ASSISTANT_ACTIONS.items())
    + "\nIf the request is unclear, or asks for something destructive "
    "(deleting data), a real phone call, or spending money, return "
    '{"action": "help", "args": {}}.\n'
    'Output format: {"action": "<name>", "args": {...}}'
)


def _parse_follow_up_at(value):
    """Turn a follow-up time from the model/operator into a datetime.

    Accepts ISO-8601 ("2026-10-27T09:00") and relative phrases such as
    "in 2 hours" or "in 3 days". Naive times are treated as local time.
    """
    if value is None or str(value).strip() == "":
        return None

    text = str(value).strip()

    relative = re.match(
        r"^in\s+(\d{1,4})\s*(minute|min|hour|hr|day)s?$", text, re.IGNORECASE
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        if unit.startswith("min"):
            delta = timedelta(minutes=amount)
        elif unit.startswith("h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return datetime.now().astimezone() + delta

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not read that follow-up time. Use an ISO date "
                "(2026-10-27T09:00) or 'in 2 hours'."
            ),
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)

    return parsed


def _assistant_fallback_plan(message: str) -> dict:
    """Deterministic parser used if the model is unavailable/unclear."""
    text = message.strip()
    lowered = text.lower()

    if any(word in lowered for word in ("help", "what can you do")):
        return {"action": "help", "args": {}}

    if any(
        phrase in lowered
        for phrase in (
            "callbacks due",
            "callback due",
            "callback queue",
            "scheduled callbacks",
            "who is due",
            "who's due",
        )
    ):
        return {"action": "callbacks_due", "args": {"due_only": True}}

    if any(word in lowered for word in ("metric", "stats", "how many")):
        return {"action": "metrics", "args": {}}

    if "next carrier" in lowered or "next lead" in lowered:
        return {"action": "next_carrier", "args": {}}

    if "export" in lowered or "excel" in lowered:
        args = {}
        if "interested" in lowered:
            args["review_status"] = "interested"
        elif "callback" in lowered:
            args["review_status"] = "callback_requested"
        return {"action": "export_excel", "args": args}

    if "pipeline" in lowered or "job" in lowered:
        match = re.search(r"\b([0-9a-f]{8,16})\b", lowered)
        args = {"job_id": match.group(1)} if match else {}
        if "lead" in lowered:
            return {"action": "pipeline_leads", "args": args}
        return {"action": "pipeline_status", "args": args}

    match = re.match(
        r"^(?:mark|save|set)\s+(?:usdot\s+)?(\d{1,8})\s+"
        r"(?:as\s+)?(interested|not interested|callback|call back|"
        r"do not contact)\s*(.*)$",
        lowered,
    )
    if match:
        status_map = {
            "interested": "interested",
            "not interested": "not_interested",
            "callback": "callback_requested",
            "call back": "callback_requested",
            "do not contact": "do_not_contact",
        }
        args = {
            "usdot_number": match.group(1),
            "review_status": status_map[match.group(2)],
        }
        tail = match.group(3).strip()
        if tail:
            args["follow_up_at"] = tail
        return {"action": "save_review", "args": args}

    search = re.match(
        r"^(?:search|find|look up|lookup)\s+(.+)$", text, re.IGNORECASE
    )
    if search:
        return {
            "action": "search_carriers",
            "args": {"q": search.group(1).strip()[:100], "limit": 10},
        }

    return {"action": "help", "args": {}}


def plan_assistant_action(message: str) -> dict:
    """Ask the model for an action plan, with a safe deterministic fallback."""
    try:
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            temperature=0,
            max_completion_tokens=200,
            messages=[
                {
                    "role": "system",
                    "content": (
                        ASSISTANT_SYSTEM_PROMPT
                        + "\nCurrent local time: "
                        + datetime.now().astimezone().isoformat()
                        + "\nFor callback_requested, compute follow_up_at as an "
                        "ISO-8601 datetime in the future (or echo the "
                        "operator's relative phrase like 'in 2 hours')."
                    ),
                },
                {"role": "user", "content": message},
            ],
        )
        content = completion.choices[0].message.content or ""
        import json as _json

        start = content.find("{")
        end = content.rfind("}")
        plan = _json.loads(content[start : end + 1]) if start >= 0 else {}
        action = plan.get("action")
        args = plan.get("args") or {}

        if action not in ASSISTANT_ACTIONS or not isinstance(args, dict):
            return _assistant_fallback_plan(message)

        return {"action": action, "args": args}

    except Exception:
        return _assistant_fallback_plan(message)


def execute_assistant_action(action: str, args: dict, conversation_id: str):
    """Run one whitelisted action and return (reply, data)."""
    if action == "help":
        return (
            "I can do these for you: show metrics, search carriers, reserve "
            "the next carrier, save a review (e.g. \"mark 1234567 interested\" "
            "or \"mark 1234567 callback in 2 hours\"), list callbacks that "
            "are due, show pipeline job status and leads, and prepare the "
            "Excel export (e.g. \"export interested carriers to excel\"). "
            "I never delete data, never make real phone calls, and never "
            "spend money.",
            {"actions": sorted(ASSISTANT_ACTIONS)},
        )

    if action == "metrics":
        data = dashboard_metrics()
        leads = data.get("leads", {})
        return (
            f"You have {leads.get('total_leads', 0)} leads "
            f"({leads.get('high_priority_leads', 0)} high priority, "
            f"{leads.get('leads_with_phone', 0)} with phone numbers). "
            f"Active reservations: {data.get('active_reservations', 0)}. "
            f"Outbound calling mode: {data.get('outbound_call_mode')}.",
            data,
        )

    if action == "search_carriers":
        q = str(args.get("q", ""))[:100]
        try:
            limit = int(args.get("limit", 10))
        except (TypeError, ValueError):
            limit = 10
        limit = max(1, min(limit, 100))

        data = search_carriers(q=q, limit=limit)
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            return f"No carriers matched '{q}'.", data

        lines = [
            f"{row.get('usdot_number')} - {row.get('company_name')}"
            f" (score {row.get('lead_score')}, {row.get('priority')})"
            for row in results[:5]
        ]
        return (
            f"Found {len(results)} match(es) for '{q}':\n"
            + "\n".join(lines),
            data,
        )

    if action == "next_carrier":
        data = get_next_carrier(conversation_id)
        if not data.get("found"):
            return data.get("message", "No eligible carriers found."), data
        carrier = data.get("carrier", {})
        return (
            f"Reserved {carrier.get('usdot_number')} - "
            f"{carrier.get('company_name')} for review "
            f"(15-minute hold, no call is made).",
            data,
        )

    if action == "save_review":
        usdot = str(args.get("usdot_number", "")).strip()
        status = str(args.get("review_status", "")).strip()
        notes = args.get("notes")
        follow_up_at = _parse_follow_up_at(args.get("follow_up_at"))

        if not usdot.isdigit():
            raise HTTPException(
                status_code=400,
                detail="A numeric USDOT number is required to save a review.",
            )

        try:
            review_request = CarrierReviewRequest(
                usdot_number=usdot,
                review_status=status,
                notes=notes,
                follow_up_at=follow_up_at,
            )
        except Exception as error:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid review. Use interested, not_interested, "
                    f"callback_requested or do_not_contact. ({error})"
                ),
            )

        data = save_carrier_review(review_request)

        if status == "callback_requested" and follow_up_at:
            return (
                f"{data.get('message', 'Review saved.')} Callback scheduled "
                f"for {follow_up_at.isoformat(timespec='minutes')}. The "
                "carrier stays out of the eligible queue until then, and no "
                "call is made automatically.",
                data,
            )

        return f"{data.get('message', 'Review saved.')}", data

    if action == "callbacks_due":
        due_only = args.get("due_only", True)
        if isinstance(due_only, str):
            due_only = due_only.strip().lower() not in ("false", "0", "no")

        data = list_callbacks(due_only=bool(due_only))
        callbacks = data.get("callbacks", [])
        due_count = data.get("due_count", 0)

        if not callbacks:
            return (
                "No callbacks scheduled yet. Save one with: "
                "\"mark 1234567 callback in 2 hours\".",
                data,
            )

        lines = [
            f"{row['usdot_number']} - {row['company_name']} at "
            f"{str(row['follow_up_at'])[:16]}{' (DUE)' if row['due'] else ''}"
            for row in callbacks[:8]
        ]
        return (
            f"{due_count} callback(s) due now, {len(callbacks)} scheduled "
            "in total:\n" + "\n".join(lines) + "\nNo call is placed "
            "automatically while calling is offline.",
            data,
        )

    if action == "pipeline_status":
        import pipeline_service

        job_id = str(args.get("job_id", "")).strip()

        if job_id:
            job = pipeline_service.registry.get(job_id)
        else:
            jobs = pipeline_service.registry.list_jobs(limit=1)
            job = jobs[0] if jobs else None

        if job is None:
            return (
                "No pipeline job found yet. Upload a PDF on this page to "
                "start one.",
                {"job_id": job_id or None},
            )

        stages = ", ".join(
            f"{stage['name']}={stage['status']}" for stage in job.stages
        )
        return (
            f"Job {job.job_id} is {job.status}. Stages: {stages}.",
            job.to_dict(),
        )

    if action == "pipeline_leads":
        import pipeline_service

        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            jobs = pipeline_service.registry.list_jobs(limit=1)
            if not jobs:
                return "No pipeline job found yet.", {"leads": []}
            job_id = jobs[0].job_id

        try:
            limit = max(1, min(int(args.get("limit", 10)), 100))
        except (TypeError, ValueError):
            limit = 10

        data = pipeline_leads(job_id)
        leads = data[:limit] if isinstance(data, list) else []
        if not leads:
            return f"Job {job_id} has no qualified leads yet.", {"leads": []}

        lines = [
            f"{lead.get('usdot_number')} - {lead.get('company_name')} "
            f"(score {lead.get('lead_score')}, {lead.get('priority')})"
            for lead in leads[:5]
        ]
        return (
            f"Top leads in job {job_id}:\n" + "\n".join(lines),
            {"job_id": job_id, "leads": leads},
        )

    if action == "export_excel":
        status = str(args.get("review_status", "")).strip()
        allowed = (
            "interested",
            "not_interested",
            "callback_requested",
            "do_not_contact",
        )

        if status and status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=(
                    "review_status must be one of: " + ", ".join(allowed)
                ),
            )

        url = "/export-excel" + (f"?review_status={status}" if status else "")
        label = status.replace("_", " ") if status else "all leads"
        return (
            f"The Excel export for {label} is ready: {url} . "
            "Click 'Download Excel' in this panel to save it (it sends "
            "your admin token automatically).",
            {"url": url, "review_status": status or None},
        )

    raise HTTPException(status_code=400, detail="Unsupported action.")


@app.post("/assistant", dependencies=[Depends(require_admin)])
def operator_assistant(request: AssistantRequest):
    """Chatbot that executes whitelisted operator actions.

    Refuses destructive/billing/real-calling requests outright, before
    any model or database work happens.
    """
    blocked = _assistant_refusal_reason(request.message)

    if blocked:
        log_event(
            "assistant_action_refused",
            reason=blocked.strip(),
            conversation_id=request.conversation_id,
        )
        return {
            "reply": (
                "I can't do that. I never delete lead data, never place "
                "real phone calls, and never spend money. I can still show "
                "metrics, search carriers, reserve the next carrier, save "
                "reviews, and report pipeline results."
            ),
            "action": "refused",
            "data": {},
        }

    plan = plan_assistant_action(request.message)
    action = plan["action"]
    args = plan["args"]

    log_event(
        "assistant_action",
        action=action,
        conversation_id=request.conversation_id,
    )

    try:
        reply, data = execute_assistant_action(
            action, args, request.conversation_id
        )
    except HTTPException:
        raise
    except Exception as error:
        print(f"Assistant error: {type(error).__name__}: {error}")
        raise HTTPException(
            status_code=500,
            detail="The assistant could not complete that action.",
        ) from error

    return {"reply": reply, "action": action, "data": data}



def make_sse_event(data: dict) -> str:
    """Format one server-sent event as JSON."""
    return f"data: {json.dumps(data)}\n\n"



def save_confirmed_carrier_review(
    usdot_number: str,
    review_status: str,
    notes: str | None = None,
    follow_up_at: datetime | None = None,
):
    request = CarrierReviewRequest(
        usdot_number=usdot_number,
        review_status=review_status,
        notes=notes,
        follow_up_at=follow_up_at,
    )
    return save_carrier_review(request)


def normalize_command(message: str) -> str:
    """Normalize an STT utterance for command matching."""
    text = message.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


REVIEW_COMMAND_PATTERNS = {
    "interested": re.compile(
        r"^mark (this |the |that )?(carrier|one) (as )?interested$"
    ),
    "not_interested": re.compile(
        r"^mark (this |the |that )?(carrier|one) (as )?not interested$"
    ),
    "callback_requested": re.compile(
        r"^mark (this |the |that )?(carrier|one) (for )?(a )?callback$"
    ),
    "do_not_contact": re.compile(
        r"^mark (this |the |that )?(carrier|one) "
        r"(as )?do not (contact|call)$"
    ),
}

CONFIRM_COMMAND_PATTERNS = [
    re.compile(r"^yes[, ]*confirm (the )?carrier review( please)?$"),
    re.compile(r"^confirm (the )?carrier review( please)?$"),
    re.compile(r"^(yes, )?save the (confirmed )?carrier review$"),
]


def detect_review_request(message: str) -> str | None:
    """Recognize only explicit review commands."""
    command = normalize_command(message)

    for status, pattern in REVIEW_COMMAND_PATTERNS.items():
        if pattern.match(command):
            return status

    return None


def is_explicit_review_confirmation(message: str) -> bool:
    """Accept only clear confirmation commands."""
    command = normalize_command(message)

    return any(
        pattern.match(command)
        for pattern in CONFIRM_COMMAND_PATTERNS
    ) or command in {"confirm carrier review"}


@app.post("/stream-voice")
def stream_voice(request: StreamVoiceRequest):

    def generate():
        buffer = ""

        try:
            current_usdot_number = None
            carrier_context = "No carrier is currently reserved for review."
            try:
                with get_db_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                                    SELECT
                                        usdot_number,
                                        company_name,
                                        phone,
                                        vehicle_count,
                                        priority,
                                        lead_score
                                    FROM public.carrier_leads
                                    WHERE review_reserved_until > CURRENT_TIMESTAMP
                                        AND review_reserved_at IS NOT NULL
                                        AND review_reserved_by = %s
                                    ORDER BY review_reserved_at DESC
                                    LIMIT 1
                                """, (request.conversation_id,))
                        row = cur.fetchone()
                        
                        if row is not None:
                            current_usdot_number = row[0]
                            carrier_context = (
                                f"Company name: {row[1] or 'Not available'}\n"
                                f"USDOT number: {row[0]}\n"
                                f"Phone: {row[2] or 'Not available'}\n"
                                f"Vehicle count: "
                                f"{row[3] if row[3] is not None else 'Not available'}\n"
                                f"Priority: {row[4] or 'Not available'}\n"
                                f"Lead score: "
                                f"{row[5] if row[5] is not None else 'Not available'}"
                            )
            except Exception as exc:
                print(
                    f"Carrier context lookup failed: "
                    f"{type(exc).__name__}: {exc}"
                    )
                carrier_context = "Carrier information is temporarily unavailable."

            
            # Memory is scoped to the MC under review. When this
            # conversation moves to a different carrier, forget the
            # previous one so details never get mixed up.
            if current_usdot_number is not None:
                current_carrier = str(current_usdot_number)
                previous_carrier = conversation_carriers.get(
                    request.conversation_id
                )

                if (
                    previous_carrier is not None
                    and previous_carrier != current_carrier
                ):
                    clear_conversation_memory(
                        request.conversation_id,
                        reason="carrier_switched",
                    )

                conversation_carriers[request.conversation_id] = (
                    current_carrier
                )

            # Get this conversation's previous messages.
            history = get_history(request.conversation_id)
            
            if current_usdot_number:
                pending = pending_carrier_reviews.get(request.conversation_id)

                if pending is None or pending.get("usdot_number") != current_usdot_number:
                    pending_carrier_reviews.pop(request.conversation_id, None)
                    
            review_status = detect_review_request(request.message)

            if review_status and current_usdot_number:
                pending_carrier_reviews[request.conversation_id] = {
                    "usdot_number": current_usdot_number,
                    "review_status": review_status,
                    "follow_up_at": None,
                    "notes": None,
                    "confirmed": False,
                }
            
            
            review_action_message = ""
            memory_cleared_this_turn = False

            command = normalize_command(request.message)

            if re.match(r"^cancel (the )?carrier review$", command):
                pending_carrier_reviews.pop(request.conversation_id, None)
                review_action_message = (
                    "Pending carrier review cancelled. No database change was made."
                )

            elif is_explicit_review_confirmation(request.message):
                pending = pending_carrier_reviews.get(request.conversation_id)

                if not pending:
                    review_action_message = (
                        "No pending carrier review exists. "
                        "Do not claim a review was saved."
                    )

                elif (
                    not current_usdot_number
                    or pending["usdot_number"] != current_usdot_number
                ):
                    review_action_message = (
                        "The pending review does not match the current reserved carrier. "
                        "Do not save it."
                    )

                elif (
                    pending["review_status"] == "callback_requested"
                    and pending["follow_up_at"] is None
                ):
                    review_action_message = (
                        "A callback needs a follow-up date and time before it can be saved. "
                        "Ask the user to provide them."
                    )

                else:
                    with get_db_connection() as conn:
                          with conn.cursor() as cur:
                               cur.execute("""
                                    SELECT 1
                                    FROM public.carrier_leads
                                    WHERE usdot_number = %s
                                        AND review_reserved_by = %s
                                        AND review_reserved_until > CURRENT_TIMESTAMP
                                """, (
                                    pending["usdot_number"],
                                    request.conversation_id,
                                ))
                               reservation_is_owned = cur.fetchone() is not None
                        
                    if not reservation_is_owned:
                        review_action_message = (
                                "This conversation no longer owns the carrier reservation. "
                                "Do not save the review."
                            )
                    else:
                        save_result = save_confirmed_carrier_review(
                            usdot_number=pending["usdot_number"],
                            review_status=pending["review_status"],
                            notes=pending["notes"],
                            follow_up_at=pending["follow_up_at"],
                        )

                        pending_carrier_reviews.pop(request.conversation_id, None)

                        # The review is saved: this MC is done, so its
                        # conversation memory is deleted automatically.
                        clear_conversation_memory(
                            request.conversation_id,
                            reason="carrier_review_saved",
                        )
                        memory_cleared_this_turn = True

                        review_action_message = (
                            f"{save_result['message']} Tell the user the actual result."
                        )
                     

            
            
            system_message = {
                "role": "system",
                "content": (
                    "You are Adam, a friendly AI truck dispatch assistant. "
                    "Speak naturally and briefly, usually one or two sentences "
                    "at a time. Ask one question at a time. Use conversation "
                    "history to understand follow-up questions. Never invent "
                    "carrier details, truck availability, or load information. "
                    "Never claim a call or booking actually happened.\n\n"

                    "CURRENT CARRIER RESERVED FOR REVIEW:\n"
                    f"{carrier_context}\n\n"

                    "Use the carrier details above when relevant. If no carrier "
                    "is reserved or a detail is unavailable, say so honestly. "
                    "Do not invent missing information. You may discuss the "
                    "carrier, but you cannot initiate calls or claim that consent "
                    "has been obtained.\n\n"

                    "CARRIER REVIEW SAFETY RULES:\n"
                    "- Never claim a carrier review was saved unless the backend confirms it.\n"
                    "- When a review command has been detected but REVIEW ACTION RESULT "
                    "is empty, tell the user which review status is proposed and ask for confirmation.\n"
                    "- Tell the user to say exactly 'confirm carrier review' to save it.\n"
                    "- If REVIEW ACTION RESULT says the pending review was cancelled, "
                    "confirm cancellation and that no database change was made.\n"
                    "- If the user asks to cancel without an explicit cancellation command, "
                    "do not save anything; tell them to say 'cancel carrier review'.\n"
                    "- If REVIEW ACTION RESULT says a review was saved, report the actual result accurately.\n"
                    "- Never claim a phone call was made.\n"
                    "- A callback review needs a follow-up date and time before it can be saved.\n"

                    f"\nREVIEW ACTION RESULT: {review_action_message}"
                ),
            }


            # Build the prompt using the last 20 messages.
            messages = [
                system_message,
                *history[-HISTORY_MAX_MESSAGES:],
                {"role": "user", "content": request.message},
            ]

            # Collect the complete reply while streaming.
            assistant_reply_parts = []

            # Generate Adam's answer incrementally.
            llm_stream = groq_client.chat.completions.create(
                model="openai/gpt-oss-20b",
                temperature=0.3,
                reasoning_effort="low",
                max_completion_tokens=300,
                stream=True,
                messages=messages,
            )

            def speak_chunk(text: str):
                """Generate one playable audio chunk."""
                audio_stream = elevenlabs_client.text_to_speech.stream(
                    text=text,
                    voice_id="JBFqnCBsd6RMkjVDRZzb",
                    model_id="eleven_flash_v2_5",
                    output_format="mp3_44100_128",
                    optimize_streaming_latency=3,
                )

                audio_bytes = b"".join(audio_stream)

                if audio_bytes:
                    return {
                        "type": "audio",
                        "text": text,
                        "audio": base64.b64encode(
                            audio_bytes
                        ).decode("ascii"),
                    }

                return None

            
            def extract_sentence(text: str):
                """Release short phrases early to reduce first-audio latency."""

    # Speak immediately when a complete sentence is available.
                match = re.search(r'[.!?](?:["\']?)(?:\s|$)', text)
                if match:
                    return match.end()

    # Otherwise, release a short phrase without waiting
    # for the model to finish a long sentence.
                if len(text) >= 32:
                    split_at = text.rfind(" ", 0, 32)
                    if split_at > 20:
                        return split_at

                return None


            
            
            for event in llm_stream:
                if not event.choices:
                    continue

                delta = event.choices[0].delta.content

                if not delta:
                    continue

                assistant_reply_parts.append(delta)
                
                buffer += delta

                while True:
                    cut = extract_sentence(buffer)
                    if cut is None:
                        break

                    sentence = buffer[:cut].strip()
                    buffer = buffer[cut:].lstrip()

                    if sentence:
                        audio_event = speak_chunk(sentence)
                        if audio_event:
                            yield make_sse_event(audio_event)

            # Speak any remaining text after the model finishes.
            if buffer.strip():
                audio_event = speak_chunk(buffer.strip())
                if audio_event:
                    yield make_sse_event(audio_event)
                    
            assistant_reply = "".join(assistant_reply_parts).strip()

            if assistant_reply:
                # Save the turn only after the reply fully succeeded,
                # so failed generations do not pollute the history.
                # If the finished MC's memory was just cleared, stay
                # clean instead of writing this closing turn back.
                if not memory_cleared_this_turn:
                    history.append({
                        "role": "user",
                        "content": request.message,
                    })
                    history.append({
                        "role": "assistant",
                        "content": assistant_reply,
                    })

                    save_history(request.conversation_id, history)

            print("STREAM COMPLETED")

            yield make_sse_event({"type": "done"})

        except Exception as exc:
            print(f"Streaming voice error: {exc}")
            yield make_sse_event({
                "type": "error",
                "message": (
                    "Adam could not finish the response. "
                    "Check the server terminal."
                ),
            })

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )

@app.post("/carrier-call-attempts", dependencies=[Depends(require_admin)])
def save_call_attempt(request: CallAttemptRequest):
    """Save one carrier outreach result to PostgreSQL."""

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM public.carrier_leads
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Carrier USDOT number was not found.",
                    )

                cur.execute(
                    """
                    INSERT INTO public.carrier_call_attempts (
                        usdot_number,
                        call_status,
                        outcome,
                        interested,
                        consent_status,
                        do_not_call,
                        follow_up_at,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING
                        id,
                        usdot_number,
                        attempted_at,
                        call_status,
                        interested,
                        do_not_call
                    """,
                    (
                        request.usdot_number,
                        request.call_status,
                        request.outcome,
                        request.interested,
                        request.consent_status,
                        request.do_not_call,
                        request.follow_up_at,
                        request.notes,
                    ),
                )

                row = cur.fetchone()
                
                cur.execute(
                    """ UPDATE public.carrier_leads
                     SET
                        review_reserved_at = NULL,
                        review_reserved_until = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                record_audit(
                    cur,
                    action="call_attempt_saved",
                    usdot_number=request.usdot_number,
                    details={
                        "origin": "manual",
                        "call_status": request.call_status,
                        "interested": request.interested,
                        "consent_status": request.consent_status,
                        "do_not_call": request.do_not_call,
                    },
                )

                return {
                    "success": True,
                    "message": "Call outcome saved.",
                    "attempt": {
                        "id": row[0],
                        "usdot_number": row[1],
                        "attempted_at": row[2].isoformat(),
                        "call_status": row[3],
                        "interested": row[4],
                        "do_not_call": row[5],
                    },
                }

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Database save error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not save the call outcome.",
        )


@app.post("/carrier-reviews", dependencies=[Depends(require_admin)])
def save_carrier_review(request: CarrierReviewRequest):
    """Save a carrier review without recording a phone call."""

    if (
        request.review_status == "callback_requested"
        and request.follow_up_at is None
    ):
        raise HTTPException(
            status_code=400,
            detail="A follow-up date and time are required for callbacks.",
        )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM public.carrier_leads
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Carrier USDOT number was not found.",
                    )

                cur.execute(
                    """
                    INSERT INTO public.carrier_reviews (
                        usdot_number,
                        review_status,
                        follow_up_at,
                        notes
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING
                        id,
                        usdot_number,
                        review_status,
                        follow_up_at,
                        reviewed_at
                    """,
                    (
                        request.usdot_number,
                        request.review_status,
                        request.follow_up_at,
                        request.notes,
                    ),
                )

                row = cur.fetchone()

                cur.execute(
                    """
                    UPDATE public.carrier_leads
                    SET
                        review_reserved_at = NULL,
                        review_reserved_until = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                record_audit(
                    cur,
                    action="carrier_review_saved",
                    usdot_number=request.usdot_number,
                    details={
                        "origin": "api",
                        "review_status": request.review_status,
                    },
                )

                return {
                    "success": True,
                    "message": "Carrier review saved. No phone call was made.",
                    "review": {
                        "id": row[0],
                        "usdot_number": row[1],
                        "review_status": row[2],
                        "follow_up_at": (
                            row[3].isoformat() if row[3] else None
                        ),
                        "reviewed_at": row[4].isoformat(),
                    },
                }

    except HTTPException:
        raise
    except Exception as exc:
        print(
            f"Carrier review save error: {type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail="Could not save the carrier review.",
        ) from exc




@app.post("/mock-call", dependencies=[Depends(require_admin)])
def place_mock_call(request: MockCallRequest):
    """Dry-run the calling workflow: simulate one outcome, store it as a
    real attempt row, release reservations, and audit the action.

    Never places a real call and requires OUTBOUND_CALL_MODE=mock.
    """

    if OUTBOUND_CALL_MODE != "mock":
        raise HTTPException(
            status_code=409,
            detail=(
                "Mock calling is disabled. Set OUTBOUND_CALL_MODE=mock "
                "to enable the dry-run calling workflow."
            ),
        )

    mapping = MOCK_OUTCOME_TO_ATTEMPT[request.simulated_outcome]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM public.carrier_leads
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                if cur.fetchone() is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Carrier USDOT number was not found.",
                    )

                # Never create duplicate records for carriers whose
                # consent history forbids further contact.
                cur.execute(
                    """
                    SELECT 1
                    FROM public.carrier_call_attempts
                    WHERE usdot_number = %s
                        AND (
                            do_not_call IS TRUE
                            OR consent_status IN ('denied', 'revoked')
                        )
                    """,
                    (request.usdot_number,),
                )

                if cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "This carrier has a do-not-call or revoked "
                            "consent record. Mock calls are not permitted."
                        ),
                    )

                simulate_notes = (
                    f"MOCK CALL ({request.simulated_outcome})."
                    + (f" {request.notes}" if request.notes else "")
                )

                cur.execute(
                    """
                    INSERT INTO public.carrier_call_attempts (
                        usdot_number,
                        call_status,
                        outcome,
                        interested,
                        consent_status,
                        do_not_call,
                        notes
                    )
                    VALUES (%s, %s, 'simulated_outcome', %s, 'unknown',
                            FALSE, %s)
                    RETURNING
                        id,
                        usdot_number,
                        attempted_at,
                        call_status,
                        interested,
                        do_not_call
                    """,
                    (
                        request.usdot_number,
                        mapping["call_status"],
                        mapping["interested"],
                        simulate_notes,
                    ),
                )

                row = cur.fetchone()

                cur.execute(
                    """
                    UPDATE public.carrier_leads
                    SET
                        review_reserved_at = NULL,
                        review_reserved_until = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE usdot_number = %s
                    """,
                    (request.usdot_number,),
                )

                record_audit(
                    cur,
                    action="mock_call_recorded",
                    usdot_number=request.usdot_number,
                    details={
                        "simulated_outcome": request.simulated_outcome,
                        "call_status": mapping["call_status"],
                    },
                )

        log_event(
            "mock_call_recorded",
            usdot_number=request.usdot_number,
            simulated_outcome=request.simulated_outcome,
        )

        return {
            "success": True,
            "message": (
                "Mock call recorded. This was NOT a real phone call."
            ),
            "attempt": {
                "id": row[0],
                "usdot_number": row[1],
                "attempted_at": row[2].isoformat(),
                "call_status": row[3],
                "interested": row[4],
                "do_not_call": row[5],
            },
        }

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Mock call save error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not record the mock call outcome.",
        ) from exc


@app.get("/carriers/search", dependencies=[Depends(require_admin)])
def search_carriers(
    q: str = Query(default="", max_length=100),
    limit: int = 25,
):
    """Read-only operator search over leads and their latest outcomes."""
    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=400,
            detail="Limit must be between 1 and 100.",
        )

    term = f"%{q.strip()}%"

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.usdot_number,
                        c.company_name,
                        c.phone,
                        c.vehicle_count,
                        c.lead_score,
                        c.priority,
                        c.basic_qualification_passed,
                        r.review_status,
                        r.follow_up_at
                    FROM public.carrier_leads AS c
                    LEFT JOIN LATERAL (
                        SELECT review_status, follow_up_at
                        FROM public.carrier_reviews
                        WHERE usdot_number = c.usdot_number
                        ORDER BY reviewed_at DESC, id DESC
                        LIMIT 1
                    ) AS r ON TRUE
                    LEFT JOIN LATERAL (
                        SELECT COUNT(*) AS attempts
                        FROM public.carrier_call_attempts AS a
                        WHERE a.usdot_number = c.usdot_number
                    ) AS att ON TRUE
                    WHERE (
                        %s = ''
                        OR c.company_name ILIKE %s
                        OR c.usdot_number ILIKE %s
                    )
                    ORDER BY
                        CASE c.priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            ELSE 3
                        END,
                        c.lead_score DESC NULLS LAST
                    LIMIT %s
                    """,
                    (q.strip(), term, term, limit),
                )

                rows = cur.fetchall()

        results = []
        for row in rows:
            results.append(
                {
                    "usdot_number": row[0],
                    "company_name": row[1],
                    "phone": row[2],
                    "vehicle_count": row[3],
                    "lead_score": row[4],
                    "priority": row[5],
                    "basic_qualification_passed": row[6],
                    "latest_review_status": row[7],
                    "callback_scheduled_for": (
                        row[8].isoformat() if row[8] else None
                    ),
                }
            )

        return {"count": len(rows), "results": results}

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Search error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not search carriers.",
        ) from exc


def list_callbacks(due_only: bool = True) -> dict:
    """Read-only list of carriers whose latest review is a callback.

    Callbacks whose follow-up time has passed become eligible for the
    normal reservation queue again; this view just reports them.
    """
    due_clause = (
        "AND r.follow_up_at <= CURRENT_TIMESTAMP" if due_only else ""
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT
                        c.usdot_number,
                        c.company_name,
                        c.phone,
                        c.lead_score,
                        c.priority,
                        r.follow_up_at,
                        r.notes,
                        (r.follow_up_at <= CURRENT_TIMESTAMP) AS due
                    FROM public.carrier_leads AS c
                    JOIN LATERAL (
                        SELECT
                            review_status,
                            follow_up_at,
                            notes,
                            reviewed_at,
                            id
                        FROM public.carrier_reviews
                        WHERE usdot_number = c.usdot_number
                        ORDER BY reviewed_at DESC, id DESC
                        LIMIT 1
                    ) AS r ON TRUE
                    WHERE r.review_status = 'callback_requested'
                      AND r.follow_up_at IS NOT NULL
                      {due_clause}
                    ORDER BY r.follow_up_at
                    LIMIT 100
                    """
                )

                rows = cur.fetchall()

    except HTTPException:
        raise
    except Exception as exc:
        print(f"Callback list error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not load scheduled callbacks.",
        ) from exc

    callbacks = [
        {
            "usdot_number": row[0],
            "company_name": row[1],
            "phone": row[2],
            "lead_score": row[3],
            "priority": row[4],
            "follow_up_at": row[5].isoformat() if row[5] else None,
            "notes": row[6],
            "due": bool(row[7]),
        }
        for row in rows
    ]

    return {
        "callbacks": callbacks,
        "total": len(callbacks),
        "due_count": sum(1 for item in callbacks if item["due"]),
        "due_only": due_only,
    }


@app.get("/eligible-carriers", dependencies=[Depends(require_admin)])
def get_eligible_carriers(limit: int = 25):
    """Return qualified carrier candidates for human review."""

    if not 1 <= limit <= 100:
        raise HTTPException(
            status_code=400,
            detail="Limit must be between 1 and 100.",
        )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        c.usdot_number,
                        c.company_name,
                        c.phone,
                        c.vehicle_count,
                        c.lead_score,
                        c.priority
                    FROM public.carrier_leads AS c
                    WHERE c.phone IS NOT NULL
                      AND BTRIM(c.phone) <> ''
                      AND c.basic_qualification_passed IS TRUE
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.carrier_call_attempts AS a
                          WHERE a.usdot_number = c.usdot_number
                              AND (
                                  a.do_not_call IS TRUE
                                  OR a.consent_status IN ('denied', 'revoked')
                          )
                      )

                      -- Exclude carriers with a final review outcome,
                      -- consistent with /next-carrier.
                      AND NOT EXISTS (
                          SELECT 1
                          FROM public.carrier_reviews AS r
                          WHERE r.usdot_number = c.usdot_number
                              AND NOT EXISTS (
                                  SELECT 1
                                  FROM public.carrier_reviews AS newer
                                  WHERE newer.usdot_number = r.usdot_number
                                      AND (
                                          newer.reviewed_at > r.reviewed_at
                                          OR (
                                              newer.reviewed_at = r.reviewed_at
                                              AND newer.id > r.id
                                          )
                                      )
                              )
                              AND (
                                  r.review_status IN (
                                      'interested',
                                      'not_interested',
                                      'do_not_contact'
                                  )
                                  OR (
                                      r.review_status = 'callback_requested'
                                      AND r.follow_up_at > CURRENT_TIMESTAMP
                                  )
                              )
                      )
                    ORDER BY
                        c.lead_score DESC NULLS LAST
                    LIMIT %s
                    """,
                    (limit,),
                )

                rows = cur.fetchall()

                return {
                    "count": len(rows),
                    "candidates": [
                        {
                            "usdot_number": row[0],
                            "company_name": row[1],
                            "phone": row[2],
                            "vehicle_count": row[3],
                            "lead_score": row[4],
                            "priority": row[5],
                        }
                        for row in rows
                    ],
                    "note": (
                        "Candidates are for review only. "
                        "Their presence does not authorize an outbound call."
                    ),
                }

    except Exception as exc:
        print(
            f"Database candidate lookup error: "
            f"{type(exc).__name__}: {exc}"
        )
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve carrier candidates.",
        )


@app.get("/next-carrier", dependencies=[Depends(require_admin)])
def get_next_carrier(conversation_id: str):
    """Return the highest-priority eligible carrier for review."""

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    
                    WITH candidate AS (
                        SELECT c.usdot_number
                        FROM carrier_leads AS c
                        WHERE c.basic_qualification_passed IS TRUE
                            AND c.phone IS NOT NULL
                            AND TRIM(c.phone) <> ''

                            AND (
                                c.review_reserved_until IS NULL
                                OR c.review_reserved_until < CURRENT_TIMESTAMP
                            )

                            AND NOT EXISTS (
                                SELECT 1
                                FROM carrier_call_attempts AS a
                                WHERE a.usdot_number = c.usdot_number
                                    AND (
                                        a.do_not_call IS TRUE
                                        OR a.consent_status IN ('denied', 'revoked')
                                    )
                            )

                            AND NOT EXISTS (
                                SELECT 1
                                FROM carrier_call_attempts AS a
                                WHERE a.usdot_number = c.usdot_number
                            )
                            
                            AND NOT EXISTS (
                                SELECT 1
                                FROM public.carrier_reviews AS r
                                WHERE r.usdot_number = c.usdot_number
                                    AND NOT EXISTS (
                                        SELECT 1
                                        FROM public.carrier_reviews AS newer
                                        WHERE newer.usdot_number = r.usdot_number
                                            AND (
                                                newer.reviewed_at > r.reviewed_at
                                                OR (
                                                    newer.reviewed_at = r.reviewed_at
                                                    AND newer.id > r.id
                                                )
                                            )
                                    )
                                    AND (
                                        r.review_status IN (
                                            'interested',
                                            'not_interested',
                                            'do_not_contact'
                                        )
                                        OR (
                                            r.review_status = 'callback_requested'
                                            AND r.follow_up_at > CURRENT_TIMESTAMP
                                        )
                                    ) 
                            )

                        ORDER BY
                            CASE c.priority
                                WHEN 'high' THEN 1
                                WHEN 'medium' THEN 2
                                ELSE 3
                            END,
                            c.lead_score DESC NULLS LAST,
                            c.usdot_number

                        FOR UPDATE OF c SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE carrier_leads AS c
                    SET
                        review_reserved_at = CURRENT_TIMESTAMP,
                        review_reserved_until = CURRENT_TIMESTAMP + INTERVAL '15 minutes',
                        review_reserved_by = %s
                        FROM candidate
                        WHERE c.usdot_number = candidate.usdot_number
                        RETURNING
                            c.usdot_number,
                            c.company_name,
                            c.phone,
                            c.vehicle_count,
                            c.lead_score,
                            c.priority;

                """, (conversation_id,))

                row = cur.fetchone()

                if row is None:
                    return {
                        "found": False,
                        "message": "No eligible carrier candidates found."
                    }

                columns = [desc.name for desc in cur.description]
                carrier = dict(zip(columns, row))

                # A fresh MC was reserved: the previous carrier's
                # conversation memory is no longer relevant.
                clear_conversation_memory(
                    conversation_id,
                    reason="new_carrier_reserved",
                )

                return {
                    "found": True,
                    "carrier": carrier,
                    "call_authorized": False,
                    "message": (
                        "Candidate selected for review. "
                        "Verify consent and call permissions before calling."
                    )
                }
    except Exception as exc:
        print(f"ERROR in /next-carrier: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not retrieve the next carrier. Check server terminal."
        ) from exc


@app.get("/export-excel", dependencies=[Depends(require_admin)])
def export_excel(
    review_status: Literal[
        "interested",
        "not_interested",
        "callback_requested",
        "do_not_contact",
    ]
    | None = Query(default=None),
):
    """Export carrier leads and their latest review outcomes to Excel.

    Optionally filter to carriers whose latest review has the given
    status (e.g. `?review_status=interested`).
    """

    where_clause = ""
    params: tuple = ()
    if review_status is not None:
        where_clause = "WHERE r.review_status = %s"
        params = (review_status,)

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT
                        c.usdot_number,
                        c.company_name,
                        c.phone,
                        c.vehicle_count,
                        c.lead_score,
                        c.priority,
                        r.review_status,
                        r.follow_up_at,
                        r.notes,
                        r.reviewed_at
                    FROM public.carrier_leads AS c
                    LEFT JOIN LATERAL (
                        SELECT
                            review_status,
                            follow_up_at,
                            notes,
                            reviewed_at
                        FROM public.carrier_reviews
                        WHERE usdot_number = c.usdot_number
                        ORDER BY reviewed_at DESC, id DESC
                        LIMIT 1
                    ) AS r ON TRUE
                    {where_clause}
                    ORDER BY
                        CASE c.priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            ELSE 3
                        END,
                        c.lead_score DESC NULLS LAST,
                        c.company_name;
                """, params)

                rows = cur.fetchall()
                headers = [column.name for column in cur.description]

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Carrier Leads"

        worksheet.append(headers)

        for row in rows:
            worksheet.append([
                value.replace(tzinfo=None) if hasattr(value, "tzinfo") and value.tzinfo else value
                for value in row
            ])

        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions

        for column_cells in worksheet.columns:
            max_length = max(
                len(str(cell.value)) if cell.value is not None else 0
                for cell in column_cells
            )
            worksheet.column_dimensions[
                column_cells[0].column_letter
            ].width = min(max_length + 2, 40)

        output = BytesIO()
        workbook.save(output)
        output.seek(0)

        filename = (
            "carrier_leads.xlsx"
            if review_status is None
            else f"carrier_leads_{review_status}.xlsx"
        )

        return StreamingResponse(
            output,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    except Exception as exc:
        print(f"ERROR in /export-excel: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not export carrier data. Check server terminal.",
        ) from exc
