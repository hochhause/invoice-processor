import anthropic, base64, fitz, io, json, os, re

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

PROMPT = """You are an invoice field extractor. Extract fields from the invoice image(s).

Return ONLY a valid JSON object with exactly these keys:
  invoice_id, receiver, amount, currency, iban, bic, reference

Field definitions:
- invoice_id: invoice number or document reference printed on the invoice
- receiver: name of the company or person being paid (the payee/creditor)
- amount: total amount due as a decimal string, e.g. "1234.56" — no currency symbol, no apostrophes
- currency: ISO 4217 three-letter code — CHF, EUR, USD, etc.
- iban: International Bank Account Number — 15-34 alphanumeric chars, no spaces, e.g. "CH5604835012345678009"
- bic: Bank Identifier Code (SWIFT code) — 8 or 11 chars, e.g. "KBSBCHBB" or "RAIFCH22XXX"
- reference: payment reference number (QR-reference, ESR, or free text)

Rules:
- Use null for any field not found on the invoice
- Do not invent or guess values

Example output:
{"invoice_id": "RE-2024-00123", "receiver": "Muster AG", "amount": "4800.00", "currency": "CHF", "iban": "CH5604835012345678009", "bic": "KBSBCHBB", "reference": "210000000003139471430009017"}
"""

def extract_fields(pdf_path: str) -> dict | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    images = _pdf_to_images(pdf_path)

    content = []
    for img_b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": img_b64}
        })
    content.append({"type": "text", "text": PROMPT})

    print(f"[llm] model={MODEL} pages={len(images)} pdf={pdf_path}", flush=True)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "{"},
            ]
        )
        print(f"[llm] stop_reason={response.stop_reason} content_blocks={len(response.content)}", flush=True)
        # prefill adds "{" — model continues from there; prepend it back
        continuation = response.content[0].text if response.content else ""
        raw = "{" + continuation
        # trim anything outside the outermost { }
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        raw = m.group(0) if m else raw
        print(f"[llm] raw_response={raw!r}", flush=True)
        parsed = json.loads(raw)
        return {k: (v or "") for k, v in parsed.items()}
    except (json.JSONDecodeError, KeyError, anthropic.APIError) as e:
        print(f"[llm] extraction failed: {type(e).__name__}: {e}", flush=True)
        return None


def _pdf_to_images(pdf_path: str, dpi: int = 150) -> list[str]:
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images
