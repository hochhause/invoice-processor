"""
extract.py — Regex extraction of billing fields from markdown text.
Ported from C:\ClaudeTools\markitdown\extract_invoices.py.
No LLM involved — pure pattern matching.

Bank-specific rule sets: import from rules/<CSV_RULE_SET>.py if present,
otherwise use defaults defined here.

Field confidence levels:
  SUCCESSFUL — value found and passes sanity checks
  SUSPICIOUS — value found but looks wrong (image artifact, too short, too small, etc.)
  EMPTY      — value not found at all

QR-filled fields always become SUCCESSFUL (overridden in pipeline.py).
"""
import calendar
import os
import re
from datetime import datetime

# Field confidence levels
FIELD_STATUS_SUCCESSFUL = "SUCCESSFUL"
FIELD_STATUS_SUSPICIOUS = "SUSPICIOUS"
FIELD_STATUS_EMPTY = "EMPTY"

# Fields whose absence/issues block payment → needs_review=YES
ERROR_FLAGS = frozenset({
    "amount_not_found", "currency_not_found", "receiver_not_found",
    "due_date_not_found", "no_payment_method", "reference_not_found",
    "iban_invalid_checksum", "qr_scan_failed",
    "invoice_id_suspicious", "receiver_suspicious", "iban_suspicious",
})
# Fields whose absence is worth noting but not blocking
WARN_FLAGS = frozenset({
    "invoice_id_not_found", "iban_invalid_checksum_dev_skipped",
    "iban_missing_bic", "qr_iban_missing", "due_date_defaulted",
})

# invoice_id intentionally excluded — warning only, not blocking
MANDATORY_BASE = ["amount", "currency", "receiver", "due_date"]
MANDATORY_PROD = ["amount", "currency", "receiver", "due_date", "reference"]


def _first_match(patterns, text):
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""


def _clean_amount(s):
    s = s.strip()
    if re.search(r",\d{2}$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    return re.sub(r"[^\d.]", "", s)


def _clean_iban(raw):
    # Remove all spaces first
    raw = re.sub(r"\s", "", raw)
    # Match valid IBAN: CC + 2 check digits + 11-30 alphanumeric (total 15-34 chars, typically 22)
    m = re.match(r"([A-Z]{2}\d{2}[A-Z0-9]{11,30})", raw)
    if not m:
        return ""
    iban = m.group(1)
    return iban if 15 <= len(iban) <= 34 else ""


def _clean_bic(raw):
    """Remove spaces from BIC code. E.g. 'FUIB UA 2X' → 'FRIBUAA2X'"""
    return re.sub(r"\s", "", raw.upper()) if raw else ""


def _validate_iban_checksum(iban: str) -> bool:
    """MOD-97 IBAN checksum validation (ISO 13616). No external deps."""
    if len(iban) < 5:
        return False
    rearranged = iban[4:] + iban[:4]
    numeric = re.sub(r"[A-Z]", lambda m: str(ord(m.group()) - 55), rearranged)
    try:
        return int(numeric) % 97 == 1
    except ValueError:
        return False


def _iban_suspicious_letters(iban: str) -> bool:
    """True if IBAN body (after 2-char country code) has >4 alpha chars.
    Swiss IBANs have 0, UK IBANs have 4 (bank code). More than 4 → likely an OCR artifact."""
    stripped = re.sub(r"\s", "", iban)
    if len(stripped) < 2:
        return True
    body_letters = sum(1 for c in stripped[2:] if c.isalpha())
    return body_letters > 4


def _extract_dates(text):
    """Extract all dates found in text (YYYY-MM-DD, DD.MM.YYYY, DD/MM/YYYY formats)."""
    from datetime import datetime, timedelta
    dates = []
    patterns = [
        r"(\d{4}-\d{2}-\d{2})",
        r"(\d{1,2}[.\\/]\d{1,2}[.\\/]\d{4})",
    ]
    for p in patterns:
        for m in re.finditer(p, text):
            try:
                date_str = m.group(1)
                if "-" in date_str and len(date_str) == 10:
                    dates.append(datetime.strptime(date_str, "%Y-%m-%d"))
                elif "/" in date_str:
                    parts = date_str.split("/")
                    dates.append(datetime.strptime(f"{parts[2]}-{parts[1]}-{parts[0]}", "%Y-%m-%d"))
                elif "." in date_str:
                    parts = date_str.split(".")
                    dates.append(datetime.strptime(f"{parts[2]}-{parts[1]}-{parts[0]}", "%Y-%m-%d"))
            except (ValueError, IndexError):
                pass
    return dates


_DE_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}


