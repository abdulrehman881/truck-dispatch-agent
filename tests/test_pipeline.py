"""
Phase 1/2 pipeline tests using small synthetic inputs.

These tests run src/ helpers directly with made-up records.
No network, no real PDF, no paid API calls.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import extract_pdf, prepare_leads, qualify_leads  # noqa: E402


# --------------------------------------------------
# Phase 1: helpers
# --------------------------------------------------

def test_normalize_usdot_keeps_digits_only():
    assert extract_pdf.normalize_usdot("USDOT 1234567") == "1234567"
    assert extract_pdf.normalize_usdot("") is None
    assert extract_pdf.normalize_usdot(None) is None


def test_valid_phone_format():
    assert extract_pdf.valid_phone_format("555-123-4567") is True
    assert extract_pdf.valid_phone_format("1 (555) 123-4567") is True
    assert extract_pdf.valid_phone_format("1234") is False
    assert extract_pdf.valid_phone_format("") is False


def test_is_header_detects_repeated_headers():
    assert extract_pdf.is_header(
        ["USDOT Number", "Legal Business Name", "a", "b", "c", "d"]
    ) is True
    assert extract_pdf.is_header(["ACME Trucks", ""]) is False
    assert extract_pdf.is_header([]) is False


def test_is_category_heading():
    assert extract_pdf.is_category_heading(
        "MOTOR CARRIER OF PROPERTY"
    ) is True
    assert extract_pdf.is_category_heading("ACME TRUCKING LLC") is False
    assert extract_pdf.is_category_heading("") is False


def test_update_category_from_blocks_picks_nearest_heading():
    current = None
    current = extract_pdf.update_category_from_blocks(
        [
            (0, 0, 10, 10, "BROKER OF HOUSEHOLD GOODS"),
            (50, 200, 10, 210, "MOTOR CARRIER OF PROPERTY"),
            (300, 0, 10, 310, "ACME TRUCKING LLC"),
        ],
        table_top=400.0,
        current_category=current,
    )
    assert "MOTOR CARRIER OF PROPERTY" in current
    # Headings below the table top are ignored.
    assert "ACME" not in current


def test_extraction_summary_reconciles_counts():
    records = [
        {
            "usdot_number": "123",
            "legal_business_name": "Truck Co 1",
            "business_telephone": "5551234567",
            "phone_format_valid": True,
            "application_category": "MOTOR CARRIER OF PROPERTY",
            "business_mailing_address": "1 Main St",
            "company_officer": "J. Doe",
        },
        {
            "usdot_number": "123",
            "legal_business_name": "Truck Co 1",
            "business_telephone": "",
            "phone_format_valid": False,
            "application_category": "",
            "business_mailing_address": "",
            "company_officer": "",
        },
    ]
    summary = extract_pdf.create_summary(records, ["f.pdf"], [])

    # Total equals the sum of records processed; duplicates detected.
    assert summary["total_records"] == 2
    assert summary["unique_usdot_numbers"] == 1
    assert summary["duplicate_usdot_numbers"] == 1
    assert summary["duplicate_usdot_details"] == {"123": 2}
    assert summary["pdf_files_failed"] == 0

    # Every missing-field counter must match the records themselves.
    assert summary["records_missing_phone"] == 1
    assert summary["records_missing_category"] == 1
    assert summary["records_missing_address"] == 1
    assert summary["records_missing_officer"] == 1
    assert summary["records_with_invalid_phone_format"] == 1


# --------------------------------------------------
# Phase 2 of the pipeline: filtering and review
# --------------------------------------------------

def _record(usdot, name, phone, category):
    return {
        "usdot_number": usdot,
        "legal_business_name": name,
        "business_telephone": phone,
        "business_mailing_address": "1 Main St",
        "application_category": category,
    }


def clean_carrier_record():
    return _record(
        "111111",
        "Clean Movers LLC",
        "5551234567",
        "MOTOR CARRIER OF PROPERTY",
    )


def test_prepare_leads_groups_and_cleans_conflicts():
    records = [
        clean_carrier_record(),
        _record("222222", "Confused Co", "5551110000", "MOTOR CARRIER OF PROPERTY"),
        _record("222222", "Different Name", "5551110000", "MOTOR CARRIER OF PROPERTY"),
        _record("unknown", "No DOT Freight", "5559998888", "MOTOR CARRIER OF PROPERTY"),
    ]

    result = prepare_leads.process_records(records)

    # Two candidate USDOT groups (the missing-usdot record is excluded).
    assert result["unique_usdot_count"] == 2
    assert result["unique_usdot_count"] == len(
        {r["usdot_number"] for r in result["candidates"]}
    )
    assert len(result["candidates"]) == 2

    # The conflicting group is routed into review with a reason.
    conflict_review = [
        r
        for r in result["review_queue"]
        if r.get("usdot_number") == "222222"
    ]
    assert conflict_review
    assert "conflicting_company_names" in conflict_review[0]["review_reasons"]

    # The missing-usdot record is reported separately with its reason.
    missing_reviews = [r for r in result["review_queue"] if "reason" in r]
    assert missing_reviews
    assert missing_reviews[0]["reason"] == "missing_usdot_number"


def test_prepare_leads_does_not_invent_values():
    records = [clean_carrier_record()]
    result = prepare_leads.process_records(records)
    candidate = result["candidates"][0]

    assert candidate["legal_business_name"] == "Clean Movers LLC"
    assert candidate["business_telephone"] == "5551234567"

    # Since clean, no review requirements were fabricated.
    assert candidate["review_reasons"] == []
    assert candidate["review_required"] is False


# --------------------------------------------------
# Phase 4 of the pipeline: scoring
# --------------------------------------------------

def _verified(**overrides):
    verified = {
        "usdot_number": "111111",
        "lookup_status": "response_received",
        "identity_match": True,
        "allow_to_operate": "Y",
        "carrier_status_code": "A",
        "official_legal_name": "Clean Movers LLC",
        "official_phone": "5551234567",
        "vehicle_count": 12,
        "total_drivers": 15,
        "carrier_operation": {
            "code": "C",
            "description": "MOTOR CARRIER OF PROPERTY",
        },
    }
    verified.update(overrides)
    return verified


def _candidate(categories=("MOTOR CARRIER OF PROPERTY",)):
    return {
        "usdot_number": "111111",
        "legal_business_name": "Clean Movers LLC",
        "business_telephone": "5551234567",
        "business_mailing_address": "1 Main St",
        "application_categories": list(categories),
    }


def test_fully_verified_carrier_scores_max():
    lead = qualify_leads.qualify_carrier(_candidate(), _verified())

    assert lead["lead_score"] == 100
    assert lead["priority"] == "high"
    assert lead["basic_qualification_passed"] is True
    assert lead["review_required"] is False
    # No fabricated fields: the identity match is a real comparison.
    assert lead["identity_match"] is True


def test_carrier_missing_information_requires_review():
    lead = qualify_leads.qualify_carrier(
        _candidate(),
        _verified(
            identity_match=None,
            allow_to_operate=None,
            carrier_status_code=None,
            vehicle_count=None,
            official_phone=None,
        ),
    )

    assert lead["lead_score"] < 100
    assert lead["basic_qualification_passed"] is False
    assert lead["review_required"] is True
    assert "USDOT identity not confirmed" in lead["review_reasons"]


def test_mismatched_identity_is_not_eligible():
    lead = qualify_leads.qualify_carrier(
        _candidate(),
        _verified(identity_match=False),
    )
    assert lead["basic_qualification_passed"] is False


def test_passenger_and_household_goods_do_not_match_target_category():
    lead = qualify_leads.qualify_carrier(
        _candidate(categories=("MOTOR CARRIER OF PASSENGERS",)),
        _verified(),
    )
    # Category match contributes 10 points and is required to qualify.
    assert lead["lead_score"] == 90
    assert lead["basic_qualification_passed"] is False


def test_priority_bands():
    high = qualify_leads.qualify_carrier(_candidate(), _verified())
    low = qualify_leads.qualify_carrier(
        _candidate(),
        _verified(allow_to_operate=None, carrier_status_code=None),
    )
    assert high["priority"] == "high"
    assert low["priority"] != "high"
