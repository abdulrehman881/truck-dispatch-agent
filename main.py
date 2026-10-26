import os
import time
import uuid
import hmac
import logging
from contextvars import ContextVar
from pathlib import Path

from datetime import datetime
from typing import Literal

from io import BytesIO
from openpyxl import Workbook

import psycopg
from psycopg.types.json import Jsonb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi import Depends
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
def export_excel():
    """Export carrier leads and their latest review outcomes to Excel."""

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
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
                    ORDER BY
                        CASE c.priority
                            WHEN 'high' THEN 1
                            WHEN 'medium' THEN 2
                            ELSE 3
                        END,
                        c.lead_score DESC NULLS LAST,
                        c.company_name;
                """)

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

        return StreamingResponse(
            output,
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            headers={
                "Content-Disposition": 'attachment; filename="carrier_leads.xlsx"'
            },
        )

    except Exception as exc:
        print(f"ERROR in /export-excel: {type(exc).__name__}: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not export carrier data. Check server terminal.",
        ) from exc
