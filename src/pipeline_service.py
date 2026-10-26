"""
Pipeline orchestration service.

Runs the existing phase modules (extract -> prepare -> verify ->
qualify -> optional DB import) as a background job so the /pipeline
UI can show live progress of every stage.

All phase code is reused, not rebuilt. The job registry is in-memory
only (jobs vanish on server restart), consistent with the runtime layer
being decoupled from the offline phase scripts. Per-job outputs are
written to output/pipeline_runs/<job_id>/ so the production output/
files are never touched.
"""

import json
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = PROJECT_ROOT / "output" / "pipeline_runs"

# Load .env so FMCSA_WEB_KEY behaves the same as when running the
# phase scripts directly.
load_dotenv(PROJECT_ROOT / ".env")

# Rate limit between FMCSA API calls during the verify stage, seconds.
FMCSA_CALL_DELAY = 0.25

MAX_LOG_LINES = 400


class Job:
    """One pipeline run: stages, logs, and final results."""

    def __init__(self, job_id, original_filename):
        self.job_id = job_id
        self.original_filename = original_filename
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.status = "running"  # running | done | failed
        self.error = None
        self.log_lines = []
        self.results = None
        self.stages = [
            {"name": "extract", "label": "Extract PDF", "status": "pending",
             "detail": ""},
            {"name": "prepare", "label": "Clean & classify", "status":
             "pending", "detail": ""},
            {"name": "verify", "label": "FMCSA verification", "status":
             "pending", "detail": ""},
            {"name": "qualify", "label": "Score & qualify", "status":
             "pending", "detail": ""},
            {"name": "import", "label": "Import to PostgreSQL", "status":
             "pending", "detail": ""},
        ]

    def to_dict(self):
        return {
            "job_id": self.job_id,
            "original_filename": self.original_filename,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "log_lines": self.log_lines[-MAX_LOG_LINES:],
            "results": self.results,
            "stages": self.stages,
        }


class _JobRegistry:
    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def create(self, original_filename):
        job = Job(uuid.uuid4().hex[:12], original_filename)
        with self._lock:
            self._jobs[job.job_id] = job
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit=20):
        """Return the most recent jobs, newest first."""
        with self._lock:
            jobs = sorted(
                self._jobs.values(),
                key=lambda job: job.created_at,
                reverse=True,
            )
            return jobs[:limit]


registry = _JobRegistry()


def _log(job, message):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {message}"
    with registry._lock:
        job.log_lines.append(line)


def _stage(job, name):
    for stage in job.stages:
        if stage["name"] == name:
            return stage
    raise KeyError(name)


