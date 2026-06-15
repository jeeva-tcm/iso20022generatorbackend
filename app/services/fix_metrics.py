"""
Lightweight in-process metrics for the AI fix suggester.

No external dependencies; counters live for the process lifetime and are
exposed read-only via GET /fixes/metrics. Recording is purely observational
and best-effort — it must never alter suggestion behaviour, so every caller
treats a recording failure as a no-op.

Tracks: which confidence buckets are produced, closed-loop verify pass/fail,
LLM invocation + cache hit/miss, and self-consistency retry outcomes — enough
to answer "what % of errors do we actually fix, and where is the engine
spending LLM calls" without standing up a metrics backend.
"""
from __future__ import annotations

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: "defaultdict[str, int]" = defaultdict(int)


def _inc(key: str, n: int = 1) -> None:
    with _lock:
        _counters[key] += n


def record_suggestion(confidence: str, verified=None) -> None:
    _inc("suggestions_total")
    _inc(f"confidence.{confidence or 'none'}")
    if verified is True:
        _inc("verified_pass")
    elif verified is False:
        _inc("verified_fail")


def record_llm_invoked() -> None:
    _inc("llm_fallback_invoked")


def record_llm_call(cache_hit: bool) -> None:
    _inc("llm_calls_total")
    _inc("llm_cache_hit" if cache_hit else "llm_cache_miss")


def record_self_consistency(succeeded: bool) -> None:
    _inc("self_consistency_runs")
    _inc("self_consistency_success" if succeeded else "self_consistency_exhausted")


def snapshot() -> dict:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()
