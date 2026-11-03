
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pymupdf



PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
OUTPUT_DIR = PROJECT_DIR / "output"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)



def clean_text(value):
    """Convert extracted values to clean, single-line text."""
    if value is None:
        return ""

    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_usdot(value):
    """Keep only digits in a USDOT identifier."""
    digits = re.sub(r"\D", "", clean_text(value))
    return digits or None


def valid_phone_format(phone):
    """
    Check whether a phone number has a plausible US format.
    This does not verify that the number is active or correct.
    """
    digits = re.sub(r"\D", "", clean_text(phone))

    if len(digits) == 10:
        return True

    if len(digits) == 11 and digits.startswith("1"):
        return True

    return False


def is_header(row):
    """Identify repeated table header rows."""
    if not row or len(row) < 2:
        return False

    first = clean_text(row[0]).upper()
    second = clean_text(row[1]).upper()

    return (
        "USDOT NUMBER" in first
        and "LEGAL BUSINESS NAME" in second
    )



def normalize_heading(text):
    """Normalize a possible category heading."""
    text = clean_text(text).upper()
    return re.sub(r"\s+", " ", text)


def is_category_heading(text):
    """
    Recognize likely FMCSA application-category headings.

    Ordinary uppercase company names are not enough to qualify.
    The text must start with a known category phrase.
    """
    text = normalize_heading(text)

    if not text or len(text) > 180:
        return False

    known_prefixes = (
        "BROKER OF ",
        "BROKER FOR ",
        "MOTOR CARRIER OF ",
        "MOTOR CARRIER FOR ",
        "ENTERPRISE MOTOR CARRIER OF ",
        "ENTERPRISE MOTOR CARRIER FOR ",
        "FREIGHT FORWARDER OF ",
        "FREIGHT FORWARDER FOR ",
        "HOUSEHOLD GOODS MOTOR CARRIER",
        "HOUSEHOLD GOODS BROKER",
        "HOUSEHOLD GOODS FREIGHT FORWARDER",
    )

    return text.startswith(known_prefixes)



def update_category_from_blocks(blocks, table_top, current_category):
    """
    Find recognized category headings above the current table.

    The previous category is retained if the page contains no
    new category heading, including when a category continues
    across a page break.
    """
    headings = []

    for block in blocks:
        if len(block) < 5:
            continue

        x0, y0, x1, y1, text = block[:5]

        if y1 > table_top:
            continue

        text = normalize_heading(text)

        if is_category_heading(text):
            headings.append((y0, x0, text))

    headings.sort(key=lambda item: (item[0], item[1]))

    if headings:
        current_category = headings[-1][2]

    return current_category



def extract_pdf(pdf_path):
    records = []
    current_category = None

    with pymupdf.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf):
            page_number = page_index + 1

            blocks = sorted(
                page.get_text("blocks"),
                key=lambda block: (block[1], block[0]),
            )

            tables = page.find_tables()

            sorted_tables = sorted(
                tables.tables,
                key=lambda table: table.bbox[1],
            )

            for table in sorted_tables:
                rows = table.extract()

                if not rows:
                    continue

                if not is_header(rows[0]):
                    continue

                current_category = update_category_from_blocks(
                    blocks,
                    table.bbox[1],
                    current_category,
                )

                for row in rows[1:]:
                    if not row or is_header(row):
                        continue

                    if len(row) < 6:
                        continue

                    usdot_number = normalize_usdot(row[0])
                    company_name = clean_text(row[1])

                    if not usdot_number or not company_name:
                        continue

                    filing_date = clean_text(row[2])
                    address = clean_text(row[3])
                    officer = clean_text(row[4])
                    phone = clean_text(row[5])

                    records.append({
                        "usdot_number": usdot_number,
                        "legal_business_name": company_name,
                        "filing_date": filing_date,
                        "business_mailing_address": address,
                        "company_officer": officer,
                        "business_telephone": phone,
                        "phone_format_valid": valid_phone_format(phone),
                        "application_category": current_category,
                        "page_number": page_number,
                        "source_pdf": pdf_path.name,
                        "extracted_at": datetime.now().astimezone().isoformat(),
                    })

    return records



