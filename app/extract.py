"""
extract.py — Regex extraction of billing fields from markdown text.
Ported from C:\ClaudeTools\markitdown\extract_invoices.py.
No LLM involved — pure pattern matching.

Bank-specific rule sets: import from rules/<CSV_RULE_SET>.py if present,
otherwise use defaults defined here.
"""
import os
import re

MANDATORY_BASE = ["invoice_id", "amount", "currency", "receiver", "due_date"]
MANDATORY_PROD = ["invoice_id", "amount", "currency", "receiver", "due_date", "reference"]


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
    raw = re.sub(r"\s", "", raw)
    m = re.match(r"([A-Z]{2}\d{2}[A-Z0-9]{11,30})", raw)
    return m.group(1) if m else raw


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


def _generate_due_date(text, existing_due_date):
    """If no due_date found and DEV_MODE enabled, generate one month after latest found date."""
    if existing_due_date:
        return existing_due_date
    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    if not dev_mode:
        return ""
    dates = _extract_dates(text)
    if dates:
        latest = max(dates)
        from datetime import timedelta
        due = latest + timedelta(days=30)
        return due.strftime("%Y-%m-%d")
    from datetime import datetime, timedelta
    return (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")


# ── Default patterns (override per bank via rules/<name>.py) ─────────────────

DEFAULT_PATTERNS = {
    "invoice_id": [
        r"INVOICE\s+NUMBER\s*\|\s*([\w\-]+)",
        r"Invoice\s+number[:\s#]*([A-Z0-9\-]+)",
        r"invoice\s*(?:no|#)[:\s#]*([A-Z0-9\-]+)",
    ],
    "amount": [
        r"\|\s*Total\s*\|\s*([\d,\.]+)\s*\|",
        r"\|\s*\|\s*Total\s*([\d,\.]+)\s*\|",
        r"\bTotal\b\s+([\d,\.]+)\s",
        r"\bTotal\b[^\d\n]*([\d,\.]+)",
    ],
    "receiver": [
        r"Beneficiary\s+Name[:\s]+(.+)",
        r"BILL\s+FROM\s*\|\s*BILL\s+TO[^\n]*\n\|[-\s\|]+\n\|\s*([^\|]+?)\s*\|",
    ],
    "iban": [
        r"IBAN\s*(?:Number)?[:\s]*([A-Z]{2}\d{2}[A-Z0-9\s]{11,30})",
        r"\b([A-Z]{2}\d{2}[A-Z0-9]{4}\d{7,})\b",
    ],
    "bic": [
        r"Swift\s*Code[:\s]+([A-Z]{6}[A-Z0-9]{2,5})",
        r"BIC[:\s]+([A-Z]{6}[A-Z0-9]{2,5})",
    ],
    "bankgiro": [r"Bankgiro[:\s]*([\d\-]+)"],
    "plusgiro": [r"Plusgiro[:\s]*([\d\-]+)"],
    "due_date": [
        r"DUE\s+DATE\s*\|\s*([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Due\s+Date[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
    ],
    "reference": [
        r"OCR-?nr[:\s]*([0-9\s]+)",
        r"Beneficiary\s+Account\s+Number[:\s]+([0-9]+)",
        r"ORDER\s+NUMBER\s*\|\s*\|\s*([\w\-]+)",
        r"order\s*(?:number|no)[:\s#]*([A-Z0-9\-]+)",
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
    # rules file may override individual keys; merge with defaults
    patterns = dict(DEFAULT_PATTERNS)
    if hasattr(mod, "PATTERNS"):
        patterns.update(mod.PATTERNS)
    return patterns


def extract_fields(md: str, filename: str) -> dict:
    patterns = _load_rule_set()
    flags = []

    invoice_id = _first_match(patterns["invoice_id"], md)

    amount_raw = _first_match(patterns["amount"], md)
    amount = _clean_amount(amount_raw) if amount_raw else ""

    currency = ""
    m = re.search(CURRENCY_PATTERN, md, re.IGNORECASE)
    if m:
        currency = m.group(1).upper()
    else:
        for symbol, code in CURRENCY_SYMBOL_MAP.items():
            if symbol in md:
                currency = code
                break
    if not currency:
        flags.append("currency_not_found")

    receiver = _first_match(patterns["receiver"], md)
    if not receiver:
        for line in md.splitlines():
            ln = line.strip()
            if ln and not ln.startswith("|") and not ln.startswith("#") \
               and ln.upper() not in ("INVOICE", "BILL", "RECEIPT") and len(ln) > 3:
                receiver = ln
                break

    iban_raw = _first_match(patterns["iban"], md)
    iban = _clean_iban(iban_raw) if iban_raw else ""

    bic = _first_match(patterns["bic"], md)
    bankgiro = _first_match(patterns["bankgiro"], md)
    plusgiro = _first_match(patterns["plusgiro"], md)
    due_date = _first_match(patterns["due_date"], md)
    due_date = _generate_due_date(md, due_date)

    reference = _first_match(patterns["reference"], md)
    reference = re.sub(r"\s+", "", reference)

    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    mandatory = MANDATORY_BASE if dev_mode else MANDATORY_PROD

    check = {"invoice_id": invoice_id, "amount": amount,
             "currency": currency, "receiver": receiver, "due_date": due_date,
             "reference": reference}
    for field in mandatory:
        if not check[field]:
            flags.append(f"{field}_not_found")

    has_iban_bic = iban and bic
    has_bankgiro = bankgiro
    has_plusgiro = plusgiro
    has_payment = has_iban_bic or has_bankgiro or has_plusgiro
    if not has_payment:
        flags.append("no_payment_method")
    elif iban and not bic:
        flags.append("iban_missing_bic")

    seen = set()
    deduped = [f for f in flags if not (f in seen or seen.add(f))]

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
        "needs_review": "YES" if deduped else "NO",
        "review_reasons": "; ".join(deduped),
        "ocr_method": "",   # filled by pipeline.py
    }