def _parse_german_month_date(s: str) -> str:
    """Parse '5. Oktober 2025' or '5.Oktober 2025' → '05.10.2025', or '' on failure."""
    m = re.match(r"(\d{1,2})\.?\s*([A-Za-zä]+)\s+(\d{4})", s.strip())
    if not m:
        return ""
    month = _DE_MONTHS.get(m.group(2).lower())
    if not month:
        return ""
    try:
        return datetime(int(m.group(3)), month, int(m.group(1))).strftime("%d.%m.%Y")
    except ValueError:
        return ""


def _to_display_date(date_str: str) -> str:
    """Normalize any date string to dd.mm.yyyy (incl. German month names)."""
    if not date_str:
        return date_str
    german = _parse_german_month_date(date_str)
    if german:
        return german
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%d.%m.%Y")
        except ValueError:
            pass
    return date_str


def _end_of_current_month() -> str:
    today = datetime.now()
    last = calendar.monthrange(today.year, today.month)[1]
    return datetime(today.year, today.month, last).strftime("%d.%m.%Y")


def _generate_due_date(text, existing_due_date) -> tuple[str, bool]:
    """Return (normalised_date, defaulted). defaulted=True when no date found."""
    if existing_due_date:
        return _to_display_date(existing_due_date), False
    return _end_of_current_month(), True


# ── Default patterns (override per bank via rules/<name>.py) ─────────────────

