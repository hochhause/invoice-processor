import anthropic, base64, fitz, io, json, os

MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

PROMPT = """You are an invoice field extractor.
Return ONLY a valid JSON object with exactly these keys:
  invoice_id, receiver, amount, currency, due_date, iban, bic, reference

Rules:
- amount: decimal string like "1234.56", no currency symbol
- currency: ISO 4217 three-letter code (CHF, EUR, USD, etc.)
- due_date: YYYY-MM-DD format
- iban: no spaces
- Use null for missing fields
- No markdown, no explanation, only the JSON object
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

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": content}]
        )
        raw = response.content[0].text.strip()
        parsed = json.loads(raw)
        return {k: (v or "") for k, v in parsed.items()}
    except (json.JSONDecodeError, KeyError, anthropic.APIError) as e:
        print(f"[llm] extraction failed: {e}", flush=True)
        return None


def _pdf_to_images(pdf_path: str, dpi: int = 150) -> list[str]:
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images
