"""
extract.py — Regex extraction of billing fields from markdown text.
Ported from C:\ClaudeTools\markitdown\extract_invoices.py.
No LLM involved — pure pattern matching.

Bank-specific rule sets: import from rules/<CSV_RULE_SET>.py if present,
otherwise use defaults defined here.
"""
import calendar
import os
import re
from datetime import datetime

# Fields whose absence blocks payment → needs_review=YES
ERROR_FLAGS = frozenset({
    "amount_not_found", "currency_not_found", "receiver_not_found",
    "due_date_not_found", "no_payment_method", "reference_not_found",
    "iban_invalid_checksum", "qr_scan_failed",
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
    raw = re.sub(r"\s", "", raw)
    m = re.match(r"([A-Z]{2}\d{2}[A-Z0-9]{11,30})", raw)
    return m.group(1) if m else raw


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
    ],
    "receiver": [
        r"Beneficiary\s+Name[:\s]+(.+)",
        r"BILL\s+FROM\s*\|\s*BILL\s+TO[^\n]*\n\|[-\s\|]+\n\|\s*([^\|]+?)\s*\|",
    ],
    "iban": [
        r"IBAN\s*(?:Number)?[:\s]*([A-Z]{2}\d{2}[A-Z0-9]{11,30})",
        r"IBAN[:\s]*([A-Z]{2}\d{2}[A-Z0-9\s]{11,})",
        r"\b([A-Z]{2}\d{2}[A-Z0-9]{11,30})\b",
    ],
    "bic": [
        r"(?:Swift\s*Code|BIC|SWIFT)[:\s]+([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b",
    ],
    # LEGACY: Bankgiro/Plusgiro are Swedish-only payment methods, no longer extracted.
    # "bankgiro": [r"Bankgiro[:\s]*([\d\-]+)"],
    # "plusgiro": [r"Plusgiro[:\s]*([\d\-]+)"],
    "due_date": [
        # Table cell: | Zahlbar bis | 30.06.2026 |  (case-insensitive)
        r"\|\s*[Zz]ahlbar\s+bis\s*:?\s*\|\s*([\d]{1,2}\.[\d]{1,2}\.[\d]{4})\s*\|",
        # Table cell with German month name: | zahlbar bis: | 5. Oktober 2025 |
        r"\|\s*[Zz]ahlbar\s+bis\s*:?\s*\|\s*(\d{1,2}\.\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})",
        # Inline with German month name: zahlbar bis: 5. Dezember 2025
        r"[Zz]ahlbar\s+bis\s*:?\s*(\d{1,2}\.\s*(?:Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4})",
        # Fälligkeitsdatum table: | Fälligkeitsdatum | 10/13/2024 |
        r"\|\s*F[äa]lligkeitsdatum\s*\|\s*([\d]{1,2}[\/\.][\d]{1,2}[\/\.][\d]{4})",
        # English
        r"DUE\s+DATE\s*\|?\s*([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Due\s+Date[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Payment\s+Due[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Payable\s+by[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        # German / Swiss inline
        r"[Zz]ahlbar\s+bis[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"F[äa]llig(?:keitsdatum)?[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Zahlungsfrist[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        r"Valuta[:\s]+([\d]{1,2}[\/\-\.][\d]{1,2}[\/\-\.][\d]{2,4})",
        # ISO date after label
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
    # rules file may override individual keys; merge with defaults
    patterns = dict(DEFAULT_PATTERNS)
    if hasattr(mod, "PATTERNS"):
        patterns.update(mod.PATTERNS)
    return patterns


def extract_fields(md: str, filename: str, skip_fields: set = None) -> dict:
    if skip_fields is None:
        skip_fields = set()
    patterns = _load_rule_set()
    flags = []

    invoice_id = _first_match(patterns["invoice_id"], md) if "invoice_id" not in skip_fields else ""

    amount = ""
    if "amount" not in skip_fields:
        amount_raw = _first_match(patterns["amount"], md)
        amount = _clean_amount(amount_raw) if amount_raw else ""

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
        if not currency:
            flags.append("currency_not_found")

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

    iban = ""
    if "iban" not in skip_fields:
        iban_raw = _first_match(patterns["iban"], md)
        iban = _clean_iban(iban_raw) if iban_raw else ""
    dev_mode_iban = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    if iban and not _validate_iban_checksum(iban):
        if dev_mode_iban:
            flags.append("iban_invalid_checksum_dev_skipped")
        else:
            flags.append("iban_invalid_checksum")
            iban = ""

    bic = ""
    if "bic" not in skip_fields:
        bic = _first_match(patterns["bic"], md)

    bankgiro = ""
    plusgiro = ""

    due_date = ""
    due_date_defaulted = False
    if "due_date" not in skip_fields:
        due_date_raw = _first_match(patterns["due_date"], md)
        due_date, due_date_defaulted = _generate_due_date(md, due_date_raw)

    reference = ""
    if "reference" not in skip_fields:
        reference = _first_match(patterns["reference"], md)
        reference = re.sub(r"\s+", "", reference)

    dev_mode = os.environ.get("DEV_MODE", "").lower() in ("true", "1", "yes")
    mandatory = MANDATORY_BASE if dev_mode else MANDATORY_PROD

    check = {"amount": amount, "currency": currency,
             "receiver": receiver, "due_date": due_date, "reference": reference}
    for field in mandatory:
        if not check[field]:
            flags.append(f"{field}_not_found")

    # invoice_id: warn only, never blocks payment
    if not invoice_id:
        flags.append("invoice_id_not_found")

    # due_date defaulted to end-of-month: warn, remove blocking error
    if due_date_defaulted:
        flags.append("due_date_defaulted")
        flags = [f for f in flags if f != "due_date_not_found"]

    # Payment method check: IBAN + BIC required (bankgiro/plusgiro legacy, no longer supported)
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
        "flags": deduped,          # structured list for pipeline merging
        "ocr_method": "",          # filled by pipeline.py
    }


def llm_extract_field(md: str, field: str) -> str:
    """Ask Haiku to extract a single missing field from markdown (Tier 1 fallback)."""
    import os

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return ""

    prompt = f"""Extract the {field} from this invoice markdown.
Return ONLY the extracted value, nothing else. No explanation.
If the {field} is not found, return: NOT_FOUND

Field to extract: {field}

Markdown:
{md[:2000]}
"""

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()
        return "" if result == "NOT_FOUND" else result
    except Exception as e:
        print(f"[llm_extract_field] Failed for {field}: {e}")
        return ""
