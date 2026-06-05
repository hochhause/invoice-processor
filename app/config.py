"""
config.py — Application configuration and health checks.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def llm_available() -> bool:
    """Test Anthropic connection once at startup; cache result."""
    flag = os.getenv("ENABLE_LLM_FALLBACK", "")
    if flag.lower() not in ("true", "1", "yes"):
        print(f"[config] LLM disabled: ENABLE_LLM_FALLBACK={repr(flag)}", flush=True)
        return False

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("[config] LLM disabled: ANTHROPIC_API_KEY not set", flush=True)
        return False
    if not key.startswith("sk-ant-"):
        print(f"[config] LLM disabled: key prefix wrong ({key[:12]}...)", flush=True)
        return False

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: OK"}],
        )
        # Check any content returned — do NOT check stop_reason:
        # stop_reason="max_tokens" is also a valid success (model responded).
        result = len(response.content) > 0
        print(f"[config] LLM health check passed ✓ stop={response.stop_reason}", flush=True)
        return result
    except ImportError:
        print("[config] LLM disabled: 'anthropic' package not installed (add to requirements.txt + rebuild)", flush=True)
        return False
    except Exception as e:
        print(f"[config] LLM health check failed: {type(e).__name__}: {e}", flush=True)
        return False
