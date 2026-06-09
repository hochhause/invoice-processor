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

def _extract_text_layer(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _parse_response(raw: str) -> dict | None:
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    raw = m.group(0) if m else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[llm] parse failed: {e}", flush=True)
        return None


def _usage(response, model: str) -> dict:
    """Token-usage record for one API call (cache_* tokens are 0 — caching off)."""
    return {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "model": model,
    }


def _call_text_llm(client: anthropic.Anthropic, text: str) -> tuple[dict | None, dict | None]:
    """Returns (parsed_fields_or_None, usage_or_None). Usage is captured even when
    parsing fails (tokens were still spent); None only when the API call raised."""
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
        return _parse_response(raw), _usage(response, TEXT_MODEL)
    except anthropic.APIError as e:
        print(f"[llm] text LLM failed: {e}", flush=True)
        return None, None


def _call_image_llm(client: anthropic.Anthropic, pdf_path: str) -> tuple[dict | None, dict | None]:
    """Returns (parsed_fields_or_None, usage_or_None)."""
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
        return _parse_response(raw), _usage(response, MODEL)
    except anthropic.APIError as e:
        print(f"[llm] image LLM failed: {e}", flush=True)
        return None, None


def _make_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=api_key)


def extract_text_stage(pdf_path: str) -> tuple[dict | None, dict | None]:
    """Text-layer extraction. Returns (fields_or_None, usage_or_None) — (None, None)
    when there is no usable text layer or the read/API call fails."""
    try:
        raw_text = _extract_text_layer(pdf_path)
    except Exception as e:
        print(f"[llm] text layer read failed: {e}", flush=True)
        return None, None
    if len(raw_text.strip()) < 80:
        return None, None
    return _call_text_llm(_make_client(), raw_text)


def extract_image_stage(pdf_path: str) -> tuple[dict | None, dict | None]:
    """Image-based extraction. Returns (fields_or_None, usage_or_None)."""
    try:
        return _call_image_llm(_make_client(), pdf_path)
    except Exception as e:
        print(f"[llm] image stage failed: {e}", flush=True)
        return None, None


def _pdf_to_images(pdf_path: str, dpi: int = 150) -> list[str]:
    doc = fitz.open(pdf_path)
    try:
        return [
            base64.b64encode(page.get_pixmap(dpi=dpi).tobytes("png")).decode()
            for page in doc
        ]
    finally:
        doc.close()
