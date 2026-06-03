"""
llm.py — Pluggable LLM client for OCR fallback.
Provider selected via LLM_PROVIDER env var: ollama | claude | deepseek
Returns plain text (the OCR'd content of the PDF image/page).
Adding a new provider: implement a function matching the signature of
_call_ollama and register it in PROVIDERS.
"""
import os
import json
import base64
import urllib.request
import urllib.error

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")
LLM_URL = os.environ.get("LLM_URL", "http://host.docker.internal:11434")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama3.2")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PROMPT = (
    "You are an OCR engine. Extract ALL text from this document image exactly as written. "
    "Preserve layout, numbers, and punctuation. Return only the extracted text, nothing else."
)


def _post(url: str, payload: dict, headers: dict) -> str:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read().decode()


def _call_ollama(image_b64: str) -> str:
    body = json.loads(_post(
        f"{LLM_URL.rstrip('/')}/api/generate",
        {"model": LLM_MODEL, "prompt": PROMPT, "images": [image_b64], "stream": False},
        {"Content-Type": "application/json"},
    ))
    return body.get("response", "")


def _call_claude(image_b64: str) -> str:
    body = json.loads(_post(
        "https://api.anthropic.com/v1/messages",
        {
            "model": LLM_MODEL,
            "max_tokens": 2048,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        },
        {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01",
        },
    ))
    return body["content"][0]["text"]


def _call_deepseek(image_b64: str) -> str:
    body = json.loads(_post(
        f"{LLM_URL.rstrip('/')}/v1/chat/completions",
        {
            "model": LLM_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
            "max_tokens": 2048,
        },
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_KEY}",
        },
    ))
    return body["choices"][0]["message"]["content"]


PROVIDERS = {
    "ollama": _call_ollama,
    "claude": _call_claude,
    "deepseek": _call_deepseek,
}


def ocr_pdf_via_llm(pdf_path: str) -> str:
    """
    Convert each page of pdf_path to a PNG image, send to the configured
    LLM vision endpoint, concatenate page texts.
    Requires: pip install pymupdf  (imported lazily so container still starts
    if it's absent and LLM fallback is never triggered)
    """
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("pymupdf not installed — cannot use LLM OCR fallback. "
                           "Add 'pymupdf' to requirements.txt and rebuild.")

    caller = PROVIDERS.get(LLM_PROVIDER)
    if not caller:
        raise RuntimeError(f"Unknown LLM_PROVIDER: {LLM_PROVIDER!r}")

    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        img_b64 = base64.b64encode(pix.tobytes("png")).decode()
        pages.append(caller(img_b64))
    doc.close()
    return "\n\n".join(pages)
