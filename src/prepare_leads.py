
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


# --------------------------------------------------
# 1. PROJECT PATHS
# --------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "output"

INPUT_FILE = OUTPUT_DIR / "extracted_leads.json"

CANDIDATES_FILE = OUTPUT_DIR / "phase2_carrier_candidates.json"
REVIEW_FILE = OUTPUT_DIR / "phase2_review_queue.json"
SUMMARY_FILE = OUTPUT_DIR / "phase2_summary.json"


# --------------------------------------------------
# 2. HELPER FUNCTIONS
# --------------------------------------------------

def clean_text(value):
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_phone(phone):
    """Normalize digits for comparison, not phone verification."""
    return re.sub(r"\D", "", clean_text(phone))


def normalize_name(name):
    """Normalize a company name for comparison."""
    return re.sub(
        r"[^A-Z0-9]",
        "",
        clean_text(name).upper(),
    )


def classify_category(category):
    """
    Classify the application category.

    This identifies likely carrier applications, not proof
    that the company is currently operating.
    """
    category = clean_text(category).upper()

    if not category:
        return "unknown"

    if "BROKER" in category:
        return "broker"

    if "FREIGHT FORWARDER" in category:
        return "freight_forwarder"

    if "MOTOR CARRIER" in category:
        return "motor_carrier"

    return "unknown"


def unique_values(records, field, normalizer=clean_text):
    """Return distinct non-empty values while preserving their text."""
    found = {}

    for record in records:
        value = clean_text(record.get(field))

        if value:
            key = normalizer(value)
            if key and key not in found:
                found[key] = value

    return list(found.values())


# --------------------------------------------------
# 3. LOAD PIPELINE 1 OUTPUT
# --------------------------------------------------

def load_records():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file not found: {INPUT_FILE}\n"
            "Run extract_pdf.py first."
        )

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError(
            "extracted_leads.json must contain a JSON list."
        )

    return records


# --------------------------------------------------
# 4. GROUP RECORDS BY USDOT
# --------------------------------------------------

def group_by_usdot(records):
    """
    Keep all original records grouped by USDOT.
    Do not silently delete duplicate source rows.
    """
    groups = defaultdict(list)
    missing_usdot = []

    for record in records:
        usdot = re.sub(
            r"\D",
            "",
            clean_text(record.get("usdot_number")),
        )

        if not usdot:
            missing_usdot.append({
                "reason": "missing_usdot_number",
                "record": record,
            })
            continue

        # Use the normalized identifier as the group key.
        groups[usdot].append(record)

    return groups, missing_usdot


# --------------------------------------------------
# 5. PREPARE CANDIDATES AND REVIEW QUEUE
# --------------------------------------------------

