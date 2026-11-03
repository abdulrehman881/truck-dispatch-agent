
import csv
import json
from pathlib import Path



PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "output"

CANDIDATES_FILE = OUTPUT_DIR / "phase2_carrier_candidates.json"
VERIFIED_FILE = OUTPUT_DIR / "phase3_verified_carriers.json"

QUALIFIED_JSON = OUTPUT_DIR / "qualified_leads.json"
QUALIFIED_CSV = OUTPUT_DIR / "qualified_leads.csv"
REVIEW_JSON = OUTPUT_DIR / "leads_needing_review.json"



def load_json(path):
    if not path.exists():
        raise FileNotFoundError(
            f"Missing file: {path}\n"
            "Run the previous pipeline phases first."
        )

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def normalize_usdot(value):
    if value is None:
        return ""

    value = str(value).strip()

    if value.isdigit():
        return str(int(value))

    return value


def get_address(value):
    """Convert an address string or dictionary to readable text."""
    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return ", ".join(
            str(part).strip()
            for part in value.values()
            if part not in (None, "")
        )

    return ""


def get_categories(candidate):
    categories = candidate.get("application_categories", [])

    if isinstance(categories, str):
        return [categories]

    if isinstance(categories, list):
        return [str(item) for item in categories]

    return []



def qualify_carrier(candidate, verified):
    score = 0
    reasons = []
    review_reasons = []

    categories = get_categories(candidate)

    
    category_match = any(
        "MOTOR CARRIER OF PROPERTY" in category.upper()
        and "MOTOR CARRIER OF PASSENGERS" not in category.upper()
        and "MOTOR CARRIER OF HOUSEHOLD GOODS" not in category.upper()
        for category in categories
    )


    identity_match = verified.get("identity_match") is True

    allowed = verified.get("allow_to_operate")
    status_code = verified.get("carrier_status_code")

    fleet = verified.get("vehicle_count")

    phone = (
        verified.get("official_phone")
        or candidate.get("business_telephone")
        or verified.get("source_phone")
    )

    if identity_match:
        score += 30
        reasons.append("USDOT identity confirmed")
    else:
        review_reasons.append("USDOT identity not confirmed")

    if allowed == "Y":
        score += 25
        reasons.append("FMCSA reports allowed to operate")
    elif allowed == "N":
        review_reasons.append(
            "FMCSA reports not allowed to operate"
        )
    else:
        review_reasons.append(
            "Operating permission is unknown"
        )

    if status_code == "A":
        score += 15
        reasons.append("FMCSA status code is A")
    elif status_code is None:
        review_reasons.append("Carrier status is unknown")
    else:
        review_reasons.append(
            f"Carrier status code is {status_code}"
        )

    if fleet is not None:
        try:
            fleet_number = int(fleet)

            if fleet_number >= 0:
                score += 10
                reasons.append(
                    f"Fleet size available: {fleet_number}"
                )
            else:
                review_reasons.append(
                    "Invalid fleet size"
                )
        except (TypeError, ValueError):
            review_reasons.append(
                "Fleet size is not a valid number"
            )
    else:
        review_reasons.append("Fleet size is unknown")

    if phone:
        score += 10
        reasons.append("Business phone available")
    else:
        review_reasons.append("No phone number available")

    if category_match:
        score += 10
        reasons.append(
            "Matches motor carrier of property category"
        )
    else:
        review_reasons.append(
            "Target carrier category not confirmed"
        )

    if score >= 80:
        priority = "high"
    elif score >= 60:
        priority = "medium"
    else:
        priority = "low"

    lookup_status = verified.get("lookup_status")

    review_required = (
        not identity_match
        or lookup_status != "response_received"
        or allowed != "Y"
        or status_code != "A"
        or not category_match
        or not phone
        or fleet is None
    )

    basic_qualification_passed = (
        identity_match
        and lookup_status == "response_received"
        and allowed == "Y"
        and status_code == "A"
        and category_match
        and bool(phone)
        and fleet is not None
    )

    return {
        "usdot_number": normalize_usdot(
            candidate.get("usdot_number")
        ),
        "company_name": (
            verified.get("official_legal_name")
            or candidate.get("legal_business_name")
        ),
        "phone": phone,
        "mailing_address": get_address(
            candidate.get("business_mailing_address")
        ),
        "application_categories": categories,
        "carrier_operation": verified.get(
            "carrier_operation"
        ),
        "allow_to_operate": allowed,
        "carrier_status_code": status_code,
        "vehicle_count": fleet,
        "total_drivers": verified.get("total_drivers"),
        "identity_match": identity_match,
        "lookup_status": lookup_status,
        "lead_score": score,
        "priority": priority,
        "basic_qualification_passed": (
            basic_qualification_passed
        ),
        "review_required": review_required,
        "qualification_reasons": reasons,
        "review_reasons": review_reasons,
        "fmcsa_lookup_url": verified.get(
            "fmcsa_lookup_url"
        ),
        "verification_checked_at": verified.get(
            "verification_checked_at"
        ),
    }



