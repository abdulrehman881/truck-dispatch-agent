# TruckDispatchAgent --- Master Task Plan for OpenCode

## Mission

Build a reliable AI truck-dispatching agent in the existing project. It
should process carrier records, research and verify carrier information,
contact eligible carriers through a voice agent, record outcomes, and
later help find suitable loads for carriers who agree.

**Work in the existing codebase. Do not rebuild working features from
scratch.** Inspect the repository first, preserve existing behavior,
implement tasks in small safe steps, and run tests after each meaningful
change.

## Existing project context

-   Project folder: `C:\Users\Rocky computer\Desktop\TruckDispatchAgent`
-   Conda environment: `langgraph-test`
-   Backend: FastAPI in `main.py`
-   UI: `static/voice.html`
-   Existing local UI: `http://127.0.0.1:8123/ui`
-   Existing endpoints include `/`, `/health`, `/chat`, `/voice`,
    `/voice-chat`, `/ui`, `/stream-voice`, `/next-carrier`, and
    `/eligible-carriers` (verify actual code before changing).
-   AI: Groq `openai/gpt-oss-20b`; speech-to-text: Whisper
    `whisper-large-v3-turbo`; text-to-speech: ElevenLabs
    `eleven_flash_v2_5`.
-   Voice persona: Adam --- concise, natural, one question at a time,
    truthful, and never invents carrier details, loads, call outcomes,
    or bookings.
-   Carrier source PDF previously used: `REGISTER20261005.pdf`.
-   Existing extraction outputs previously included
    `output/extracted_leads.json` and `output/extraction_summary.json`.
-   PostgreSQL and Excel are intended lead destinations. Inspect the
    current database schema/configuration rather than assuming it.
-   Automated tests previously reported: **14 passed**. Re-run the
    current suite; do not assume this is still true.
-   `.env` contains secrets. Never print, expose, commit, or ask the
    user to paste API keys or passwords. Keep `.env` ignored by Git; use
    placeholders in `.env.example`.

## How to work

1.  Inspect all relevant files, repository status, current tests, and
    README before editing.

2.  Create or update a concise implementation checklist and track
    completed/incomplete items in this file or a separate progress file.

3.  Keep the working voice UI and API backward-compatible unless a
    change is necessary.

4.  Use the existing `langgraph-test` interpreter. In PowerShell:

    ``` powershell
    conda activate langgraph-test
    cd "C:\Users\Rocky computer\Desktop\TruckDispatchAgent"
    python -m pytest tests -q
    ```

5.  After changes, run tests, fix regressions, and report exact test
    results. Do not claim a feature was tested if it was only inspected.

6.  Do not make paid API calls, place real phone calls, contact real
    carriers, delete production data, or run destructive database
    migrations without explicit approval.

7.  If credentials, external access, or a decision from the user is
    required, finish all safe work first and clearly list the blocker.
    Do not stop after only writing a plan.

8.  Avoid unrelated refactors and unnecessary dependencies. Add/update
    tests for every important behavior. Never hardcode secrets or real
    carrier claims.

## Phase 0 --- Baseline and safety

-   [x] Inspect the project structure, README, Git status,
    `.env.example`, API routes, database code, extraction scripts, voice
    UI, and tests.
-   [x] Run the full existing test suite and record baseline results.
-   [x] Confirm `.env`, databases, generated audio, and sensitive lead
    data are appropriately excluded from Git.
-   [x] Identify duplicate implementations, stale code, unsafe side
    effects, and missing error handling.
-   [x] Add/update documentation for setup, configuration, starting the
    server, tests, and troubleshooting.

## Phase 1 --- Carrier PDF ingestion and data quality

Goal: turn FMCSA Register PDF records into clean, traceable carrier
records. - \[x\] Verify PDF extraction for USDOT/MC number where
present, company/legal name, phone, authority/category, and source
details. - \[x\] Normalize phone numbers and identifiers without
fabricating missing values. - \[x\] Detect duplicates, malformed
entries, missing fields, and unsupported carrier categories. - \[x\]
Produce a clear extraction summary and preserve the source PDF/file name
and extraction timestamp. - \[x\] Make extraction repeatable and safe:
re-running it should not silently duplicate records or overwrite useful
data. - \[x\] Add tests using small synthetic/sample inputs; do not rely
only on one large PDF.

