
import json
import os
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import requests



PROJECT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_DIR / ".env")
OUTPUT_DIR = PROJECT_DIR / "output"

INPUT_FILE = OUTPUT_DIR / "phase2_carrier_candidates.json"
OUTPUT_FILE = OUTPUT_DIR / "phase3_verified_carriers.json"
ERROR_FILE = OUTPUT_DIR / "phase3_errors.json"

BASE_URL = "https://mobile.fmcsa.dot.gov/qc/services"

MAX_RECORDS = None

REQUEST_DELAY_SECONDS = 0.5
TIMEOUT_SECONDS = 20



def save_json(path, data):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def load_candidates():
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Missing input file: {INPUT_FILE}\n"
            "Run prepare_leads.py first."
        )

    with INPUT_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def normalize_name(value):
    """Normalize names for a basic identity comparison."""
    if not value:
        return ""

    return " ".join(
        "".join(
            char.lower() if char.isalnum() else " "
            for char in str(value)
        ).split()
    )


def normalize_usdot(value):
    """Compare USDOT numbers without formatting differences."""
    if value is None:
        return None

    value = str(value).strip()

    if not value.isdigit():
        return value

    return str(int(value))


def fetch_endpoint(session, usdot, endpoint, api_key):
    """Request one endpoint from the official FMCSA API."""

    url = f"{BASE_URL}/carriers/{usdot}"

    if endpoint:
        url = f"{url}/{endpoint}"

    response = session.get(
        url,
        params={"webKey": api_key},
        headers={"Accept": "application/json"},
        timeout=TIMEOUT_SECONDS,
    )

    if not response.ok:
        print(f"  HTTP status: {response.status_code}")

        raise requests.HTTPError(
            f"FMCSA returned HTTP {response.status_code}",
            response=response,
        )

    return response.json()


def extract_carrier(api_response):
    """Extract the carrier object from the API response."""

    content = api_response.get("content", {})

    if isinstance(content, dict):
        carrier = content.get("carrier")

        if isinstance(carrier, dict):
            return carrier

    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                carrier = item.get("carrier")

                if isinstance(carrier, dict):
                    return carrier

                if "dotNumber" in item or "legalName" in item:
                    return item

    return {}



def verify_carrier(session, candidate, api_key):
    usdot = candidate.get("usdot_number")

    result = {
        "usdot_number": usdot,
        "source_company_name": candidate.get(
            "legal_business_name"
        ),
        "source_phone": candidate.get("business_telephone"),

        "fmcsa_lookup_url": (
            "https://safer.fmcsa.dot.gov/CompanySnapshot.aspx"
            "?searchtype=ANY&query_type=queryCarrierSnapshot"
            f"&query_param=USDOT&query_string={usdot}"
        ),

        "verification_checked_at": (
            datetime.now().astimezone().isoformat()
        ),

        "lookup_status": "pending",
        "identity_match": None,

        "official_legal_name": None,
        "official_phone": None,

        "allow_to_operate": None,
        "carrier_status_code": None,
        "carrier_operation": None,

        "out_of_service": None,
        "out_of_service_date": None,

        "vehicle_count": None,
        "total_drivers": None,

        "raw_carrier_response": None,
        "raw_basics_response": None,

        "errors": [],
    }

    try:
        carrier_response = fetch_endpoint(
            session, usdot, "", api_key
        )

        carrier = extract_carrier(carrier_response)

        carrier.pop("ein", None)

        result["raw_carrier_response"] = carrier_response

        if not carrier:
            result["lookup_status"] = "needs_manual_review"
            result["errors"].append(
                "No carrier object found in the API response."
            )
            return result

        official_dot = carrier.get("dotNumber")
        official_name = carrier.get("legalName")

        result["official_legal_name"] = official_name

        result["official_phone"] = (
            carrier.get("telephone")
            or carrier.get("phone")
        )

        if official_dot is not None:
            result["identity_match"] = (
                normalize_usdot(official_dot)
                == normalize_usdot(usdot)
            )

        result["allow_to_operate"] = carrier.get(
            "allowedToOperate"
        )

        result["carrier_status_code"] = carrier.get(
            "statusCode"
        )

        operation = carrier.get("carrierOperation")

        if isinstance(operation, dict):
            result["carrier_operation"] = {
                "code": operation.get("carrierOperationCode"),
                "description": operation.get("carrierOperationDesc"),
            }

        result["vehicle_count"] = carrier.get(
            "totalPowerUnits"
        )

        result["total_drivers"] = carrier.get(
            "totalDrivers"
        )

        result["out_of_service_date"] = carrier.get("oosDate")

        if isinstance(carrier.get("outOfService"), bool):
            result["out_of_service"] = carrier["outOfService"]

        try:
            basics_response = fetch_endpoint(
                session, usdot, "basics", api_key
            )

            result["raw_basics_response"] = basics_response


        except requests.RequestException as error:
            result["errors"].append(
                f"Basics request failed: {error}"
            )

        except (ValueError, TypeError) as error:
            result["errors"].append(
                f"Could not parse basics response: {error}"
            )

        if result["identity_match"] is False:
            result["lookup_status"] = "needs_manual_review"
            result["errors"].append(
                "The returned USDOT number does not match "
                "the requested USDOT number."
            )

        elif (
            result["identity_match"] is True
            and official_name
        ):
            result["lookup_status"] = "response_received"

        else:
            result["lookup_status"] = "needs_manual_review"
            result["errors"].append(
                "The response did not contain enough identity "
                "information to confirm the carrier."
            )

    except requests.HTTPError as error:
        result["lookup_status"] = "api_http_error"
        result["errors"].append(str(error))

    except requests.RequestException as error:
        result["lookup_status"] = "request_failed"
        result["errors"].append(
            f"{type(error).__name__}: request failed"
        )

    except (ValueError, TypeError) as error:
        result["lookup_status"] = "invalid_api_response"
        result["errors"].append(
            f"{type(error).__name__}: invalid API response"
        )

    return result