def main():
    candidates = load_json(CANDIDATES_FILE)
    verified_records = load_json(VERIFIED_FILE)

    verified_by_usdot = {
        normalize_usdot(item.get("usdot_number")): item
        for item in verified_records
        if item.get("usdot_number") is not None
    }

    qualified = []
    review_queue = []
    missing_verification = 0

    for candidate in candidates:
        usdot = normalize_usdot(
            candidate.get("usdot_number")
        )

        verified = verified_by_usdot.get(usdot)

        if verified is None:
            missing_verification += 1

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
                "source_phone": candidate.get(
                    "business_telephone"
                ),
                "errors": ["No Phase 3 record found"],
            }

        lead = qualify_carrier(candidate, verified)
        qualified.append(lead)

        if lead["review_required"]:
            review_queue.append(lead)

    qualified.sort(
        key=lambda item: item["lead_score"],
        reverse=True,
    )

    save_data = [
        item for item in qualified
    ]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with QUALIFIED_JSON.open("w", encoding="utf-8") as file:
        json.dump(
            save_data,
            file,
            indent=2,
            ensure_ascii=False,
        )

    with REVIEW_JSON.open("w", encoding="utf-8") as file:
        json.dump(
            review_queue,
            file,
            indent=2,
            ensure_ascii=False,
        )

    csv_fields = [
        "usdot_number",
        "company_name",
        "phone",
        "mailing_address",
        "application_categories",
        "allow_to_operate",
        "carrier_status_code",
        "vehicle_count",
        "total_drivers",
        "identity_match",
        "lookup_status",
        "lead_score",
        "priority",
        "basic_qualification_passed",
        "review_required",
        "qualification_reasons",
        "review_reasons",
        "fmcsa_lookup_url",
        "verification_checked_at",
    ]

    with QUALIFIED_CSV.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=csv_fields,
            extrasaction="ignore",
        )

        writer.writeheader()

        for lead in qualified:
            row = lead.copy()

            row["application_categories"] = "; ".join(
                row["application_categories"]
            )

            row["qualification_reasons"] = "; ".join(
                row["qualification_reasons"]
            )

            row["review_reasons"] = "; ".join(
                row["review_reasons"]
            )

            writer.writerow(row)

    print("\nPHASE 4 COMPLETE")
    print(f"Candidates: {len(candidates)}")
    print(f"Verification records: {len(verified_records)}")
    print(f"Qualified-list records: {len(qualified)}")
    print(f"Missing verification records: {missing_verification}")
    print(f"Records needing review: {len(review_queue)}")

    print("\nPriority distribution:")

    for priority in ["high", "medium", "low"]:
        count = sum(
            item["priority"] == priority
            for item in qualified
        )
        print(f"  {priority.title()}: {count}")

    print(
        "Passed basic qualification checks: "
        f"{sum(item['basic_qualification_passed'] for item in qualified)}"
    )

    print(f"\nJSON output: {QUALIFIED_JSON}")
    print(f"Excel-compatible CSV: {QUALIFIED_CSV}")
    print(f"Review queue: {REVIEW_JSON}")


if __name__ == "__main__":
    main()
