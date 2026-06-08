import anthropic, base64, fitz, json, os, re

MODEL      = os.environ.get("LLM_MODEL",      "claude-haiku-4-5-20251001")
TEXT_MODEL = os.environ.get("LLM_MODEL_TEXT", "claude-haiku-4-5-20251001")

PROMPT = """You are an invoice field extractor. Return a JSON object with NO other text,
NO code fences, NO explanation. Start with { and end with }.

Fields (all required, use null if missing):
invoice_id, receiver, amount (decimal string e.g. "1234.56"), currency (ISO 4217),
due_date (YYYY-MM-DD), iban (uppercase no spaces), bic, reference

Rules: all keys must be present; use null not empty string; if due_date cannot
be parsed to YYYY-MM-DD use null.
IMPORTANT: "receiver" is the payee (the company being paid). NEVER set receiver to
"Lyfegen", "Lyfegen HealthTech AG", "Lyfegen Health Tech AG", or any name that
contains "Lyfegen" — that is the paying company. If only a Lyfegen entity appears,
set receiver to null.

Example: {"invoice_id":"INV-001","receiver":"Acme","amount":"1500.00","currency":"USD","due_date":"2024-12-31","iban":null,"bic":null,"reference":null}"""

_MANDATORY = ["receiver", "amount", "currency", "iban"]


def _extract_text_layer(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    text = "\n".join(page.get_text() for page in doc)
    doc.close()
    return text


def _parse_response(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    raw = m.group(0) if m else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[llm] parse failed: {e}", flush=True)
        return None


def _call_text_llm(client: anthropic.Anthropic, text: str) -> dict | None:
    try:
        response = client.messages.create(
            model=TEXT_MODEL,
            max_tokens=512,
            system=PROMPT,
            messages=[
                {"role": "user", "content": text},
            ],
        )
        raw = response.content[0].text if response.content else ""
        print(f"[llm] text_layer raw={raw!r}", flush=True)
        return _parse_response(raw)
    except anthropic.APIError as e:
        print(f"[llm] text LLM failed: {e}", flush=True)
        return None


def _call_image_llm(client: anthropic.Anthropic, pdf_path: str) -> dict | None:
    images = _pdf_to_images(pdf_path)
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}}
        for b in images
    ]
    content.append({"type": "text", "text": PROMPT})

    print(f"[llm] image model={MODEL} pages={len(images)} pdf={pdf_path}", flush=True)
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=512,
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "{"},
            ],
        )
        print(f"[llm] stop_reason={response.stop_reason} content_blocks={len(response.content)}", flush=True)
        raw = "{" + (response.content[0].text if response.content else "")
        print(f"[llm] image raw={raw!r}", flush=True)
        return _parse_response(raw)
    except anthropic.APIError as e:
        print(f"[llm] image LLM failed: {e}", flush=True)
        return None


def _make_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def extract_text_stage(pdf_path: str) -> dict | None:
    """Text-layer extraction only. Returns parsed fields or None (no text layer / API fail)."""
    raw_text = _extract_text_layer(pdf_path)
    if len(raw_text.strip()) < 80:
        return None
    return _call_text_llm(_make_client(), raw_text)


def extract_image_stage(pdf_path: str) -> dict | None:
    """Image-based extraction only."""
    return _call_image_llm(_make_client(), pdf_path)


def extract_fields(pdf_path: str) -> dict | None:
    client = _make_client()

    raw_text = _extract_text_layer(pdf_path)
    has_text = len(raw_text.strip()) >= 80

    text_result: dict | None = None
    image_result: dict | None = None

    if has_text:
        print(f"[llm] text layer present ({len(raw_text.strip())} chars)", flush=True)
        text_result = _call_text_llm(client, raw_text)

    run_image = (
        not has_text
        or text_result is None
        or any(text_result.get(f) is None for f in _MANDATORY)
    )

    if run_image:
        image_result = _call_image_llm(client, pdf_path)

    if text_result is None and image_result is None:
        return None

    if text_result is not None and image_result is None:
        merged = text_result
        match_type = "text_full"
    elif text_result is None:
        merged = image_result
        match_type = "image_only"
    else:
        merged = {**image_result, **{k: v for k, v in text_result.items() if v is not None}}
        match_type = "hybrid"

    merged["_match_type"] = match_type
    return merged


def _pdf_to_images(pdf_path: str, dpi: int = 150) -> list[str]:
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        images.append(base64.b64encode(pix.tobytes("png")).decode())
    doc.close()
    return images
