import os
import argparse
import re
from pathlib import Path

# Add project root to path so we can import from stock_screener_filter
import sys
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from stock_screener_filter import document_store

def parse_filename(filename: str):
    """
    Parses filenames like:
    FY2025_Q4_2025-05-01.pdf
    FY2025_2025-05-01.pdf
    Returns (document_year, document_quarter)
    """
    match = re.match(r"(FY\d{4})_(Q[1-4])_.*\.pdf", filename, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2).upper()
    
    match = re.match(r"(FY\d{4})_.*\.pdf", filename, re.IGNORECASE)
    if match:
        return match.group(1), "FY"
        
    return "", ""

def main():
    parser = argparse.ArgumentParser(description="Bulk upload downloaded reports and concalls to the AI Document Store.")
    parser.add_argument("symbol", nargs="?", help="Specific stock symbol to upload (e.g., GPPL). If not provided, uploads everything.")
    parser.add_argument("--type", choices=["report", "concall", "all"], default="all", help="Type of document to upload")
    args = parser.parse_args()

    reports_dir = PROJECT_ROOT / "data" / "reports"
    if not reports_dir.exists():
        print(f"Error: {reports_dir} does not exist. Please download reports first.")
        sys.exit(1)

    uploaded = 0
    duplicates = 0
    failed = 0

    print("=== Bulk Document Uploader ===")
    
    # Iterate through data/reports/{symbol}/{type}/...
    for symbol_dir in reports_dir.iterdir():
        if not symbol_dir.is_dir():
            continue
            
        stock_id = symbol_dir.name
        if args.symbol and stock_id.upper() != args.symbol.upper():
            continue

        for type_dir in symbol_dir.iterdir():
            if not type_dir.is_dir():
                continue
                
            doc_type = type_dir.name.lower() # "report" or "concall"
            if args.type != "all" and doc_type != args.type:
                continue
                
            if doc_type not in ["report", "concall"]:
                continue

            for file_path in type_dir.glob("*.pdf"):
                filename = file_path.name
                doc_year, doc_quarter = parse_filename(filename)
                
                try:
                    with file_path.open("rb") as f:
                        result = document_store.save_upload(
                            stock_id=stock_id.upper(),
                            company_name=stock_id.upper(),  # We use ticker as company name if we don't have the full name
                            filename=filename,
                            source=f,
                            document_type=doc_type,
                            document_year=doc_year,
                            document_quarter=doc_quarter
                        )
                    
                    if result.duplicate:
                        print(f"  [Skipped] {stock_id} | {doc_type.upper()} | {filename} (Already in database)")
                        duplicates += 1
                    else:
                        print(f"  [Success] {stock_id} | {doc_type.upper()} | {filename} -> {result.chunk_count} chunks embedded.")
                        uploaded += 1
                except Exception as e:
                    print(f"  [Failed]  {stock_id} | {doc_type.upper()} | {filename} - {str(e)}")
                    failed += 1

    print("-" * 30)
    print("Upload Complete!")
    print(f"Successfully processed: {uploaded} files")
    print(f"Skipped duplicates  : {duplicates} files")
    print(f"Failed to process   : {failed} files")

if __name__ == "__main__":
    main()
