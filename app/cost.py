"""
cost.py — LLM cost estimation from stored token usage.

Cost is computed from the tokens the API actually reported (usage.input_tokens /
usage.output_tokens), stored per job alongside the model that produced them. So
switching LLM_MODEL (e.g. to Sonnet) reprices new jobs automatically, and the
analytics breakdown stays correct per model. Prompt caching is not enabled, so
cache tokens are 0 and not separated out here; add columns if caching is added.

Prices are USD per 1,000,000 tokens (input, output), per Anthropic's published
pricing. Keep in sync with the model the pipeline runs (see llm.py MODEL).
"""

# USD per 1M tokens: (input_per_mtok, output_per_mtok).
PRICING = {
    "claude-haiku-4-5":           (1.00, 5.00),
    "claude-haiku-4-5-20251001":  (1.00, 5.00),
    "claude-sonnet-4-5":          (3.00, 15.00),
    "claude-sonnet-4-5-20250929": (3.00, 15.00),
    "claude-sonnet-4-6":          (3.00, 15.00),
    "claude-opus-4-8":            (5.00, 25.00),
}

# Applied when a job's recorded model isn't in the table (unknown/older id).
_FALLBACK = (1.00, 5.00)


def price_for(model: str) -> tuple[float, float]:
    """(input_per_mtok, output_per_mtok) for a model id, tolerating date suffixes."""
    if model in PRICING:
        return PRICING[model]
    # match date-suffixed / aliased ids by known prefix (longest first)
    for known in sorted(PRICING, key=len, reverse=True):
        if model and model.startswith(known):
            return PRICING[known]
    return _FALLBACK


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost for a single (model, tokens) usage record."""
    in_rate, out_rate = price_for(model)
    return (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