DEFAULT_PATTERNS = {
    "invoice_id": [
        r"INVOICE\s+NUMBER\s*\|\s*([\w\-]+)",
        r"Invoice\s+number[:\s#]*([A-Z0-9\-]+)",
        r"invoice\s*(?:no|#)[:\s#]*([A-Z0-9\-]+)",
        r"Rechnungsnummer[:\s#]*([A-Z0-9\-\.]+)",
        r"Rechnungs-?Nr\.?[:\s#]*([A-Z0-9\-\.]+)",
        r"Auftrag[:\s]+([A-Z0-9\-]+)",
        r"No\.?\s*([A-Z0-9\-]+)",
        r"invoice.{0,5}no\.?\s+([A-Z0-9\-]+)",
    ],
    "amount": [
        r"\|\s*Total\s*\|\s*([\d,\.]+)\s*\|",
        r"\|\s*\|\s*Total\s*([\d,\.]+)\s*\|",
        r"\bTotal\b\s+([\d,\.]+)\s",
        r"\bTotal\b[^\d\n]*([\d,\.]+)",
        # Betrag pattern (German) with optional EUR/CHF label above/below
        r"(?:Währung|Currency)?\s*(?:EUR|CHF|USD)?\s*Betrag[:\s]+([\d\s,\.]+)",
        r"Betrag[:\s]+([\d\s,\.]+)",
        # Amount after EUR/CHF label on same or next line
        r"(?:EUR|CHF|USD)\s*[\n\s]+([\d\s,\.]+)",
        # Standalone currency followed by amount
        r"\b(?:EUR|CHF|USD|GBP)\b[^\d\n]*([\d\s,\.]+)",
    ],
    "receiver": [
        r"Beneficiary\s+Name[:\s]+(.+)",
        r"BILL\s+FROM\s*\|\s*BILL\s+TO[^\n]*\n\|[-\s\|]+\n\|\s*([^\|]+?)\s*\|",
    ],
    "iban": [
        r"IBAN\s*(?:Number)?[:\s]*([A-Z]{2}\d{2}[A-Z0-9\s]{11,})",
        r"IBAN[:\s]*([A-Z]{2}\d{2}[A-Z0-9\s]{11,})",
        r"\b([A-Z]{2}\d{2}(?:[A-Z0-9\s]){11,30}?)(?:[a-z]|$|\s[A-Z]{2,})",
    ],
    "bic": [
        r"(?:Swift\s*Code|BIC|SWIFT)[:\s]+([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
        r"(?:Swift\s*Code|BIC|SWIFT)[:\s]+([A-Z]{4,6}\s*[A-Z0-9]{2,4}\s*[A-Z0-9]{0,3})",
    ],
    # LEGACY: Bankgiro/Plusgiro are Swedish-only payment methods, no longer extracted.
    # "bankgiro": [r"Bankgiro[:\s]*([\d\-]+)"],
    # "plusgiro": [r"Plusgiro[:\s]*([\d\-]+)"],
    "due_date": [
        r"\|\s*[Zz]ahlbar\s+bis\s*:?\s*\|\s*([\d]{1,2}\.[\d]{1,2}\.[\d]{4})\s*\|",
        r"\|\s*[Zz]ahlbar\s+bis\s*:?\s*\|\s*(\d{1,2}\.\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})",
        r"[Zz]ahlbar\s+bis\s*:?\s*(\d{1,2}\.\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})",
        r"bis\s+zum\s+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"bis\s+zum\s+(\d{1,2}\.\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})",
        r"\|\s*F[äa]lligkeitsdatum\s*\|\s*([\d]{1,2}[\/\.][\d]{1,2}[\/\.][\d]{4})",
        r"DUE\s+DATE\s*\|?\s*([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Due\s+Date[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Payment\s+Due[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Payable\s+by[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"[Zz]ahlbar\s+bis[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"F[äa]llig(?:keitsdatum)?[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Zahlungsfrist[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Valuta[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Due\s+Date[:\s]+(\d{4}-\d{2}-\d{2})",
        r"[Zz]ahlbar\s+bis[:\s]+(\d{4}-\d{2}-\d{2})",
    ],
    "reference": [
        r"OCR-?nr[:\s]*([0-9\s]+)",
        r"Beneficiary\s+Account\s+Number[:\s]+([0-9]+)",
        r"ORDER\s+NUMBER\s*\|\s*\|\s*([\w\-]+)",
        r"order\s*(?:number|no)[:\s#]*([A-Z0-9\-]+)",
        r"## Referenz\s*\n+\s*([0-9\s]+)",
        r"Referenz[:\s]+([0-9]{2}\s+[0-9]{5}\s+[0-9\s]+)",
    ],
}

CURRENCY_PATTERN = r"\b(CHF|EUR|USD|GBP|SEK|NOK|DKK|CNY|RMB|JPY|AUD|CAD)\b"
CURRENCY_SYMBOL_MAP = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "¢": "USD",
    "₹": "INR",
    "₽": "RUB",
}


