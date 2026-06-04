"""
pipeline.py — PDF → Docling markdown → billing dict

Step 1: Swiss QR-bill extraction (qr_swiss) — locks known fields if found
Step 2: Docling DocumentConverter → markdown
Step 3: regex extraction from markdown → billing dict (extract.py)

Debug mode: If DEBUG_MD_DIR is set, store Docling markdown to disk for inspection.
"""
import os
import sys
from pathlib import Path
from docling.document_converter import DocumentConverter
from extract import extract_fields
import qr_swiss

DEBUG_MD_DIR = os.environ.get("DEBUG_MD_DIR", "")

_converter = DocumentConverter()


def _convert_docling(pdf_path: str) -> str:
    """Convert PDF via Docling, return markdown."""
    result = _converter.convert(pdf_path)
    return result.document.export_to_markdown()


def _save_debug_md(job_id: str, filename: str, md_content: str):
    """Store Docling markdown to disk if DEBUG_MD_DIR is set."""
    if not DEBUG_MD_DIR:
        return
    debug_dir = Path(DEBUG_MD_DIR)
    debug_dir.mkdir(parents=True, exist_ok=True)
    base_name = Path(filename).stem
    md_file = debug_dir / f"{job_id}_{base_name}.md"
    md_file.write_text(md_content, encoding="utf-8")


def _delete_debug_md(job_id: str, upload_dir: Path):
    """Delete all markdown debug files for a job."""
    if not DEBUG_MD_DIR:
        return
    debug_dir = Path(DEBUG_MD_DIR)
    for md_file in debug_dir.glob(f"{job_id}_*.md"):
        md_file.unlink(missing_ok=True)


def _merge_qr(fields: dict, qr: dict) -> dict:
    """Merge QR-extracted fields with regex-extracted fields. QR takes precedence."""
    from extract import ERROR_FLAGS
    for key in ("iban", "bic", "receiver", "amount", "currency", "reference"):
        if qr.get(key):
            fields[key] = qr[key]

    merged = qr.get("flags", []) + fields.get("flags", [])
    seen = set()
    merged = [f for f in merged if not (f in seen or seen.add(f))]

    if qr.get("iban") and "no_payment_method" in merged:
        merged = [f for f in merged if f != "no_payment_method"]

    fields["flags"] = merged
    fields["review_reasons"] = "; ".join(merged)
    has_error = any(f in ERROR_FLAGS for f in merged)
    fields["needs_review"] = "YES" if has_error else "NO"
    fields["ocr_method"] = "docling+qr"
    return fields


def run(pdf_path: str) -> dict:
    """
    QR-first pipeline: extract QR → Docling → regex → merge

    Returns dict: invoice_id, receiver, iban, bic, bankgiro, plusgiro,
    amount, currency, due_date, reference, needs_review, review_reasons, ocr_method
    """
    filename = os.path.basename(pdf_path)
    qr = None
    qr_locked = set()

    # ── Step 1: QR extraction (first) ──────────────────────────────────────────
    try:
        qr = qr_swiss.extract_from_pdf(pdf_path)
        if qr:
            qr_locked = {"iban", "bic", "receiver", "amount", "currency", "reference"}
    except Exception as e:
        print(f"[pipeline] QR scan failed for {filename}: {e}", file=sys.stderr, flush=True)

    # ── Step 2: Docling conversion ────────────────────────────────────────────
    try:
        md = _convert_docling(pdf_path)
    except Exception as e:
        print(f"[pipeline] Docling convert failed: {e}", file=sys.stderr, flush=True)
        md = ""

    # ── Step 3: Regex extraction ──────────────────────────────────────────────
    fields = extract_fields(md, filename, skip_fields=qr_locked)
    fields["ocr_method"] = "docling"

    # ── Step 4: QR overlay (if found) ─────────────────────────────────────────
    if qr:
        fields = _merge_qr(fields, qr)
    else:
        # No QR found, just ensure flags list is set
        flags = fields.get("flags", [])
        fields["review_reasons"] = "; ".join(flags)

    # ── Debug: save markdown if DEBUG_MD_DIR set ──────────────────────────────
    if DEBUG_MD_DIR:
        job_id = filename.split("_")[0]
        _save_debug_md(job_id, filename, md)

    return fields
