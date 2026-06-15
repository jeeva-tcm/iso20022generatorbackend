"""
OpenAI ChatGPT client for XML fix generation.
Using gpt-4o-mini: faster, cheaper, excellent at structured XML repairs.
"""
import os
import logging
from collections import OrderedDict

logger = logging.getLogger(__name__)

_client = None
_model = None

# Deterministic (temperature=0) completions are cached in-process: identical
# (model, system, user, max_tokens) requests — common when a batch contains many
# rows with the same error — return the first answer instead of re-hitting the
# API. Sampled (temperature>0) calls, used only by self-consistency retries, are
# never cached. Bounded LRU so memory can't grow unbounded.
_CACHE: "OrderedDict[tuple, str]" = OrderedDict()
_CACHE_MAX = 512


def _get_client():
    """Lazy-init OpenAI client. Returns None if key not configured."""
    global _client, _model
    if _client is not None:
        return _client

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    _model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        logger.warning("[OpenAIClient] OPENAI_API_KEY not set — fix suggestions unavailable.")
        return None

    try:
        from openai import OpenAI
        _client = OpenAI(api_key=api_key)
        logger.info(f"[OpenAIClient] Initialized with model={_model}")
        return _client
    except ImportError:
        logger.error("[OpenAIClient] 'openai' package not installed. Run: pip install openai")
        return None
    except Exception as e:
        logger.error(f"[OpenAIClient] Init failed: {e}")
        return None


def _record_cache(hit: bool) -> None:
    """Best-effort metric; never let telemetry break a completion."""
    try:
        from app.services import fix_metrics
        fix_metrics.record_llm_call(cache_hit=hit)
    except Exception:
        pass


def complete(system: str, user: str, max_tokens: int = 500,
             temperature: float = 0) -> tuple[str, bool]:
    """
    Call ChatGPT with a system + user prompt.

    max_tokens:  max output tokens (default 500 for complete XML elements).
    temperature: 0 (default) = deterministic + cached; >0 = sampled (used by
                 self-consistency retries) and never cached.

    Returns: (response_text, available)
        - available=False if API unreachable or OPENAI_API_KEY not set
    """
    client = _get_client()
    if client is None:
        return ("", False)

    cache_key = None
    if temperature == 0:
        cache_key = (_model, system, user, max_tokens)
        cached = _CACHE.get(cache_key)
        if cached is not None:
            _CACHE.move_to_end(cache_key)
            _record_cache(hit=True)
            return (cached, True)

    try:
        response = client.chat.completions.create(
            model=_model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
        if cache_key is not None and text:
            _CACHE[cache_key] = text
            _CACHE.move_to_end(cache_key)
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
        _record_cache(hit=False)
        return (text, True)
    except Exception as e:
        logger.error(f"[OpenAIClient] API call failed: {e}")
        return ("", False)