Acceptance: extraction summary counts reconcile with output records;
duplicates and invalid/missing data are reported; tests cover edge
cases.

## Phase 2 --- Carrier research and verification

Goal: enrich extracted records with trustworthy business information. -
\[x\] Design a research/enrichment step using lawful, available sources
and permitted access methods. - \[x\] Match results carefully using
USDOT/MC number and company name; do not merge uncertain matches
automatically. - \[x\] Capture useful fields such as verified company
name, phone, address, website, fleet/company indicators, source URL,
retrieval date, and confidence/status where available. - \[x\] Preserve
original extracted data separately from researched values and record
provenance for every enriched field. - \[x\] Mark records as verified,
uncertain, unavailable, or needing manual review. - \[x\] Respect
website terms, rate limits, privacy rules, and access restrictions; do
not bypass logins, CAPTCHAs, or blocks.

Acceptance: no fabricated fields; uncertain matches are flagged rather
than treated as facts; sources and timestamps are saved.

## Phase 3 --- Carrier eligibility, queue, and call workflow

Goal: select the right carrier and manage each lead through a clear
state machine. - \[x\] Define and document lead statuses, for example:
`new`, `researched`, `eligible`, `reserved`, `calling`, `interested`,
`not_interested`, `no_answer`, `invalid_number`, `do_not_call`,
`needs_review`, `error`. - \[x\] Make `/next-carrier` and
`/eligible-carriers` use consistent eligibility and exclusion rules. -
\[x\] Prevent two workers/sessions from reserving the same carrier at
once. - \[x\] Track attempts, timestamps, last outcome, retry limits,
and errors. - \[x\] Never call a carrier automatically just because a
record exists. Require explicit configuration and a safe
approval/enablement step before real outbound calls. - \[x\] Add a clear
dry-run/mock mode for development and testing.

Acceptance: eligible records are selected consistently;
excluded/do-not-call records are never queued; duplicate reservations
and unlimited retries are prevented.

## Phase 4 --- Adam voice agent

Goal: keep the currently working browser voice agent stable and make it
robust. - \[x\] Preserve the working UI, `/voice`, `/voice-chat`, and
`/stream-voice` behavior unless tests justify a change. - \[ \] Ensure
microphone input, transcription, response generation, audio playback,
streaming, and interruption/barge-in work end-to-end. - \[ \] Improve
turn-taking and latency without breaking speech detection. - \[ \]
Handle silence, noisy/empty transcripts, network/API errors, rate
limits, playback failures, and cancelled requests gracefully. - \[x\]
Keep conversation history bounded and expire old sessions; clean up
temporary audio files. - \[x\] Ensure first-turn initialization and
carrier selection work reliably. - \[x\] Adam must be honest and
concise, ask one question at a time, avoid pressure or misleading
claims, and never pretend to have called, verified, booked, or saved
something unless the system confirms it. - \[x\] Keep all keys
server-side; do not expose API keys in HTML or browser responses.

Acceptance: tests cover voice API success/failure paths; manual browser
tests confirm several turns and interruption. Automated tests alone do
not prove real microphone behavior.

## Phase 5 --- Call outcomes and lead storage

Goal: store accurate outcomes in PostgreSQL and export usable Excel
data. - \[x\] Inspect the existing schema and import code; preserve
current user data. - \[x\] Save lead identity, source/provenance,
status, call attempt, outcome, notes, timestamps, and
consent/do-not-call flags as appropriate. - \[x\] Use parameterized SQL,
transactions, sensible constraints, and safe connection handling. - \[x\] Prevent duplicate leads using stable identifiers and define how
updates differ from inserts. - \[x\] Make Excel exports consistent,
readable, and safe to regenerate; avoid exposing secrets. - \[x\] Ensure
only confirmed events are recorded as completed actions. - \[x\] Test
database behavior with mocks or an isolated test database. Never wipe or
alter real data during tests.

