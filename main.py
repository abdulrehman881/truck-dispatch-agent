import os
import time
import uuid
import hmac
import logging
import threading
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
# Scheduled tasks (persistent operator to-do queue)
# --------------------------------------------------
# Every follow-up is stored in PostgreSQL, so schedules survive server
# restarts. A background worker flips tasks from "pending" to "due" when
# their time arrives. Because outbound calling is offline, a due callback
# becomes a visible reminder for the operator — the app never dials by
# itself.

SCHEDULED_TASKS_DDL = (
    """
    CREATE TABLE IF NOT EXISTS public.scheduled_tasks (
        id BIGSERIAL PRIMARY KEY,
        task_type TEXT NOT NULL,
        usdot_number TEXT,
        title TEXT NOT NULL,
        details TEXT,
        due_at TIMESTAMPTZ NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        source TEXT NOT NULL DEFAULT 'api',
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        notified_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS scheduled_tasks_status_due_idx
    ON public.scheduled_tasks (status, due_at)
    """,
)

TASK_STATUSES = ("pending", "due", "done", "cancelled")
TASK_TYPES = ("callback", "reminder")

TASK_SELECT_COLUMNS = (
    "id, task_type, usdot_number, title, details, due_at, status, "
    "source, notified_at, completed_at"
)

TASK_SCHEDULER_ENABLED = os.getenv(
    "TASK_SCHEDULER_ENABLED", "true"
).strip().lower() not in ("false", "0", "no")

TASK_SCHEDULER_POLL_SECONDS = max(
    5, int(os.getenv("TASK_SCHEDULER_POLL_SECONDS", "30"))
)

_scheduler_stop = threading.Event()
_scheduler_thread = None


def ensure_scheduled_tasks_table():
    """Create the task table/index if missing, then backfill callbacks."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for statement in SCHEDULED_TASKS_DDL:
                cur.execute(statement)

            # Existing callback reviews predating the task queue get one
            # task each (idempotent: only when no active task exists).
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (usdot_number)
                        usdot_number,
                        review_status,
                        follow_up_at,
                        notes
                    FROM public.carrier_reviews
                    ORDER BY usdot_number, reviewed_at DESC, id DESC
                )
                INSERT INTO public.scheduled_tasks (
                    task_type, usdot_number, title, details, due_at,
                    source, payload
                )
                SELECT
                    'callback',
                    latest.usdot_number,
                    'Call back carrier ' || latest.usdot_number,
                    latest.notes,
                    latest.follow_up_at,
                    'backfill',
                    '{}'::jsonb
                FROM latest
                WHERE latest.review_status = 'callback_requested'
                  AND latest.follow_up_at IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM public.scheduled_tasks AS t
                      WHERE t.usdot_number = latest.usdot_number
                        AND t.task_type = 'callback'
                        AND t.status IN ('pending', 'due')
                  )
                """
            )


def _task_row_to_dict(row) -> dict:
    return {
        "id": row[0],
        "task_type": row[1],
        "usdot_number": row[2],
        "title": row[3],
        "details": row[4],
        "due_at": row[5].isoformat() if row[5] else None,
        "status": row[6],
        "source": row[7],
        "notified_at": row[8].isoformat() if row[8] else None,
        "completed_at": row[9].isoformat() if row[9] else None,
    }


def _insert_task(
    cur,
    task_type,
    title,
    due_at,
    usdot_number=None,
    details=None,
    source="api",
    payload=None,
):
    """Insert one task using an existing transaction cursor."""
    if task_type not in TASK_TYPES:
        raise ValueError(f"Unknown task type: {task_type}")

    if task_type == "callback" and usdot_number:
        # Rescheduling replaces the previous active callback task.
        cur.execute(
            """
            UPDATE public.scheduled_tasks
            SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
            WHERE usdot_number = %s
              AND task_type = 'callback'
              AND status IN ('pending', 'due')
            """,
            (usdot_number,),
        )

    cur.execute(
        f"""
        INSERT INTO public.scheduled_tasks (
            task_type, usdot_number, title, details, due_at, source,
            payload
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING {TASK_SELECT_COLUMNS}
        """,
        (
            task_type,
            usdot_number,
            title,
            details,
            due_at,
            source,
            Jsonb(payload or {}),
        ),
    )

    return _task_row_to_dict(cur.fetchone())


def create_task(
    task_type,
    title,
    due_at,
    usdot_number=None,
    details=None,
    source="api",
    payload=None,
):
    """Create a scheduled task in its own transaction."""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            return _insert_task(
                cur,
                task_type=task_type,
                title=title,
                due_at=due_at,
                usdot_number=usdot_number,
                details=details,
                source=source,
                payload=payload,
            )


