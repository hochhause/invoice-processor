import re
import db
import llm
import qr_swiss


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

    return fields


def run_llm(pdf_path: str, existing_fields: dict) -> dict:
    """Run LLM extraction. Merges into existing_fields — QR fields win. Called from /api/run-llm-batch."""
    llm_fields = llm.extract_fields(pdf_path)

    if llm_fields is None:
        return {**existing_fields, "status": "needs_review"}

    # QR-extracted fields win over LLM
    merged = {**llm_fields, **{k: v for k, v in existing_fields.items() if v}}
    merged["bank_target"] = db.derive_bank_target(merged.get("currency", ""))

    if merged.get("iban") and not _validate_iban(merged["iban"]):
        merged["iban"] = ""

    mandatory = ["receiver", "iban", "amount", "currency"]
    merged["status"] = "LLM-Done" if all(merged.get(f) for f in mandatory) else "needs_review"

    return merged


def _validate_iban(iban: str) -> bool:
    """MOD-97 checksum validation."""
    iban = re.sub(r"\s", "", iban).upper()
    if len(iban) < 5:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
    return int(numeric) % 97 == 1
