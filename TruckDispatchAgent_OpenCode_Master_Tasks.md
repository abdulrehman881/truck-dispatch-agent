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
>
> **Web phone call center record (2026-10-27):** added `static/call.html`
> ("incoming call" operator console at `/call`), plus backend call sessions
> (`public.call_sessions`, `public.call_messages`). Callers are identified
> by USDOT, phone, company, or person name; repeat visitors automatically
> resume past conversations and transcripts. **Strictly read-only agent**:
> discusses loads/lanes (`openai/gpt-oss-20b`, temp 0.4, ≤250 tokens), but
> any driver order to change, update, delete, or modify database records
> is caught by deterministic regex refusal **before** any model or DB work,
> flagged as `flagged=TRUE`, and refused with `CALL_REFUSAL_REPLY`.
> Tested: 24 call-center tests (caller key normalization, change-intent regex,
> refusal bypassing model/DB, session resumption, message storage, end call
> summary, 401/404/409/422 responses). Full suite **188 passed / 1 skipped /
> 0 failed** (70.4 s). Live verified: incoming call started, conversation
> stored, change-request refused and flagged, re-calling with same identity
> successfully resumed past transcript and summary. Carrier lead/review data
> remained untouched.
>
> **Auto-open receiver console record (2026-10-28):** saying "call
> carrier 1234567" (or a company/phone) to the assistant now plans the
> new whitelisted `place_outbound_call` action (Groq prompt examples +
> fallback parser). It creates/resumes the call session and returns an
> `auto_connect_url`; the pipeline chat automatically opens `/call` in a
> new tab (token handed over via `localStorage`) and the page
> auto-connects via `?session_id=...&auto=1` — ended sessions fall back
> to a fresh call with the same identity. Endpoint
> `POST /call-sessions/outbound` builds the same URL directly, and the
> pipeline results table has a per-lead Call button. Only real dialing
> phrasings stay refused ("dial/phone the carrier"); "call carrier X" is
> allowed (web console). Fixed a resume-page bug where a shadowed
> `sessionId` parameter prevented message sends. Verified live:
> assistant planned the action, auto URL returned, page auto-connected
> ("Auto-connected." state, timer running), driver message sent and
> agent replied in-session. 8 new tests; full suite **198 passed /
> 0 failed / 0 skipped** (71.9 s).
>
> **Call page UI restyle record (2026-10-28):** `static/call.html`
> restyled from dark theme to a professional white-and-green look —
> white cards/screen on a light background, green accent buttons and
> "READ-ONLY AGENT" badge, soft green agent bubbles vs light gray driver
> bubbles, green timer pill, amber warning highlight for refused
> data-change requests, hover rows in Recent callers. Styling only; no
> markup ids/behavior changed. Browser-verified a full call cycle on the
> new theme: answer (resumed identity), driver message → read-only
> refusal, end call → "Call ended. Summary stored." Full suite **198
> passed / 0 failed / 0 skipped** (35.2 s).
>
> **Pipeline page UI restyle record (2026-10-28):** `static/pipeline.html`
> restyled from dark theme to the same white-and-green professional look
> as the call console — light background, white cards with soft shadows,
> green accent buttons/chips, green stat numbers, light green user chat
> bubbles and green-crowned assistant labels, light red/amber states for
> refused/error badges and due tasks, hover rows on tables, green ⼁Call
> button per lead. Styling only; no ids/behavior changed. Browser-verified:
> computed styles are light (card `rgb(255,255,255)`, button
> `rgb(22,163,74)`), and the assistant answered "Show metrics" live on the
> new theme (487 leads reported). Full suite **198 passed / 0 failed /
> 0 skipped** (79.5 s).
>
> **Operator console v2 + incoming ring record (2026-10-28):** major UI
> upgrade across both pages, backend untouched. (1) `static/pipeline.html`
> rebuild as a same-tab console: sticky topbar with ☰ hamburger opening a
> slide-in sidebar (Agent chat & voice · Upload carrier PDF · Excel files
> · Reminders & schedules); old cards became `.view` sections switched in
> the same tab; chat is the main-page center with quick chips, exports
> moved to tile buttons on the Excel view, tasks table to the Reminders
> view. New 🎤 mic button uses Web Speech recognition — admin speaks,
> text goes to `/assistant`, replies optionally spoken via
> speechSynthesis (🔊 toggle, mic paused while Adam talks). Fixed a latent
> bug: auto-open used to stringify the `#token` input element into
> localStorage; now it stores the real admin token. (2)
> `static/call.html` incoming-call flow: when the agent auto-opens the
> receiver page, a ringing overlay (pulsing avatar + caller name resolved
> via a read-only session GET) shows **✅ Answer / ❌ Decline**; decline
> starts no conversation and ends a pre-created session as missed; answer
> connects as before with **voice agent default** (speaker + mic auto-on)
> and the chat box as second option. Browser-verified live: real outbound
> session (`call_f69db43e…`), ring visible with caller "Ring Test
> Logistics", decline path clears status, answer path connected (greeting
> displayed + spoken), driver message produced an agent reply, call
> ended. Pipeline page verified: sidebar open/close, four views switch in
> the same tab, assistant answered "Show metrics" live (487 leads). Full
> suite **198 passed / 0 failed / 0 skipped** (61.2 s).
>
> **Voice-duplex + open chat + no-emoji pass (2026-10-28):** three UI
> fixes, backend untouched. (1) Driver can now talk BACK hands-free: the
> phone screen gained a round Talk button (mic SVG, green pulse while
> live) answering the same engine — continuous recognition that
> auto-restarts on no-speech/network/aborted errors, submits on the final
> result, and shows plain instructions when the browser denies the mic
> (automation browser denied it; verified the clear hint appears). The
> operator-side 🎤 mic on /pipeline was rebuilt the same way: wantListening
> flag, onend restart loop, and the mic lifts itself while Adam's reply is
> being spoken so it never hears its own voice. (2) The chatbot moved out
> of its card into open space on the main page
> (`#view-chat .open-chat`, no card/box around the transcript).
> (3) All emojis removed from both pages (titles, buttons, chips, tiles,
> ring avatar is "ADAM"); the hamburger is an inline SVG; the admin token
> input moved to the top of the sidebar with its own section. Verified
> byte-clean: scan shows zero emoji chars in call.html and pipeline.html.
> Live browser checks: token box inside sidebar, cardless chat, SVG mic
> button, no emoji strings, assistant answered "Show metrics" (487
> leads); outbound session (call_f1a6c5d2…, caller "Export Group") rang,
> answered → voice-first defaults on, Talk button present with mic-block
> hint, call ended. Full suite **198 passed / 0 failed / 0 skipped**
> (70.9 s).
>
> **Barge-in interrupt + receiver Hang Up + mojibake cleanup
> (2026-10-29):** user-reported issue "agent keeps talking while I
> interrupt" fixed on both voice surfaces. Speech recognition now keeps
> running while Adam's reply is playing: each final transcript is
> compared with the text Adam is speaking by word-overlap (tokens longer
> than 2 chars, ratio above 0.55 = speaker echo, ignored); any genuinely
> different driver/operator utterance instantly cancels speechSynthesis,
> shows a "You interrupted" marker, and the utterance becomes the next
> message — the mic no longer pauses during replies. The Talk/mic buttons
> double as interrupt buttons (tap while Adam talks = stop + listen).
> Verified by simulating both paths in the live pages: echo ignored,
> driver sentence passed, cancel called exactly once. Receiver page
> gained a prominent side **Hang up the call** red button next to Recent
> callers sharing the verified end-call flow (hidden when no call is
> open). Also wiped the last mojibake byte sequences a plain emoji scan
> could not see (telephone/warning glyphs) from call.html. Server and
> docker container had gone cold after an overnight restart — relaunched
> uvicorn and started langgraph-postgres; health OK. Full suite **198
> passed / 0 failed / 0 skipped** (56.8 s).
>
> **Short replies + driver profile + console usability (2026-10-29,
> commit bb19174):** user said agent replies were too long. Call-agent
> system prompt now demands AT MOST TWO SHORT sentences and the backend
> enforces it with `_shorten_call_reply` (sentence-split cutter, max 2).
> Discovered live that gpt-oss-20b spends a tiny token budget entirely
> in its hidden reasoning channel (110 tokens -> finish=length, empty
> content, silent fallback); fixed with `max_completion_tokens=220` +
> `reasoning_effort="low"` (probed Groq directly: finish=stop, real
> 2-sentence content), plus empty-content logging. New
> `derive_call_profile` regex-extracts equipment/trailer lengths/lanes
> from the driver's own words (stopword-trimmed, assistant words
> excluded); `end_call` returns it as `profile` and logs equipment +
> lanes; call page shows "Driver profile (from his own words)" on end.
> Summary format unchanged. Operator console usability: admin token now
> persists in localStorage (survives browser restart, restored on
> load), `Escape` closes the sidebar, `/` focuses the chat input. 7 new
> tests (`_shorten_call_reply`, `derive_call_profile`, prompt budget,
> end-call profile). Live-verified on a real outbound session: short
> reply, profile {reefer, 53ft, texas to chicago}; pipeline checks:
> token prefilled from previous visit, Escape closes sidebar, `/`
> focuses chat. Full suite **205 passed / 0 failed / 0 skipped**
> (43.2 s).
>
> **Full UI redesign, operator + call consoles (2026-10-30, commit
> 4988241):** user chose the full-redesign option for both pages. New
> design system (CSS custom properties): deep forest-green gradient
> sidebar with white links and icon tiles (statically visible on
> screens wider than 1150 px, hamburger + overlay below), refined
> typography scale (15 px body, 1.7 rem page titles, big tabular-nums
> stat numbers), card depth with soft shadows and hover lift, one
> consistent status-color system across badges/pills/priorities,
> dashed empty states ("No tasks scheduled", transcript placeholder),
> device-style phone screen on the call page (dark gradient header
> strip with ADAM avatar, tabular timer, gradient driver bubbles,
> white agent cards), stylized incoming-ring card with rounded ADAM
> avatar, and mobile-friendly paddings. All element IDs, handlers and
> behavior kept byte-identical: mic/barge-in, ring Answer/Decline,
> Hang Up, token persistence, keyboard shortcuts, view switching.
> Verified by a byte scan (zero emojis, zero mojibake, every
> get-element id resolves) and live in the browser: sidebar hamburger
> open/Escape-close (open=left 0, closed=left -306), all four views
> switch, chips present, "/" focuses chat; call page full cycle on a
> real session — ring showed "Redesign Test Lines", Answer connected
> with timer running, driver bubble in gradient style, agent reply
> shown, Hang Up returned the setup screen. Note: csstransition on the
> sidebar appears frozen when the tab is not focused (browser
> throttling) — verified by disabling transition during measurement;
> real usage is unaffected. Server + langgraph-postgres container
> relaunched after an overnight cold start. Full suite **205 passed /
> 0 failed / 0 skipped** (34.3 s).
>
> **Admin token permanently saved (2026-10-30, commit 22871d3):** new
> `GET /admin/api-token` endpoint in `main.py` returns the configured
> `ADMIN_API_TOKEN` so the operator never retypes it. Restricted to
> loopback clients only — `127.0.0.1`, `::1`, `localhost`, and the
> TestClient default host; anything else gets **403** (logged as
> `admin_api_token_refused`), an unconfigured token gets **503**, and
> success is logged as `admin_api_token_bridged`. Both UI pages call it
> automatically on load when no token is in `sessionStorage`/`localStorage`,
> then persist what they received to both storages. Verified three ways:
> 3 new tests in `tests/test_auth_admin.py` (local client hands the
> token, remote client refused 403 via direct call with a fake remote
> scope, missing-client edge refused 403; file now 24 passed standalone),
> live browser verification with both storages cleared on fresh tabs —
> `/pipeline` auto-filled the token box with "Token loaded from the
> server automatically", green status pill on, tasks list loaded with
> no 401, and `/call` fetched the token and rendered 11 recent-caller
> rows — and a full suite run of **209 passed / 0 failed / 0 skipped**
> (43.3 s). README API table updated with the endpoint.
>
> **Assistant conversational mode — "let me talk to you" fix
> (2026-10-30, commit 78b58b6):** user reported that asking Adam to
> "let me talk to him" made the agent open the carrier call console
> instead of chatting with the operator. Server logs confirmed the real
> failure: the planner had routed operator chat to `place_outbound_call`
> which resumed a previous carrier session (usdot:1234567) and returned
> an auto-connect URL to `/call`. Fix, three layers: (1) new whitelisted
> `just_chat` action with `args.reply` — the model may now answer the
> operator conversationally (greetings, small talk, "talk to me") with
> no side effects, including in the system prompt with explicit rules
> that pronouns (me/you/him/her/us/them) are never carrier names;
> (2) deterministic fallback parser recognizes talk-request phrases
> ("talk to you", "talk to me", "let me call you", "hello"...,
> checked last so concrete actions like metrics/search always win) and
> reflexive pronoun targets in "call X" (call me / call him / phone you)
> now return a just_chat reply inviting the operator to press the green
> mic instead of opening the call console; (3) an executor safety net
> redirects any `place_outbound_call` whose every provided target is a
> reflexive pronoun, with `begin_call` never running (proven by a test
> that explodes if it does). Verified live with two bounded Groq calls
> on the restarted server: "let me talk to you" -> action `just_chat`,
> reply "Sure! Press the green mic button to start speaking.", empty
> data (no auto-connect URL); "let me call you" -> same behavior.
> 10 new tests in `tests/test_assistant.py` (71 passed standalone).
> Full suite **228 passed / 0 failed / 0 skipped** (68.2 s).
>
> **Due-task reminder alarm (2026-10-30, commit f6819e2):** when a
> scheduled task turns due, the operator console (pipeline page) now
> announces it instead of relying on the user to open the reminders
> view. The server scheduler already flipped tasks to status `due`
> every 30 s; the page's existing 30 s poll now feeds the fresh
> `checkDueTasks()` which shows an alert card (amber bell, task title,
> due time) with three buttons — "Mark done" (posts
> `/tasks/{id}/done` and silences forever), "Remind me later"
> (snooze: rings again on a later poll ~5 minutes later), and
> "Dismiss" (persists the task id in localStorage so it never rings
> again). A generated two-tone Web-Audio chime repeats every 1.8 s
> while the card is open (AudioContext primed on the operator's first
> click/keypress to satisfy browser autoplay rules), and the page
> speaks "Reminder! Time to: <title>" via speech synthesis. Snoozes
> persist across page reloads via a `snoozed_due_tasks` localStorage
> map (expired entries are pruned on each check) — this was a bug
> found and fixed live during verification. Byte scan clean (no
> emojis, no mojibake, all element ids resolve); live browser
> verification: test reminder created via `/tasks/reminders`,
> scheduler flipped it to due, page reload showed the card with
> ringing state, correct title and the due row; snooze closed the
> card and a reload stayed silent; "Remind me later" stored
> `{"1": <ts>}` in localStorage; a second test reminder showed "Mark
> done" closing the card and the task left the active list (server
> status done). Both test tasks cleaned up afterwards; database left
> empty as the operator requested. Full suite **228 passed / 0 failed
> / 0 skipped** (106.2 s).
>
> **Database cleanup on operator request (2026-10-30):** full
> pg_dump backup taken first
> (`backups/truck_dispatch_db_2026-10-30_full.sql.bak`, 373 KB, all 7
> tables, verified: 7 COPY dumps, no error markers, gitignored via
> `*.sql.bak`), then all tables truncated with
> `RESTART IDENTITY CASCADE` (carrier_leads 487, carrier_reviews 6,
> call_sessions 13, call_messages 48, scheduled_tasks 2 -> all 0).
> Schema untouched; the app stayed healthy throughout.
>
> **In-browser carrier data viewer (2026-10-31, commit 5b027ac):**
> user asked to see the export sheets in the browser instead of
> downloading files. Added `GET /export-data` (admin token required)
> returning the exact rows the Excel export uses — the SQL was
> refactored into a shared `_export_rows()` so `/export-excel` and
> `/export-data` can never drift apart; `?review_status=` and
> `?limit=` (default 500, max 2000) filters, JSON out. On the
> pipeline page every export tile now has a "View in browser" button
> beside Download which opens a large modal: title, live count,
> search box filtering by USDOT/company/phone, sticky-header scroll
> table (USDOT, company, phone, fleet, score, priority colors,
> review status, follow-up, notes), top 300 rows, closes via Close
> button, background click, or Escape. Verified 7 new tests in
> `tests/test_export_data.py` (auth 401, Literal validation 422 for
> unknown status, limit bounds 422, dict shape, filter+limit reaching
> the SQL, DB-error 500, and `/export-excel` still producing a valid
> xlsx "PK" zip after the refactor). Live browser verification with
> two seeded test carriers and one review: "All leads" viewer showed
> "2 carriers" with correct data and joined review status, search
> narrowed to "1 of 2", Escape closed the modal; the "Interested
> carriers" tile showed only the reviewed carrier. Test rows deleted
> afterwards; database left empty as the operator requested. Docker
> Desktop needed restarting after an overnight boot. Full suite
> **235 passed / 0 failed / 0 skipped** (91.1 s).
>
> **Sidebar collapse on wide screens (2026-10-31, commit 0518ffe):**
> user report: on his wide browser the sidebar was permanently open
> with no way to close it (the redesign pinned it above 1150 px and
> hid the hamburger). Fix in `static/pipeline.html`, id/behavior
> compatible. CSS: the ≥1150 px block now keeps the sidebar open by
> default but honors a new `closed-desktop` class
> (*`transform: translateX(-102%)`*). `#menuBtn` stays visible at
> every width, and `body.side-closed .wrap { margin-left: 0 }` reclaims
> sidebar space when collapsed. JS: the hamburger now routes to
> `toggleDesktopSidebar()` on wide screens (same menu button opens
> and closes); Escape collapses the pinned sidebar (close only, never
> expands, data viewer still handled first); the choice persists in
> `localStorage["side_desktop"]` = "open"/"closed"; a real
> `matchMedia` change listener re-applies state when the window is
> resized across the 1150 px boundary, and on wide load the stored
> choice is honored instead of always opening. Verification: narrow
> mode (800 px) regression — hamburger opens (left 0, overlay on),
> Escape closes (left -306, overlay off), localStorage untouched. Wide
> mode simulated in the browser via a session matchMedia stub
> (interpreter viewport is 800 px; there is no resize tool) plus a
> temporary style sheet replicating the media block's selectors so the
> real geometry was measured: toggle collapsed
> (`sidebar closed-desktop`, `body side-closed`, left -306, wrap
> margin 0, `side_desktop`="closed"), second click reopened
> (left 0, wrap margin 300 px, `side_desktop`="open"), Escape
> collapsed again; menu button visible in desktop mode. Two retry
> lessons recorded: a wrong result was first caused by STALE JS on an
> unreloaded tab (always reload after editing static HTML), and a
> second wrong measurement caught the sidebar mid-animation —
> transition must be disabled during `getBoundingClientRect` reads.
> Stale `side_desktop` values are harmless in narrow mode (verified by
> loading with "closed" stored). Scanner-passed: zero emojis/mojibake,
> all ids resolve, data-viewer structure intact. Real-viewport step for
> the operator: hard-refresh `/pipeline` (Ctrl+Shift+R) on his wide
> browser — menu button should now collapse/expand and remember his
> choice. Backend untouched; no test count change (suite last ran
> 235 passed).
>
> **Found the real wide-screen cause (2026-11-01, commit 1d01ecf):**
> the operator's screenshot showed the sidebar still permanently open
> with no hamburger even after the toggle logic WAS being served
> ("operator console" title visibly cut at its left edge). Root
> cause: `.topbar` is a direct child of `<body>` spanning the FULL
> window width, so on wide screens the pinned sidebar (z-index 60)
> covered the topbar (z-index 40) — the rendered
> `#menuBtn` sat underneath the sidebar and could never be seen or
> clicked. Fix inside the ≥1150 px block: `.topbar { margin-left:
> 300px }` moves the whole bar right of the sidebar, `body.side-closed
> .topbar { margin-left: 0 }` restores the full width when the
> sidebar is collapsed. Browser-verified in a wide simulation with
> the replicated media CSS: sidebar open -> menu button at left 330
> (clear of the 300 px sidebar, on-screen, clickable), topbar at 300;
> collapse -> menu button back at left 30, topbar at 0, sidebar at
> -306, `side_desktop`="closed"; Escape collapse and re-open via the
> button both work. Narrow-mode regression re-checked after the CSS
> edit (hamburger opens, Escape closes, viewport untouched). Byte
> scan clean. This ALSO clears the cache suspicion for the operator:
> his browser had fetched the page before the first fix was served,
> and the earlier no-cache header (commit e05269c) makes every future
> load revalidate automatically. Operator still needs ONE normal
> refresh (F5) to pick up the corrected page; afterwards reloads
> always serve fresh code.

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