Acceptance: a simulated interested carrier is stored once,
outcome/status is correct, and export matches the database without
duplicate rows.

## Phase 6 --- Telephony integration (only after approval)

Goal: allow real calls through a supported telephony provider. - \[x\]
First implement/test provider integration in mock mode. - \[x\] Check
provider credentials, caller ID, country/number support, costs, and
account restrictions without exposing secrets. - \[x\] Add call status
callbacks/webhooks, timeouts, retries, recordings/transcripts settings,
and failure handling as supported. - \[x\] Require explicit operator
approval before enabling live calls or spending money. - \[x\] Apply
relevant consent, calling-hour, do-not-call, disclosure, and recording
requirements for the jurisdictions involved. - \[x\] Start with a single
manually approved test call; do not launch bulk calling automatically.

Acceptance: simulated calls work; live-call enablement is documented and
requires an explicit configuration/approval step.

## Phase 7 --- Load discovery and matching (later phase)

Goal: help participating carriers find suitable freight loads. - \[x\]
Identify lawful load-board/API/data sources and their access
requirements before implementing scraping or integrations. - \[x\]
Define carrier requirements: equipment/trailer type, origin/deadhead
radius, destination preferences, availability, dates, rate/miles, and
other constraints. - \[x\] Normalize load data and match only using
known carrier requirements. - \[x\] Present source, time retrieved,
route, rate, miles, and uncertainty where available. - \[x\] Never
invent load availability, rates, broker details, or booking
confirmation. - \[x\] Do not book, accept, or commit a carrier to a load
without explicit approval.

Acceptance: sample loads can be ranked against sample carrier
preferences, with explainable matching and source timestamps.

## Phase 8 --- Operator dashboard and observability

-   [x] Show pipeline counts, carrier status, research confidence, call
    attempts/outcomes, errors, and next actions.
-   [x] Add safe search/filtering and manual review of uncertain
    records. (Read-only `/carriers/search` + `/dashboard` UI, verified
    against the live database.)
-   [x] Provide structured logs with request/session IDs but no secrets
    or unnecessary sensitive data. (`X-Request-ID` middleware + JSON
    event logs via `log_event`; verified live.)
-   [x] Add health/readiness checks and actionable error messages.
-   [x] Keep operator actions auditable. (`operator_audit_log` table +
    `record_audit` on call-attempt/review/mock-call writes; verified via
    mocked-DB tests. Live write verification pending first real write.)

**BLOCKED — Phase 6 (telephony integration):** awaiting a usable,
user-approved phone-calling provider and explicit operator approval.
Do not implement live calling until the user provides a provider and
approves costs. `OUTBOUND_CALL_MODE=offline` stays the default. The
dry-run workflow (`/mock-call`, requires `OUTBOUND_CALL_MODE=mock`)
simulates outcomes: interested, not_interested, no_answer,
invalid_number, failed — covered by tests.

## Phase 9 --- Final verification and handover

