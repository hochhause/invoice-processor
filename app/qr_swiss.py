"""
qr_swiss.py — Swiss QR-bill (QR-Rechnung) extractor.

Scans each page of a PDF for a Swiss Payments Code QR (SPC), decodes it,
and returns a billing dict in the same shape as extract.extract_fields().

Swiss QR-bill payload line layout (SIX standard 2.2):
  0  SPC
  1  0200
  2  1  (UTF-8)
  3  IBAN
  4  creditor address type (S/K)
  5  creditor name
  6  creditor street / address line 1
  7  creditor building / address line 2
  8  creditor postal code  (blank for type K)
  9  creditor city         (blank for type K)
  10 creditor country (CH)
  11-17  ultimate creditor (7 lines, usually blank)
  18 amount  (blank = open)
  19 currency
  20 debtor address type
  21 debtor name
  22-26 debtor address (5 lines)
  27 reference type  (QRR / SCOR / NON)
  28 reference number
  29 unstructured message (additional info)
  30 EPD
  31+ alternative procedures (optional)
"""
import os
import re


def _decode_spc(img, zbar_decode) -> str | None:
    """Try pyzbar on img; return SPC payload string or None."""
    for sym in zbar_decode(img):
        data = sym.data.decode("utf-8", errors="replace")
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        if data.startswith("SPC\n"):
            return data
    return None


def _scan_pdf_qr(pdf_path: str) -> str | None:
    """Return raw QR payload string from first SPC QR code found in pdf, or None.

    Strategy per page:
      1. Full page at 150/300/400 DPI
      2. Bottom-half crop at 150/300/400 DPI (Swiss QR slip is always bottom portion)
    Cropping the bottom half on a large page significantly boosts pyzbar detection
    because the QR occupies a larger fraction of the smaller image.
    """
    try:
        import fitz
        from pyzbar.pyzbar import decode as zbar_decode
        from PIL import Image
        import io
    except ImportError:
        return None

    import sys
    from pathlib import Path

    debug_dir = os.environ.get("DEBUG_QR_DIR", "")
    base = os.path.splitext(os.path.basename(pdf_path))[0]

    def _save_debug(img, label):
        if not debug_dir:
            return
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        img.save(Path(debug_dir) / f"{base}_{label}.png")

    fitz.TOOLS.mupdf_display_errors(False)
    doc = fitz.open(pdf_path)
    for page_num, page in enumerate(doc):
        for dpi in (150, 300, 400):
            pix = page.get_pixmap(dpi=dpi)
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            # 1. full page
            _save_debug(img, f"p{page_num+1}_{dpi}dpi_full")
            result = _decode_spc(img, zbar_decode)
            if result:
                print(f"[qr_swiss] found SPC on page {page_num+1} full at {dpi}dpi", file=sys.stderr, flush=True)
                doc.close()
                return result

            # 2. bottom third, center 60% (Swiss QR slip location)
            w, h = img.size
            bottom = img.crop((w * 2 // 10, h * 2 // 3, w * 8 // 10, h))
            _save_debug(bottom, f"p{page_num+1}_{dpi}dpi_crop")
            result = _decode_spc(bottom, zbar_decode)
            if result:
                print(f"[qr_swiss] found SPC on page {page_num+1} bottom-crop at {dpi}dpi", file=sys.stderr, flush=True)
                doc.close()
                return result

        print(f"[qr_swiss] no SPC found on page {page_num+1} of {pdf_path}", file=sys.stderr, flush=True)

    doc.close()
    return None


def _parse_spc(payload: str) -> dict | None:
    """Parse Swiss Payments Code payload into billing fields. Returns None if not valid SPC."""
    lines = payload.split("\n")
    if len(lines) < 30 or lines[0].strip() != "SPC":
        return None

    def g(i):
        return lines[i].strip() if i < len(lines) else ""

    iban = re.sub(r"\s", "", g(3))
    creditor_name = g(5)
    amount_raw = g(18)
    currency = g(19).upper()
    ref_type = g(27)
    reference = re.sub(r"\s", "", g(28))
    additional = g(29)

    amount = ""
    if amount_raw:
        try:
            amount = f"{float(amount_raw):.2f}"
        except ValueError:
            amount = amount_raw

    flags = []
    if not iban:
        flags.append("qr_iban_missing")
    if not currency:
        flags.append("currency_not_found")

    return {
        "iban": iban,
        "bic": "",
        "receiver": creditor_name,
        "amount": amount,
        "currency": currency,
        "reference": reference if ref_type != "NON" else "",
        "additional_info": additional,
        "flags": flags,
        "qr_ref_type": ref_type,
    }


def extract_from_pdf(pdf_path: str) -> dict | None:
    """
    Try to extract a Swiss QR-bill from pdf_path.
    Returns partial billing dict (same keys as extract.extract_fields minus
    invoice_id / due_date which must come from the document text), or None
    if no valid SPC QR code found.
    """
    payload = _scan_pdf_qr(pdf_path)
    if not payload:
        return None
    return _parse_spc(payload)
