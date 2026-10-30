# TruckDispatchAgent

AI truck-dispatch voice agent ("Adam") built with FastAPI, PostgreSQL,
Groq, and ElevenLabs. It ingests FMCSA carrier records from the
REGISTER PDF, verifies them against the official FMCSA API, queues
eligible carriers for review, and talks to operators/carriers through a
browser voice UI.

## Stack

- **Backend**: FastAPI (`main.py`), Python 3.13 (conda env `langgraph-test`)
- **Database**: PostgreSQL (`psycopg` 3)
- **AI**: Groq (`openai/gpt-oss-20b`, Whisper `whisper-large-v3-turbo`)
- **TTS**: ElevenLabs (`eleven_flash_v2_5`)
- **Lead pipeline**: PyMuPDF extraction + FMCSA verification (src/ scripts)

## Setup

1. Create/activate the conda environment:

   ```powershell
   conda activate langgraph-test
   ```

2. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in real values
   (Groq key, ElevenLabs key, FMCSA WebKey, DB credentials).
   Never commit the real `.env`.

4. Create the database schema:

   ```powershell
   python src/apply_schema.py
   ```

## Run the server

```powershell
python -m uvicorn main:app --port 8123
```

Open `http://127.0.0.1:8123/ui` in Chrome and allow microphone access.

## Lead pipeline (offline, in order)

```powershell
python src/extract_pdf.py          # Phase 1: PDF -> extracted_leads.json
python src/prepare_leads.py        # Phase 2: filter/group -> candidates + review queue
python src/verify_carriers.py      # Phase 3: FMCSA verification (requires FMCSA_WEB_KEY)
python src/qualify_leads.py        # Phase 4: scoring -> qualified_leads.json
python src/import_to_postgres.py   # Phase 5: upsert into carrier_leads
```

Re-running import is safe: it upserts by `usdot_number` instead of duplicating.

## Tests

```powershell
python -m pytest tests -q
```

- AI providers are mocked; no paid API calls are made by tests.
- Database-touching tests are read-only and skip automatically when the
  database is unreachable.

## API overview

| Endpoint | Purpose |
|---|---|
| `GET /` and `/health` | Status checks |
| `POST /chat` | Text chat with Adam (no voice) |
| `POST /voice` | Text in, Adam's speech MP3 out |
| `POST /voice-chat` | Audio upload -> transcript, reply, MP3 out |
| `POST /stream-voice` | SSE streaming voice reply (main UI path) |
| `GET /next-carrier` | Reserve next eligible carrier for review (15 min) |
| `GET /eligible-carriers` | List eligible candidates (no reservation) |
| `POST /carrier-reviews` | Save a review outcome (interested / callback / DNC...) |
| `POST /carrier-call-attempts` | Save a call outcome |
| `GET /export-excel` | Export leads + reviews to Excel (`?review_status=` optional filter) |
| `GET /metrics` | Dashboard counts (admin token required) |
| `GET /pipeline` | Carrier-pipeline UI (upload PDF → live stage progress) |
| `POST /pipeline/upload` | Start a pipeline run on a PDF (admin token required) |
| `GET /pipeline/status/{id}` | Live progress/logs of a pipeline run (admin) |
| `GET /pipeline/leads/{id}` | Qualified leads of a finished run (admin) |
| `POST /assistant` | Operator chatbot: runs whitelisted actions (admin) |
| `GET /tasks` | Scheduled tasks/reminders (admin) |
| `POST /tasks/reminders` | Store a reminder (`title`, `due_at`) (admin) |
| `POST /tasks/{id}/done` | Mark a task done (admin) |
| `POST /tasks/{id}/cancel` | Cancel a pending task (admin) |
| `GET /call` | Simulated carrier phone call console (public UI) |
| `POST /call-sessions` | Start or resume a call session (admin) |
| `POST /call-sessions/{id}/messages` | Post a driver message & get agent reply (admin) |
| `GET /call-sessions/{id}` | Get call session detail and transcript (admin) |
| `POST /call-sessions/{id}/end` | End a call session & derive summary (admin) |
| `GET /call-sessions` | List recent call sessions (admin) |
| `GET /admin/api-token` | Hand the admin token to the UI on this machine (localhost only; 403 for remote clients, 503 if unconfigured) |

#### Web phone call center (`/call`)

Because there is no outbound telephony provider or live dialing, `GET /call`
provides a simulated call console where you can "answer" carrier calls.

- **Identity & resumption**: Callers are identified by USDOT (`usdot:<digits>`),
  phone (`phone:<last10>`), company (`name:<normalized company>`), or name
  (`person:<normalized name>`). When a caller calls back, their conversation
  history and derived summary are reloaded so the agent welcomes them back
  and resumes where they left off.
- **Strictly read-only agent**: The agent discusses loads, lanes, and equipment
  in a natural phone style (`openai/gpt-oss-20b`, temp 0.4, ≤250 tokens). If the
  driver orders the agent to change, update, delete, or modify any database
  record (rates, phone numbers, lead statuses, etc.), a deterministic regex
  refusal catches it **before** any model call or database write, flags the
  message, and replies with `CALL_REFUSAL_REPLY`.
- **Database tables**: `public.call_sessions` and `public.call_messages`,
  created automatically at startup, with cascades and indexes. Recent
  callers are listed for quick call-backs.
- **Speech**: Optional browser Web Speech API (speech synthesis to hear the
  agent, speech recognition for voice input in Chrome).