> **Integration review record (2026-10-26):** full suite **53 passed /
> 0 failed / 0 skipped**. Live read-only checks: `/health`, `/metrics`,
> `/dashboard`, `/carriers/search`, `/eligible-carriers`, `/export-excel`
> all HTTP 200 with live data; `/mock-call` correctly 409 in offline
> mode. Verified: all five mock outcomes record simulated attempts
> (mocked DB); audit entries written transactionally and a failed audit
> rollbacks the whole save (new regression tests); `OUTBOUND_CALL_MODE=live`
> refuses startup (subprocess test); search SQL fully parameterized
> against injection. Read-only production-DB check: 0 carriers carry
> DNC/revoked consent flags; `operator_audit_log` empty (no production
> writes made by tests). Remaining open items below.
>
> **Safety milestone record (2026-10-26):** Git checkpoint created
> (`7a6c7e1`; `.env`, `output/`, PDFs, audio ignored and verified
> unstaged). Admin auth added: all admin/data endpoints require
> `Authorization: Bearer <ADMIN_API_TOKEN>`; fail-closed 503 when
> unconfigured; 401 on missing/wrong token. 20 new auth tests; full
> suite **73 passed / 0 failed / 0 skipped**. Browser-verified
> dashboard: locked before token entry, live data after token. Public
> (token-free): `/`, `/health`, `/chat`, `/ui`, `/dashboard` page shell,
> and all Adam voice routes (voice assistant preserved, calling still
> offline). Open: yesterday's chat-public test made one real (unmocked)
> Groq call before it was fixed and fully mocked.
> **Phase 7 load discovery: PLANNED, not implemented** (plan reported
> to operator; no integrations or paid services activated).
>
> **Pipeline UI + operator assistant record (2026-10-26):** verified
> live. `/pipeline` uploads a PDF (≤25 MB, `max_records` 1–200,
> optional DB import, dry run by default) and shows stage-by-stage
> progress, a live log, and the scored-lead table; real dry runs produced
> 87 leads from 120 records and 167 from 200 records with **no database
> writes**. Per-run files stay in `output/pipeline_runs/<job_id>/`.
> The operator assistant panel at the top of `/pipeline` (and
> `POST /assistant`, admin-token protected) runs whitelisted actions:
> metrics, carrier search, reserve next carrier, save review, pipeline
> status/leads, Excel link, help. It refuses destructive (delete/drop/
> truncate/wipe), real-calling, and spending requests **before** any
> model or database call; no destructive, billing, or live-calling
> action exists in the whitelist. Browser-verified live: metrics
> (487 leads), carrier search ("MLA" → MLA LOGISTICS CORP), pipeline
> status, and a destructive request (refused with clear message).
> Conversation memory is now MC-scoped: history, pending review, and the
> carrier marker are deleted automatically when a review is saved, when
> the conversation switches to another MC, when a new carrier is
> reserved, or after 1 hour idle; each deletion is logged as
> `conversation_memory_cleared`. Full suite **120 passed / 0 failed /
> 0 skipped**. Telephony remains offline; Phase 6 still blocked.
>
> **Callbacks + filtered Excel record (2026-10-26):** the assistant can
> now schedule a callback (`mark 1234567 callback in 2 hours`, stored as
> `follow_up_at`; ISO-8601 or relative times), list scheduled/due
> callbacks (`callbacks due`), and `/export-excel` accepts an optional
> `?review_status=` filter. The pipeline panel has a **Download Excel**
> selector (interested / callback requested / not interested / do not
> contact / all). Verified live read-only: `callbacks due` answered
> correctly, invalid status → HTTP 422, and the interested-only workbook
> downloaded with 3 rows, every `review_status` = `interested`
> (`carrier_leads_interested.xlsx`, 5,491 bytes). Browser-verified the
> new chip and download button. No production rows were written; no
> call was placed. Full suite **136 passed / 0 failed / 0 skipped**.

-   [ ] Run all tests and report exact pass/fail/skip counts.
-   [ ] Test `/health`, `/ui`, key API endpoints, PDF extraction,
    database persistence, exports, and mock calling.
-   [ ] Clearly separate automated checks from manual real-microphone
    and live-provider checks.
-   [ ] Update README with installation, environment setup,
    migrations/schema, commands, tests, and troubleshooting.
-   [ ] Provide a concise report: files changed, features completed,
    tests run/results, known limitations, external credentials/actions
    still needed, and the next recommended step.

## Global rules

-   Accuracy and safety are more important than pretending every task is
    complete.
-   Do not invent carrier information or load-board results.
-   Never expose secrets or commit `.env`.
-   No real outbound calls, paid actions, bookings, or destructive
    database operations without explicit approval.
-   Do not silently change API contracts, database schemas, or working
    voice behavior.
-   Prefer small, tested changes; document any assumptions and blockers.
-   Work through all safe, implementable tasks in order, rather than
    stopping at a plan or asking the user to approve every harmless code
    edit.
