-- TruckDispatchAgent database schema
-- Safe to run on an existing database: everything uses IF NOT EXISTS.
-- Rebuilt from what import_to_postgres.py and main.py read and write.

-- 1. Carrier leads (created by import_to_postgres.py)
CREATE TABLE IF NOT EXISTS public.carrier_leads (
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

-- Temporary reservation columns used by /next-carrier (main.py)
ALTER TABLE public.carrier_leads
    ADD COLUMN IF NOT EXISTS review_reserved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_reserved_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_reserved_by TEXT;

-- 2. Call attempts (used by /carrier-call-attempts)
CREATE TABLE IF NOT EXISTS public.carrier_call_attempts (
    id BIGSERIAL PRIMARY KEY,
    usdot_number TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    call_status TEXT NOT NULL DEFAULT 'not_started',
    outcome TEXT,
    interested BOOLEAN,
    consent_status TEXT NOT NULL DEFAULT 'unknown',
    do_not_call BOOLEAN NOT NULL DEFAULT FALSE,
    follow_up_at TIMESTAMPTZ,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_call_attempts_usdot
    ON public.carrier_call_attempts (usdot_number);

-- 3. Carrier reviews (used by /carrier-reviews)
CREATE TABLE IF NOT EXISTS public.carrier_reviews (
    id BIGSERIAL PRIMARY KEY,
    usdot_number TEXT NOT NULL,
    review_status TEXT NOT NULL CHECK (
        review_status IN (
            'interested',
            'not_interested',
            'callback_requested',
            'do_not_contact'
        )
    ),
    follow_up_at TIMESTAMPTZ,
    notes TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_carrier_reviews_usdot
    ON public.carrier_reviews (usdot_number, reviewed_at DESC);

-- No foreign keys on purpose: later, when not-interested carriers' details
-- are deleted, we want to keep only a small "already contacted" record.

-- 4. Audit trail (used by record_audit in main.py)
CREATE TABLE IF NOT EXISTS public.operator_audit_log (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    action TEXT NOT NULL,
    usdot_number TEXT,
    request_id TEXT,
    details JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_created_at
    ON public.operator_audit_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_usdot
    ON public.operator_audit_log (usdot_number);