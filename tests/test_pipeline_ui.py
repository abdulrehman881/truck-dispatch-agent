"""
Pipeline UI orchestration tests: the background job runs the real phase
modules on a synthetic PDF and monkeypatched verification, plus
authentication tests for the /pipeline endpoints.
"""

import json

from fastapi.testclient import TestClient

import main
import pipeline_service


class FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return [0]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def cursor(self):
        return FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


client = TestClient(main.app)
authed = TestClient(
    main.app, headers={"Authorization": "Bearer test-admin-token"}
)


def real_pdf_path():
    """The production registration PDF (read-only, never modified)."""
    path = main.PROJECT_ROOT / "data" / "REGISTER20261005.pdf"
    if not path.is_file():
        import pytest
        pytest.skip("Real registration PDF not present in data/")
    return path


def _run_job(pdf_path, monkeypatch, import_to_db=False, max_records=150):
    """Run a pipeline job synchronously (in-process, no thread)."""
    monkeypatch.delenv("FMCSA_WEB_KEY", raising=False)

    job = pipeline_service.registry.create(pdf_path.name)
    run_dir = pipeline_service.RUNS_ROOT / job.job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    pipeline_service._run_pipeline(
        job, pdf_path, run_dir, import_to_db=import_to_db,
        max_records=max_records,
    )
    return job


def test_pipeline_extracts_classifies_and_qualifies(monkeypatch):
    job = _run_job(real_pdf_path(), monkeypatch)

    assert job.status == "done", job.log_lines
    statuses = {s["name"]: s["status"] for s in job.stages}
    assert statuses["extract"] == "done"
    assert statuses["prepare"] == "done"
    assert statuses["verify"] == "skipped"
    assert statuses["qualify"] == "done"
    assert statuses["import"] == "skipped"

    results = job.results
    assert results["records_extracted"] >= 1
    assert results["records_extracted"] <= 150
    assert results["qualified"] >= 1
    assert results["imported"] is None


def test_pipeline_writes_job_output_files(monkeypatch):
    job = _run_job(real_pdf_path(), monkeypatch)
    run_dir = pipeline_service.RUNS_ROOT / job.job_id

    assert (run_dir / "01_extracted.json").is_file()
    assert (run_dir / "02_prepared.json").is_file()
    assert (run_dir / "03_verified.json").is_file()
    assert (run_dir / "04_qualified.json").is_file()

    leads = json.loads((run_dir / "04_qualified.json").read_text("utf-8"))
    assert leads, "qualification produced no leads"
    first = leads[0]
    assert first.get("usdot_number")
    assert "lead_score" in first
    assert first.get("priority") in ("high", "medium", "low")


def test_pipeline_import_stage(monkeypatch):
    """When import_to_db is on, the import stage upserts via the DB layer."""
    executed = []

    def fake_upsert(cursor, records):
        executed.extend(records)
        return len(records), 0

    monkeypatch.delenv("FMCSA_WEB_KEY", raising=False)

    import import_to_postgres as importer

    monkeypatch.setattr(importer, "upsert_records", fake_upsert)
    monkeypatch.setattr(importer, "DB_CONFIG", {"password": "x"})

    probe_pdf = tmp_probe_pdf()
    job = pipeline_service.registry.create(probe_pdf.name)
    run_dir = pipeline_service.RUNS_ROOT / job.job_id
    run_dir.mkdir(parents=True, exist_ok=True)

    import extract_pdf
    monkeypatch.setattr(
        extract_pdf, "extract_pdf", lambda path: [sample_record()]
    )

    class FakePsycopg:
        @staticmethod
        def connect(*args, **config):
            return FakeConnection()

    monkeypatch.setattr(importer.psycopg, "connect", FakePsycopg.connect)

    pipeline_service._run_pipeline(
        job, probe_pdf, run_dir, import_to_db=True, max_records=5
    )

    assert job.status == "done", job.log_lines
    statuses = {s["name"]: s["status"] for s in job.stages}
    assert statuses["import"] == "done"
    assert len(executed) == job.results["imported"]
    assert all("lead_score" in record for record in executed)


def sample_record():
    return {
        "usdot_number": "1234567",
        "legal_business_name": "AAA Test Carrier",
        "business_telephone": "(214) 555-0100",
        "application_category": "motor_carrier",
    }


def tmp_probe_pdf():
    probe = pipeline_service.RUNS_ROOT / "_probe.pdf"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"%PDF-probe")
    return probe


def test_pipeline_status_endpoint_auth_and_flow(monkeypatch):
    job = _run_job(real_pdf_path(), monkeypatch)

    assert authed.get("/pipeline/status/doesnotexist").status_code == 404
    assert client.get("/pipeline/status/" + job.job_id).status_code == 401

    res = authed.get("/pipeline/status/" + job.job_id)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert [s["name"] for s in body["stages"]] == [
        "extract", "prepare", "verify", "qualify", "import",
    ]
    assert "job_id" in body


def test_pipeline_leads_endpoint(monkeypatch):
    job = _run_job(real_pdf_path(), monkeypatch)

    res = authed.get("/pipeline/leads/" + job.job_id)
    assert res.status_code == 200
    leads = res.json()
    assert isinstance(leads, list) and leads

    res = client.get("/pipeline/leads/" + job.job_id)
    assert res.status_code == 401


def test_pipeline_page_is_public():
    res = client.get("/pipeline")
    assert res.status_code == 200
    assert "Carrier Pipeline" in res.text


def test_pdf_upload_validations():
    res = authed.post(
        "/pipeline/upload",
        files={"file": ("x.txt", b"not a pdf", "text/plain")},
    )
    assert res.status_code == 400

    res = client.post(
        "/pipeline/upload",
        files={"file": ("x.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert res.status_code == 401