def list_tasks(
    status=None,
    due_only=False,
    include_done=False,
    limit=100,
):
    """Return scheduled tasks (open tasks by default)."""
    limit = max(1, min(int(limit), 200))
    clauses = []
    params = []

    if status:
        if status not in TASK_STATUSES:
            raise ValueError(f"Unknown task status: {status}")
        clauses.append("status = %s")
        params.append(status)
    elif not include_done:
        clauses.append("status IN ('pending', 'due')")

    if due_only:
        clauses.append("due_at <= CURRENT_TIMESTAMP")
        clauses.append("status IN ('pending', 'due')")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {TASK_SELECT_COLUMNS}
                FROM public.scheduled_tasks
                {where}
                ORDER BY due_at, id
                LIMIT %s
                """,
                tuple(params),
            )
            rows = cur.fetchall()

    return [_task_row_to_dict(row) for row in rows]


def update_task_status(task_id, status):
    """Move a task to done/cancelled/pending. Returns the updated row."""
    if status not in ("done", "cancelled", "pending"):
        raise ValueError(f"Unsupported task status: {status}")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE public.scheduled_tasks
                SET
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP,
                    completed_at = CASE
                        WHEN %s = 'done' THEN CURRENT_TIMESTAMP
                        ELSE completed_at
                    END
                WHERE id = %s
                RETURNING {TASK_SELECT_COLUMNS}
                """,
                (status, status, int(task_id)),
            )
            row = cur.fetchone()

    return _task_row_to_dict(row) if row else None


