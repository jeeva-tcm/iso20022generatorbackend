"""
Optional PII scrubbing for outbound LLM fix-suggestion prompts.

ISO 20022 payment fragments carry real IBANs and account numbers. When the
env flag FIXSUGGESTER_SCRUB_PII is enabled, high-risk value tokens are
replaced with reversible placeholders BEFORE the fragment is sent to the
external LLM, then restored in the model's returned fragment.

Disabled by default — so the existing flow is byte-for-byte unchanged unless
explicitly turned on. Deliberately conservative: only IBANs and long account
numbers are scrubbed. BICs are NOT scrubbed (the prompt intentionally feeds
the model real BICFIs as guidance) and structure/tag names are never touched.
"""
from __future__ import annotations

import os
import re
import logging

logger = logging.getLogger(__name__)

# IBAN: 2 letters + 2 check digits + 10..30 alphanumerics.
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
# Bare account-like runs: 8+ consecutive digits (run AFTER IBANs are masked).
_ACCT_RE = re.compile(r"\b\d{8,}\b")


def enabled() -> bool:
    return os.getenv("FIXSUGGESTER_SCRUB_PII", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def scrub(text: str) -> tuple[str, dict[str, str]]:
    """Replace IBANs / long account numbers with reversible placeholders.

    Returns (scrubbed_text, mapping) where mapping is placeholder -> original.
    When the flag is off (default) returns the text unchanged and an empty map.
    """
    if not text or not enabled():
        return text, {}
    try:
        mapping: dict[str, str] = {}

        def _mask(rx: re.Pattern, prefix: str, s: str) -> str:
            def repl(m: re.Match) -> str:
                val = m.group(0)
                for ph, orig in mapping.items():           # reuse one token per value
                    if orig == val:
                        return ph
                ph = f"__{prefix}_{len(mapping)}__"
                mapping[ph] = val
                return ph
            return rx.sub(repl, s)

        out = _mask(_IBAN_RE, "IBAN", text)
        out = _mask(_ACCT_RE, "ACCT", out)
        return out, mapping
    except Exception as e:                                  # never break the LLM path
        logger.debug(f"[PIIScrub] scrub failed, sending original: {e}")
        return text, {}


def restore(text: str, mapping: dict[str, str]) -> str:
    """Put the original values back wherever the model echoed a placeholder."""
    if not text or not mapping:
        return text
    for ph, orig in mapping.items():
        text = text.replace(ph, orig)
    return text