def _load_rule_set() -> dict:
    """Load bank-specific patterns from rules/<CSV_RULE_SET>.py if present."""
    rule_set = os.environ.get("CSV_RULE_SET", "default")
    rules_dir = os.path.join(os.path.dirname(__file__), "rules")
    path = os.path.join(rules_dir, f"{rule_set}.py")
    if rule_set == "default" or not os.path.exists(path):
        return DEFAULT_PATTERNS
    import importlib.util
    spec = importlib.util.spec_from_file_location("rules", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    patterns = dict(DEFAULT_PATTERNS)
    if hasattr(mod, "PATTERNS"):
        patterns.update(mod.PATTERNS)
    return patterns


def extract_fields(md: str, filename: str, skip_fields: set = None, check_whitelist: bool = True) -> dict:
    if skip_fields is None:
        skip_fields = set()
    patterns = _load_rule_set()
    flags = []
    statuses = {}  # field → SUCCESSFUL | SUSPICIOUS | EMPTY

    # Import whitelist check (lazy import to avoid circular dep)
    is_whitelisted = None
    if check_whitelist:
        try:
            from db import is_whitelisted as check_wl
            is_whitelisted = check_wl
        except ImportError:
            pass

    # ── invoice_id ────────────────────────────────────────────────────────────
    invoice_id = _first_match(patterns["invoice_id"], md) if "invoice_id" not in skip_fields else ""
    if not invoice_id:
        statuses["invoice_id"] = FIELD_STATUS_EMPTY
    elif len(invoice_id) < 6:
        statuses["invoice_id"] = FIELD_STATUS_SUSPICIOUS
        flags.append("invoice_id_suspicious")
    elif sum(1 for c in invoice_id if not c.isalnum() and c not in "-./") / len(invoice_id) > 0.5:
        statuses["invoice_id"] = FIELD_STATUS_SUSPICIOUS
        flags.append("invoice_id_suspicious")
    else:
        statuses["invoice_id"] = FIELD_STATUS_SUCCESSFUL

    # ── amount ────────────────────────────────────────────────────────────────
    amount = ""
    if "amount" not in skip_fields:
        amount_raw = _first_match(patterns["amount"], md)
        amount = _clean_amount(amount_raw) if amount_raw else ""
    if not amount:
        statuses["amount"] = FIELD_STATUS_EMPTY
    else:
        try:
            amt_f = float(amount)
            if amt_f == 0.0:
                amount = ""
                statuses["amount"] = FIELD_STATUS_EMPTY
            elif amt_f < 100.0:
                statuses["amount"] = FIELD_STATUS_SUSPICIOUS
            else:
                statuses["amount"] = FIELD_STATUS_SUCCESSFUL
        except ValueError:
            statuses["amount"] = FIELD_STATUS_SUSPICIOUS

    # ── currency ──────────────────────────────────────────────────────────────
    currency = ""
    if "currency" not in skip_fields:
        m = re.search(CURRENCY_PATTERN, md, re.IGNORECASE)
        if m:
            currency = m.group(1).upper()
        else:
            for symbol, code in CURRENCY_SYMBOL_MAP.items():
                if symbol in md:
                    currency = code
                    break
    statuses["currency"] = FIELD_STATUS_SUCCESSFUL if currency else FIELD_STATUS_EMPTY

    # ── receiver ──────────────────────────────────────────────────────────────
    receiver = ""
    if "receiver" not in skip_fields:
        receiver = _first_match(patterns["receiver"], md)
        if not receiver:
            for line in md.splitlines():
                ln = line.strip()
                if ln and not ln.startswith("|") and not ln.startswith("#") \
                   and ln.upper() not in ("INVOICE", "BILL", "RECEIPT") and len(ln) > 3:
                    receiver = ln
                    break
        # HTML image placeholder → clear but mark SUSPICIOUS (OCR picked up artifact)
        if receiver and (
            "<!-- image -->" in receiver.lower()
            or re.search(r"<[a-z!][^>]{0,20}>", receiver, re.IGNORECASE)
        ):
            receiver = ""
            statuses["receiver"] = FIELD_STATUS_SUSPICIOUS
        elif not receiver:
            statuses["receiver"] = FIELD_STATUS_EMPTY
        elif is_whitelisted and not is_whitelisted("receiver", receiver):
            statuses["receiver"] = FIELD_STATUS_SUSPICIOUS
            flags.append("receiver_suspicious")
        else:
            statuses["receiver"] = FIELD_STATUS_SUCCESSFUL

    # ── iban ──────────────────────────────────────────────────────────────────
    iban = ""
    if "iban" not in skip_fields:
        iban_raw = _first_match(patterns["iban"], md)
        iban = _clean_iban(iban_raw) if iban_raw else ""
    dev_mode_iban = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    iban_ok = True
    if iban:
        if _iban_suspicious_letters(iban):
            statuses["iban"] = FIELD_STATUS_SUSPICIOUS
            flags.append("iban_suspicious")
            iban_ok = False
        elif not _validate_iban_checksum(iban):
            if dev_mode_iban:
                flags.append("iban_invalid_checksum_dev_skipped")
                statuses["iban"] = FIELD_STATUS_SUSPICIOUS
            else:
                flags.append("iban_invalid_checksum")
                statuses["iban"] = FIELD_STATUS_SUSPICIOUS
                iban = ""
            iban_ok = False
        elif is_whitelisted and not is_whitelisted("iban", iban):
            flags.append("iban_suspicious")
            statuses["iban"] = FIELD_STATUS_SUSPICIOUS
            iban_ok = False
        else:
            statuses["iban"] = FIELD_STATUS_SUCCESSFUL
    else:
        statuses["iban"] = FIELD_STATUS_EMPTY

    # ── bic ───────────────────────────────────────────────────────────────────
    bic = ""
    if "bic" not in skip_fields:
        bic_raw = _first_match(patterns["bic"], md)
        bic = _clean_bic(bic_raw) if bic_raw else ""
    statuses["bic"] = FIELD_STATUS_SUCCESSFUL if bic else FIELD_STATUS_EMPTY

    bankgiro = ""
    plusgiro = ""

    # ── due_date ──────────────────────────────────────────────────────────────
    due_date = ""
    due_date_defaulted = False
    if "due_date" not in skip_fields:
        due_date_raw = _first_match(patterns["due_date"], md)
        due_date, due_date_defaulted = _generate_due_date(md, due_date_raw)
    if due_date_defaulted:
        statuses["due_date"] = FIELD_STATUS_SUSPICIOUS
    elif due_date:
        statuses["due_date"] = FIELD_STATUS_SUCCESSFUL
    else:
        statuses["due_date"] = FIELD_STATUS_EMPTY

    # ── reference ─────────────────────────────────────────────────────────────
    reference = ""
    if "reference" not in skip_fields:
        reference = _first_match(patterns["reference"], md)
        reference = re.sub(r"\s+", "", reference)
    statuses["reference"] = FIELD_STATUS_SUCCESSFUL if reference else FIELD_STATUS_EMPTY

    # ── Mandatory field flags ─────────────────────────────────────────────────
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    mandatory = MANDATORY_BASE if dev_mode else MANDATORY_PROD

    check = {"amount": amount, "currency": currency,
             "receiver": receiver, "due_date": due_date, "reference": reference}
    for field in mandatory:
        if not check[field]:
            flags.append(f"{field}_not_found")

    # invoice_id: warn if missing (suspicious already flagged above)
    if not invoice_id and "invoice_id_suspicious" not in flags:
        flags.append("invoice_id_not_found")

    # due_date defaulted to end-of-month: warn, remove blocking error
    if due_date_defaulted:
        flags.append("due_date_defaulted")
        flags = [f for f in flags if f != "due_date_not_found"]

    # Payment method check
    if not (iban and bic):
        flags.append("no_payment_method")
    elif iban and not bic:
        flags.append("iban_missing_bic")

    seen = set()
    deduped = [f for f in flags if not (f in seen or seen.add(f))]

    has_error = any(f in ERROR_FLAGS for f in deduped)

    return {
        "invoice_id": invoice_id,
        "receiver": receiver,
        "iban": iban,
        "bic": bic,
        "bankgiro": bankgiro,
        "plusgiro": plusgiro,
        "amount": amount,
        "currency": currency,
        "due_date": due_date,
        "reference": reference,
        "needs_review": "YES" if has_error else "NO",
        "review_reasons": "; ".join(deduped),
        "flags": deduped,
        "field_statuses": statuses,
        "ocr_method": "",
    }


def validate_job_fields(job_data: dict) -> dict:
    """Validate manually reviewed job fields. Returns (needs_review, review_reasons)."""
    flags = []
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    mandatory = MANDATORY_BASE if dev_mode else MANDATORY_PROD

    for field in mandatory:
        val = job_data.get(field, "").strip() if job_data.get(field) else ""
        if not val:
            flags.append(f"{field}_not_found")

    iban = job_data.get("iban", "").strip() if job_data.get("iban") else ""
    if iban:
        if not _validate_iban_checksum(iban):
            flags.append("iban_invalid_checksum")
        elif job_data.get("bic"):
            pass
        else:
            flags.append("iban_missing_bic")
    elif job_data.get("bic"):
        flags.append("no_payment_method")

    amount = job_data.get("amount", "").strip() if job_data.get("amount") else ""
    if amount:
        try:
            if float(amount) == 0.0:
                flags.append("amount_not_found")
        except ValueError:
            flags.append("amount_not_found")

    has_error = any(f in ERROR_FLAGS for f in flags)
    return {
        "needs_review": "YES" if has_error else "NO",
        "review_reasons": "; ".join(flags),
        "flags": flags,
    }


def evaluate_field_statuses(fields: dict) -> dict:
    """Re-evaluate SUCCESSFUL/SUSPICIOUS/EMPTY for each field based on current values.
    Input: dict with field values (invoice_id, amount, currency, receiver, iban, bic, due_date, reference).
    Returns: new field_statuses dict. Whitelist not checked (operator edits are trusted)."""
    S, P, E = FIELD_STATUS_SUCCESSFUL, FIELD_STATUS_SUSPICIOUS, FIELD_STATUS_EMPTY
    statuses = {}

    # invoice_id
    v = (fields.get("invoice_id") or "").strip()
    if not v:
        statuses["invoice_id"] = E
    elif len(v) < 6:
        statuses["invoice_id"] = P
    elif sum(1 for c in v if not c.isalnum() and c not in "-./") / len(v) > 0.5:
        statuses["invoice_id"] = P
    else:
        statuses["invoice_id"] = S

    # amount
    v = (fields.get("amount") or "").strip()
    if not v:
        statuses["amount"] = E
    else:
        try:
            f = float(v)
            statuses["amount"] = E if f == 0.0 else (P if f < 100.0 else S)
        except ValueError:
            statuses["amount"] = P

    # currency
    statuses["currency"] = S if (fields.get("currency") or "").strip() else E

    # receiver
    v = (fields.get("receiver") or "").strip()
    if not v:
        statuses["receiver"] = E
    elif "<!-- image -->" in v.lower() or re.search(r"<[a-z!][^>]{0,20}>", v, re.IGNORECASE):
        statuses["receiver"] = P
    else:
        statuses["receiver"] = S

    # iban
    v = (fields.get("iban") or "").strip()
    if not v:
        statuses["iban"] = E
    elif _iban_suspicious_letters(v):
        statuses["iban"] = P
    elif not _validate_iban_checksum(v):
        statuses["iban"] = P
    else:
        statuses["iban"] = S

    # bic
    statuses["bic"] = S if (fields.get("bic") or "").strip() else E

    # due_date — treat any non-empty value as SUCCESSFUL after manual edit
    statuses["due_date"] = S if (fields.get("due_date") or "").strip() else E

    # reference
    statuses["reference"] = S if (fields.get("reference") or "").strip() else E

    return statuses


def llm_extract_fields(md: str, fields: list) -> dict:
    """Ask Haiku to extract multiple fields in one call. Returns {field: value} for found fields."""
    import os, json as _json

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {}

    fields_list = "\n".join(f'  "{f}": "<value or null>"' for f in fields)
    prompt = f"""You are extracting structured data from a Swiss invoice.
Extract the following fields from the markdown below.
Return ONLY valid JSON — no explanation, no markdown fences.
Use null for fields not found. Do not guess or invent values.

Fields:
{{{fields_list}
}}

Invoice markdown:
{md[:3000]}
"""

    print(f"[llm_extract_fields] fields={fields} prompt_chars={len(prompt)}\n--- PROMPT ---\n{prompt}\n--- END ---", flush=True)

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            stop_sequences=["}"],
            messages=[
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "{"},  # prefill — forces JSON object start
            ],
        )
        # Model continues from "{", stop_sequence "}" closes it
        raw = "{" + response.content[0].text + "}"
        print(f"[llm_extract_fields] raw={raw!r} stop={response.stop_reason}", flush=True)

        parsed = _json.loads(raw)
        return {k: v for k, v in parsed.items() if v and v != "null"}
    except Exception as e:
        print(f"[llm_extract_fields] FAILED: {type(e).__name__}: {e}", flush=True)
        return {}