def process_due_tasks(limit=25):
    """Flip pending tasks whose time has come to 'due' (reminders).

    Never places a call. Returns how many tasks became due.
    """
    due_tasks = []

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, task_type, usdot_number, title, due_at
                FROM public.scheduled_tasks
                WHERE status = 'pending'
                  AND due_at <= CURRENT_TIMESTAMP
                ORDER BY due_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )

            for row in cur.fetchall():
                task_id, task_type, usdot_number, title, due_at = row
                cur.execute(
                    """
                    UPDATE public.scheduled_tasks
                    SET
                        status = 'due',
                        notified_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (task_id,),
                )
                due_tasks.append(
                    {
                        "task_id": task_id,
                        "task_type": task_type,
                        "usdot_number": usdot_number,
                        "title": title,
                        "due_at": due_at.isoformat() if due_at else None,
                    }
                )

    for task in due_tasks:
        log_event("task_due", **task)

    return len(due_tasks)


def _scheduler_loop():
    while not _scheduler_stop.wait(TASK_SCHEDULER_POLL_SECONDS):
        try:
            count = process_due_tasks()
            if count:
                log_event("task_scheduler_processed", due_tasks=count)
        except Exception as error:
            log_event(
                "task_scheduler_error",
                error=f"{type(error).__name__}: {error}",
            )


def start_task_scheduler():
    global _scheduler_thread

    if not TASK_SCHEDULER_ENABLED:
        return

    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return

    try:
        ensure_scheduled_tasks_table()
    except Exception as error:
        log_event(
            "task_scheduler_schema_error",
            error=f"{type(error).__name__}: {error}",
        )

    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        name="task-scheduler",
        daemon=True,
    )
    _scheduler_thread.start()
    log_event(
        "task_scheduler_started",
        poll_seconds=TASK_SCHEDULER_POLL_SECONDS,
    )


def stop_task_scheduler():
    _scheduler_stop.set()


@app.on_event("startup")
def _start_scheduler_on_startup():
    start_task_scheduler()


@app.on_event("shutdown")
def _stop_scheduler_on_shutdown():
    stop_task_scheduler()


class ReminderRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    details: str | None = Field(default=None, max_length=2000)
    due_at: str = Field(min_length=1, max_length=100)


@app.get("/tasks", dependencies=[Depends(require_admin)])
def get_tasks(
    status: Literal["pending", "due", "done", "cancelled"] | None = None,
    due_only: bool = False,
    include_done: bool = False,
    limit: int = 100,
):
    """List scheduled tasks/reminders (admin token required)."""
    try:
        tasks = list_tasks(
            status=status,
            due_only=due_only,
            include_done=include_done,
            limit=limit,
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Task list error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not load scheduled tasks.",
        ) from exc

    return {
        "tasks": tasks,
        "total": len(tasks),
        "due_count": sum(1 for task in tasks if task["status"] == "due"),
    }


@app.post("/tasks/reminders", dependencies=[Depends(require_admin)])
def create_reminder(request: ReminderRequest):
    """Store a reminder for the operator."""
    due_at = _parse_follow_up_at(request.due_at)

    try:
        task = create_task(
            "reminder",
            request.title,
            due_at,
            details=request.details,
            source="operator",
        )
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Reminder create error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not save the reminder.",
        ) from exc

    log_event("task_created", task_id=task["id"], task_type="reminder")
    return {"success": True, "task": task}


@app.post("/tasks/{task_id}/done", dependencies=[Depends(require_admin)])
def complete_task(task_id: int):
    return _set_task_status(task_id, "done")


@app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_task(task_id: int):
    return _set_task_status(task_id, "cancelled")


def _set_task_status(task_id: int, status: str):
    try:
        task = update_task_status(task_id, status)
    except Exception as exc:
        print(f"Task update error: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not update the task.",
        ) from exc

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    log_event("task_updated", task_id=task_id, status=status)
    return {"success": True, "task": task}


# --------------------------------------------------
# Web phone: simulated carrier calls with remembered history
# --------------------------------------------------
# There is no telephony provider yet, so calls happen on a browser page
# connected to FastAPI. Each caller is identified by USDOT, phone,
# company, or person name; conversations are stored with that key and
# resumed on the next call. The agent is strictly read-only: it can
# discuss loads but never changes database records.

CALL_TABLES_DDL = (
    """
    CREATE TABLE IF NOT EXISTS public.call_sessions (
        id TEXT PRIMARY KEY,
        caller_key TEXT NOT NULL,
        display_name TEXT,
        company_name TEXT,
        usdot_number TEXT,
        phone TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMPTZ,
        summary TEXT,
        resumed_from TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS call_sessions_caller_key_idx
    ON public.call_sessions (caller_key, started_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS public.call_messages (
        id BIGSERIAL PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES public.call_sessions(id)
            ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        flagged BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS call_messages_session_idx
    ON public.call_messages (session_id, id)
    """,
)

CALL_SESSION_COLUMNS = (
    "id, caller_key, display_name, company_name, usdot_number, phone, "
    "status, started_at, last_activity_at, ended_at, summary, resumed_from"
)

CALL_HISTORY_LIMIT = 20
CALL_CONTEXT_MESSAGES = 12

CALL_CHANGE_PATTERNS = (
    re.compile(
        r"\b(change|update|edit|modify|set|correct|fix)\b.{0,40}"
        r"\b(rate|price|pay|phone|number|email|address|mc|dot|usdot|"
        r"status|record|database|profile|details|company|name|"
        r"appointment|pickup|delivery|load|equipment)\b"
    ),
    re.compile(r"\b(delete|remove|erase|drop|wipe|purge)\b"),
    re.compile(
        r"\b(cancel|reschedule)\b.{0,30}"
        r"\b(load|booking|appointment|pickup|delivery|record|callback)\b"
    ),
    re.compile(
        r"\b(add|put|insert|save)\b.{0,30}\b(me|us|this)\b.{0,30}"
        r"\b(database|system|record)\b"
    ),
    re.compile(
        r"\b(mark|set)\b.{0,30}"
        r"\b(interested|not interested|do not call|dnc)\b"
    ),
)

CALL_REFUSAL_REPLY = (
    "I can't change anything in our records on this call — I'm read-only. "
    "I've flagged that request for the dispatcher, who can update it. "
    "Meanwhile, I can talk about your lane, equipment, or availability."
)


def ensure_call_tables():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            for statement in CALL_TABLES_DDL:
                cur.execute(statement)


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _normalize_name(value) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower())
    return " ".join(text.split())


def build_caller_key(usdot="", phone="", company="", person="") -> str:
    """Pick one stable identity for a caller, in priority order."""
    usdot = _digits(usdot)
    phone = _digits(phone)
    if phone:
        phone = phone[-10:]

    if usdot:
        return f"usdot:{usdot}"
    if phone:
        return f"phone:{phone}"
    if _normalize_name(company):
        return f"name:{_normalize_name(company)}"
    if _normalize_name(person):
        return f"person:{_normalize_name(person)}"
    return ""


def call_change_intent(message: str) -> str | None:
    """Return a matched fragment when the caller asks to modify records."""
    lowered = (message or "").lower()
    for pattern in CALL_CHANGE_PATTERNS:
        match = pattern.search(lowered)
        if match:
            return match.group(0)[:80]
    return None


def derive_call_summary(messages) -> str:
    """Deterministic short summary from the caller's own words."""
    parts = []
    for item in messages:
        if item.get("role") != "user":
            continue
        text = " ".join(str(item.get("content", "")).split())
        if text:
            parts.append(text[:160])
    summary = " | ".join(parts[-4:])
    return summary[:500]


def _iso(value):
    return value.isoformat() if value else None


def _call_session_to_dict(row) -> dict:
    return {
        "id": row[0],
        "caller_key": row[1],
        "display_name": row[2],
        "company_name": row[3],
        "usdot_number": row[4],
        "phone": row[5],
        "status": row[6],
        "started_at": _iso(row[7]),
        "last_activity_at": _iso(row[8]),
        "ended_at": _iso(row[9]),
        "summary": row[10],
        "resumed_from": row[11],
    }


def _load_call_transcript(cur, session_id, limit=CALL_HISTORY_LIMIT):
    cur.execute(
        """
        SELECT role, content, flagged, created_at
        FROM public.call_messages
        WHERE session_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (session_id, int(limit)),
    )
    rows = cur.fetchall()
    rows = list(reversed(rows))
    return [
        {
            "role": row[0],
            "content": row[1],
            "flagged": bool(row[2]),
            "created_at": _iso(row[3]),
        }
        for row in rows
    ]


def _find_previous_call(cur, usdot, phone, company, person):
    clauses = []
    params = []

    if usdot:
        clauses.append("usdot_number = %s")
        params.append(usdot)
    if phone:
        clauses.append("right(regexp_replace(phone, '\\D', '', 'g'), 10) = %s")
        params.append(phone)
    if _normalize_name(company):
        clauses.append(
            "lower(regexp_replace(company_name, '[^a-zA-Z0-9]+', ' ', 'g')) "
            "= %s"
        )
        params.append(_normalize_name(company))
    if _normalize_name(person):
        clauses.append(
            "lower(regexp_replace(display_name, '[^a-zA-Z0-9]+', ' ', 'g')) "
            "= %s"
        )
        params.append(_normalize_name(person))

    if not clauses:
        return None

    cur.execute(
        f"""
        SELECT {CALL_SESSION_COLUMNS}
        FROM public.call_sessions
        WHERE {' OR '.join(clauses)}
        ORDER BY started_at DESC
        LIMIT 1
        """,
        tuple(params),
    )
    row = cur.fetchone()
    return _call_session_to_dict(row) if row else None


def _greeting_for(session, resumed, transcript) -> str:
    name = (session.get("display_name") or "").strip()
    name_part = f" {name}" if name else ""

    if resumed:
        summary = (session.get("summary") or "").strip()
        if not summary:
            summary = derive_call_summary(transcript)
        if summary:
            return (
                f"Welcome back{name_part}. Last time we were talking about: "
                f"{summary}. Want to pick up there, or is something new?"
            )
        return (
            f"Welcome back{name_part}. I have your earlier call on file. "
            "What would you like to discuss today?"
        )

    company = (session.get("company_name") or "").strip()
    company_part = f" with {company}" if company else ""
    return (
        f"Thanks for calling{company_part}. This is Adam, the dispatch "
        "assistant. I can talk about loads, lanes, equipment, and "
        "availability. I can't change any records, but I can note your "
        "details for the dispatcher. What lane are you running?"
    )


def _shorten_call_reply(reply: str, max_sentences: int = 2) -> str:
    """Force phone-style brevity: at most `max_sentences` sentences.

    The model tends to ramble; on a phone call the driver stops listening
    after the second sentence, so we keep the first two and drop the rest.
    """
    text = " ".join(str(reply or "").split())
    if not text:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) <= max_sentences:
        return text
    kept = " ".join(sentences[:max_sentences]).strip()
    if kept[-1] not in ".!?":
        kept += "."
    return kept


_CALL_EQUIPMENT_TERMS = (
    "reefer", "dry van", "flatbed", "step deck", "hotshot",
    "box truck", "sprinter van", "tanker", "gooseneck", "conestoga",
    "lowboy", "power only", "straight truck", "car hauler",
)


def derive_call_profile(messages) -> dict:
    """Pull a structured carrier profile out of the driver's own words.

    Returns {"equipment": [...], "trailer_lengths": [...], "lanes": [...]}.
    Everything comes from regex on what the driver actually said — nothing
    is invented.
    """
    driver_text = " ".join(
        str(item.get("content", ""))
        for item in messages
        if item.get("role") == "user"
    ).lower()

    equipment = sorted({term for term in _CALL_EQUIPMENT_TERMS
                        if term in driver_text})
    lengths = sorted({m.group(1) + "ft" for m in
                      re.finditer(
                          r"\b(48|53|26|40|45)\s?(?:ft|feet|foot)\b",
                          driver_text,
                      )})
    lanes = []
    # Words drivers say after a destination ("to Texas as well",
    # "to Chicago next week") are not place names.
    _LANE_STOP = {
        "as", "well", "and", "also", "then", "next", "this", "that",
        "week", "today", "tomorrow", "for", "with", "from", "to",
        "sometimes", "there", "here",
    }

    def _place(segment):
        words = []
        for token in segment.split():
            if token in _LANE_STOP or len(words) == 3:
                break
            words.append(token)
        return " ".join(words)

    for m in re.finditer(
        r"\bfrom\s+([a-z \'.]{2,28}?)\s+to\s+([a-z \'.]{2,28}?)"
        r"(?=\s*[.,;!?]|$)",
        driver_text,
    ):
        origin, dest = _place(m.group(1)), _place(m.group(2))
        if not origin or not dest:
            continue
        lane = f"{origin} to {dest}"
        if lane not in lanes:
            lanes.append(lane[:60])

    return {
        "equipment": equipment,
        "trailer_lengths": lengths,
        "lanes": lanes,
    }


def _call_system_prompt(session) -> str:
    name = session.get("display_name") or "unknown caller"
    company = session.get("company_name") or "unknown company"
    usdot = session.get("usdot_number") or "unknown"
    summary = session.get("summary") or "no previous call"

    return (
        "You are Adam, a truck-dispatch assistant speaking on a phone call "
        "with a carrier or driver.\n"
        "Answer in AT MOST TWO SHORT sentences (under 30 words total). "
        "One question at a time is usually enough.\n"
        "STRICT RULES:\n"
        "- You CANNOT change, create, cancel, or delete anything in the "
        "database. If asked, say you are read-only and the dispatcher will "
        "handle it. Never claim a record was changed.\n"
        "- Never invent load availability, rates, broker names, or booking "
        "confirmations. If unknown, say a dispatcher will confirm.\n"
        "- Never take payment or card details.\n"
        "- Stay on trucking/dispatch topics.\n"
        f"Caller: {name}; company: {company}; USDOT: {usdot}.\n"
        f"Previous conversation summary: {summary}."
    )


def _call_agent_reply(session, transcript, message) -> str:
    """Ask the model for one phone-style reply. No tools are exposed."""
    messages = [{"role": "system", "content": _call_system_prompt(session)}]

    for item in transcript[-CALL_CONTEXT_MESSAGES:]:
        if item.get("role") in ("user", "assistant"):
            messages.append(
                {"role": item["role"], "content": item["content"]}
            )

    messages.append({"role": "user", "content": message})

    try:
        # gpt-oss is a reasoning model: a tiny token budget is consumed by
        # the hidden reasoning channel and the visible content comes back
        # empty. A moderate budget plus the 2-sentence cutter keeps both
        # quality and brevity.
        completion = groq_client.chat.completions.create(
            model="openai/gpt-oss-20b",
            temperature=0.4,
            max_completion_tokens=220,
            reasoning_effort="low",
            messages=messages,
        )
        reply = (completion.choices[0].message.content or "").strip()
    except Exception as error:
        log_event(
            "call_agent_error",
            error=f"{type(error).__name__}: {error}",
        )
        reply = ""

    if not reply:
        log_event("call_agent_empty_reply", reason="no content from model")
        reply = (
            "I'm having trouble hearing that clearly. I can note your "
            "details for the dispatcher, but I can't change any records. "
            "Could you repeat your lane and equipment?"
        )

    return _shorten_call_reply(reply)[:500]


def begin_call(usdot="", phone="", company="", person="") -> dict:
    """Start or resume a call, returning the transcript and greeting."""
    usdot = _digits(usdot)[:30]
    phone = _digits(phone)[-10:]
    company = (company or "").strip()[:200]
    person = (person or "").strip()[:120]

    if not any((usdot, phone, company, person)):
        raise HTTPException(
            status_code=400,
            detail=(
                "Identify the caller with a USDOT number, phone, company, "
                "or name."
            ),
        )

    resumed = False
    transcript = []
    resumed_from = None

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            previous = _find_previous_call(
                cur, usdot, phone, company, person
            )

            if previous and previous["status"] == "open":
                session = previous
                resumed = True
                transcript = _load_call_transcript(cur, session["id"])
            else:
                caller_key = (
                    previous["caller_key"]
                    if previous
                    else build_caller_key(usdot, phone, company, person)
                )

                session_id = "call_" + uuid.uuid4().hex
                resumed = previous is not None
                resumed_from = previous["id"] if previous else None

                if previous:
                    transcript = _load_call_transcript(
                        cur, previous["id"]
                    )
                    session = {
                        **previous,
                        "id": session_id,
                        "status": "open",
                        "summary": previous.get("summary"),
                    }
                else:
                    session = {
                        "id": session_id,
                        "caller_key": caller_key,
                        "display_name": person or None,
                        "company_name": company or None,
                        "usdot_number": usdot or None,
                        "phone": phone or None,
                        "status": "open",
                        "started_at": None,
                        "last_activity_at": None,
                        "ended_at": None,
                        "summary": None,
                        "resumed_from": None,
                    }

                cur.execute(
                    f"""
                    INSERT INTO public.call_sessions (
                        id, caller_key, display_name, company_name,
                        usdot_number, phone, status, summary, resumed_from
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, %s)
                    RETURNING {CALL_SESSION_COLUMNS}
                    """,
                    (
                        session_id,
                        caller_key,
                        person or (previous.get("display_name") if previous else None),
                        company or (previous.get("company_name") if previous else None),
                        usdot or (previous.get("usdot_number") if previous else None),
                        phone or (previous.get("phone") if previous else None),
                        previous.get("summary") if previous else None,
                        resumed_from,
                    ),
                )
                session = _call_session_to_dict(cur.fetchone())

            greeting = _greeting_for(session, resumed, transcript)

            cur.execute(
                """
                INSERT INTO public.call_messages (session_id, role, content)
                VALUES (%s, 'assistant', %s)
                """,
                (session["id"], greeting),
            )

    log_event(
        "call_started",
        session_id=session["id"],
        caller_key=session["caller_key"],
        resumed=resumed,
    )

    return {
        "session_id": session["id"],
        "caller_key": session["caller_key"],
        "resumed": resumed,
        "resumed_from": resumed_from,
        "company_name": session.get("company_name"),
        "display_name": session.get("display_name"),
        "usdot_number": session.get("usdot_number"),
        "phone": session.get("phone"),
        "summary": session.get("summary"),
        "greeting": greeting,
        "transcript": transcript,
    }


def get_call_session(session_id: str) -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {CALL_SESSION_COLUMNS}
                FROM public.call_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="Call session not found."
                )

            session = _call_session_to_dict(row)
            session["transcript"] = _load_call_transcript(
                cur, session_id, limit=100
            )

    return session


def handle_call_message(session_id: str, message: str) -> dict:
    """Store one caller turn and produce the agent's reply.

    The caller can never modify lead/review data here: there are no
    write paths to those tables, and change requests are refused before
    the model is called.
    """
    message = " ".join((message or "").split())[:1000]
    if not message:
        raise HTTPException(status_code=400, detail="Message is required.")

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {CALL_SESSION_COLUMNS}
                FROM public.call_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="Call session not found."
                )
            session = _call_session_to_dict(row)

            if session["status"] != "open":
                raise HTTPException(
                    status_code=409, detail="This call has already ended."
                )

            transcript = _load_call_transcript(
                cur, session_id, limit=CALL_HISTORY_LIMIT
            )

    intent = call_change_intent(message)

    if intent:
        reply = CALL_REFUSAL_REPLY
        log_event(
            "call_data_change_refused",
            session_id=session_id,
            caller_key=session["caller_key"],
            intent=intent,
        )
    else:
        reply = _call_agent_reply(session, transcript, message)

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.call_messages (
                    session_id, role, content, flagged
                )
                VALUES (%s, 'user', %s, %s)
                """,
                (session_id, message, bool(intent)),
            )
            cur.execute(
                """
                INSERT INTO public.call_messages (session_id, role, content)
                VALUES (%s, 'assistant', %s)
                """,
                (session_id, reply),
            )
            cur.execute(
                """
                UPDATE public.call_sessions
                SET last_activity_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (session_id,),
            )

    return {
        "session_id": session_id,
        "reply": reply,
        "refused": bool(intent),
        "flagged": bool(intent),
        "intent": intent,
    }


def end_call(session_id: str) -> dict:
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {CALL_SESSION_COLUMNS}
                FROM public.call_sessions
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=404, detail="Call session not found."
                )

            session = _call_session_to_dict(row)
            transcript = _load_call_transcript(cur, session_id, limit=100)
            summary = derive_call_summary(transcript)
            profile = derive_call_profile(transcript)

            cur.execute(
                """
                UPDATE public.call_sessions
                SET
                    status = 'ended',
                    ended_at = CURRENT_TIMESTAMP,
                    last_activity_at = CURRENT_TIMESTAMP,
                    summary = %s
                WHERE id = %s
                RETURNING 
                """
                + CALL_SESSION_COLUMNS,
                (summary, session_id),
            )
            session = _call_session_to_dict(cur.fetchone())

    log_event(
        "call_ended",
        session_id=session_id,
        caller_key=session["caller_key"],
        messages=len(transcript),
        equipment=profile["equipment"],
        lanes=profile["lanes"],
    )

    return {
        "success": True,
        "session": session,
        "summary": summary,
        "profile": profile,
    }


def list_call_sessions(limit=20) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {CALL_SESSION_COLUMNS}
                FROM public.call_sessions
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

    return [_call_session_to_dict(row) for row in rows]


class CallStartRequest(BaseModel):
    usdot_number: str | None = Field(default=None, max_length=30)
    phone: str | None = Field(default=None, max_length=30)
    company_name: str | None = Field(default=None, max_length=200)
    caller_name: str | None = Field(default=None, max_length=120)


class CallMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)


@app.get("/call")
def call_page():
    return FileResponse(PROJECT_ROOT / "static" / "call.html")


@app.post("/call-sessions/outbound", dependencies=[Depends(require_admin)])
def place_outbound_call(request: CallStartRequest):
    """Operator-initiated outbound call to a carrier.

    Creates (or resumes) the call session and returns an auto-connect
    URL for the /call page so the receiver console opens by itself.
    """
    result = begin_call(
        usdot=request.usdot_number or "",
        phone=request.phone or "",
        company=request.company_name or "",
        person=request.caller_name or "",
    )
    auto_url = "/call?session_id=" + result["session_id"] + "&auto=1"
    log_event(
        "outbound_call_requested",
        session_id=result["session_id"],
        caller_key=result["caller_key"],
        resumed=result["resumed"],
    )
    return {
        **result,
        "auto_connect_url": auto_url,
        "message": "Call session ready. Open " + auto_url + " to connect.",
    }


@app.post("/call-sessions", dependencies=[Depends(require_admin)])
def start_call(request: CallStartRequest):
    return begin_call(
        usdot=request.usdot_number or "",
        phone=request.phone or "",
        company=request.company_name or "",
        person=request.caller_name or "",
    )


@app.post(
    "/call-sessions/{session_id}/messages",
    dependencies=[Depends(require_admin)],
)
def post_call_message(session_id: str, request: CallMessageRequest):
    return handle_call_message(session_id, request.message)


@app.get(
    "/call-sessions/{session_id}",
    dependencies=[Depends(require_admin)],
)
def get_call(session_id: str):
    return get_call_session(session_id)


@app.post(
    "/call-sessions/{session_id}/end",
    dependencies=[Depends(require_admin)],
)
def post_end_call(session_id: str):
    return end_call(session_id)


@app.get("/call-sessions", dependencies=[Depends(require_admin)])
def get_call_sessions(limit: int = 20):
    return {"sessions": list_call_sessions(limit=limit)}


@app.on_event("startup")
def _ensure_call_tables_on_startup():
    try:
        ensure_call_tables()
    except Exception as error:
        log_event(
            "call_tables_schema_error",
            error=f"{type(error).__name__}: {error}",
        )


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
    "add_reminder": (
        "Store a reminder for the operator. args: {title, due_at "
        "(ISO-8601 or 'in 2 hours')}"
    ),
    "list_tasks": (
        "List scheduled tasks and reminders. args: {due_only: bool, "
        "include_done: bool}"
    ),
    "complete_task": (
        "Mark a scheduled task done. args: {task_id (number)}"
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
    "place_outbound_call": (
        "Open the web call console for a carrier. args: {usdot_number, "
        "company_name, phone, caller_name}. This creates a call session "
        "and returns an auto-connect URL for the /call page."
    ),
}

ASSISTANT_REFUSAL_KEYWORDS = (
    "delete", "drop ", "truncate", "wipe", "erase", "remove all",
    "reset all", "purge", "rm -rf", "buy", "purchase", "pay ", "charge",
    "subscribe", "send email", "send sms", "dial", "real call",
    "place a call", "make the call", "go live", "phone the carrier",
)

# Phrasing variants that are still "make a real phone call". Kept as
# patterns so "place a real call" is refused while ordinary questions
# such as "what is the outbound calling mode" are still allowed.
# Note: "call carrier" / "call the carrier" is now ALLOWED for the web
# call console (/call page) — only real telephony dialing is refused.
ASSISTANT_REFUSAL_PATTERNS = (
    re.compile(r"\b(place|make|start|begin|initiate)\b.{0,25}\bcall\b"),
    re.compile(r"\b(dial|phone)\b.{0,25}\b(carrier|lead|number|them|him|her)\b"),
    # Allow "call carrier" for web console; only block explicit "real call" / "dial" / "phone"
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
    + "\n\nExamples:\n"
    + '- "call carrier 1234567" -> {"action": "place_outbound_call", "args": {"usdot_number": "1234567"}}\n'
    + '- "call carrier MLA Logistics" -> {"action": "place_outbound_call", "args": {"company_name": "MLA Logistics"}}\n'
    + '- "call carrier 555-123-4567" -> {"action": "place_outbound_call", "args": {"phone": "555-123-4567"}}\n'
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
    if text.lower().startswith("at "):
        text = text[3:].strip()

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
            "my tasks",
            "what do i need to do",
            "what should i do",
            "what's due",
            "what is due",
            "scheduled tasks",
            "reminders",
            "to-do list",
            "todo list",
        )
    ):
        return {"action": "list_tasks", "args": {}}

    task_done = re.match(
        r"^(?:mark\s+)?task\s+#?(\d+)\s+(?:as\s+)?"
        r"(?:done|complete|completed)$",
        lowered,
    )
    if task_done:
        return {
            "action": "complete_task",
            "args": {"task_id": int(task_done.group(1))},
        }

    reminder = re.match(
        r"^remind me(?:\s+to)?\s+(.+?)"
        r"(?:\s+(in\s+\d+\s+(?:minutes?|hours?|days?)|at\s+\S+))?$",
        text,
        re.IGNORECASE,
    )
    if reminder and reminder.group(1).strip():
        return {
            "action": "add_reminder",
            "args": {
                "title": reminder.group(1).strip()[:200],
                "due_at": (reminder.group(2) or "in 1 hour").strip(),
            },
        }

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

    # call carrier <usdot|company|phone> -- opens web call console
    call_match = re.match(
        r"^(?:call|phone|dial)\s+(?:carrier\s+)?(.+)$", text, re.IGNORECASE
    )
    if call_match:
        target = call_match.group(1).strip()
        # Try to parse as USDOT, phone, or company name
        args = {}
        if re.fullmatch(r"\d{1,8}", target):
            args["usdot_number"] = target
        elif re.search(r"\d{3}.*\d{3}.*\d{4}", target):
            args["phone"] = target
        else:
            args["company_name"] = target
        return {"action": "place_outbound_call", "args": args}

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
                        "\nFor add_reminder, put the reminder text in title "
                        "and the time in due_at ('in 2 hours' is fine too)."
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
            "are due, keep reminders (\"remind me to ... in 2 hours\"), "
            "list what is due, mark tasks done, show pipeline job status "
            "and leads, prepare the Excel export (e.g. \"export "
            "interested carriers to excel\"), and open the web call console "
            "for a carrier (e.g. \"call carrier 1234567\" or "
            "\"call carrier MLA Logistics\"). I never delete data, never "
            "make real phone calls, and never spend money.",
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

    if action == "add_reminder":
        title = str(args.get("title", "")).strip()[:200]
        if not title:
            raise HTTPException(
                status_code=400,
                detail="A reminder needs a title.",
            )

        due_at = _parse_follow_up_at(args.get("due_at", "in 1 hour"))
        task = create_task(
            "reminder",
            title,
            due_at,
            details=args.get("details"),
            source="assistant",
        )
        log_event("task_created", task_id=task["id"], task_type="reminder")
        return (
            f"Reminder #{task['id']} saved for "
            f"{due_at.isoformat(timespec='minutes')}: {title}. "
            "It will show as due at that time; no call is placed.",
            task,
        )

    if action == "list_tasks":
        include_done = args.get("include_done", False)
        if isinstance(include_done, str):
            include_done = include_done.strip().lower() in ("true", "1", "yes")

        tasks = list_tasks(include_done=bool(include_done))
        due_tasks = [task for task in tasks if task["status"] == "due"]

        if not tasks:
            return (
                "Nothing scheduled. Say e.g. "
                "\"remind me to check carrier prices in 2 hours\".",
                {"tasks": []},
            )

        lines = [
            f"#{task['id']} {task['title']} — due {str(task['due_at'])[:16]} "
            f"[{task['status']}]"
            for task in tasks[:10]
        ]
        return (
            f"{len(due_tasks)} task(s) due now, {len(tasks)} open in total:\n"
            + "\n".join(lines),
            {"tasks": tasks, "due_count": len(due_tasks)},
        )

    if action == "complete_task":
        try:
            task_id = int(args.get("task_id"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="A numeric task id is required.",
            )

        task = update_task_status(task_id, "done")
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found.")

        log_event("task_updated", task_id=task_id, status="done")
        return (
            f"Task #{task_id} marked done: {task['title']}.",
            task,
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

    if action == "place_outbound_call":
        usdot = str(args.get("usdot_number", "")).strip()
        company = str(args.get("company_name", "")).strip()
        phone = str(args.get("phone", "")).strip()
        caller = str(args.get("caller_name", "")).strip()

        if not any((usdot, company, phone, caller)):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Provide at least one of: usdot_number, company_name, "
                    "phone, or caller_name to identify the carrier."
                ),
            )

        result = begin_call(
            usdot=usdot, company=company, phone=phone, person=caller
        )
        auto_url = f"/call?session_id={result['session_id']}&auto=1"
        who = company or usdot or phone or caller or "the carrier"
        return (
            f"Opening call console for {who}. "
            f"The call page will auto-connect: {auto_url}",
            {
                "session_id": result["session_id"],
                "caller_key": result["caller_key"],
                "auto_connect_url": auto_url,
                "resumed": result["resumed"],
            },
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
                    "Speak naturally but VERY brief: AT MOST two short "
                    "sentences (under 30 words) per reply. Ask one question "
                    "at a time. Use conversation history to understand "
                    "follow-up questions. Never invent carrier details, "
                    "truck availability, or load information. Never claim a "
                    "call or booking actually happened.\n\n"

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

                # A requested callback becomes a persistent scheduled task
                # so it is not lost on restart and shows up when due.
                if (
                    request.review_status == "callback_requested"
                    and request.follow_up_at is not None
                ):
                    _insert_task(
                        cur,
                        task_type="callback",
                        title=f"Call back carrier {request.usdot_number}",
                        due_at=request.follow_up_at,
                        usdot_number=request.usdot_number,
                        details=request.notes,
                        source="carrier_review",
                        payload={"review_id": row[0]},
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
