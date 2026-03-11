import json
import os
import sys
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


class TeeToFile:
    def __init__(self, file_path):
        self.terminal = sys.stdout
        self.log_file = open(file_path, "w", encoding="utf-8")

    def write(self, message):
        self.terminal.write(message)
        self.log_file.write(message)
        self.flush()

    def flush(self):
        self.terminal.flush()
        self.log_file.flush()

    def close(self):
        self.log_file.close()


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
    folder_path = Path(f"tests_v2/batch_reconciliation_{timestamp}")
    folder_path.mkdir(exist_ok=True)
    return folder_path


def save_json(data: dict | list, folder: Path, filename: str):
    filepath = folder / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved file: {filepath}")


def print_section_header(title: str):
    """Print a formatted section header."""
    print(f"\n{'=' * 60}")
    print(f" {title}")
    print(f"{'=' * 60}\n")


def extract_invoice_data(invoice_url: str, run_folder: Path) -> dict:
    print_section_header("STEP 1: INVOICE DATA EXTRACTION")
    print(f"Invoice URL: {invoice_url}")
    print("Invoking OpenAI API for invoice extraction...")
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
    print(f"Extraction completed in {duration:.2f} seconds")

    invoice_data = json.loads(response.output_text)
    save_json(invoice_data, run_folder, "1_extracted_invoice.json")

    print("\nInvoice extraction summary:")
    print(f"  - Invoice number: {invoice_data.get('invoice_number', 'N/A')}")
    print(f"  - Line items extracted: {len(invoice_data.get('line_items', []))}")

    return invoice_data


def reconcile_batch_with_invoice(
    batch_items: list, invoice: dict, run_folder: Path
) -> list:
    print_section_header("STEP 3: BATCH–INVOICE RECONCILIATION")
    print(f"Batch items count: {len(batch_items)}")
    print(f"Invoice items count: {len(invoice.get('line_items', []))}")
    print("Invoking OpenAI API for reconciliation...")

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
    print(f"Reconciliation completed in {duration:.2f} seconds")

    match_results = json.loads(response.output_text)
    save_json(match_results, run_folder, "3_reconciliation_results.json")

    matched_count = sum(1 for item in match_results if item.get("matched", False))
    print("\nReconciliation summary:")
    print(f"  - Total records processed: {len(match_results)}")
    print(f"  - Successfully matched: {matched_count}")
    print(f"  - Unmatched or discrepant: {len(match_results) - matched_count}")

    return match_results


def fetch_batch_items(batch_id: int, run_folder: Path) -> list:
    print_section_header("STEP 2: FETCHING BATCH ITEMS FROM DATABASE")
    print(f"Batch ID: {batch_id}")
    print("Executing database query...")

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            query = """
                SELECT 
                    id,
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

            print("\nDatabase query summary:")
            print(f"  - Records retrieved: {len(batch_items)}")

            return batch_items
    finally:
        conn.close()


def process_batch_invoice(batch_id: int, invoice_url: str):
    start_time = datetime.now()
    tee = None

    try:
        run_folder = create_run_folder()
        print(f"✓ Run folder created: {run_folder}")

        log_file_path = run_folder / "process.log"
        tee = TeeToFile(log_file_path)
        sys.stdout = tee

        print_section_header("BATCH INVOICE RECONCILIATION PROCESS")
        print(f"Process started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Batch ID: {batch_id}")
        print(f"Invoice URL: {invoice_url}")

        invoice_data = extract_invoice_data(invoice_url, run_folder)
        batch_items = fetch_batch_items(batch_id, run_folder)

        if not batch_items:
            print(f"\n⚠ WARNING: No records found for batch ID {batch_id}")
            return

        match_results = reconcile_batch_with_invoice(
            batch_items, invoice_data, run_folder
        )

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
        print(f"Total duration: {duration:.2f} seconds")
        print(f"Output directory: {run_folder}")
        print("\nFiles generated:")
        print("  - 0_metadata.json")
        print("  - 1_extracted_invoice.json")
        print("  - 2_fetched_batch_items.json")
        print("  - 3_reconciliation_results.json")
        print("  - process.log")

    except Exception as exc:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print_section_header("PROCESS FAILED")
        print(f"Error message: {str(exc)}")
        print(f"Elapsed time before failure: {duration:.2f} seconds")

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

    finally:
        if tee:
            sys.stdout = tee.terminal
            tee.close()


if __name__ == "__main__":
    # BATCH_ID = 978
    # INVOICE_URL = (
    #  "https://assets.thekindkart.org/invoice/754/invoice_20250616_122802.pdf"
    # )

    # process_batch_invoice(BATCH_ID, INVOICE_URL)

    tests = [
        {"batch_id": 1703, "invoice_url": "https://assets.thekindkart.org/invoice/251/invoice_20250411_174741.pdf"},
        {"batch_id": 978,  "invoice_url": "https://assets.thekindkart.org/invoice/754/invoice_20250616_122802.pdf"},
        {"batch_id": 1601, "invoice_url": "https://assets.thekindkart.org/invoice/195/invoice_20250319_151230.pdf"},
        {"batch_id": 1990, "invoice_url": "https://assets.thekindkart.org/invoice/770/invoice_20250616_183939.pdf"},
        {"batch_id": 1982, "invoice_url": "https://assets.thekindkart.org/invoice/768/invoice_20250616_183255.pdf"},
        {"batch_id": 134,  "invoice_url": "https://assets.thekindkart.org/invoice/134/invoice_20250226_105752.pdf"},
        {"batch_id": 2642, "invoice_url": "https://assets.thekindkart.org/invoice/1764/invoice_20251029_114646.pdf"},
        {"batch_id": 2470, "invoice_url": "https://assets.thekindkart.org/invoice/1591/invoice_20250919_183642.pdf"},
        {"batch_id": 1509, "invoice_url": "https://assets.thekindkart.org/invoice/80/invoice_20250218_132612.pdf"},
        {"batch_id": 1499, "invoice_url": "https://assets.thekindkart.org/invoice/79/invoice_20250218_130412.pdf"},
        {"batch_id": 1511, "invoice_url": "https://assets.thekindkart.org/invoice/89/invoice_20250220_062000.pdf"},
        {"batch_id": 1510, "invoice_url": "https://assets.thekindkart.org/invoice/89/invoice_20250220_062000.pdf"},
        {"batch_id": 1425, "invoice_url": "https://assets.thekindkart.org/invoice/13/2024-252899.pdf"},
        {"batch_id": 1426, "invoice_url": "https://assets.thekindkart.org/invoice/13/2024-252899.pdf"},
        {"batch_id": 3112, "invoice_url": "https://assets.thekindkart.org/invoice/2180/invoice_20260116_124028.pdf"},
        {"batch_id": 2946, "invoice_url": "https://assets.thekindkart.org/invoice/2046/invoice_20251219_130113.pdf"},
        {"batch_id": 2860, "invoice_url": "https://assets.thekindkart.org/invoice/1896/invoice_20251122_171604.pdf"},
        {"batch_id": 2751, "invoice_url": "https://assets.thekindkart.org/invoice/1896/invoice_20251122_171604.pdf"},
    ]

    for test in tests:
        print("\n\n" + "#" * 80)
        print(f"Starting test for batch ID {test['batch_id']}")
        print("#" * 80 + "\n")
        process_batch_invoice(test["batch_id"], test["invoice_url"])


