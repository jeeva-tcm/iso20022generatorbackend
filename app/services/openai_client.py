"""
OpenAI ChatGPT client for XML fix generation.
Using gpt-4o-mini: faster, cheaper, excellent at structured XML repairs.
"""
import os
import logging

logger = logging.getLogger(__name__)

_client = None
_model = None


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


def complete(system: str, user: str, max_tokens: int = 500) -> tuple[str, bool]:
    """
    Call ChatGPT with a system + user prompt.

    max_tokens: max output tokens (default 500 for complete XML elements).
    temperature=0: deterministic output.

    Returns: (response_text, available)
        - available=False if API unreachable or OPENAI_API_KEY not set
    """
    client = _get_client()
    if client is None:
        return ("", False)

    try:
        response = client.chat.completions.create(
            model=_model,
            max_tokens=max_tokens,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user}
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
        return (text, True)
    except Exception as e:
        logger.error(f"[OpenAIClient] API call failed: {e}")
        return ("", False)
