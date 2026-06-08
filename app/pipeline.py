import re
import db
import llm
import qr_swiss
import vendors


def run_qr(pdf_path: str) -> dict:
    """Run QR scan on upload. Returns fields dict + status. Called synchronously — fast."""
    qr = qr_swiss.extract_from_pdf(pdf_path)
    if qr is None:
        return {"status": "LLM-Pending"}

    fields = {
        "status":     "QR-processed",
        "receiver":   qr.get("receiver", ""),
        "iban":       qr.get("iban", ""),
        "bic":        "",
        "amount":     qr.get("amount", ""),
        "currency":   qr.get("currency", ""),
        "reference":  qr.get("reference", ""),
        "invoice_id": "",
        "due_date":   "",
        "bank_target": db.derive_bank_target(qr.get("currency", "")),
    }

    if fields["iban"] and not _validate_iban(fields["iban"]):
        fields["iban"] = ""
        fields["status"] = "needs_review"

    fields = run_vendor_check(fields)
    return fields


def run_llm(pdf_path: str, existing_fields: dict) -> dict:
    """Run LLM extraction. Merges into existing_fields — QR fields win. Called from /api/run-llm-batch."""
    llm_fields = llm.extract_fields(pdf_path)

    if llm_fields is None:
        result = {**existing_fields, "status": "needs_review"}
        result = run_vendor_check(result)
        return result

    # QR-extracted fields win over LLM
    merged = {**llm_fields, **{k: v for k, v in existing_fields.items() if v}}
    merged["bank_target"] = db.derive_bank_target(merged.get("currency", ""))

    if merged.get("iban") and not _validate_iban(merged["iban"]):
        merged["iban"] = ""

    # Tag IBAN source before vendor check: LLM-provided if QR didn't have it
    if not existing_fields.get("iban") and merged.get("iban"):
        merged["iban_source"] = "llm"

    mandatory = ["receiver", "iban", "amount", "currency"]
    merged["status"] = "LLM-Done" if all(merged.get(f) for f in mandatory) else "needs_review"

    merged = run_vendor_check(merged)
    return merged


def run_vendor_check(fields: dict) -> dict:
    """Post-extraction vendor IBAN verify / autofill. Mutates iban_source; never changes status."""
    vendor = vendors.lookup(fields.get("receiver", ""))
    if not vendor:
        return fields

    extracted_iban = fields.get("iban", "")
    if extracted_iban:
        if extracted_iban == vendor["iban"]:
            fields["iban_source"] = "document"
        else:
            fields["iban_source"] = "document_mismatch"
            fields["iban_mismatch_db"] = vendor["iban"]
    else:
        fields["iban"] = vendor["iban"]
        fields["bic"] = vendor["bic"] or fields.get("bic", "")
        fields["iban_source"] = "database"

    return fields


def _validate_iban(iban: str) -> bool:
    """MOD-97 checksum validation."""
    iban = re.sub(r"\s", "", iban).upper()
    if len(iban) < 5:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    return int(numeric) % 97 == 1
