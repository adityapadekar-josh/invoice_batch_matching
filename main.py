import json
import os
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import psycopg2
from psycopg2.extras import RealDictCursor
from openai import OpenAI
from dotenv import load_dotenv
import prompts

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        database=os.getenv("DB_NAME", "your_database"),
        user=os.getenv("DB_USER", "your_user"),
        password=os.getenv("DB_PASSWORD", "your_password"),
    )


def create_run_folder() -> Path:
    """Create a timestamped folder for this run."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_path = Path(f"batch_reconciliation_{timestamp}")
    folder_path.mkdir(exist_ok=True)
    return folder_path


def save_json(data: dict | list, folder: Path, filename: str):
    filepath = folder / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved: {filepath}")


def print_section_header(title: str):
    """Print a formatted section header."""
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}\n")


def extract_invoice_data(invoice_url: str, run_folder: Path) -> dict:
    print_section_header("STEP 1: EXTRACTING INVOICE DATA")
    print(f"Invoice URL: {invoice_url}")
    print("Calling OpenAI API for invoice extraction...")
    start_time = datetime.now()

    response = client.responses.create(
        model="gpt-5.2",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompts.VENDOR_INVOICE_EXTRACTION_PROMPT,
                    },
                    {"type": "input_file", "file_url": invoice_url},
                ],
            }
        ],
    )

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"Duration for EXTRACTING INVOICE DATA: {duration:.2f} seconds")

    invoice_data = json.loads(response.output_text)
    save_json(invoice_data, run_folder, "1_extracted_invoice.json")

    print(f"\nInvoice extraction completed:")
    return invoice_data


def reconcile_batch_with_invoice(
    batch_items: list, invoice: dict, run_folder: Path
) -> list:
    print_section_header("STEP 3: RECONCILING BATCH WITH INVOICE")
    print(f"Batch items count: {len(batch_items)}")
    print(f"Invoice items count: {len(invoice.get('line_items', []))}")
    print("Calling OpenAI API for reconciliation...")

    serializable_batch_items = []

    for item in batch_items:
        serializable_item = {}
        for key, value in item.items():
            if isinstance(value, Decimal):
                serializable_item[key] = float(value)
            else:
                serializable_item[key] = value
        serializable_batch_items.append(serializable_item)

    payload = {
        "batch_audit_items": serializable_batch_items,
        "invoice": invoice,
    }

    start_time = datetime.now()

    response = client.responses.create(
        model="gpt-5.2",
        input=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": prompts.VENDOR_INVOICE_BATCH_PRODUCT_RECONCILIATION_PROMPT,
                    },
                    {"type": "input_text", "text": json.dumps(payload)},
                ],
            }
        ],
    )

    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    print(f"Duration for RECONCILING BATCH WITH INVOICE: {duration:.2f} seconds")

    match_results = json.loads(response.output_text)
    save_json(match_results, run_folder, "3_reconciliation_results.json")

    # Print summary
    matched_count = sum(1 for item in match_results if item.get("matched", False))
    print(f"\nReconciliation completed:")
    print(f"  - Total matches processed: {len(match_results)}")
    print(f"  - Successfully matched: {matched_count}")
    print(f"  - Unmatched/Discrepancies: {len(match_results) - matched_count}")

    return match_results


def fetch_batch_items(batch_id: int, run_folder: Path) -> list:
    print_section_header("STEP 2: FETCHING BATCH ITEMS FROM DATABASE")
    print(f"Batch ID: {batch_id}")
    print("Querying database...")

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            query = """
                SELECT 
                    id,
                    order_id,
                    order_item_id,
                    batch_id,
                    product_id,
                    product_name,
                    order_item_quantity,
                    product_quantity,
                    product_unit
                FROM order_item_product_audits
                WHERE batch_id = %s
            """
            cursor.execute(query, (batch_id,))
            results = cursor.fetchall()
            batch_items = [dict(row) for row in results]
            save_json(batch_items, run_folder, "2_fetched_batch_items.json")

            print(f"\nDatabase query completed:")
            print(f"  - Records found: {len(batch_items)}")

            return batch_items
    finally:
        conn.close()


def process_batch_invoice(batch_id: int, invoice_url: str):
    start_time = datetime.now()

    try:
        print_section_header("BATCH INVOICE RECONCILIATION PROCESS")
        print(f"Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Batch ID: {batch_id}")
        print(f"Invoice URL: {invoice_url}")

        run_folder = create_run_folder()
        print(f"\n✓ Created run folder: {run_folder}")

        # Step 1: Extract invoice data
        invoice_data = extract_invoice_data(invoice_url, run_folder)

        # Step 2: Fetch batch items from database
        batch_items = fetch_batch_items(batch_id, run_folder)

        if not batch_items:
            print(f"\n⚠ WARNING: No records found for batch_id: {batch_id}")
            return

        # Step 3: Reconcile batch with invoice
        match_results = reconcile_batch_with_invoice(
            batch_items, invoice_data, run_folder
        )

        # Save processing metadata
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        metadata = {
            "batch_id": batch_id,
            "invoice_url": invoice_url,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "duration_seconds": duration,
            "batch_items_count": len(batch_items),
            "invoice_items_count": len(invoice_data.get("line_items", [])),
            "reconciliation_results_count": len(match_results),
            "status": "completed",
        }
        save_json(metadata, run_folder, "0_metadata.json")

        print_section_header("PROCESS COMPLETED SUCCESSFULLY")
        print(f"Duration: {duration:.2f} seconds")
        print(f"All files saved to: {run_folder}")
        print(f"\nFiles created:")
        print(f"  - 0_metadata.json")
        print(f"  - 1_extracted_invoice.json")
        print(f"  - 2_fetched_batch_items.json")
        print(f"  - 3_reconciliation_results.json")

    except Exception as exc:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print_section_header("ERROR OCCURRED")
        print(f"Error: {str(exc)}")
        print(f"Duration before error: {duration:.2f} seconds")

        # Try to save error metadata
        try:
            error_metadata = {
                "batch_id": batch_id,
                "invoice_url": invoice_url,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration_seconds": duration,
                "status": "failed",
                "error": str(exc),
            }
            if "run_folder" in locals():
                save_json(error_metadata, run_folder, "0_metadata_error.json")
        except:
            pass

        raise


if __name__ == "__main__":
    BATCH_ID = 1703
    INVOICE_URL = (
        "https://assets.thekindkart.org/invoice/251/invoice_20250411_174741.pdf"
    )

    process_batch_invoice(BATCH_ID, INVOICE_URL)
