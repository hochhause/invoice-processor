"""
config.py — Application configuration and health checks.
"""
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def llm_available() -> bool:
    """Test Anthropic connection once at startup; cache result."""
    if not os.getenv("ENABLE_LLM_FALLBACK", "").lower() in ("true", "1", "yes"):
        return False

    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key or not key.startswith("sk-ant-"):
        return False

    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{"role": "user", "content": "ok"}],
        )
        result = response.stop_reason == "end_turn"
        if result:
            print("[config] LLM health check passed ✓")
        return result
    except Exception as e:
        print(f"[config] LLM health check failed: {e}")
        return False
