"""
vendors.py — Vendor matching and invoice ID format detection.

On each processed doc:
  1. Match extracted receiver/iban against vendors table (either field → fill both).
  2. For the matched vendor, scan the document for invoice IDs matching
     the stored format in the range [last_known + 1 … last_known + 300].
"""
import re


def _parse_id_format(invoice_id: str):
    """Return (prefix, num, zero_padded_width) from a trailing-digit invoice ID, or None."""
    m = re.search(r'^(.*?)(\d+)$', invoice_id.strip())
    if not m:
        return None
    prefix, digits = m.group(1), m.group(2)
    return prefix, int(digits), len(digits)


def _candidate_ids(invoice_ids: list) -> list[tuple]:
    """Parse all stored IDs into (prefix, num, width) triples, drop unparseable."""
    result = []
    for iid in invoice_ids:
        parsed = _parse_id_format(iid)
        if parsed:
            result.append(parsed)
    return result


def match_vendor_fields(fields: dict, md: str) -> dict:
    """
    Given extracted fields and the raw markdown, look up a vendor match.
    Returns a dict of field overrides (may be empty if no match).

    Side effects: none — caller is responsible for persisting vendor invoice_id updates.
    """
    import db

    receiver = (fields.get("receiver") or "").strip()
    iban = (fields.get("iban") or "").strip()

    vendor = db.find_vendor(receiver=receiver, iban=iban)
    if not vendor:
        return {}

    overrides = {}

    # Fill receiver + IBAN from vendor record
    if vendor["name"] and not receiver:
        overrides["receiver"] = vendor["name"]
    if vendor["iban"] and not iban:
        overrides["iban"] = vendor["iban"]

    # Try to find invoice_id in doc using stored format + range
    found_invoice_id = _match_invoice_id_in_doc(vendor["invoice_ids"], md)
    if found_invoice_id:
        overrides["invoice_id"] = found_invoice_id

    overrides["_matched_vendor_id"] = vendor["id"]
    return overrides


def _match_invoice_id_in_doc(invoice_ids: list, md: str) -> str:
    """
    Parse stored invoice ID formats, build a regex for each prefix,
    search the markdown for any number in [last+1 … last+300] with that format.
    Returns the first match found, or "".
    """
    if not invoice_ids:
        return ""

    candidates = _candidate_ids(invoice_ids)
    if not candidates:
        return ""

    # Group by prefix, find max num per prefix
    by_prefix: dict[str, tuple[int, int]] = {}  # prefix → (max_num, width)
    for prefix, num, width in candidates:
        if prefix not in by_prefix or num > by_prefix[prefix][0]:
            by_prefix[prefix] = (num, width)

    for prefix, (max_num, width) in by_prefix.items():
        low = max_num + 1
        high = max_num + 300
        escaped = re.escape(prefix)
        # Match prefix followed by exactly `width` digits (or more if not zero-padded)
        pat = re.compile(rf'{escaped}(\d{{{width},}})')
        for m in pat.finditer(md):
            n = int(m.group(1))
            if low <= n <= high:
                # Reconstruct with same zero-padding
                formatted_num = str(n).zfill(width)
                return f"{prefix}{formatted_num}"

    return ""
