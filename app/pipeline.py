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


_MANDATORY = ["receiver", "iban", "amount", "currency"]


def run_llm(pdf_path: str, existing_fields: dict) -> dict:
    """Staged LLM extraction with vendor check between stages.

    Flow: text LLM → vendor check → skip image if complete → image LLM → vendor check.
    QR/existing fields always win over LLM output.
    """
    # ── Stage 1: text LLM ────────────────────────────────────────────────────
    text_fields = llm.extract_text_stage(pdf_path)

    # Interim: QR/existing wins over text, then vendor check
    interim = {**(text_fields or {}), **{k: v for k, v in existing_fields.items() if v}}
    interim = run_vendor_check(interim)

    # ── Stage 2: image LLM (skipped if text + vendor already complete) ────────
    needs_image = any(not interim.get(f) for f in _MANDATORY)
    image_fields = llm.extract_image_stage(pdf_path) if needs_image else None

    # Both stages failed
    if text_fields is None and image_fields is None:
        result = {**existing_fields, "status": "needs_review", "match_type": "image_only"}
        result = run_vendor_check(result)
        return result

    # ── Match type ────────────────────────────────────────────────────────────
    if text_fields and image_fields:
        match_type = "hybrid"
    elif text_fields:
        match_type = "text_full"
    else:
        match_type = "image_only"

    # ── Final merge: image < text (non-null) < QR/existing ───────────────────
    merged = {**(image_fields or {})}
    if text_fields:
        merged.update({k: v for k, v in text_fields.items() if v is not None})
    merged.update({k: v for k, v in existing_fields.items() if v})

    # Carry forward vendor-autofilled IBAN from text stage (absent in raw text_fields)
    if not merged.get("iban") and interim.get("iban"):
        merged["iban"] = interim["iban"]
        if not merged.get("bic") and interim.get("bic"):
            merged["bic"] = interim["bic"]

    merged["match_type"] = match_type
    merged["bank_target"] = db.derive_bank_target(merged.get("currency", ""))

    if merged.get("iban") and not _validate_iban(merged["iban"]):
        merged["iban"] = ""

    # Tag LLM-sourced IBAN (not from QR, not yet vendor-tagged)
    if not existing_fields.get("iban") and merged.get("iban") and not merged.get("iban_source"):
        merged["iban_source"] = "llm"

    # ── Final vendor check (image may have resolved receiver) ────────────────
    merged = run_vendor_check(merged)

    merged["status"] = "LLM-Done" if all(merged.get(f) for f in _MANDATORY) else "needs_review"
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
