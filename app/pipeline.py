"""
pipeline.py — PDF → markdown → billing dict

Step 1: markitdown (in-process) + md_clean  →  markdown text
Step 2: if markdown is empty / garbage, fall back to LLM OCR (llm.py)
Step 3: regex extraction from markdown  →  billing dict (extract.py)

"Garbage" heuristic: fewer than MDX_MIN_CHARS printable chars after cleaning.
"""
import os
import re
from markitdown import MarkItDown
from md_clean import clean_markdown
from extract import extract_fields

MDX_MIN_CHARS = int(os.environ.get("MDX_MIN_CHARS", "80"))

_md = MarkItDown()


def _is_garbage(text: str) -> bool:
    printable = re.sub(r"[\x00-\x1f�|#\-\s]", "", text)
    return len(printable) < MDX_MIN_CHARS


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
    return fields