def _run_pipeline(job, pdf_path, run_dir, import_to_db=False, max_records=None):
    """Execute all pipeline stages for one job. Runs in a thread."""

    def log(message):
        _log(job, message)

    log(f"Pipeline job started for '{job.original_filename}'")
    summary = {}
    summary["imported"] = None

    try:
        # ---- Stage 1: extract ------------------------------------------------
        stage = _stage(job, "extract")
        stage["status"] = "running"
        stage["detail"] = "Reading PDF"
        log("Extracting carrier records from PDF...")

        import extract_pdf

        records = extract_pdf.extract_pdf(Path(pdf_path))

        if max_records is not None and int(max_records) < len(records):
            log(f"Capping at {int(max_records)} of {len(records)} records "
                "(operator limit)")
            records = records[: int(max_records)]

        summary["records_extracted"] = len(records)
        stage["status"] = "done"
        stage["detail"] = f"{len(records)} record(s) extracted"
        log(f"Extraction complete: {len(records)} record(s) found")

        if not records:
            raise ValueError("No carrier records were found in the PDF.")

        _save(run_dir, "01_extracted.json", records)

        # ---- Stage 2: prepare --------------------------------------------------
        stage = _stage(job, "prepare")
        stage["status"] = "running"
        stage["detail"] = "Grouping and classifying"
        log("Cleaning records and classifying carrier categories...")

        import prepare_leads

        prepared = prepare_leads.process_records(records)
        candidates = prepared.get("candidates", [])
        summary["candidates"] = len(candidates)
        summary["duplicate_groups"] = prepared.get("duplicate_groups")
        summary["conflicting_groups"] = prepared.get("conflicting_groups")
        summary["review_queue_count"] = len(prepared.get("review_queue", []))

        stage["status"] = "done"
        stage["detail"] = f"{len(candidates)} candidate(s) after cleaning"
        log(f"Cleaning complete: {len(candidates)} unique USDOT candidate(s)")

        _save(run_dir, "02_prepared.json", prepared)

        # ---- Stage 3: verify ----------------------------------------------------
        stage = _stage(job, "verify")
        stage["status"] = "running"
        stage["detail"] = "Calling FMCSA API"
        log("Verifying carrier identities against the official FMCSA API...")

        api_key = os.getenv("FMCSA_WEB_KEY")
        working_records = candidates

        if not api_key:
            stage["status"] = "skipped"
            stage["detail"] = (
                "FMCSA_WEB_KEY not configured - verification skipped"
            )
            log("Verification skipped (no FMCSA_WEB_KEY configured).")
        else:
            import requests

            import verify_carriers

            session = requests.Session()
            verified_records = []
            for index, candidate in enumerate(candidates, start=1):
                result = verify_carriers.verify_carrier(
                    session, candidate, api_key
                )
                verified_records.append(result)
                if index % 5 == 0 or index == len(candidates):
                    log(f"FMCSA verification: {index}/{len(candidates)}")
                time.sleep(FMCSA_CALL_DELAY)

            summary["verified"] = len(verified_records)
            responded = sum(
                1
                for item in verified_records
                if item.get("lookup_status")
                in {"response_received", "needs_manual_review"}
            )
            failed = len(verified_records) - responded
            stage["status"] = "done"
            if responded:
                stage["detail"] = (
                    f"{responded} of {len(verified_records)} carrier(s) "
                    f"verified" + (f" ({failed} failed)" if failed else "")
                )
            else:
                stage["detail"] = (
                    f"0 of {len(verified_records)} verified - "
                    "FMCSA API unreachable/blocked"
                )
            log(f"FMCSA verification complete: {responded} verified, "
                f"{failed} failed")
            if responded == 0 and verified_records:
                log(
                    "WARNING: the FMCSA API returned errors for every "
                    "carrier. Check network access (FMCSA blocks many "
                    "non-US IPs) or the FMCSA_WEB_KEY value."
                )
            working_records = verified_records

        _save(run_dir, "03_verified.json", working_records)

        # ---- Stage 4: qualify ---------------------------------------------------
        stage = _stage(job, "qualify")
        stage["status"] = "running"
        stage["detail"] = "Scoring leads"
        log("Scoring carriers and sorting by priority...")

        import qualify_leads

        verified_by_usdot = {
            qualify_leads.normalize_usdot(item.get("usdot_number")): item
            for item in working_records
            if item.get("usdot_number") is not None
        }

        leads = []
        for candidate in candidates:
            usdot = qualify_leads.normalize_usdot(
                candidate.get("usdot_number")
            )
            verified = verified_by_usdot.get(usdot)
            if verified is None:
                # Mirror qualify_leads.main(): score without verification
                # flagged as missing_verification rather than dropped.
                verified = {
                    "usdot_number": usdot,
                    "lookup_status": "missing_verification",
                    "identity_match": None,
                    "allow_to_operate": None,
                    "carrier_status_code": None,
                    "vehicle_count": None,
                    "total_drivers": None,
                    "official_legal_name": None,
                    "official_phone": None,
                    "source_phone": candidate.get("business_telephone"),
                    "errors": ["Verification skipped or missing"],
                }
            leads.append(qualify_leads.qualify_carrier(candidate, verified))

        leads.sort(key=_priority_sort_key, reverse=True)

        summary["qualified"] = len(leads)
        high = sum(1 for lead in leads if lead.get("priority") == "high")
        summary["high_priority"] = high
        stage["status"] = "done"
        stage["detail"] = f"{len(leads)} lead(s) scored ({high} high)"
        log(f"Qualification complete: {len(leads)} lead(s), {high} high")
        log("Top leads:")
        for lead in leads[:5]:
            log(
                f" - {lead.get('usdot_number')} "
                f"score={lead.get('lead_score')} "
                f"priority={lead.get('priority')}"
            )

        _save(run_dir, "04_qualified.json", leads)

        # ---- Stage 5: import (optional, dry-run by default) ------------------------
        stage = _stage(job, "import")
        if not import_to_db:
            stage["status"] = "skipped"
            stage["detail"] = "Dry run - nothing written to the database"
            log("Database import skipped (dry run). Leads saved as files only.")
        else:
            stage["status"] = "running"
            stage["detail"] = "Writing to PostgreSQL"
            log("Importing qualified leads to PostgreSQL...")

            import import_to_postgres as importer

            if not importer.DB_CONFIG["password"]:
                raise RuntimeError(
                    "DB_PASSWORD is missing - cannot import to database"
                )

            with importer.psycopg.connect(
                **importer.DB_CONFIG
            ) as connection:
                with connection.cursor() as cursor:
                    imported, _skipped = importer.upsert_records(cursor, leads)

            summary["imported"] = imported
            stage["status"] = "done"
            stage["detail"] = f"{imported} lead(s) upserted"
            log(f"Database import complete: {imported} upserted")

        job.results = summary
        job.status = "done"
        log("Pipeline job finished successfully.")

    except Exception as error:  # noqa: BLE001 - the job fails, not the server
        if job.status != "done":
            job.status = "failed"
            job.error = str(error)
            failed_stage = next(
                (s for s in job.stages if s["status"] == "running"), None
            )
            if failed_stage:
                failed_stage["status"] = "failed"
                failed_stage["detail"] = str(error)
            log(f"PIPELINE ERROR: {error}")
            log(traceback.format_exc(limit=4))


def _priority_sort_key(lead):
    priority_order = {"high": 3, "medium": 2, "low": 1}
    return (
        priority_order.get(lead.get("priority"), 0),
        lead.get("lead_score") or 0,
    )


def _save(run_dir, filename, data):
    (run_dir / filename).write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def start_job(pdf_path, original_filename, import_to_db=False,
              max_records=None):
    """Create a job and run it in a background thread."""
    job = registry.create(original_filename)
    run_dir = RUNS_ROOT / job.job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    thread = threading.Thread(
        target=_run_pipeline,
        args=(job, Path(pdf_path), run_dir, import_to_db, max_records),
        daemon=True,
        name=f"pipeline-{job.job_id}",
    )
    thread.start()
    return job