- **Auto-open receiver console**: Saying "call carrier 1234567" (or a
  company/phone) to the assistant creates the call session, hands the
  token to the call page via `localStorage`, and opens `/call` in a new
  tab; the page auto-connects that session (`?session_id=...&auto=1`,
  or identity params `usdot/company/phone/person` + `auto=1`). If the
  session already ended, the page starts a fresh call with the same
  identity. The pipeline results table also has a per-lead ** Call**
  button, and `POST /call-sessions/outbound` builds the auto URL direct.


Every follow-up is stored in the `public.scheduled_tasks` table, so
schedules survive server restarts. The table is created automatically
at startup (`CREATE TABLE IF NOT EXISTS`) and existing callback reviews
are backfilled once.

- A saved `callback_requested` review automatically creates a `callback`
  task at its `follow_up_at` time (rescheduling cancels the previous
  active task for that carrier).
- The background worker polls every `TASK_SCHEDULER_POLL_SECONDS`
  (default 30) and flips tasks whose time has come from `pending` to
  `due` (`notified_at` set, `task_due` logged). The pipeline page's
  **Scheduled tasks & reminders** card shows them and lets you mark them
  done/cancelled.
- Chat commands: `remind me to <thing> in 2 hours` (or an ISO time),
  `my tasks` / `what do I need to do`, and `mark task 12 done`.
- **The worker never places a call.** A due callback appears as a
  reminder for the operator; real dialing stays blocked until telephony
  is approved and configured.
- Set `TASK_SCHEDULER_ENABLED=false` to disable the worker. Run one
  server process per database (the worker uses `FOR UPDATE SKIP LOCKED`,
  but keeping a single scheduler is simplest).

#### Operator assistant (chatbot)

The pipeline page has an assistant panel at the top. It converts a plain
instruction into one whitelisted action and executes it: `metrics`,
`search_carriers`, `next_carrier`, `save_review`, `callbacks_due`,
`pipeline_status`, `pipeline_leads`, `export_excel`, and `help`. It
refuses — before any model or database work — requests to delete/drop
data, place a real phone call, or spend money. There are no destructive,
billing, or live-calling actions in the whitelist. `POST /assistant`
requires the admin token and is logged as `assistant_action` (or
`assistant_action_refused`).

Callbacks can be scheduled by chat, e.g.
`mark 1234567 callback in 2 hours` (the follow-up time is stored on the
review). A carrier with a future callback stays out of the eligible
queue until that time passes, then becomes eligible again. `callbacks
due` lists them; nothing is dialed automatically while calling is
offline. The panel's **Download Excel** button exports the selected
review status (default: interested carriers) using the admin token —
e.g. `GET /export-excel?review_status=interested`.

#### Carrier pipeline UI (`/pipeline`)

Upload an FMCSA registration PDF and the agent runs the full lead
pipeline in the background — extract → clean/classify → FMCSA
verification → score/qualify → optional database import (dry run by
default; tick "Import to database" to write). Each stage shows live
status, a scrolling run log, and the final scored-lead table. The job
registry is in-memory only (runs vanish on server restart); per-run
files are saved under `output/pipeline_runs/<job_id>/` and the
production `output/` phase files are never touched. Runs operate on the
uploaded copy in `data/uploads/` — production lead data is never
modified by a UI run unless you explicitly enable the import stage.

All admin/data endpoints (`/metrics`, `/carriers/search`,
`/eligible-carriers`, `/next-carrier`, `/carrier-reviews`,
`/carrier-call-attempts`, `/mock-call`, `/export-excel`,
`/pipeline/upload`, `/pipeline/status/*`, `/pipeline/leads/*`,
`/assistant`, `/tasks`, `/tasks/*`) require
`Authorization: Bearer <ADMIN_API_TOKEN>`. They fail closed (HTTP 503)
when the token is not configured. Voice/chat endpoints, `/pipeline`
page, `/dashboard` page, and `/health` are public and never need the
token (the data behind the dashboard/pipeline pages do).

## Lead lifecycle and outbound calling safety

Lead statuses used across the system:

| Status | Meaning / where set |
|---|---|
| `new` / `researched` | Pipeline input and FMCSA verification records |
| `eligible` | `basic_qualification_passed = TRUE` and a phone exists |
| `reserved` | Temporarily locked by `/next-carrier` for 15 minutes |
| `interested`, `not_interested`, `do_not_contact` | Saved via `/carrier-reviews`, permanently excluded from the queue |
| `callback_requested` | Excluded until `follow_up_at` has passed |
| `no_answer`, `invalid_number`, `error` | Recorded on `/carrier-call-attempts` rows |
| `needs_review` | FMCSA lookup uncertain (`needs_manual_review`) |

Queue rules: `/next-carrier` and `/eligible-carriers` apply identical
exclusions (no phone, DNC/revoked consent, resolved reviews, active
callbacks). Concurrent reservations are prevented by
`FOR UPDATE ... SKIP LOCKED`; a carrier with any recorded call attempt
is not re-queued, so unlimited retries are impossible.

Real outbound calls are not enabled. `OUTBOUND_CALL_MODE` (default
`offline`) accepts `offline`, `mock`, or `manual`. `offline` keeps the
server in review/manual-logging mode; `mock` is reserved for simulated
flow testing; `live` is rejected at startup until a telephony provider
integration (Phase 6) is approved and configured.

## Troubleshooting

- **`ModuleNotFoundError: elevenlabs`**: the ElevenLabs SDK contains a
  file path long enough to trigger the Windows MAX_PATH limit. Enable
  Windows Long Paths or use an environment where it is already
  installed (the `langgraph-test` conda env has `elevenlabs 2.72.x`).
- **Whisper/STT fails**: Chrome only; check the mic permission banner.
- **DB errors on first run**: check `.env` DB_* values and that the
  PostgreSQL service is running; verify with `python test_db_connection.py`.
