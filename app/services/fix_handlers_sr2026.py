"""SR2026-specific fix handlers.

The shared fix engine lives in ``fix_suggester.py`` and serves BOTH releases —
~95% of fixes are identical across SR2025 and SR2026, so they stay there.

This module is the override surface for fixes that are UNIQUE to SR2026 (e.g. the
new SR2026 delta-rule error codes from app/sr2026/.../delta_rules/) or whose
behaviour must DIFFER from SR2025. ``handle()`` gets first refusal before the
shared deterministic chain: return a FixSuggestion to take over, or None to defer
to the shared engine.

To add a handler:

    from app.services.fix_suggester import FixSuggestion

    def handle(suggester, code, msg, root, fix_hint=""):
        if code == "SR2026_NEW_RULE_CODE":
            el = ...                                   # locate the element in root
            parent = el.getparent()
            return FixSuggestion(
                suggester._xpath_of(parent),
                suggester._serialize(parent),
                suggester._serialize(fixed_parent),    # built via suggester._copy(...)
                code, msg, "high")
        return None

`suggester` is the live FixSuggester instance, so its helpers (_serialize, _copy,
_xpath_of, _parse_xml) and KB loaders (already version-scoped to SR2026 — base
sr2025 KB + sr2026 overlay) are all available.
"""
from typing import Optional


def handle(suggester, code: str, msg: str, root, fix_hint: str = "") -> Optional[object]:
    # No SR2026-specific handlers yet. Add SR2026 delta-rule fixes above this
    # line; they only fire for SR2026 requests (this module is only consulted
    # when the active release is SR2026).
    return None
