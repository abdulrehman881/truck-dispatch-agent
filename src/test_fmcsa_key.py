
import os
import requests

api_key = os.getenv("FMCSA_WEB_KEY")

if not api_key:
    print("ERROR: FMCSA_WEB_KEY is missing.")
    raise SystemExit(1)

url = "https://mobile.fmcsa.dot.gov/qc/services/carriers/44110"

try:
    response = requests.get(
        url,
        params={"webKey": api_key},
        headers={
            "Accept": "application/json",
            "User-Agent": "TruckDispatchAgent/1.0",
        },
        timeout=20,
    )

    print("HTTP status:", response.status_code)
    print("Content type:", response.headers.get("Content-Type"))
    print("Response body:", response.text[:1000])

except requests.RequestException as error:
    print("Request failed:", type(error).__name__)
