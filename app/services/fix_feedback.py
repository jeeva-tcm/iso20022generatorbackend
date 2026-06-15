"""
Feedback capture + learned-fix surfacing for the AI fix suggester.

When a user accepts (or edits) a suggested fix, the (issue_code, tag, fragment)
is persisted to data/learned_fixes.json. Accepted fixes are then surfaced as
EXTRA context to the LLM fallback for the same (code, tag) — so a fix the model
got right once becomes cheap, high-signal guidance next time, and an edited fix
teaches the model the shape the user actually wanted.

This is additive context ONLY. It never overrides the deterministic pipeline,
never mutates the shipped knowledge base, and a read/write failure degrades to
"no learned examples" rather than affecting suggestion behaviour.
"""
from __future__ import annotations

import json
import os
import threading
import time
import logging

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_STORE = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "learned_fixes.json")
)
_MAX_ENTRIES = 2000          # keep the store bounded
_ACCEPTED = ("accept", "edit")


def _load() -> list:
    try:
        with open(_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def record_feedback(issue_code: str, tag: str, original_fragment: str,
                    fragment_xml: str, action: str,
                    edited_xml: str | None = None) -> bool:
    """Persist one accept/edit/reject event. Best-effort; returns success."""
    entry = {
        "ts": time.time(),
        "issue_code": issue_code or "",
        "tag": tag or "",
        "original_fragment": original_fragment or "",
        "fragment_xml": (edited_xml or fragment_xml or ""),
        "action": action or "",
    }
    try:
        with _lock:
            data = _load()
            data.append(entry)
            data = data[-_MAX_ENTRIES:]
            os.makedirs(os.path.dirname(_STORE), exist_ok=True)
            tmp = _STORE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STORE)          # atomic swap
        return True
    except Exception as e:
        logger.warning(f"[FixFeedback] record failed: {e}")
        return False


def accepted_examples(issue_code: str, tag: str, limit: int = 3) -> list[str]:
    """Up to `limit` distinct accepted/edited fragments for (code, tag),
    newest first. Returns [] on any error so callers can ignore the result."""
    try:
        data = _load()
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for e in reversed(data):
        if e.get("action") not in _ACCEPTED:
            continue
        if issue_code and e.get("issue_code") and e.get("issue_code") != issue_code:
            continue
        if tag and e.get("tag") and e.get("tag") != tag:
            continue
        frag = (e.get("fragment_xml") or "").strip()
        if frag and frag not in seen:
            seen.add(frag)
            out.append(frag)
        if len(out) >= limit:
            break
    return out
