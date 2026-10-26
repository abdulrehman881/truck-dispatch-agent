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
| `GET /export-excel` | Export leads + reviews to Excel |
| `GET /metrics` | Dashboard counts (admin token required) |

All admin/data endpoints (`/metrics`, `/carriers/search`,
`/eligible-carriers`, `/next-carrier`, `/carrier-reviews`,
`/carrier-call-attempts`, `/mock-call`, `/export-excel`) require
`Authorization: Bearer <ADMIN_API_TOKEN>`. They fail closed (HTTP 503)
when the token is not configured. Voice/chat endpoints and `/health`
are public and never need the token.

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
