
import csv
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Jsonb


PROJECT_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = PROJECT_DIR / "output" / "qualified_leads.json"

load_dotenv(PROJECT_DIR / ".env")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "5442")),
    "dbname": os.getenv("DB_NAME", "truck_dispatch_db"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD"),
}


def connect_db():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        parts = urlsplit(dsn)
        query = parse_qs(parts.query)
        if "sslmode" not in query:
            separator = "&" if parts.query else "?"
            dsn += separator + "sslmode=require"
        return psycopg.connect(dsn, connect_timeout=15)
    return psycopg.connect(**DB_CONFIG)


def get_value(record, *possible_names, default=None):
    """Return the first available value among possible field names."""
    for name in possible_names:
        value = record.get(name)
        if value is not None and value != "":
            return value
    return default


UPSERT_SQL = """
INSERT INTO carrier_leads (
    usdot_number,
    company_name,
    phone,
    vehicle_count,
    allow_to_operate,
    carrier_status_code,
    carrier_operation,
    identity_match,
    lookup_status,
    lead_score,
    priority,
    basic_qualification_passed,
    review_required,
    review_reasons,
    application_categories,
    fleet_segment,
    source_record
)
VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s, %s, %s, %s, %s, %s, %s
)
ON CONFLICT (usdot_number) DO UPDATE SET
    company_name = EXCLUDED.company_name,
    phone = EXCLUDED.phone,
    vehicle_count = EXCLUDED.vehicle_count,
    allow_to_operate = EXCLUDED.allow_to_operate,
    carrier_status_code = EXCLUDED.carrier_status_code,
    carrier_operation = EXCLUDED.carrier_operation,
    identity_match = EXCLUDED.identity_match,
    lookup_status = EXCLUDED.lookup_status,
    lead_score = EXCLUDED.lead_score,
    priority = EXCLUDED.priority,
    basic_qualification_passed =
        EXCLUDED.basic_qualification_passed,
    review_required = EXCLUDED.review_required,
    review_reasons = EXCLUDED.review_reasons,
    application_categories =
        EXCLUDED.application_categories,
    fleet_segment = EXCLUDED.fleet_segment,
    source_record = EXCLUDED.source_record,
    updated_at = CURRENT_TIMESTAMP;
"""


def record_to_params(record):
    """Map one qualified-lead record to the UPSERT_SQL parameter tuple."""
    usdot = get_value(record, "usdot_number", "usdot", "USDOTNumber")

    if usdot is None:
        return None

    usdot = str(usdot).strip()

    if not usdot:
        return None

    company_name = get_value(
        record,
        "official_legal_name",
        "legal_business_name",
        "company_name",
        "source_company_name",
        "business_name",
    )

    vehicle_count = get_value(
        record, "vehicle_count", "power_units", "fleet_size"
    )

    try:
        vehicle_count = (
            int(vehicle_count)
            if vehicle_count is not None
            else None
        )
    except (TypeError, ValueError):
        vehicle_count = None

    score = get_value(record, "lead_score", "score")
    try:
        score = int(score) if score is not None else None
    except (TypeError, ValueError):
        score = None

    safe_source = {
        key: record.get(key)
        for key in (
            "usdot_number",
            "legal_business_name",
            "source_company_name",
            "official_legal_name",
            "phone",
            "vehicle_count",
            "allow_to_operate",
            "carrier_status_code",
            "carrier_operation",
            "identity_match",
            "lookup_status",
            "lead_score",
            "priority",
            "basic_qualification_passed",
            "review_required",
            "review_reasons",
            "application_categories",
        )
        if key in record
    }

    carrier_operation = get_value(record, "carrier_operation")

    if isinstance(carrier_operation, dict):
        carrier_operation = json.dumps(carrier_operation)

    return (
        usdot,
        company_name,
        get_value(record, "phone", "telephone"),
        vehicle_count,
        get_value(record, "allow_to_operate", "allowed_to_operate"),
        get_value(record, "carrier_status_code", "status_code"),
        carrier_operation,
        get_value(record, "identity_match"),
        get_value(record, "lookup_status"),
        score,
        get_value(record, "priority"),
        get_value(record, "basic_qualification_passed"),
        get_value(record, "review_required"),
        Jsonb(get_value(record, "review_reasons", default=[])),
        Jsonb(get_value(record, "application_categories", default=[])),
        get_value(record, "fleet_segment"),
        Jsonb(safe_source),
    )


def upsert_records(cursor, records):
    """Upsert records into carrier_leads via an existing open cursor.

    Returns (imported, skipped). Used by main() and the pipeline UI job.
    """
    imported = 0
    skipped = 0

    cursor.execute(CREATE_TABLE_SQL)

    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue

        params = record_to_params(record)

        if params is None:
            skipped += 1
            continue

        cursor.execute(UPSERT_SQL, params)
        imported += 1

    return imported, skipped


CREATE_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS carrier_leads (
        usdot_number TEXT PRIMARY KEY,
        company_name TEXT,
        phone TEXT,
        vehicle_count INTEGER,
        allow_to_operate TEXT,
        carrier_status_code TEXT,
        carrier_operation TEXT,
        identity_match BOOLEAN,
        lookup_status TEXT,
        lead_score INTEGER,
        priority TEXT,
        basic_qualification_passed BOOLEAN,
        review_required BOOLEAN,
        review_reasons JSONB,
        application_categories JSONB,
        fleet_segment TEXT,
        source_record JSONB,
        imported_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );
"""


def main():
    if not (os.getenv("DATABASE_URL") or DB_CONFIG.get("password")):
        raise RuntimeError(
            "DB_PASSWORD is missing. "
            "Set it in your .env file or PowerShell session first."
        )

    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"Input file not found: {INPUT_FILE}")

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        records = data.get("leads", data.get("records", []))
    else:
        raise ValueError("Unexpected JSON structure.")

    if not records:
        raise ValueError(
            "No leads found. Check the structure of qualified_leads.json."
        )

    with connect_db() as connection:
        with connection.cursor() as cursor:

            imported, skipped = upsert_records(cursor, records)

        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM carrier_leads;")
            total = cursor.fetchone()[0]

    print("Import completed successfully!")
    print(f"Input records: {len(records)}")
    print(f"Processed records: {imported}")
    print(f"Skipped records without a USDOT: {skipped}")
    print(f"Total unique leads in database: {total}")


if __name__ == "__main__":
    main()
