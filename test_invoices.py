import sys
import json
import os
from pathlib import Path

sys.path.insert(0, "app")
from pipeline import run

invoice_dir = "C:\\Users\\SashaBieri\\Downloads\\Mobile Devices"
output_dir = "dockling"

os.makedirs(output_dir, exist_ok=True)

invoices = sorted([f for f in os.listdir(invoice_dir) if f.lower().endswith('.pdf')])

for invoice in invoices:
    pdf_path = os.path.join(invoice_dir, invoice)
    try:
        result = run(pdf_path)
        output_file = os.path.join(output_dir, f"{Path(invoice).stem}.json")
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"OK: {invoice}")
    except Exception as e:
        print(f"FAIL: {invoice} - {e}")
