"""
pipeline.py — PDF → markdown → billing dict

Step 1: markitdown (in-process) + md_clean  →  markdown text
Step 2: if markdown is empty / garbage, fall back to LLM OCR (llm.py)
Step 3: regex extraction from markdown  →  billing dict (extract.py)

"Garbage" heuristic: fewer than MDX_MIN_CHARS printable chars after cleaning.

Debug mode: If DEBUG_MD_DIR is set, store extracted markdown to disk for inspection.
"""
import os
import re
from pathlib import Path
from markitdown import MarkItDown
from md_clean import clean_markdown
from extract import extract_fields

MDX_MIN_CHARS = int(os.environ.get("MDX_MIN_CHARS", "80"))
DEBUG_MD_DIR = os.environ.get("DEBUG_MD_DIR", "")

_md = MarkItDown()


def _is_garbage(text: str) -> bool:
    printable = re.sub(r"[\x00-\x1f�|#\-\s]", "", text)
    return len(printable) < MDX_MIN_CHARS


def _save_debug_md(job_id: str, filename: str, md_content: str):
    """Store extracted markdown to disk if DEBUG_MD_DIR is set."""
    if not DEBUG_MD_DIR:
        return
    debug_dir = Path(DEBUG_MD_DIR)
    debug_dir.mkdir(parents=True, exist_ok=True)
    # Extract base filename without extension
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
    """Overwrite regex fields with QR values where QR has data, merge flags."""
    from extract import ERROR_FLAGS
    for key in ("iban", "bic", "receiver", "amount", "currency", "reference"):
        if qr.get(key):
            fields[key] = qr[key]

    # merge flag lists (QR flags first so they appear prominently)
    merged = qr.get("flags", []) + fields.get("flags", [])
    seen = set()
    merged = [f for f in merged if not (f in seen or seen.add(f))]

    # QR resolves no_payment_method if it provided an IBAN
    if qr.get("iban") and "no_payment_method" in merged:
        merged = [f for f in merged if f != "no_payment_method"]

    fields["flags"] = merged
    fields["review_reasons"] = "; ".join(merged)
    has_error = any(f in ERROR_FLAGS or f.startswith("llm_fallback_failed") for f in merged)
    fields["needs_review"] = "YES" if has_error else "NO"
    fields["ocr_method"] = fields.get("ocr_method", "mdx") + "+qr"
    return fields


def run(pdf_path: str, force_llm: bool = False) -> dict:
    """
    Returns dict with keys: invoice_id, receiver, iban, bic, bankgiro,
    plusgiro, amount, currency, due_date, reference,
    needs_review, review_reasons, ocr_method

    force_llm=True skips mdx and goes straight to LLM vision (used when
    user manually queues a script-processed file for AI re-processing).
    """
    # ── Step 1: mdx (skipped if force_llm) ───────────────────────────────────
    ocr_method = "mdx"
    if not force_llm:
        try:
            result = _md.convert(pdf_path)
            raw_md = result.text_content or ""
            md = clean_markdown(raw_md)
        except Exception:
            md = ""
        if _is_garbage(md):
            force_llm = True

    # ── Step 2: LLM fallback / forced LLM ────────────────────────────────────
    if force_llm:
        try:
            from llm import ocr_pdf_via_llm, LLM_PROVIDER
            ocr_method = LLM_PROVIDER
            md = ocr_pdf_via_llm(pdf_path)
        except Exception as e:
            md = ""
            fields = extract_fields("", os.path.basename(pdf_path))
            fields["ocr_method"] = ocr_method if ocr_method != "mdx" else "llm"
            fields["review_reasons"] = f"llm_fallback_failed({e}); " + fields["review_reasons"]
            fields["needs_review"] = "YES"
            return fields

    # ── Step 3: extraction ───────────────────────────────────────────────────
    fields = extract_fields(md, os.path.basename(pdf_path))
    fields["ocr_method"] = ocr_method

    # ── Step 3b: Swiss QR-bill — overrides regex for IBAN/amount/receiver/ref ─
    try:
        import qr_swiss
        qr = qr_swiss.extract_from_pdf(pdf_path)
        if qr:
            fields = _merge_qr(fields, qr)
    except Exception:
        existing = fields.get("review_reasons", "")
        fields["review_reasons"] = "; ".join(filter(None, ["qr_scan_failed", existing]))
        fields["needs_review"] = "YES"

    # ── Debug: save markdown if DEBUG_MD_DIR set ──────────────────────────────
    if DEBUG_MD_DIR:
        job_id = os.path.splitext(os.path.basename(pdf_path))[0]
        # Extract actual job_id from filename (format: {job_id}_{filename})
        job_id = os.path.basename(pdf_path).split("_")[0]
        _save_debug_md(job_id, os.path.basename(pdf_path), md)

    return fields