def main():
    api_key = os.getenv("FMCSA_WEB_KEY")

    if not api_key:
        raise SystemExit(
            "FMCSA_WEB_KEY is not set in this terminal.\n"
            "Set it in PowerShell before running this script."
        )

    candidates = load_candidates()

    if MAX_RECORDS is not None:
        candidates = candidates[:MAX_RECORDS]

    verified_results = []
    errors = []

    with requests.Session() as session:
        session.headers.update({
            "User-Agent": "TruckDispatchAgent/1.0"
        })

        for index, candidate in enumerate(candidates, start=1):
            usdot = candidate.get("usdot_number")

            if not usdot:
                errors.append({
                    "candidate": candidate,
                    "error": "Missing USDOT number",
                })
                continue

            print(
                f"[{index}/{len(candidates)}] "
                f"Checking USDOT {usdot}..."
            )

            result = verify_carrier(
                session, candidate, api_key
            )

            verified_results.append(result)

            print(f"  Status: {result['lookup_status']}")
            print(
                f"  Official name: "
                f"{result['official_legal_name']}"
            )
            print(
                f"  Identity match: {result['identity_match']}"
            )
            print(
                f"  Allowed to operate: "
                f"{result['allow_to_operate']}"
            )
            print(
                f"  Power units: {result['vehicle_count']}"
            )

            if result["errors"]:
                errors.append({
                    "usdot_number": usdot,
                    "errors": result["errors"],
                })

            save_json(OUTPUT_FILE, verified_results)
            save_json(ERROR_FILE, errors)

            time.sleep(REQUEST_DELAY_SECONDS)

    successful = sum(
        r["lookup_status"] == "response_received"
        for r in verified_results
    )

    manual_review = sum(
        r["lookup_status"] == "needs_manual_review"
        for r in verified_results
    )

    failures = sum(
        r["lookup_status"] in {
            "api_http_error",
            "request_failed",
            "invalid_api_response",
        }
        for r in verified_results
    )

    print("\nPHASE 3 TEST COMPLETE")
    print(f"Records checked: {len(verified_results)}")
    print(f"Identity-confirmed responses: {successful}")
    print(f"Manual review needed: {manual_review}")
    print(f"API/request failures: {failures}")
    print(f"Results: {OUTPUT_FILE}")
    print(f"Errors: {ERROR_FILE}")


if __name__ == "__main__":
    main()
