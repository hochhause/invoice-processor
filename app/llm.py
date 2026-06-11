import anthropic, base64, fitz, json, os, re, time

# Models resolved per call (not import time) so the desktop settings popup
# can switch models without a restart.
def _model() -> str:
    return os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")


def _text_model() -> str:
    return os.environ.get("LLM_MODEL_TEXT", "claude-haiku-4-5-20251001")


USE_BATCH = os.environ.get("LLM_USE_BATCH", "false").lower() == "true"

PROMPT = """You are an invoice field extractor. Return a JSON object with NO other text,
NO code fences, NO explanation. Start with { and end with }.

Fields (all required, use null if missing):
invoice_id, receiver, amount (decimal string e.g. "1234.56"), currency (ISO 4217),
due_date (YYYY-MM-DD), iban (uppercase no spaces), bic, reference,
cdtr_street (street name only, no building number),
cdtr_building_no (building or house number as a string),
cdtr_postcode (postal/ZIP code),
cdtr_town (city or town name),
cdtr_country (ISO 3166-1 alpha-2 two-letter code e.g. "CH", "DE", "US")

Rules: all keys must be present; use null not empty string; if due_date cannot
be parsed to YYYY-MM-DD use null.
IMPORTANT: "receiver" is the payee (the company being paid). NEVER set receiver to
"Lyfegen", "Lyfegen HealthTech AG", "Lyfegen Health Tech AG", or any name that
contains "Lyfegen" — that is the paying company. If only a Lyfegen entity appears,
set receiver to null.
Address fields (cdtr_*) are the receiver's address as printed on the invoice;
set all to null when no address appears. cdtr_country MUST be a 2-letter ISO code
or null — never a full country name.

Example: {"invoice_id":"INV-001","receiver":"Acme","amount":"1500.00","currency":"USD","due_date":"2024-12-31","iban":null,"bic":null,"reference":null,"cdtr_street":null,"cdtr_building_no":null,"cdtr_postcode":null,"cdtr_town":null,"cdtr_country":null}"""

_CACHED_SYSTEM = [{"type": "text", "text": PROMPT, "cache_control": {"type": "ephemeral"}}]

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
    u = response.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "model": model,
    }


def _call_text_llm(client: anthropic.Anthropic, text: str) -> tuple[dict | None, dict | None]:
    """Returns (parsed_fields_or_None, usage_or_None)."""
    text_model = _text_model()
    try:
        if USE_BATCH:
            return _call_batch(client, text_model, system=_CACHED_SYSTEM,
                               messages=[{"role": "user", "content": text}])
        response = client.messages.create(
            model=text_model,
            max_tokens=512,
            system=_CACHED_SYSTEM,
            messages=[{"role": "user", "content": text}],
        )
        raw = response.content[0].text if response.content else ""
        print(f"[llm] text_layer raw={raw!r}", flush=True)
        return _parse_response(raw), _usage(response, text_model)
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
    # Prompt appended to user content; system carries the cached instruction block.
    content.append({"type": "text", "text": "Extract the invoice fields as JSON."})

    model = _model()
    print(f"[llm] image model={model} pages={len(images)} pdf={pdf_path}", flush=True)
    try:
        if USE_BATCH:
            return _call_batch(client, model, system=_CACHED_SYSTEM,
                               messages=[{"role": "user", "content": content}])
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=_CACHED_SYSTEM,
            messages=[
                {"role": "user", "content": content},
                {"role": "assistant", "content": "{"},
            ],
        )
        print(f"[llm] stop_reason={response.stop_reason} content_blocks={len(response.content)}", flush=True)
        raw = "{" + (response.content[0].text if response.content else "")
        print(f"[llm] image raw={raw!r}", flush=True)
        return _parse_response(raw), _usage(response, model)
    except anthropic.APIError as e:
        print(f"[llm] image LLM failed: {e}", flush=True)
        return None, None


def _call_batch(
    client: anthropic.Anthropic,
    model: str,
    system: list,
    messages: list,
) -> tuple[dict | None, dict | None]:
    """Submit a single-request batch, poll until done, return (fields, usage)."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    batch = client.messages.batches.create(
        requests=[
            Request(
                custom_id="invoice",
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=512,
                    system=system,
                    messages=messages,
                ),
            )
        ]
    )
    print(f"[llm] batch submitted id={batch.id}", flush=True)
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        print(f"[llm] batch polling status={batch.processing_status}", flush=True)
        time.sleep(10)

    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            msg = result.result.message
            raw = msg.content[0].text if msg.content else ""
            print(f"[llm] batch raw={raw!r}", flush=True)
            return _parse_response(raw), _usage(msg, model)
        print(f"[llm] batch result type={result.result.type}", flush=True)
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