def process_records(records):
    groups, missing_usdot = group_by_usdot(records)

    candidates = []
    review_queue = []

    category_counts = Counter()
    duplicate_groups = 0
    conflicting_groups = 0

    for usdot, source_records in sorted(groups.items()):
        categories = [
            classify_category(record.get("application_category"))
            for record in source_records
        ]

        category_counts.update(categories)

        has_carrier = "motor_carrier" in categories
        has_broker = "broker" in categories
        has_forwarder = "freight_forwarder" in categories
        has_unknown = "unknown" in categories

        names = unique_values(
            source_records,
            "legal_business_name",
            normalize_name,
        )

        phones = unique_values(
            source_records,
            "business_telephone",
            normalize_phone,
        )

        addresses = unique_values(
            source_records,
            "business_mailing_address",
            lambda value: re.sub(
                r"[^A-Z0-9]",
                "",
                value.upper(),
            ),
        )

        duplicate = len(source_records) > 1

        # Different names, phones, or addresses need review.
        name_conflict = len(names) > 1
        phone_conflict = len(phones) > 1
        address_conflict = len(addresses) > 1

        mixed_categories = (
            has_carrier
            and (has_broker or has_forwarder)
        )

        reasons = []

        if duplicate:
            duplicate_groups += 1

        if name_conflict:
            reasons.append("conflicting_company_names")

        if phone_conflict:
            reasons.append("conflicting_phone_numbers")

        if address_conflict:
            reasons.append("conflicting_addresses")

        if mixed_categories:
            reasons.append("mixed_application_categories")

        if has_unknown:
            reasons.append("unknown_or_missing_category")

        if duplicate and (
            name_conflict
            or phone_conflict
            or address_conflict
            or mixed_categories
        ):
            conflicting_groups += 1

        # Store all source rows for traceability.
        candidate = {
            "usdot_number": usdot,
            "legal_business_name": names[0] if names else None,
            "business_telephone": phones[0] if len(phones) == 1 else None,
            "business_mailing_address": (
                addresses[0] if len(addresses) == 1 else None
            ),
            "application_categories": sorted(
                set(
                    clean_text(record.get("application_category"))
                    for record in source_records
                    if clean_text(record.get("application_category"))
                )
            ),
            "source_record_count": len(source_records),
            "source_records": source_records,
            "fleet_size": None,
            "fleet_size_verified": False,
            "carrier_status_verified": False,
            "review_required": bool(reasons),
            "review_reasons": reasons,
        }

        # A motor-carrier category makes this a candidate.
        # It does not prove active authority or fleet size.
        if has_carrier:
            candidates.append(candidate)

        # Route uncertain or conflicting groups to review.
        if reasons:
            review_queue.append({
                "usdot_number": usdot,
                "legal_business_names": names,
                "phone_numbers": phones,
                "addresses": addresses,
                "categories": candidate["application_categories"],
                "source_record_count": len(source_records),
                "review_reasons": reasons,
                "source_records": source_records,
            })

    for item in missing_usdot:
        review_queue.append(item)

    return {
        "candidates": candidates,
        "review_queue": review_queue,
        "category_counts": dict(category_counts),
        "duplicate_groups": duplicate_groups,
        "conflicting_groups": conflicting_groups,
        "missing_usdot_count": len(missing_usdot),
        "unique_usdot_count": len(groups),
    }


# --------------------------------------------------
# 6. SAVE OUTPUT FILES
# --------------------------------------------------

def save_json(path, data):
    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )


# --------------------------------------------------
# 7. MAIN
# --------------------------------------------------

def main():
    print("Starting Phase 2: Lead filtering and review...\n")

    records = load_records()
    result = process_records(records)

    candidates = result["candidates"]
    review_queue = result["review_queue"]

    save_json(CANDIDATES_FILE, candidates)
    save_json(REVIEW_FILE, review_queue)

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "input_file": INPUT_FILE.name,
        "input_record_count": len(records),
        "unique_usdot_count": result["unique_usdot_count"],
        "carrier_candidate_count": len(candidates),
        "review_queue_count": len(review_queue),
        "duplicate_usdot_groups": result["duplicate_groups"],
        "conflicting_duplicate_groups": result["conflicting_groups"],
        "records_missing_usdot": result["missing_usdot_count"],
        "category_classification_counts": result["category_counts"],
        "fleet_size_verified_count": sum(
            candidate["fleet_size_verified"]
            for candidate in candidates
        ),
        "note": (
            "Carrier candidates are based on application category only. "
            "Operating status, authority, contact details, and fleet size "
            "must be verified separately."
        ),
    }

    save_json(SUMMARY_FILE, summary)

    print("=" * 48)
    print("PHASE 2 COMPLETE")
    print("=" * 48)
    print(f"Input records: {len(records)}")
    print(f"Unique USDOT numbers: {result['unique_usdot_count']}")
    print(f"Carrier candidates: {len(candidates)}")
    print(f"Review queue records: {len(review_queue)}")
    print(f"Duplicate USDOT groups: {result['duplicate_groups']}")
    print(
        "Conflicting duplicate groups: "
        f"{result['conflicting_groups']}"
    )
    print(f"Missing USDOT numbers: {result['missing_usdot_count']}")

    print("\nFiles saved:")
    print(f"  {CANDIDATES_FILE}")
    print(f"  {REVIEW_FILE}")
    print(f"  {SUMMARY_FILE}")

    print("\nFirst 3 carrier candidates:")
    for candidate in candidates[:3]:
        print(
            json.dumps(
                {
                    "usdot_number": candidate["usdot_number"],
                    "legal_business_name": candidate["legal_business_name"],
                    "business_telephone": candidate["business_telephone"],
                    "application_categories": candidate[
                        "application_categories"
                    ],
                    "source_record_count": candidate["source_record_count"],
                    "review_required": candidate["review_required"],
                },
                indent=2,
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