def create_summary(records, processed_files, errors):
    """Create useful quality checks for the extracted data."""

    usdot_counts = Counter(
        record["usdot_number"] for record in records
    )

    duplicate_usdot_numbers = {
        usdot: count
        for usdot, count in usdot_counts.items()
        if count > 1
    }

    category_counts = Counter(
        record["application_category"] or "UNKNOWN"
        for record in records
    )

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "pdf_files_found": len(processed_files) + len(errors),
        "pdf_files_processed": len(processed_files),
        "pdf_files_failed": len(errors),
        "processed_files": processed_files,
        "errors": errors,
        "total_records": len(records),
        "unique_usdot_numbers": len(usdot_counts),
        "duplicate_usdot_numbers": len(duplicate_usdot_numbers),
        "duplicate_usdot_details": duplicate_usdot_numbers,
        "records_missing_phone": sum(
            not record["business_telephone"] for record in records
        ),
        "records_with_invalid_phone_format": sum(
            not record["phone_format_valid"] for record in records
        ),
        "records_missing_category": sum(
            not record["application_category"] for record in records
        ),
        "records_missing_address": sum(
            not record["business_mailing_address"] for record in records
        ),
        "records_missing_officer": sum(
            not record["company_officer"] for record in records
        ),
        "records_by_category": dict(category_counts),
    }



def main():
    pdf_files = sorted(DATA_DIR.glob("*.pdf"))

    if not pdf_files:
        print(f"No PDF files found in: {DATA_DIR}")
        print("Place your FMCSA PDF inside the data folder.")
        return

    all_records = []
    processed_files = []
    errors = []

    print(f"Found {len(pdf_files)} PDF file(s).")
    print("Starting PDF extraction...\n")

    for pdf_path in pdf_files:
        print(f"Processing: {pdf_path.name}")

        try:
            records = extract_pdf(pdf_path)
            all_records.extend(records)
            processed_files.append(pdf_path.name)

            print(f"  Extracted records: {len(records)}")

        except Exception as error:
            errors.append({
                "file": pdf_path.name,
                "error": str(error),
            })

            print(f"  ERROR: {error}")

    records_path = OUTPUT_DIR / "extracted_leads.json"

    with records_path.open("w", encoding="utf-8") as file:
        json.dump(
            all_records,
            file,
            indent=2,
            ensure_ascii=False,
        )

    summary = create_summary(
        all_records,
        processed_files,
        errors,
    )

    summary_path = OUTPUT_DIR / "extraction_summary.json"

    with summary_path.open("w", encoding="utf-8") as file:
        json.dump(
            summary,
            file,
            indent=2,
            ensure_ascii=False,
        )

    print("\n" + "=" * 50)
    print("EXTRACTION COMPLETE")
    print("=" * 50)
    print(f"PDF files processed: {summary['pdf_files_processed']}")
    print(f"PDF files failed: {summary['pdf_files_failed']}")
    print(f"Total records: {summary['total_records']}")
    print(f"Unique USDOT numbers: {summary['unique_usdot_numbers']}")
    print(f"Duplicate USDOT numbers: {summary['duplicate_usdot_numbers']}")
    print(f"Missing categories: {summary['records_missing_category']}")
    print(f"Missing phone numbers: {summary['records_missing_phone']}")
    print(
        "Invalid phone formats: "
        f"{summary['records_with_invalid_phone_format']}"
    )

    print("\nRecords by category:")
    for category, count in sorted(
        summary["records_by_category"].items()
    ):
        print(f"  {category}: {count}")

    print(f"\nRecords saved to: {records_path}")
    print(f"Summary saved to: {summary_path}")

    print("\nFirst 3 extracted records:")
    for record in all_records[:3]:
        print(json.dumps(record, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
