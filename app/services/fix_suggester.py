"""
XSD-driven Inline Fix Suggester.

How it works:
  path = "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR"
    → last part  = missing/broken element  (UETR)
    → rest parts = parent path             (CdtTrfTxInf.PmtId)

Fix strategy (in order, deterministic first):
  1. Extract XML from fix_suggestion field (rules already have templates)
  2. Known element templates for common ISO 20022 elements
  3. XSD type map to build unknown elements
  4. LLM last resort only for semantic/expression rules (max_tokens=400)

Key improvements over v1:
  - _build_child receives and uses codelists + fix_hint for smart values
  - LLM fallback prompt includes fix_hint, rule description, and context
  - apply() uses indexed xpath to pick the exact element instance
  - suggest() checks for existing child sub-elements before appending to avoid
    creating duplicates or opening stale tags
  - _fix_value is enhanced: uses fix_hint codelist hints, charge_bearer,
    service_level, local_instrument, purpose codelists from rules data
"""
from __future__ import annotations

import logging
import re
import os
import json
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Dict, Any
from lxml import etree

from app.services.openai_client import complete
from app.services import fix_metrics, fix_feedback, pii_scrub

logger = logging.getLogger(__name__)

XS = "http://www.w3.org/2001/XMLSchema"

# Compiled XSD schemas cached by path for the closed-loop self-verify. Compiling
# a full ISO 20022 schema is expensive; verification only runs on the interactive
# single-suggest path, and after the first compile every check is a fast in-memory
# validate.
_XSD_SCHEMA_CACHE: dict = {}

# ── LLM negative cache ────────────────────────────────────────────────────────
# The positive side (successful completions) is cached in openai_client by
# (model, system, user). The *failure* side has no memory there because whether
# a completion is acceptable is only known AFTER _validate_llm_fragment runs in
# _llm_fallback. Without this, a field the LLM cannot fix re-hits the API on
# EVERY auto-fix round: temp-0 returns the (cached, still-invalid) answer, then
# self-consistency resamples temp 0.4/0.7 — uncached — again and again across
# all 6 rounds. We record prompts that already exhausted self-consistency with
# no valid fix and short-circuit them to the same low-confidence decline,
# skipping the API entirely. Keyed on (system, user) — identical to the positive
# cache — so it only fires when the broken fragment + context are byte-identical;
# any earlier fix that mutated the element changes the prompt and re-runs fresh,
# so convergence is unaffected. Bounded LRU; process-wide like the positive cache.
_LLM_NEG_CACHE: "OrderedDict[tuple, bool]" = OrderedDict()
_LLM_NEG_CACHE_MAX = 512

# ── Codelists loader (cached) ─────────────────────────────────────────────────

_CODELISTS_CACHE: Optional[Dict[str, Any]] = None

def _load_codelists() -> Dict[str, Any]:
    global _CODELISTS_CACHE
    if _CODELISTS_CACHE is not None:
        return _CODELISTS_CACHE
    base = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "sr2025", "resources", "codelists")
    )
    result: Dict[str, Any] = {}
    if not os.path.isdir(base):
        _CODELISTS_CACHE = result
        return result
    for fname in os.listdir(base):
        if not fname.endswith(".json"):
            continue
        key = fname[:-5]  # strip .json
        try:
            with open(os.path.join(base, fname), "r", encoding="utf-8-sig") as f:
                result[key] = json.load(f)
        except Exception as e:
            logger.warning(f"[FixSuggester] Could not load codelist {fname}: {e}")
    _CODELISTS_CACHE = result
    return result


def _codelist_codes(name: str) -> list:
    """Return list of valid codes from a codelist by name."""
    cl = _load_codelists().get(name, {})
    if isinstance(cl, list):
        return cl
    if isinstance(cl, dict):
        return cl.get("codes", []) or list(cl.get("currencies", {}).keys()) or []
    return []


def _ccy_precision(ccy: str) -> int:
    """Return the ISO 4217 decimal precision for a currency code by consulting
    currency.json. Falls back to 2 (the most common) when not found.
    Also returns the canonical set of valid currency codes as a side effect via
    the shared codelists cache — call _codelist_codes('currency') for that list."""
    ccy = (ccy or "").upper().strip()
    cl = _load_codelists().get("currency", {})
    if isinstance(cl, dict):
        currencies = cl.get("currencies", {})
        if isinstance(currencies, dict) and ccy in currencies:
            return int(currencies[ccy])
    return 2


def _valid_currency_codes() -> list:
    """Return the sorted list of valid ISO 4217 currency codes from currency.json."""
    cl = _load_codelists().get("currency", {})
    if isinstance(cl, dict):
        currencies = cl.get("currencies", {})
        if isinstance(currencies, dict):
            return sorted(currencies.keys())
    return _codelist_codes("currency")


def _parse_allowed_values(text: str) -> list:
    """Extract the allowed code values an error message enumerates, e.g.
    "The value 'CLRG' is not valid. It must be one of the following values :
    INDA, INGA." or "Expected is one of ( INDA, INGA )". Returns code-like
    tokens (2–11 uppercase alphanumerics) AFTER the 'one of' clause, so the
    offending value quoted earlier in the sentence is never mistaken for an
    allowed one. Empty when the message doesn't enumerate a closed set."""
    if not text:
        return []
    m = re.search(
        r"(?:must be one of|one of the following(?:\s+\w+)?\s*:?|expected is one of)\s*[:\(]?\s*(.+)$",
        text, re.I | re.S,
    )
    if not m:
        return []
    vals: list = []
    for tok in re.findall(r"'?([A-Z][A-Z0-9]{1,10})'?", m.group(1)):
        if tok not in vals:
            vals.append(tok)
    return vals


# ── AI Knowledge Base loader (cached, app-wide) ───────────────────────────────
# Loads ai_knowledge_base.json once. Provides:
#   - field_constraints  : per-field type, length, examples
#   - tag_templates      : per-message-type exact XML structures
#   - dependencies       : cross-field invariants the AI must respect
#   - dummy_data         : harvest-fallback values
#   - placeholder_resolution : how to resolve $VAR placeholders in templates

# Active SWIFT release for KB resolution. FixSuggester.suggest() sets this (from
# self._sr_version) before any KB access, so the module-level loaders below can
# overlay the SR2026 delta. Defaults to SR2025 (unchanged existing behaviour).
_ACTIVE_SR_VERSION: str = "SR2025"


def _set_active_sr_version(version: Optional[str]) -> None:
    global _ACTIVE_SR_VERSION
    _ACTIVE_SR_VERSION = version if version in ("SR2025", "SR2026") else "SR2025"


# Per-release deterministic-handler modules. The shared engine in this file
# serves both releases; each release may register its own handlers in a separate
# module, which get first refusal before the shared chain (see suggest()).
_VERSION_HANDLER_CACHE: Dict[str, Any] = {}
_VERSION_HANDLER_MODULES = {
    "SR2025": "fix_handlers_sr2025",
    "SR2026": "fix_handlers_sr2026",
}


def _version_handler(version: str):
    """Return the release's handler entry point — a callable
    handle(suggester, code, msg, root, fix_hint) -> Optional[FixSuggestion] —
    or None if the release defines no version-specific handlers."""
    v = version if version in _VERSION_HANDLER_MODULES else "SR2025"
    if v in _VERSION_HANDLER_CACHE:
        return _VERSION_HANDLER_CACHE[v]
    handler = None
    try:
        import importlib
        mod = importlib.import_module(f"app.services.{_VERSION_HANDLER_MODULES[v]}")
        handler = getattr(mod, "handle", None)
    except Exception as e:
        logger.debug(f"[FixSuggester] no version handler module for {v}: {e}")
    _VERSION_HANDLER_CACHE[v] = handler
    return handler


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Merge overlay onto a copy of base. Dicts merge key-by-key; any non-dict
    value (scalars AND lists) in overlay replaces the base value. Lets an SR2026
    delta override a single error code / field without restating the whole KB."""
    out = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json_first(paths: list) -> Dict[str, Any]:
    """Return the first JSON file that loads from `paths`, else {}."""
    last_err: Optional[Exception] = None
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                return json.load(f)
        except FileNotFoundError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            break
    if last_err is not None and not isinstance(last_err, FileNotFoundError):
        logger.warning(f"[FixSuggester] KB JSON load error for {paths}: {last_err}")
    return {}


# ── KB directory layout ───────────────────────────────────────────────────────
# All KB files live under one of two release folders: KB/sr2025/ (the base /
# foundation — every SR2025 file) and KB/sr2026/ (overrides + new SR2026 files).
# There are no loose files at the KB root.
_KB_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "resources", "KB"))


def _kb_dirs(version: str = None) -> list:
    """KB directories to consult, most-specific first. sr2025/ is the base; the
    active release's folder is checked first so it can override a file."""
    v = (version or _ACTIVE_SR_VERSION or "SR2025").lower()
    dirs = [os.path.join(_KB_ROOT, v)]
    if v != "sr2025":
        dirs.append(os.path.join(_KB_ROOT, "sr2025"))
    return dirs


def _kb_path(name: str, version: str = None) -> Optional[str]:
    """First existing path for KB file `name` across _kb_dirs(), else None.
    Resolves the active release's override, falling back to the sr2025 base."""
    for d in _kb_dirs(version):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


# Per-version cache: SR2025 = base KB; SR2026 = base KB + sr2026 delta overlay.
_KB_CACHE: Dict[str, Dict[str, Any]] = {}


def _load_knowledge_base() -> Dict[str, Any]:
    """ai_knowledge_base.json for the active SR version. The shared base
    (KB/ai_knowledge_base.json) carries the bulk of the rules; the active
    release's folder — KB/sr2025/ or KB/sr2026/ — overlays ONLY the entries that
    release adds or changes. An entry placed in a release folder is scoped to
    that release (it does NOT leak to the other); entries in the base apply to
    both."""
    version = _ACTIVE_SR_VERSION
    cached = _KB_CACHE.get(version)
    if cached is not None:
        return cached
    kb = _read_json_first([os.path.join(_KB_ROOT, "sr2025", "ai_knowledge_base.json")])
    if version == "SR2026":
        delta = _read_json_first([os.path.join(_KB_ROOT, "sr2026", "ai_knowledge_base.json")])
        if delta:
            kb = _deep_merge(kb, delta)
    if not kb:
        logger.warning("[FixSuggester] ai_knowledge_base.json load failed (empty).")
    _KB_CACHE[version] = kb
    return kb


def _kb_get(path: str, default: Any = None) -> Any:
    """Walk a dotted path through the knowledge base, e.g. 'dummy_data.ids.MsgId'."""
    cur: Any = _load_knowledge_base()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# ── KB-folder per-message validation KBs (cached) ─────────────────────────────
# resources/KB/<msg>_cbprplus_sr2025_validation_kb.json is the authoritative
# reference for each tag's datatype and expected value. Consulted to generate
# schema-valid leaf values (e.g. DtOfSgntr is ISODate, not 'SMPL-...').

_KB_FOLDER_CACHE: Dict[str, Any] = {}


def _kb_folder_name(msg_type: str, xml: str = "") -> Optional[str]:
    if not msg_type:
        return None
    if msg_type.startswith("pacs.009"):
        is_cov = ("cov" in msg_type.lower() or "cov" in xml.lower()
                  or "swift.cbprplus.cov" in xml.lower())
        if is_cov:
            return "pacs009_cov_cbprplus_sr2025_validation_kb.json"
        # ADV (advice) variant — e.g. msg_type "pacs.009.001.08_ADV". Routed to
        # its dedicated KB; falls back to the CORE KB for plain pacs.009.
        is_adv = ("adv" in msg_type.lower() or "adv" in xml.lower())
        if is_adv:
            return "pacs009_adv_cbprplus_sr2025_validation_kb.json"
        return "pacs009_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pacs.002"):
        return "pacs002_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pacs.003"):
        return "pacs003_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pacs.004"):
        return "pacs004_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pacs.008"):
        return "pacs008_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pacs.010"):
        return "pacs010_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.052"):
        return "camt052_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.053"):
        return "camt053_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.054"):
        return "camt054_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.055"):
        return "camt055_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.056"):
        return "camt056_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("camt.057"):
        return "camt057_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pain.001"):
        return "pain001_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pain.002"):
        return "pain002_cbprplus_sr2025_validation_kb.json"
    if msg_type.startswith("pain.008"):
        return "pain008_cbprplus_sr2025_validation_kb.json"
    return None


def _load_kb_folder(msg_type: str, xml: str = "") -> Dict[str, Any]:
    name = _kb_folder_name(msg_type, xml)
    if not name:
        return {}
    version = _ACTIVE_SR_VERSION
    cache_key = f"{version}:{name}"
    if cache_key in _KB_FOLDER_CACHE:
        return _KB_FOLDER_CACHE[cache_key]
    # These per-message KBs are list-based ("tags" arrays), so the active
    # release's file REPLACES the base wholesale rather than merging. For SR2026
    # the release-renamed file (sr2025→sr2026) takes precedence; we resolve it
    # FIRST because _kb_path falls back to the KB/sr2025/ base and would
    # otherwise return the base file before the rename branch is ever tried.
    # Falls back to the KB/sr2025/ base when no SR2026 override exists.
    path = None
    if version == "SR2026":
        path = _kb_path(name.replace("sr2025", "sr2026"))
    if not path:
        path = _kb_path(name)
    kb = _read_json_first([path]) if path else {}
    _KB_FOLDER_CACHE[cache_key] = kb
    return kb


def _value_for_datatype(dt: str) -> Optional[str]:
    """Generate a schema-valid leaf value for a KB-declared datatype."""
    from datetime import date, datetime, timezone
    dt = (dt or "").lower()
    if "datetime" in dt:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if "date" in dt:
        return date.today().isoformat()
    if "decimal" in dt or "amount" in dt:
        return None  # amount values must be preserved in-place; never replace with dummy
    if "uuid" in dt:
        return str(uuid.uuid4())
    return None


def _kb_folder_leaf_value(tag_name: str, msg_type: str, xml: str = "") -> Optional[str]:
    """Authoritative leaf value for a tag from the KB folder: expected_value,
    first valid_value, or a value derived from the declared datatype. None when
    the KB has no entry yielding a concrete value."""
    kb = _load_kb_folder(msg_type, xml)
    for entry in kb.get("tags", []):
        xml_el = entry.get("xml_element") or entry.get("tag", "").split("/")[-1]
        if xml_el != tag_name:
            continue
        ev = entry.get("expected_value")
        if ev:
            return str(ev)
        vv = entry.get("valid_values")
        if isinstance(vv, list) and vv:
            return str(vv[0])
        dt = entry.get("datatype")
        if dt:
            val = _value_for_datatype(dt)
            if val is not None:
                return val
            # Text types (Max35Text, Max140Text, etc.) have no datatype-only
            # recipe — fall through to the ai_knowledge_base field_constraints
            # example for this specific tag name (e.g. EndToEndId → "E2E-…").
            con = _kb_field_constraint(tag_name)
            ex = (con.get("example") or con.get("preferred")) if isinstance(con, dict) else None
            if ex:
                return str(ex)
        return None
    return None


def _cbpr_bizsvc_value(msg_type: str, xml: str = "") -> Optional[str]:
    """The correct CBPR+ AppHdr/BizSvc value for this message + variant, for the
    ACTIVE SWIFT release.

    pacs.009 splits into three business services:
      • CORE → swift.cbprplus.03 / .04   (SR2025 / SR2026)
      • ADV  → swift.cbprplus.adv.03 / .adv.04   (carries <Prtry>ADV</Prtry> / UndrlygFITxInf)
      • COV  → swift.cbprplus.cov.03 / .cov.04   (carries UndrlygCstmrCdtTrf)

    SR2026 moved CBPR+ onto the '.04' generation but the value is NOT uniform —
    it mirrors exactly what each SR2026 delta-validator enforces. This MUST stay
    in lockstep with app/sr2026/validation/delta_rules/* (and pacs.009 base.json):
    a mismatch makes the auto-fix loop oscillate forever.
      • pacs.003 / pain.008 / camt.055  → swift.cbprplus.03  (stayed on .03)
      • everything else (pacs.008/002/004, pain.001/002, camt.052/053/054/056/057,
        pacs.009 CORE) → swift.cbprplus.04
    SR2025 keeps the legacy generation: pacs.008 → .02 per its CBPR_R8 rule;
    camt.052-055/camt.057 → .03; camt.056/pain.*/pacs.010 → .02, resolved from the
    per-message KB 'expected_value'.
    """
    low = f"{msg_type} {xml}".lower()
    fam = (msg_type or "").lower()
    is_pacs009 = fam.startswith("pacs.009") or "financialinstitutioncredittransfer" in low
    is_cov = "cov" in fam or "undrlygcstmrcdttrf" in low or "swift.cbprplus.cov" in low
    is_adv = ("adv" in fam or "<prtry>adv</prtry>" in low
              or "undrlygfitxinf" in low or "swift.cbprplus.adv" in low)

    if _ACTIVE_SR_VERSION == "SR2026":
        if is_pacs009:
            if is_cov:
                return "swift.cbprplus.cov.04"
            if is_adv:
                return "swift.cbprplus.adv.04"
            return "swift.cbprplus.04"
        # pacs.003 / pain.008 / camt.055 stayed on the .03 service in SR2026.
        if any(k in fam for k in ("pacs.003", "pain.008", "camt.055")):
            return "swift.cbprplus.03"
        return "swift.cbprplus.04"

    # ── SR2025 (and earlier releases) ──
    if is_pacs009:
        if is_cov:
            return "swift.cbprplus.cov.03"
        if is_adv:
            return "swift.cbprplus.adv.03"
        return "swift.cbprplus.03"
    # All other message types: trust the per-message KB's expected_value.
    return _kb_folder_leaf_value("BizSvc", msg_type, xml)


def _kb_folder_structural_hints(tag_names: list[str], code: str,
                                msg_type: str, xml: str = "") -> list[str]:
    """Pull authoritative STRUCTURE context for the given elements from the
    per-message KB JSON in resources/KB (e.g. pacs.008.json): each element's
    canonical xpath (so the LLM sees correct nesting/parentage), cardinality,
    whether it's mandatory, and any matching error's documented possible_fixes.

    This is what lets the AI "check the KB before fixing" — it hands the model
    the exact shape the element must take per the knowledge base rather than
    leaving it to guess. Returns a list of short context lines (possibly empty).
    """
    kb = _load_kb_folder(msg_type, xml)
    entries = kb.get("tags", []) if isinstance(kb, dict) else []
    if not entries:
        return []
    wanted = {t for t in tag_names if t}
    lines: list[str] = []
    seen: set = set()
    for entry in entries:
        xml_el = entry.get("xml_element") or entry.get("tag", "").split("/")[-1]
        if xml_el not in wanted or xml_el in seen:
            continue
        seen.add(xml_el)
        bits = [f"<{xml_el}>"]
        if entry.get("xpath"):
            bits.append(f"path={entry['xpath']}")
        if entry.get("occurrence"):
            bits.append(f"occurrence={entry['occurrence']}")
        if entry.get("mandatory") is not None:
            bits.append(f"mandatory={entry['mandatory']}")
        if entry.get("datatype"):
            bits.append(f"datatype={entry['datatype']}")
        lines.append("  " + " | ".join(bits))
        # Surface the documented fix for the matching error code, if any.
        for err in entry.get("errors", []) or []:
            if code and err.get("error_code") not in (code, "") and err.get("error_id") != code:
                continue
            for pf in (err.get("possible_fixes") or [])[:2]:
                lines.append(f"    fix: {pf}")
            break
    return lines


# ── Enterprise KB (swift_mx_enterprise_llm_kb.json) — cached, app-wide ───────
# 17-module knowledge base with per-message field_rules, dependency_rules,
# error_resolution_rules, tag_templates, and shared dummy data.

_ENTERPRISE_KB_CACHE: Optional[Dict[str, Any]] = None
_ENTERPRISE_MODULE_INDEX: Dict[str, Any] = {}


def _load_enterprise_kb() -> Dict[str, Any]:
    global _ENTERPRISE_KB_CACHE, _ENTERPRISE_MODULE_INDEX
    if _ENTERPRISE_KB_CACHE is not None:
        return _ENTERPRISE_KB_CACHE
    # Resolve the active release's enterprise KB (sr2026 override if present,
    # else the sr2025 base).
    path = _kb_path("swift_mx_enterprise_llm_kb.json")
    _ENTERPRISE_KB_CACHE = _read_json_first([path]) if path else {}
    if not _ENTERPRISE_KB_CACHE:
        logger.warning("[FixSuggester] swift_mx_enterprise_llm_kb.json load failed (empty).")
    for mod in _ENTERPRISE_KB_CACHE.get("modules", []):
        name = mod.get("module_name")
        if name:
            _ENTERPRISE_MODULE_INDEX[name] = mod
    return _ENTERPRISE_KB_CACHE


def _enterprise_module(msg_type: str) -> Dict[str, Any]:
    """Return the enterprise KB module for this message type, or {}."""
    _load_enterprise_kb()
    if not msg_type:
        return {}
    if msg_type in _ENTERPRISE_MODULE_INDEX:
        return _ENTERPRISE_MODULE_INDEX[msg_type]
    family = ".".join(msg_type.split(".")[:2]) if "." in msg_type else msg_type
    return _ENTERPRISE_MODULE_INDEX.get(family, {})


def _enterprise_field_constraint_any(tag_name: str) -> Dict[str, Any]:
    """Search all enterprise KB modules for a field_rules entry for tag_name."""
    _load_enterprise_kb()
    for mod in _ENTERPRISE_MODULE_INDEX.values():
        rules = mod.get("field_rules", {})
        if tag_name in rules:
            return rules[tag_name]
    return {}


def _enterprise_tag_template_any(tag_name: str, msg_type: str = "") -> Optional[str]:
    """Return the enterprise KB tag template for tag_name, msg-type-specific first."""
    _load_enterprise_kb()
    mod = _enterprise_module(msg_type)
    if mod:
        tmpl = mod.get("tag_templates", {}).get(tag_name)
        if tmpl:
            return tmpl
    # Fall back: search all modules
    for m in _ENTERPRISE_MODULE_INDEX.values():
        tmpl = m.get("tag_templates", {}).get(tag_name)
        if tmpl:
            return tmpl
    return None


def _enterprise_error_resolution_any(code: str) -> Dict[str, Any]:
    """Search all enterprise KB modules for an error_resolution_rules entry."""
    _load_enterprise_kb()
    for mod in _ENTERPRISE_MODULE_INDEX.values():
        rules = mod.get("error_resolution_rules", {})
        if code in rules:
            return rules[code]
    return {}


def _enterprise_date_fix_rules() -> Dict[str, Any]:
    """Return global date_fix_rules from the enterprise KB."""
    kb = _load_enterprise_kb()
    return kb.get("global_rules", {}).get("date_fix_rules", {})


def _enterprise_dependencies_all(dep_kind: str) -> list:
    """Return all dependency rules of dep_kind across all modules, deduplicated by id."""
    _load_enterprise_kb()
    seen_ids: set = set()
    result: list = []
    for mod in _ENTERPRISE_MODULE_INDEX.values():
        for dep in mod.get("dependency_rules", {}).get(dep_kind, []) or []:
            dep_id = dep.get("id", "")
            if dep_id and dep_id not in seen_ids:
                seen_ids.add(dep_id)
                result.append(dep)
            elif not dep_id:
                result.append(dep)
    return result


def _enterprise_shared(path: str, default: Any = None) -> Any:
    """Walk a dotted path through shared_resources of the enterprise KB."""
    kb = _load_enterprise_kb()
    cur: Any = kb.get("shared_resources", {})
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# ── Syntactic / Lexical Validation KB (cached) ────────────────────────────────
# resources/KB/syntactic_lexical_cbprplus_validation_kb.json is a companion
# knowledge base for detecting and repairing SYNTACTIC errors (character encoding,
# well-formedness, whitespace, numeric/date lexical form) that are checked BEFORE
# schema/business validation can even run. It is loaded once and consulted by
# the LLM fallback and any deterministic syntactic repair paths.

_SYNTACTIC_KB_CACHE: Optional[Dict[str, Any]] = None


def _load_syntactic_kb() -> Dict[str, Any]:
    global _SYNTACTIC_KB_CACHE
    if _SYNTACTIC_KB_CACHE is not None:
        return _SYNTACTIC_KB_CACHE
    path = _kb_path("syntactic_lexical_cbprplus_validation_kb.json")
    _SYNTACTIC_KB_CACHE = _read_json_first([path]) if path else {}
    if not _SYNTACTIC_KB_CACHE:
        logger.warning("[FixSuggester] Syntactic KB load failed (empty).")
    return _SYNTACTIC_KB_CACHE


def _syntactic_kb_get(path: str, default: Any = None) -> Any:
    """Walk a dotted path through the syntactic KB."""
    cur: Any = _load_syntactic_kb()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def _syntactic_error_codes() -> set:
    """Return the set of error_codes defined in the syntactic KB's error_code_catalogue."""
    catalogue = _syntactic_kb_get("error_code_catalogue", {})
    if isinstance(catalogue, dict):
        return set(catalogue.keys())
    return set()


def _syntactic_fix_hint(code: str) -> str:
    """Return a one-line fix hint from the syntactic KB for a given error code."""
    entry = _syntactic_kb_get(f"error_code_catalogue.{code}", {})
    if isinstance(entry, dict):
        return (entry.get("deterministic_fix") or entry.get("fix") or
                entry.get("description") or "")
    return ""


def _kb_msg_family(msg_type: str) -> str:
    """Reduce 'pacs.008.001.08' → 'pacs.008'."""
    if not msg_type:
        return ""
    parts = msg_type.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else msg_type


# ── Per-message KB context (resources/KB/<family>.json) ───────────────────────
# Rich, human-authored catalogue keyed by tag/xpath: for every error it lists a
# documented set of `possible_fixes`, plus cross-tag dependency rules. Used to:
#   • drive deterministic AI fixes (extract the recommended literal value), and
#   • surface the documented fix recipes to the LLM fallback as context, and
#   • feed the validator's KB-driven dependency-rule checks.

class _KBContext:
    _cache: Dict[str, "_KBContext"] = {}

    def __init__(self, family: str):
        self.family = family
        self.by_tag: Dict[str, list] = {}    # leaf local-name → [error records]
        self.by_code: Dict[str, list] = {}   # error_code      → [error records]
        self.valid_by_tag: Dict[str, list] = {}  # leaf local-name → [valid enum codes]
        self.dependency_rules: list = []
        self.formal_rules: list = []
        self.insertion_order: Dict[str, list] = {}  # parent tag → ordered child tags
        self._load()

    # Common KB files that are NOT per-message context catalogues.
    _COMMON_KB_FILES = {"ai_knowledge_base.json", "swift_mx_enterprise_llm_kb.json"}

    @classmethod
    def _find_file(cls, kb_dir: str, family: str) -> Optional[str]:
        """
        Locate the per-message KB file for `family` (e.g. 'camt.054'), tolerating
        any filename convention:
          • exact  <family>.json            (e.g. pacs.008.json)
          • dotted  *<family>*.json          (e.g. CBPRPlus_camt.054.001.08_..._KB.json)
          • dotless *<familynodot>*.json      (e.g. pacs009_cbprplus_..._kb.json)
        The shared common KBs are never matched here.
        """
        if not family or not os.path.isdir(kb_dir):
            return None
        exact = os.path.join(kb_dir, f"{family}.json")
        if os.path.exists(exact):
            return exact
        # Match either the dotted family ('pacs.009') or its dotless form
        # ('pacs009'), since KB files use both naming conventions.
        tokens = {family, family.replace(".", "")}
        cands = [
            fn for fn in os.listdir(kb_dir)
            if fn.endswith(".json")
            and fn not in cls._COMMON_KB_FILES
            and any(tok in fn for tok in tokens)
        ]
        if cands:
            cands.sort(key=len)  # prefer the most specific/shortest match
            return os.path.join(kb_dir, cands[0])
        return None

    def _load(self) -> None:
        # Search the active release's folder first, then the sr2025 base.
        path = None
        for kb_dir in _kb_dirs():
            path = self._find_file(kb_dir, self.family)
            if path:
                break
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning(f"[KBContext] Failed to load KB for {self.family}: {e}")
            return
        # KB files come from several authors with slightly different shapes;
        # coerce list-valued sections defensively (some store them as dicts).
        def _as_list(v):
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return list(v.values())
            return []

        for t in _as_list(data.get("tags")):
            if not isinstance(t, dict):
                continue
            leaf = t.get("xml_element") or (t.get("tag", "").split("/")[-1])
            # Tag-level enum allow-list (e.g. CdtDbtInd → [CRDT, DBIT]).
            vv = t.get("valid_values")
            if leaf and isinstance(vv, list) and vv and leaf not in self.valid_by_tag:
                self.valid_by_tag[leaf] = [str(c) for c in vv]
            for e in _as_list(t.get("errors")):
                if not isinstance(e, dict):
                    continue
                rec = {
                    "error_id": e.get("error_id", ""),
                    "error_code": e.get("error_code", ""),
                    "severity": e.get("severity", ""),
                    "description": e.get("description", ""),
                    "possible_fixes": e.get("possible_fixes", []) or [],
                    "tag": t.get("tag", ""),
                    "xpath": t.get("xpath", ""),
                    "leaf": leaf,
                }
                if leaf:
                    self.by_tag.setdefault(leaf, []).append(rec)
                if rec["error_code"]:
                    self.by_code.setdefault(rec["error_code"], []).append(rec)
                # Enum codes are sometimes carried on the error entry instead.
                ev = e.get("valid_values")
                if leaf and isinstance(ev, list) and ev and leaf not in self.valid_by_tag:
                    self.valid_by_tag[leaf] = [str(c) for c in ev]
        self.dependency_rules = _as_list(data.get("cross_tag_dependency_rules"))
        self.formal_rules = (_as_list(data.get("cbpr_plus_formal_rules"))
                             + _as_list(data.get("iso_20022_rules")))
        io = data.get("tag_insertion_order")
        if isinstance(io, dict):
            self.insertion_order = {k: v for k, v in io.items()
                                    if isinstance(v, list) and v}

    def _matching_records(self, code: str = "", leaf: str = "") -> list:
        """Records matching the issue's error_code first, then the element tag."""
        recs = list(self.by_code.get(code, [])) if code else []
        if leaf:
            for r in self.by_tag.get(leaf, []):
                if r not in recs:
                    recs.append(r)
        return recs

    def possible_fixes(self, code: str = "", leaf: str = "") -> list:
        out: list = []
        for r in self._matching_records(code, leaf):
            for fx in r.get("possible_fixes", []):
                if fx not in out:
                    out.append(fx)
        return out

    def valid_codes(self, leaf: str) -> list:
        """KB-documented enum allow-list for `leaf` (e.g. ['CRDT','DBIT'])."""
        return self.valid_by_tag.get(leaf, [])

    def literal_value(self, leaf: str, code: str = "") -> Optional[str]:
        """
        Extract a concrete, ready-to-use replacement value for `leaf` from the
        documented possible_fixes (e.g. BizSvc → 'swift.cbprplus.03',
        ChrgBr → 'SLEV'). Placeholder-bearing recipes ({...}, generated UUIDs)
        are skipped — those are handled by the deterministic generators.

        Only records for the SAME element (`leaf`) are considered — a literal
        value is element-specific, so we must never borrow a value documented
        for a different tag (some KB error_codes are shared across tags).
        Records whose error_code matches the issue are tried first.
        """
        recs = self.by_tag.get(leaf, [])
        ordered = sorted(recs, key=lambda r: 0 if r.get("error_code") == code else 1)
        for r in ordered:
            for fx in r.get("possible_fixes", []):
                val = _extract_literal_from_fix(fx, leaf)
                if val:
                    return val
        return None

    @classmethod
    def get(cls, msg_type: str) -> Optional["_KBContext"]:
        family = _kb_msg_family(msg_type)
        if not family:
            return None
        # pacs.009 ships variant-specific KBs (CORE / COV / ADV) that all share
        # the 'pacs.009' family and namespace, so _find_file would pick the
        # shortest (CORE) filename for every variant. Disambiguate from the
        # msg_type when it names the variant (e.g. 'pacs.009.001.08_COV'); fall
        # back to CORE — the prior behaviour — when it does not.
        key = family
        if family == "pacs.009":
            mt = (msg_type or "").lower()
            if "cov" in mt:
                key = "pacs009_cov"
            elif "adv" in mt:
                key = "pacs009_adv"
        if key not in cls._cache:
            cls._cache[key] = _KBContext(key)
        ctx = cls._cache[key]
        # Treat an empty context (no file) as unavailable
        return ctx if (ctx.by_tag or ctx.dependency_rules) else None


def _extract_literal_from_fix(fix_text: str, leaf: str) -> Optional[str]:
    """
    Pull a concrete literal value out of a documented fix instruction.

    Handles, in order:
      1. ``<Leaf ...>VALUE</Leaf>`` inline element       → VALUE
      2. ``'VALUE'`` / ``"VALUE"`` quoted literal        → VALUE
      3. ``one of: A, B, C`` enumerations                → A (first / preferred)
    Any candidate containing a ``{placeholder}`` is rejected (it needs runtime
    harvesting, not a literal).
    """
    if not fix_text:
        return None

    def _ok(v: str) -> bool:
        v = (v or "").strip()
        return bool(v) and "{" not in v and "}" not in v and "..." not in v

    # 1. Inline element form: <Leaf>VALUE</Leaf>
    m = re.search(rf"<{re.escape(leaf)}\b[^>]*>([^<]+)</{re.escape(leaf)}>", fix_text)
    if m and _ok(m.group(1)):
        return m.group(1).strip()

    # 2. Quoted literal (prefer ones that look like real values: codes / dotted ids).
    #    GUARD: the alternation regex can pair the CLOSING quote of one literal
    #    with the OPENING quote of the next — e.g. "Replace 'Z' with '+00:00'"
    #    yields the span "' with '" → "with", an English filler word, NOT a value.
    #    Two defences: (a) reject candidates with internal whitespace (real ISO
    #    literals — codes, BICs, dates, ids — never contain spaces), and (b)
    #    reject a small stop-word list of connectives that show up in fix prose.
    _STOPWORDS = {
        "with", "the", "and", "for", "use", "set", "add", "to", "from", "into",
        "this", "that", "value", "element", "field", "tag", "replace", "remove",
    }
    for qm in re.finditer(r"'([^']{2,40})'|\"([^\"]{2,40})\"", fix_text):
        cand = (qm.group(1) or qm.group(2) or "").strip()
        if (_ok(cand)
                and " " not in cand                       # no internal whitespace
                and cand.lower() not in _STOPWORDS         # not a connective word
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", cand)):
            return cand

    # 3. "one of: A, B, C" enumeration → first option
    m = re.search(r"one of:\s*([A-Z0-9]{2,}(?:\s*,\s*[A-Z0-9]{2,})+)", fix_text)
    if m:
        first = m.group(1).split(",")[0].strip()
        if _ok(first):
            return first
    return None


# ── Cross-message KB fix index (built once) ───────────────────────────────────
# The per-message validation KBs (resources/KB/<msg>_cbprplus_*.json) each
# document `possible_fixes` recipes for the errors that message can raise. A
# recipe for a tag shared across families (e.g. a charset/datetime/BIC repair)
# is, however, only ever consulted for the message it happens to be filed under
# (_KBContext is per-family). So an error the AI fixes in pacs.008 — because
# pacs.008's KB documents it — can go unfixed in camt.054 purely because camt.054's
# KB lacks that entry. This index aggregates every per-message KB's recipes so the
# LLM fallback can borrow a sibling message's documented fix when the current
# message's KB is silent. RECIPE TEXT ONLY — never literal values (those are
# element/message-specific; borrowing them is unsafe) — and only as advisory LLM
# context that still passes _validate_llm_fragment + re-validation downstream.
_CROSS_KB_INDEX: Optional[Dict[str, Dict[Any, list]]] = None


def _build_cross_kb_index() -> Dict[str, Dict[Any, list]]:
    global _CROSS_KB_INDEX
    if _CROSS_KB_INDEX is not None:
        return _CROSS_KB_INDEX
    by_code_leaf: Dict[Any, list] = {}
    by_leaf: Dict[Any, list] = {}
    valid_leaf: Dict[Any, list] = {}     # leaf → deduped enum allow-list across families
    # Aggregate per-message KBs from the active release's folder and the sr2025
    # base; the active folder's file for a family wins (added last).
    files_by_dir: "OrderedDict[str, str]" = OrderedDict()
    for kb_dir in reversed(_kb_dirs()):  # base first, active last so active overrides
        try:
            for f in os.listdir(kb_dir):
                if (f.endswith("_validation_kb.json")
                        and f not in _KBContext._COMMON_KB_FILES
                        and "syntactic" not in f):
                    files_by_dir[f] = kb_dir
        except Exception:
            continue
    for fn, kb_dir in files_by_dir.items():
        # Derive a family key the _KBContext file-matcher recognises
        # ('pacs008', 'pacs009_cov', 'camt054', …).
        key = fn.split("_cbprplus")[0] if "_cbprplus" in fn \
            else fn.replace("_validation_kb.json", "")
        try:
            ctx = _KBContext(key)
        except Exception:
            continue
        for recs in ctx.by_tag.values():
            for r in recs:
                leaf = r.get("leaf", "")
                code = r.get("error_code", "")
                for fx in r.get("possible_fixes", []) or []:
                    if not fx or "{" in fx:        # skip placeholder recipes
                        continue
                    pair = (key, fx)
                    if leaf:
                        by_leaf.setdefault(leaf, []).append(pair)
                        if code:
                            by_code_leaf.setdefault((code, leaf), []).append(pair)
        for leaf, codes in ctx.valid_by_tag.items():
            bucket = valid_leaf.setdefault(leaf, [])
            for c in codes:
                if c not in bucket:
                    bucket.append(c)
    _CROSS_KB_INDEX = {"code_leaf": by_code_leaf, "leaf": by_leaf,
                       "valid_leaf": valid_leaf}
    return _CROSS_KB_INDEX


def _cross_message_possible_fixes(code: str, leaf: str, limit: int = 4) -> list:
    """Documented `possible_fixes` for (code, leaf) drawn from OTHER message
    families' KBs. Code+leaf matches are preferred over leaf-only. Each recipe is
    prefixed with its source family for provenance. Returns [] when nothing
    applies. Only meaningful to call when the current message's KB had no recipe
    — at that point the current family contributes nothing here either, so no
    self-pollution check is needed."""
    if not leaf:
        return []
    idx = _build_cross_kb_index()
    out: list = []
    seen: set = set()

    def _take(pairs: list) -> None:
        for fam, fx in pairs:
            key = fx.strip()
            if key and key not in seen:
                seen.add(key)
                out.append(f"[{fam}] {fx}")
                if len(out) >= limit:
                    return

    if code:
        _take(idx["code_leaf"].get((code, leaf), []))
    if len(out) < limit:
        _take(idx["leaf"].get(leaf, []))
    return out


def _cross_message_valid_codes(leaf: str) -> list:
    """Enum allow-list for `leaf` aggregated across ALL per-message KBs. Used as a
    deterministic, offline (no-LLM) source for fixing an invalid enum value when
    neither ai_knowledge_base nor the current message's own KB documents the
    allowed set. Enum allow-lists for a given leaf (e.g. ChrgBr → DEBT/CRED/SHAR/
    SLEV, CdtDbtInd → CRDT/DBIT) are message-agnostic, so borrowing them is safe."""
    if not leaf:
        return []
    return list(_build_cross_kb_index()["valid_leaf"].get(leaf, []))


def _detect_family_from_tree(root: "etree._Element") -> str:
    """Find the payload message family (e.g. 'pacs.008') from any element's
    namespace in the tree — works for both AppHdr (head.001) and Document nodes."""
    if root is None:
        return ""
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        ns = etree.QName(el.tag).namespace or ""
        m = re.search(r"((?:pacs|pain|camt|sese|reda|acmt)\.\d{2,3})\.\d{3}\.\d{2}", ns)
        if m:
            return m.group(1)
    return ""


def _kb_tag_template(tag_name: str, msg_type: str) -> Optional[str]:
    """
    Return the message-specific template for a tag, else the default.
    Templates may contain $PLACEHOLDERS that resolve at apply time.
    """
    templates = _kb_get(f"tag_templates.{tag_name}", {})
    if not isinstance(templates, dict):
        return _enterprise_tag_template_any(tag_name, msg_type)
    family = _kb_msg_family(msg_type)
    if family and family in templates:
        return templates[family]
    result = templates.get("default")
    if result is None:
        # Enterprise KB has module-specific tag_templates with $PLACEHOLDER support
        result = _enterprise_tag_template_any(tag_name, msg_type)
    return result


def _kb_field_constraint(tag_name: str, parent_tag: Optional[str] = None) -> Dict[str, Any]:
    """
    Return the field_constraints entry for a tag, or a derived one.

    Falls back via heuristic for tags not explicitly listed:
      *Amt        → Amount type
      *Dt         → Date type
      *DtTm       → DateTime type
      *Id (Max35) → Max35Text
    """
    if tag_name == "Cd" and parent_tag == "Sts":
        direct = _kb_get("field_constraints.Sts", None)
        if isinstance(direct, dict) and direct:
            return direct

    direct = _kb_get(f"field_constraints.{tag_name}", None)
    if isinstance(direct, dict) and direct:
        return direct

    tn = tag_name
    # Suffix-based heuristics — return a synthesized constraint
    # Max35Text ID fields — explicit max_length=35 so length overflow fires correctly
    if tn in ("MsgId", "BizMsgIdr", "EndToEndId", "InstrId", "TxId",
              "OrgnlMsgId", "OrgnlEndToEndId", "OrgnlInstrId", "OrgnlTxId",
              "PmtInfId", "ClrSysRef", "MsgDefIdr"):
        return {"type": "Max35Text", "max_length": 35}
    if tn.endswith("Amt") or tn == "Amt":
        return {"type": "Amount", "example": "1000.00"}
    # AppHdr/CreDt is ISODateTime (dateTime), not ISODate — must be treated as
    # DateTime so value fixes generate a full timestamp with timezone offset.
    if tn == "CreDt":
        return {"type": "DateTime", "example": "2026-05-27T10:00:00+00:00"}
    if tn.endswith("DtTm"):
        return {"type": "DateTime", "example": "2026-05-27T10:00:00+00:00"}
    if tn.endswith("Dt") and len(tn) > 2:
        return {"type": "Date", "example": "2026-05-27"}
    if tn.endswith("BICFI") or tn == "BICFI" or tn.endswith("AnyBIC"):
        return {"type": "BICFI", "max_length": 11, "min_length": 8, "example": "DEUTDEFFXXX"}
    if tn.endswith("IBAN") or tn == "IBAN":
        return {"type": "IBAN", "max_length": 34, "min_length": 15}
    if tn.endswith("LEI") or tn == "LEI":
        return {"type": "LEI", "max_length": 20, "min_length": 20}
    if tn == "Ctry" or tn.endswith("CtryOfRes") or tn.endswith("CntryCd"):
        return {"type": "Country", "max_length": 2, "min_length": 2, "example": "US"}
    if tn == "Ccy" or tn.endswith("Ccy"):
        return {"type": "Currency", "max_length": 3, "min_length": 3, "example": "USD"}
    if tn == "UETR":
        return {"type": "UUID", "max_length": 36, "min_length": 36}
    # Fall back to the enterprise KB (covers all 17 message-type modules)
    ent = _enterprise_field_constraint_any(tag_name)
    if ent:
        return ent
    return {}


# ── Rules index (cached per message type) ─────────────────────────────────────
# Loads rules JSON for a given message type and exposes lookups by:
#   - rule_id      (exact match)
#   - leaf path    (last 2-3 segments of mandatory_fields[*])
#   - leaf tag     (last segment of mandatory_fields[*])
#
# Each entry preserves the original `fix` string from the rules file, which
# contains the EXACT tag structure for that specific message type.

class _RulesIndex:
    _cache: dict[str, "_RulesIndex"] = {}

    def __init__(self, msg_type: str):
        self.msg_type = msg_type
        self.by_rule_id: dict[str, dict] = {}
        self.by_leaf_path: dict[str, list[dict]] = {}  # "Cdtr.PstlAdr.Ctry" → [rules]
        self.by_leaf_tag: dict[str, list[dict]] = {}   # "Ctry"             → [rules]
        self._load()

    def _load(self) -> None:
        base = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "resources", "rules")
        )
        if not os.path.isdir(base):
            return

        # Load in priority order so the most-specific message rules override
        # more-general ones. Last write wins for by_rule_id.
        # global → cbpr_common → family (pacs/pain/camt) → specific (pacs.008)
        load_order: list[str] = []
        parts = self.msg_type.split(".")
        load_order.append("global.json")
        load_order.append("cbpr_common.json")
        if parts:
            load_order.append(f"{parts[0]}.json")            # pacs.json
        if len(parts) >= 2:
            load_order.append(f"{parts[0]}.{parts[1]}.json")  # pacs.008.json
        # Also load any exact match file (e.g. pacs.009.001.08_COV.json)
        full_name = self.msg_type + ".json"
        if full_name not in load_order:
            load_order.append(full_name)

        for fname in load_order:
            path = os.path.join(base, fname)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    rules = json.load(f)
                if isinstance(rules, list):
                    for rule in rules:
                        self._index_rule(rule)
            except Exception as e:
                logger.warning(f"[RulesIndex] Failed to load {fname}: {e}")

    def _index_rule(self, rule: dict) -> None:
        rid = rule.get("rule_id", "")
        if rid:
            self.by_rule_id[rid] = rule

        # Index by mandatory_fields paths
        for path in rule.get("mandatory_fields", []) or []:
            if not isinstance(path, str):
                continue
            parts = [p for p in path.split(".") if p]
            if not parts:
                continue
            leaf = parts[-1]
            self.by_leaf_tag.setdefault(leaf, []).append(rule)
            # Index by progressively-shorter suffixes for fuzzy lookup
            for n in (3, 2):
                if len(parts) >= n:
                    suffix = ".".join(parts[-n:])
                    self.by_leaf_path.setdefault(suffix, []).append(rule)

        # Also index by selector if it captures a tag at the end
        sel = rule.get("selector", "")
        if isinstance(sel, str):
            m = re.search(r'\\\.(\w+)\$$', sel)
            if m:
                self.by_leaf_tag.setdefault(m.group(1), []).append(rule)

    def lookup(self, rule_id: str = "",
                path_parts: Optional[list[str]] = None,
                leaf_tag: str = "") -> Optional[dict]:
        """
        Lookup the most-specific matching rule:
          1. exact rule_id match
          2. exact path suffix match (last 3 parts, then last 2)
          3. leaf tag match (must have a `fix` field to be useful)
        Returns the rule dict, or None.
        """
        if rule_id and rule_id in self.by_rule_id:
            return self.by_rule_id[rule_id]

        if path_parts:
            for n in (3, 2):
                if len(path_parts) >= n:
                    key = ".".join(path_parts[-n:])
                    candidates = self.by_leaf_path.get(key, [])
                    for r in candidates:
                        if r.get("fix"):
                            return r
                    if candidates:
                        return candidates[0]

        if leaf_tag:
            candidates = self.by_leaf_tag.get(leaf_tag, [])
            for r in candidates:
                if r.get("fix"):
                    return r
            if candidates:
                return candidates[0]
        return None

    @classmethod
    def get(cls, msg_type: str) -> Optional["_RulesIndex"]:
        if not msg_type:
            return None
        if msg_type not in cls._cache:
            cls._cache[msg_type] = _RulesIndex(msg_type)
        return cls._cache[msg_type]


def _verify_iban_mod97(iban: str) -> bool:
    """Return True when the IBAN passes ISO 13616 MOD-97-10 check digit verification."""
    try:
        iban = iban.strip().upper()
        if not re.match(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$', iban):
            return False
        rearranged = iban[4:] + iban[:4]
        numeric = ''.join(str(ord(c) - 55) if c.isalpha() else c for c in rearranged)
        return int(numeric) % 97 == 1
    except Exception:
        return False


def _dedupe_id_value(text: str) -> str:
    """Return a same-length, distinct value derived from `text` for de-duplicating
    a repeated ID — increments a trailing numeric run (zero-padded, same width),
    or tweaks the last character if there is no trailing digit run.

    Same length matters: a "-2" suffix can exceed tight max-length constraints
    (e.g. InstrId capped at 16 chars) when the original already uses the max.
    """
    m = re.search(r"(\d+)$", text)
    if m:
        num_str = m.group(1)
        new_num = str(int(num_str) + 1).zfill(len(num_str))
        if len(new_num) > len(num_str):
            new_num = new_num[-len(num_str):]
        return text[:m.start()] + new_num
    if len(text) >= 1:
        return text[:-1] + ("0" if text[-1] != "0" else "1")
    return text


def _iban_for_ccy(root: Optional["etree._Element"],
                  el: Optional["etree._Element"] = None) -> str:
    """Return a dummy IBAN whose country currency matches the nearest Ccy attribute.

    Lookup order:
      1. Scan amount elements (IntrBkSttlmAmt, InstdAmt, TtlIntrBkSttlmAmt, Amt)
         for a Ccy attribute; use the first one found.
      2. Map currency → IBAN country via dummy_data.currencies_by_country KB entry.
      3. Pick a matching IBAN from dummy_data.ibans[country_code].
      4. Fall back to KB default IBAN, then hard-coded EUR DE IBAN.
    """
    _CCY_AMT_TAGS = {"IntrBkSttlmAmt", "InstdAmt", "TtlIntrBkSttlmAmt", "Amt",
                     "EqvtAmt", "InstdAmt", "TxAmt"}
    _FALLBACK = "DE89370400440532013000"  # DE/EUR — most common cross-border currency
    _IBAN_PAT = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$')
    _NON_IBAN = {"US", "CA", "AU", "NZ", "HK", "SG", "JP", "CN", "IN"}

    tx_ccy: Optional[str] = None
    if root is not None:
        for _a in root.iter():
            if not isinstance(_a.tag, str):
                continue
            _ccy = _a.get("Ccy")
            if _ccy and etree.QName(_a.tag).localname in _CCY_AMT_TAGS:
                tx_ccy = _ccy
                break

    ibdata = (_kb_get("dummy_data.ibans", {}) or
              _enterprise_shared("dummy_data.ibans", {}) or {})
    if not isinstance(ibdata, dict):
        ibdata = {}

    if tx_ccy:
        ccy_by_ctry = (_kb_get("dummy_data.currencies_by_country", {}) or {})
        if isinstance(ccy_by_ctry, dict):
            for ctry, ccy in ccy_by_ctry.items():
                if ccy == tx_ccy and ctry not in _NON_IBAN and ctry in ibdata:
                    cand = ibdata[ctry]
                    if isinstance(cand, str) and _IBAN_PAT.match(cand) and _verify_iban_mod97(cand):
                        return cand

    return ibdata.get("default") or _FALLBACK


def _detect_msg_type(xml: str) -> str:
    """
    Extract message type from xmlns, e.g. 'pacs.008.001.08'.
    Handles full ISO 20022 URNs like:
      urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08
      urn:iso:std:iso:20022:tech:xsd:pain.001.001.09
      urn:iso:std:iso:20022:tech:xsd:camt.053.001.08

    Format: <family>.<message_no>.<variant>.<version>
            letters . digits . digits . digits
    """
    # MsgDefIdr is the explicit declaration — check it first so a wrong Document
    # namespace (e.g. pacs.008 on a camt.056 message) never overrides intent.
    mdi = re.search(r"<MsgDefIdr>([^<]+)</MsgDefIdr>", xml)
    if mdi:
        val = mdi.group(1).strip()
        if val and not val.startswith("head."):
            return val

    # A header-wrapped message contains BOTH the BAH type (head.001.001.0x,
    # which appears first) and the actual Document message type. Always prefer
    # the business message type — otherwise every message-specific lookup (KB
    # folder, rules index, templates) would key off 'head.*'.
    matches = re.findall(r"([a-z]+\.\d{2,3}\.\d{3}\.\d{2})", xml)
    for mt in matches:
        if not mt.startswith("head."):
            return mt
    if matches:
        return matches[0]
    # Looser fallback: family.NNN (e.g. just pacs.008)
    m = re.search(r"([a-z]+\.\d{2,3})", xml)
    return m.group(1) if m else ""


def _extract_xml_from_fix(fix_str: str, tag_name: str) -> Optional[str]:
    """
    Extract the XML fragment for `tag_name` from a rule's `fix` string.

    Rule fix strings often contain the exact tag structure, e.g.:
      "Add <DbtrAcct><Id><IBAN>...</IBAN></Id></DbtrAcct> (or <Othr>...)"
    Returns the matched fragment (with ... placeholders intact) or None.
    """
    if not fix_str:
        return None
    pattern = rf"(<{tag_name}[\s>][^<]*(?:<(?!/{tag_name}>)[^<]*)*</{tag_name}>|<{tag_name}\s*/>)"
    m = re.search(pattern, fix_str, re.DOTALL)
    if m:
        return m.group(1)
    # Looser: tag may have attrs and nested content
    pattern2 = rf"<{tag_name}[\s>].*?</{tag_name}>"
    m = re.search(pattern2, fix_str, re.DOTALL)
    return m.group(0) if m else None


# ── Known templates for common ISO 20022 elements ────────────────────────────
# These cover 80%+ of all CBPR+ validation errors.
# Namespace is added at apply-time; keep templates namespace-free here.
#
# UETR sentinel: all UETR/OrgnlUETR values inside templates use this marker.
# _inject_fresh_uetrs() replaces it with str(uuid.uuid4()) at usage time so
# every inserted UETR is globally unique — never reuse a static value.
_UETR_SENTINEL = "00000000-0000-4000-a000-000000000000"

def _inject_fresh_uetrs(tmpl: str) -> str:
    """Replace every <UETR>…</UETR> and <OrgnlUETR>…</OrgnlUETR> value inside
    a template string with a freshly generated UUID v4.  Called before the
    template is parsed as XML, so every fix application gets a unique UETR."""
    return re.sub(
        r'(<(?:UETR|OrgnlUETR)>)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(</(?:UETR|OrgnlUETR)>)',
        lambda m: m.group(1) + str(uuid.uuid4()) + m.group(2),
        tmpl,
        flags=re.IGNORECASE,
    )


_TEMPLATES: dict[str, str] = {
    # Identifiers
    "UETR":           f"<UETR>{_UETR_SENTINEL}</UETR>",
    "MsgId":          "<MsgId>MSG-2025-001</MsgId>",
    "PmtInfId":       "<PmtInfId>PMT-INF-2025-001</PmtInfId>",
    "Id":             "<Id>ID-2025-001</Id>",
    "BICFI":          "<BICFI>DEUTDEFFXXX</BICFI>",
    "IBAN":           "<IBAN>GB29NWBK60161331926819</IBAN>",
    "LEI":            "<LEI>549300TRUWOII88U4F73</LEI>",
    "ClrSysRef":      "<ClrSysRef>REF-2025-001</ClrSysRef>",
    "EndToEndId":     "<EndToEndId>E2E-2025-001</EndToEndId>",
    "TxId":           "<TxId>TXN-2025-001</TxId>",
    "InstrId":        "<InstrId>INSTR-2025-001</InstrId>",
    # Dates
    "IntrBkSttlmDt":  "<IntrBkSttlmDt>2025-01-15</IntrBkSttlmDt>",
    "CreDtTm":        "<CreDtTm>2025-01-15T10:00:00Z</CreDtTm>",
    "ReqdExctnDt":    "<ReqdExctnDt><Dt>2025-01-15</Dt></ReqdExctnDt>",
    # Simple value elements
    "Nm":             "<Nm>Sample Name</Nm>",
    "Ctry":           "<Ctry>US</Ctry>",
    "AdrLine":        "<AdrLine>123 Main Street</AdrLine>",
    "TwnNm":          "<TwnNm>New York</TwnNm>",
    "PstCd":          "<PstCd>10001</PstCd>",
    "StrtNm":         "<StrtNm>Main Street</StrtNm>",
    "BldgNb":         "<BldgNb>123</BldgNb>",
    "ChrgBr":         "<ChrgBr>SHAR</ChrgBr>",
    "SttlmMtd":       "<SttlmMtd>INGA</SttlmMtd>",
    "NbOfTxs":        "<NbOfTxs>1</NbOfTxs>",
    "CtrlSum":        "<CtrlSum>1000.00</CtrlSum>",
    "Ccy":            "<Ccy>USD</Ccy>",
    "XchgRate":       "<XchgRate>1.0</XchgRate>",
    # Complex blocks
    "FinInstnId": (
        "<FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId>"
    ),
    "PstlAdr": (
        "<PstlAdr>"
        "<AdrTp><Cd>ADDR</Cd></AdrTp>"
        "<AdrLine>123 Main Street</AdrLine>"
        "<Ctry>US</Ctry>"
        "</PstlAdr>"
    ),
    "Dbtr": (
        "<Dbtr>"
        "<Nm>Debtor Name</Nm>"
        "<PstlAdr><AdrTp><Cd>ADDR</Cd></AdrTp>"
        "<AdrLine>123 Main Street</AdrLine><Ctry>US</Ctry></PstlAdr>"
        "</Dbtr>"
    ),
    "Cdtr": (
        "<Cdtr>"
        "<Nm>Creditor Name</Nm>"
        "<PstlAdr><AdrTp><Cd>ADDR</Cd></AdrTp>"
        "<AdrLine>456 Oak Avenue</AdrLine><Ctry>GB</Ctry></PstlAdr>"
        "</Cdtr>"
    ),
    "DbtrAcct": (
        "<DbtrAcct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></DbtrAcct>"
    ),
    "CdtrAcct": (
        "<CdtrAcct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></CdtrAcct>"
    ),
    "DbtrAgt": (
        "<DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>"
    ),
    "CdtrAgt": (
        "<CdtrAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></CdtrAgt>"
    ),
    "InstgAgt": (
        "<InstgAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></InstgAgt>"
    ),
    "InstdAgt": (
        "<InstdAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></InstdAgt>"
    ),
    "IntrmyAgt1": (
        "<IntrmyAgt1><FinInstnId><BICFI>MIDLGB22XXX</BICFI></FinInstnId></IntrmyAgt1>"
    ),
    "ChrgsInf": (
        '<ChrgsInf><Amt Ccy="USD">0.00</Amt>'
        "<Agt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Agt>"
        "</ChrgsInf>"
    ),
    "Agt": (
        "<Agt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Agt>"
    ),
    "PmtTpInf": (
        "<PmtTpInf><SvcLvl><Cd>SEPA</Cd></SvcLvl></PmtTpInf>"
    ),
    "SvcLvl":    "<SvcLvl><Cd>SEPA</Cd></SvcLvl>",
    "LclInstrm": "<LclInstrm><Cd>CORE</Cd></LclInstrm>",
    "InstrPrty": "<InstrPrty>NORM</InstrPrty>",
    "CtgyPurp":  "<CtgyPurp><Cd>SUPP</Cd></CtgyPurp>",
    "Purp":      "<Purp><Cd>GDDS</Cd></Purp>",
    "RmtInf":    "<RmtInf><Ustrd>Payment reference</Ustrd></RmtInf>",
    "Strd":      "<Strd><RfrdDocInf><Tp><CdOrPrtry><Cd>CINV</Cd></CdOrPrtry></Tp></RfrdDocInf></Strd>",
    "PmtId": (
        "<PmtId>"
        "<InstrId>INSTR-2025-001</InstrId>"
        "<EndToEndId>E2E-2025-001</EndToEndId>"
        f"<UETR>{_UETR_SENTINEL}</UETR>"
        "</PmtId>"
    ),
    "Grphdr": (
        "<GrpHdr>"
        "<MsgId>MSG-2025-001</MsgId>"
        "<CreDtTm>2025-01-15T10:00:00Z</CreDtTm>"
        "<NbOfTxs>1</NbOfTxs>"
        "<SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>"
        "</GrpHdr>"
    ),
    "SttlmInf":  "<SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>",
    "AcctId":    "<AcctId><IBAN>GB29NWBK60161331926819</IBAN></AcctId>",

    # ── pain.001 / pain.008 ────────────────────────────────────────────────
    "InitgPty": "<InitgPty><Nm>Initiating Party</Nm></InitgPty>",
    "ReqdColltnDt": "<ReqdColltnDt>2026-01-15</ReqdColltnDt>",
    "ReqdExctnDt":  "<ReqdExctnDt><Dt>2026-01-15</Dt></ReqdExctnDt>",
    "DrctDbtTx": (
        "<DrctDbtTx>"
        "<MndtRltdInf><MndtId>MNDT-001</MndtId><DtOfSgntr>2024-01-01</DtOfSgntr></MndtRltdInf>"
        "</DrctDbtTx>"
    ),
    "MndtRltdInf": "<MndtRltdInf><MndtId>MNDT-001</MndtId><DtOfSgntr>2024-01-01</DtOfSgntr></MndtRltdInf>",

    # ── camt.056 ───────────────────────────────────────────────────────────
    "Assgnmt": (
        "<Assgnmt>"
        "<Id>ASSGNMT-001</Id>"
        "<Assgnr><Agt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Agt></Assgnr>"
        "<Assgne><Agt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></Agt></Assgne>"
        "<CreDtTm>2026-01-15T10:00:00+00:00</CreDtTm>"
        "</Assgnmt>"
    ),
    "Assgnr": "<Assgnr><Agt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Agt></Assgnr>",
    "Assgne": "<Assgne><Agt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></Agt></Assgne>",
    "CxlRsnInf": "<CxlRsnInf><Rsn><Cd>DUPL</Cd></Rsn></CxlRsnInf>",
    # camt.055/056 TxInf mandatory sequence per CBPR+ SR2026 XSD:
    # CxlId(opt) → Case(opt) → OrgnlInstrId(opt) → OrgnlEndToEndId(mand) →
    # OrgnlUETR(mand) → OrgnlInstdAmt(mand) → OrgnlReqdExctnDt(opt) → CxlRsnInf(mand)
    "OrgnlEndToEndId": "<OrgnlEndToEndId>E2E-ORIG-001</OrgnlEndToEndId>",
    "OrgnlInstrId":    "<OrgnlInstrId>INSTR-ORIG-001</OrgnlInstrId>",
    "OrgnlUETR":       f"<OrgnlUETR>{_UETR_SENTINEL}</OrgnlUETR>",
    "OrgnlInstdAmt":   '<OrgnlInstdAmt Ccy="EUR">1000.00</OrgnlInstdAmt>',
    "Undrlyg": (
        "<Undrlyg>"
        "<OrgnlPmtInfAndCxl>"
        "<OrgnlPmtInfId>ORIG-PMT-INF-001</OrgnlPmtInfId>"
        "<TxInf>"
        "<OrgnlEndToEndId>E2E-ORIG-001</OrgnlEndToEndId>"
        f"<OrgnlUETR>{_UETR_SENTINEL}</OrgnlUETR>"
        '<OrgnlInstdAmt Ccy="EUR">1000.00</OrgnlInstdAmt>'
        "<CxlRsnInf><Rsn><Cd>DUPL</Cd></Rsn></CxlRsnInf>"
        "</TxInf>"
        "</OrgnlPmtInfAndCxl>"
        "</Undrlyg>"
    ),
    "TxInf": (
        "<TxInf>"
        "<OrgnlEndToEndId>E2E-ORIG-001</OrgnlEndToEndId>"
        f"<OrgnlUETR>{_UETR_SENTINEL}</OrgnlUETR>"
        '<OrgnlInstdAmt Ccy="EUR">1000.00</OrgnlInstdAmt>'
        "<CxlRsnInf><Rsn><Cd>DUPL</Cd></Rsn></CxlRsnInf>"
        "</TxInf>"
    ),
    "Case": "<Case><Id>CASE-001</Id></Case>",
    "OrgnlGrpInf": (
        "<OrgnlGrpInf>"
        "<OrgnlMsgId>ORIG-MSG-001</OrgnlMsgId>"
        "<OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>"
        "</OrgnlGrpInf>"
    ),

    # ── camt.057 ───────────────────────────────────────────────────────────
    "Ntfctn": (
        "<Ntfctn>"
        "<Id>NTFCTN-001</Id>"
        '<Itm><Id>ITM-001</Id><Amt Ccy="EUR">0.00</Amt></Itm>'
        "</Ntfctn>"
    ),
    "Itm": '<Itm><Id>ITM-001</Id><Amt Ccy="EUR">0.00</Amt></Itm>',
    "MsgSndr": "<MsgSndr><Agt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Agt></MsgSndr>",
    "AcctOwnr": "<AcctOwnr><Pty><Nm>Account Owner</Nm></Pty></AcctOwnr>",
    # ── camt.056 Cretr / Pty ───────────────────────────────────────────────
    # Cretr requires either Agt (with BICFI) or Pty (with Nm when no BICFI present).
    # This template uses Pty+Nm as the safe default when BICFI is unknown.
    "Cretr": (
        "<Cretr><Pty><Nm>Case Creator</Nm>"
        "<PstlAdr><StrtNm>Main Street</StrtNm><BldgNb>1</BldgNb>"
        "<TwnNm>Amsterdam</TwnNm><Ctry>NL</Ctry></PstlAdr>"
        "</Pty></Cretr>"
    ),
    "Pty": (
        "<Pty><Nm>Case Creator Name</Nm>"
        "<PstlAdr><StrtNm>Allen Street</StrtNm><BldgNb>1</BldgNb>"
        "<TwnNm>Oslo</TwnNm><Ctry>NO</Ctry></PstlAdr>"
        "</Pty>"
    ),
    "AcctSvcr": "<AcctSvcr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></AcctSvcr>",
    "XpctdValDt": "<XpctdValDt>2026-01-15</XpctdValDt>",

    # ── camt.052/053/054 Ntry mandatory children ───────────────────────────
    # BookgDt / ValDt are date-choice containers; BkTxCd must have at least
    # one of Domn or Prtry (both optional in base XSD, but required by CBPR+).
    "BookgDt": "<BookgDt><Dt>2026-01-15</Dt></BookgDt>",
    "ValDt":   "<ValDt><Dt>2026-01-15</Dt></ValDt>",
    "BkTxCd": (
        "<BkTxCd>"
        "<Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>OTHR</SubFmlyCd></Fmly></Domn>"
        "</BkTxCd>"
    ),

    # ── pacs.010 ───────────────────────────────────────────────────────────
    "CdtInstr": (
        "<CdtInstr>"
        "<CdtId>CDT-001</CdtId>"
        "<SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>"
        "<Cdtr><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></Cdtr>"
        "<DrctDbtTxInf>"
        "<PmtId><InstrId>INSTR-001</InstrId><EndToEndId>E2E-001</EndToEndId>"
        f"<UETR>{_UETR_SENTINEL}</UETR></PmtId>"
        '<IntrBkSttlmAmt Ccy="USD">0.00</IntrBkSttlmAmt>'
        "<Dbtr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Dbtr>"
        "<DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>"
        "</DrctDbtTxInf>"
        "</CdtInstr>"
    ),

    # ── pain.002 ───────────────────────────────────────────────────────────
    "OrgnlGrpInfAndSts": (
        "<OrgnlGrpInfAndSts>"
        "<OrgnlMsgId>ORIG-MSG-001</OrgnlMsgId>"
        "<OrgnlMsgNmId>pain.001.001.09</OrgnlMsgNmId>"
        "</OrgnlGrpInfAndSts>"
    ),
    "OrgnlPmtInfAndSts": (
        "<OrgnlPmtInfAndSts>"
        "<OrgnlPmtInfId>ORIG-PMT-INF-001</OrgnlPmtInfId>"
        "<TxInfAndSts><OrgnlEndToEndId>E2E-001</OrgnlEndToEndId><TxSts>ACCP</TxSts></TxInfAndSts>"
        "</OrgnlPmtInfAndSts>"
    ),
}


# ── XSD type map (cached) ─────────────────────────────────────────────────────

class _XsdTypeMap:
    _cache: dict[str, "_XsdTypeMap"] = {}

    def __init__(self, xsd_path: str):
        self.element_type: dict[str, str] = {}    # global element name → type
        self.local_type: dict[tuple, str] = {}    # (parent_type, child) → type
        self.type_info: dict[str, dict] = {}      # type_name → info
        try:
            self._parse(xsd_path)
        except Exception as e:
            logger.warning(f"[XsdTypeMap] {xsd_path}: {e}")

    def _parse(self, path: str) -> None:
        tree = etree.parse(path)
        root = tree.getroot()
        for el in root.findall(f"{{{XS}}}element"):
            n, t = el.get("name"), el.get("type")
            if n and t:
                self.element_type[n] = t
        for ct in root.findall(f"{{{XS}}}complexType"):
            tn = ct.get("name")
            if not tn:
                continue
            info: dict = {"kind": "empty", "children": [], "attrs": [], "enums": []}
            seq = ct.find(f"{{{XS}}}sequence")
            if seq is not None:
                info["kind"] = "sequence"
                for el in seq.findall(f"{{{XS}}}element"):
                    cn, ct_ = el.get("name"), el.get("type")
                    if cn and ct_:
                        info["children"].append({"name": cn, "type": ct_,
                                                  "min": el.get("minOccurs", "1"),
                                                  "max": el.get("maxOccurs", "1")})
                        self.local_type[(tn, cn)] = ct_
            chc = ct.find(f"{{{XS}}}choice")
            if chc is not None and info["kind"] == "empty":
                info["kind"] = "choice"
                for el in chc.findall(f"{{{XS}}}element"):
                    cn, ct_ = el.get("name"), el.get("type")
                    if cn and ct_:
                        info["children"].append({"name": cn, "type": ct_,
                                                  "min": el.get("minOccurs", "1"),
                                                  "max": el.get("maxOccurs", "1")})
                        # Register choice members in local_type too so
                        # get_child_type / type_of_path can resolve them. Without
                        # this, choice leaves (SvcLvl/Cd, AcctId/Othr, …) were
                        # invisible to the XSD-buildable gate and dropped.
                        self.local_type[(tn, cn)] = ct_
                        # Record choice children too, so type_of_path can traverse
                        # through a choice (e.g. AppHdr/Fr is Party44Choice → FIId).
                        self.local_type[(tn, cn)] = ct_
            sc = ct.find(f"{{{XS}}}simpleContent/{{{XS}}}extension")
            if sc is not None:
                info["kind"] = "simpleContent"
                info["base"] = sc.get("base", "")
                info["attrs"] = [{"name": a.get("name", ""), "type": a.get("type", ""),
                                   "use": a.get("use", "optional")}
                                  for a in sc.findall(f"{{{XS}}}attribute")]
            self.type_info[tn] = info
        for st in root.findall(f"{{{XS}}}simpleType"):
            tn = st.get("name")
            if not tn:
                continue
            enums = [e.get("value") for e in st.findall(f".//{{{XS}}}enumeration") if e.get("value")]
            restr = st.find(f"{{{XS}}}restriction")
            base  = restr.get("base", "") if restr is not None else ""
            self.type_info[tn] = {"kind": "simple", "children": [], "attrs": [],
                                   "enums": enums, "base": base}

    def get_child_type(self, parent_type: str, child_name: str) -> Optional[str]:
        return self.local_type.get((parent_type, child_name)) or self.element_type.get(child_name)

    def type_of_path(self, path_locals: list) -> Optional[str]:
        """Resolve the XSD type of the element addressed by a list of local
        names (e.g. ['Document','FIToFICstmrDrctDbt','DrctDbtTxInf','PmtId']).

        ISO 20022 XSDs declare a single global element (Document) and nest every
        other element as a *local* element keyed by its parent TYPE — so a
        name-only lookup resolves nothing. We instead walk the type chain from
        the root through local_type. Works for every MX message schema.
        """
        if not path_locals:
            return None
        parts = list(path_locals)
        # Anchor on the first part that is a known global element (handles
        # wrapper roots like RequestPayload/AppHdr or AppHdr/Document/...).
        anchor = next((i for i, p in enumerate(parts) if p in self.element_type), None)
        if anchor is not None:
            cur  = self.element_type[parts[anchor]]
            rest = parts[anchor + 1:]
        elif len(self.element_type) == 1:
            cur  = next(iter(self.element_type.values()))  # anchor on sole root
            rest = parts
        else:
            return None
        for child in rest:
            cur = self.local_type.get((cur, child))
            if cur is None:
                return None
        return cur

    def order_for_type(self, type_name: Optional[str]) -> list:
        """Return the child element order for a sequence complexType, or []."""
        info = self.type_info.get(type_name or "", {})
        if info.get("kind") == "sequence":
            return [c["name"] for c in info.get("children", [])]
        return []

    @classmethod
    def get(cls, xsd_path: str) -> "_XsdTypeMap":
        if xsd_path not in cls._cache:
            cls._cache[xsd_path] = _XsdTypeMap(xsd_path)
        return cls._cache[xsd_path]


def _xsd_simple_value(name: str, type_name: str, info: dict) -> Optional[str]:
    """Generate a schema-valid value for an XSD simpleType leaf, derived from
    its restriction base (xs:date, xs:dateTime, xs:decimal, xs:boolean, ...),
    its enumerations, or name/type heuristics. Returns None so the caller can
    fall back to a generic token for plain free-text types.

    This is what makes XSD-driven construction datatype-aware for ALL message
    types — e.g. an ISODate leaf becomes a real date instead of 'SMPL-...'.
    """
    enums = info.get("enums") or []
    if enums:
        return enums[0]
    base = (info.get("base") or "").split(":")[-1].lower()
    from datetime import date, datetime, timezone
    if base == "datetime":
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if base == "date":
        return date.today().isoformat()
    if base in ("decimal", "double", "float"):
        return "0"
    if base in ("integer", "nonnegativeinteger", "positiveinteger", "int", "long"):
        return "1"
    if base == "boolean":
        return "true"
    if base == "gyear":
        return str(date.today().year)
    if base == "gyearmonth":
        return date.today().strftime("%Y-%m")
    # String-ish types: reuse known field examples (BICFI, IBAN, Ctry, Ccy, ...)
    ex = (_kb_field_constraint(name) or {}).get("example")
    if ex:
        return ex
    tnl = (type_name or "").lower()
    if "bic" in tnl:                          return "DEUTDEFFXXX"
    if "iban" in tnl:                         return "GB29NWBK60161331926819"
    if "countrycode" in tnl:                  return "US"
    if "currencycode" in tnl or "currency" in tnl: return "USD"
    # External code-list elements: avoid SMPL-Cd / SMPL-Prtry placeholders.
    # These are string-typed with no embedded XSD enums (the list is external).
    # Use well-known codelist defaults so the inserted scaffold is schema-valid.
    nl = name.lower()
    if nl == "cd":                            return "SEPA"   # overridden by parent context later
    if nl in ("prtry", "issr"):               return "NOTPROVIDED"
    if nl in ("chrgbr",):                     return "SHAR"
    if nl in ("txsts", "grpsts"):             return "ACCP"
    return None


def _xsd_build(name: str, type_name: str, tmap: Optional[_XsdTypeMap], ns: str, depth: int = 0) -> Optional[etree._Element]:
    """Recursively build an element from XSD type definition."""
    if depth > 6 or not tmap:
        return None
    tag = f"{{{ns}}}{name}" if ns else name
    el  = etree.Element(tag)
    info = tmap.type_info.get(type_name, {})
    kind = info.get("kind", "")
    if kind == "simple":
        v = _xsd_simple_value(name, type_name, info)
        el.text = v if v is not None else f"SMPL-{name}"
        return el
    if kind == "simpleContent":
        el.text = "1000.00" if "Amount" in type_name else "SMPL"
        for a in info.get("attrs", []):
            if a.get("use") == "required":
                el.set(a["name"], "USD" if "Currency" in a.get("type", "") else "VAL")
        return el
    if kind == "choice":
        children = info.get("children", [])
        if children:
            c = children[0]
            child = _xsd_build(c["name"], c.get("type", ""), tmap, ns, depth + 1)
            if child is not None:
                el.append(child)
        return el
    if kind == "sequence":
        for c in info.get("children", []):
            if c.get("min", "1") == "0":
                continue
            child = _xsd_build(c["name"], c.get("type", ""), tmap, ns, depth + 1)
            if child is not None:
                el.append(child)
        return el
    # Unknown kind fallback — apply same name-based defaults as _xsd_simple_value
    # to avoid generating schema-invalid placeholder values like 'SMPL-Cd'.
    _nl = name.lower()
    if _nl == "cd":                  el.text = "SEPA"
    elif _nl in ("prtry", "issr"):   el.text = "NOTPROVIDED"
    elif _nl in ("chrgbr",):         el.text = "SHAR"
    elif _nl in ("txsts", "grpsts"): el.text = "ACCP"
    else:                            el.text = f"SMPL-{name}"
    return el


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class FixSuggestion:
    xpath: str
    original_fragment: str
    fragment_xml: str
    issue_code: str
    issue_message: str
    confidence: str
    # Closed-loop self-check result, attached by suggest_verified():
    #   True  = applying the fix stays well-formed and does not worsen XSD validity
    #   False = the fix fails to apply / breaks well-formedness / adds schema errors
    #   None  = not judged (no actionable fix, or no XSD available to judge it)
    # Optional with a default so every existing positional constructor is unaffected.
    verified: Optional[bool] = None


class FixApplyError(Exception):
    pass


# Legal XML element local-name (NCName, minus ':'). Used to reject path segments
# that are actually line numbers / free text (e.g. validators that report
# path="7"). Treating those as tag names makes the missing-element builder try
# to create <7/> and crash with lxml "Invalid tag name".
_VALID_XML_NAME = re.compile(r'^[A-Za-z_][\w.\-]*$')


# CBPR+ structural exclusions: child elements that are NOT permitted inside a
# given parent under CBPR+, even though the base ISO 20022 XSD allows them.
# Keyed by parent local-name → set of forbidden child local-names. Example:
# <ClrSys> inside <SttlmInf> — CBPR+ settles pacs.008/009 via INDA/INGA, so a
# clearing-system identification has no place there (SettlementInstruction in the
# CBPR+ schema has no ClrSys property). The fixer's own type map is the lenient
# base schema and can't tell, so these exclusions are encoded explicitly and the
# offending element is removed when the validator flags it "not expected here".
_CBPR_FORBIDDEN_CHILDREN = {"SttlmInf": {"ClrSys"}}


# ── FixSuggester ──────────────────────────────────────────────────────────────

class FixSuggester:

    def __init__(self, version: str = None):
        # Release this instance is bound to, used when a per-call version isn't
        # supplied. The shared engine is identical across releases; only the
        # per-release handler modules differ (see _version_handler).
        self._default_version = version if version in ("SR2025", "SR2026") else None

    # ── XML helpers ───────────────────────────────────────────────────────────

    def _parse_xml(self, xml: str) -> etree._Element:
        # Preserve the original byte layout (including the XML declaration and
        # any leading blank lines) so that lxml's element.sourceline values
        # match the line numbers the validator reports to the user. Stripping
        # the prolog shifted every sourceline by 1+ and made line-hint-based
        # element resolution pick the wrong sibling.
        try:
            return etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            # If the declaration is malformed, fall back to a cleaned version
            # so callers don't get a hard failure.
            try:
                cleaned = re.sub(r"<\?xml[^?]*\?>", "", xml, count=1).lstrip()
                return etree.fromstring(cleaned.encode("utf-8"))
            except etree.XMLSyntaxError:
                raise FixApplyError(f"XML parse error: {e}")

    # AppHdr-only tags that must NOT appear as direct children of BusMsgEnvlp.
    # When they do appear there (copy-paste / AI generation artefact) they are
    # stray duplicates of what's already inside <AppHdr> and must be removed.
    _APPHDR_ONLY_TAGS = frozenset({
        "BizMsgIdr", "MsgDefIdr", "BizSvc", "CreDt", "CreDtTm",
        "CharSet", "Fr", "To", "MsgDefIdrRef",
    })

    def _normalize_busmsgenvlp(self, xml: str) -> str:
        """Fix BusMsgEnvlp envelope structure issues.

        Handles the following problems:

        1. Document wrongly nested inside AppHdr → move it to sibling.

        2. Stray AppHdr-only tags (BizMsgIdr, MsgDefIdr, BizSvc, CreDt, …)
           placed as direct children of BusMsgEnvlp → remove them.

        3. <Fr> or <To> deleted, leaving <FIId> as a direct AppHdr child.
           When this happens, AppHdr-level tags (BizMsgIdr, MsgDefIdr, …)
           often get absorbed inside <FIId> because the closing </Fr> was
           the delimiter. Fix: re-wrap <FIId> in <Fr>, then extract any
           AppHdr-level siblings that were wrongly placed inside <FIId>.

        4. <FinInstnId> inside Fr/To/FIId has no children (opening tag was
           deleted along with its content). Inject a dummy <BICFI> so the
           document is structurally complete after tag-balance recovery.

        Correct SWIFT CBPR+ BusMsgEnvlp structure:
          <BusMsgEnvlp>
            <AppHdr>
              <Fr><FIId><FinInstnId>…</FinInstnId></FIId></Fr>
              <To><FIId><FinInstnId>…</FinInstnId></FIId></To>
              <BizMsgIdr>…</BizMsgIdr>
              …
            </AppHdr>
            <Document>…</Document>
          </BusMsgEnvlp>
        """
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return xml  # not parseable — leave unchanged

        root_local = etree.QName(root.tag).localname
        if root_local == "BulkMessages":
            # BulkMessages container — normalize each inner BusMsgEnvlp in-place
            _any_changed = False
            for _envlp in list(root):
                if not isinstance(_envlp.tag, str):
                    continue
                if etree.QName(_envlp.tag).localname != "BusMsgEnvlp":
                    continue
                _inner_xml = etree.tostring(_envlp, encoding="unicode")
                _fixed_inner = self._normalize_busmsgenvlp(_inner_xml)
                if _fixed_inner != _inner_xml:
                    try:
                        _fixed_envlp = etree.fromstring(_fixed_inner.encode("utf-8"))
                        _envlp.getparent().replace(_envlp, _fixed_envlp)
                        _any_changed = True
                    except Exception:
                        pass
            if not _any_changed:
                return xml
            decl = ""
            m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
            if m:
                decl = m.group(1) + "\n"
            return decl + etree.tostring(root, encoding="unicode", pretty_print=True)
        if root_local != "BusMsgEnvlp":
            return xml

        apphdr = next((c for c in root
                       if isinstance(c.tag, str)
                       and etree.QName(c.tag).localname == "AppHdr"), None)
        if apphdr is None:
            return xml

        ah_ns = etree.QName(apphdr.tag).namespace or ""
        _changed = False

        # ── Problem 3: bare <FIId> as direct AppHdr child ─────────────────────
        # Caused by deleting <Fr> or <To> opening tag — the <FIId> content
        # and any following AppHdr-level tags (BizMsgIdr, MsgDefIdr, …) end up
        # inside <FIId> instead of at AppHdr level.
        # Sub-case: balance engine may have re-inserted an empty <Fr>/<To> shell
        # AFTER the orphaned FIId content — detect this and move the FIId inside.
        _APPHDR_SIBLINGS = self._APPHDR_ONLY_TAGS  # BizMsgIdr, MsgDefIdr, BizSvc, CreDt, …
        _apphdr_children = list(apphdr)
        for _ci, fiid_el in enumerate(_apphdr_children):
            if not isinstance(fiid_el.tag, str):
                continue
            if etree.QName(fiid_el.tag).localname != "FIId":
                continue

            # Extract any AppHdr-level tags that got absorbed inside <FIId>
            absorbed = []
            for child in list(fiid_el):
                if not isinstance(child.tag, str):
                    continue
                if etree.QName(child.tag).localname in _APPHDR_SIBLINGS:
                    fiid_el.remove(child)
                    absorbed.append(child)

            # Determine the correct wrapper: check if an empty Fr or To sits
            # immediately adjacent (before or after) this bare FIId — the balance
            # engine inserts the synthetic open AFTER the orphan's content, so the
            # empty shell will be at index _ci+1.  If found, move the FIId inside
            # that empty shell rather than creating a brand-new wrapper.
            _prev = _apphdr_children[_ci - 1] if _ci > 0 else None
            _next = _apphdr_children[_ci + 1] if _ci + 1 < len(_apphdr_children) else None

            def _is_empty_frto(el, tag_name):
                return (el is not None and isinstance(el.tag, str)
                        and etree.QName(el.tag).localname == tag_name
                        and not any(isinstance(c.tag, str) for c in el))

            _target_shell = None
            if _is_empty_frto(_next, "Fr") or _is_empty_frto(_next, "To"):
                _target_shell = _next
            elif _is_empty_frto(_prev, "Fr") or _is_empty_frto(_prev, "To"):
                _target_shell = _prev

            if _target_shell is not None:
                # Move FIId into the adjacent empty Fr/To shell
                fiid_idx = list(apphdr).index(fiid_el)
                apphdr.remove(fiid_el)
                _target_shell.insert(0, fiid_el)
            else:
                # No adjacent empty shell — need to determine Fr or To from context
                fr_exists = any(isinstance(c.tag, str) and etree.QName(c.tag).localname == "Fr"
                                for c in apphdr)
                to_exists = any(isinstance(c.tag, str) and etree.QName(c.tag).localname == "To"
                                for c in apphdr)
                # If both exist already, pick whichever one is empty
                if fr_exists and not to_exists:
                    wrapper_name = "To"
                elif to_exists and not fr_exists:
                    wrapper_name = "Fr"
                else:
                    wrapper_name = "Fr"
                tag_qname = f"{{{ah_ns}}}{wrapper_name}" if ah_ns else wrapper_name
                wrapper = etree.Element(tag_qname)
                fiid_idx = list(apphdr).index(fiid_el)
                apphdr.remove(fiid_el)
                wrapper.append(fiid_el)
                apphdr.insert(fiid_idx, wrapper)

            # Re-insert absorbed AppHdr-level tags after the FIId's parent wrapper
            _frto_now = fiid_el.getparent()
            if _frto_now is not None and _frto_now.getparent() is apphdr:
                insert_pos = list(apphdr).index(_frto_now) + 1
            else:
                insert_pos = list(apphdr).index(fiid_el) + 1 if fiid_el in list(apphdr) else len(list(apphdr))
            for tag_el in absorbed:
                apphdr.insert(insert_pos, tag_el)
                insert_pos += 1

            _changed = True

        # ── Problem 3b: AppHdr-level tags absorbed inside Fr or To ─────────────
        # Covers two sub-cases:
        #   (i)  tags absorbed as direct children of Fr/To (balance swallowed them
        #        because </To> close was missing, so everything up to </AppHdr>
        #        ended up inside To).
        #   (ii) tags absorbed inside Fr/To > FIId (previous behaviour).
        # Lift BizMsgIdr, MsgDefIdr, BizSvc, CreDt, etc. back to AppHdr level.
        for _frto_el in [c for c in list(apphdr)
                         if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname in ("Fr", "To")]:
            _apphdr_idx = list(apphdr).index(_frto_el)
            _lift_pos = _apphdr_idx + 1

            # (i) Direct children of Fr/To that belong at AppHdr level
            for _direct in list(_frto_el):
                if not isinstance(_direct.tag, str):
                    continue
                _local = etree.QName(_direct.tag).localname
                if _local in self._APPHDR_ONLY_TAGS:
                    _frto_el.remove(_direct)
                    apphdr.insert(_lift_pos, _direct)
                    _lift_pos += 1
                    _changed = True

            # (ii) Children of FIId inside Fr/To
            _fiid = next((c for c in _frto_el
                          if isinstance(c.tag, str)
                          and etree.QName(c.tag).localname == "FIId"), None)
            if _fiid is None:
                continue
            for _absorbed in list(_fiid):
                if not isinstance(_absorbed.tag, str):
                    continue
                _local = etree.QName(_absorbed.tag).localname
                if _local in self._APPHDR_ONLY_TAGS or _local in ("Fr", "To"):
                    _fiid.remove(_absorbed)
                    if _local in ("Fr", "To") and len(_absorbed) == 0:
                        pass  # discard empty stray Fr/To
                    else:
                        apphdr.insert(_lift_pos, _absorbed)
                        _lift_pos += 1
                    _changed = True

            # (iii) Children of BICFI inside Fr/To > FIId > FinInstnId — caused
            # when _balance_xml_tags closes an unclosed empty <BICFI> by nesting
            # subsequent siblings (<To>, AppHdr-level tags) inside it instead of
            # leaving them as siblings. Lift those misplaced elements to AppHdr.
            _fininstnid = next((c for c in _fiid
                                if isinstance(c.tag, str)
                                and etree.QName(c.tag).localname == "FinInstnId"), None)
            if _fininstnid is None:
                continue
            _bicfi_el = next((c for c in _fininstnid
                              if isinstance(c.tag, str)
                              and etree.QName(c.tag).localname == "BICFI"), None)
            if _bicfi_el is None:
                continue
            for _absorbed in list(_bicfi_el):
                if not isinstance(_absorbed.tag, str):
                    continue
                _local = etree.QName(_absorbed.tag).localname
                if _local in self._APPHDR_ONLY_TAGS or _local in ("Fr", "To"):
                    _bicfi_el.remove(_absorbed)
                    if _local in ("Fr", "To") and len(_absorbed) == 0:
                        pass  # discard empty stray Fr/To shells
                    else:
                        apphdr.insert(_lift_pos, _absorbed)
                        _lift_pos += 1
                    _changed = True

        # ── Problem 4: missing or incomplete Fr/To AppHdr blocks ──────────────
        # Handles two sub-cases:
        #   a) Fr/To entirely absent (stripped by _strip_empty_apphdr_frto_closes
        #      after their content + opening tags were all deleted) → create from scratch.
        #   b) Fr/To present but FIId/FinInstnId/BICFI chain broken or empty
        #      (after _balance_xml_tags restored the skeleton) → fill in missing levels.
        # In both cases the result is Fr/To > FIId > FinInstnId > BICFI(dummy).
        _dummy_bic_fr = "DEUTDEFFXXX"
        _dummy_bic_to = "CHASUS33XXX"
        for _insert_pos, frto_local, dummy_bic in ((0, "Fr", _dummy_bic_fr), (1, "To", _dummy_bic_to)):
            frto_el = next((c for c in apphdr
                            if isinstance(c.tag, str)
                            and etree.QName(c.tag).localname == frto_local), None)
            if frto_el is None:
                # Create the whole Fr/To > FIId > FinInstnId > BICFI chain
                wrapper_tag = f"{{{ah_ns}}}{frto_local}" if ah_ns else frto_local
                fiid_tag    = f"{{{ah_ns}}}FIId"         if ah_ns else "FIId"
                fin_tag     = f"{{{ah_ns}}}FinInstnId"   if ah_ns else "FinInstnId"
                bicfi_tag   = f"{{{ah_ns}}}BICFI"        if ah_ns else "BICFI"
                frto_el = etree.Element(wrapper_tag)
                fiid_el = etree.SubElement(frto_el, fiid_tag)
                fin_el  = etree.SubElement(fiid_el, fin_tag)
                bicfi_el = etree.SubElement(fin_el, bicfi_tag)
                bicfi_el.text = dummy_bic
                apphdr.insert(_insert_pos, frto_el)
                _changed = True
                continue
            # Fr/To exists — ensure FIId inside it
            fiid_el = next((c for c in frto_el
                            if isinstance(c.tag, str)
                            and etree.QName(c.tag).localname == "FIId"), None)
            if fiid_el is None:
                fiid_tag = f"{{{ah_ns}}}FIId" if ah_ns else "FIId"
                fiid_el = etree.SubElement(frto_el, fiid_tag)
                _changed = True
            # Ensure FinInstnId inside FIId
            fin_el = next((c for c in fiid_el
                           if isinstance(c.tag, str)
                           and etree.QName(c.tag).localname == "FinInstnId"), None)
            if fin_el is None:
                fin_ns = etree.QName(fiid_el.tag).namespace or ah_ns
                fin_tag = f"{{{fin_ns}}}FinInstnId" if fin_ns else "FinInstnId"
                fin_el = etree.SubElement(fiid_el, fin_tag)
                _changed = True
            # AppHdr FinInstnId may only contain BICFI (CBPR+ restriction).
            # Strip everything except BICFI; inject dummy BICFI if none present.
            _allowed_in_apphdr_fininstnid = {"BICFI"}
            for _child in list(fin_el):
                if not isinstance(_child.tag, str):
                    continue
                if etree.QName(_child.tag).localname not in _allowed_in_apphdr_fininstnid:
                    fin_el.remove(_child)
                    _changed = True
            if not [c for c in fin_el if isinstance(c.tag, str)]:
                fin_ns = etree.QName(fin_el.tag).namespace or ah_ns
                bicfi_tag = f"{{{fin_ns}}}BICFI" if fin_ns else "BICFI"
                bicfi_el = etree.SubElement(fin_el, bicfi_tag)
                bicfi_el.text = dummy_bic
                _changed = True

        # ── Problem 2: strip stray AppHdr-only tags from BusMsgEnvlp level ──
        for child in list(root):
            if not isinstance(child.tag, str):
                continue
            if (etree.QName(child.tag).localname in self._APPHDR_ONLY_TAGS
                    and child is not apphdr):
                root.remove(child)
                _changed = True

        # Known ISO 20022 message root elements that must live inside Document —
        # shared by Problem 1 (lift stray roots out of AppHdr alongside Document)
        # and Problem 5.7 (re-home children stranded before an empty root) below.
        _MSG_ROOT_LOCALS_EARLY = {
            "FIToFICstmrCdtTrf", "FICdtTrf", "FIToFICstmrDrctDbt", "PmtRtr",
            "PmtStsRpt", "FIToFIPmtStsRpt", "FIToFIPmtCxlReq",
            "CstmrPmtCxlReq", "FIToFIPmtCxlRpt", "BkToCstmrAcctRpt",
            "BkToCstmrStmt", "BkToCstmrDbtCdtNtfctn", "NtfctnToRcv",
            "NtfctnToRcvCxlAdvc", "RsltnOfInvstgtn", "CstmrCdtTrfInitn",
            "CstmrDrctDbtInitn", "CstmrPmtStsRpt",
        }

        # ── Problem 1: Document nested inside AppHdr (at any depth) ──────────────
        # Handles: direct AppHdr child AND LLM-hallucinated Document inside
        # party blocks like <Fr><FIId><Document> (can't be recovered by the
        # AppHdr-only-tag lifter since "Document" is not in _APPHDR_ONLY_TAGS).
        doc_in_apphdr = next(
            (el for el in apphdr.iter()
             if el is not apphdr
             and isinstance(el.tag, str)
             and etree.QName(el.tag).localname == "Document"),
            None
        )
        if doc_in_apphdr is not None:
            doc_parent = doc_in_apphdr.getparent()
            if doc_parent is not None:
                doc_parent.remove(doc_in_apphdr)
            doc_in_envlp = next((c for c in root
                                 if isinstance(c.tag, str)
                                 and etree.QName(c.tag).localname == "Document"), None)
            if doc_in_envlp is None:
                apphdr_idx = list(root).index(apphdr)
                doc_in_apphdr.tail = None
                root.insert(apphdr_idx + 1, doc_in_apphdr)
            _changed = True

            # Both <Document>'s opening tag AND a message-root's opening tag
            # (e.g. <PmtRtr>) can be deleted TOGETHER (e.g. "</AppHdr>" and
            # "<Document xmlns=...>" both removed) — _balance_xml_tags then
            # reinserts the synthetic empty <Document> open INSIDE AppHdr too,
            # but its real message-root SIBLING (already fully populated with
            # the user's actual data, e.g. PmtRtr) is left behind as a direct
            # AppHdr child, since this handler only ever looked for "Document"
            # by name. Lifting Document alone here, then letting the generic
            # mandatory-field machinery later notice Document is "missing" its
            # message-root child, fabricates a FRESH placeholder root instead of
            # using the real one still sitting in AppHdr — duplicating the
            # element with garbage data while leaving the real one stranded.
            # Move any direct AppHdr child that is a recognised message-root
            # element OUT alongside Document, into the now-relocated Document
            # (or, if Document unexpectedly already had its own children, drop
            # it as a duplicate of what Document already holds).
            _doc_now = (doc_in_apphdr if doc_in_envlp is None else doc_in_envlp)
            for _stray_root in [c for c in list(apphdr)
                                 if isinstance(c.tag, str)
                                 and etree.QName(c.tag).localname in _MSG_ROOT_LOCALS_EARLY]:
                apphdr.remove(_stray_root)
                _stray_root.tail = None
                if len(_doc_now) == 0:
                    _doc_now.append(_stray_root)
                # else: Document already has real content — the AppHdr copy is
                # a stale duplicate (already-handled case); drop it silently.

        # ── Problem 5.7: orphaned children stranded BEFORE an empty message-root
        #    element, both as direct BusMsgEnvlp siblings ─────────────────────────
        # When BOTH <Document>'s opening tag AND a message-root element's (e.g.
        # <PmtRtr>) opening tag are deleted simultaneously, _balance_xml_tags can
        # only reinsert each synthetic open immediately before its own orphaned
        # close — it has no way to know the root's real children (CdtrAgt, Cdtr,
        # OrgnlTxRef, …) belong INSIDE the root, not as siblings preceding it. The
        # result: <PmtRtr></PmtRtr> ends up EMPTY, with its rightful children
        # sitting as BusMsgEnvlp-level siblings just before it. Detect that shape
        # (an empty/near-empty message-root local immediately preceded by
        # non-AppHdr/Document siblings) and move those siblings inside it BEFORE
        # Problem 6 below tries to relocate the (then correctly populated) root
        # into Document. (_MSG_ROOT_LOCALS_EARLY defined above, with Problem 1.)
        _root_children = list(root)
        for _rc in _root_children:
            if not isinstance(_rc.tag, str):
                continue
            if etree.QName(_rc.tag).localname not in _MSG_ROOT_LOCALS_EARLY:
                continue
            if len(_rc) > 0:
                continue  # already has children — not the empty-root case
            _rc_idx = _root_children.index(_rc)
            _stray_preceding = []
            for _sib in _root_children[:_rc_idx]:
                if not isinstance(_sib.tag, str):
                    continue
                _sib_local = etree.QName(_sib.tag).localname
                if _sib is apphdr or _sib_local in ("Document",) or _sib_local in _MSG_ROOT_LOCALS_EARLY:
                    continue
                _stray_preceding.append(_sib)
            if not _stray_preceding:
                continue
            for _sib in _stray_preceding:
                root.remove(_sib)
                _sib.tail = None
                _rc.append(_sib)
            _changed = True

        # ── Problem 6: body elements orphaned as BusMsgEnvlp siblings of Document ──
        # Occurs when the balance engine reconstructs an empty <Document> and places
        # real body elements (CstmrPmtCxlReq, Assgnmt, Undrlyg, …) as BusMsgEnvlp
        # direct children instead of inside Document.  Also fixes Document namespace
        # when it inherited the envelope namespace instead of the message namespace.
        #
        # Known ISO 20022 message root elements that must live inside Document:
        _MSG_ROOT_LOCALS = {
            # pacs
            "FIToFICstmrCdtTrf", "CdtTrfTxInf", "FICdtTrf",
            "FIToFICstmrDrctDbt", "PmtRtr", "PmtStsRpt",
            "FIToFIPmtStsRpt", "FIToFIPmtCxlReq",
            # camt
            "CstmrPmtCxlReq", "FIToFIPmtCxlRpt",
            "BkToCstmrAcctRpt", "BkToCstmrStmt", "BkToCstmrDbtCdtNtfctn",
            "NtfctnToRcv", "NtfctnToRcvCxlAdvc", "RsltnOfInvstgtn",
            # pain
            "CstmrCdtTrfInitn", "CstmrDrctDbtInitn", "CstmrPmtStsRpt",
            # shared body elements that appear under message root
            "Assgnmt", "Undrlyg", "OrgnlPmtInfAndCxl", "GrpHdr",
        }
        _doc_el = next((c for c in root
                        if isinstance(c.tag, str)
                        and etree.QName(c.tag).localname == "Document"), None)
        _orphaned_body = [
            c for c in list(root)
            if isinstance(c.tag, str)
            and c is not apphdr
            and etree.QName(c.tag).localname in _MSG_ROOT_LOCALS
        ]
        if _orphaned_body:
            if _doc_el is None:
                # Create Document placeholder — namespace will be fixed below
                _doc_el = etree.Element("Document")
                apphdr_idx = list(root).index(apphdr)
                root.insert(apphdr_idx + 1, _doc_el)
            # Move orphaned body elements into Document, preserving order
            _insert_at = len(_doc_el)
            for _body_el in _orphaned_body:
                root.remove(_body_el)
                _body_el.tail = None
                _doc_el.insert(_insert_at, _body_el)
                _insert_at += 1
            _changed = True

        # Fix Document namespace when it's wrong (inherited envelope ns or bare).
        # Derive the correct namespace from MsgDefIdr, child ns, or body element names.
        _DOC_BODY_NS_MAP = {
            # camt
            "CstmrPmtCxlReq":        "urn:iso:std:iso:20022:tech:xsd:camt.055.001.08",
            "FIToFIPmtCxlReq":        "urn:iso:std:iso:20022:tech:xsd:camt.056.001.08",
            "BkToCstmrAcctRpt":       "urn:iso:std:iso:20022:tech:xsd:camt.052.001.08",
            "BkToCstmrStmt":          "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08",
            "BkToCstmrDbtCdtNtfctn":  "urn:iso:std:iso:20022:tech:xsd:camt.054.001.08",
            "NtfctnToRcv":            "urn:iso:std:iso:20022:tech:xsd:camt.057.001.06",
            "RsltnOfInvstgtn":        "urn:iso:std:iso:20022:tech:xsd:camt.029.001.09",
            # pacs
            "FIToFICstmrCdtTrf":      "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
            "FICdtTrf":               "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08",
            "FIToFICstmrDrctDbt":     "urn:iso:std:iso:20022:tech:xsd:pacs.003.001.08",
            "PmtRtr":                 "urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09",
            "FIToFIPmtStsRpt":        "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10",
            # pain
            "CstmrCdtTrfInitn":       "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09",
            "CstmrDrctDbtInitn":      "urn:iso:std:iso:20022:tech:xsd:pain.008.001.09",
            "CstmrPmtStsRpt":         "urn:iso:std:iso:20022:tech:xsd:pain.002.001.09",
        }
        if _doc_el is not None:
            _doc_ns = etree.QName(_doc_el.tag).namespace or ""
            _envlp_ns = etree.QName(root.tag).namespace or ""
            _doc_has_wrong_ns = _doc_ns == _envlp_ns or not _doc_ns
            # Body-root-name says a DIFFERENT message family than the (otherwise
            # "valid-looking") current namespace. Real-world cause: a message was
            # mislabeled (MsgDefIdr/Document xmlns say pacs.008) but its actual
            # body root is a different message's root element (e.g. PmtRtr,
            # pacs.004's root — only ever present in a genuine pacs.004 message).
            # Recovery from a SEPARATE structural defect (Document+body-root
            # opening tags both deleted) can surface this for the first time once
            # the body is correctly reassembled — at that point the schema
            # validator reports the body root as "Unexpected field" against the
            # WRONG (mislabeled) namespace, and without this check the generic
            # mandatory-field machinery "fixes" that by discarding the real body
            # and fabricating a fresh one of the WRONG type instead. The body
            # content is what the user actually has — trust it over the label.
            def _ns_family(_ns: str) -> str:
                # "...xsd:pacs.004.001.09" -> "pacs.004"; tolerant of version/
                # variant differences (.001.06 vs .001.08 is the SAME message,
                # not a mismatch) — only the family (first two dot-segments)
                # indicates a genuinely different message type.
                _tail = _ns.rsplit(":", 1)[-1] if _ns else ""
                _segs = _tail.split(".")
                return ".".join(_segs[:2]) if len(_segs) >= 2 else _tail

            _body_root_local = next(
                (etree.QName(c.tag).localname for c in _doc_el
                 if isinstance(c.tag, str) and etree.QName(c.tag).localname in _DOC_BODY_NS_MAP),
                None,
            )
            _body_root_family_mismatch = bool(
                _body_root_local
                and _ns_family(_DOC_BODY_NS_MAP[_body_root_local]) != _ns_family(_doc_ns)
            )
            if not _doc_has_wrong_ns and _body_root_family_mismatch:
                _doc_has_wrong_ns = True
            if _doc_has_wrong_ns:
                _correct_ns = None
                # 1. Body-root-name mapping wins WHEN body and namespace actively
                #    disagree on FAMILY (mislabeled-message case above) — MsgDefIdr
                #    is the very thing that's wrong there, so it must not win this
                #    round. A same-family version/variant difference (e.g.
                #    camt.057.001.06 vs .001.08) is NOT a mismatch — leave ns alone
                #    and let MsgDefIdr (step 2) or existing ns stand.
                if _body_root_family_mismatch:
                    _correct_ns = _DOC_BODY_NS_MAP[_body_root_local]
                # 2. Try MsgDefIdr in AppHdr (normal case: ns inherited envelope's,
                #    MsgDefIdr is the surviving source of truth).
                if not _correct_ns:
                    _mdi = apphdr.findtext(".//{*}MsgDefIdr") if apphdr is not None else None
                    if _mdi:
                        _correct_ns = f"urn:iso:std:iso:20022:tech:xsd:{_mdi.strip()}"
                if not _correct_ns:
                    # 3. Check if any Document descendant carries an ISO 20022 ns
                    for _ch in _doc_el.iter():
                        if isinstance(_ch.tag, str):
                            _ch_ns = etree.QName(_ch.tag).namespace or ""
                            if _ch_ns and "iso:20022" in _ch_ns:
                                _correct_ns = _ch_ns
                                break
                if not _correct_ns and _body_root_local:
                    # 4. Fall back to the body-root mapping unconditionally.
                    _correct_ns = _DOC_BODY_NS_MAP[_body_root_local]
                if _correct_ns:
                    # Mutating .tag in place does NOT register the namespace in
                    # the element's nsmap, so lxml serializes it with a freshly
                    # auto-generated prefix (<ns1:Document xmlns:ns1="...">)
                    # instead of the default-namespace style every CBPR+ message
                    # uses (<Document xmlns="...">) — both are valid XML, but the
                    # prefixed form fails every "local-name()"-blind consumer
                    # downstream and looks corrupted to users. Must rebuild the
                    # element with nsmap={None: _correct_ns} (same pattern as
                    # _rebuild_missing_document_wrapper) and re-namespace any
                    # descendants stuck in the wrong (envelope/bare) ns too —
                    # the balance engine's synthetic <Document> open tag carries
                    # no namespace at all, so children inherited the ambient
                    # envelope default-ns just like Document itself did.
                    _new_doc_el = etree.Element(f"{{{_correct_ns}}}Document", nsmap={None: _correct_ns})
                    for _k, _v in _doc_el.attrib.items():
                        _new_doc_el.set(_k, _v)
                    _new_doc_el.text = _doc_el.text
                    for _dchild in _doc_el:
                        if isinstance(_dchild.tag, str):
                            _new_doc_el.append(self._rens_subtree(_dchild, _doc_ns, _correct_ns))
                    _doc_el.getparent().replace(_doc_el, _new_doc_el)
                    _doc_el = _new_doc_el
                    _changed = True

        # ── Problem 5: duplicate AppHdr children (Fr, To, BizMsgIdr, …) ─────────
        # _balance_xml_tags + Problem 3b can leave duplicate AppHdr-level tags:
        # - An empty <To> from the balance engine alongside the real <To> rebuilt
        #   by Problem 3.
        # - An empty <BizMsgIdr/> lifted from inside Fr/FIId alongside the real
        #   <BizMsgIdr> that was already at AppHdr level.
        # For each tag that appears more than once: keep the first element that
        # has non-empty content (text or children); remove the rest.
        _seen_locals: dict = {}
        for _ch in list(apphdr):
            if not isinstance(_ch.tag, str):
                continue
            _loc = etree.QName(_ch.tag).localname
            if _loc not in _seen_locals:
                _seen_locals[_loc] = _ch
            else:
                # Duplicate — decide which to keep
                _prev = _seen_locals[_loc]
                _prev_has = len(_prev) > 0 or (_prev.text and _prev.text.strip())
                _cur_has  = len(_ch)   > 0 or (_ch.text   and _ch.text.strip())
                if _cur_has and not _prev_has:
                    # Current has content, previous is empty — swap keeper
                    apphdr.remove(_prev)
                    _seen_locals[_loc] = _ch
                else:
                    # Previous is already the better (or equal) one — drop current
                    apphdr.remove(_ch)
                _changed = True

        if not _changed:
            return xml

        # Strip stale text/tail whitespace so pretty_print produces clean output
        etree.indent(root, space="    ")

        decl = ""
        m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
        if m:
            decl = m.group(1) + "\n"
        return decl + etree.tostring(root, encoding="unicode", pretty_print=True)

    def _build_nsmap(self, root: etree._Element) -> dict:
        nsmap: dict[str, str] = {}
        for el in root.iter():
            for p, u in (el.nsmap or {}).items():
                if p and p not in nsmap:
                    nsmap[p] = u
        return nsmap

    def _serialize(self, el: etree._Element) -> str:
        return etree.tostring(el, encoding="unicode", pretty_print=True)

    def _copy(self, el: etree._Element) -> etree._Element:
        return etree.fromstring(etree.tostring(el))

    def _xpath_of(self, el: etree._Element) -> str:
        """Return an indexed xpath like /Document/FIToFICstmrCdtTrf/CdtTrfTxInf[2]/PmtId."""
        parts, cur = [], el
        while cur is not None:
            parent = cur.getparent()
            local = etree.QName(cur.tag).localname
            if parent is not None:
                sibs = [c for c in parent
                        if isinstance(c.tag, str) and etree.QName(c.tag).localname == local]
                idx = sibs.index(cur) + 1
                parts.append(f"{local}[{idx}]" if len(sibs) > 1 else local)
            else:
                parts.append(local)
            cur = parent
        parts.reverse()
        return "/" + "/".join(parts)

    # ── Walk dot-path ─────────────────────────────────────────────────────────

    @staticmethod
    def _path_candidate_children(node: etree._Element,
                                 unwrap_envelope: bool) -> list[etree._Element]:
        """Children of `node` to match the next path segment against.

        Normally just the direct children, in document order. For the FIRST
        path segment (`unwrap_envelope`) we ALSO expose the children of any
        <BusMsgEnvlp> wrapper at its position. Validator paths are
        envelope-agnostic — they start at "Document"/"AppHdr" — but inside a
        <BulkMessages> container (and in a single enveloped file) each message's
        Document is nested one level down under <BusMsgEnvlp> alongside <AppHdr>.
        Without this, a path like Document.FIDrctDbt… walked from <BulkMessages>
        only sees Documents that are DIRECT children (bare messages) and silently
        resolves against the wrong sibling. The wrapper element itself is kept in
        the list too, so a path that genuinely starts with "BusMsgEnvlp" still
        matches. Deeper segments stay strict."""
        if not unwrap_envelope:
            return list(node)
        out: list[etree._Element] = []
        for child in node:
            out.append(child)
            if (isinstance(child.tag, str)
                    and etree.QName(child.tag).localname == "BusMsgEnvlp"):
                out.extend(list(child))
        return out

    def _find_all_paths(self, root: etree._Element, parts: list[str]) -> list[etree._Element]:
        if not parts:
            return [root]
        root_local = etree.QName(root.tag).localname
        first_tag = re.sub(r'\[\d+\]', '', parts[0])
        start = 1 if root_local == first_tag else 0
        current = [root]
        for depth, part in enumerate(parts[start:]):
            m = re.match(r'^([^\[]+)(?:\[(\d+)\])?$', part)
            if not m:
                return []
            tag_name = m.group(1)
            target_idx = int(m.group(2)) if m.group(2) else None

            next_nodes = []
            for node in current:
                count = 0
                for child in self._path_candidate_children(node, depth == 0):
                    if isinstance(child.tag, str) and etree.QName(child.tag).localname == tag_name:
                        count += 1
                        if target_idx is None or count == target_idx:
                            next_nodes.append(child)
            current = next_nodes
            if not current:
                break
        return current

    def _walk_dot_path(self, root: etree._Element, parts: list[str]) -> Optional[etree._Element]:
        """
        Walk root following dot-path parts by local-name (with optional [index]).
        E.g. ["Document","FIToFICstmrCdtTrf","CdtTrfTxInf[2]","PmtId"]
        Returns the deepest element found, or None. Uses line_hint to break ties.
        """
        matches = self._find_all_paths(root, parts)
        if not matches:
            return None
        lh = getattr(self, "_line_hint", None)
        if lh is not None:
            return min(matches, key=lambda e: abs((e.sourceline or 0) - lh))
        return matches[0]

    def _child_exists(self, parent: etree._Element, local_name_with_idx: str) -> Optional[etree._Element]:
        m = re.match(r'^([^\[]+)(?:\[(\d+)\])?$', local_name_with_idx)
        if not m:
            return None
        tag_name = m.group(1)
        # When checking existence for a specific target, if no index is given, default to 1
        # because _child_exists is used to see if the EXACT target leaf exists.
        target_idx = int(m.group(2)) if m.group(2) else 1
        count = 0
        for child in parent:
            if isinstance(child.tag, str) and etree.QName(child.tag).localname == tag_name:
                count += 1
                if count == target_idx:
                    return child
        return None

    def _recover_target_from_message(self, root: etree._Element,
                                      msg: str, fix_hint: str) -> Optional[etree._Element]:
        """
        Extract candidate tag names from msg + fix_hint and find the first
        matching element in the document. Used when path="/" or path is unparseable.
        Looks for <Tag> or 'Tag' patterns in the text.

        When the message mentions both a container (e.g. <Fr>) and a leaf
        (e.g. BICFI), descend into the container to return the leaf element —
        otherwise _fix_value receives an opaque container and bails out.
        """
        text = f"{msg} {fix_hint}"
        # Tags mentioned in the text — look for <TagName> first (highest signal)
        tags = re.findall(r'<(\w+)>', text)
        # Then capitalized words that look like ISO tags
        if not tags:
            tags = re.findall(r'\b([A-Z][a-zA-Z]{1,15})\b', text)
            # Filter out common English words
            skipwords = {
                "If", "Then", "Must", "Should", "When", "Add", "Use", "The",
                "This", "Valid", "Invalid", "Missing", "Error", "Warning",
                "Required", "Mandatory", "Rule", "Field", "Value", "Code",
                "ISO", "CBPR", "And", "Or", "Not", "For", "BIC", "UETR",
            }
            tags = [t for t in tags if t not in skipwords]

        if not tags:
            return None

        # Known leaf tags whose values carry the actual constraint. If any
        # is mentioned alongside a container tag, prefer the leaf inside that
        # container.
        LEAF_TAGS = {
            "BICFI", "IBAN", "Othr", "Id", "Ctry", "CtryOfRes", "AdrLine",
            "Nm", "BirthDt", "PrvcOfBirth", "CityOfBirth", "CtryOfBirth",
            "InstrId", "EndToEndId", "TxId", "UETR", "MsgId", "BizMsgIdr",
            "MsgDefIdr", "CreDt", "CreDtTm",
        }
        # Containers that wrap one of the above leaves; ordered roughly
        # leaf-first so a Fr+BICFI message resolves the BICFI inside Fr.
        CONTAINER_TAGS = {
            "Fr", "To", "InstgAgt", "InstdAgt", "FwdgAgt",
            "DbtrAgt", "CdtrAgt", "IntrmyAgt1", "IntrmyAgt2", "IntrmyAgt3",
            "PrvsInstgAgt1", "PrvsInstgAgt2", "PrvsInstgAgt3",
            "Dbtr", "Cdtr", "UltmtDbtr", "UltmtCdtr", "InitgPty",
            "FIId", "FinInstnId", "PstlAdr",
            "Assgnr", "Assgne", "Assgnmt",
        }

        mentioned_leaves     = [t for t in tags if t in LEAF_TAGS]
        mentioned_containers = [t for t in tags if t in CONTAINER_TAGS]

        # Also catch bare-word leaf mentions in the prose (e.g. "BICFI" without
        # angle brackets, "must match BICFI...").
        if mentioned_containers and not mentioned_leaves:
            for leaf in LEAF_TAGS:
                if re.search(r'\b' + re.escape(leaf) + r's?\b', text):
                    mentioned_leaves.append(leaf)
                    break

        # ── Canonical fix direction for AppHdr ↔ doc-body BIC mismatches ──
        # The CdtTrfTxInf/InstgAgt and InstdAgt BICFIs are the source of truth
        # for the transaction; AppHdr/Fr and AppHdr/To must mirror them. When
        # the message mentions BOTH an AppHdr-side container (Fr/To) AND a
        # doc-body container (InstgAgt/InstdAgt/IntrmyAgt*/etc.), put the
        # AppHdr side FIRST so the fixer lands there — the harvester will then
        # read the doc-body value across.
        APPHDR_CONTAINERS  = {"Fr", "To"}
        DOC_BODY_CONTAINERS = {
            "InstgAgt", "InstdAgt", "FwdgAgt", "DbtrAgt", "CdtrAgt",
            "IntrmyAgt1", "IntrmyAgt2", "IntrmyAgt3",
            "PrvsInstgAgt1", "PrvsInstgAgt2", "PrvsInstgAgt3",
            "Assgnr", "Assgne",
        }
        has_apphdr = any(c in APPHDR_CONTAINERS for c in mentioned_containers)
        has_doc    = any(c in DOC_BODY_CONTAINERS for c in mentioned_containers)
        if has_apphdr and has_doc:
            mentioned_containers = (
                [c for c in mentioned_containers if c in APPHDR_CONTAINERS]
                + [c for c in mentioned_containers if c not in APPHDR_CONTAINERS]
            )

        line_hint = getattr(self, "_line_hint", None)

        def _pick(candidates):
            """Return the candidate whose source line is closest to line_hint
            (when known), else the first candidate. Empty list → None."""
            if not candidates:
                return None
            if line_hint is None:
                return candidates[0]
            return min(
                candidates,
                key=lambda e: abs((e.sourceline or 0) - line_hint),
            )

        # If a container + leaf are both named, descend into the container
        # and return the matching leaf — that's the element _fix_value can act on.
        if mentioned_leaves and mentioned_containers:
            leaf = mentioned_leaves[0]
            leaf_candidates = []
            for cont in mentioned_containers:
                for cont_el in root.iter():
                    if not isinstance(cont_el.tag, str):
                        continue  # skip comment / processing-instruction nodes
                    if etree.QName(cont_el.tag).localname != cont:
                        continue
                    for desc in cont_el.iter():
                        if isinstance(desc.tag, str) and etree.QName(desc.tag).localname == leaf:
                            leaf_candidates.append(desc)
            picked = _pick(leaf_candidates)
            if picked is not None:
                return picked
            # Container present but leaf not found inside it — fall through
            # so we still return the container rather than nothing.

        # Default: pick the line-nearest element matching any mentioned tag.
        for tag in tags:
            matches = [el for el in root.iter()
                       if isinstance(el.tag, str) and etree.QName(el.tag).localname == tag]
            picked = _pick(matches)
            if picked is not None:
                return picked

        # ── Value-based recovery ──────────────────────────────────────────────
        # Enum/codelist errors quote the offending VALUE, not the element name,
        # e.g. "The value 'CLRG' is not valid. It must be one of the following
        # values : INDA, INGA." Locate the leaf element whose text IS that value
        # (line-nearest) so the caller can correct it.
        bad_m = re.search(r"(?:value|code)\s+'([^']+)'|'([^']+)'\s+is\s+not\s+(?:a\s+)?valid",
                          text, re.I)
        bad_val = next((g for g in (bad_m.groups() if bad_m else ()) if g), None)
        if bad_val:
            val_matches = [el for el in root.iter()
                           if isinstance(el.tag, str) and len(el) == 0
                           and (el.text or "").strip() == bad_val]
            picked = _pick(val_matches)
            if picked is not None:
                return picked
        return None

    # ── Insert deleted mandatory sibling(s) (XSD "not expected" → missing) ────

    def _try_insert_missing_sibling(self, root: etree._Element, xml: str,
                                    code: str, msg: str, fix_hint: str,
                                    explicit_parent: Optional[etree._Element] = None,
                                    explicit_missing: str = "") -> Optional["FixSuggestion"]:
        """
        Insert a mandatory element that was deleted entirely (classic case:
        AppHdr/Fr or AppHdr/To removed) back into its correct schema slot,
        namespaced to the parent and indented to match the document.

        Two entry points:
          • Implicit — the XSD raised "The element 'X' is not expected here …
            another mandatory element is missing before this one. One of the
            following elements is expected : 'A, B'". We parse X and the
            expected list, and (because the validator surfaces only ONE ordering
            error per pass) reinsert EVERY missing mandatory sibling at once so
            both Fr and To come back in a single fix.
          • Explicit — the caller already knows the parent element and the exact
            missing tag (used for the validator's per-field "mandatory header
            element is missing" issues); we insert just that one.

        Returns None (caller declines) when no buildable missing element can be
        identified.
        """
        text = f"{msg} {fix_hint}"
        line_hint = getattr(self, "_line_hint", None)

        if explicit_missing:
            # Explicit mode: parent + missing tag are already known.
            parent = explicit_parent
            if parent is None:
                return None
            found_elem = ""
            exp_candidates = [explicit_missing]
            self._targeted_insert_flag = False
        else:
            # 1. The element the validator stumbled on ("not expected").
            # Matches both:
            #   "element 'PmtMtd' is not expected here"  (XSD-style)
            #   "Unexpected field 'PmtMtd' found here"   (custom validator style)
            m_found = re.search(
                r"(?:element '([\w:{}.\-]+)' is not expected"
                r"|[Uu]nexpected\s+(?:field|element)\s+'([\w:{}.\-]+)')",
                text, re.I,
            )
            if not m_found:
                return None
            _raw_found = (m_found.group(1) or m_found.group(2) or "")
            found_elem = _raw_found.split('}')[-1].split(':')[-1]
            if not found_elem:
                return None

            # 2. The candidate elements the schema expected at that slot. They are
            #    quoted after "expected", e.g. expected : 'CharSet, Fr'  →  the
            #    quoted blob may itself be a comma-separated list.
            # For "right before or instead of 'PmtMtd': 'PmtInfId'" patterns,
            # exclude the found_elem itself from candidates (it already exists).
            exp_candidates = []
            # _targeted_insert=True means the found element IS valid/correctly
            # named but just misplaced — the misnaming check must be skipped
            # (see below) so the missing sibling is actually inserted.
            _targeted_insert = False
            # Primary extraction: targeted pattern for "instead of 'X': 'Y'" or
            # "instead of 'X': 'Y, Z'" where Y (and Z, …) are the missing
            # predecessor(s) — the SR2026 fallback message
            # (Layer2Mixin._simplify_error_message's final "Unexpected field"
            # branch) lists every missing field in ONE comma-separated quoted
            # blob, e.g. "...instead of 'StsRsnInf': 'OrgnlInstrId,
            # OrgnlEndToEndId'." This avoids apostrophe-in-words (e.g. "It's")
            # misaligning the generic quote-pair finder below.
            _m_instead = re.search(
                r"instead of '[\w]+'[^']*'([\w][\w,\s]*)'", text, re.I
            )
            if _m_instead:
                for _inst_tok in re.split(r"[,\s]+", _m_instead.group(1).strip()):
                    if _inst_tok and _inst_tok != found_elem and _inst_tok not in exp_candidates:
                        exp_candidates.append(_inst_tok)
                        _targeted_insert = True
            # Also handle "right before 'X'" pattern
            _m_before = re.search(
                r"(?:right\s+)?before '([\w]+)'", text, re.I
            )
            if _m_before:
                _bef_tok = _m_before.group(1).strip()
                if _bef_tok and _bef_tok != found_elem and _bef_tok not in exp_candidates:
                    exp_candidates.append(_bef_tok)
                    _targeted_insert = True
            # Fallback: extract 'Token' patterns from the text, but use only
            # 1-word CamelCase tokens (XSD element names) to avoid prose words.
            m_exp = re.search(r"expected\s*:?\s*(.+)$", text, re.I | re.S)
            if m_exp:
                for blob in re.findall(r"'([^']+)'", m_exp.group(1)):
                    for tok in re.split(r"[,\s|]+", blob):
                        tok = tok.strip().split('}')[-1].split(':')[-1]
                        # Only accept CamelCase single-word XSD element names
                        if (tok and re.match(r'^[A-Z][A-Za-z0-9]{1,}$', tok)
                                and tok not in exp_candidates and tok != found_elem):
                            exp_candidates.append(tok)

            # 3. Locate the offending element in the live document — issue-path
            #    aware first (line numbers drift across batch roll-forward).
            matches = [el for el in root.iter()
                       if isinstance(el.tag, str)
                       and etree.QName(el.tag).localname == found_elem]
            if not matches:
                return None
            found_el = self._pick_candidate(matches)

            parent = found_el.getparent()
            if parent is None:
                return None
            # Propagate targeted-insert flag so the misnaming check is skipped
            # for "element exists but missing predecessor" cases.
            self._targeted_insert_flag = _targeted_insert

        parent_local = etree.QName(parent.tag).localname

        present = {etree.QName(c.tag).localname for c in parent
                   if isinstance(c.tag, str)}
        # Prefer the BUSINESS message type (Document body, e.g. pacs.008) over
        # the BAH type (head.001). _detect_msg_type returns whichever appears
        # first in the file, which is the AppHdr's head.001 namespace — but
        # mandatory_fields and templates are keyed by the business type.
        msg_type = _detect_msg_type(xml)
        _all_types = re.findall(r"([a-z]+\.\d{2,3}\.\d{3}\.\d{2})", xml)
        _biz = next((t for t in _all_types if not t.startswith("head.")), "")
        if _biz:
            msg_type = _biz
        order = _kb_get(f"tag_insertion_order.{parent_local}")
        order = order if isinstance(order, list) else None

        # Load the XSD type map + parent path NOW (was loaded later) so the
        # _buildable gate can also accept tags the XSD can build — not only tags
        # with a KB/_TEMPLATES entry. Without this, removing a mandatory tag that
        # has no hand-written template (the long tail of ISO 20022 elements) was
        # silently dropped from `wanted` and never reinserted.
        parent_ns = etree.QName(parent.tag).namespace or ""
        parent_path = self._local_name_path(parent)
        # Use head.001 XSD for elements in AppHdr scope; message XSD otherwise.
        # Guard: Document nested inside AppHdr (BusMsgEnvlp) belongs to message XSD.
        _pp_ah_idx = next((i for i, p in enumerate(parent_path) if p == "AppHdr"), -1)
        _pp_doc_after = any(p == "Document" for p in parent_path[_pp_ah_idx + 1:]) if _pp_ah_idx >= 0 else False
        _pp_in_apphdr = _pp_ah_idx >= 0 and not _pp_doc_after
        xsd_path  = (self._get_apphdr_xsd_path(xml) if _pp_in_apphdr
                     else self._get_xsd_path(xml))
        tmap      = _XsdTypeMap.get(xsd_path) if xsd_path else None
        rules_idx = _RulesIndex.get(msg_type) if msg_type else None
        # Resolve the parent's XSD type once; used to test child build-ability.
        _parent_type = None
        if tmap is not None:
            try:
                _parent_type = tmap.type_of_path(parent_path)
            except Exception:
                _parent_type = None
            if not _parent_type:
                _parent_type = tmap.element_type.get(parent_local)

        def _xsd_buildable(tag: str) -> bool:
            """True when the XSD type map can build this child of `parent`."""
            if tmap is None:
                return False
            # Direct child-of-parent type, else a globally declared element.
            t = (tmap.get_child_type(_parent_type, tag) if _parent_type else None)
            if not t:
                try:
                    t = tmap.type_of_path(parent_path + [tag])
                except Exception:
                    t = None
            if not t:
                t = tmap.element_type.get(tag)
            return bool(t)

        def _buildable(tag: str) -> bool:
            return (bool(_kb_tag_template(tag, msg_type))
                    or tag in _TEMPLATES
                    or _xsd_buildable(tag))

        # 4. Everything we should (re)insert, restricted to those MISSING from
        #    the parent and buildable (so we never inject optional noise like
        #    CharSet, which has no template), ordered by the KB's
        #    tag_insertion_order so they go back in schema sequence.
        #    Implicit mode reinserts the error's expected list UNION the KB's
        #    mandatory children (fix-all from a single error); explicit mode
        #    inserts only the one named field (the validator already enumerated
        #    each missing field as its own issue).
        # XSD-driven mandatory children of this parent that are ABSENT. The KB
        # mandatory_fields list is curated and incomplete (e.g. it omits DbtrAgt,
        # whose deletion only surfaces as "DbtrAgtAcct is not expected here").
        # The XSD sequence is authoritative: any child with minOccurs != 0 that
        # isn't present must be reinserted. This is what lets ANY removed
        # mandatory tag come back, not just the hand-listed ones.
        _xsd_children = []
        _xsd_order_names: list[str] = []
        # ALL children of the parent's type regardless of kind — choice parents
        # (e.g. Party50Choice Assgnr/Assgne: Pty | Agt) need the same
        # wrong-nesting protection and validity gating as sequences.
        _pchildren_all: list = []
        _pchild_names: list[str] = []
        if tmap is not None and _parent_type:
            _pinfo = tmap.type_info.get(_parent_type, {})
            _pchildren_all = _pinfo.get("children", []) or []
            _pchild_names = [c["name"] for c in _pchildren_all]
            if _pinfo.get("kind") == "sequence":
                _xsd_children = _pchildren_all
                _xsd_order_names = _pchild_names
        _xsd_mandatory_absent = [
            c["name"] for c in _xsd_children
            if c.get("min", "1") != "0" and c["name"] not in present
        ]

        # `found_elem` is not a valid direct child of `parent` at all (e.g.
        # PrvsInstgAgt2 stranded in FICdtTrf, or a bare FinInstnId inside the
        # Assgne CHOICE) — check whether it's a valid child of one of parent's
        # OTHER child container types (e.g. CdtTrfTxInf / the Agt choice
        # member). If so this is a wrong-nesting-level error: the element
        # belongs inside an EXISTING sibling container, not a missing
        # predecessor at this level. Decline so _try_wrap_orphaned_block can
        # relocate it correctly instead of us inserting unrelated noise
        # (e.g. SplmtryData) that doesn't fix the real placement issue.
        if (not explicit_missing and tmap is not None and _parent_type
                and _pchild_names and found_elem not in _pchild_names):
            for _c in _pchildren_all:
                _ctype = tmap.get_child_type(_parent_type, _c["name"])
                if not _ctype:
                    continue
                _cchildren = {g["name"] for g in
                              tmap.type_info.get(_ctype, {}).get("children", [])}
                if found_elem in _cchildren:
                    return None

        if explicit_missing:
            candidate_tags = list(exp_candidates)
        else:
            candidate_tags = (list(exp_candidates)
                              + self._kb_mandatory_children(parent_local, msg_type)
                              + _xsd_mandatory_absent)
        # XSD-OPTIONAL children must NOT be force-inserted when derived from the
        # KB/XSD mandatory lists — those are speculative (we don't know the user
        # wants them). But when the validator's error message EXPLICITLY names a
        # tag as expected at this position (exp_candidates), it means the tag was
        # present and the user removed it — we MUST reinsert it regardless of
        # min=0. Only block optional tags that came from _kb_mandatory_children or
        # _xsd_mandatory_absent (not explicitly named by the validator).
        _xsd_optional = {c["name"] for c in _xsd_children if c.get("min", "1") == "0"}
        _exp_set = set(exp_candidates)  # explicitly named by validator error
        wanted: list[str] = []
        for tag in candidate_tags:
            if tag in wanted or tag in present or not _buildable(tag):
                continue
            # XSD-validity gate: never insert a tag the parent's resolved type
            # does not accept AT ALL. KB mandatory lists are keyed by LOCAL
            # name, and generic names (Agt, Id, Nm) collide across contexts —
            # e.g. parent Agt = BranchAndFinancialInstitutionIdentification
            # must not receive the Pty/Agt children of a Party-choice "Agt".
            if (not explicit_missing and _pchild_names
                    and tag not in _pchild_names):
                continue
            # Allow optional tags only when the validator explicitly named them
            # (user removed them) or when explicit_missing mode is active.
            if not explicit_missing and tag in _xsd_optional and tag not in _exp_set:
                continue
            # Never insert AppHdr-only tags into BusMsgEnvlp — they belong
            # inside AppHdr and appear there as stray elements at envlp level
            # only due to copy-paste errors. Inserting them again would create
            # duplicates; _normalize_busmsgenvlp already removes the strays.
            if (parent_local == "BusMsgEnvlp"
                    and tag in self._APPHDR_ONLY_TAGS):
                continue
            wanted.append(tag)
        # If the offending element is itself a near-miss of something we'd insert
        # (e.g. <BIC> vs <BICFI>), it's a MISNAMED element, not a missing one.
        # Inserting here would leave the stray misspelling AND a fresh duplicate;
        # decline that target so the sequence-fix renames it in place instead.
        #
        # GUARD: only when found_elem is NOT itself a valid XSD child of the
        # parent. Otherwise legitimate distinct elements get mis-paired — e.g.
        # "DbtrAgtAcct".startswith("DbtrAgt"), so deleting the mandatory DbtrAgt
        # (which surfaces as "DbtrAgtAcct not expected") was misread as a typo of
        # DbtrAgtAcct and the real missing predecessor was never reinserted.
        _found_is_valid_child = bool(
            tmap is not None and _parent_type
            and tmap.get_child_type(_parent_type, found_elem)
        )
        # Skip misnaming check when candidates came from a targeted "instead of
        # 'X': 'Y'" extraction — in that case the found element IS a valid, correctly
        # named element that simply has a missing mandatory predecessor. The shared-
        # prefix heuristic would incorrectly discard the missing sibling (e.g.
        # "PmtMtd" and "PmtInfId" both start with "Pmt").
        _skip_misname = getattr(self, "_targeted_insert_flag", False)
        if found_elem and wanted and not _found_is_valid_child and not _skip_misname:
            _misnamed_target = self._closest_expected(found_elem, wanted)
            if _misnamed_target:
                wanted = [t for t in wanted if t != _misnamed_target]
        if not wanted:
            # No buildable missing predecessor found — none of the "expected"
            # candidates have a template/XSD-buildable type (e.g. UltmtCdtr,
            # InstrForCdtrAgt, Purp, RgltryRptg are all XSD-optional fields we
            # have no data to populate). If `found_elem` ALSO isn't a valid
            # child of `parent` at all (a true orphan — e.g. a bare FinInstnId
            # stranded as the last child of CdtTrfTxInf, with no missing Agt
            # slot to wrap it into — every Instg/Instd/Dbtr/CdtrAgt already
            # populated), inserting nothing fixes nothing and the error
            # persists forever. The only deterministic repair left is to
            # delete the orphan outright — explicit_missing mode never reaches
            # here (it always has a concrete tag to build), so this only
            # triggers for implicit-mode genuine garbage.
            if (not explicit_missing and not _found_is_valid_child
                    and found_el is not None):
                _orphan_parent = found_el.getparent()
                if _orphan_parent is not None:
                    _op_orig = self._serialize(_orphan_parent)
                    _op_copy = self._copy(_orphan_parent)
                    _op_idx = list(_orphan_parent).index(found_el)
                    _op_copy.remove(list(_op_copy)[_op_idx])
                    _op_ser = self._serialize(_op_copy)
                    if _op_ser != _op_orig:
                        return FixSuggestion(self._xpath_of(_orphan_parent),
                                             _op_orig, _op_ser, code, msg, "low")
            return None
        # Sort into schema sequence: prefer the KB's tag_insertion_order, fall
        # back to the XSD-declared child order so XSD-discovered tags land in the
        # right slot even when the KB has no order list for this parent.
        _sort_order = order or _xsd_order_names
        if _sort_order:
            wanted.sort(key=lambda t: _sort_order.index(t) if t in _sort_order else 999)

        # 5. Build each missing element, namespaced to the PARENT (AppHdr lives
        #    in the head.001 namespace, not the Document body namespace).
        #    parent_ns/xsd_path/tmap/rules_idx/parent_path resolved above.
        parent_copy = self._copy(parent)
        base_cols, unit, close_cols = self._derive_child_indent(parent_copy)

        inserted_any = False
        for tag in wanted:
            child_el = self._build_child(
                tag, fix_hint, parent_ns, tmap,
                existing_parent=parent_copy, rules_idx=rules_idx,
                path_parts=parent_path + [tag], rule_id=code, root=root,
                msg_type=msg_type,
            )
            if child_el is None:
                continue
            # Indent the inserted subtree to match the document layout.
            if base_cols is not None:
                self._indent_el(child_el, base_cols, unit)
            insert_idx = self._sibling_insert_index(
                parent_copy, tag, _sort_order, found_elem
            )
            if insert_idx is None:
                parent_copy.append(child_el)
            else:
                parent_copy.insert(insert_idx, child_el)
            inserted_any = True

        if not inserted_any:
            return None

        # 6. Normalise the spacing between all direct children so the inserted
        #    elements sit on their own lines, aligned with their siblings.
        if base_cols is not None:
            self._normalize_child_tails(parent_copy, base_cols, close_cols)

        return FixSuggestion(
            xpath=self._xpath_of(parent),
            original_fragment=self._serialize(parent),
            fragment_xml=self._serialize(parent_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    # Agents that CBPR+ makes MANDATORY but the base ISO 20022 XSD leaves
    # OPTIONAL (minOccurs=0). Deleting one therefore raises NO schema error —
    # only a CBPR layer-3 rule (CBPR_R3 / L3-*-MANDATORY-PARTIES) whose reported
    # path points at the header, not the missing agent. The XSD-driven inserter
    # never sees them, so they need this dedicated route. Keyed by message family
    # → list of (transaction-block parent local-name, agent local-name).
    _CBPR_MANDATORY_AGENTS: dict[str, list] = {
        "pacs.008": [("CdtTrfTxInf", "InstgAgt"), ("CdtTrfTxInf", "InstdAgt")],
        "pacs.009": [("CdtTrfTxInf", "InstgAgt"), ("CdtTrfTxInf", "InstdAgt")],
        "pacs.010": [("CdtInstr", "InstgAgt"), ("CdtInstr", "InstdAgt")],
        "pain.001": [("PmtInf", "DbtrAgt"), ("CdtTrfTxInf", "CdtrAgt")],
        "pain.008": [("PmtInf", "CdtrAgt"), ("DrctDbtTxInf", "DbtrAgt")],
    }

    # Codes that signal a CBPR-mandatory agent/party is absent (path unreliable).
    _CBPR_MANDATORY_CODES = {
        "CBPR_R3", "L3-MANDATORY-PAYMENT-PARTIES", "L3-PAIN-MANDATORY-PARTIES",
        "PACS010_AGENTS_REQUIRED", "L3-PACS-MATCH-FR", "L3-PACS-MATCH-TO",
    }

    def _fix_missing_cbpr_mandatory(self, root: etree._Element, xml: str,
                                    code: str, msg: str,
                                    msg_type: str) -> Optional["FixSuggestion"]:
        """Insert a CBPR-mandatory-but-XSD-optional agent that was deleted.

        Schema validation can't catch these (the XSD makes them optional), so a
        deleted InstgAgt/InstdAgt/CdtrAgt only trips a CBPR business rule whose
        path points elsewhere. We find the first transaction block missing such
        an agent and insert it, sourcing the BICFI so cross-field rules hold:
          • InstgAgt BICFI := AppHdr/Fr BICFI   (CBPR_R3: Fr must equal InstgAgt)
          • InstdAgt BICFI := AppHdr/To BICFI   (CBPR_R3: To must equal InstdAgt)
          • Dbtr/CdtrAgt   := any existing same-agent BICFI, else dummy bank.
        Inserts ONE per call; the iterative loop revalidates and repeats.
        """
        _mtext = f"{code} {msg}".lower()
        if (code not in self._CBPR_MANDATORY_CODES
                and "must be provided" not in _mtext
                and "must match" not in _mtext):
            return None

        # Prefer the BUSINESS message type over the BAH (head.001) type, which
        # _detect_msg_type may return first (it leads the file). mandatory-agent
        # targets are keyed by the business family (pacs.008, pain.001, …).
        _biz = next((t for t in re.findall(r"([a-z]+\.\d{2,3}\.\d{3}\.\d{2})", xml)
                     if not t.startswith("head.")), "")
        fam = _kb_msg_family(_biz or msg_type)
        targets = self._CBPR_MANDATORY_AGENTS.get(fam, [])
        if not targets:
            return None

        ns = etree.QName(root.tag).namespace or ""
        xsd_path = self._get_xsd_path(xml)
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None

        def _bicfi_for(agent: str) -> str:
            v = None
            if agent == "InstgAgt":
                v = self._harvest_under(root, "Fr", "BICFI")
            elif agent == "InstdAgt":
                v = self._harvest_under(root, "To", "BICFI")
            if not v:
                # Any existing instance of this same agent elsewhere.
                v = self._harvest_under(root, agent, "BICFI")
            if not v:
                banks = _kb_get("dummy_data.banks", []) or []
                v = banks[0].get("bicfi", "DEUTDEFFXXX") if banks else "DEUTDEFFXXX"
            return v

        for parent_local, agent in targets:
            for parent in root.iter():
                if not isinstance(parent.tag, str):
                    continue
                if etree.QName(parent.tag).localname != parent_local:
                    continue
                if self._child_exists(parent, agent) is not None:
                    continue

                parent_ns = etree.QName(parent.tag).namespace or ns
                bicfi = _bicfi_for(agent)
                frag = (f"<{agent}><FinInstnId><BICFI>{bicfi}</BICFI>"
                        f"</FinInstnId></{agent}>")
                try:
                    agent_el = self._apply_ns(
                        etree.fromstring(frag.encode("utf-8")), parent_ns)
                except Exception:
                    continue

                parent_copy = self._copy(parent)
                base_cols, unit, close_cols = self._derive_child_indent(parent_copy)
                if base_cols is not None:
                    self._indent_el(agent_el, base_cols, unit)
                idx = self._find_insert_index(
                    parent_copy, agent, tmap,
                    parent_path=self._local_name_path(parent))
                if idx is None:
                    parent_copy.append(agent_el)
                else:
                    parent_copy.insert(idx, agent_el)
                if base_cols is not None:
                    self._normalize_child_tails(parent_copy, base_cols, close_cols)

                return FixSuggestion(
                    xpath=self._xpath_of(parent),
                    original_fragment=self._serialize(parent),
                    fragment_xml=self._serialize(parent_copy),
                    issue_code=code,
                    issue_message=msg,
                    confidence="high",
                )
        return None

    def _kb_mandatory_children(self, parent_local: str, msg_type: str) -> list[str]:
        """
        Return the DIRECT mandatory child tags of `parent_local` declared in the
        KB's cbpr_plus_rules.mandatory_fields for this message family.

        mandatory_fields lists dotted paths like "AppHdr.Fr" and
        "CdtTrfTxInf.PmtId.UETR"; we keep every path whose IMMEDIATE parent
        segment is `parent_local` (i.e. the parent's own direct children),
        regardless of how deep the path is. Matching only 2-segment paths missed
        nested parents like PmtId (whose entries are "CdtTrfTxInf.PmtId.X"), so a
        deleted <EndToEndId> could never be identified for re-insertion.
        """
        fam = _kb_msg_family(msg_type)
        # NB: index the family key directly — _kb_get can't walk it because the
        # key itself contains a dot ("pacs.008") and it splits on every dot.
        mf = _kb_get("cbpr_plus_rules.mandatory_fields", {}) or {}
        entries = mf.get(fam, []) if isinstance(mf, dict) else []
        out: list[str] = []
        for p in entries:
            if not isinstance(p, str):
                continue
            parts = [s for s in p.split(".") if s]
            if len(parts) >= 2 and parts[-2] == parent_local and parts[-1] not in out:
                out.append(parts[-1])
        return out

    def _sibling_insert_index(self, parent_copy: etree._Element, tag: str,
                              order: Optional[list], found_elem: str) -> Optional[int]:
        """
        Index at which to insert `tag` among parent_copy's children: the slot
        before the first existing child that follows it in the KB order; failing
        that, immediately before the element that tripped the error; else append.
        """
        if order and tag in order:
            new_pos = order.index(tag)
            for idx, c in enumerate(parent_copy):
                cl = etree.QName(c.tag).localname
                if cl in order and order.index(cl) > new_pos:
                    return idx
        for idx, c in enumerate(parent_copy):
            if etree.QName(c.tag).localname == found_elem:
                return idx
        return None

    # ── Whitespace / indentation helpers ──────────────────────────────────────

    def _derive_child_indent(self, parent: etree._Element):
        """
        Inspect an existing pretty-printed parent and return
        (child_indent, unit, close_indent) as whitespace strings:
          child_indent — spaces before each direct child (e.g. 8 spaces)
          unit         — one indentation step (e.g. 4 spaces)
          close_indent — spaces before the parent's closing tag (e.g. 4 spaces)
        Returns (None, None, None) when the parent isn't pretty-printed (single
        line), so the caller leaves the compact layout untouched.
        """
        kids = [k for k in parent if isinstance(k.tag, str)]
        if not kids:
            return (None, None, None)
        base = None
        if parent.text and "\n" in parent.text:
            base = parent.text.split("\n")[-1]
        if base is None:
            for k in kids[:-1]:
                if k.tail and "\n" in k.tail:
                    base = k.tail.split("\n")[-1]
                    break
        if base is None:
            return (None, None, None)
        close = None
        if kids[-1].tail and "\n" in kids[-1].tail:
            close = kids[-1].tail.split("\n")[-1]
        if close is None:
            close = base[:-4] if len(base) >= 4 else ""
        unit = base[len(close):] if (base.startswith(close) and len(base) > len(close)) else "    "
        return (base, unit, close)

    def _indent_el(self, el: etree._Element, indent_cols: str, unit: str) -> None:
        """
        Recursively pretty-print a freshly built (whitespace-free) element so its
        children sit at indent_cols + unit, one per line. `indent_cols` is the
        column at which `el` itself sits.
        """
        kids = [k for k in el if isinstance(k.tag, str)]
        if not kids:
            return
        child_cols = indent_cols + unit
        el.text = "\n" + child_cols
        for i, k in enumerate(kids):
            self._indent_el(k, child_cols, unit)
            k.tail = "\n" + (child_cols if i < len(kids) - 1 else indent_cols)

    def _normalize_child_tails(self, parent: etree._Element,
                               base_cols: str, close_cols: str) -> None:
        """
        Make every direct child of `parent` sit on its own line: non-last
        children get `base_cols` of indentation after them, the last child
        dedents to `close_cols` before the parent's closing tag. Only the
        inter-child whitespace is touched; each child's inner content is left
        as-is.
        """
        kids = [k for k in parent if isinstance(k.tag, str)]
        if not kids:
            return
        # Always overwrite the leading whitespace so blank lines left behind by a
        # manual deletion (e.g. the user removed Fr/To, leaving empty lines) are
        # collapsed to a single newline + indent — no extra space above the tags.
        parent.text = "\n" + base_cols
        for i, k in enumerate(kids):
            k.tail = "\n" + (base_cols if i < len(kids) - 1 else close_cols)

    # ── XSD loading ───────────────────────────────────────────────────────────

    def _xsd_extracted_dir(self) -> Optional[str]:
        """Locate the directory holding extracted MX/head XSDs for SR2025.

        Tries known layouts in order and returns the first that exists:
          1. repo-root  xsds/extracted        (packaged/deploy layout)
          2. app/sr2025/xsds/extracted        (in-repo source layout)
        Returns None when neither is present (schema-aware fixing then disabled).

        SR2026-only: NOT this directory — see _sr2026_xsd_dir/_get_xsd_path,
        which route to app/sr2026/xsds (a different naming convention
        entirely — long descriptive filenames, not '<msgtype>.xsd') whenever
        the active release is SR2026. Calling this directly for an SR2026
        message silently resolves the WRONG (SR2025) schema version — e.g.
        pacs.002.001.10 (SR2026) was resolving to pacs.002.001.15.xsd
        (SR2025), a different transaction-type shape, breaking every
        schema-aware insert/build for that message.
        """
        here = os.path.dirname(__file__)
        for rel in (("..", "..", "xsds", "extracted"),
                    ("..", "sr2025", "xsds", "extracted")):
            cand = os.path.normpath(os.path.join(here, *rel))
            if os.path.isdir(cand):
                return cand
        return None

    def _sr2026_xsd_dir(self) -> Optional[str]:
        """SR2026's XSD directory (app/sr2026/xsds) — flat, long descriptive
        filenames (e.g. 'CBPRPlus_SR2026_(Combined)_CBPRPlus-pacs_002_001_10_
        FIToFIPaymentStatusReport_....xsd'), NOT '<msgtype>.xsd' like SR2025."""
        here = os.path.dirname(__file__)
        cand = os.path.normpath(os.path.join(here, "..", "sr2026", "xsds"))
        return cand if os.path.isdir(cand) else None

    def _sr2026_resolve_xsd_filename(self, msg_type: str, xsd_dir: str) -> Optional[str]:
        """Find the SR2026 XSD file matching `msg_type` by substring search —
        mirrors app/sr2026/validation/validators/layer2.py's
        Layer2Validator._resolve_xsd_path (duplicated narrowly here rather than
        imported, to avoid a fix_suggester <-> sr2026.validation circular
        import). Keep the two in sync if SR2026 adds new variant-disambiguation
        rules (ADV/COV/STP/etc.)."""
        try:
            files = [f for f in os.listdir(xsd_dir) if f.endswith(".xsd")]
        except Exception:
            return None
        mt = (msg_type or "").strip().lower()
        if "pacs.008" in mt:
            term = "pacs_008_001_08_stp" if "stp" in mt else "pacs_008_001_08_fito"
        elif "pacs.009" in mt:
            term = ("pacs_009_001_08_cov" if "cov" in mt
                    else "pacs_009_001_08_adv" if "adv" in mt
                    else "pacs_009_001_08_financial")
        else:
            parts = mt.split(".")
            term = "_".join(parts[:2]) if len(parts) >= 2 else mt
        for f in files:
            if term and term in f.lower():
                return os.path.join(xsd_dir, f)
        # Fallback: bare family prefix (e.g. "pacs_002")
        parts = mt.split(".")
        fam = "_".join(parts[:2]) if len(parts) >= 2 else mt
        for f in files:
            if fam and fam in f.lower():
                return os.path.join(xsd_dir, f)
        return None

    def _get_xsd_path(self, xml: str) -> Optional[str]:
        """Resolve the Document message XSD for any MX type, version-blind.

        Messages frequently declare an older version than the XSD library ships
        (e.g. pacs.003.001.08 vs pacs.003.001.11.xsd). We resolve the message
        type robustly, try an exact file, then fall back to the highest
        available <family>.<msg>.<variant>.* version. This unlocks schema-aware
        fixing for every message type with an XSD on disk (acmt, admi, camt,
        pacs, pain, sese, reda, …).
        """
        msg_type = _detect_msg_type(xml)
        if not msg_type:
            return None
        # SR2026 ships a SEPARATE XSD set (app/sr2026/xsds) with a different
        # filename convention (long descriptive names, not '<msgtype>.xsd').
        # Resolving against SR2025's directory for an SR2026 message silently
        # picks the wrong schema VERSION (e.g. pacs.002.001.10 → .15) — see
        # _xsd_extracted_dir's docstring.
        _active_v = getattr(self, "_sr_version", None) or _ACTIVE_SR_VERSION
        if _active_v == "SR2026":
            _sr26_dir = self._sr2026_xsd_dir()
            if _sr26_dir:
                _sr26_path = self._sr2026_resolve_xsd_filename(msg_type, _sr26_dir)
                if _sr26_path:
                    return _sr26_path
            # No SR2026 match (e.g. message type not covered by the SR2026 set)
            # — fall through to SR2025 below rather than returning None, so
            # schema-aware fixing still degrades gracefully instead of fully
            # disabling for that message type.
        xsd_dir = self._xsd_extracted_dir()
        if not xsd_dir:
            return None
        exact = os.path.join(xsd_dir, f"{msg_type}.xsd")
        if os.path.exists(exact):
            return exact
        try:
            parts = msg_type.split(".")
            # Match on family.msg.variant (e.g. 'pacs.003.001'); pick newest version.
            prefix = ".".join(parts[:3]) if len(parts) >= 3 else msg_type
            cands = [f for f in os.listdir(xsd_dir)
                     if f.startswith(prefix + ".") and f.endswith(".xsd")]
            if cands:
                return os.path.join(xsd_dir, sorted(cands, reverse=True)[0])
        except Exception:
            pass
        return None

    def _get_apphdr_xsd_path(self, xml: str) -> Optional[str]:
        """Resolve the Business Application Header (head.001) XSD for AppHdr/*
        fixes. Prefers the exact version declared by the AppHdr namespace, else
        the newest head.001.001.* on disk.

        SR2026 ships its own head.001.001.02.xsd (app/sr2026/xsds — same simple
        naming as SR2025's, unlike the Document XSDs) with looser cardinality
        on some fields (e.g. BizSvc minOccurs="0" — see
        project_sr2026_bizsvc_missing_check memory); prefer it when active."""
        m = re.search(r"head\.001\.001\.\d{2}", xml)
        _active_v = getattr(self, "_sr_version", None) or _ACTIVE_SR_VERSION
        if _active_v == "SR2026":
            _sr26_dir = self._sr2026_xsd_dir()
            if _sr26_dir:
                if m:
                    _exact26 = os.path.join(_sr26_dir, f"{m.group(0)}.xsd")
                    if os.path.exists(_exact26):
                        return _exact26
                try:
                    _cands26 = [f for f in os.listdir(_sr26_dir)
                                if f.startswith("head.001.001.") and f.endswith(".xsd")]
                    if _cands26:
                        return os.path.join(_sr26_dir, sorted(_cands26, reverse=True)[0])
                except Exception:
                    pass
        xsd_dir = self._xsd_extracted_dir()
        if not xsd_dir:
            return None
        if m:
            exact = os.path.join(xsd_dir, f"{m.group(0)}.xsd")
            if os.path.exists(exact):
                return exact
        try:
            cands = [f for f in os.listdir(xsd_dir)
                     if f.startswith("head.001.001.") and f.endswith(".xsd")]
            if cands:
                return os.path.join(xsd_dir, sorted(cands, reverse=True)[0])
        except Exception:
            pass
        return None

    # ── Smart value extraction from rules data ────────────────────────────────

    def _extract_value_from_hint(self, tag_name: str, fix_hint: str, parent_tag: Optional[str] = None) -> Optional[str]:
        """
        Extract a concrete value from the fix_hint using the rules and codelists.
        Returns a plain string value (not XML), or None if not found.
        """
        if not fix_hint:
            return None
        tag_l = tag_name.lower()
        parent_l = parent_tag.lower() if parent_tag else ""

        # 1. Explicit quoted value in hint like 'SLEV' or "INGA"
        val_m = re.search(r"['\"]([A-Z0-9]{2,11})['\"]", fix_hint)
        if val_m:
            candidate = val_m.group(1)
            # Validate against known codelists
            for cl_name in ("charge_bearer", "service_level", "local_instrument",
                             "status_code", "purpose_code", "return_reason",
                             "cancellation_reason", "ctgyPurp", "purp", "entry_status"):
                codes = _codelist_codes(cl_name)
                if codes and candidate in codes:
                    return candidate
            # Accept it anyway if it looks like an ISO code (2–4 uppercase)
            if re.match(r'^[A-Z]{2,4}$', candidate):
                return candidate

        # 2. Tag-specific codelist lookups
        if tag_l in ("chrgbr", "chargebearer"):
            codes = [c for c in _codelist_codes("charge_bearer") if c != "SLEV"]
            # SLEV is disallowed by policy — never suggest it; prefer SHAR.
            for preferred in ("SHAR", "CRED", "DEBT"):
                if preferred in codes:
                    return preferred
            return codes[0] if codes else "SHAR"

        if tag_l in ("svccd", "cd") and "svcl" in fix_hint.lower():
            codes = _codelist_codes("service_level")
            # Avoid SEPA as a blind default — it is only valid for EUR payments
            # (CBPR_COV_R32). Prefer currency-agnostic service levels so repairing
            # an invalid code never introduces a SEPA/non-EUR violation.
            for preferred in ("SDVA", "NURG", "G001", "URGP"):
                if preferred in codes:
                    return preferred
            return next((c for c in codes if c != "SEPA"),
                        codes[0] if codes else "SDVA")

        if tag_l == "cd" and "lcl" in fix_hint.lower():
            codes = _codelist_codes("local_instrument")
            return codes[0] if codes else "CORE"

        if tag_l in ("txsts", "grpsts"):
            codes = _codelist_codes("status_code")
            return codes[0] if codes else "ACCP"

        if tag_l == "cd" and (parent_l == "sts" or "status" in fix_hint.lower() or "sts" in fix_hint.lower() or "entrystatus" in fix_hint.lower()):
            codes = _codelist_codes("entry_status")
            for preferred in ("BOOK", "PDNG", "INFO", "FUTR"):
                if preferred in codes:
                    return preferred
            return codes[0] if codes else "BOOK"

        if tag_l in ("rsn", "cd") and ("reason" in fix_hint.lower() or "rjct" in fix_hint.lower()):
            codes = _codelist_codes("return_reason")
            return codes[0] if codes else "AC01"

        if tag_l in ("ctry", "country"):
            # Extract country code from hint
            ctry_m = re.search(r'\b([A-Z]{2})\b', fix_hint)
            if ctry_m:
                candidate = ctry_m.group(1)
                country_codes = _codelist_codes("country")
                if candidate in country_codes:
                    return candidate
            return "US"

        # 3. UUID hint for UETR
        if tag_l == "uetr":
            uuid_m = re.search(
                r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                fix_hint, re.I
            )
            return uuid_m.group(1) if uuid_m else str(uuid.uuid4())

        return None

    # ── Build child element ───────────────────────────────────────────────────

    def _build_child(self, tag_name: str, fix_hint: str, ns: str,
                     tmap: Optional[_XsdTypeMap],
                     existing_parent: Optional[etree._Element] = None,
                     rules_idx: Optional["_RulesIndex"] = None,
                     path_parts: Optional[list[str]] = None,
                     rule_id: str = "",
                     root: Optional[etree._Element] = None,
                     msg_type: str = "") -> Optional[etree._Element]:
        """
        Build a child element to add, in priority order:
          1. AI Knowledge Base — message-specific tag_template
          2. Per-message rule's `fix` field (extracted XML for this exact tag)
          3. Extract XML from the issue's fix_hint
          4. Known _TEMPLATES dict
          5. XSD type map recursive build
          6. Minimal leaf with sensible default

        msg_type:    e.g. 'pacs.008.001.08'. Used to pick the message-specific
                     template from ai_knowledge_base.json.
        rules_idx:   the loaded _RulesIndex for this message.
        root:        the full XML document; used to harvest existing values
                     (so a new BICFI reuses an existing one when applicable).
        existing_parent: parent into which the new child will be inserted; used
                     to avoid creating duplicate children that already exist there.
        """
        # ── 0. BizSvc — variant-aware CBPR+ value. The generic KB template is
        #    'swift.cbprplus.02', which is wrong for pacs.009 (needs .03 / .adv.03
        #    / .cov.03) and would fail the CBPR_P9_R6 enum rule. Resolve the exact
        #    value from the message family + variant before anything else. ───────
        if tag_name == "BizSvc":
            _bv = _cbpr_bizsvc_value(
                msg_type or (_detect_msg_type(self._serialize(root)) if root is not None else ""),
                self._serialize(root) if root is not None else "",
            )
            if _bv:
                tag = f"{{{ns}}}{tag_name}" if ns else tag_name
                _el = etree.Element(tag)
                _el.text = _bv
                return _el

        # ── 0.5. Account Id — AccountIdentification4Choice requires exactly one
        #    of <IBAN>/<Othr>; it is NEVER a plain-text leaf. _TEMPLATES["Id"]
        #    ('<Id>ID-2025-001</Id>') and the bare XSD-type-map build (step 5)
        #    both produce an empty/invalid <Id/> for this case because "Id" is
        #    too ambiguous a tag name to route generically. Detect by parent
        #    (*Acct) the same way the EMPTY_ACCOUNT_CONTAINER handler does. ───
        if tag_name == "Id" and existing_parent is not None and isinstance(existing_parent.tag, str) \
                and etree.QName(existing_parent.tag).localname.endswith("Acct"):
            tag = f"{{{ns}}}{tag_name}" if ns else tag_name
            _el = etree.Element(tag)
            _iban_tag = f"{{{ns}}}IBAN" if ns else "IBAN"
            _iban_el = etree.SubElement(_el, _iban_tag)
            _iban_el.text = "GB29NWBK60161331926819"
            return _el

        # ── 1. AI Knowledge Base tag template (message-specific) ──────────────
        kb_tmpl = _kb_tag_template(tag_name, msg_type)
        if kb_tmpl:
            try:
                resolved = self._resolve_kb_placeholders(kb_tmpl, tag_name, root)
                kb_el = etree.fromstring(resolved.encode("utf-8"))
                kb_el = self._apply_ns(kb_el, ns)
                if existing_parent is not None and len(kb_el):
                    kb_el = self._prune_duplicate_children(kb_el, existing_parent)
                return kb_el
            except Exception as e:
                logger.debug(f"[_build_child] KB template parse failed for {tag_name}: {e}")

        # ── 2. Look up the message-specific rule for this tag/path ────────────
        if rules_idx:
            rule = rules_idx.lookup(rule_id=rule_id,
                                     path_parts=path_parts,
                                     leaf_tag=tag_name)
            if rule and rule.get("fix"):
                rule_fix = rule["fix"]
                raw = _extract_xml_from_fix(rule_fix, tag_name)
                if raw:
                    raw = self._resolve_placeholders(raw, tag_name, root)
                    try:
                        rule_el = etree.fromstring(raw.encode("utf-8"))
                        rule_el = self._apply_ns(rule_el, ns)
                        if existing_parent is not None and len(rule_el):
                            rule_el = self._prune_duplicate_children(
                                rule_el, existing_parent
                            )
                        return rule_el
                    except Exception as e:
                        logger.debug(f"[_build_child] rule fix parse failed for {tag_name}: {e}")

        # ── 2. Extract XML fragment from the issue's fix_hint ─────────────────
        if fix_hint:
            pattern = rf"(<{tag_name}[\s>].*?</{tag_name}>|<{tag_name}\s*/>)"
            m = re.search(pattern, fix_hint, re.DOTALL | re.IGNORECASE)
            if m:
                raw = m.group(1)
                raw = self._resolve_placeholders(raw, tag_name, root)
                try:
                    hint_el = etree.fromstring(raw.encode("utf-8"))
                    return self._apply_ns(hint_el, ns)
                except Exception:
                    pass

            # UUID hint for UETR
            if tag_name == "UETR":
                uuid_m = re.search(
                    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    fix_hint, re.I
                )
                tag = f"{{{ns}}}{tag_name}" if ns else tag_name
                el  = etree.Element(tag)
                el.text = uuid_m.group(1) if uuid_m else str(uuid.uuid4())
                return el

            # Try extracting a smart value from the hint
            parent_tag = etree.QName(existing_parent.tag).localname if existing_parent is not None and isinstance(existing_parent.tag, str) else None
            smart_val = self._extract_value_from_hint(tag_name, fix_hint, parent_tag)
            if smart_val is not None:
                tag = f"{{{ns}}}{tag_name}" if ns else tag_name
                el = etree.Element(tag)
                el.text = smart_val
                return el

        # ── 3. Known templates ────────────────────────────────────────────────
        tmpl = _TEMPLATES.get(tag_name)
        if tmpl:
            try:
                # Inject fresh UUID v4 for every UETR/OrgnlUETR before parsing
                tmpl = _inject_fresh_uetrs(tmpl)
                # Use existing-document values where possible
                tmpl = self._resolve_placeholders(tmpl, tag_name, root)
                el = etree.fromstring(tmpl.encode("utf-8"))
                el_out = self._apply_ns(el, ns)
                if existing_parent is not None and len(el_out):
                    el_out = self._prune_duplicate_children(el_out, existing_parent)
                return el_out
            except Exception:
                pass

        # 3. XSD type map — resolve the element's type via its full path. ISO
        #    XSDs nest local elements by parent type, so path resolution (not a
        #    name-only lookup) is required for the type map to apply at all.
        if tmap:
            type_name = None
            if path_parts:
                type_name = tmap.type_of_path(path_parts)
            if not type_name:
                type_name = tmap.element_type.get(tag_name, tag_name)
            el = _xsd_build(tag_name, type_name, tmap, ns)
            if el is not None and (el.text or len(el)):
                # Preserve existing amount values — never overwrite real amounts with 1000.00
                if root is not None:
                    for _bc in el.iter():
                        if not isinstance(_bc.tag, str):
                            continue
                        _bc_local = etree.QName(_bc.tag).localname
                        if ((_bc_local.endswith("Amt") or _bc_local == "Amt")
                                and (_bc.text or "").strip() in ("1000.00", "1000")):
                            _existing_a = self._harvest_value(root, _bc_local)
                            if _existing_a and re.match(r'^\d+(\.\d+)?$', _existing_a.strip()):
                                _bc.text = _existing_a.strip()
                if existing_parent is not None and len(el):
                    el = self._prune_duplicate_children(el, existing_parent)
                return el

        # 4. Minimal leaf — prefer the KB-folder authoritative value
        #    (datatype / expected_value / valid_values) so typed leaves like
        #    DtOfSgntr (ISODate) get a real value instead of 'SMPL-...'.
        #
        # GUARD: never assign text content to an element-only (complex) type.
        # Doing so emits e.g. <CdtrAcct>SMPL-...</CdtrAcct>, which the schema
        # rejects ("Character content ... not allowed because the content type
        # is element-only") — i.e. the fix would CREATE a new error. If we got
        # this far without a structural template/XSD build for a complex type,
        # decline (return None) so the caller skips it rather than corrupting
        # the document.
        if self._is_element_only(tag_name, path_parts, tmap):
            return None
        tag = f"{{{ns}}}{tag_name}" if ns else tag_name
        leaf = etree.Element(tag)
        kb_val = _kb_folder_leaf_value(tag_name, msg_type)
        leaf.text = kb_val if kb_val is not None else self._placeholder(tag_name)
        return leaf

    def _is_element_only(self, tag_name: str,
                         path_parts: Optional[list[str]],
                         tmap: Optional["_XsdTypeMap"]) -> bool:
        """
        True when `tag_name` is a complex (element-only) type that must NOT carry
        text content. Resolved from the XSD type map when available, with a
        conservative name-based fallback for well-known ISO 20022 containers.
        """
        if tmap:
            type_name = None
            if path_parts:
                try:
                    type_name = tmap.type_of_path(path_parts)
                except Exception:
                    type_name = None
            if not type_name:
                type_name = tmap.element_type.get(tag_name)
            info = tmap.type_info.get(type_name, {}) if type_name else {}
            kind = info.get("kind")
            if kind in ("sequence", "choice"):
                return True
            if kind in ("simple", "simpleContent"):
                return False
        # Fallback when the type can't be resolved: flag the clearest complex
        # containers by name (account / agent blocks and known structures).
        if tag_name.endswith("Acct") or tag_name.endswith("Agt"):
            return True
        # Only unambiguously element-only containers here. Tags like Id/Othr are
        # context-dependent (often simple leaves), so we rely on the XSD check
        # above for those and never name-flag them.
        return tag_name in {
            "PstlAdr", "FinInstnId", "FIId", "PmtId", "SttlmInf", "GrpHdr",
            "CdtTrfTxInf", "Dbtr", "Cdtr", "UltmtDbtr", "UltmtCdtr", "InitgPty",
            "RmtInf", "PmtTpInf", "ClrSysMmbId",
        }

    def _harvest_value(self, root: Optional[etree._Element], tag_name: str) -> Optional[str]:
        """
        Walk the existing XML document and return the first non-empty text value
        for a leaf element with this local-name. Used so generated fixes reuse
        values already present in the message (BICFI, MsgId, dates, currencies).
        """
        if root is None or not tag_name:
            return None
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if etree.QName(el.tag).localname == tag_name and el.text:
                txt = el.text.strip()
                if txt:
                    return txt
        return None

    def _resolve_kb_placeholders(self, raw_xml: str, tag_name: str,
                                  root: Optional[etree._Element]) -> str:
        """
        Resolve $PLACEHOLDER variables in a knowledge-base template.

        Resolution order per the KB's placeholder_resolution config:
          1. harvest_from_xml — find an existing instance in the live document
          2. dummy            — pull from ai_knowledge_base.dummy_data
          3. dummy_constant   — use the literal constant value

        Then resolve any literal `...` placeholders inside the template.
        """
        if not raw_xml:
            return raw_xml

        # Merge enterprise KB shared placeholder_resolution with legacy KB (legacy wins)
        resolution_map = {
            **(_enterprise_shared("placeholder_resolution", {}) or {}),
            **(_kb_get("placeholder_resolution", {}) or {}),
        }

        def _resolve_one(placeholder: str) -> str:
            cfg = resolution_map.get(placeholder, {})
            if not isinstance(cfg, dict):
                return placeholder

            # 1. Try harvesting from the live XML
            harvest_tag = cfg.get("harvest_from_xml")
            if harvest_tag and root is not None:
                # Support harvest patterns like "DbtrAgt//BICFI" → find BICFI under any DbtrAgt
                if "//" in harvest_tag:
                    container, leaf = harvest_tag.split("//", 1)
                    val = self._harvest_under(root, container, leaf)
                    if val:
                        return val
                elif "/" in harvest_tag:
                    parent, leaf = harvest_tag.split("/", 1)
                    val = self._harvest_under(root, parent, leaf)
                    if val:
                        return val
                elif harvest_tag.startswith("@"):
                    # Attribute harvest, e.g. "@Ccy"
                    val = self._harvest_attribute(root, harvest_tag[1:])
                    if val:
                        return val
                else:
                    val = self._harvest_value(root, harvest_tag)
                    if val:
                        return val

            # 2. Try dummy_data dotted path
            dummy_path = cfg.get("dummy")
            if dummy_path:
                val = _kb_get(f"dummy_data.{dummy_path}")
                if isinstance(val, str) and val:
                    return val

            # 3. Constant fallback
            const = cfg.get("dummy_constant")
            if const:
                return str(const)

            # 4. Last resort: empty so we don't break the XML
            return self._placeholder(tag_name)

        # Replace all $PLACEHOLDERS
        def _sub(m: re.Match) -> str:
            return _resolve_one(m.group(0))

        resolved = re.sub(r"\$[A-Z_][A-Z0-9_]*", _sub, raw_xml)
        # Handle stray `...` placeholders too (rules-style)
        resolved = self._resolve_placeholders(resolved, tag_name, root)
        
        # Dynamic Date Resolution
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        resolved = resolved.replace("USE_TODAY", now.strftime("%Y-%m-%d"))
        resolved = resolved.replace("USE_NOW_OFFSET", now.strftime("%Y-%m-%dT%H:%M:%S+00:00"))
        return resolved

    def _harvest_under(self, root: etree._Element, container_tag: str,
                        leaf_tag: str) -> Optional[str]:
        """Find an element with local-name container_tag and harvest leaf_tag's text from inside it."""
        for el in root.iter():
            if isinstance(el.tag, str) and etree.QName(el.tag).localname == container_tag:
                for desc in el.iter():
                    if (isinstance(desc.tag, str)
                        and etree.QName(desc.tag).localname == leaf_tag
                        and desc.text):
                        txt = desc.text.strip()
                        if txt:
                            return txt
        return None

    def _harvest_attribute(self, root: etree._Element, attr_name: str) -> Optional[str]:
        """Walk the document and return the first value of any element's named attribute."""
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            val = el.get(attr_name)
            if val:
                return val
        return None

    def _resolve_placeholders(self, raw_xml: str, tag_name: str,
                               root: Optional[etree._Element]) -> str:
        """
        Replace `...` placeholders in a rule's fix string with values harvested
        from the existing XML document, or sensible defaults for the tag.

        E.g. `<DbtrAcct><Id><IBAN>...</IBAN></Id></DbtrAcct>`
          → IBAN already in document is reused; otherwise default IBAN inserted.
        """
        if "..." not in raw_xml:
            return raw_xml

        # Find every inner-most `<Tag>...</Tag>` with `...` content and
        # substitute a real value (existing > placeholder default)
        def sub_inner(m: re.Match) -> str:
            inner_tag = m.group(1)
            harvested = self._harvest_value(root, inner_tag)
            value = harvested if harvested else self._placeholder(inner_tag)
            return f"<{inner_tag}>{value}</{inner_tag}>"

        # Repeat until no more `...` (handles multiple placeholders)
        prev = None
        cur = raw_xml
        while prev != cur:
            prev = cur
            cur = re.sub(r"<(\w+)>\.\.\.</\1>", sub_inner, cur)

        # Any remaining bare ... (no surrounding tag) → use the outer tag's placeholder
        if "..." in cur:
            cur = cur.replace("...", self._placeholder(tag_name))
        return cur

    def _prune_duplicate_children(self, new_el: etree._Element,
                                   existing_parent: etree._Element) -> etree._Element:
        """
        Remove sub-children from new_el whose local-name ALREADY EXISTS
        directly inside existing_parent (one level up, not inside new_el itself).

        This prevents tag duplication when a template includes a child that
        already exists in the parent. E.g. if PmtId already has InstrId,
        and we're adding UETR only, don't re-add InstrId.
        """
        existing_local_names = {
            etree.QName(c.tag).localname for c in existing_parent
            if isinstance(c.tag, str)
        }
        # Prune sub-children of new_el that already sit at parent level
        for child in list(new_el):
            if not isinstance(child.tag, str):
                continue
            child_local = etree.QName(child.tag).localname
            if child_local in existing_local_names:
                new_el.remove(child)
        return new_el

    def _placeholder(self, name: str) -> str:
        """Return a sensible default value for a leaf element."""
        n = name.lower()

        # Try codelist-based defaults first
        if n in ("chrgbr",):
            codes = _codelist_codes("charge_bearer")
            # SLEV is disallowed by policy — prefer SHAR, then other non-SLEV codes.
            for p in ("SHAR", "CRED", "DEBT"):
                if p in codes:
                    return p
            return "SHAR"
        if n in ("sttlmmtd",):
            return "INGA"
        if n in ("instrprty",):
            return "NORM"

        if "bicfi" in n or n == "bicfi":  return "DEUTDEFFXXX"
        if "bic"  in n:                   return "DEUTDEFFXXX"
        if "iban" in n:                   return "GB29NWBK60161331926819"
        if "uetr" in n:                   return str(uuid.uuid4())
        if "ctry" in n or "country" in n: return "US"
        if "ccy"  in n or "currency" in n: return "USD"
        if "dt"   == n or n.endswith("dt") or "date" in n: return "2025-01-15"
        if "dtm"  in n or "datetime" in n or "time" in n:  return "2025-01-15T10:00:00Z"
        if "amt"  in n or "amount"   in n: return "1000.00"
        if "nm"   == n or "name"     in n: return "Sample Name"
        if "id"   in n and len(name) <= 6: return "ID-2025-001"
        if "nb"   == n or n.startswith("nb"): return "1"
        if "lei"  in n:                   return "549300TRUWOII88U4F73"
        return f"SMPL-{name[:8]}"

    def _apply_ns(self, el: etree._Element, ns: str) -> etree._Element:
        """Recursively stamp namespace on all elements in a fragment."""
        if not ns:
            return el
        def stamp(e: etree._Element) -> None:
            if isinstance(e.tag, str) and "{" not in e.tag:
                e.tag = f"{{{ns}}}{e.tag}"
            for child in e:
                stamp(child)
        stamp(el)
        return el

    # ── suggest ───────────────────────────────────────────────────────────────

    def suggest(self, xml: str, issue: dict, version: str = None) -> FixSuggestion:
        # Active SWIFT release for this fix. Handlers whose fix differs by
        # release (bucket C) and SR2026-only delta-rule codes (bucket B) read
        # self._sr_version. Defaults to SR2025 so existing callers are unaffected.
        self._sr_version = (version or issue.get("sr_version")
                            or getattr(self, "_default_version", None) or "SR2025")
        # Make the active release visible to the module-level KB loaders so they
        # overlay the SR2026 delta KB for this request.
        _set_active_sr_version(self._sr_version)
        path      = str(issue.get("path", ""))
        code      = str(issue.get("code", ""))
        msg       = str(issue.get("message", ""))
        fix_hint  = str(issue.get("fix_suggestion", ""))
        line_hint = issue.get("line")
        try:
            line_hint = int(line_hint) if line_hint is not None else None
        except (TypeError, ValueError):
            line_hint = None
        self._line_hint = line_hint  # consumed by _recover_target_from_message
        # The validator's slash/dot path (when it isn't just a line number) —
        # consumed by _pick_candidate so candidate selection survives the
        # line-number drift introduced by suggest_batch's roll-forward applies.
        self._issue_path = path if not str(path).strip().isdigit() else ""

        # ── SWIFT / CBPR+ Character Set Repair ───────────────────────────────────
        # Handles three codes:
        #   SWIFT_FORBIDDEN_CHAR  — forbidden chars in inter-element / tail text
        #   PARTY_NAME_SWIFT_CHARSET — non-CBPR+ chars in a <Nm> leaf element
        #   SWIFT_CHARSET_WARN    — non-CBPR+ chars in <Ustrd> or similar leaf
        #
        # CBPR+ UHB permitted set: A-Z a-z 0-9 space / - ? : ( ) . , '
        # Everything else (@ # $ % ^ & * ! etc.) must be stripped.
        _is_swift_charset_code = code in (
            "SWIFT_FORBIDDEN_CHAR", "PARTY_NAME_SWIFT_CHARSET", "SWIFT_CHARSET_WARN",
            "INVALID_CHARSET", "INVALID_CHARSETS", "CBPR_COM_R1",
        )
        if _is_swift_charset_code:
            try:
                # CBPR+ restricted charset — strip anything not in the allowed set
                _CBPR_ALLOWED = re.compile(r"[^A-Za-z0-9 /\-?:().,']")

                def _strip_forbidden(text: str) -> str:
                    return _CBPR_ALLOWED.sub('', text)

                _parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _root = etree.fromstring(xml.encode("utf-8"), _parser)
                _changed = False

                # Free-text leaf tags where CBPR+ charset applies.
                # Structured fields (amounts, datetimes, BICs) have their own
                # charset rules and must NOT be stripped here (e.g. '+' in
                # datetime offsets would be stripped by _CBPR_ALLOWED).
                _LEAF_CHARSET_TAGS = {
                    "Nm", "Ustrd", "AddtlInf", "InfTp", "TwnNm", "StrtNm",
                    "BldgNm", "Dept", "SubDept", "Flr", "Room", "PstBx",
                    "AdrLine", "DstrctNm", "CtrySubDvsn", "TwnLctnNm", "ClrSysRef",
                    "BldgNb", "PstCd", "Purp", "Cd", "Prtry",
                    # ID fields where CBPR+ RestrictedFINXMax35Text applies
                    "MsgId", "BizMsgIdr", "EndToEndId", "InstrId", "TxId",
                    "UETR", "InstrNb", "ClrSysRef",
                }

                for _el in _root.iter():
                    if not isinstance(_el.tag, str):
                        continue
                    _local = etree.QName(_el.tag).localname
                    # Fix leaf element text for known CBPR+ free-text and ID tags
                    if len(_el) == 0 and _el.text and _local in _LEAF_CHARSET_TAGS:
                        _cleaned = _strip_forbidden(_el.text)
                        if _cleaned != _el.text:
                            # If ALL chars were stripped, use "Unknown" placeholder
                            # to avoid empty/invalid leaf element
                            _el.text = _cleaned if _cleaned.strip() else "Unknown"
                            _changed = True
                    # Fix inter-element text on container elements
                    if len(_el) > 0 and _el.text:
                        _cleaned = _strip_forbidden(_el.text)
                        if _cleaned != _el.text:
                            _el.text = _cleaned
                            _changed = True
                    # Always fix tail text
                    if _el.tail:
                        _cleaned = _strip_forbidden(_el.tail)
                        if _cleaned != _el.tail:
                            _el.tail = _cleaned
                            _changed = True

                if _changed:
                    _fixed_xml = etree.tostring(_root, encoding="unicode", pretty_print=False)
                    _decl_m = re.match(r'<\?xml[^?]*\?>\s*', xml)
                    if _decl_m:
                        _fixed_xml = _decl_m.group(0) + _fixed_xml
                    return FixSuggestion("/", xml, _fixed_xml, code, msg, "high")
            except Exception:
                pass

        # ── XML Syntax / reserved-character issues ────────────────────────────
        # SR2025 Layer 1 emits "XML Syntax Error" (with space), Layer 2 emits
        # "XML_SYNTAX". SR2026 Layer 1 emits "XML_SYNTAX_ERROR" plus distinct
        # codes for missing declaration, illegal control chars, wrong encoding.
        # All mean the document is malformed — route to recovery BEFORE attempting
        # to parse so that even a code-only signal gets the dedicated repair path.
        _is_syntax_code = code in ("XML_SYNTAX", "XML Syntax Error",
                                   "XML Markup Error", "Invalid Characters",
                                   "Missing Header",
                                   # SR2026 Layer 1 codes:
                                   "XML_SYNTAX_ERROR", "MISSING_XML_DECLARATION",
                                   "ILLEGAL_CONTROL_CHARACTERS", "INVALID_ENCODING",
                                   "INVALID_XML_STRUCTURE")
        # Only route to XML recovery when there are ACTUAL unescaped ampersands
        # (e.g. "Smith & Jones").  Valid XML entities like &gt; &amp; &lt; are
        # already correct and must NOT trigger recovery — doing so would return
        # the document unchanged and skip every schema-error handler below.
        _has_unescaped_amp = bool(
            re.search(r'&(?!(?:amp|lt|gt|apos|quot|#[0-9]+|#x[0-9a-fA-F]+);)', xml)
        )
        _has_missing_decl = (
            "missing" in msg.lower()
            and any(k in msg.lower() for k in ("declaration", "header", "xml"))
        )
        _msg_lower = msg.lower()
        if (_is_syntax_code or _has_unescaped_amp or _has_missing_decl
                or "reserved" in _msg_lower or "unclosed" in _msg_lower
                or "close the open tag" in _msg_lower
                or ("add </" in _msg_lower and "closing tag" in _msg_lower)):
            recovered = self._try_xml_recovery(xml, code, msg)
            if recovered is not None:
                return recovered
            # Fall through to normal parse + suggest for anything the recovery
            # couldn't handle — it may still be partially actionable.

        # Fix BusMsgEnvlp envelope structure: Document must be sibling of AppHdr.
        # If the envelope was wrong, return this as a whole-document fix and use
        # the corrected XML for all subsequent element-level fixes in this call.
        _envlp_fixed = self._normalize_busmsgenvlp(xml)
        if _envlp_fixed != xml:
            _envlp_original = xml
            xml = _envlp_fixed
            # Return whole-doc fix for schema/structure errors and any error
            # whose path or message involves a tag we just moved/removed at
            # the BusMsgEnvlp level (stray AppHdr-only tags).
            _path_tag = path.split("/")[-1].split("[")[0] if path else ""
            _is_envlp_stray = _path_tag in self._APPHDR_ONLY_TAGS
            if (_is_envlp_stray
                    or code in ("SCHEMA_VAL", "XML_SYNTAX", "STRUCTURE_ERROR",
                                "MISSING_STRUCTURE", "")
                    or "AppHdr" in msg or "Document" in msg):
                return FixSuggestion(
                    "/", _envlp_original, _envlp_fixed, code,
                    msg or "BusMsgEnvlp structure corrected: Document moved to sibling of AppHdr.",
                    "high",
                )

        try:
            root = self._parse_xml(xml)
        except FixApplyError:
            # XML cannot be parsed at all — recovery already ran above and
            # couldn't fix it. Never call the LLM with a truncated fragment
            # (partial output would overwrite the whole document at xpath="/");
            # instead send the FULL document for a markup-only repair, accepted
            # only if the answer parses and preserves the content.
            whole = self._llm_whole_doc_repair(xml, code, msg)
            if whole is not None:
                return whole
            return self._unavail(path, code, msg)

        # Message family for this document — lets _llm_fallback pull the full
        # per-message KB (enum allow-lists, dependency/formal rules, child
        # order) regardless of which handler routed to it.
        try:
            self._kb_family = _detect_family_from_tree(root)
        except Exception:
            self._kb_family = ""

        # ── ID_LENGTH_ERROR: early dedicated handler ──────────────────────────
        # Runs first so no other handler can intercept it. Handles the
        # Max16Text fields Assgnmt/Id and Case/Id (and any other Id with a
        # numeric max length embedded in the error message).
        if code == "ID_LENGTH_ERROR":
            _idlen_max_m = re.search(r"maximum\s+(?:allowed\s+)?(\d+)", msg, re.I)
            _idlen_max = int(_idlen_max_m.group(1)) if _idlen_max_m else 16
            # Determine the parent context from the error path (e.g. "//Assgnmt/Id")
            _idlen_pp = [p for p in path.replace("/", ".").split(".")
                         if p and _VALID_XML_NAME.match(p)]
            _idlen_tgt  = _idlen_pp[-1] if _idlen_pp else "Id"
            _idlen_par  = _idlen_pp[-2] if len(_idlen_pp) >= 2 else ""
            for _idel in root.iter():
                if not isinstance(_idel.tag, str):
                    continue
                _idel_ln = etree.QName(_idel.tag).localname
                if _idel_ln != _idlen_tgt:
                    continue
                _idel_val = (_idel.text or "").strip()
                if len(_idel_val) <= _idlen_max:
                    continue
                if _idlen_par:
                    _idel_par = _idel.getparent()
                    _idel_par_ln = (etree.QName(_idel_par.tag).localname
                                    if _idel_par is not None and isinstance(_idel_par.tag, str)
                                    else "")
                    if _idel_par_ln != _idlen_par:
                        continue
                _idel_copy = self._copy(_idel)
                _idel_copy.text = _idel_val[:_idlen_max]
                return FixSuggestion(
                    self._xpath_of(_idel), self._serialize(_idel),
                    self._serialize(_idel_copy), code, msg, "high"
                )

        # ── Per-release handler override surface ──────────────────────────────
        # The release-specific module (fix_handlers_sr2025 / sr2026) gets first
        # refusal: it can override or add to the shared deterministic chain below
        # without touching this engine. Returns a FixSuggestion or None.
        _vh = _version_handler(self._sr_version)
        if _vh is not None:
            try:
                _vsug = _vh(self, code, msg, root, fix_hint)
            except Exception as _ve:
                logger.warning(f"[FixSuggester] {self._sr_version} handler error: {_ve}")
                _vsug = None
            if _vsug is not None:
                return _vsug

        # ── Duplicate element → keep the first valid occurrence, remove extras ─
        # Runs before the ordering/insert guards so a duplicate (e.g. a second
        # <BICFI>, which the schema reports as "not expected here") is removed
        # rather than mis-repaired by inserting the other expected siblings.
        dup_fix = self._try_remove_duplicate(root, code, msg, xml)
        if dup_fix is not None:
            return dup_fix

        # ── pacs.002 CBPR+: TxInfAndSts missing OrgnlInstrId/OrgnlEndToEndId ──
        # MyStandards requires at least one original identifier before TxSts.
        # Insert <OrgnlEndToEndId> immediately before <TxSts>.
        if code == "PACS002_TXINF_NO_ORIG_ID":
            for _txinf in root.iter():
                if not isinstance(_txinf.tag, str):
                    continue
                if etree.QName(_txinf.tag).localname != "TxInfAndSts":
                    continue
                _child_locals = {etree.QName(c.tag).localname for c in _txinf
                                 if isinstance(c.tag, str)}
                if _child_locals & {"OrgnlInstrId", "OrgnlEndToEndId"}:
                    continue  # already has one — skip
                _txsts_child = next(
                    (c for c in _txinf if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "TxSts"),
                    None,
                )
                if _txsts_child is None:
                    continue
                _txinf_copy = self._copy(_txinf)
                # Find TxSts in copy and insert OrgnlEndToEndId before it
                _txsts_copy = next(
                    (c for c in _txinf_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "TxSts"),
                    None,
                )
                if _txsts_copy is not None:
                    _ns = etree.QName(_txinf.tag).namespace or ""
                    _e2e_tag = f"{{{_ns}}}OrgnlEndToEndId" if _ns else "OrgnlEndToEndId"
                    # Harvest an existing EndToEndId from the document as the value
                    _e2e_val = None
                    for _cand in root.iter():
                        if not isinstance(_cand.tag, str):
                            continue
                        if etree.QName(_cand.tag).localname in ("EndToEndId", "OrgnlEndToEndId"):
                            _v = (_cand.text or "").strip()
                            if _v:
                                _e2e_val = _v
                                break
                    if not _e2e_val:
                        import uuid as _uuid
                        _e2e_val = "E2E-" + _uuid.uuid4().hex[:12].upper()
                    _new_el = etree.Element(_e2e_tag)
                    _new_el.text = _e2e_val
                    # Insert at the correct sequence position: OrgnlEndToEndId must
                    # come before OrgnlTxId, OrgnlUETR, and TxSts (CBPR+ sequence).
                    # Find the first element that must follow OrgnlEndToEndId.
                    _AFTER_E2E = {"OrgnlTxId", "OrgnlUETR", "TxSts", "StsRsnInf",
                                  "InstgAgt", "InstdAgt", "OrgnlTxRef"}
                    _anchor = next(
                        (c for c in _txinf_copy if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname in _AFTER_E2E),
                        None,
                    )
                    if _anchor is not None:
                        _new_el.tail = _anchor.tail
                        _anchor.addprevious(_new_el)
                    else:
                        _txinf_copy.append(_new_el)
                    return FixSuggestion(
                        self._xpath_of(_txinf), self._serialize(_txinf),
                        self._serialize(_txinf_copy), code, msg, "high",
                    )

        # ── camt.055/056 TxInf: OrgnlEndToEndId + OrgnlUETR missing ────────────
        # CBPR+ SR2026 camt.055 XSD mandates OrgnlEndToEndId and OrgnlUETR before
        # OrgnlInstdAmt in TxInf. When they're absent, the XSD sequence validator
        # fires SCHEMA_VAL "OrgnlInstdAmt not allowed here / missed mandatory field".
        # Insert both missing elements immediately before OrgnlInstdAmt.
        if (code in ("SCHEMA_VAL", "SCHEMA_ERROR", "XSD_SEQUENCE")
                and "orgnlinstdamt" in msg.lower()
                and ("not allowed" in msg.lower() or "missed" in msg.lower()
                     or "unexpected" in msg.lower() or "expected" in msg.lower())):
            for _txinf_el in root.iter():
                if not isinstance(_txinf_el.tag, str):
                    continue
                if etree.QName(_txinf_el.tag).localname != "TxInf":
                    continue
                _ns_tx = etree.QName(_txinf_el.tag).namespace or ""
                def _q(tag): return f"{{{_ns_tx}}}{tag}" if _ns_tx else tag
                _child_lns = [etree.QName(c.tag).localname
                              for c in _txinf_el if isinstance(c.tag, str)]
                _has_e2e  = "OrgnlEndToEndId" in _child_lns
                _has_uetr = "OrgnlUETR"       in _child_lns
                if _has_e2e and _has_uetr:
                    continue  # already present — this TxInf is fine
                # Find OrgnlInstdAmt as the insertion anchor
                _amt_el = next(
                    (c for c in _txinf_el if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "OrgnlInstdAmt"),
                    None,
                )
                if _amt_el is None:
                    continue
                _txinf_copy = self._copy(_txinf_el)
                # Locate OrgnlInstdAmt in the copy
                _amt_copy = next(
                    (c for c in _txinf_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "OrgnlInstdAmt"),
                    None,
                )
                if _amt_copy is None:
                    continue
                _copy_child_lns = [etree.QName(c.tag).localname
                                   for c in _txinf_copy if isinstance(c.tag, str)]
                # Insert OrgnlUETR before OrgnlInstdAmt if missing
                if "OrgnlUETR" not in _copy_child_lns:
                    import uuid as _uuid2
                    _uetr_el = etree.Element(_q("OrgnlUETR"))
                    _uetr_el.text = str(_uuid2.uuid4())
                    _amt_copy.addprevious(_uetr_el)
                # Insert OrgnlEndToEndId before OrgnlUETR (or OrgnlInstdAmt) if missing
                if "OrgnlEndToEndId" not in _copy_child_lns:
                    # Harvest existing E2E value from document
                    _e2e_val = None
                    for _cand in root.iter():
                        if not isinstance(_cand.tag, str):
                            continue
                        if etree.QName(_cand.tag).localname in ("EndToEndId", "OrgnlEndToEndId"):
                            _v = (_cand.text or "").strip()
                            if _v:
                                _e2e_val = _v
                                break
                    if not _e2e_val:
                        import uuid as _uuid2
                        _e2e_val = "E2E-" + _uuid2.uuid4().hex[:12].upper()
                    _e2e_el = etree.Element(_q("OrgnlEndToEndId"))
                    _e2e_el.text = _e2e_val
                    # Insert before OrgnlUETR (now present) or OrgnlInstdAmt
                    _anchor_e2e = next(
                        (c for c in _txinf_copy if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname in ("OrgnlUETR", "OrgnlInstdAmt")),
                        None,
                    )
                    if _anchor_e2e is not None:
                        _anchor_e2e.addprevious(_e2e_el)
                    else:
                        _txinf_copy.append(_e2e_el)
                return FixSuggestion(
                    self._xpath_of(_txinf_el), self._serialize(_txinf_el),
                    self._serialize(_txinf_copy), code, msg, "high",
                )

        # ── camt.055/056 TxInf: stray <Rsn> at TxInf level + empty <CxlRsnInf> ──
        # AI sometimes places <Rsn> directly inside TxInf (wrong) with an adjacent
        # empty <CxlRsnInf/>. Fix: remove the stray <Rsn>, populate CxlRsnInf with
        # it. Also fires for SCHEMA_VAL "Rsn not allowed here".
        if (code in ("SCHEMA_VAL", "SCHEMA_ERROR", "XSD_SEQUENCE")
                and ("rsn" in msg.lower() or "cxlrsninf" in msg.lower())
                and ("not allowed" in msg.lower() or "missed" in msg.lower()
                     or "unexpected" in msg.lower())):
            for _txinf_el in root.iter():
                if not isinstance(_txinf_el.tag, str):
                    continue
                if etree.QName(_txinf_el.tag).localname != "TxInf":
                    continue
                _ns_tx = etree.QName(_txinf_el.tag).namespace or ""
                def _q2(tag): return f"{{{_ns_tx}}}{tag}" if _ns_tx else tag
                # Find stray <Rsn> that is a direct child of TxInf
                _stray_rsn = next(
                    (c for c in _txinf_el if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "Rsn"),
                    None,
                )
                if _stray_rsn is None:
                    continue
                # Find CxlRsnInf (may be empty)
                _cxlinf = next(
                    (c for c in _txinf_el if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "CxlRsnInf"),
                    None,
                )
                _txinf_copy = self._copy(_txinf_el)
                # Work on copy: find stray Rsn and CxlRsnInf
                _stray_copy = next(
                    (c for c in _txinf_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "Rsn"),
                    None,
                )
                _cxlinf_copy = next(
                    (c for c in _txinf_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "CxlRsnInf"),
                    None,
                )
                if _stray_copy is None:
                    continue
                # Remove stray Rsn from TxInf level
                _txinf_copy.remove(_stray_copy)
                _stray_copy.tail = None
                if _cxlinf_copy is not None:
                    # CxlRsnInf exists — populate it if empty
                    _existing_rsn = next(
                        (c for c in _cxlinf_copy if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname == "Rsn"),
                        None,
                    )
                    if _existing_rsn is None:
                        _cxlinf_copy.append(_stray_copy)
                else:
                    # No CxlRsnInf — build one with the Rsn inside
                    _new_cxlinf = etree.Element(_q2("CxlRsnInf"))
                    _new_cxlinf.append(_stray_copy)
                    _txinf_copy.append(_new_cxlinf)
                # Also insert missing mandatory sequence fields:
                # OrgnlEndToEndId + OrgnlUETR + OrgnlInstdAmt (required before CxlRsnInf)
                _copy_lns = [etree.QName(c.tag).localname
                             for c in _txinf_copy if isinstance(c.tag, str)]
                # Find CxlRsnInf in copy as insertion anchor
                _cxl_anchor = next(
                    (c for c in _txinf_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "CxlRsnInf"),
                    None,
                )
                import uuid as _uuid3
                if "OrgnlInstdAmt" not in _copy_lns:
                    _amt2 = etree.Element(_q2("OrgnlInstdAmt"))
                    _amt2.set("Ccy", "EUR")
                    _amt2.text = "1000.00"
                    if _cxl_anchor is not None:
                        _cxl_anchor.addprevious(_amt2)
                    else:
                        _txinf_copy.append(_amt2)
                    _copy_lns.insert(-1, "OrgnlInstdAmt")
                if "OrgnlUETR" not in _copy_lns:
                    _uetr2 = etree.Element(_q2("OrgnlUETR"))
                    _uetr2.text = str(_uuid3.uuid4())
                    _ins_before = next(
                        (c for c in _txinf_copy if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname in ("OrgnlInstdAmt", "CxlRsnInf")),
                        None,
                    )
                    if _ins_before is not None:
                        _ins_before.addprevious(_uetr2)
                    else:
                        _txinf_copy.append(_uetr2)
                if "OrgnlEndToEndId" not in _copy_lns:
                    _e2e2 = etree.Element(_q2("OrgnlEndToEndId"))
                    _e2e_val2 = next(
                        ((_c.text or "").strip() for _c in root.iter()
                         if isinstance(_c.tag, str)
                         and etree.QName(_c.tag).localname in ("EndToEndId", "OrgnlEndToEndId")
                         and (_c.text or "").strip()),
                        None,
                    ) or "E2E-" + _uuid3.uuid4().hex[:12].upper()
                    _e2e2.text = _e2e_val2
                    _ins_before2 = next(
                        (c for c in _txinf_copy if isinstance(c.tag, str)
                         and etree.QName(c.tag).localname in
                         ("OrgnlUETR", "OrgnlInstdAmt", "CxlRsnInf")),
                        None,
                    )
                    if _ins_before2 is not None:
                        _ins_before2.addprevious(_e2e2)
                    else:
                        _txinf_copy.append(_e2e2)
                return FixSuggestion(
                    self._xpath_of(_txinf_el), self._serialize(_txinf_el),
                    self._serialize(_txinf_copy), code, msg, "high",
                )

        # ── SCHEMA_VAL/INVALID_NAMESPACE_FORMAT: Document has wrong namespace ────
        # Fires when <Document xmlns="…head.001…"> or other wrong-ns is present but
        # the body contains a valid ISO 20022 message. Fix: re-namespace Document
        # using MsgDefIdr from AppHdr (or infer from body element name).
        _doc_ns_codes = ("SCHEMA_VAL", "INVALID_NAMESPACE_FORMAT", "SCHEMA_ERROR")
        if code in _doc_ns_codes and (
            "document" in path.lower()
            or "no matching global declaration" in msg.lower()
            or "namespace" in msg.lower()
            or code == "INVALID_NAMESPACE_FORMAT"
        ):
            _doc_el_ns = next(
                (e for e in root.iter()
                 if isinstance(e.tag, str) and etree.QName(e.tag).localname == "Document"),
                None,
            )
            if _doc_el_ns is not None:
                _doc_cur_ns = etree.QName(_doc_el_ns.tag).namespace or ""
                _ISO_PFX = "urn:iso:std:iso:20022:tech:xsd:"
                # Wrong if: not an ISO 20022 ns at all, OR is head.001 (AppHdr ns ≠ body ns)
                import re as _re_ns
                _doc_ns_match = _re_ns.match(
                    r"^urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$",
                    _doc_cur_ns,
                )
                _doc_ns_msgid = _doc_ns_match.group(1) if _doc_ns_match else None
                # head.001 is the AppHdr schema — never valid as a Document body ns
                _is_wrong_ns = (
                    _doc_ns_msgid is None
                    or _doc_ns_msgid.startswith("head.")
                )
                if _is_wrong_ns:
                    # Determine correct ns: 1) MsgDefIdr 2) body element name
                    _DOC_NS_MAP2 = {
                        "FIToFICstmrCdtTrf":     "pacs.008.001.08",
                        "FICdtTrf":               "pacs.009.001.08",
                        "FIToFICstmrDrctDbt":     "pacs.003.001.08",
                        "PmtRtr":                 "pacs.004.001.09",
                        "FIToFIPmtStsRpt":        "pacs.002.001.10",
                        "CstmrPmtCxlReq":         "camt.055.001.08",
                        "FIToFIPmtCxlReq":        "camt.056.001.08",
                        "BkToCstmrAcctRpt":       "camt.052.001.08",
                        "BkToCstmrStmt":          "camt.053.001.08",
                        "BkToCstmrDbtCdtNtfctn":  "camt.054.001.08",
                        "NtfctnToRcv":            "camt.057.001.06",
                        "RsltnOfInvstgtn":        "camt.029.001.09",
                        "CstmrCdtTrfInitn":       "pain.001.001.09",
                        "CstmrDrctDbtInitn":      "pain.008.001.09",
                        "CstmrPmtStsRpt":         "pain.002.001.09",
                        "FIToFIPmtStsRpt":        "pacs.002.001.10",
                        "FIToFIPmtCxlReq":        "camt.056.001.08",
                    }
                    _correct_msg_id = None
                    # 1. Body element name (most authoritative — MsgDefIdr may be wrong)
                    for _body_ch in _doc_el_ns:
                        if isinstance(_body_ch.tag, str):
                            _ln = etree.QName(_body_ch.tag).localname
                            if _ln in _DOC_NS_MAP2:
                                _correct_msg_id = _DOC_NS_MAP2[_ln]
                                break
                    # 2. MsgDefIdr — only if body lookup failed and MsgDefIdr is not head.*
                    if not _correct_msg_id:
                        _apphdr_ns_el = next(
                            (e for e in root.iter() if isinstance(e.tag, str)
                             and etree.QName(e.tag).localname == "AppHdr"), None,
                        )
                        if _apphdr_ns_el is not None:
                            _mdi_el = next(
                                (e for e in _apphdr_ns_el.iter() if isinstance(e.tag, str)
                                 and etree.QName(e.tag).localname == "MsgDefIdr"), None,
                            )
                            if _mdi_el is not None:
                                _mdi_val = (_mdi_el.text or "").strip()
                                if _mdi_val and not _mdi_val.startswith("head."):
                                    _correct_msg_id = _mdi_val
                    if _correct_msg_id:
                        _target_ns = (
                            _ISO_PFX + _correct_msg_id
                            if not _correct_msg_id.startswith("urn:")
                            else _correct_msg_id
                        )
                        # Re-namespace Document and all children that share the old ns
                        try:
                            _parser_dns = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                            _r_dns = etree.fromstring(xml.encode("utf-8"), _parser_dns)
                            _doc_dns = next(
                                (e for e in _r_dns.iter()
                                 if isinstance(e.tag, str)
                                 and etree.QName(e.tag).localname == "Document"),
                                None,
                            )
                            if _doc_dns is not None:
                                _old_doc_ns = etree.QName(_doc_dns.tag).namespace or ""
                                # Rebuild with default ns so lxml serializes xmlns="..." not ns1:
                                def _rens_defns2(el, old_ns, new_ns):
                                    q = etree.QName(el.tag)
                                    cur = q.namespace or ""
                                    tgt = new_ns if cur in (old_ns, "") else cur
                                    nsmap = {None: tgt} if tgt else {}
                                    ne = etree.Element(
                                        f"{{{tgt}}}{q.localname}" if tgt else q.localname,
                                        nsmap=nsmap,
                                    )
                                    ne.text = el.text
                                    ne.tail = el.tail
                                    for k, v in el.attrib.items():
                                        ne.set(k, v)
                                    for ch in el:
                                        if isinstance(ch.tag, str):
                                            ne.append(_rens_defns2(ch, old_ns, new_ns))
                                    return ne
                                _doc_rens2 = _rens_defns2(_doc_dns, _old_doc_ns, _target_ns)
                                _doc_parent2 = _doc_dns.getparent()
                                if _doc_parent2 is not None:
                                    _idx2 = list(_doc_parent2).index(_doc_dns)
                                    _doc_parent2.remove(_doc_dns)
                                    _doc_parent2.insert(_idx2, _doc_rens2)
                                _decl_dns = '<?xml version="1.0" encoding="UTF-8"?>\n'
                                _fixed_dns = etree.tostring(_r_dns, encoding="unicode", pretty_print=True)
                                if not _fixed_dns.startswith("<?"):
                                    _fixed_dns = _decl_dns + _fixed_dns
                                if _fixed_dns != xml:
                                    return FixSuggestion("/", xml, _fixed_dns, code, msg, "high")
                        except Exception:
                            pass

        # ── INVALID_MSG_DEF_IDR — fix MsgDefIdr to match Document body ──────────
        # Fires when MsgDefIdr says head.001.001.02 (or wrong msg type) but the
        # Document body contains e.g. FIToFICstmrCdtTrf → should be pacs.008.001.08.
        if code in ("INVALID_MSG_DEF_IDR", "HEAD001_MSGDEFIDR_MISMATCH", "INVALID_MSGDEFIDR"):
            _MDI_FIX_MAP = {
                "FIToFICstmrCdtTrf": "pacs.008.001.08",
                "FICdtTrf":           "pacs.009.001.08",
                "FIToFICstmrDrctDbt": "pacs.003.001.08",
                "PmtRtr":             "pacs.004.001.09",
                "FIToFIPmtStsRpt":    "pacs.002.001.10",
                "FIToFIPmtCxlReq":    "camt.056.001.08",
                "CstmrPmtCxlReq":     "camt.055.001.08",
                "BkToCstmrAcctRpt":   "camt.052.001.08",
                "BkToCstmrStmt":      "camt.053.001.08",
                "BkToCstmrDbtCdtNtfctn": "camt.054.001.08",
                "NtfctnToRcv":        "camt.057.001.06",
                "RsltnOfInvstgtn":    "camt.029.001.09",
                "CstmrCdtTrfInitn":   "pain.001.001.09",
                "CstmrDrctDbtInitn":  "pain.008.001.09",
                "CstmrPmtStsRpt":     "pain.002.001.09",
            }
            _mdi_apphdr = next((e for e in root.iter()
                                if isinstance(e.tag, str) and etree.QName(e.tag).localname == "AppHdr"), None)
            _mdi_el2 = next((e for e in root.iter()
                             if isinstance(e.tag, str) and etree.QName(e.tag).localname == "MsgDefIdr"), None)
            _doc_el2 = next((e for e in root.iter()
                             if isinstance(e.tag, str) and etree.QName(e.tag).localname == "Document"), None)
            if _mdi_el2 is not None and _doc_el2 is not None:
                _correct_mdi = None
                for _dch2 in _doc_el2:
                    if isinstance(_dch2.tag, str):
                        _dln2 = etree.QName(_dch2.tag).localname
                        if _dln2 in _MDI_FIX_MAP:
                            _correct_mdi = _MDI_FIX_MAP[_dln2]
                            break
                if _correct_mdi and (_mdi_el2.text or "").strip() != _correct_mdi:
                    try:
                        _parser_mdi = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                        _r_mdi = etree.fromstring(xml.encode("utf-8"), _parser_mdi)
                        _mdi_fix = next((e for e in _r_mdi.iter()
                                         if isinstance(e.tag, str) and etree.QName(e.tag).localname == "MsgDefIdr"), None)
                        if _mdi_fix is not None:
                            _mdi_fix.text = _correct_mdi
                            # Also fix Document namespace if it matches old MsgDefIdr (head.*)
                            _doc_fix = next((e for e in _r_mdi.iter()
                                             if isinstance(e.tag, str) and etree.QName(e.tag).localname == "Document"), None)
                            if _doc_fix is not None:
                                _doc_fix_ns = etree.QName(_doc_fix.tag).namespace or ""
                                import re as _re_mdi
                                _doc_fix_m = _re_mdi.match(
                                    r"^urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$",
                                    _doc_fix_ns,
                                )
                                _doc_fix_mid = _doc_fix_m.group(1) if _doc_fix_m else None
                                if _doc_fix_mid and _doc_fix_mid.startswith("head."):
                                    _target_mdi_ns = f"urn:iso:std:iso:20022:tech:xsd:{_correct_mdi}"
                                    def _rens_mdi(el, old_ns, new_ns):
                                        q = etree.QName(el.tag)
                                        cur = q.namespace or ""
                                        tgt = new_ns if cur in (old_ns, "") else cur
                                        nsmap = {None: tgt} if tgt else {}
                                        ne = etree.Element(
                                            f"{{{tgt}}}{q.localname}" if tgt else q.localname,
                                            nsmap=nsmap,
                                        )
                                        ne.text = el.text; ne.tail = el.tail
                                        for k, v in el.attrib.items(): ne.set(k, v)
                                        for ch in el:
                                            if isinstance(ch.tag, str):
                                                ne.append(_rens_mdi(ch, old_ns, new_ns))
                                        return ne
                                    _doc_rens_mdi = _rens_mdi(_doc_fix, _doc_fix_ns, _target_mdi_ns)
                                    _doc_par_mdi = _doc_fix.getparent()
                                    if _doc_par_mdi is not None:
                                        _idx_mdi = list(_doc_par_mdi).index(_doc_fix)
                                        _doc_par_mdi.remove(_doc_fix)
                                        _doc_par_mdi.insert(_idx_mdi, _doc_rens_mdi)
                            _decl_mdi = '<?xml version="1.0" encoding="UTF-8"?>\n'
                            _fixed_mdi = etree.tostring(_r_mdi, encoding="unicode", pretty_print=True)
                            if not _fixed_mdi.startswith("<?"):
                                _fixed_mdi = _decl_mdi + _fixed_mdi
                            return FixSuggestion("/", xml, _fixed_mdi, code, msg, "high")
                    except Exception:
                        pass

        # ── SCHEMA_VAL //FinInstnId (or stray) in CstmrPmtCxlReq — restructure camt.055 ──
        # CstmrPmtCxlReq only allows Assgnmt + Undrlyg.
        # Stray elements (FinInstnId, Agt, Cretr, …) must be stripped.
        # Orphaned Orig*/CxlRsnInf/TxInf/Case at this level must move into Undrlyg/OrgnlPmtInfAndCxl/TxInf.
        _CSTMR_ALLOWED = {"Assgnmt", "Undrlyg"}
        _TXINF_FIELDS = {
            "CxlId", "Case", "OrgnlInstrId", "OrgnlEndToEndId", "OrgnlUETR",
            "OrgnlInstdAmt", "OrgnlReqdExctnDt", "OrgnlReqdColltnDt", "CxlRsnInf",
        }
        _TXINF_ORDER = [
            "CxlId", "Case", "OrgnlInstrId", "OrgnlEndToEndId", "OrgnlUETR",
            "OrgnlInstdAmt", "OrgnlReqdExctnDt", "OrgnlReqdColltnDt", "CxlRsnInf",
        ]
        if code == "SCHEMA_VAL" and (
            "fininstnid" in path.lower() or "cstmrpmtcxlreq" in path.lower()
            or (
                "unexpected" in msg.lower()
                and any(k in msg.lower() for k in ("fininstnid", "agt", "cretr", "assgnmt"))
            )
        ):
            try:
                _parser_c55 = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r_c55 = etree.fromstring(xml.encode("utf-8"), _parser_c55)
                _cpr_c55 = next(
                    (e for e in _r_c55.iter()
                     if isinstance(e.tag, str) and etree.QName(e.tag).localname == "CstmrPmtCxlReq"),
                    None,
                )
                if _cpr_c55 is not None:
                    _cpr_ns = etree.QName(_cpr_c55.tag).namespace or ""
                    _children_c55 = list(_cpr_c55)
                    _child_lns = [etree.QName(c.tag).localname for c in _children_c55 if isinstance(c.tag, str)]
                    # Only act if there are stray children
                    _has_stray = any(ln not in _CSTMR_ALLOWED for ln in _child_lns)
                    if _has_stray:
                        def _mk_c55(tag, ns, text=None, children=None, attrib=None):
                            el = etree.Element(f"{{{ns}}}{tag}" if ns else tag, nsmap={None: ns} if ns else {})
                            if text:
                                el.text = text
                            if attrib:
                                for k, v in attrib.items():
                                    el.set(k, v)
                            if children:
                                for ch in children:
                                    el.append(ch)
                            return el

                        # Collect all children by localname
                        _by_ln = {}
                        for _c in _children_c55:
                            if isinstance(_c.tag, str):
                                _ln = etree.QName(_c.tag).localname
                                _by_ln.setdefault(_ln, []).append(_c)

                        # Existing Assgnmt and Undrlyg kept as-is
                        _assgnmt_el = _by_ln.get("Assgnmt", [None])[0]
                        _undrlyg_el = _by_ln.get("Undrlyg", [None])[0]

                        # Collect TxInf fields — from orphaned direct children + existing TxInf element
                        _txinf_existing = _by_ln.get("TxInf", [None])[0]
                        _txinf_fields_collected = {}  # localname → element

                        # From orphaned direct children
                        for _fln in _TXINF_FIELDS:
                            _orphans = _by_ln.get(_fln, [])
                            if _orphans:
                                _txinf_fields_collected[_fln] = _orphans[0]

                        # Absorb existing TxInf children if present
                        if _txinf_existing is not None:
                            for _tc in list(_txinf_existing):
                                if isinstance(_tc.tag, str):
                                    _tln = etree.QName(_tc.tag).localname
                                    if _tln not in _txinf_fields_collected:
                                        _txinf_fields_collected[_tln] = _tc

                        # Build new TxInf in XSD order
                        _txinf_children = []
                        for _slot in _TXINF_ORDER:
                            if _slot in _txinf_fields_collected:
                                _txinf_children.append(_txinf_fields_collected[_slot])

                        # Need at least OrgnlEndToEndId in TxInf
                        if not any(etree.QName(c.tag).localname == "OrgnlEndToEndId" for c in _txinf_children if isinstance(c.tag, str)):
                            _ee = _mk_c55("OrgnlEndToEndId", _cpr_ns, text="NOTPROVIDED")
                            _txinf_children.insert(0, _ee)
                        # Need OrgnlUETR
                        if not any(etree.QName(c.tag).localname == "OrgnlUETR" for c in _txinf_children if isinstance(c.tag, str)):
                            import uuid as _uuid_c55
                            _uu = _mk_c55("OrgnlUETR", _cpr_ns, text=str(_uuid_c55.uuid4()))
                            # Insert after OrgnlEndToEndId
                            _ee_idx = next((i for i,c in enumerate(_txinf_children) if isinstance(c.tag,str) and etree.QName(c.tag).localname=="OrgnlEndToEndId"), -1)
                            _txinf_children.insert(_ee_idx + 1, _uu)
                        # Need OrgnlInstdAmt
                        if not any(etree.QName(c.tag).localname == "OrgnlInstdAmt" for c in _txinf_children if isinstance(c.tag, str)):
                            _ia = _mk_c55("OrgnlInstdAmt", _cpr_ns, text="0", attrib={"Ccy": "USD"})
                            _txinf_children.append(_ia)
                        # Need CxlRsnInf
                        if not any(etree.QName(c.tag).localname == "CxlRsnInf" for c in _txinf_children if isinstance(c.tag, str)):
                            _rsn = _mk_c55("Cd", _cpr_ns, text="CUST")
                            _rsnw = _mk_c55("Rsn", _cpr_ns, children=[_rsn])
                            _cxl = _mk_c55("CxlRsnInf", _cpr_ns, children=[_rsnw])
                            _txinf_children.append(_cxl)

                        _new_txinf = _mk_c55("TxInf", _cpr_ns, children=_txinf_children)

                        # Build Undrlyg/OrgnlPmtInfAndCxl
                        if _undrlyg_el is not None:
                            # Try to reuse existing OrgnlPmtInfAndCxl inside Undrlyg
                            _orig_pmt = next(
                                (c for c in _undrlyg_el if isinstance(c.tag, str)
                                 and etree.QName(c.tag).localname == "OrgnlPmtInfAndCxl"),
                                None,
                            )
                        else:
                            _orig_pmt = _by_ln.get("OrgnlPmtInfAndCxl", [None])[0]

                        # Build OrgnlPmtInfAndCxl with mandatory children
                        _opmti_children = []
                        # OrgnlPmtInfId (mandatory)
                        if _orig_pmt is not None:
                            _existing_opi = next((c for c in _orig_pmt if isinstance(c.tag,str) and etree.QName(c.tag).localname=="OrgnlPmtInfId"), None)
                        else:
                            _existing_opi = None
                        _opmti_children.append(
                            _existing_opi if _existing_opi is not None
                            else _mk_c55("OrgnlPmtInfId", _cpr_ns, text="NOTPROVIDED")
                        )
                        # OrgnlGrpInf (mandatory): OrgnlMsgId + OrgnlMsgNmId
                        if _orig_pmt is not None:
                            _existing_ogi = next((c for c in _orig_pmt if isinstance(c.tag,str) and etree.QName(c.tag).localname=="OrgnlGrpInf"), None)
                        else:
                            _existing_ogi = None
                        if _existing_ogi is None:
                            _ogi_mid = _mk_c55("OrgnlMsgId", _cpr_ns, text="NOTPROVIDED")
                            _ogi_mnm = _mk_c55("OrgnlMsgNmId", _cpr_ns, text="pacs.008.001.08")
                            _existing_ogi = _mk_c55("OrgnlGrpInf", _cpr_ns, children=[_ogi_mid, _ogi_mnm])
                        _opmti_children.append(_existing_ogi)
                        # TxInf (mandatory)
                        _opmti_children.append(_new_txinf)

                        _new_orig_pmt = _mk_c55("OrgnlPmtInfAndCxl", _cpr_ns, children=_opmti_children)
                        _new_undrlyg = _mk_c55("Undrlyg", _cpr_ns, children=[_new_orig_pmt])

                        # Rebuild CstmrPmtCxlReq with only Assgnmt + Undrlyg
                        for _ch in list(_cpr_c55):
                            _cpr_c55.remove(_ch)
                        if _assgnmt_el is not None:
                            _cpr_c55.append(_assgnmt_el)
                        else:
                            # Assgnmt is mandatory — generate minimal stub
                            # Assgnr: Party40Choice__1 (Pty|Agt); Pty=PartyIdentification135__1 (only Id)
                            # Assgne: Party40Choice__2 (only Agt)
                            _assgnr_fid = _mk_c55("FinInstnId", _cpr_ns, children=[_mk_c55("BICFI", _cpr_ns, text="NOTPROVIDED")])
                            _assgnr = _mk_c55("Agt", _cpr_ns, children=[_assgnr_fid])
                            _assgne_fid = _mk_c55("FinInstnId", _cpr_ns, children=[_mk_c55("BICFI", _cpr_ns, text="NOTPROVIDED")])
                            _assgne = _mk_c55("Agt", _cpr_ns, children=[_assgne_fid])
                            _assgnmt_stub = _mk_c55("Assgnmt", _cpr_ns, children=[
                                _mk_c55("Id", _cpr_ns, text="ASSGNMT-001"),
                                _mk_c55("Assgnr", _cpr_ns, children=[_assgnr]),
                                _mk_c55("Assgne", _cpr_ns, children=[_assgne]),
                                _mk_c55("CreDtTm", _cpr_ns, text="2000-01-01T00:00:00+00:00"),
                            ])
                            _cpr_c55.append(_assgnmt_stub)
                        _cpr_c55.append(_new_undrlyg)

                        _decl_c55 = '<?xml version="1.0" encoding="UTF-8"?>\n'
                        _fixed_c55 = etree.tostring(_r_c55, encoding="unicode", pretty_print=True)
                        if not _fixed_c55.startswith("<?"):
                            _fixed_c55 = _decl_c55 + _fixed_c55
                        return FixSuggestion("/", xml, _fixed_c55, code, msg, "high")
            except Exception:
                pass

        # ── SCHEMA_VAL: stray element in camt.056 TxInf — strip + reorder ──────────
        # PaymentTransaction106__1 has a fixed sequence; AddtlInf and OrgnlInstdAmt are not in it.
        # Also handles out-of-order children by reordering to XSD sequence.
        _C056_TXINF_ORDER = [
            "CxlId", "Case", "OrgnlGrpInf", "OrgnlInstrId", "OrgnlEndToEndId",
            "OrgnlTxId", "OrgnlUETR", "OrgnlClrSysRef",
            "OrgnlIntrBkSttlmAmt", "OrgnlIntrBkSttlmDt", "CxlRsnInf",
        ]
        _C056_TXINF_ALLOWED = set(_C056_TXINF_ORDER)
        if code == "SCHEMA_VAL" and "unexpected" in msg.lower() and (
            path.lower() in ("//addtlinf",)
            or (
                any(k in path.lower() for k in ("addtlinf", "orgnlinstdamt"))
                and "txinf" in xml.lower()
            )
            or (
                "addtlinf" in msg.lower() or "orgnlinstdamt" in msg.lower()
            )
        ):
            try:
                _parser_c56 = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r_c56 = etree.fromstring(xml.encode("utf-8"), _parser_c56)
                # Find FIToFIPmtCxlReq TxInf elements
                _txinf_c56 = [
                    e for e in _r_c56.iter()
                    if isinstance(e.tag, str) and etree.QName(e.tag).localname == "TxInf"
                    and any(
                        isinstance(p.tag, str) and etree.QName(p.tag).localname in ("Undrlyg", "FIToFIPmtCxlReq")
                        for p in [e.getparent()] if p is not None
                    )
                ]
                _changed_c56 = False
                for _txi in _txinf_c56:
                    _txi_ns = etree.QName(_txi.tag).namespace or ""
                    _kids = list(_txi)
                    _lns = [(etree.QName(c.tag).localname if isinstance(c.tag, str) else None, c) for c in _kids]
                    # Check if any stray (unknown) children exist
                    _stray = [c for ln, c in _lns if ln is not None and ln not in _C056_TXINF_ALLOWED]
                    if _stray:
                        for _sc in _stray:
                            _txi.remove(_sc)
                        _changed_c56 = True
                    # Reorder remaining children by XSD sequence
                    _remaining = [(etree.QName(c.tag).localname if isinstance(c.tag, str) else None, c) for c in list(_txi)]
                    _known = {ln: c for ln, c in _remaining if ln in _C056_TXINF_ORDER}
                    _unknown_rest = [c for ln, c in _remaining if ln not in _C056_TXINF_ORDER and ln is not None]
                    _ordered = [_known[slot] for slot in _C056_TXINF_ORDER if slot in _known]
                    _ordered.extend(_unknown_rest)
                    _cur_order = [c for _, c in _remaining]
                    if _ordered != _cur_order:
                        for _c in list(_txi):
                            _txi.remove(_c)
                        for _c in _ordered:
                            _txi.append(_c)
                        _changed_c56 = True
                    # Insert mandatory OrgnlInstrId if missing
                    _child_lns_c56 = {etree.QName(c.tag).localname for c in list(_txi) if isinstance(c.tag, str)}
                    if "OrgnlInstrId" not in _child_lns_c56:
                        _ins_el = etree.Element(
                            f"{{{_txi_ns}}}OrgnlInstrId" if _txi_ns else "OrgnlInstrId",
                            nsmap={None: _txi_ns} if _txi_ns else {},
                        )
                        _ins_el.text = "NOTPROVIDED"
                        # Insert after OrgnlGrpInf
                        _ogi_idx = next((i for i, c in enumerate(list(_txi)) if isinstance(c.tag, str) and etree.QName(c.tag).localname == "OrgnlGrpInf"), -1)
                        _txi.insert(_ogi_idx + 1, _ins_el)
                        _changed_c56 = True
                if _changed_c56:
                    _decl_c56 = '<?xml version="1.0" encoding="UTF-8"?>\n'
                    _fixed_c56 = etree.tostring(_r_c56, encoding="unicode", pretty_print=True)
                    if not _fixed_c56.startswith("<?"):
                        _fixed_c56 = _decl_c56 + _fixed_c56
                    return FixSuggestion("/", xml, _fixed_c56, code, msg, "high")
            except Exception:
                pass

        # ── SCHEMA_VAL //Nm or //Pty: Assgnmt party type violations (camt.055) ──────
        # Assgnr (Party40Choice__1): Pty allowed but Pty/Nm is NOT (PartyIdentification135__1 has no Nm).
        # Assgne (Party40Choice__2): only Agt allowed, Pty is forbidden entirely.
        # Fix: strip stray Nm from Assgnr/Pty; replace Assgne/Pty with Agt stub.
        if code == "SCHEMA_VAL" and "unexpected" in msg.lower() and path.lower() in ("//nm", "//pty"):
            try:
                _parser_asgn = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r_asgn = etree.fromstring(xml.encode("utf-8"), _parser_asgn)
                _asgn_ns = None
                _doc_asgn = next((e for e in _r_asgn.iter() if isinstance(e.tag, str) and etree.QName(e.tag).localname == "Document"), None)
                if _doc_asgn is not None:
                    _asgn_ns = etree.QName(_doc_asgn.tag).namespace or ""
                _changed_asgn = False

                # Fix 1: Assgnr/Pty — PartyIdentification135__1 only allows Id (no Nm, no PstlAdr etc.)
                # If Pty contains only invalid fields (Nm/PstlAdr) and no Id → convert to Agt stub.
                # If Pty has an Id → only strip invalid sibling fields.
                _ASSGNR_PTY_VALID = {"Id"}  # PartyIdentification135__1 only allows Id
                for _assgnr_el in _r_asgn.iter():
                    if not isinstance(_assgnr_el.tag, str): continue
                    if etree.QName(_assgnr_el.tag).localname != "Assgnr": continue
                    _pty_el = next((c for c in _assgnr_el if isinstance(c.tag, str) and etree.QName(c.tag).localname == "Pty"), None)
                    if _pty_el is not None:
                        _pty_child_lns = [etree.QName(c.tag).localname for c in _pty_el if isinstance(c.tag, str)]
                        _has_valid = any(ln in _ASSGNR_PTY_VALID for ln in _pty_child_lns)
                        _ns_ar = _asgn_ns or (etree.QName(_assgnr_el.tag).namespace or "")
                        if not _has_valid:
                            # No valid content — replace Pty with Agt stub
                            _assgnr_el.remove(_pty_el)
                            _bicfi_ar = etree.Element(f"{{{_ns_ar}}}BICFI" if _ns_ar else "BICFI", nsmap={None: _ns_ar} if _ns_ar else {})
                            _bicfi_ar.text = "NOTPROVIDED"
                            _fid_ar = etree.Element(f"{{{_ns_ar}}}FinInstnId" if _ns_ar else "FinInstnId", nsmap={None: _ns_ar} if _ns_ar else {})
                            _fid_ar.append(_bicfi_ar)
                            _agt_ar = etree.Element(f"{{{_ns_ar}}}Agt" if _ns_ar else "Agt", nsmap={None: _ns_ar} if _ns_ar else {})
                            _agt_ar.append(_fid_ar)
                            _assgnr_el.append(_agt_ar)
                            _changed_asgn = True
                        else:
                            # Has Id — strip invalid siblings (Nm, PstlAdr, etc.)
                            for _inv_c in [c for c in list(_pty_el) if isinstance(c.tag, str) and etree.QName(c.tag).localname not in _ASSGNR_PTY_VALID]:
                                _pty_el.remove(_inv_c)
                                _changed_asgn = True

                # Fix 2: replace Assgne/Pty with Agt (Party40Choice__2 only allows Agt)
                for _assgne_el in _r_asgn.iter():
                    if not isinstance(_assgne_el.tag, str): continue
                    if etree.QName(_assgne_el.tag).localname != "Assgne": continue
                    _pty_assgne = next((c for c in _assgne_el if isinstance(c.tag, str) and etree.QName(c.tag).localname == "Pty"), None)
                    if _pty_assgne is not None:
                        _assgne_el.remove(_pty_assgne)
                        _ns_a = _asgn_ns or (etree.QName(_assgne_el.tag).namespace or "")
                        _bicfi_a = etree.Element(f"{{{_ns_a}}}BICFI" if _ns_a else "BICFI", nsmap={None: _ns_a} if _ns_a else {})
                        _bicfi_a.text = "NOTPROVIDED"
                        _fid_a = etree.Element(f"{{{_ns_a}}}FinInstnId" if _ns_a else "FinInstnId", nsmap={None: _ns_a} if _ns_a else {})
                        _fid_a.append(_bicfi_a)
                        _agt_a = etree.Element(f"{{{_ns_a}}}Agt" if _ns_a else "Agt", nsmap={None: _ns_a} if _ns_a else {})
                        _agt_a.append(_fid_a)
                        _assgne_el.append(_agt_a)
                        _changed_asgn = True

                if _changed_asgn:
                    _decl_asgn = '<?xml version="1.0" encoding="UTF-8"?>\n'
                    _fixed_asgn = etree.tostring(_r_asgn, encoding="unicode", pretty_print=True)
                    if not _fixed_asgn.startswith("<?"):
                        _fixed_asgn = _decl_asgn + _fixed_asgn
                    return FixSuggestion("/", xml, _fixed_asgn, code, msg, "high")
            except Exception:
                pass

        # ── SCHEMA_VAL //BICFI: stray BICFI outside FinInstnId ───────────────────
        # BranchAndFinancialInstitutionIdentification6 only allows FinInstnId.
        # A stray <BICFI> directly under InstgAgt/InstdAgt/DbtrAgt/etc. must be removed.
        if code == "SCHEMA_VAL" and "bicfi" in path.lower() and (
            "wrong place" in msg.lower() or "unexpected" in msg.lower() or "not allowed" in msg.lower()
        ):
            # Agent container tags that only allow FinInstnId as child
            _AGENT_CONTAINERS = {
                "InstgAgt", "InstdAgt", "DbtrAgt", "CdtrAgt",
                "IntrmyAgt1", "IntrmyAgt2", "IntrmyAgt3",
                "PrvsInstgAgt1", "PrvsInstgAgt2", "PrvsInstgAgt3",
            }
            _stray_bicfi_found = False
            for _ag_el in root.iter():
                if not isinstance(_ag_el.tag, str):
                    continue
                if etree.QName(_ag_el.tag).localname not in _AGENT_CONTAINERS:
                    continue
                _stray_bics = [c for c in _ag_el if isinstance(c.tag, str)
                                and etree.QName(c.tag).localname == "BICFI"]
                if _stray_bics:
                    _stray_bicfi_found = True
                    break
            if _stray_bicfi_found:
                try:
                    _parser_bic = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                    _r_bic = etree.fromstring(xml.encode("utf-8"), _parser_bic)
                    _removed_bic = 0
                    for _ag_r in _r_bic.iter():
                        if not isinstance(_ag_r.tag, str):
                            continue
                        if etree.QName(_ag_r.tag).localname not in _AGENT_CONTAINERS:
                            continue
                        for _bic_ch in list(_ag_r):
                            if isinstance(_bic_ch.tag, str) and etree.QName(_bic_ch.tag).localname == "BICFI":
                                _ag_r.remove(_bic_ch)
                                _removed_bic += 1
                    if _removed_bic:
                        _decl_bic = '<?xml version="1.0" encoding="UTF-8"?>\n'
                        _fixed_bic = etree.tostring(_r_bic, encoding="unicode", pretty_print=True)
                        if not _fixed_bic.startswith("<?"):
                            _fixed_bic = _decl_bic + _fixed_bic
                        return FixSuggestion("/", xml, _fixed_bic, code, msg, "high")
                except Exception:
                    pass

        # ── SCHEMA_VAL order errors in pacs.008 PmtId / CdtTrfTxInf ────────────
        # "Unexpected field EndToEndId/ChrgBr — missed mandatory field before it"
        # Cause 1: PmtId missing InstrId (mandatory in pacs.008 SR2026 before EndToEndId)
        # Cause 2: CdtTrfTxInf child elements out of XSD sequence order
        if code == "SCHEMA_VAL" and (
            "endtoendid" in path.lower() or "chrgbr" in path.lower()
            or (("endtoendid" in msg.lower() or "chrgbr" in msg.lower())
                and ("missed" in msg.lower() or "unexpected" in msg.lower()))
        ):
            # pacs.008 SR2026 CdtTrfTxInf canonical order
            _CDTTRF_ORDER = [
                "PmtId", "PmtTpInf", "IntrBkSttlmAmt", "IntrBkSttlmDt",
                "SttlmPrty", "SttlmTmIndctn", "SttlmTmReq",
                "InstdAmt", "XchgRate", "ChrgBr", "ChrgsInf",
                "PrvsInstgAgt1", "PrvsInstgAgt1Acct", "PrvsInstgAgt2", "PrvsInstgAgt2Acct",
                "PrvsInstgAgt3", "PrvsInstgAgt3Acct",
                "InstgAgt", "InstdAgt",
                "IntrmyAgt1", "IntrmyAgt1Acct", "IntrmyAgt2", "IntrmyAgt2Acct",
                "IntrmyAgt3", "IntrmyAgt3Acct",
                "UltmtDbtr", "InitgPty",
                "Dbtr", "DbtrAcct", "DbtrAgt", "DbtrAgtAcct",
                "CdtrAgt", "CdtrAgtAcct", "Cdtr", "CdtrAcct", "UltmtCdtr",
                "InstrForCdtrAgt", "InstrForNxtAgt",
                "Purp", "RgltryRptg", "Tax", "RltdRmtInf", "RmtInf",
                "SplmtryData",
            ]
            # pacs.008 SR2026 PmtId canonical order
            _PMTID_ORDER = ["InstrId", "EndToEndId", "TxId", "UETR", "ClrSysRef"]
            _cdttrf_el = next(
                (e for e in root.iter() if isinstance(e.tag, str)
                 and etree.QName(e.tag).localname == "CdtTrfTxInf"), None,
            )
            if _cdttrf_el is not None:
                try:
                    _parser_ord = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                    _r_ord = etree.fromstring(xml.encode("utf-8"), _parser_ord)
                    _cdttrf_ord = next(
                        (e for e in _r_ord.iter() if isinstance(e.tag, str)
                         and etree.QName(e.tag).localname == "CdtTrfTxInf"), None,
                    )
                    if _cdttrf_ord is not None:
                        _cns = etree.QName(_cdttrf_ord.tag).namespace or ""
                        def _cq(t): return f"{{{_cns}}}{t}" if _cns else t
                        _changed_ord = False
                        # Fix PmtId: insert InstrId if missing
                        _pmtid_ord = next(
                            (c for c in _cdttrf_ord if isinstance(c.tag, str)
                             and etree.QName(c.tag).localname == "PmtId"), None,
                        )
                        if _pmtid_ord is not None:
                            _pmtid_lns = [etree.QName(c.tag).localname for c in _pmtid_ord if isinstance(c.tag, str)]
                            if "InstrId" not in _pmtid_lns:
                                _instrid_el = etree.Element(_cq("InstrId"))
                                _instrid_el.text = "NOTPROVIDED"
                                _pmtid_ord.insert(0, _instrid_el)
                                _changed_ord = True
                            # Reorder PmtId children
                            _pmtid_kids = [(etree.QName(c.tag).localname, c) for c in list(_pmtid_ord) if isinstance(c.tag, str)]
                            _orig_pmtid_ord = [ln for ln, _ in _pmtid_kids]
                            _known_pmtid = {ln: c for ln, c in _pmtid_kids if ln in _PMTID_ORDER}
                            _unk_pmtid = [c for ln, c in _pmtid_kids if ln not in _PMTID_ORDER]
                            for c in list(_pmtid_ord): _pmtid_ord.remove(c)
                            for slot in _PMTID_ORDER:
                                if slot in _known_pmtid:
                                    _pmtid_ord.append(_known_pmtid[slot])
                            for c in _unk_pmtid: _pmtid_ord.append(c)
                            _new_pmtid_ord = [etree.QName(c.tag).localname for c in _pmtid_ord if isinstance(c.tag, str)]
                            if _new_pmtid_ord != _orig_pmtid_ord:
                                _changed_ord = True
                        # Reorder CdtTrfTxInf children
                        _cdt_kids = [(etree.QName(c.tag).localname, c) for c in list(_cdttrf_ord) if isinstance(c.tag, str)]
                        _orig_cdt_ord = [ln for ln, _ in _cdt_kids]
                        _known_cdt = {ln: c for ln, c in _cdt_kids if ln in _CDTTRF_ORDER}
                        _unk_cdt = [c for ln, c in _cdt_kids if ln not in _CDTTRF_ORDER]
                        for c in list(_cdttrf_ord): _cdttrf_ord.remove(c)
                        for slot in _CDTTRF_ORDER:
                            if slot in _known_cdt:
                                _cdttrf_ord.append(_known_cdt[slot])
                        for c in _unk_cdt: _cdttrf_ord.append(c)
                        _new_cdt_ord = [etree.QName(c.tag).localname for c in _cdttrf_ord if isinstance(c.tag, str)]
                        if _new_cdt_ord != _orig_cdt_ord:
                            _changed_ord = True
                        if _changed_ord:
                            _decl_ord = '<?xml version="1.0" encoding="UTF-8"?>\n'
                            _fixed_ord = etree.tostring(_r_ord, encoding="unicode", pretty_print=True)
                            if not _fixed_ord.startswith("<?"):
                                _fixed_ord = _decl_ord + _fixed_ord
                            return FixSuggestion("/", xml, _fixed_ord, code, msg, "high")
                except Exception:
                    pass

        # ── camt.055 TxInf: missing OrgnlReqdExctnDt / OrgnlReqdColltnDt (R8) ──
        # CBPR+ R8: exactly one must be present. Validator emits EXEC_COLL_DATE_MISSING.
        if code in ("EXEC_COLL_DATE_MISSING", "SCHEMA_VAL", "SCHEMA_ERROR", "CBPR_FORMAL_RULE") and (
            "orgnlreqdexctndt" in msg.lower()
            or "orgnlreqdcolltndt" in msg.lower()
            or "exec_coll_date_missing" in msg.lower()
            or ("exectndt" in msg.lower() or "colltndt" in msg.lower())
            or ("requestedexecutiondate" in msg.lower() or "requestedcollectiondate" in msg.lower())
            or code == "EXEC_COLL_DATE_MISSING"
        ):
            import uuid as _uuid_r8
            for _txinf_r8 in root.iter():
                if not isinstance(_txinf_r8.tag, str):
                    continue
                if etree.QName(_txinf_r8.tag).localname != "TxInf":
                    continue
                _ns_r8 = etree.QName(_txinf_r8.tag).namespace or ""
                def _q_r8(tag): return f"{{{_ns_r8}}}{tag}" if _ns_r8 else tag
                _lns_r8 = [etree.QName(c.tag).localname
                           for c in _txinf_r8 if isinstance(c.tag, str)]
                if "OrgnlReqdExctnDt" in _lns_r8 or "OrgnlReqdColltnDt" in _lns_r8:
                    continue  # already has one — not a missing-date error
                # Insert OrgnlReqdExctnDt before CxlRsnInf (or at end of TxInf)
                from datetime import date as _date_r8
                _txinf_r8_copy = self._copy(_txinf_r8)
                _today = _date_r8.today().isoformat()
                _exctn_el = etree.Element(_q_r8("OrgnlReqdExctnDt"))
                _dt_el = etree.SubElement(_exctn_el, _q_r8("Dt"))
                _dt_el.text = _today
                _cxlinf_anchor = next(
                    (c for c in _txinf_r8_copy if isinstance(c.tag, str)
                     and etree.QName(c.tag).localname == "CxlRsnInf"),
                    None,
                )
                if _cxlinf_anchor is not None:
                    _cxlinf_anchor.addprevious(_exctn_el)
                else:
                    _txinf_r8_copy.append(_exctn_el)
                return FixSuggestion(
                    self._xpath_of(_txinf_r8), self._serialize(_txinf_r8),
                    self._serialize(_txinf_r8_copy), code, msg, "high",
                )

        # ── Structural rescue: elements absorbed into account <Id> ──────────────
        # Handles large-deletion collapse where balance engine nests DrctDbtTxInf-
        # level elements (Dbtr, DbtrAcct, DbtrAgt, RmtInf…) inside CdtrAcct/Id.
        _rescued = self._try_rescue_collapsed_account_id(root, xml)
        if _rescued is not None:
            return _rescued

        # ── IBAN format/pattern error: replace with a valid IBAN ────────────────
        # L2 reports "Invalid IBAN format: 'XXXX'" when the value lacks the
        # 2-letter country prefix (e.g. '60161331926819' or '11112222333344').
        # _recover_target_from_message now finds <IBAN> elements (IBAN removed
        # from skipwords), but _regenerate_value's same-tag harvest can pick
        # US12345678901231 (non-real IBAN country) before a proper GB IBAN.
        # This handler finds the exact bad element and selects a real IBAN.
        if "iban" in msg.lower() and any(k in msg.lower() for k in ("format", "invalid", "pattern")):
            _bad_m = re.search(r"'([^']+)'", msg)
            _bad_iban = _bad_m.group(1).strip() if _bad_m else None
            _iban_els = [e for e in root.iter()
                         if isinstance(e.tag, str) and etree.QName(e.tag).localname == "IBAN"]
            _iban_target = None
            if _bad_iban:
                _iban_target = next((e for e in _iban_els
                                     if (e.text or "").strip() == _bad_iban), None)
            if _iban_target is None and line_hint is not None and _iban_els:
                _iban_target = min(_iban_els, key=lambda e: abs((e.sourceline or 0) - line_hint))
            elif _iban_target is None and _iban_els:
                _iban_target = _iban_els[0]
            if _iban_target is not None:
                _iban_pat = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$')
                _non_iban_ctry = {"US", "CA", "AU", "NZ", "HK", "SG", "JP", "CN", "IN"}
                # Determine transaction currency to pick a currency-compatible IBAN
                _tx_ccy = None
                for _amt_el in root.iter():
                    if not isinstance(_amt_el.tag, str):
                        continue
                    _accy = _amt_el.get("Ccy")
                    if _accy and etree.QName(_amt_el.tag).localname in (
                        "IntrBkSttlmAmt", "InstdAmt", "TtlIntrBkSttlmAmt", "Amt"
                    ):
                        _tx_ccy = _accy
                        break
                _ccy_by_ctry = _kb_get("dummy_data.currencies_by_country", {}) or {}
                _ibdata = _kb_get("dummy_data.ibans", {}) or {}
                _good_iban = None
                # 1. Prefer another valid IBAN in doc that matches tx currency country
                for _other in _iban_els:
                    if _other is _iban_target:
                        continue
                    _t = (_other.text or "").strip()
                    if (_t and _iban_pat.match(_t)
                            and _t[:2].upper() not in _non_iban_ctry
                            and _verify_iban_mod97(_t)):
                        _ctry2 = _t[:2].upper()
                        _eccy = _ccy_by_ctry.get(_ctry2)
                        if _tx_ccy is None or _eccy == _tx_ccy:
                            _good_iban = _t
                            break
                # 2. Pick from KB ibans matching tx currency
                if _good_iban is None and _tx_ccy and isinstance(_ibdata, dict):
                    for _ctry2, _ccy2 in (_ccy_by_ctry.items() if isinstance(_ccy_by_ctry, dict) else []):
                        if _ccy2 == _tx_ccy and _ctry2 in _ibdata:
                            _cand = _ibdata[_ctry2]
                            if isinstance(_cand, str) and _iban_pat.match(_cand) and _verify_iban_mod97(_cand):
                                _good_iban = _cand
                                break
                if _good_iban is None:
                    _good_iban = _iban_for_ccy(root)
                if _good_iban != (_iban_target.text or "").strip():
                    _tc = self._copy(_iban_target)
                    _tc.text = _good_iban
                    return FixSuggestion(
                        self._xpath_of(_iban_target), self._serialize(_iban_target),
                        self._serialize(_tc), code, msg, "high"
                    )

        # ── Route: BizSvc wrong pattern (e.g. 'swift..02') ─────────────────────
        # HEAD001_BIZSVC_FORMAT is emitted with path=line-number, not an XPath,
        # so the normal path-walk misses the element. Find it directly and fix.
        if code == "HEAD001_BIZSVC_FORMAT":
            _apphdr = root.find(".//{*}AppHdr")
            if _apphdr is None and etree.QName(root.tag).localname == "AppHdr":
                _apphdr = root
            if _apphdr is not None:
                _bsvc_el = self._child_exists(_apphdr, "BizSvc")
                if _bsvc_el is not None:
                    _xml_full = self._serialize(root)
                    _bv = _cbpr_bizsvc_value(_detect_msg_type(_xml_full), _xml_full)
                    if _bv and _bv != (_bsvc_el.text or "").strip():
                        _bsvc_copy = self._copy(_bsvc_el)
                        _bsvc_copy.text = _bv
                        return FixSuggestion(
                            self._xpath_of(_bsvc_el),
                            self._serialize(_bsvc_el),
                            self._serialize(_bsvc_copy),
                            code, msg, "high",
                        )

        # ── Route: DUPLICATE_TAG — use the XPath embedded in fix_hint ───────────
        # The duplicate-tag validator embeds the parent XPath in the fix_suggestion
        # string: "... only 1 is allowed at this location (/A/B/C/Tag)."
        # When path is a line number (not an XPath), extract the location XPath
        # from fix_hint so we target the CORRECT parent element, not a same-named
        # element elsewhere in the document.
        if code == "DUPLICATE_TAG":
            _xpath_m = re.search(r"\((/[^\)]+)\)", fix_hint)
            if _xpath_m:
                _dup_xpath = _xpath_m.group(1).strip()
                # _dup_xpath points to the CHILD (e.g. /…/Agt/FinInstnId).
                # Navigate to the PARENT (/…/Agt) and deduplicate there.
                _dup_parts = [p for p in _dup_xpath.split("/") if p]
                _dup_tag   = _dup_parts[-1] if _dup_parts else ""
                _par_parts = _dup_parts[:-1]
                _par_el = self._walk_dot_path(root, _par_parts) if _par_parts else None
                if _par_el is not None and _dup_tag:
                    _dup_ns = etree.QName(root.tag).namespace or ""
                    _fix = self._fix_value(_par_el, code,
                                           f"Duplicate tag detected: <{_dup_tag}>",
                                           fix_hint, _dup_ns)
                    if _fix is not None and _fix.confidence == "high":
                        return _fix

        # ── Route: missing mandatory AppHdr field (validator completeness scan) ─
        # The header completeness scan emits one clean "/AppHdr/<Tag>" issue per
        # missing mandatory BAH field. The normal missing-child flow can't place
        # these correctly (it would stamp the envelope/Document namespace and,
        # lacking the head.001 XSD order, append at the end). Route them to the
        # namespace-aware, KB-ordered, indented inserter instead.
        _mh = re.search(r"(?:^|/)AppHdr/(\w+)$", path)

        # ── Route: AppHdr child exists but is OUT OF ORDER ───────────────────
        # When HEADER_VAL fires because a named AppHdr element is present but
        # in the wrong position (e.g. CharSet after Fr/To instead of first),
        # resequence all AppHdr children into the canonical head.001 order.
        _APPHDR_ORDER = [
            "CharSet", "Fr", "To", "BizMsgIdr", "MsgDefIdr", "BizSvc",
            "MktPrctc", "CreDt", "CpyDplct", "Pssbl", "Prty", "Sgntr", "Rltd",
        ]
        if code in ("HEADER_VAL",) and _mh:
            _tag_oos = _mh.group(1)
            _apphdr_oos = root.find(".//{*}AppHdr")
            if _apphdr_oos is None and etree.QName(root.tag).localname == "AppHdr":
                _apphdr_oos = root
            _existing_oos_el = self._child_exists(_apphdr_oos, _tag_oos) if _apphdr_oos is not None else None
            # Skip reorder when the element exists but is empty/invalid-value — that
            # is a content error, not a sequence error. The reorder path would return
            # the XML unchanged (order was already correct) and block the value fixer.
            _oos_is_empty = (
                _existing_oos_el is not None
                and not list(_existing_oos_el)
                and not (_existing_oos_el.text or "").strip()
            )
            if _apphdr_oos is not None and _existing_oos_el is not None and not _oos_is_empty:
                # Element exists — it may be out of order. Resequence all children
                # and only return a fix when the order actually changed.
                try:
                    _parser_oos = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                    _r_oos = etree.fromstring(xml.encode("utf-8"), _parser_oos)
                    _ah_oos = _r_oos.find(".//{*}AppHdr")
                    if _ah_oos is None and etree.QName(_r_oos.tag).localname == "AppHdr":
                        _ah_oos = _r_oos
                    if _ah_oos is not None:
                        def _ln_oos(el) -> str:
                            return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""
                        _ah_children = [((_ln_oos(c)), c) for c in list(_ah_oos)]
                        _orig_order = [ln for ln, _ in _ah_children]
                        for _, _c in _ah_children:
                            _ah_oos.remove(_c)
                        _known_oos = {ln: c for ln, c in _ah_children if ln in _APPHDR_ORDER}
                        _unknown_oos = [c for ln, c in _ah_children if ln not in _APPHDR_ORDER]
                        for _slot in _APPHDR_ORDER:
                            if _slot in _known_oos:
                                _ah_oos.append(_known_oos[_slot])
                        for _unk in _unknown_oos:
                            _ah_oos.append(_unk)
                        _new_order = [_ln_oos(c) for c in list(_ah_oos)]
                        if _new_order != _orig_order:
                            _decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
                            _fixed_oos = etree.tostring(_r_oos, encoding="unicode", pretty_print=True)
                            if not _fixed_oos.startswith("<?"):
                                _fixed_oos = _decl + _fixed_oos
                            return FixSuggestion("/", xml, _fixed_oos, code, msg, "high")
                except Exception:
                    pass

        # ── Route: stray/forbidden AppHdr element — strip it ─────────────────
        # When HEADER_VAL fires for a tag that is NOT in _APPHDR_ORDER (e.g.
        # Agt, Cretr, Case, CxlRsnInf injected by mistake into the header),
        # remove all such unknown children from AppHdr in one pass.
        if code in ("HEADER_VAL",) and _mh:
            _stray_tag = _mh.group(1)
            if _stray_tag not in _APPHDR_ORDER:
                _apphdr_str = root.find(".//{*}AppHdr")
                if _apphdr_str is None and etree.QName(root.tag).localname == "AppHdr":
                    _apphdr_str = root
                if _apphdr_str is not None:
                    _unknown_children = [
                        c for c in list(_apphdr_str)
                        if isinstance(c.tag, str)
                        and etree.QName(c.tag).localname not in _APPHDR_ORDER
                    ]
                    if _unknown_children:
                        try:
                            _parser_str = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                            _r_str = etree.fromstring(xml.encode("utf-8"), _parser_str)
                            _ah_str = _r_str.find(".//{*}AppHdr")
                            if _ah_str is None and etree.QName(_r_str.tag).localname == "AppHdr":
                                _ah_str = _r_str
                            if _ah_str is not None:
                                _removed = 0
                                for _c_str in list(_ah_str):
                                    if isinstance(_c_str.tag, str) and etree.QName(_c_str.tag).localname not in _APPHDR_ORDER:
                                        _ah_str.remove(_c_str)
                                        _removed += 1
                                # Also fix Document namespace if it's a head.001 ns (wrong body ns)
                                _doc_str = next(
                                    (e for e in _r_str.iter()
                                     if isinstance(e.tag, str) and etree.QName(e.tag).localname == "Document"),
                                    None,
                                )
                                if _doc_str is not None:
                                    _dstr_ns = etree.QName(_doc_str.tag).namespace or ""
                                    import re as _re_str
                                    _dstr_match = _re_str.match(
                                        r"^urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$",
                                        _dstr_ns,
                                    )
                                    _dstr_msgid = _dstr_match.group(1) if _dstr_match else None
                                    if _dstr_msgid and _dstr_msgid.startswith("head."):
                                        _correct_str_ns = None
                                        # Body element name is authoritative (MsgDefIdr may itself be head.*)
                                        _STR_NS_MAP = {
                                            "FIToFICstmrCdtTrf": "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
                                            "FICdtTrf": "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08",
                                            "FIToFICstmrDrctDbt": "urn:iso:std:iso:20022:tech:xsd:pacs.003.001.08",
                                            "PmtRtr": "urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09",
                                            "FIToFIPmtStsRpt": "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10",
                                            "CstmrPmtCxlReq": "urn:iso:std:iso:20022:tech:xsd:camt.055.001.08",
                                            "FIToFIPmtCxlReq": "urn:iso:std:iso:20022:tech:xsd:camt.056.001.08",
                                            "BkToCstmrAcctRpt": "urn:iso:std:iso:20022:tech:xsd:camt.052.001.08",
                                            "BkToCstmrStmt": "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08",
                                            "BkToCstmrDbtCdtNtfctn": "urn:iso:std:iso:20022:tech:xsd:camt.054.001.08",
                                            "NtfctnToRcv": "urn:iso:std:iso:20022:tech:xsd:camt.057.001.06",
                                            "RsltnOfInvstgtn": "urn:iso:std:iso:20022:tech:xsd:camt.029.001.09",
                                            "CstmrCdtTrfInitn": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09",
                                            "CstmrDrctDbtInitn": "urn:iso:std:iso:20022:tech:xsd:pain.008.001.09",
                                            "CstmrPmtStsRpt": "urn:iso:std:iso:20022:tech:xsd:pain.002.001.09",
                                        }
                                        for _bch in _doc_str:
                                            if isinstance(_bch.tag, str):
                                                _bln = etree.QName(_bch.tag).localname
                                                if _bln in _STR_NS_MAP:
                                                    _correct_str_ns = _STR_NS_MAP[_bln]
                                                    break
                                        # Fallback: MsgDefIdr if body lookup failed and not head.*
                                        if not _correct_str_ns:
                                            _mdi_str = next(
                                                (e for e in _ah_str.iter()
                                                 if isinstance(e.tag, str) and etree.QName(e.tag).localname == "MsgDefIdr"),
                                                None,
                                            )
                                            if _mdi_str is not None:
                                                _mv = (_mdi_str.text or "").strip()
                                                if _mv and not _mv.startswith("head."):
                                                    _correct_str_ns = f"urn:iso:std:iso:20022:tech:xsd:{_mv}"
                                        if _correct_str_ns:
                                            # Rebuild Document subtree with correct ns as default.
                                            # Use nsmap={None: ns} so lxml serializes xmlns="..." (no prefix).
                                            def _rens_defaultns(el, old_ns, new_ns):
                                                q = etree.QName(el.tag)
                                                cur = q.namespace or ""
                                                tgt = new_ns if cur in (old_ns, "") else cur
                                                nsmap = {None: tgt} if tgt else {}
                                                ne = etree.Element(
                                                    f"{{{tgt}}}{q.localname}" if tgt else q.localname,
                                                    nsmap=nsmap,
                                                )
                                                ne.text = el.text
                                                ne.tail = el.tail
                                                for k, v in el.attrib.items():
                                                    ne.set(k, v)
                                                for ch in el:
                                                    if isinstance(ch.tag, str):
                                                        ne.append(_rens_defaultns(ch, old_ns, new_ns))
                                                return ne
                                            _doc_rens = _rens_defaultns(_doc_str, _dstr_ns, _correct_str_ns)
                                            _doc_parent = _doc_str.getparent()
                                            if _doc_parent is not None:
                                                _doc_idx = list(_doc_parent).index(_doc_str)
                                                _doc_parent.remove(_doc_str)
                                                _doc_parent.insert(_doc_idx, _doc_rens)
                                            _removed += 1
                                if _removed:
                                    _decl_str = '<?xml version="1.0" encoding="UTF-8"?>\n'
                                    _fixed_str = etree.tostring(_r_str, encoding="unicode", pretty_print=True)
                                    if not _fixed_str.startswith("<?"):
                                        _fixed_str = _decl_str + _fixed_str
                                    return FixSuggestion("/", xml, _fixed_str, code, msg, "high")
                        except Exception:
                            pass

        if code in ("HEADER_VAL", "HEAD001_BIZSVC_MISSING") and _mh:
            _apphdr = root.find(".//{*}AppHdr")
            if _apphdr is None and etree.QName(root.tag).localname == "AppHdr":
                _apphdr = root
            if _apphdr is not None:
                _tag_name = _mh.group(1)
                _existing_direct = self._child_exists(_apphdr, _tag_name)
                # ── Present-but-empty AppHdr leaf (e.g. <BizMsgIdr/>, <MsgDefIdr/>) ──
                # _child_exists returns the element even when empty — route to
                # _fix_value so the empty-BizMsgIdr handler (and similar) fires.
                if _existing_direct is not None and not list(_existing_direct) and not (
                        _existing_direct.text or "").strip():
                    _ns_local2 = etree.QName(_apphdr.tag).namespace or ""
                    _vfix2 = self._fix_value(_existing_direct, code, msg, fix_hint, _ns_local2)
                    if _vfix2 is not None:
                        return _vfix2
                elif _existing_direct is None:
                    # The flattened "//AppHdr/<Tag>" path only names the leaf and its
                    # nearest reported ancestor — it does NOT mean <Tag> is a DIRECT
                    # child of AppHdr. e.g. BICFI actually lives at
                    # AppHdr/Fr/FIId/FinInstnId/BICFI. Without this check, a value
                    # error (wrong BIC) on that nested element looks "missing" here
                    # and gets a DUPLICATE inserted straight under AppHdr — turning
                    # one error into two (the original bad value AND an "unexpected
                    # field" structural error) while never fixing the real value.
                    _existing_nested = next(
                        (d for d in _apphdr.iter()
                         if d is not _apphdr and isinstance(d.tag, str)
                         and etree.QName(d.tag).localname == _tag_name),
                        None
                    )
                    if _existing_nested is not None:
                        # "Character content not allowed in element-only" means
                        # stray text (e.g. ",k0") is sitting in a container tag.
                        # _fix_value can't strip it — fall through to the Fr/To
                        # rebuilder at the HEADER_VAL block below which does.
                        _is_stray_text_err = (
                            "character content" in msg.lower()
                            or "element-only" in msg.lower()
                            or "not allowed" in msg.lower()
                        )
                        if not _is_stray_text_err:
                            _ns_local = etree.QName(_apphdr.tag).namespace or ""
                            _vfix = self._fix_value(_existing_nested, code, msg, fix_hint, _ns_local)
                            if _vfix is not None:
                                return _vfix
                    else:
                        _res = self._try_insert_missing_sibling(
                            root, xml, code, msg, fix_hint,
                            explicit_parent=_apphdr, explicit_missing=_tag_name,
                        )
                        if _res is not None:
                            return _res

        # ── Route: Empty <Pty/> inside <Cretr> ──────────────────────────────────
        # When <Pty/> is self-closed (no children) inside <Cretr>, the XSD reports
        # "content of element 'Pty' is not complete / expected: 'Nm, Id, ...'"
        # AND Layer 2 may also report "Empty elements found in 'Pty'".
        # Fix: replace the empty <Pty/> (or <Pty></Pty>) with the full Pty block
        # containing Nm + PstlAdr, which satisfies the CBPR+ rule that Nm is
        # mandatory when AnyBIC/BICFI is absent.
        _empty_pty_msg = (
            ("empty" in msg.lower() and "pty" in msg.lower())
            or ("pty" in msg.lower() and "not complete" in msg.lower())
            or ("pty" in msg.lower() and ("nm" in msg.lower() or "id" in msg.lower()))
        )
        if _empty_pty_msg:
            for _pty_el in root.iter():
                if not isinstance(_pty_el.tag, str):
                    continue
                if etree.QName(_pty_el.tag).localname != "Pty":
                    continue
                # Only fix if the Pty has NO child elements (empty/self-closed)
                if len(_pty_el) == 0:
                    _pty_orig = self._serialize(_pty_el)
                    _pty_xpath = self._xpath_of(_pty_el)
                    _pty_tmpl = _TEMPLATES.get("Pty", "")
                    if _pty_tmpl:
                        try:
                            # Use Pty element's own namespace, not the root envelope namespace
                            _ns_pty = etree.QName(_pty_el.tag).namespace or ""
                            _pty_new = etree.fromstring(_pty_tmpl.encode("utf-8"))
                            _pty_new = self._apply_ns(_pty_new, _ns_pty)
                            return FixSuggestion(
                                _pty_xpath, _pty_orig,
                                self._serialize(_pty_new),
                                code, msg, "high"
                            )
                        except Exception:
                            pass

        # ── Route: empty <PstlAdr> — populate with template instead of removing ─
        # When <PstlAdr/> or <PstlAdr></PstlAdr> exists but has no children the
        # AI fallback and KB removal rules would delete it. We populate it with
        # the standard AdrType+AdrLine+Ctry block from _TEMPLATES so the schema
        # constraint (Ctry mandatory) is satisfied without data loss.
        _empty_pstladr = (
            ("pstladr" in msg.lower() and any(k in msg.lower()
                for k in ("empty", "present", "not complete", "missing")))
            or ("PstlAdr" in (msg + fix_hint) and "empty" in (msg + fix_hint).lower())
        )
        if _empty_pstladr:
            for _pa_el in root.iter():
                if not isinstance(_pa_el.tag, str):
                    continue
                if etree.QName(_pa_el.tag).localname != "PstlAdr":
                    continue
                if len(_pa_el) == 0:  # empty (no children)
                    _pa_orig  = self._serialize(_pa_el)
                    _pa_xpath = self._xpath_of(_pa_el)
                    _pa_tmpl  = _TEMPLATES.get("PstlAdr", "")
                    if _pa_tmpl:
                        try:
                            _ns_pa  = etree.QName(_pa_el.tag).namespace or ""
                            _pa_new = etree.fromstring(_pa_tmpl.encode("utf-8"))
                            _pa_new = self._apply_ns(_pa_new, _ns_pa)
                            return FixSuggestion(
                                _pa_xpath, _pa_orig,
                                self._serialize(_pa_new),
                                code, msg, "high",
                            )
                        except Exception:
                            pass

        # ── Route: "content of element 'X' is not complete. One of the following
        #    elements is expected: 'Y'." (e.g. PAIN008_FWDGAGT_MANDATORY — GrpHdr
        #    missing FwdgAgt). The element X EXISTS but is missing a mandatory
        #    CHILD Y. These often arrive with path="/" (the rule reports a line
        #    number), so the normal flow recovers X and tries to fix ITS value —
        #    a no-op. Instead, locate X and INSERT the missing child Y into it.
        _mc = re.search(r"content of element '([\w:.\-]+)' is not complete", msg, re.I)
        if _mc:
            _parent_name = _mc.group(1).split('}')[-1].split(':')[-1]
            _exp = re.search(r"expected\s*:?\s*(.+)$", msg, re.I | re.S)
            _children: list[str] = []
            if _exp:
                for blob in re.findall(r"'([^']+)'", _exp.group(1)):
                    for tok in re.split(r"[,\s|]+", blob):
                        tok = tok.strip().split('}')[-1].split(':')[-1]
                        if tok and tok not in _children:
                            _children.append(tok)
            _matches = [el for el in root.iter()
                        if isinstance(el.tag, str)
                        and etree.QName(el.tag).localname == _parent_name]
            if _matches and _children:
                _lh = getattr(self, "_line_hint", None)
                _parent_el = (min(_matches, key=lambda e: abs((e.sourceline or 0) - _lh))
                              if _lh is not None else _matches[0])
                # The XSD "expected: A, B, C" list enumerates every element ALLOWED
                # next — including OPTIONAL ones. Inserting the first missing one
                # blindly injects e.g. an optional TtlIntrBkSttlmAmt=0.00 ahead of
                # the genuinely-missing mandatory SttlmInf, manufacturing a
                # NON_POSITIVE_AMOUNT error. Try XSD-MANDATORY expected children
                # first; only fall back to optional ones if no mandatory is missing.
                _xp = self._get_xsd_path(xml)
                _tm = _XsdTypeMap.get(_xp) if _xp else None
                _ptype = None
                if _tm is not None:
                    try:
                        _ptype = _tm.type_of_path(self._local_name_path(_parent_el))
                    except Exception:
                        _ptype = None
                    if not _ptype:
                        _ptype = _tm.element_type.get(_parent_name)
                _optional = set()
                if _tm is not None and _ptype:
                    _pi = _tm.type_info.get(_ptype, {})
                    if _pi.get("kind") == "sequence":
                        _optional = {c["name"] for c in _pi.get("children", [])
                                     if c.get("min", "1") == "0"}
                _mand = [c for c in _children if c not in _optional]
                _opt = [c for c in _children if c in _optional]
                for _child in _mand + _opt:
                    if self._child_exists(_parent_el, _child) is None:
                        _res = self._try_insert_missing_sibling(
                            root, xml, code, msg, fix_hint,
                            explicit_parent=_parent_el, explicit_missing=_child,
                        )
                        if _res is not None:
                            return _res

        # ── Route: SCHEMA_VAL "character content … element-only" ──────────────
        # Triggered when stray characters (e.g. " ,.?"} ") are typed directly
        # into the editor between or after element tags inside an element-only
        # container (e.g. after </FIToFIPmtStsRpt> but before </Document>).
        # The validator surfaces this as:
        #   "Character content other than whitespace is not allowed because the
        #    content type is 'element-only'."
        # Fix: locate the named container, strip any non-whitespace text/tail
        # nodes from all its direct children (and its own .text).
        _eo_m = re.search(
            r"(?:field|element)\s+'([^']+)'.*?content type is\s+'element-only'",
            msg, re.I | re.S
        )
        if code in ("SCHEMA_VAL", "XML_SYNTAX", "WF_STRAY_TEXT_BETWEEN_ELEMENTS") and _eo_m:
            _eo_fix = self._fix_stray_text_element_only(root, _eo_m.group(1), code, msg, xml)
            if _eo_fix is not None:
                return _eo_fix

        # Broader catch: even without a named element in the message, strip
        # non-whitespace text/tail from any element-only container when the
        # error fingerprint matches.
        if code == "SCHEMA_VAL" and "element-only" in msg.lower() and "character content" in msg.lower():
            _eo_fix2 = self._fix_stray_text_element_only(root, "", code, msg, xml)
            if _eo_fix2 is not None:
                return _eo_fix2

        # ── Route: simple-type element contains child elements ────────────────
        # "Field 'X': Element content is not allowed, because the type definition
        # is simple." — the element is declared as Max35Text (or similar simple
        # type) but the XML has child elements inside it (e.g. <Id> containing
        # an <IBAN> child alongside text, from copy-paste or editor error).
        # Fix: strip all child elements, keep only the text content. Use line
        # hint to pick the right element when multiple same-name elements exist.
        if code == "SCHEMA_VAL" and "element content is not allowed" in msg.lower() and "simple" in msg.lower():
            # All-at-once variant first: repairs EVERY corrupted simple leaf in
            # one fix (common-ancestor targeted) so the loop converges in one
            # round instead of one round per leaf.
            _all_leaf_fix = self._fix_elements_inside_simple_leaf(root, xml, code, msg)
            if _all_leaf_fix is not None:
                return _all_leaf_fix
            _st_m = re.search(r"[Ff]ield '([\w:{}.\-]+)'", msg)
            _st_tag = _st_m.group(1).split('}')[-1].split(':')[-1] if _st_m else ""
            if _st_tag:
                _st_cands = [_e for _e in root.iter()
                             if isinstance(_e.tag, str)
                             and etree.QName(_e.tag).localname == _st_tag
                             and len(_e) > 0]  # must have child elements to be relevant
                if _st_cands:
                    _lh = getattr(self, "_line_hint", None)
                    _st_el = (min(_st_cands,
                                  key=lambda _e: abs((_e.sourceline or 0) - _lh))
                              if _lh is not None else _st_cands[0])
                    _st_copy = self._copy(_st_el)
                    # Keep only text content; remove all child elements
                    _text = (_st_copy.text or "").strip()
                    for _ch in list(_st_copy):
                        _st_copy.remove(_ch)
                    _st_copy.text = _text if _text else None
                    return FixSuggestion(
                        self._xpath_of(_st_el),
                        self._serialize(_st_el),
                        self._serialize(_st_copy),
                        code, msg, "high"
                    )

        # ── Route: ADDR_CTRY_MISSING — Country missing from PstlAdr ─────────
        # Emitted with path=line_number (→ "/"), so normal path-walking fails.
        # We extract the party name from the message, locate the right PstlAdr,
        # and either insert <Ctry> into it OR insert a full dummy PstlAdr block
        # if the address element doesn't exist at all.
        if code == "ADDR_CTRY_MISSING":
            _ns_early = etree.QName(root.tag).namespace or ""
            _xsd_path_early = self._get_xsd_path(xml)
            _tmap_early = _XsdTypeMap.get(_xsd_path_early) if _xsd_path_early else None
            _mt_early = _detect_msg_type(xml)
            _ridx_early = _RulesIndex.get(_mt_early) if _mt_early else None
            _addr_fix = self._fix_addr_ctry_missing(
                root, xml, code, msg, fix_hint,
                _ns_early, _tmap_early, _ridx_early, _mt_early,
            )
            if _addr_fix is not None:
                return _addr_fix

        # ── Route: element-only container has stray text content ─────────────
        # "Field 'X': Character content other than whitespace is not allowed
        #  because the content type is 'element-only'."
        # Fix: strip the stray text from the container.
        # Also repair the most common structural follow-on: a bare <BICFI> /
        # <LEI> sitting directly inside <Agt> without a <FinInstnId> wrapper.
        if "element-only" in msg.lower() or (
                "character content" in msg.lower() and "not allowed" in msg.lower()):
            _eo_m = re.search(r"[Ff]ield '([\w:{}.\-]+)'", msg)
            _eo_tag = _eo_m.group(1).split('}')[-1].split(':')[-1] if _eo_m else ""
            if _eo_tag:
                _eo_cands = [_e for _e in root.iter()
                             if isinstance(_e.tag, str)
                             and etree.QName(_e.tag).localname == _eo_tag]
                if _eo_cands:
                    _lh = getattr(self, "_line_hint", None)
                    _eo_el = (min(_eo_cands,
                                  key=lambda _e: abs((_e.sourceline or 0) - _lh))
                              if _lh is not None else _eo_cands[0])
                    _eo_copy = self._copy(_eo_el)
                    # Strip any text/tail on the container and its children
                    _eo_copy.text = None
                    for _ch in _eo_copy:
                        _ch.tail = None
                    # For <Agt>: if children are bare FinInstnId leaf tags
                    # (BICFI, LEI, AnyBIC, …) without a <FinInstnId> wrapper,
                    # add the wrapper so the XSD sequence is satisfied.
                    if _eo_tag == "Agt":
                        _fi_leaf = {
                            "BICFI", "LEI", "AnyBIC", "ClrSysMmbId",
                            "Othr", "Nm", "PstlAdr",
                        }
                        _kids = list(_eo_copy)
                        _has_fi = any(
                            isinstance(_c.tag, str)
                            and etree.QName(_c.tag).localname == "FinInstnId"
                            for _c in _kids
                        )
                        if not _has_fi and _kids and all(
                            isinstance(_c.tag, str)
                            and etree.QName(_c.tag).localname in _fi_leaf
                            for _c in _kids
                        ):
                            _eo_ns = etree.QName(_eo_el.tag).namespace or ""
                            _fi_tag = f"{{{_eo_ns}}}FinInstnId" if _eo_ns else "FinInstnId"
                            _fi_wrap = etree.Element(_fi_tag)
                            for _c in list(_eo_copy):
                                _eo_copy.remove(_c)
                                _fi_wrap.append(_c)
                            _eo_copy.append(_fi_wrap)
                    _fixed_frag = self._serialize(_eo_copy)
                    if _fixed_frag != self._serialize(_eo_el):
                        return FixSuggestion(
                            self._xpath_of(_eo_el),
                            self._serialize(_eo_el),
                            _fixed_frag,
                            code, msg, "high",
                        )

        # ── Guard: element-ordering / structural-position errors ─────────────
        # "X is not expected at this position" / "not allowed here" means the
        # element exists but sits in the wrong XSD sequence slot. A safe fix
        # requires reordering siblings into the schema's declared order — risky
        # to guess element-by-element, and a wrong guess CREATES new errors
        # (the exact "it changes tags and makes new errors" complaint). Without
        # an LLM we deliberately decline rather than corrupt the document:
        # return low confidence so the UI shows guidance, not a bad auto-fix.
        # ── Element-ordering / structural-position errors ────────────────────
        # "X is not expected here ... One of the following elements is expected:
        # A, B." means a mandatory element is missing before X (or siblings are
        # out of XSD order). We FIRST attempt a schema-driven reconstruction:
        # insert the missing mandatory element(s) named by the schema and
        # reorder the parent to the XSD sequence (see _try_sequence_fix). Only
        # if the schema cannot resolve it do we decline (low confidence) rather
        # than risk corrupting the document with a guessed reorder.
        _msg_lc_early = (msg + " " + fix_hint).lower()
        if any(s in _msg_lc_early for s in (
            "not expected at this position",
            "not expected here",
            "is not allowed here",
            "not allowed in this context",
            "following element",
            "missing before",
            "found here",
            "unexpected field",
            "missed a mandatory field",
        )):
            # ── Wrong ISO 20022 document root element ────────────────────────
            # Each message type has a unique Document child element name.
            # When the wrong one appears (e.g. BkToCstmrStmt inside a camt.052
            # or NtfctnToRcvStsRpt inside a camt.053), rename it to the element
            # the declared namespace actually requires.
            _ISO20022_DOC_ROOTS = {
                # camt — cash management
                "camt.029": "RsltnOfInvstgtn",
                "camt.052": "BkToCstmrAcctRpt",
                "camt.053": "BkToCstmrStmt",
                "camt.054": "BkToCstmrDbtCdtNtfctn",
                "camt.055": "CstmrPmtCxlReq",
                "camt.056": "FIToFIPmtCxlReq",
                "camt.057": "NtfctnToRcv",
                "camt.058": "NtfctnToRcvCxlAdvc",
                "camt.059": "NtfctnToRcvStsRpt",
                # pacs — payments clearing and settlement
                "pacs.002": "FIToFIPmtStsRpt",
                "pacs.004": "PmtRtr",
                "pacs.008": "FIToFICstmrCdtTrf",
                "pacs.009": "FICdtTrf",
                # pain — payment initiation
                "pain.001": "CstmrCdtTrfInitn",
                "pain.002": "CstmrPmtStsRpt",
                "pain.008": "CstmrDrctDbtInitn",
            }
            _all_known_roots = "|".join(re.escape(v) for v in _ISO20022_DOC_ROOTS.values())
            _m_wrong_root = re.search(
                rf"element '({_all_known_roots})' is not expected",
                msg, re.I
            )
            if _m_wrong_root:
                _wrong_name = _m_wrong_root.group(1)
                _mt_now = _detect_msg_type(xml)
                _correct_root = next(
                    (v for k, v in _ISO20022_DOC_ROOTS.items() if k in _mt_now), None
                )
                if _correct_root and _correct_root != _wrong_name:
                    for _bad_el in root.iter():
                        if (isinstance(_bad_el.tag, str)
                                and etree.QName(_bad_el.tag).localname == _wrong_name):
                            _root_copy = self._copy(root)
                            for _bad_copy in _root_copy.iter():
                                if (isinstance(_bad_copy.tag, str)
                                        and etree.QName(_bad_copy.tag).localname == _wrong_name):
                                    _ns = etree.QName(_bad_copy.tag).namespace
                                    _bad_copy.tag = (f"{{{_ns}}}{_correct_root}"
                                                     if _ns else _correct_root)
                            return FixSuggestion(
                                "/", self._serialize(root),
                                self._serialize(_root_copy), code, msg, "high"
                            )
            # ── Missing closing tag check ─────────────────────────────────────
            # When lxml recover=True auto-closes a removed closing tag at the
            # wrong nesting level, the document looks well-formed but has wrong
            # structure → XSD gives "Element X not expected here".
            # Strict (non-recovery) parse exposes the real structural error;
            # if it fails with a tag mismatch/unclosed error, route to recovery
            # which correctly re-inserts the missing closing tag.
            try:
                etree.fromstring(xml.encode("utf-8"),
                                 etree.XMLParser(recover=False, no_network=True))
            except etree.XMLSyntaxError as _strict_e:
                _se_msg = str(_strict_e).lower()
                if any(k in _se_msg for k in (
                    "end tag", "endtag", "not found", "mismatch",
                    "unclosed", "unexpected", "opening and ending",
                )):
                    _rcv = self._try_xml_recovery(xml, code, str(_strict_e))
                    if _rcv is not None:
                        return _rcv
            except Exception:
                pass
            # ── Choice over-population: a Choice container holds MORE THAN ONE
            # mutually-exclusive member, so the parser flags the extra one as
            # "not expected". Canonical case: AccountIdentification4Choice — an
            # <Id> carrying BOTH <IBAN> and <Othr> (only one is allowed). Unlike
            # a blind choice-member removal (which the code below rightly refuses
            # to guess), this is unambiguous: exactly one must go. Keep whichever
            # member is VALID, preferring IBAN, and drop the competing member.
            _choice_fix = self._try_collapse_choice(root, code, msg)
            if _choice_fix is not None:
                return _choice_fix
            # ── CBPR+ forbidden element → remove it outright ──────────────────
            # When the flagged element is one CBPR+ doesn't permit in its parent
            # (e.g. <ClrSys> in <SttlmInf>), it can't be reordered or completed
            # into validity — it must go. Take precedence over the insert/reorder
            # attempts below, which (using the lenient base XSD) would otherwise
            # wrongly try to keep it.
            _m_off = re.search(r"element '([\w:{}.\-]+)' is not expected", msg, re.I)
            _off_local = _m_off.group(1).split('}')[-1].split(':')[-1] if _m_off else ""
            if _off_local:
                _cands = []
                for _el in root.iter():
                    if not isinstance(_el.tag, str) or etree.QName(_el.tag).localname != _off_local:
                        continue
                    _p = _el.getparent()
                    _pl = (etree.QName(_p.tag).localname
                           if (_p is not None and isinstance(_p.tag, str)) else "")
                    if _pl in _CBPR_FORBIDDEN_CHILDREN and _off_local in _CBPR_FORBIDDEN_CHILDREN[_pl]:
                        _cands.append((_el, _p))
                    # AppHdr/FinInstnId may only contain BICFI (CBPR+ head.001
                    # restriction). Nm/PstlAdr/ClrSysMmbId/LEI/Othr are valid in
                    # Document FinInstnId blocks but NOT in AppHdr Fr/To chains.
                    _apphdr_fininstnid_forbidden = {"Nm", "PstlAdr", "ClrSysMmbId", "LEI", "Othr"}
                    if (_pl == "FinInstnId" and _off_local in _apphdr_fininstnid_forbidden):
                        # Confirm this FinInstnId is under AppHdr (not Document)
                        _anc_locals = {etree.QName(a.tag).localname
                                       for a in _el.iterancestors()
                                       if isinstance(a.tag, str)}
                        if "AppHdr" in _anc_locals and "Document" not in _anc_locals:
                            _cands.append((_el, _p))
                if _cands:
                    _lh = getattr(self, "_line_hint", None)
                    _el, _p = (min(_cands, key=lambda t: abs((t[0].sourceline or 0) - _lh))
                               if _lh is not None else _cands[0])
                    _pcopy = self._copy(_p)
                    _pcopy.remove(list(_pcopy)[list(_p).index(_el)])
                    return FixSuggestion(self._xpath_of(_p), self._serialize(_p),
                                         self._serialize(_pcopy), code, msg, "high")

            # A mandatory sibling that must precede this element may have been
            # deleted entirely (e.g. AppHdr/Fr removed → <To> trips this error).
            # That is a safe INSERT, not a risky reorder — try it before
            # declining. Falls through to the low-confidence no-op when no
            # missing mandatory element can be confidently identified/built.
            # Wrong-level element (no expected list) → move it into the schema
            # container that actually allows it (e.g. FinInstnId/Ctry → PstlAdr).
            repositioned = self._try_reposition_element(
                root, xml, code, msg, _detect_msg_type(xml)
            )
            if repositioned is not None:
                return repositioned

            inserted = self._try_insert_missing_sibling(root, xml, code, msg, fix_hint)
            if inserted is not None:
                return inserted

            # KB-driven removal: some "not expected" elements are CBPR-removed
            # elements (e.g. SplmtryData, MsgPgntn) whose documented fix is simply
            # to delete them. Only remove when the KB explicitly says "Remove <X>"
            # — never guess removal for choice members like Othr.
            try:
                _nx_el = self._find_target(root, path, self._build_nsmap(root)) if (path and path != "/") else None
            except Exception:
                _nx_el = None
            if _nx_el is None:
                _nx_el = self._recover_target_from_message(root, msg, fix_hint)
            if _nx_el is not None:
                _nx_local = etree.QName(_nx_el.tag).localname
                _kb = _KBContext.get(_detect_family_from_tree(root))
                _kb_fixes = _kb.possible_fixes(code, _nx_local) if _kb else []
                if any(re.search(r"\bremove\b", fx, re.I) and _nx_local.lower() in fx.lower()
                       for fx in _kb_fixes):
                    _rem = self._remove_element_fix(_nx_el, code, msg)
                    if _rem is not None:
                        return _rem

            seq_fix = self._try_sequence_fix(
                root, xml, code, msg, "", _detect_msg_type(xml), None
            )
            if seq_fix is not None:
                return seq_fix
            # Schema-level exclusivity (e.g. "<Nm> is not expected" while BICFI is
            # present) → remove the forbidden sibling(s) per the KB rules.
            dep_fix = self._try_dependency_fix(root, path, code, msg, _detect_msg_type(xml), None)
            if dep_fix is not None:
                return dep_fix
            # Wrap orphaned block: when TxInf-level elements (OrgnlGrpInf,
            # OrgnlEndToEndId, …) appear at Undrlyg level after </TxInf>, group
            # them into a new TxInf. Also resolves the lxml-recover duplicate
            # that results from those elements being folded into the prior TxInf.
            _wrap_fix = self._try_wrap_orphaned_block(root, xml, code, msg)
            if _wrap_fix is not None:
                return _wrap_fix

            # Relocate elements nested too deeply: when an element (and its
            # trailing misplaced siblings) are stranded inside a parent that
            # doesn't accept them but a known ancestor does, lift the whole
            # group to the correct ancestor level and reorder both levels per
            # XSD sequence.  Must run BEFORE _try_sequence_fix so the relocation
            # path wins over Case 1b's deletion of small sub-trees.
            _reloc_fix = self._try_relocate_to_ancestor(root, xml, code, msg)
            if _reloc_fix is not None:
                return _reloc_fix

            # Schema-driven sequence fix: handles pure ordering violations (e.g.
            # AppHdr/BizMsgIdr before Fr/To) and missing mandatory predecessors
            # for ANY message type, including camt.052 AppHdr reordering.
            _seq_ns = etree.QName(root.tag).namespace or ""
            _seq_msg_type = _detect_msg_type(xml)
            _seq_rules_idx = _RulesIndex.get(_seq_msg_type) if _seq_msg_type else None
            _seq_fix = self._try_sequence_fix(root, xml, code, msg, _seq_ns,
                                              _seq_msg_type or "", _seq_rules_idx)
            if _seq_fix is not None:
                return _seq_fix

            _target_xpath = path if (path and path != "/") else self._xpath_of(root)
            _frag = ""
            try:
                _tgt = self._find_target(root, _target_xpath, self._build_nsmap(root))
                if _tgt is not None:
                    _frag = self._serialize(_tgt)
                    _target_xpath = self._xpath_of(_tgt)
            except Exception:
                pass
            return FixSuggestion(_target_xpath, _frag, _frag, code, msg, "low")

        ns = etree.QName(root.tag).namespace or ""

        # Load XSD and rules index once per XML (cached). AppHdr/* elements are
        # defined by the Business Application Header (head.001) schema, not the
        # Document message schema — pick the right one so AppHdr fixes are also
        # schema-driven (e.g. AppHdr/CreDt is ISODateTime, ordered Fr→To→…→CreDt).
        # True when the path targets an element inside AppHdr scope — but NOT when
        # the path crosses into a Document body nested under AppHdr (BusMsgEnvlp
        # pattern: AppHdr/Document/FIDrctDbt/… lives in the message XSD, not head.001).
        # Rule: AppHdr is in the path AND Document does NOT appear after it.
        _path_parts = [p for p in path.replace("/", ".").split(".") if p]
        _ah_idx = next((i for i, p in enumerate(_path_parts) if p == "AppHdr"), -1)
        _doc_after_ah = any(p == "Document" for p in _path_parts[_ah_idx + 1:]) if _ah_idx >= 0 else False
        targets_apphdr = _ah_idx >= 0 and not _doc_after_ah
        xsd_path  = (self._get_apphdr_xsd_path(xml) if targets_apphdr
                     else self._get_xsd_path(xml))
        tmap      = _XsdTypeMap.get(xsd_path) if xsd_path else None
        msg_type  = _detect_msg_type(xml)
        rules_idx = _RulesIndex.get(msg_type) if msg_type else None

        # ── Cross-field dependency rules (BICFI/AnyBIC exclusivity, Name/Address
        #    coexistence) report a line number or "/" path — not the offending
        #    element — so resolve and repair the block directly from the rule. ──
        pred_fix = self._fix_missing_predecessor(root, xml, code, msg, msg_type)
        if pred_fix is not None:
            return pred_fix

        # ── Route: CBPR-mandatory-but-XSD-optional agent deleted ──────────────
        # InstgAgt/InstdAgt (pacs.008/009), CdtrAgt/DbtrAgt (pain.001/008) are
        # mandatory under CBPR+ but optional in the base XSD, so deletion trips
        # only a business rule (CBPR_R3 / L3-*-MANDATORY-PARTIES) whose path is
        # the header — the schema inserter never targets the missing agent.
        # Insert it here, sourcing BICFI so CBPR_R3 (Fr==InstgAgt, To==InstdAgt)
        # holds. Run BEFORE the generic dependency fix (which would no-op on the
        # header BICFI the rule misleadingly points at).
        cbpr_agent_fix = self._fix_missing_cbpr_mandatory(root, xml, code, msg, msg_type)
        if cbpr_agent_fix is not None:
            return cbpr_agent_fix

        # ── Route: MISSING_MANDATORY_FIELD — path-driven insertion ───────────────
        # CBPR+ validators emit code="MISSING_MANDATORY_FIELD" (not an XSD error)
        # for pacs.010 DrctDbtTxInf missing UETR/EndToEndId/IntrBkSttlmAmt/Dbtr/
        # DbtrAgt.  The path points at the absent element, not an existing one, so
        # _find_target returns None and the implicit _try_insert_missing_sibling
        # (which relies on XSD "not expected here" messages) never fires.
        # Route: parse path → derive parent_tag + missing_tag → explicit insert.
        # SR2026 delta-rule validators (new_mandatory_fields.py, pacs004_validator.py,
        # camt_statement_validator.py, etc.) emit one bespoke "MISSING_<FIELD>" code
        # per field instead of a shared generic code — but the path still points at
        # the absent element, so the same path-driven insertion below applies.
        # MISSING_ATTRIBUTE is excluded: it has its own dedicated @Ccy-attribute
        # route further down, not an element insertion.
        _is_missing_field_code = code in (
            "MISSING_MANDATORY_FIELD", "CBPR_MANDATORY_FIELD",
            "MANDATORY_FIELD_MISSING", "MISSING_FIELD",
        ) or (code.startswith("MISSING_") and code != "MISSING_ATTRIBUTE")
        _missing_in_msg = (
            any(w in msg.lower() for w in ("absent", "missing", "not present", "required"))
            and any(w in msg.lower() for w in ("mandatory", "required", "cbpr"))
        )

        # ── Route: MISSING_MANDATORY_FIELD — entirely absent AppHdr ─────────────
        # When AppHdr is completely absent the path is "/AppHdr" (length-1 after
        # splitting), so the generic path-driven handler below can't fire.
        # Build a canonical AppHdr from data already present in the Document and
        # insert it in the correct position:
        #   BusMsgEnvlp envelope → after </Document>, before </BusMsgEnvlp>
        #   Bare Document root   → immediately before <Document>
        if (
            _is_missing_field_code
            and path
            and re.sub(r'[/.]', '', path).strip() == "AppHdr"
            and root.find(".//{*}AppHdr") is None
        ):
            try:
                _fixed_ah = self._insert_missing_apphdr(root, xml, code, msg, msg_type)
                if _fixed_ah is not None:
                    return _fixed_ah
            except Exception:
                pass

        # ── Route: MISSING_MANDATORY_FIELD — path-driven insertion ───────────────
        # CBPR+ validators emit code="MISSING_MANDATORY_FIELD" (not an XSD error)
        # for pacs.010 DrctDbtTxInf missing UETR/EndToEndId/IntrBkSttlmAmt/Dbtr/
        # DbtrAgt.  The path points at the absent element, not an existing one, so
        # _find_target returns None and the implicit _try_insert_missing_sibling
        # (which relies on XSD "not expected here" messages) never fires.
        # Route: parse path → derive parent_tag + missing_tag → explicit insert.
        if (_is_missing_field_code or _missing_in_msg) and path and path != "/":
            _pp = [p for p in re.split(r'[/.]', path)
                   if p and _VALID_XML_NAME.match(p)]
            if len(_pp) >= 2:
                _missing_tag = _pp[-1]
                _parent_tag  = _pp[-2]
                _parent_cands = [el for el in root.iter()
                                 if isinstance(el.tag, str)
                                 and etree.QName(el.tag).localname == _parent_tag]
                if _parent_cands:
                    _pe = (min(_parent_cands, key=lambda e: abs((e.sourceline or 0) - (line_hint or 0)))
                           if line_hint is not None else _parent_cands[0])
                    if self._child_exists(_pe, _missing_tag) is None:
                        _mf_res = self._try_insert_missing_sibling(
                            root, xml, code, msg, fix_hint,
                            explicit_parent=_pe, explicit_missing=_missing_tag,
                        )
                        if _mf_res is not None:
                            return _mf_res

        dep_fix = self._try_dependency_fix(root, path, code, msg, msg_type, tmap)
        if dep_fix is not None:
            return dep_fix

        # ── Route: AppHdr/MsgDefIdr ≠ Document namespace ─────────────────────────
        # The path is a line number, not an XPath, so the normal path-walk fails.
        # MsgDefIdr text is format-valid — _fix_value would exit as a no-op.
        # Document namespace is authoritative; update MsgDefIdr to match it.
        if code == "HEAD001_MSGDEFIDR_MISMATCH":
            _mdi_fix = self._fix_msgdefidr_mismatch(root, code, msg)
            if _mdi_fix is not None:
                return _mdi_fix

        # ── Route: BizMsgIdr / MsgDefIdr invalid CBPR+ characters ────────────
        # Strip characters not in RestrictedFINXMax35Text: A-Za-z0-9 space /\-?:().,'+
        if code in ("BIZMSGID_INVALID_CHARS", "MSGDEFIDR_INVALID_CHARS"):
            _tag_local = "BizMsgIdr" if code == "BIZMSGID_INVALID_CHARS" else "MsgDefIdr"
            _apphdr = root.find(".//{*}AppHdr")
            _target_el = None
            if _apphdr is not None:
                for _ch in _apphdr.iter():
                    if isinstance(_ch.tag, str) and etree.QName(_ch.tag).localname == _tag_local:
                        _target_el = _ch
                        break
            if _target_el is None:
                for _ch in root.iter():
                    if isinstance(_ch.tag, str) and etree.QName(_ch.tag).localname == _tag_local:
                        _target_el = _ch
                        break
            if _target_el is not None and _target_el.text:
                _CBPR_ALLOWED_RE = re.compile(r"[^A-Za-z0-9 /\-?:().,'+]")
                _cleaned = _CBPR_ALLOWED_RE.sub('', _target_el.text.strip())
                if _cleaned and _cleaned != _target_el.text.strip():
                    _xpath = self._xpath_of(_target_el)
                    _orig = self._serialize(_target_el)
                    _copy_el = self._copy(_target_el)
                    _copy_el.text = _cleaned
                    return FixSuggestion(
                        xpath=_xpath,
                        original_fragment=_orig,
                        fragment_xml=self._serialize(_copy_el),
                        issue_code=code,
                        issue_message=msg,
                        confidence="high",
                    )

        # ── Route: camt BAH/BIC mismatch (Fr≠Assgnr or To≠Assgne) ──────────────
        # Covers SR2025 codes (CAMT056_FR_EQ_ASSGNR_BIC / CAMT056_TO_EQ_ASSGNE_BIC)
        # and SR2026 codes (BAH_FR_ASSGNR_MISMATCH / BAH_TO_ASSGNE_MISMATCH).
        # AppHdr BICFI is format-valid so _fix_value exits early — route directly.
        _ASSGNR_CODES = {
            "CAMT056_FR_EQ_ASSGNR_BIC", "BAH_FR_ASSGNR_MISMATCH",
            "CAMT056_TO_EQ_ASSGNE_BIC", "BAH_TO_ASSGNE_MISMATCH",
        }
        _msg_lc = msg.lower()
        _is_assgnr_msg = (
            "assgnr" in _msg_lc or "assgne" in _msg_lc
            or "assigner" in _msg_lc or "assignee" in _msg_lc
        )
        if code in _ASSGNR_CODES or (code in ("BAH_FR_INSTGAGT_MISMATCH", "BAH_TO_INSTDAGT_MISMATCH") and _is_assgnr_msg):
            # Determine side from code
            if code in ("CAMT056_FR_EQ_ASSGNR_BIC", "BAH_FR_ASSGNR_MISMATCH"):
                _assgnr_side_code = "CAMT056_FR_EQ_ASSGNR_BIC"
            elif code in ("CAMT056_TO_EQ_ASSGNE_BIC", "BAH_TO_ASSGNE_MISMATCH"):
                _assgnr_side_code = "CAMT056_TO_EQ_ASSGNE_BIC"
            else:
                _assgnr_side_code = "CAMT056_FR_EQ_ASSGNR_BIC" if ("fr" in _msg_lc or "assgnr" in _msg_lc or "assigner" in _msg_lc) else "CAMT056_TO_EQ_ASSGNE_BIC"
            _bic_fix = self._fix_bah_assgnr_assgne_bic(root, _assgnr_side_code, msg)
            if _bic_fix is not None:
                return _bic_fix

        # ── Route: AppHdr Fr/To BICFI must match InstgAgt/InstdAgt BICFI ────────
        # Codes: CBPR_R3 (pacs.008 rules), L3-PACS-MATCH-TO/FR (shared pacs.json),
        # CBPR_R3 variants for other message families. Path is a line number and
        # the AppHdr BIC is format-valid, so the normal path-walk is a no-op.
        _BIC_HEADER_CODES = {
            "CBPR_R3", "CBPR_P9_R2", "CBPR_P9_R3",
            "L3-PACS-MATCH-TO", "L3-PACS-MATCH-FR",
            "TO_BIC_MISMATCH", "FROM_BIC_MISMATCH",
            "BAH_FR_INSTGAGT_MISMATCH", "BAH_TO_INSTDAGT_MISMATCH",
        }
        _is_bic_header_msg = (
            ("apphdr" in _msg_lc and "bicfi" in _msg_lc
             and ("instdagt" in _msg_lc or "instgagt" in _msg_lc))
            or ("bah" in _msg_lc and "bic" in _msg_lc
                and ("instructed agent" in _msg_lc or "instructing agent" in _msg_lc))
        )
        if code in _BIC_HEADER_CODES or _is_bic_header_msg:
            _r3_fix = self._fix_cbpr_r3_bic(root, code, msg)
            if _r3_fix is not None:
                return _r3_fix

        # ── Route: pain.002 party elements misplaced in GrpHdr ────────────────
        # Rule L3-PAIN002-PARTY-PLACEMENT: <Dbtr>, <Cdtr>, <DbtrAgt>, <CdtrAgt>
        # must live under TxInfAndSts/OrgnlTxRef, NOT inside GrpHdr.
        # Fix: remove misplaced party elements from GrpHdr and inject them into
        # OrgnlTxRef inside each TxInfAndSts (creating OrgnlTxRef if absent).
        if code == "L3-PAIN002-PARTY-PLACEMENT":
            _PARTY_TAGS = {"Dbtr", "Cdtr", "DbtrAgt", "CdtrAgt"}
            try:
                import copy as _copy_mod
                _parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r2 = etree.fromstring(xml.encode("utf-8"), _parser)
                _ns = ""

                def _ln(el) -> str:
                    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""

                # Collect misplaced party elements from GrpHdr
                _grp_hdr = next(
                    (el for el in _r2.iter() if _ln(el) == "GrpHdr"), None
                )
                _misplaced: list = []
                if _grp_hdr is not None:
                    _ns = etree.QName(_grp_hdr.tag).namespace or ""
                    for _ch in list(_grp_hdr):
                        if isinstance(_ch.tag, str) and _ln(_ch) in _PARTY_TAGS:
                            _misplaced.append(_copy_mod.deepcopy(_ch))
                            _grp_hdr.remove(_ch)

                if not _misplaced:
                    # Nothing misplaced found — nothing to fix
                    raise ValueError("no misplaced party elements detected")

                # Inject into OrgnlTxRef under each TxInfAndSts
                _tx_inf_els = [el for el in _r2.iter() if _ln(el) == "TxInfAndSts"]
                for _tx in _tx_inf_els:
                    _orig_ref = next(
                        (c for c in _tx if isinstance(c.tag, str) and _ln(c) == "OrgnlTxRef"),
                        None
                    )
                    if _orig_ref is None:
                        _orig_ref = etree.SubElement(
                            _tx,
                            f"{{{_ns}}}OrgnlTxRef" if _ns else "OrgnlTxRef"
                        )
                    # Avoid duplicating party elements already present
                    _existing_party_names = {_ln(c) for c in _orig_ref if isinstance(c.tag, str)}
                    for _party_el in _misplaced:
                        if _ln(_party_el) not in _existing_party_names:
                            _to_insert = _copy_mod.deepcopy(_party_el)
                            # Re-namespace element and all descendants to match document ns
                            if _ns:
                                for _sub in _to_insert.iter():
                                    if isinstance(_sub.tag, str) and not _sub.tag.startswith("{"):
                                        _sub.tag = f"{{{_ns}}}{etree.QName(_sub.tag).localname}"
                            _orig_ref.append(_to_insert)

                _decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
                _fixed = etree.tostring(_r2, encoding="unicode", pretty_print=True)
                if not _fixed.startswith("<?"):
                    _fixed = _decl + _fixed
                return FixSuggestion("/", xml, _fixed, code, msg, "high")
            except Exception:
                pass

        # ── Route: pain.002 GrpHdr missing InitgPty before FwdgAgt ────────────
        # PAIN002_GRPHDR_INITGPTY_REQUIRED: insert <InitgPty> before <FwdgAgt>.
        if code == "PAIN002_GRPHDR_INITGPTY_REQUIRED":
            try:
                import copy as _copy_mod
                _parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r3 = etree.fromstring(xml.encode("utf-8"), _parser)

                def _ln3(el) -> str:
                    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""

                _grp = next((el for el in _r3.iter() if _ln3(el) == "GrpHdr"), None)
                if _grp is not None:
                    _ns3 = etree.QName(_grp.tag).namespace or ""
                    # Only insert if InitgPty is truly absent
                    _has_initg = any(_ln3(c) == "InitgPty" for c in _grp if isinstance(c.tag, str))
                    if not _has_initg:
                        _initg_el = etree.fromstring(
                            (f'<InitgPty xmlns="{_ns3}"><Nm>Initiating Party</Nm></InitgPty>'
                             if _ns3 else '<InitgPty><Nm>Initiating Party</Nm></InitgPty>'
                             ).encode("utf-8")
                        )
                        # Find insertion position: before FwdgAgt, or after CreDtTm
                        _fwdg = next(
                            (i for i, c in enumerate(_grp) if isinstance(c.tag, str) and _ln3(c) == "FwdgAgt"),
                            None
                        )
                        if _fwdg is not None:
                            _grp.insert(_fwdg, _initg_el)
                        else:
                            _grp.append(_initg_el)
                        _decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
                        _fixed3 = etree.tostring(_r3, encoding="unicode", pretty_print=True)
                        if not _fixed3.startswith("<?"):
                            _fixed3 = _decl + _fixed3
                        return FixSuggestion("/", xml, _fixed3, code, msg, "high")
            except Exception:
                pass

        # ── Route: pain.002 TxInfAndSts missing OrgnlUETR before TxSts ────────
        # PAIN002_ORGNLUETR_REQUIRED: insert <OrgnlUETR> before <TxSts>.
        if code == "PAIN002_ORGNLUETR_REQUIRED":
            try:
                import copy as _copy_mod
                _parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _r4 = etree.fromstring(xml.encode("utf-8"), _parser)
                _lh4 = getattr(self, "_line_hint", None)

                def _ln4(el) -> str:
                    return etree.QName(el.tag).localname if isinstance(el.tag, str) else ""

                _tx_els = [el for el in _r4.iter() if _ln4(el) == "TxInfAndSts"]
                _target_tx = self._pick_nearest(_tx_els, _lh4) if _tx_els else None
                if _target_tx is not None:
                    _ns4 = etree.QName(_target_tx.tag).namespace or ""
                    _has_uetr = any(_ln4(c) == "OrgnlUETR" for c in _target_tx if isinstance(c.tag, str))
                    if not _has_uetr:
                        _new_uetr_val = str(uuid.uuid4())
                        _uetr_el = etree.fromstring(
                            (f'<OrgnlUETR xmlns="{_ns4}">{_new_uetr_val}</OrgnlUETR>'
                             if _ns4 else f'<OrgnlUETR>{_new_uetr_val}</OrgnlUETR>'
                             ).encode("utf-8")
                        )
                        # Insert before TxSts (or before OrgnlTxRef if present)
                        _txsts_idx = next(
                            (i for i, c in enumerate(_target_tx)
                             if isinstance(c.tag, str) and _ln4(c) == "TxSts"),
                            None
                        )
                        if _txsts_idx is not None:
                            _target_tx.insert(_txsts_idx, _uetr_el)
                        else:
                            _target_tx.append(_uetr_el)
                        _decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
                        _fixed4 = etree.tostring(_r4, encoding="unicode", pretty_print=True)
                        if not _fixed4.startswith("<?"):
                            _fixed4 = _decl + _fixed4
                        return FixSuggestion("/", xml, _fixed4, code, msg, "high")
            except Exception:
                pass

        # ── Route: NbOfTxs count mismatch ─────────────────────────────────────
        # Path is a line number; element value is numeric-valid so _fix_value
        # won't update it. Route directly to count-aware fixer.
        if code in ("NBOFTXS_MISMATCH", "PACS008_NBOFTXS_EQ_TX_COUNT",
                    "CBPR_COV_R30"):
            _nb_fix = self._fix_nb_of_txs(root, code, msg)
            if _nb_fix is not None:
                return _nb_fix

        # ── Route: pain.001 CBPR+ NbOfTxs must equal PmtInf block count ───────
        if code == "PAIN001_NBOFTXS_MISMATCH":
            _nb_el = next(
                (el for el in root.iter()
                 if isinstance(el.tag, str)
                 and etree.QName(el.tag).localname == "NbOfTxs"),
                None
            )
            if _nb_el is not None:
                _pmtinf_count = sum(
                    1 for el in root.iter()
                    if isinstance(el.tag, str)
                    and etree.QName(el.tag).localname == "PmtInf"
                )
                if _pmtinf_count > 0 and str(_pmtinf_count) != (_nb_el.text or "").strip():
                    _orig = self._serialize(_nb_el)
                    _copy = self._copy(_nb_el)
                    _copy.text = str(_pmtinf_count)
                    return FixSuggestion(
                        self._xpath_of(_nb_el), _orig, self._serialize(_copy),
                        code, msg, "high"
                    )

        # ── Route: pain.001 UETR missing inside CdtTrfTxInf/PmtId ─────────────
        if code == "PAIN001_UETR_REQUIRED":
            _lh = getattr(self, "_line_hint", None)
            _pmtid_els = [
                el for el in root.iter()
                if isinstance(el.tag, str)
                and etree.QName(el.tag).localname == "PmtId"
                and not any(
                    etree.QName(c.tag).localname == "UETR"
                    for c in el
                    if isinstance(c.tag, str)
                )
            ]
            if _pmtid_els:
                _target_pmtid = self._pick_nearest(_pmtid_els, _lh)
                if _target_pmtid is not None:
                    _ns = etree.QName(_target_pmtid.tag).namespace or ""
                    _new_uetr = str(uuid.uuid4())
                    _orig = self._serialize(_target_pmtid)
                    _copy = self._copy(_target_pmtid)
                    _uetr_el = etree.SubElement(
                        _copy,
                        f"{{{_ns}}}UETR" if _ns else "UETR"
                    )
                    _uetr_el.text = _new_uetr
                    return FixSuggestion(
                        self._xpath_of(_target_pmtid), _orig,
                        self._serialize(_copy), code, msg, "high"
                    )

        # ── Route: pain.001 extra CdtTrfTxInf inside a PmtInf ─────────────────
        # Structural split (move extra CdtTrfTxInf to its own PmtInf) is too
        # complex to auto-apply safely; return a descriptive low-confidence hint.
        if code == "PAIN001_MAX_ONE_TX_PER_PMTINF":
            _lh = getattr(self, "_line_hint", None)
            _extra_txs = [
                el for el in root.iter()
                if isinstance(el.tag, str)
                and etree.QName(el.tag).localname == "CdtTrfTxInf"
            ]
            _target_tx = self._pick_nearest(_extra_txs, _lh) if _extra_txs else None
            if _target_tx is not None:
                _orig = self._serialize(_target_tx)
                return FixSuggestion(
                    self._xpath_of(_target_tx), _orig, _orig,
                    code, msg, "low"
                )

        # ── Route: pacs.009 COV SttlmMtd must be INDA/INGA (CBPR_COV_R31) ──────
        # The CBPR+ COV usage guideline (SWIFT MyStandards) REJECTS 'COVE' for
        # the COV variant — it must be INDA or INGA (cover settled on accounts).
        # Path is a line number so _fix_value's path-walk never lands on the
        # GrpHdr SttlmMtd — find it directly and set a permitted value.
        if code == "CBPR_COV_R31":
            for _sm_el in root.iter():
                if (isinstance(_sm_el.tag, str)
                        and etree.QName(_sm_el.tag).localname == "SttlmMtd"
                        and (_sm_el.text or "").strip() not in ("INDA", "INGA")):
                    _sm_copy = self._copy(_sm_el)
                    _sm_copy.text = "INGA"
                    return FixSuggestion(
                        self._xpath_of(_sm_el), self._serialize(_sm_el),
                        self._serialize(_sm_copy), code, msg, "high")

        # ── Route: SEPA service level requires EUR (CBPR_COV_R32) ─────────────
        # SEPA is only valid for EUR. On a non-EUR pacs.009 the SvcLvl is the
        # erroneous element — removing it is the safe minimal fix (changing the
        # IntrBkSttlmAmt currency would alter the payment's monetary value). If
        # the enclosing PmtTpInf is left childless, drop PmtTpInf instead so no
        # empty (schema-invalid) container remains.
        if code == "CBPR_COV_R32":
            for _sl_el in root.iter():
                if not isinstance(_sl_el.tag, str):
                    continue
                if etree.QName(_sl_el.tag).localname != "SvcLvl":
                    continue
                _cd = next((c for c in _sl_el
                            if isinstance(c.tag, str)
                            and etree.QName(c.tag).localname == "Cd"), None)
                if _cd is None or (_cd.text or "").strip() != "SEPA":
                    continue
                _ptp = _sl_el.getparent()
                _ptp_children = [c for c in _ptp if isinstance(c.tag, str)] \
                    if _ptp is not None else []
                _target = _ptp if (_ptp is not None
                                   and etree.QName(_ptp.tag).localname == "PmtTpInf"
                                   and len(_ptp_children) == 1) else _sl_el
                _r32_fix = self._remove_element_fix(_target, code, msg)
                if _r32_fix is not None:
                    return _r32_fix

        # ── Route: pacs.009 COV reimbursement agents not allowed (CBPR_COV_R33) ─
        # Reimbursement agents belong to SttlmMtd=COVE (pacs.009 ADV). The CBPR+
        # COV usage guideline rejects them — remove each one (and its paired
        # account) from SttlmInf. One removal per call; the loop re-fires until
        # all are gone.
        if code == "CBPR_COV_R33":
            _RMB = {"InstgRmbrsmntAgt", "InstgRmbrsmntAgtAcct",
                    "InstdRmbrsmntAgt", "InstdRmbrsmntAgtAcct",
                    "ThrdRmbrsmntAgt", "ThrdRmbrsmntAgtAcct"}
            for _rb_el in root.iter():
                if (isinstance(_rb_el.tag, str)
                        and etree.QName(_rb_el.tag).localname in _RMB):
                    _r33_fix = self._remove_element_fix(_rb_el, code, msg)
                    if _r33_fix is not None:
                        return _r33_fix

        # ── Route: Wrong Namespace — strip extra version components ───────────
        # Layer 1 emits "Wrong Namespace" when xmlns has extra dot-segments
        # (e.g. pacs.004.001.09.12.12.12). Fix by truncating to 4 components.
        if code == "Wrong Namespace":
            _ns_fix = self._fix_wrong_namespace(xml, code, msg)
            if _ns_fix is not None:
                return _ns_fix

        # ── Route: CURR_IBAN_MISMATCH — currency doesn't match IBAN country ──
        # Path may be the IBAN key (no @Ccy suffix), so attr routing misses it.
        if code == "CURR_IBAN_MISMATCH":
            _ccy_fix = self._fix_iban_currency_mismatch(root, code, msg, fix_hint, path)
            if _ccy_fix is not None:
                return _ccy_fix

        # ── Route: XchgRate must be removed (currencies are identical) ────────
        # PACS008_NO_XCHGRATE_IF_SAME_CCY / PACS003_XCHGRATE_FORBIDDEN_WHEN_INSTD_SAME
        # The fix is deletion, not a value change — intercept before _fix_value
        # would repair the decimal to a plausible rate (1.0).
        if "NO_XCHGRATE" in code or "XCHGRATE_FORBIDDEN" in code:
            for _xr_el in root.iter():
                if isinstance(_xr_el.tag, str) and etree.QName(_xr_el.tag).localname == "XchgRate":
                    _xr_fix = self._remove_element_fix(_xr_el, code, msg)
                    if _xr_fix is not None:
                        return _xr_fix

        # ══════════════════════════════════════════════════════════════════════
        # ── GAP HANDLERS: deterministic fixes for previously-unhandled codes ──
        # Each block is self-contained and returns early when it can act.
        # ══════════════════════════════════════════════════════════════════════

        # ── UETR_FORMAT_ERROR — malformed UUID v4 → fresh UUID ────────────────
        # Covers both <UETR> and <OrgnlUETR> (camt.055/056 originals can also
        # end up with embedded whitespace after XML recovery truncates tags).
        if code == "UETR_FORMAT_ERROR" or ("uetr" in msg.lower() and "format" in msg.lower()):
            for _uel in root.iter():
                if isinstance(_uel.tag, str) and etree.QName(_uel.tag).localname in ("UETR", "OrgnlUETR"):
                    _uel_copy = self._copy(_uel)
                    _uel_copy.text = str(uuid.uuid4())
                    return FixSuggestion(self._xpath_of(_uel), self._serialize(_uel),
                                         self._serialize(_uel_copy), code, msg, "high")

        # ── LEI_FORMAT — invalid LEI → replace with valid 20-char LEI ─────────
        if code == "LEI_FORMAT" or ("lei" in msg.lower() and ("format" in msg.lower() or "invalid" in msg.lower())):
            for _lel in root.iter():
                if isinstance(_lel.tag, str) and etree.QName(_lel.tag).localname == "LEI":
                    _lel_copy = self._copy(_lel)
                    _lel_copy.text = "549300TRUWOII88U4F73"
                    return FixSuggestion(self._xpath_of(_lel), self._serialize(_lel),
                                         self._serialize(_lel_copy), code, msg, "high")

        # ── BICFI whitespace — strip leading/trailing whitespace from BIC value ─
        # XSD pattern check fails when <BICFI> contains embedded newlines/spaces
        # (e.g. "BNPPGB2LXXX\n            "). The stripped value is a valid BIC;
        # fix by trimming the element text in-place.
        _is_bic_ws = (
            "whitespace" in msg.lower()
            and any(k in msg.upper() for k in ("BICFI", "BIC"))
        )
        if _is_bic_ws:
            _BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
            # Try to extract the expected BIC from the message (after stripping)
            _bic_hint = re.search(r"exactly '([A-Z0-9]{8,11})'", msg)
            _target_bic = _bic_hint.group(1) if _bic_hint else None
            for _bel in root.iter():
                if not isinstance(_bel.tag, str):
                    continue
                if etree.QName(_bel.tag).localname not in ("BICFI", "AnyBIC"):
                    continue
                _raw = _bel.text or ""
                _stripped = _raw.strip()
                if _raw != _stripped and _BIC_RE.match(_stripped):
                    if _target_bic and _stripped != _target_bic:
                        continue
                    _bel_copy = self._copy(_bel)
                    _bel_copy.text = _stripped
                    return FixSuggestion(self._xpath_of(_bel), self._serialize(_bel),
                                         self._serialize(_bel_copy), code, msg, "high")

        # ── ID_LENGTH_ERROR — ID field exceeds max length → truncate ─────────
        if code == "ID_LENGTH_ERROR" or ("length" in msg.lower() and "max" in msg.lower()
                                          and any(t in msg for t in ("Id", "MsgId", "EndToEndId", "TxId", "InstrId"))):
            # Extract the actual max length from the error message (e.g. "maximum allowed 16")
            _maxlen_m = re.search(r"maximum\s+(?:allowed\s+)?(\d+)", msg, re.I)
            _max_id_len = int(_maxlen_m.group(1)) if _maxlen_m else 35

            _id_tags = {"MsgId", "EndToEndId", "TxId", "InstrId", "BizMsgIdr",
                        "OrgnlMsgId", "OrgnlEndToEndId", "OrgnlInstrId", "PmtInfId",
                        "Id", "ClrSysRef"}
            _pp = [p for p in path.replace("/", ".").split(".") if p and _VALID_XML_NAME.match(p)]
            _tgt_tag = _pp[-1] if _pp else ""
            _tgt_set = {_tgt_tag} if _tgt_tag else _id_tags

            # When the target is the generic "Id" tag, also use the parent context
            # (e.g. Assgnmt/Id) to pick the right element among many <Id> nodes.
            _parent_tag = _pp[-2] if len(_pp) >= 2 else ""

            for _iel in root.iter():
                if not isinstance(_iel.tag, str): continue
                _ln = etree.QName(_iel.tag).localname
                if _ln not in _tgt_set: continue
                if not (_iel.text and len(_iel.text.strip()) > _max_id_len): continue
                # If target is generic "Id", require parent tag to match for precision
                if _ln == "Id" and _parent_tag:
                    _par = _iel.getparent()
                    _par_ln = etree.QName(_par.tag).localname if _par is not None and isinstance(_par.tag, str) else ""
                    if _par_ln and _par_ln != _parent_tag:
                        continue
                _iel_copy = self._copy(_iel)
                _iel_copy.text = _iel.text.strip()[:_max_id_len]
                return FixSuggestion(self._xpath_of(_iel), self._serialize(_iel),
                                     self._serialize(_iel_copy), code, msg, "high")

        # ── DUPLICATE_ID_VALUE — same ID used twice → append suffix ───────────
        if code == "DUPLICATE_ID_VALUE":
            _pp = [p for p in path.replace("/", ".").split(".") if p and _VALID_XML_NAME.match(p)]
            _tgt_tag = _pp[-1] if _pp else ""
            if not _tgt_tag:
                # path="/" (document-level dup check) — recover the tag from
                # the message text, e.g. "...found for tag <UETR>."
                _tag_m = re.search(r"<(\w+)>", f"{msg} {fix_hint}")
                _tgt_tag = _tag_m.group(1) if _tag_m else ""
            if _tgt_tag:
                _dups = [el for el in root.iter()
                         if isinstance(el.tag, str) and etree.QName(el.tag).localname == _tgt_tag]
                if len(_dups) >= 2:
                    _lh = getattr(self, "_line_hint", None)
                    # fix the duplicate closest to the reported line; default to the last one
                    _el = (min(_dups, key=lambda e: abs((e.sourceline or 0) - _lh))
                           if _lh is not None else _dups[-1])
                    _el_copy = self._copy(_el)
                    if _tgt_tag in ("UETR", "OrgnlUETR"):
                        # UETR is a strict UUIDv4 — a "-2" suffix would break its format
                        _el_copy.text = str(uuid.uuid4())
                    else:
                        _base = (_el.text or "").strip()
                        # Keep the SAME length — a "-2" suffix can blow past tight
                        # max-length constraints (e.g. InstrId capped at 16 chars
                        # where the original value already uses all 16).
                        _el_copy.text = _dedupe_id_value(_base) if _base else str(uuid.uuid4())[:35]
                    return FixSuggestion(self._xpath_of(_el), self._serialize(_el),
                                         self._serialize(_el_copy), code, msg, "high")

        # ── PARTY_NAME_LENGTH — Nm too long → truncate ────────────────────────
        if code in ("PARTY_NAME_LENGTH", "PACS004_NM_LEN") or (
                "name" in msg.lower() and "length" in msg.lower()):
            _max_nm = 140
            _len_m = re.search(r"(?:max|maximum)\s+(\d+)", msg, re.I)
            if _len_m:
                _max_nm = int(_len_m.group(1))
            for _nel in root.iter():
                if isinstance(_nel.tag, str) and etree.QName(_nel.tag).localname == "Nm":
                    if _nel.text and len(_nel.text.strip()) > _max_nm:
                        _nel_copy = self._copy(_nel)
                        _nel_copy.text = _nel.text.strip()[:_max_nm]
                        return FixSuggestion(self._xpath_of(_nel), self._serialize(_nel),
                                              self._serialize(_nel_copy), code, msg, "high")

        # ── PARTY_NAME_CTRL_CHAR / NEWLINE / XML_CHARS — strip bad chars ──────
        if code in ("PARTY_NAME_CTRL_CHAR", "PARTY_NAME_NEWLINE", "PARTY_NAME_XML_CHARS",
                    "ADDR_CTRL_CHAR", "ADDR_FIELD_CTRL", "PACS004_FINX_CHARSET"):
            _CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
            _CBPR_ALLOWED_SIMPLE = re.compile(r"[^A-Za-z0-9 /\-?:().,'\n\r\t]")
            _changed_any = False
            _first_el = None
            _first_orig = None
            for _cel in root.iter():
                if not isinstance(_cel.tag, str): continue
                _ln = etree.QName(_cel.tag).localname
                if _ln in ("Nm", "AdrLine", "StrtNm", "TwnNm", "BldgNm",
                           "Dept", "SubDept", "Flr", "Room", "PstBx", "DstrctNm",
                           "BldgNb", "PstCd") and _cel.text:
                    _cleaned = _CTRL_RE.sub("", _cel.text).replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
                    if _cleaned != _cel.text:
                        if _first_el is None:
                            _first_el = _cel
                            _first_orig = self._serialize(_cel)
                        _cel.text = _cleaned
                        _changed_any = True
            if _changed_any and _first_el is not None:
                return FixSuggestion(self._xpath_of(_first_el), _first_orig,
                                      self._serialize(_first_el), code, msg, "high")

        # ── PARTY_NAME_EMPTY — empty Nm → placeholder ─────────────────────────
        if code == "PARTY_NAME_EMPTY":
            for _nel in root.iter():
                if isinstance(_nel.tag, str) and etree.QName(_nel.tag).localname == "Nm":
                    if not (_nel.text or "").strip():
                        _nel_copy = self._copy(_nel)
                        _nel_copy.text = "Unknown Party"
                        return FixSuggestion(self._xpath_of(_nel), self._serialize(_nel),
                                              self._serialize(_nel_copy), code, msg, "low")

        # ── ADDR_ADRLINE_LENGTH / LIMIT — AdrLine > 35 chars → truncate ───────
        if code in ("ADDR_ADRLINE_LENGTH", "ADDR_ADRLINE_LIMIT", "PACS004_ADRLINE_LEN"):
            for _ael in root.iter():
                if isinstance(_ael.tag, str) and etree.QName(_ael.tag).localname == "AdrLine":
                    if _ael.text and len(_ael.text.strip()) > 35:
                        _ael_copy = self._copy(_ael)
                        _ael_copy.text = _ael.text.strip()[:35]
                        return FixSuggestion(self._xpath_of(_ael), self._serialize(_ael),
                                              self._serialize(_ael_copy), code, msg, "high")

        # ── ADDR_ADRLINE_WHITESPACE / ADDR_FIELD_WHITESPACE — normalise ────────
        if code in ("ADDR_ADRLINE_WHITESPACE", "ADDR_FIELD_WHITESPACE",
                    "ADDR_ADRLINE_EMPTY", "ADDR_FIELD_EMPTY"):
            _addr_tags = {"AdrLine", "StrtNm", "TwnNm", "PstCd", "BldgNb",
                          "BldgNm", "Dept", "SubDept", "Ctry", "Flr", "Room", "PstBx"}
            for _ael in root.iter():
                if not isinstance(_ael.tag, str): continue
                _ln = etree.QName(_ael.tag).localname
                if _ln in _addr_tags:
                    _raw = _ael.text or ""
                    if code in ("ADDR_ADRLINE_EMPTY", "ADDR_FIELD_EMPTY") and not _raw.strip():
                        # Remove empty address line elements entirely
                        _par = _ael.getparent()
                        if _par is not None:
                            _orig_par = self._serialize(_par)
                            _par_copy = self._copy(_par)
                            for _ch in list(_par_copy):
                                if etree.QName(_ch.tag).localname == _ln and not (_ch.text or "").strip():
                                    _par_copy.remove(_ch)
                                    break
                            if self._serialize(_par_copy) != _orig_par:
                                return FixSuggestion(self._xpath_of(_par), _orig_par,
                                                      self._serialize(_par_copy), code, msg, "high")
                    elif _raw != re.sub(r'\s+', ' ', _raw).strip():
                        _ael_copy = self._copy(_ael)
                        _ael_copy.text = re.sub(r'\s+', ' ', _raw).strip()
                        return FixSuggestion(self._xpath_of(_ael), self._serialize(_ael),
                                              self._serialize(_ael_copy), code, msg, "high")

        # ── ADDR_ADRLINE_CHARSET — non-CBPR+ chars in AdrLine → strip ─────────
        if code == "ADDR_ADRLINE_CHARSET":
            _CBPR = re.compile(r"[^A-Za-z0-9 /\-?:().,']")
            for _ael in root.iter():
                if isinstance(_ael.tag, str) and etree.QName(_ael.tag).localname == "AdrLine":
                    if _ael.text:
                        _cl = _CBPR.sub("", _ael.text).strip()
                        if _cl != _ael.text.strip():
                            _ael_copy = self._copy(_ael)
                            _ael_copy.text = _cl or "Unknown Address"
                            return FixSuggestion(self._xpath_of(_ael), self._serialize(_ael),
                                                  self._serialize(_ael_copy), code, msg, "high")

        # ── ADDR_FIELD_LENGTH — address field (StrtNm/TwnNm/PstCd) too long ───
        if code == "ADDR_FIELD_LENGTH":
            _MAXLEN = {"StrtNm": 70, "TwnNm": 35, "PstCd": 16, "BldgNb": 16, "BldgNm": 35,
                       "Dept": 70, "SubDept": 70, "Flr": 70, "PstBx": 16}
            for _ael in root.iter():
                if not isinstance(_ael.tag, str): continue
                _ln = etree.QName(_ael.tag).localname
                _mx = _MAXLEN.get(_ln)
                if _mx and _ael.text and len(_ael.text.strip()) > _mx:
                    _ael_copy = self._copy(_ael)
                    _ael_copy.text = _ael.text.strip()[:_mx]
                    return FixSuggestion(self._xpath_of(_ael), self._serialize(_ael),
                                          self._serialize(_ael_copy), code, msg, "high")

        # ── ADDR_PREFER_STRUCTURED — true-unstructured address → add TwnNm/Ctry (Hybrid) ─
        if code == "ADDR_PREFER_STRUCTURED":
            for _pael in root.iter():
                if not isinstance(_pael.tag, str): continue
                if etree.QName(_pael.tag).localname == "PstlAdr":
                    _has_ctry = any(etree.QName(c.tag).localname == "Ctry"
                                    for c in _pael if isinstance(c.tag, str))
                    _has_town = any(etree.QName(c.tag).localname == "TwnNm"
                                    for c in _pael if isinstance(c.tag, str))
                    if not _has_ctry or not _has_town:
                        _orig = self._serialize(_pael)
                        _par_copy = self._copy(_pael)
                        _addr_ns = etree.QName(_pael.tag).namespace or ""
                        if not _has_town:
                            _twn_el = etree.SubElement(_par_copy,
                                f"{{{_addr_ns}}}TwnNm" if _addr_ns else "TwnNm")
                            _twn_el.text = "New York"
                        if not _has_ctry:
                            _ctry_el = etree.SubElement(_par_copy,
                                f"{{{_addr_ns}}}Ctry" if _addr_ns else "Ctry")
                            _ctry_el.text = "US"
                        return FixSuggestion(self._xpath_of(_pael), _orig,
                                              self._serialize(_par_copy), code, msg, "low")

        # ── EMPTY_PRTRY — empty <Prtry> → add placeholder text ───────────────
        if code == "EMPTY_PRTRY":
            for _epel in root.iter():
                if isinstance(_epel.tag, str) and etree.QName(_epel.tag).localname == "Prtry":
                    if not (_epel.text or "").strip() and not list(_epel):
                        _ep_copy = self._copy(_epel)
                        _ep_copy.text = "NOTPROVIDED"
                        return FixSuggestion(self._xpath_of(_epel), self._serialize(_epel),
                                              self._serialize(_ep_copy), code, msg, "low")

        # ── EMPTY_REQUIRED_CONTAINER / EMPTY_ACCOUNT_CONTAINER / EMPTY_PARTY_CONTAINER ─
        if code in ("EMPTY_REQUIRED_CONTAINER", "EMPTY_ACCOUNT_CONTAINER", "EMPTY_PARTY_CONTAINER"):
            _CONTAINER_FILLS = {
                "FinInstnId": "<FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId>",
                "Id": "<Id><IBAN>GB29NWBK60161331926819</IBAN></Id>",
                "PstlAdr": "<PstlAdr><TwnNm>New York</TwnNm><Ctry>US</Ctry><AdrLine>123 Main Street</AdrLine><AdrLine>Address Line 2</AdrLine></PstlAdr>",
                "Dbtr": "<Dbtr><Nm>Debtor Name</Nm><PstlAdr><TwnNm>New York</TwnNm><Ctry>US</Ctry><AdrLine>123 Main St</AdrLine><AdrLine>Address Line 2</AdrLine></PstlAdr></Dbtr>",
                "Cdtr": "<Cdtr><Nm>Creditor Name</Nm><PstlAdr><TwnNm>London</TwnNm><Ctry>GB</Ctry><AdrLine>456 Oak Ave</AdrLine><AdrLine>Address Line 2</AdrLine></PstlAdr></Cdtr>",
                "SttlmInf": "<SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf>",
                "PmtId": "<PmtId><EndToEndId>E2E-NOTPROVIDED</EndToEndId></PmtId>",
                "InitgPty": "<InitgPty><Nm>Initiating Party</Nm></InitgPty>",
                "BkTxCd": "<BkTxCd><Domn><Cd>PMNT</Cd><Fmly><Cd>RCDT</Cd><SubFmlyCd>OTHR</SubFmlyCd></Fmly></Domn></BkTxCd>",
            }

            def _ec_fill(_ecel) -> Optional["FixSuggestion"]:
                """Fill an empty container from the canned map, in the doc's ns."""
                _ln = etree.QName(_ecel.tag).localname
                _tmpl = _CONTAINER_FILLS.get(_ln)
                if _tmpl is None:
                    return None
                # Never fill an element that carries text — it is a populated
                # LEAF, not an empty container. Appending elements next to text
                # corrupts it (the <Othr><Id>ACCT…<IBAN>dummy</IBAN></Id> bug).
                if (_ecel.text or "").strip():
                    return None
                # The "Id" recipe is for ACCOUNT-level Id (child of *Acct) only.
                # Generic <Id> leaves (Othr/Id, ClrSysMmbId/MmbId-style) share
                # the tag name but are simple-type leaves — filling them with
                # an <IBAN> child produces invalid XML.
                if _ln == "Id":
                    _ec_par = _ecel.getparent()
                    _ec_par_ln = (etree.QName(_ec_par.tag).localname
                                  if _ec_par is not None
                                  and isinstance(_ec_par.tag, str) else "")
                    if not _ec_par_ln.endswith("Acct"):
                        return None
                try:
                    _ecel_ns = etree.QName(_ecel.tag).namespace or ""
                    _wrap = (f'<w xmlns="{_ecel_ns}">{_tmpl}</w>' if _ecel_ns
                             else f"<w>{_tmpl}</w>")
                    _new_el = etree.fromstring(_wrap.encode("utf-8"))[0]
                    _orig_ec = self._serialize(_ecel)
                    _ec_copy = self._copy(_ecel)
                    for _child in list(_new_el):
                        _ec_copy.append(self._copy(_child))
                    if self._serialize(_ec_copy) != _orig_ec:
                        return FixSuggestion(self._xpath_of(_ecel), _orig_ec,
                                              self._serialize(_ec_copy), code, msg, "low")
                except Exception:
                    pass
                return None

            # The message names the offending container; target THAT element
            # rather than the first map-hit anywhere in the document.
            _ec_name_m = re.search(
                r"<(\w+)>\s+(?:is present but empty|is present but carries no"
                r"|contains no identifying)", msg)
            _ec_name = _ec_name_m.group(1) if _ec_name_m else None
            if _ec_name:
                _ec_cands = [el for el in root.iter()
                             if isinstance(el.tag, str)
                             and etree.QName(el.tag).localname == _ec_name
                             and not any(isinstance(c.tag, str) for c in el)
                             and not (el.text or "").strip()]
                _eel = self._pick_nearest(_ec_cands, getattr(self, "_line_hint", None))
                if _eel is not None:
                    _filled = _ec_fill(_eel)
                    if _filled is not None:
                        return _filled
                    # No fill recipe — if the XSD says this container is optional
                    # in its parent, the safe repair is to drop the empty wrapper.
                    _ec_parent = _eel.getparent()
                    if _ec_parent is not None and tmap is not None:
                        _ecp_type = tmap.type_of_path(self._local_name_path(_ec_parent))
                        _ec_min = next(
                            (c.get("min", "1") for c in
                             tmap.type_info.get(_ecp_type, {}).get("children", [])
                             if c.get("name") == _ec_name), None)
                        if _ec_min == "0":
                            _rem = self._remove_element_fix(_eel, code, msg)
                            if _rem is not None:
                                return _rem
                    # Known-optional elements that are always safe to remove when
                    # empty (their XSD minOccurs=0 is not tmap-reachable in all
                    # message families).
                    _ALWAYS_REMOVABLE_EMPTY = {"CtctDtls"}
                    if _ec_name in _ALWAYS_REMOVABLE_EMPTY:
                        _rem = self._remove_element_fix(_eel, code, msg)
                        if _rem is not None:
                            return _rem

            # Fallback (message didn't name the container): first fillable empty.
            for _ecel in root.iter():
                if not isinstance(_ecel.tag, str): continue
                if not list(_ecel):
                    _filled = _ec_fill(_ecel)
                    if _filled is not None:
                        return _filled

        # ── ACCT_MISSING_ID — account has no Id child → add IBAN ─────────────
        if code == "ACCT_MISSING_ID":
            _ACCT_TAGS = {"DbtrAcct", "CdtrAcct", "ChrgsAcct", "SttlmAcct"}
            for _acct_el in root.iter():
                if not isinstance(_acct_el.tag, str): continue
                _ln = etree.QName(_acct_el.tag).localname
                if _ln in _ACCT_TAGS:
                    _has_id = any(etree.QName(c.tag).localname == "Id"
                                  for c in _acct_el if isinstance(c.tag, str))
                    if not _has_id:
                        _orig_ac = self._serialize(_acct_el)
                        _ac_ns = etree.QName(_acct_el.tag).namespace or ns
                        _ac_copy = self._copy(_acct_el)
                        _id_el = etree.fromstring(
                            f'<Id><IBAN>GB29NWBK60161331926819</IBAN></Id>'.encode("utf-8"))
                        _ac_copy.insert(0, _id_el)
                        return FixSuggestion(self._xpath_of(_acct_el), _orig_ac,
                                              self._serialize(_ac_copy), code, msg, "low")

        # ── CBPR_CTRLSUM_FORBIDDEN — CtrlSum not permitted in pacs.009 GrpHdr ──
        if code == "CBPR_CTRLSUM_FORBIDDEN":
            lh = getattr(self, "_line_hint", None)
            cs_els = [el for el in root.iter()
                      if isinstance(el.tag, str)
                      and etree.QName(el.tag).localname == "CtrlSum"
                      and el.getparent() is not None
                      and isinstance(el.getparent().tag, str)
                      and etree.QName(el.getparent().tag).localname == "GrpHdr"]
            if cs_els:
                cs_el = (min(cs_els, key=lambda e: abs((e.sourceline or 0) - lh))
                         if lh is not None else cs_els[0])
                _rem = self._remove_element_fix(cs_el, code, msg)
                if _rem is not None:
                    return _rem
            # CtrlSum already absent — return a no-op so callers don't fall
            # through to unrelated handlers that may alter unrelated content.
            return FixSuggestion("", "", "", code, msg, "low")

        # ── ACCT_MUTUAL_EXCLUSIVITY — both IBAN and Othr present → keep IBAN ──
        if code == "ACCT_MUTUAL_EXCLUSIVITY":
            for _idel in root.iter():
                if not isinstance(_idel.tag, str): continue
                if etree.QName(_idel.tag).localname == "Id":
                    _children_local = [etree.QName(c.tag).localname
                                       for c in _idel if isinstance(c.tag, str)]
                    if "IBAN" in _children_local and "Othr" in _children_local:
                        _orig_id = self._serialize(_idel)
                        _id_copy = self._copy(_idel)
                        for _ch in list(_id_copy):
                            if etree.QName(_ch.tag).localname == "Othr":
                                _id_copy.remove(_ch)
                        return FixSuggestion(self._xpath_of(_idel), _orig_id,
                                              self._serialize(_id_copy), code, msg, "high")

        # ── SCHEME_EMPTY_CD — <Cd> inside scheme is empty ─────────────────────
        if code == "SCHEME_EMPTY_CD":
            for _sel in root.iter():
                if isinstance(_sel.tag, str) and etree.QName(_sel.tag).localname == "Cd":
                    if not (_sel.text or "").strip():
                        _s_copy = self._copy(_sel)
                        _s_copy.text = "CUST"
                        return FixSuggestion(self._xpath_of(_sel), self._serialize(_sel),
                                              self._serialize(_s_copy), code, msg, "low")

        # ── ACCT_TP_EMPTY_CD / CDORPRTRY_EMPTY_CD / SVCLVL_EMPTY_CD ──────────
        # Empty <Cd/> inside a typed container — fill with a sensible default.
        _EMPTY_CD_DEFAULTS = {
            "ACCT_TP_EMPTY_CD":    ("Tp",        "CACC"),
            "CDORPRTRY_EMPTY_CD":  ("CdOrPrtry", "CINV"),
            "SVCLVL_EMPTY_CD":     ("SvcLvl",    "SEPA"),
        }
        if code in _EMPTY_CD_DEFAULTS:
            _parent_tag, _default_val = _EMPTY_CD_DEFAULTS[code]
            for _ecd in root.iter():
                if not isinstance(_ecd.tag, str):
                    continue
                if etree.QName(_ecd.tag).localname != "Cd":
                    continue
                if (_ecd.text or "").strip():
                    continue
                _par = _ecd.getparent()
                if _par is None or not isinstance(_par.tag, str):
                    continue
                if etree.QName(_par.tag).localname != _parent_tag:
                    continue
                _ecd_copy = self._copy(_ecd)
                _ecd_copy.text = _default_val
                return FixSuggestion(self._xpath_of(_ecd), self._serialize(_ecd),
                                      self._serialize(_ecd_copy), code, msg, "high")

        # ── SCHEME_MISSING / SCHEME_MISSING_CHILD — add SchmeNm/Cd ────────────
        if code in ("SCHEME_MISSING", "SCHEME_MISSING_CHILD"):
            for _snel in root.iter():
                if not isinstance(_snel.tag, str): continue
                _ln = etree.QName(_snel.tag).localname
                if _ln in ("SchmeNm", "OrgId", "PrvtId"):
                    _orig_sn = self._serialize(_snel)
                    _sn_copy = self._copy(_snel)
                    _sn_ns = etree.QName(_snel.tag).namespace or ns
                    if _ln == "SchmeNm" and not list(_sn_copy):
                        _cd_el = etree.SubElement(_sn_copy, f"{{{_sn_ns}}}Cd" if _sn_ns else "Cd")
                        _cd_el.text = "CUST"
                        return FixSuggestion(self._xpath_of(_snel), _orig_sn,
                                              self._serialize(_sn_copy), code, msg, "low")

        # ── SCHEME_NOT_ALLOWED — remove Cd/Prtry inside SchmeNm ──────────────
        if code == "SCHEME_NOT_ALLOWED":
            for _snel in root.iter():
                if isinstance(_snel.tag, str) and etree.QName(_snel.tag).localname == "SchmeNm":
                    _orig_sn = self._serialize(_snel)
                    _sn_copy = self._copy(_snel)
                    for _ch in list(_sn_copy):
                        if etree.QName(_ch.tag).localname in ("Cd", "Prtry"):
                            _sn_copy.remove(_ch)
                    if self._serialize(_sn_copy) != _orig_sn:
                        return FixSuggestion(self._xpath_of(_snel), _orig_sn,
                                              self._serialize(_sn_copy), code, msg, "high")

        # ── SCHEME_CONFLICT — both Cd and Prtry present → keep Cd ────────────
        if code == "SCHEME_CONFLICT":
            for _snel in root.iter():
                if isinstance(_snel.tag, str) and etree.QName(_snel.tag).localname == "SchmeNm":
                    _ch_names = [etree.QName(c.tag).localname for c in _snel if isinstance(c.tag, str)]
                    if "Cd" in _ch_names and "Prtry" in _ch_names:
                        _orig_sn = self._serialize(_snel)
                        _sn_copy = self._copy(_snel)
                        for _ch in list(_sn_copy):
                            if etree.QName(_ch.tag).localname == "Prtry":
                                _sn_copy.remove(_ch)
                        return FixSuggestion(self._xpath_of(_snel), _orig_sn,
                                              self._serialize(_sn_copy), code, msg, "high")

        # ── STTLMPRTY_EMPTY / INVALID — fix settlement priority value ─────────
        if code in ("STTLMPRTY_EMPTY", "STTLMPRTY_INVALID", "RTGS_STTLMPRTY_RECOMMENDED"):
            for _spel in root.iter():
                if isinstance(_spel.tag, str) and etree.QName(_spel.tag).localname == "SttlmPrty":
                    _cur = (_spel.text or "").strip()
                    _valid = {"NORM", "HIGH", "URGP"}
                    _target = "NORM" if code != "RTGS_STTLMPRTY_RECOMMENDED" else "HIGH"
                    if _cur not in _valid:
                        _sp_copy = self._copy(_spel)
                        _sp_copy.text = _target
                        return FixSuggestion(self._xpath_of(_spel), self._serialize(_spel),
                                              self._serialize(_sp_copy), code, msg, "high")

        # ── STTLMPRTY_DUPLICATE — remove extra SttlmPrty ──────────────────────
        if code == "STTLMPRTY_DUPLICATE":
            _sp_list = [el for el in root.iter()
                        if isinstance(el.tag, str) and etree.QName(el.tag).localname == "SttlmPrty"]
            if len(_sp_list) >= 2:
                _dup = _sp_list[-1]
                _dup_par = _dup.getparent()
                if _dup_par is not None:
                    _orig_dp = self._serialize(_dup_par)
                    _dp_copy = self._copy(_dup_par)
                    _seen_sp = False
                    for _ch in list(_dp_copy):
                        if etree.QName(_ch.tag).localname == "SttlmPrty":
                            if _seen_sp:
                                _dp_copy.remove(_ch)
                            else:
                                _seen_sp = True
                    return FixSuggestion(self._xpath_of(_dup_par), _orig_dp,
                                          self._serialize(_dp_copy), code, msg, "high")

        # ── STTLMPRTY_WRONG_PARENT / WRONG_POSITION — route to LLM with context
        # (element must be moved; LLM handles structural moves better)

        # ── PACS010_ELEMENT_FORBIDDEN — remove the element not permitted in
        # CBPR+ pacs.010 (deterministic removal, no relocation needed). The
        # offending element's tag is named in the message, e.g. "<UltmtDbtr>".
        if code == "PACS010_ELEMENT_FORBIDDEN":
            _pacs010_forbidden = {"SttlmPrty", "SttlmTmIndctn", "UltmtDbtr"}
            _fb_match = re.search(r"<(\w+)>", msg)
            _fb_target = _fb_match.group(1) if _fb_match else None
            if _fb_target in _pacs010_forbidden:
                for _fbel in root.iter():
                    if isinstance(_fbel.tag, str) and etree.QName(_fbel.tag).localname == _fb_target:
                        _fb_par = _fbel.getparent()
                        if _fb_par is not None:
                            _orig_fb = self._serialize(_fb_par)
                            _fbp_copy = self._copy(_fb_par)
                            for _ch in list(_fbp_copy):
                                if etree.QName(_ch.tag).localname == _fb_target:
                                    _fbp_copy.remove(_ch)
                            return FixSuggestion(self._xpath_of(_fb_par), _orig_fb,
                                                  self._serialize(_fbp_copy), code, msg, "high")

        # ── HEADER_VAL — repair the AppHdr Fr/To party block to the canonical
        # BAH shape <Fr|To><FIId><FinInstnId>…</FinInstnId></FIId></…>. The BAH
        # rejects a stray BICFI directly under FIId, or a FinInstnId/BICFI
        # directly under Fr/To. Rebuild the block around the best FinInstnId
        # found — this PRESERVES that FinInstnId's full content (BICFI, Nm,
        # PstlAdr, ClrSysMmbId, …) and drops only the misplaced duplicates.
        if code == "HEADER_VAL":
            _apphdr = next((e for e in root.iter()
                            if isinstance(e.tag, str)
                            and etree.QName(e.tag).localname == "AppHdr"), None)
            if _apphdr is not None:
                _hns = etree.QName(_apphdr.tag).namespace or ""
                def _hq(tag):
                    return f"{{{_hns}}}{tag}" if _hns else tag
                for _pty_ln in ("Fr", "To"):
                    _pty = _apphdr.find(_hq(_pty_ln))
                    if _pty is None:
                        continue
                    # Pick the FinInstnId to keep: prefer FIId/FinInstnId, else a
                    # FinInstnId placed directly under the party, else wrap the
                    # stray children of FIId (e.g. a bare BICFI) into one.
                    _fiid = _pty.find(_hq("FIId"))
                    _fin = _fiid.find(_hq("FinInstnId")) if _fiid is not None else None
                    if _fin is None and _fiid is not None:
                        _strays = [c for c in _fiid if isinstance(c.tag, str)]
                        if _strays:
                            _fin = etree.Element(_hq("FinInstnId"))
                            for _s in _strays:
                                _fin.append(self._copy(_s))
                    if _fin is None:
                        _fin = _pty.find(_hq("FinInstnId"))
                    if _fin is None:
                        continue
                    # nsmap={None: _hns} keeps the default-namespace style (xmlns="...")
                    # that the original tree uses. Without it, a bare etree.Element()
                    # has no prefix registered and lxml serializes it with an
                    # auto-generated "ns0:" prefix — making this comparison always
                    # mismatch and firing a bogus "fix" for every HEADER_VAL issue,
                    # even ones unrelated to Fr/To (e.g. an empty BizMsgIdr).
                    # Check if FIId contains stray non-whitespace text/tail content
                    # (e.g. ",k0" after </FinInstnId>) — these cause HEADER_VAL
                    # "character content not allowed in element-only type". Detect
                    # before copying because _copy uses etree.fromstring(tostring())
                    # which fails when a tail contains non-whitespace (extra content
                    # after root element). Strip tails from _fin before serialising.
                    _has_stray_text = (
                        (_fin.tail or "").strip() != ""
                        or (_fiid is not None and (_fiid.text or "").strip() != "")
                        or any((_ce.tail or "").strip() != ""
                               for _ce in _fin.iter() if isinstance(_ce.tag, str))
                    )
                    if _has_stray_text:
                        # Serialise with tail suppressed so _copy doesn't choke,
                        # then build a clean copy with all stray tails cleared.
                        _saved_tail = _fin.tail
                        _fin.tail = None
                        try:
                            _fin_copy = self._copy(_fin)
                        finally:
                            _fin.tail = _saved_tail
                        for _ce in _fin_copy.iter():
                            if isinstance(_ce.tag, str) and (_ce.tail or "").strip():
                                _ce.tail = None
                    else:
                        _fin_copy = self._copy(_fin)
                    _new_pty = etree.Element(_pty.tag, nsmap=({None: _hns} if _hns else None))
                    _new_fiid = etree.SubElement(_new_pty, _hq("FIId"))
                    _new_fiid.append(_fin_copy)
                    # Return immediately when we know stray text was present — the
                    # rebuilt block is clean by construction.
                    if _has_stray_text:
                        return FixSuggestion(self._xpath_of(_pty), self._serialize(_pty),
                                              self._serialize(_new_pty), code, msg, "high")
                    # Compare structurally (tag/attrs/text), ignoring whitespace-only
                    # text/tail formatting. _pty is part of the original, already
                    # human-indented document; _new_pty is freshly built with no
                    # source whitespace at all — a raw string/serialize comparison
                    # (even with_tail stripped) differs on indentation alone, so it
                    # ALWAYS looked "changed" and fired a bogus fix for every
                    # HEADER_VAL issue, starving unrelated fixes (e.g. an empty
                    # BizMsgIdr) of ever running.
                    def _struct_eq(a, b):
                        if a.tag != b.tag or dict(a.attrib) != dict(b.attrib):
                            return False
                        if (a.text or "").strip() != (b.text or "").strip():
                            return False
                        a_kids = [c for c in a if isinstance(c.tag, str)]
                        b_kids = [c for c in b if isinstance(c.tag, str)]
                        return len(a_kids) == len(b_kids) and all(
                            _struct_eq(x, y) for x, y in zip(a_kids, b_kids))
                    if not _struct_eq(_pty, _new_pty):
                        return FixSuggestion(self._xpath_of(_pty), self._serialize(_pty),
                                              self._serialize(_new_pty), code, msg, "high")

        # ── CLRSYSREF_FORBIDDEN — remove ClrSysRef element ────────────────────
        if code == "CLRSYSREF_FORBIDDEN":
            for _crel in root.iter():
                if isinstance(_crel.tag, str) and etree.QName(_crel.tag).localname == "ClrSysRef":
                    _cr_par = _crel.getparent()
                    if _cr_par is not None:
                        _orig_cr = self._serialize(_cr_par)
                        _crp_copy = self._copy(_cr_par)
                        for _ch in list(_crp_copy):
                            if etree.QName(_ch.tag).localname == "ClrSysRef":
                                _crp_copy.remove(_ch)
                        return FixSuggestion(self._xpath_of(_cr_par), _orig_cr,
                                              self._serialize(_crp_copy), code, msg, "high")

        # ── CLRSYSREF_EMPTY — add a placeholder reference ─────────────────────
        if code == "CLRSYSREF_EMPTY":
            for _crel in root.iter():
                if isinstance(_crel.tag, str) and etree.QName(_crel.tag).localname == "ClrSysRef":
                    if not (_crel.text or "").strip():
                        _cr_copy = self._copy(_crel)
                        _cr_copy.text = "REF-001"
                        return FixSuggestion(self._xpath_of(_crel), self._serialize(_crel),
                                              self._serialize(_cr_copy), code, msg, "low")

        # ── CLRSYSREF_DUPLICATE — remove second ClrSysRef ─────────────────────
        if code == "CLRSYSREF_DUPLICATE":
            _cr_list = [el for el in root.iter()
                        if isinstance(el.tag, str) and etree.QName(el.tag).localname == "ClrSysRef"]
            if len(_cr_list) >= 2:
                _dup_cr = _cr_list[-1]
                _dup_crp = _dup_cr.getparent()
                if _dup_crp is not None:
                    _orig_crp = self._serialize(_dup_crp)
                    _crp2 = self._copy(_dup_crp)
                    _seen_cr = False
                    for _ch in list(_crp2):
                        if etree.QName(_ch.tag).localname == "ClrSysRef":
                            if _seen_cr:
                                _crp2.remove(_ch)
                            else:
                                _seen_cr = True
                    return FixSuggestion(self._xpath_of(_dup_crp), _orig_crp,
                                          self._serialize(_crp2), code, msg, "high")

        # ── INVALID_PURPOSE_CODE — replace Purp/Cd with valid code ────────────
        if code == "INVALID_PURPOSE_CODE":
            _purpose_codes = _codelist_codes("purpose_code")
            _default_purp = (_purpose_codes[0] if _purpose_codes else "GDDS")
            for _pel in root.iter():
                if not isinstance(_pel.tag, str): continue
                _ln = etree.QName(_pel.tag).localname
                if _ln == "Cd":
                    _par_ln = ""
                    _pp = _pel.getparent()
                    if _pp is not None and isinstance(_pp.tag, str):
                        _par_ln = etree.QName(_pp.tag).localname
                    if _par_ln == "Purp":
                        _pc = (_pel.text or "").strip()
                        if _pc not in (_purpose_codes or ["GDDS"]):
                            _p_copy = self._copy(_pel)
                            _p_copy.text = _default_purp
                            return FixSuggestion(self._xpath_of(_pel), self._serialize(_pel),
                                                  self._serialize(_p_copy), code, msg, "high")

        # ── L3_SVCLVL_CODE — replace invalid SvcLvl/Cd with a valid code ──────
        # Same fix in SR2025 and SR2026 (shared codelist rule). Prefer
        # currency-agnostic service levels; never blind-default to SEPA, which
        # is EUR-only (CBPR_COV_R32).
        if code == "L3_SVCLVL_CODE":
            _svc_codes = _codelist_codes("service_level")
            _svc_default = next(
                (c for c in ("SDVA", "NURG", "G001", "URGP") if c in _svc_codes),
                next((c for c in _svc_codes if c != "SEPA"), "SDVA"))
            for _sel in root.iter():
                if not isinstance(_sel.tag, str):
                    continue
                if etree.QName(_sel.tag).localname != "Cd":
                    continue
                _sp = _sel.getparent()
                _sp_ln = (etree.QName(_sp.tag).localname
                          if _sp is not None and isinstance(_sp.tag, str) else "")
                if _sp_ln != "SvcLvl":
                    continue
                _cur = (_sel.text or "").strip()
                if _svc_codes and _cur in _svc_codes:
                    continue  # already valid — leave it
                _s_copy = self._copy(_sel)
                _s_copy.text = _svc_default
                return FixSuggestion(self._xpath_of(_sel), self._serialize(_sel),
                                      self._serialize(_s_copy), code, msg, "high")

        # ── INVALID_CURRENCY_CODE — fix @Ccy or <Ccy> value ──────────────────
        # SR2026's Layer3Validator emits "INVALID_CURRENCY" (no _CODE suffix);
        # SR2025 emits "INVALID_CURRENCY_CODE" — accept both.
        if code in ("INVALID_CURRENCY_CODE", "INVALID_CURRENCY"):
            _valid_ccys = set(_valid_currency_codes())
            # Try Ccy attribute first
            for _amt_el in root.iter():
                if not isinstance(_amt_el.tag, str): continue
                _ccy_attr = _amt_el.get("Ccy")
                if _ccy_attr and _ccy_attr not in _valid_ccys:
                    _orig_amt = self._serialize(_amt_el)
                    _amt_copy = self._copy(_amt_el)
                    _amt_copy.set("Ccy", "USD")
                    return FixSuggestion(self._xpath_of(_amt_el), _orig_amt,
                                          self._serialize(_amt_copy), code, msg, "high")
            # Try <Ccy> leaf
            for _cel in root.iter():
                if isinstance(_cel.tag, str) and etree.QName(_cel.tag).localname == "Ccy":
                    if not (_cel.text or "").strip() in _valid_ccys:
                        _c_copy = self._copy(_cel)
                        _c_copy.text = "USD"
                        return FixSuggestion(self._xpath_of(_cel), self._serialize(_cel),
                                              self._serialize(_c_copy), code, msg, "high")

        # ── INVALID_DECIMAL_PRECISION — fix decimal places on amount ──────────
        if code == "INVALID_DECIMAL_PRECISION":
            for _dp_el in root.iter():
                if not isinstance(_dp_el.tag, str): continue
                _ln = etree.QName(_dp_el.tag).localname
                if _ln.endswith("Amt") or _ln == "Amt":
                    _raw_amt = (_dp_el.text or "").strip()
                    _ccy = _dp_el.get("Ccy", "USD")
                    _prec = _ccy_precision(_ccy)
                    # Match too many decimal places OR multiple decimal points
                    if re.match(r"^\d+\.\d{3,}$", _raw_amt) or _raw_amt.count(".") > 1:
                        try:
                            _fixed_amt = f"{float(_raw_amt):.{_prec}f}"
                        except ValueError:
                            # Multiple decimal points (e.g. "4000.098.09872635") —
                            # recover the leading integer: 4000 → "4000.00"
                            _int_m = re.match(r'^(\d+)', _raw_amt)
                            _fixed_amt = (f"{int(_int_m.group(1)):.{_prec}f}"
                                          if _int_m else None)
                        if _fixed_amt and _fixed_amt != _raw_amt:
                            _dp_copy = self._copy(_dp_el)
                            _dp_copy.text = _fixed_amt
                            return FixSuggestion(self._xpath_of(_dp_el), self._serialize(_dp_el),
                                                  self._serialize(_dp_copy), code, msg, "high")

        # ── NON_POSITIVE_AMOUNT / INVALID_AMOUNT —————————————————————————————————
        # IMPORTANT: NEVER replace an existing amount with 0.01 or any other dummy.
        # For value errors (non-positive): preserve absolute value or fall back to
        # the KB default (1000.00).  For format errors (bad decimals, comma
        # separator): fix the format while keeping the numeric value.
        if code in ("NON_POSITIVE_AMOUNT", "INVALID_AMOUNT", "PACS004_AMT_NEGATIVE",
                    "PACS004_AMT_NOT_NUMERIC", "PACS004_AMT_LEN"):
            _amt_default = _kb_get("dummy_data.amounts.default") or "1000.00"
            for _ael in root.iter():
                if not isinstance(_ael.tag, str): continue
                _ln = etree.QName(_ael.tag).localname
                if (_ln.endswith("Amt") or _ln == "Amt") and not list(_ael):
                    _raw = (_ael.text or "").strip()
                    _ccy = _ael.get("Ccy", "USD")
                    _prec = _ccy_precision(_ccy)
                    _is_bad = False
                    _new_val = None
                    try:
                        # Normalize: European comma decimal separator → period
                        _norm = _raw.replace(",", ".")
                        _v = float(_norm)
                        if code in ("NON_POSITIVE_AMOUNT", "PACS004_AMT_NEGATIVE"):
                            # Value-only check — format may be perfectly valid
                            if _v <= 0:
                                _is_bad = True
                                _new_val = (f"{abs(_v):.{_prec}f}"
                                            if abs(_v) > 0 else _amt_default)
                        else:
                            # Format check only for format-related error codes
                            _valid_patt = rf"^\d+(\.(\d{{1,{max(_prec, 5)}}}))?$"
                            if not re.match(_valid_patt, _norm):
                                _is_bad = True
                                _new_val = (f"{abs(_v):.{_prec}f}"
                                            if _v != 0 else _amt_default)
                            elif _prec >= 0 and re.match(
                                    rf"^\d+\.\d{{{_prec + 1},}}$", _norm):
                                # Too many decimal places — round to currency precision
                                _is_bad = True
                                _new_val = f"{_v:.{_prec}f}"
                    except (ValueError, TypeError):
                        _is_bad = True
                        # float() failed — value has multiple decimal points or other
                        # garbage (e.g. "4000.098.09872635").  Recover the leading
                        # integer so we return "4000.00" not the unrelated "1000.00".
                        _int_m = re.match(r'^(\d+)', _raw.replace(",", "."))
                        if _int_m:
                            _leading = int(_int_m.group(1))
                            _new_val = f"{_leading:.{_prec}f}"
                        else:
                            _new_val = None  # no digits at all — truly unparseable
                    if _is_bad:
                        _ae_copy = self._copy(_ael)
                        # Never replace with 1000.00/0.01 when the original value
                        # contains a recoverable number — preserve it.
                        _ae_copy.text = _new_val or _amt_default
                        _conf = "high" if _new_val else "medium"
                        return FixSuggestion(self._xpath_of(_ael), self._serialize(_ael),
                                              self._serialize(_ae_copy), code, msg, _conf)

        # ── FUTURE_DATE_BIRTH_ERROR — birth date in future → past date ────────
        if code == "FUTURE_DATE_BIRTH_ERROR":
            for _bel in root.iter():
                if isinstance(_bel.tag, str) and etree.QName(_bel.tag).localname == "BirthDt":
                    _b_copy = self._copy(_bel)
                    _b_copy.text = "1980-01-01"
                    return FixSuggestion(self._xpath_of(_bel), self._serialize(_bel),
                                          self._serialize(_b_copy), code, msg, "high")

        # ── Network-specific currency errors — replace with correct currency ───
        _NETWORK_CCY_MAP = {
            "CHAPS_CURRENCY_ERROR": "GBP",
            "CHIPS_CURRENCY_ERROR": "USD",
            "FED_CURRENCY_ERROR":   "USD",
            "T2_CURRENCY_ERROR":    "EUR",
        }
        if code in _NETWORK_CCY_MAP:
            _target_ccy = _NETWORK_CCY_MAP[code]
            for _nce in root.iter():
                if not isinstance(_nce.tag, str): continue
                _ccy_attr = _nce.get("Ccy")
                if _ccy_attr and _ccy_attr != _target_ccy:
                    _orig_nce = self._serialize(_nce)
                    _nce_copy = self._copy(_nce)
                    _nce_copy.set("Ccy", _target_ccy)
                    return FixSuggestion(self._xpath_of(_nce), _orig_nce,
                                          self._serialize(_nce_copy), code, msg, "high")

        # ── CHRG_CCY_MISMATCH — ChrgsInf Amt currency ≠ settlement currency ──
        if code == "CHRG_CCY_MISMATCH":
            # Harvest settlement currency from IntrBkSttlmAmt
            _sttlm_ccy = None
            for _sc_el in root.iter():
                if isinstance(_sc_el.tag, str):
                    _ln = etree.QName(_sc_el.tag).localname
                    if _ln in ("IntrBkSttlmAmt", "InstdAmt", "TtlIntrBkSttlmAmt"):
                        _sttlm_ccy = _sc_el.get("Ccy")
                        if _sttlm_ccy:
                            break
            if _sttlm_ccy:
                for _chg_el in root.iter():
                    if isinstance(_chg_el.tag, str) and etree.QName(_chg_el.tag).localname == "Amt":
                        _par = _chg_el.getparent()
                        if _par is not None and etree.QName(_par.tag).localname == "ChrgsInf":
                            if _chg_el.get("Ccy") and _chg_el.get("Ccy") != _sttlm_ccy:
                                _orig_chg = self._serialize(_chg_el)
                                _chg_copy = self._copy(_chg_el)
                                _chg_copy.set("Ccy", _sttlm_ccy)
                                return FixSuggestion(self._xpath_of(_chg_el), _orig_chg,
                                                      self._serialize(_chg_copy), code, msg, "high")

        # ── PACS004 specific handlers ─────────────────────────────────────────

        # PACS004_CHRGBR_ENUM — invalid ChrgBr → SLEV
        if code == "PACS004_CHRGBR_ENUM":
            for _cb_el in root.iter():
                if isinstance(_cb_el.tag, str) and etree.QName(_cb_el.tag).localname == "ChrgBr":
                    _cb_copy = self._copy(_cb_el)
                    _cb_copy.text = "SLEV"
                    return FixSuggestion(self._xpath_of(_cb_el), self._serialize(_cb_el),
                                          self._serialize(_cb_copy), code, msg, "high")

        # PACS004_STTLMMTD_ENUM — invalid SttlmMtd → INDA
        if code == "PACS004_STTLMMTD_ENUM":
            for _sm_el in root.iter():
                if isinstance(_sm_el.tag, str) and etree.QName(_sm_el.tag).localname == "SttlmMtd":
                    _sm_copy = self._copy(_sm_el)
                    _sm_copy.text = "INDA"
                    return FixSuggestion(self._xpath_of(_sm_el), self._serialize(_sm_el),
                                          self._serialize(_sm_copy), code, msg, "high")

        # PACS004_PMTMETHOD_ENUM — invalid PmtMtd → CHK
        if code == "PACS004_PMTMETHOD_ENUM":
            for _pm_el in root.iter():
                if isinstance(_pm_el.tag, str) and etree.QName(_pm_el.tag).localname == "PmtMtd":
                    _pm_copy = self._copy(_pm_el)
                    _pm_copy.text = "CHK"
                    return FixSuggestion(self._xpath_of(_pm_el), self._serialize(_pm_el),
                                          self._serialize(_pm_copy), code, msg, "high")

        # PACS004_REFMTD_ENUM — invalid RefMtd → SCOR
        if code == "PACS004_REFMTD_ENUM":
            for _rm_el in root.iter():
                if isinstance(_rm_el.tag, str) and etree.QName(_rm_el.tag).localname == "RefMtd":
                    _rm_copy = self._copy(_rm_el)
                    _rm_copy.text = "SCOR"
                    return FixSuggestion(self._xpath_of(_rm_el), self._serialize(_rm_el),
                                          self._serialize(_rm_copy), code, msg, "high")

        # PACS004_NBTXS_INVALID — NbOfTxs mismatch for pacs.004
        if code == "PACS004_NBTXS_INVALID":
            _p4_fix = self._fix_nb_of_txs(root, code, msg)
            if _p4_fix is not None:
                return _p4_fix

        # PACS004_MSGID_INVALID — MsgId has forbidden chars → strip
        if code == "PACS004_MSGID_INVALID":
            _CBPR_ALLOWED_RE = re.compile(r"[^A-Za-z0-9 /\-?:().,'+]")
            for _mi_el in root.iter():
                if isinstance(_mi_el.tag, str) and etree.QName(_mi_el.tag).localname == "MsgId":
                    if _mi_el.text:
                        _cl = _CBPR_ALLOWED_RE.sub("", _mi_el.text.strip())
                        if _cl != _mi_el.text.strip():
                            _mi_copy = self._copy(_mi_el)
                            _mi_copy.text = _cl[:35] or "MSG-001"
                            return FixSuggestion(self._xpath_of(_mi_el), self._serialize(_mi_el),
                                                  self._serialize(_mi_copy), code, msg, "high")

        # PACS004_AGENT_BIC_MANDATORY / RULE_1A_BIC / RULE_1B_BIC — add BICFI
        if code in ("PACS004_AGENT_BIC_MANDATORY", "PACS004_AGENT_RULE_1A_BIC",
                    "PACS004_PARTY_RULE_1B_BIC", "PACS004_AGENT_RULE_1A_NOBIC"):
            for _fii in root.iter():
                if isinstance(_fii.tag, str) and etree.QName(_fii.tag).localname == "FinInstnId":
                    _has_bic = any(etree.QName(c.tag).localname == "BICFI"
                                   for c in _fii if isinstance(c.tag, str))
                    if not _has_bic:
                        _orig_fii = self._serialize(_fii)
                        _fii_copy = self._copy(_fii)
                        _bic_ns = etree.QName(_fii.tag).namespace or ns
                        _bic_el = etree.SubElement(_fii_copy,
                            f"{{{_bic_ns}}}BICFI" if _bic_ns else "BICFI")
                        _bic_el.text = "DEUTDEFFXXX"
                        return FixSuggestion(self._xpath_of(_fii), _orig_fii,
                                              self._serialize(_fii_copy), code, msg, "low")

        # PACS004_GH_MISSING — missing GrpHdr → add skeleton
        if code == "PACS004_GH_MISSING":
            _doc_el = root.find(".//{*}Document")
            if _doc_el is None:
                _doc_el = root
            _doc_local = etree.QName(_doc_el.tag).localname
            if _doc_local == "Document":
                for _body_el in list(_doc_el):
                    if isinstance(_body_el.tag, str):
                        _has_gh = any(etree.QName(c.tag).localname == "GrpHdr"
                                      for c in _body_el if isinstance(c.tag, str))
                        if not _has_gh:
                            _orig_b = self._serialize(_body_el)
                            _b_ns = etree.QName(_body_el.tag).namespace or ns
                            _b_copy = self._copy(_body_el)
                            _gh_tmpl = (
                                f'<GrpHdr><MsgId>MSG-001</MsgId>'
                                f'<CreDtTm>2026-01-15T10:00:00+00:00</CreDtTm>'
                                f'<NbOfTxs>1</NbOfTxs>'
                                f'<SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf></GrpHdr>'
                            )
                            try:
                                _gh_el = etree.fromstring(_gh_tmpl.encode("utf-8"))
                                _b_copy.insert(0, _gh_el)
                                return FixSuggestion(self._xpath_of(_body_el), _orig_b,
                                                      self._serialize(_b_copy), code, msg, "low")
                            except Exception:
                                pass

        # PACS004_STTLMINF_MISSING — missing SttlmInf in GrpHdr
        if code == "PACS004_STTLMINF_MISSING":
            for _gh_el in root.iter():
                if isinstance(_gh_el.tag, str) and etree.QName(_gh_el.tag).localname == "GrpHdr":
                    _has_si = any(etree.QName(c.tag).localname == "SttlmInf"
                                  for c in _gh_el if isinstance(c.tag, str))
                    if not _has_si:
                        _orig_gh = self._serialize(_gh_el)
                        _gh_copy = self._copy(_gh_el)
                        _gh_ns = etree.QName(_gh_el.tag).namespace or ns
                        _si_el = etree.SubElement(_gh_copy,
                            f"{{{_gh_ns}}}SttlmInf" if _gh_ns else "SttlmInf")
                        _sm = etree.SubElement(_si_el,
                            f"{{{_gh_ns}}}SttlmMtd" if _gh_ns else "SttlmMtd")
                        _sm.text = "INDA"
                        return FixSuggestion(self._xpath_of(_gh_el), _orig_gh,
                                              self._serialize(_gh_copy), code, msg, "high")

        # PACS004_RSNCD_MISSING — missing return reason code → add CUST
        if code == "PACS004_RSNCD_MISSING":
            for _rsn_el in root.iter():
                if isinstance(_rsn_el.tag, str) and etree.QName(_rsn_el.tag).localname == "Rsn":
                    _has_cd = any(etree.QName(c.tag).localname in ("Cd", "Prtry")
                                  for c in _rsn_el if isinstance(c.tag, str))
                    if not _has_cd:
                        _orig_rsn = self._serialize(_rsn_el)
                        _rsn_copy = self._copy(_rsn_el)
                        _rsn_ns = etree.QName(_rsn_el.tag).namespace or ns
                        _cd_el = etree.SubElement(_rsn_copy,
                            f"{{{_rsn_ns}}}Cd" if _rsn_ns else "Cd")
                        _cd_el.text = "CUST"
                        return FixSuggestion(self._xpath_of(_rsn_el), _orig_rsn,
                                              self._serialize(_rsn_copy), code, msg, "high")

        # PACS004_RTRCHAIN_CDTR_MISSING / DBTR_MISSING — add Cdtr/Dbtr skeleton
        if code in ("PACS004_RTRCHAIN_CDTR_MISSING", "PACS004_RTRCHAIN_DBTR_MISSING"):
            _missing_tag = "Cdtr" if "CDTR" in code else "Dbtr"
            _CHAIN_PARENTS = {"RtrChain", "TxInf"}
            for _rc_el in root.iter():
                if not isinstance(_rc_el.tag, str): continue
                if etree.QName(_rc_el.tag).localname in _CHAIN_PARENTS:
                    _has_tgt = any(etree.QName(c.tag).localname == _missing_tag
                                   for c in _rc_el if isinstance(c.tag, str))
                    if not _has_tgt:
                        _orig_rc = self._serialize(_rc_el)
                        _rc_copy = self._copy(_rc_el)
                        _rc_ns = etree.QName(_rc_el.tag).namespace or ns
                        _pt_el = etree.SubElement(_rc_copy,
                            f"{{{_rc_ns}}}{_missing_tag}" if _rc_ns else _missing_tag)
                        _fi_el = etree.SubElement(_pt_el,
                            f"{{{_rc_ns}}}FinInstnId" if _rc_ns else "FinInstnId")
                        _bic_el = etree.SubElement(_fi_el,
                            f"{{{_rc_ns}}}BICFI" if _rc_ns else "BICFI")
                        _bic_el.text = "DEUTDEFFXXX"
                        return FixSuggestion(self._xpath_of(_rc_el), _orig_rc,
                                              self._serialize(_rc_copy), code, msg, "low")

        # ── PACS004_XchgRate fraction / length ────────────────────────────────
        if code in ("PACS004_XRATE_FRAC", "PACS004_XRATE_LEN"):
            for _xr_el in root.iter():
                if isinstance(_xr_el.tag, str) and etree.QName(_xr_el.tag).localname == "XchgRate":
                    _raw_xr = (_xr_el.text or "").strip()
                    try:
                        _fixed_xr = f"{float(_raw_xr):.5f}"
                        if code == "PACS004_XRATE_LEN":
                            _fixed_xr = _fixed_xr[:11]
                        if _fixed_xr != _raw_xr:
                            _xr_copy = self._copy(_xr_el)
                            _xr_copy.text = _fixed_xr
                            return FixSuggestion(self._xpath_of(_xr_el), self._serialize(_xr_el),
                                                  self._serialize(_xr_copy), code, msg, "high")
                    except (ValueError, TypeError):
                        pass

        # ── PACS004_CREDT_INVALID — invalid Ccy on CtrlSum/IntrBkSttlmAmt ─────
        if code == "PACS004_CREDT_INVALID":
            for _cdt in root.iter():
                if not isinstance(_cdt.tag, str): continue
                if etree.QName(_cdt.tag).localname == "CreDtTm":
                    from datetime import datetime as _dt, timezone as _tz
                    _cdt_copy = self._copy(_cdt)
                    _cdt_copy.text = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    return FixSuggestion(self._xpath_of(_cdt), self._serialize(_cdt),
                                          self._serialize(_cdt_copy), code, msg, "high")

        # ── PAIN008_FWDGAGT_MANDATORY — add FwdgAgt to pacs.008/pain.008 ──────
        if code == "PAIN008_FWDGAGT_MANDATORY":
            for _tx_el in root.iter():
                if not isinstance(_tx_el.tag, str): continue
                _ln = etree.QName(_tx_el.tag).localname
                if _ln in ("DrctDbtTxInf", "CdtTrfTxInf"):
                    _has_fwdg = any(etree.QName(c.tag).localname == "FwdgAgt"
                                    for c in _tx_el if isinstance(c.tag, str))
                    if not _has_fwdg:
                        _orig_tx = self._serialize(_tx_el)
                        _tx_copy = self._copy(_tx_el)
                        _tx_ns = etree.QName(_tx_el.tag).namespace or ns
                        _fa_el = etree.SubElement(_tx_copy,
                            f"{{{_tx_ns}}}FwdgAgt" if _tx_ns else "FwdgAgt")
                        _fi2 = etree.SubElement(_fa_el,
                            f"{{{_tx_ns}}}FinInstnId" if _tx_ns else "FinInstnId")
                        _bic2 = etree.SubElement(_fi2,
                            f"{{{_tx_ns}}}BICFI" if _tx_ns else "BICFI")
                        _bic2.text = "DEUTDEFFXXX"
                        return FixSuggestion(self._xpath_of(_tx_el), _orig_tx,
                                              self._serialize(_tx_copy), code, msg, "low")

        # ── PAIN002_ORGNLPMT_NO_TXINF — add TxInfAndSts to OrgnlPmtInfAndSts ───
        if code == "PAIN002_ORGNLPMT_NO_TXINF":
            for _el in root.iter():
                if not isinstance(_el.tag, str):
                    continue
                if etree.QName(_el.tag).localname != "OrgnlPmtInfAndSts":
                    continue
                _has_txinf = any(
                    isinstance(c.tag, str) and etree.QName(c.tag).localname == "TxInfAndSts"
                    for c in _el
                )
                if not _has_txinf:
                    _orig = self._serialize(_el)
                    _copy = self._copy(_el)
                    _ons = etree.QName(_el.tag).namespace or ns
                    def _mk2(tag, parent, text=None):
                        _e = etree.SubElement(
                            parent,
                            f"{{{_ons}}}{tag}" if _ons else tag
                        )
                        if text is not None:
                            _e.text = text
                        return _e
                    _txi = _mk2("TxInfAndSts", _copy)
                    _mk2("OrgnlEndToEndId", _txi, "NOTPROVIDED")
                    _mk2("TxSts", _txi, "ACSP")
                    return FixSuggestion(self._xpath_of(_el), _orig,
                                          self._serialize(_copy), code, msg, "low")

        # ── CAMT053_LASTPGIND_CLBD_MISSING — add CLBD closing balance ───────────
        if code == "CAMT053_LASTPGIND_CLBD_MISSING":
            for _stmt in root.iter():
                if not isinstance(_stmt.tag, str): continue
                if etree.QName(_stmt.tag).localname != 'Stmt': continue
                _stmt_ns = etree.QName(_stmt.tag).namespace or ns
                def _mk(tag, parent=None, text=None):
                    el = etree.SubElement(
                        parent if parent is not None else _stmt,
                        f"{{{_stmt_ns}}}{tag}" if _stmt_ns else tag
                    )
                    if text is not None:
                        el.text = text
                    return el
                _orig_stmt = self._serialize(_stmt)
                _stmt_copy = self._copy(_stmt)
                # Build <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp>
                # <CdtDbtInd>CRDT</CdtDbtInd><Amt Ccy="EUR">0.00</Amt></Bal>
                _bal = etree.SubElement(
                    _stmt_copy,
                    f"{{{_stmt_ns}}}Bal" if _stmt_ns else "Bal"
                )
                _tp  = etree.SubElement(_bal,  f"{{{_stmt_ns}}}Tp"  if _stmt_ns else "Tp")
                _cop = etree.SubElement(_tp,   f"{{{_stmt_ns}}}CdOrPrtry" if _stmt_ns else "CdOrPrtry")
                _cd  = etree.SubElement(_cop,  f"{{{_stmt_ns}}}Cd"  if _stmt_ns else "Cd")
                _cd.text = "CLBD"
                _cdi = etree.SubElement(_bal,  f"{{{_stmt_ns}}}CdtDbtInd" if _stmt_ns else "CdtDbtInd")
                _cdi.text = "CRDT"
                _amt = etree.SubElement(_bal,  f"{{{_stmt_ns}}}Amt" if _stmt_ns else "Amt")
                _amt.set("Ccy", "EUR")
                _amt.text = "0.00"
                return FixSuggestion(self._xpath_of(_stmt), _orig_stmt,
                                      self._serialize(_stmt_copy), code, msg, "low")

        # ── CAMT053_LASTPGIND_INTM_SUBTYPE — remove INTM subtype ─────────────
        if code == "CAMT053_LASTPGIND_INTM_SUBTYPE":
            for _sub in root.iter():
                if not isinstance(_sub.tag, str): continue
                if etree.QName(_sub.tag).localname != 'Cd': continue
                if (_sub.text or '').strip() != 'INTM': continue
                _subtype_par = _sub.getparent()
                if _subtype_par is None or etree.QName(_subtype_par.tag).localname != 'SubType': continue
                _tp_par = _subtype_par.getparent()
                if _tp_par is None or etree.QName(_tp_par.tag).localname != 'Tp': continue
                _tp_copy = self._copy(_tp_par)
                # Remove SubType from the copy
                for _ch in list(_tp_copy):
                    if isinstance(_ch.tag, str) and etree.QName(_ch.tag).localname == 'SubType':
                        _tp_copy.remove(_ch)
                return FixSuggestion(self._xpath_of(_tp_par), self._serialize(_tp_par),
                                      self._serialize(_tp_copy), code, msg, "high")

        # ── PACS009_POSTAL_ADDRESS — incomplete postal address ────────────────
        if code == "PACS009_POSTAL_ADDRESS":
            for _pa_el in root.iter():
                if isinstance(_pa_el.tag, str) and etree.QName(_pa_el.tag).localname == "PstlAdr":
                    _pa_chs = {etree.QName(c.tag).localname for c in _pa_el if isinstance(c.tag, str)}
                    _needs = {"AdrLine", "Ctry"} - _pa_chs
                    if _needs:
                        _orig_pa = self._serialize(_pa_el)
                        _pa_copy = self._copy(_pa_el)
                        _pa_ns = etree.QName(_pa_el.tag).namespace or ns
                        if "AdrLine" in _needs:
                            _al = etree.SubElement(_pa_copy,
                                f"{{{_pa_ns}}}AdrLine" if _pa_ns else "AdrLine")
                            _al.text = "123 Main Street"
                        if "Ctry" in _needs:
                            _ct = etree.SubElement(_pa_copy,
                                f"{{{_pa_ns}}}Ctry" if _pa_ns else "Ctry")
                            _ct.text = "US"
                        return FixSuggestion(self._xpath_of(_pa_el), _orig_pa,
                                              self._serialize(_pa_copy), code, msg, "low")

        # ── TIME_PARSE — invalid time value → replace with valid time ─────────
        if code == "TIME_PARSE":
            for _tel in root.iter():
                if not isinstance(_tel.tag, str): continue
                _ln = etree.QName(_tel.tag).localname
                if _ln.endswith(("Tm", "DtTm")) and _tel.text:
                    from datetime import datetime as _dt, timezone as _tz
                    _t_copy = self._copy(_tel)
                    _t_copy.text = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    return FixSuggestion(self._xpath_of(_tel), self._serialize(_tel),
                                          self._serialize(_t_copy), code, msg, "high")

        # ── BBAN_VALIDATION_ERROR / SEPA_BBAN_NOT_ALLOWED — replace with IBAN ─
        if code in ("BBAN_VALIDATION_ERROR", "SEPA_BBAN_NOT_ALLOWED"):
            for _bel in root.iter():
                if not isinstance(_bel.tag, str): continue
                _ln = etree.QName(_bel.tag).localname
                if _ln == "BBAN":
                    _b_par = _bel.getparent()
                    if _b_par is not None and etree.QName(_b_par.tag).localname == "Othr":
                        # Replace Othr element with IBAN
                        _b_gp = _b_par.getparent()
                        if _b_gp is not None and etree.QName(_b_gp.tag).localname == "Id":
                            _orig_gp = self._serialize(_b_gp)
                            _gp_copy = self._copy(_b_gp)
                            _gp_ns = etree.QName(_b_gp.tag).namespace or ns
                            # Remove Othr, add IBAN
                            for _ch in list(_gp_copy):
                                if etree.QName(_ch.tag).localname == "Othr":
                                    _gp_copy.remove(_ch)
                            _iban_el = etree.SubElement(_gp_copy,
                                f"{{{_gp_ns}}}IBAN" if _gp_ns else "IBAN")
                            _iban_el.text = _iban_for_ccy(root, _bel)
                            return FixSuggestion(self._xpath_of(_b_gp), _orig_gp,
                                                  self._serialize(_gp_copy), code, msg, "low")

        # ── MISSING_ATTRIBUTE — add required @Ccy attribute ───────────────────
        if code == "MISSING_ATTRIBUTE":
            _attr_m = re.search(r"attribute '?(\w+)'?", msg, re.I)
            _attr_name = _attr_m.group(1) if _attr_m else "Ccy"
            for _amt_el in root.iter():
                if not isinstance(_amt_el.tag, str): continue
                _ln = etree.QName(_amt_el.tag).localname
                if (_ln.endswith("Amt") or _ln == "Amt") and _attr_name == "Ccy":
                    if _amt_el.get("Ccy") is None:
                        _orig_ma = self._serialize(_amt_el)
                        _ma_copy = self._copy(_amt_el)
                        _ma_copy.set("Ccy", "USD")
                        return FixSuggestion(self._xpath_of(_amt_el), _orig_ma,
                                              self._serialize(_ma_copy), code, msg, "high")

        # ── INVALID_IBAN_CTRY / IBAN_VALIDATION_ERROR — replace with valid IBAN
        # whose country matches the transaction currency (via _iban_for_ccy),
        # not a hardcoded GB IBAN — a fixed GB replacement turns e.g. a SEK
        # transaction's IBAN into a GBP-country IBAN, which just trades
        # INVALID_IBAN_CTRY for a fresh CURR_IBAN_MISMATCH.
        if code in ("INVALID_IBAN_CTRY", "IBAN_VALIDATION_ERROR"):
            for _ibel in root.iter():
                if isinstance(_ibel.tag, str) and etree.QName(_ibel.tag).localname == "IBAN":
                    _ib_copy = self._copy(_ibel)
                    _ib_copy.text = _iban_for_ccy(root, _ibel)
                    return FixSuggestion(self._xpath_of(_ibel), self._serialize(_ibel),
                                          self._serialize(_ib_copy), code, msg, "high")

        # ── PARTY_ID_DUAL — both BICFI and Nm/Othr present in FinInstnId ──────
        if code == "PARTY_ID_DUAL":
            for _fii in root.iter():
                if isinstance(_fii.tag, str) and etree.QName(_fii.tag).localname == "FinInstnId":
                    _ch_names = [etree.QName(c.tag).localname for c in _fii if isinstance(c.tag, str)]
                    if "BICFI" in _ch_names and ("Nm" in _ch_names or "Othr" in _ch_names):
                        _orig_fii = self._serialize(_fii)
                        _fii_copy = self._copy(_fii)
                        for _ch in list(_fii_copy):
                            if etree.QName(_ch.tag).localname in ("Nm", "Othr"):
                                _fii_copy.remove(_ch)
                        if self._serialize(_fii_copy) != _orig_fii:
                            return FixSuggestion(self._xpath_of(_fii), _orig_fii,
                                                  self._serialize(_fii_copy), code, msg, "high")

        # ── PARTY_NO_ID_OR_ADDR — party has neither identification nor address ─
        if code == "PARTY_NO_ID_OR_ADDR":
            for _pt_el in root.iter():
                if not isinstance(_pt_el.tag, str): continue
                _ln = etree.QName(_pt_el.tag).localname
                if _ln in ("Dbtr", "Cdtr", "UltmtDbtr", "UltmtCdtr", "InitgPty"):
                    _has_id = any(etree.QName(c.tag).localname in ("Id", "FinInstnId")
                                  for c in _pt_el if isinstance(c.tag, str))
                    _has_adr = any(etree.QName(c.tag).localname == "PstlAdr"
                                   for c in _pt_el if isinstance(c.tag, str))
                    if not _has_id and not _has_adr:
                        _orig_pt = self._serialize(_pt_el)
                        _pt_copy = self._copy(_pt_el)
                        _pt_ns = etree.QName(_pt_el.tag).namespace or ns
                        _pa2 = etree.SubElement(_pt_copy,
                            f"{{{_pt_ns}}}PstlAdr" if _pt_ns else "PstlAdr")
                        _al2 = etree.SubElement(_pa2, f"{{{_pt_ns}}}AdrLine" if _pt_ns else "AdrLine")
                        _al2.text = "123 Main Street"
                        _ct2 = etree.SubElement(_pa2, f"{{{_pt_ns}}}Ctry" if _pt_ns else "Ctry")
                        _ct2.text = "US"
                        return FixSuggestion(self._xpath_of(_pt_el), _orig_pt,
                                              self._serialize(_pt_copy), code, msg, "low")

        # ── PACS004_AMT_FRAC — too many decimal places on amount ──────────────
        if code == "PACS004_AMT_FRAC":
            for _af_el in root.iter():
                if not isinstance(_af_el.tag, str): continue
                if etree.QName(_af_el.tag).localname in ("Amt", "IntrBkSttlmAmt", "InstdAmt"):
                    _raw = (_af_el.text or "").strip()
                    if re.search(r"\.\d{3,}", _raw) or _raw.count(".") > 1:
                        _af_ccy = _af_el.get("Ccy", "USD")
                        _af_prec = _ccy_precision(_af_ccy)
                        try:
                            _af_fixed = f"{float(_raw):.{_af_prec}f}"
                        except (ValueError, TypeError):
                            # Multiple decimal points (e.g. "4000.098.09872635") —
                            # recover the leading integer: 4000 → "4000.00"
                            _af_int_m = re.match(r'^(\d+)', _raw)
                            _af_fixed = (f"{int(_af_int_m.group(1)):.{_af_prec}f}"
                                         if _af_int_m else None)
                        if _af_fixed:
                            _af_copy = self._copy(_af_el)
                            _af_copy.text = _af_fixed
                            return FixSuggestion(self._xpath_of(_af_el), self._serialize(_af_el),
                                                  self._serialize(_af_copy), code, msg, "high")

        # ── PACS004_ADDINF_LEN — AddtlInf too long → truncate to 140 ─────────
        if code == "PACS004_ADDINF_LEN":
            for _ai_el in root.iter():
                if isinstance(_ai_el.tag, str) and etree.QName(_ai_el.tag).localname == "AddtlInf":
                    if _ai_el.text and len(_ai_el.text.strip()) > 140:
                        _ai_copy = self._copy(_ai_el)
                        _ai_copy.text = _ai_el.text.strip()[:140]
                        return FixSuggestion(self._xpath_of(_ai_el), self._serialize(_ai_el),
                                              self._serialize(_ai_copy), code, msg, "high")

        # ── PACS004_CLRSYS_LEN — ClrSys code too long → truncate ─────────────
        if code == "PACS004_CLRSYS_LEN":
            for _cs_el in root.iter():
                if isinstance(_cs_el.tag, str) and etree.QName(_cs_el.tag).localname == "Cd":
                    _par_cs = _cs_el.getparent()
                    if _par_cs is not None and etree.QName(_par_cs.tag).localname in ("ClrSys", "ClrSysId"):
                        if _cs_el.text and len(_cs_el.text.strip()) > 5:
                            _cs_copy = self._copy(_cs_el)
                            _cs_copy.text = _cs_el.text.strip()[:5]
                            return FixSuggestion(self._xpath_of(_cs_el), self._serialize(_cs_el),
                                                  self._serialize(_cs_copy), code, msg, "high")

        # ── GLOBAL-RMT-001 / CBPR_R34 — RmtInf Strd and Ustrd are mutually
        # exclusive in CBPR+. Keep Ustrd (the form CBPR+ prefers and the one
        # the generators emit) and remove Strd — mirrors the XSD-driven fix
        # already applied for message types (e.g. pacs.009) whose schema
        # forbids Strd outright.
        if code in ("GLOBAL-RMT-001", "CBPR_R34"):
            for _rmt_el in root.iter():
                if not isinstance(_rmt_el.tag, str):
                    continue
                if etree.QName(_rmt_el.tag).localname != "RmtInf":
                    continue
                _children_local = [etree.QName(c.tag).localname
                                    for c in _rmt_el if isinstance(c.tag, str)]
                if "Strd" in _children_local and "Ustrd" in _children_local:
                    _orig_rmt = self._serialize(_rmt_el)
                    _rmt_copy = self._copy(_rmt_el)
                    for _ch in list(_rmt_copy):
                        if etree.QName(_ch.tag).localname == "Strd":
                            _rmt_copy.remove(_ch)
                    return FixSuggestion(self._xpath_of(_rmt_el), _orig_rmt,
                                          self._serialize(_rmt_copy), code, msg, "high")

        # ── DTD_FORBIDDEN / ENTITY_FORBIDDEN — strip DTD/entity declarations ──
        if code in ("DTD_FORBIDDEN", "ENTITY_FORBIDDEN"):
            _cleaned_xml = re.sub(r"<!DOCTYPE[^>]*>", "", xml, flags=re.I | re.S).strip()
            _cleaned_xml = re.sub(r"<!ENTITY[^>]*>", "", _cleaned_xml, flags=re.I | re.S).strip()
            if _cleaned_xml != xml:
                return FixSuggestion("/", xml, _cleaned_xml, code, msg, "high")

        # ══════════════════════════════════════════════════════════════════════
        # ── END GAP HANDLERS ──────────────────────────────────────────────────
        # ══════════════════════════════════════════════════════════════════════

        # If the issue's fix_hint is empty, try to pull `fix` from the matching
        # rule so downstream value-extraction has something to work with.
        if not fix_hint and rules_idx:
            rule = rules_idx.lookup(rule_id=code,
                                     path_parts=[p for p in path.replace("/", ".").split(".") if p],
                                     leaf_tag=(path.replace("/", ".").split(".") or [""])[-1])
            if rule:
                fix_hint = rule.get("fix") or rule.get("errorMessage") or ""

        # ── Route: invalid enum/code VALUE with several same-named leaves ──────
        # Layer 2 emits a NON-INDEXED xpath (e.g. /…/Stmt/Bal/CdtDbtInd) via
        # _get_xpath_for_element. When a message has repeating siblings (OPBD &
        # CLBD balances, multiple Ntry, etc.) the dot-path walk below always lands
        # on the FIRST sibling — so a later, actually-invalid leaf (e.g. the bad
        # CdtDbtInd at line 84) is never repaired and the fix silently no-ops.
        # Pin the exact offending leaf by the bad VALUE quoted in the message,
        # disambiguating with the line hint when several share that value.
        _bad_m = re.search(r"(?:value|code|data) '([^']*)'", f"{msg} {fix_hint}", re.I)
        if _bad_m and _bad_m.group(1).strip():
            _bad_val = _bad_m.group(1).strip()
            _leaf_m = re.search(r"(?:field|element|tag) '([^']+)'", f"{msg} {fix_hint}", re.I)
            _leaf = ""
            if _leaf_m:
                _leaf = _leaf_m.group(1).split('}')[-1].split(':')[-1]
            if not _leaf:
                _pp = [p for p in path.replace("/", ".").split(".") if p and "[" not in p]
                _leaf = _pp[-1] if _pp else ""
            if _leaf and _VALID_XML_NAME.match(_leaf):
                _val_cands = [
                    el for el in root.iter()
                    if isinstance(el.tag, str)
                    and etree.QName(el.tag).localname == _leaf
                    and not list(el)
                    and (el.text or "").strip() == _bad_val
                ]
                if _val_cands:
                    _pick = _val_cands[0]
                    if len(_val_cands) > 1 and line_hint is not None:
                        _pick = min(_val_cands,
                                    key=lambda e: abs((e.sourceline or 0) - line_hint))
                    _vfix = self._fix_value(_pick, code, msg, fix_hint, ns)
                    if _vfix is not None and _vfix.confidence != "low":
                        return _vfix

        # ── Parse dot-path ────────────────────────────────────────────────────
        # Detect attribute-target paths like 'IntrBkSttlmAmt@Ccy' or '...Amt@Ccy'.
        # These mean: fix the @Ccy attribute on the IntrBkSttlmAmt element.
        attr_target = ""
        attr_m = re.search(r"@(\w+)\s*$", path)
        if attr_m:
            attr_target = attr_m.group(1)
            path = re.sub(r"@\w+\s*$", "", path).strip().rstrip(".")

        parts = [p.strip() for p in path.replace("/", ".").split(".") if p.strip()]
        # Strip any embedded @Attr from parts but preserve [index]
        parts = [re.sub(r'@\w+$', '', p) for p in parts]
        parts = [p for p in parts if p]

        # Drop segments that aren't legal XML element names (ignoring [index] for the check)
        def _is_valid_part(part):
            m = re.match(r'^([^\[]+)(?:\[\d+\])?$', part)
            return bool(m and _VALID_XML_NAME.match(m.group(1)))
        
        parts = [p for p in parts if _is_valid_part(p)]
        parts_stripped = [re.sub(r'\[\d+\]', '', p) for p in parts]

        # ── Attribute fix: locate the element and fix the attribute value ─────
        if attr_target and parts:
            target_el = self._walk_dot_path(root, parts)
            if target_el is not None:
                return self._fix_attribute(target_el, attr_target,
                                            code, msg, fix_hint, ns)

        # ── Infer @Ccy attribute fix when validator reports an Amt element ────
        # XSD currency-attribute errors are usually reported on the Amt element
        # without an explicit "@Ccy" path suffix. Detect by message keywords and
        # by the element actually having a Ccy attribute.
        if not attr_target and parts:
            msg_lc = (msg + " " + fix_hint).lower()
            looks_like_ccy = (
                "currency code" in msg_lc
                or "ccy" in msg_lc
                or "iso 4217" in msg_lc
            )
            if looks_like_ccy:
                target_el = self._walk_dot_path(root, parts)
                if target_el is not None and target_el.get("Ccy") is not None:
                    return self._fix_attribute(target_el, "Ccy",
                                                code, msg, fix_hint, ns)

        # ── If path is empty/"/", try to extract a target from message text ───
        # Rule-level errors (e.g. cross-field checks) report path="/" but mention
        # specific tags in the message. Try to recover a usable target.
        if not parts:
            recovered = self._recover_target_from_message(root, msg, fix_hint)
            if recovered is not None:
                return self._fix_value(recovered, code, msg, fix_hint, ns)
            # Last resort: ask the LLM to fix the whole document root region
            return self._llm_fallback(
                self._xpath_of(root), self._serialize(root)[:1500],
                code, msg, fix_hint
            )

        missing_tag  = parts_stripped[-1]
        parent_parts = parts[:-1]
        parent_parts_stripped = parts_stripped[:-1]

        # ── Walk to parent ────────────────────────────────────────────────────
        parent_el = self._walk_dot_path(root, parent_parts) if parent_parts else None

        # ── Shallow/relative validator path: parent isn't root's direct child ──
        # Some validators report short relative paths (e.g. "//Stmt/Bal") that
        # omit the real ancestor chain back to Document/BkToCstmrStmt.
        # _walk_dot_path only matches direct children starting at ROOT, so it
        # can't locate a deeply-nested parent and reports "missing" even though
        # it exists. Search by local name anywhere in the document before
        # falling through to the "build a brand-new subtree" path below — that
        # path anchors on the nearest existing ancestor (often the document
        # ROOT itself), which built and inserted the new subtree as a stray
        # sibling of <Document> instead of finding the real, already-present
        # parent.
        if parent_el is None and parent_parts:
            _anywhere_tag = parent_parts_stripped[-1]
            _anywhere_cands = [el for el in root.iter()
                                if isinstance(el.tag, str)
                                and etree.QName(el.tag).localname == _anywhere_tag]
            if _anywhere_cands:
                parent_el = (min(_anywhere_cands,
                                 key=lambda e: abs((e.sourceline or 0) - line_hint))
                             if line_hint is not None else _anywhere_cands[0])

        # ── If parent not found, walk up to find the deepest existing ancestor ─
        # e.g. path = Dbtr.PstlAdr.Ctry  but PstlAdr doesn't exist yet →
        # anchor = Dbtr, then build PstlAdr with Ctry inside it.
        if parent_el is None:
            # First check: maybe the full path exists (value fix)
            target_el = self._walk_dot_path(root, parts)
            if target_el is not None:
                return self._fix_value(target_el, code, msg, fix_hint, ns)

            # Bare leaf-tag path (e.g. the validator emitted "//PstCd" because it
            # couldn't pin the exact node). _walk_dot_path only checks DIRECT
            # children of the root, so a deeply-nested element looks "missing" and
            # we'd wrongly insert a DUPLICATE. The element almost always already
            # EXISTS — this is a value error. Locate it and fix its value instead.
            # Prefer the instance whose text matches the offending value quoted in
            # the message, else the line-nearest one.
            if not parent_parts:
                existing_matches = [
                    el for el in root.iter()
                    if isinstance(el.tag, str)
                    and etree.QName(el.tag).localname == missing_tag
                ]
                if existing_matches:
                    bad_m = re.search(r"(?:value|code) '([^']*)'", f"{msg} {fix_hint}", re.I)
                    bad_val = bad_m.group(1) if bad_m else None
                    pick = None
                    if bad_val:
                        pick = next((e for e in existing_matches
                                     if (e.text or "").strip() == bad_val), None)
                    if pick is None:
                        lh = getattr(self, "_line_hint", None)
                        pick = (min(existing_matches,
                                    key=lambda e: abs((e.sourceline or 0) - lh))
                                if lh is not None else existing_matches[0])
                    return self._fix_value(pick, code, msg, fix_hint, ns)

            # Walk up from parent_parts until we find an existing ancestor
            anchor_el, missing_chain = self._find_deepest_ancestor(root, parent_parts)
            if anchor_el is None:
                # Even the top-level path doesn't match anything; try recovering
                # from message text, then LLM as a true last resort.
                recovered = self._recover_target_from_message(root, msg, fix_hint)
                if recovered is not None:
                    return self._fix_value(recovered, code, msg, fix_hint, ns)
                return self._llm_fallback(
                    self._xpath_of(root), self._serialize(root)[:1500],
                    code, msg, fix_hint
                )

            # missing_chain = [PstlAdr, ...], missing_tag = Ctry
            # Build the whole missing subtree bottom-up then nest it.
            # Namespace MUST come from the anchor element, not the document root:
            # in an enveloped message the root is BusMsgEnvlp (envelope ns) but
            # body elements live in the message (pacs.008) ns — using the root ns
            # produced invalid <ns0:Tag xmlns:ns0="...envelope"> inserts.
            anchor_ns = etree.QName(anchor_el.tag).namespace or ns
            return self._suggest_missing_subtree(
                anchor_el, missing_chain, missing_tag,
                fix_hint, anchor_ns, tmap, code, msg,
                rules_idx=rules_idx, path_parts=parts_stripped, root=root,
                msg_type=msg_type
            )

        # ── Check if child already exists ─────────────────────────────────────
        existing = self._child_exists(parent_el, parts[-1])
        if existing is not None:
            # Child exists but has wrong value — fix its value
            return self._fix_value(existing, code, msg, fix_hint, ns)

        # ── Check if it exists deeper (not a DIRECT child) ──────────────────────
        # The validator's flattened xpath (e.g. //AppHdr/BICFI) names only the
        # leaf and its nearest reported ancestor — it does NOT mean the tag is a
        # direct child. BICFI normally lives at AppHdr/Fr/FIId/FinInstnId/BICFI.
        # Without this check, _child_exists (direct-children only) reports "not
        # found" and the code below inserts a DUPLICATE leaf straight under
        # parent_el, turning a value error into a structural "unexpected field"
        # error while leaving the original bad value untouched.
        if not missing_tag.endswith("]"):
            _descendant = next(
                (d for d in parent_el.iter()
                 if d is not parent_el and isinstance(d.tag, str)
                 and etree.QName(d.tag).localname == missing_tag),
                None
            )
            if _descendant is not None:
                return self._fix_value(_descendant, code, msg, fix_hint, ns)

        # ── Add missing child ─────────────────────────────────────────────────
        original_fragment = self._serialize(parent_el)
        xpath             = self._xpath_of(parent_el)

        # Build in the PARENT's namespace, not the document root's — see note
        # above: enveloped messages have a different root (envelope) ns than the
        # body (pacs.008/etc.), and stamping the wrong ns corrupts the insert.
        child_ns = etree.QName(parent_el.tag).namespace or ns
        child_el = self._build_child(missing_tag, fix_hint, child_ns, tmap,
                                     existing_parent=parent_el,
                                     rules_idx=rules_idx, path_parts=parts_stripped,
                                     rule_id=code, root=root, msg_type=msg_type)
        if child_el is None:
            return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint)

        # Insert the new child in the correct position based on XSD sequence order.
        # Fallback: append (safe default).
        parent_copy = self._copy(parent_el)
        insert_idx  = self._find_insert_index(parent_copy, missing_tag, tmap,
                                              parent_path=parent_parts_stripped)
        if insert_idx is None:
            parent_copy.append(child_el)
        else:
            parent_copy.insert(insert_idx, child_el)

        return FixSuggestion(
            xpath=xpath,
            original_fragment=original_fragment,
            fragment_xml=self._serialize(parent_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    # ── Address / country fix ─────────────────────────────────────────────────

    _PARTY_NAMES = {
        "Dbtr", "Cdtr", "UltmtDbtr", "UltmtCdtr", "InitgPty", "FwdgAgt",
        "InstgAgt", "InstdAgt", "DbtrAgt", "CdtrAgt", "IntrmyAgt1",
        "IntrmyAgt2", "IntrmyAgt3", "Pty",
    }

    def _infer_country_from_party(self, root: etree._Element,
                                   party_name: Optional[str]) -> Optional[str]:
        """
        Best-effort country inference for a party.
        Priority: BICFI chars 4-6, IBAN prefix chars 0-2.
        Search order:
          1. Elements INSIDE the named party element (e.g. Dbtr/PstlAdr or Dbtr IBAN).
          2. Elements inside the RELATED AGENT (DbtrAgt for Dbtr, CdtrAgt for Cdtr)
             — these are siblings, not children.
          3. Any BICFI/IBAN anywhere in the document as last resort.
        Returns a 2-letter ISO country code or None.
        """
        _RELATED_AGENT = {
            "Dbtr": "DbtrAgt",  "Cdtr": "CdtrAgt",
            "UltmtDbtr": "DbtrAgt", "UltmtCdtr": "CdtrAgt",
        }

        def _country_from_el(el: etree._Element) -> Optional[str]:
            if not isinstance(el.tag, str):
                return None
            local = etree.QName(el.tag).localname
            txt = (el.text or "").strip()
            if local == "BICFI" and len(txt) >= 6:
                return txt[4:6]
            if local == "IBAN" and len(txt) >= 2:
                return txt[:2]
            return None

        def _under(el: etree._Element, parent_name: str) -> bool:
            cur = el.getparent()
            while cur is not None:
                if isinstance(cur.tag, str) and etree.QName(cur.tag).localname == parent_name:
                    return True
                cur = cur.getparent()
            return False

        # 1. Inside the named party
        if party_name:
            for el in root.iter():
                if _under(el, party_name):
                    c = _country_from_el(el)
                    if c:
                        return c

        # 2. Inside the related agent (sibling element in the same tx block)
        related_agent = _RELATED_AGENT.get(party_name or "") if party_name else None
        if related_agent:
            for el in root.iter():
                if _under(el, related_agent):
                    c = _country_from_el(el)
                    if c:
                        return c

        # 3. Any BICFI / IBAN in the document
        for el in root.iter():
            c = _country_from_el(el)
            if c:
                return c

        return None

    def _fix_cbpr_r3_bic(
        self, root: "etree._Element", code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """
        Fix CBPR_R3 / L3-PACS-MATCH-TO / L3-PACS-MATCH-FR:
        AppHdr/To BICFI must equal InstdAgt BICFI  (or Fr ↔ InstgAgt).
        Document body is authoritative; update the header side.
        """
        apphdr = root.find(".//{*}AppHdr")
        if apphdr is None and etree.QName(root.tag).localname == "AppHdr":
            apphdr = root
        if apphdr is None:
            return None

        # Determine which header role / doc role to fix from code or message.
        is_fr_side = (
            code == "L3-PACS-MATCH-FR"
            or "instgagt" in msg.lower()
            or ("fr" in msg.lower() and "to" not in msg.lower())
        )
        header_role = "Fr" if is_fr_side else "To"
        doc_agent  = "instgagt" if is_fr_side else "instdagt"

        # Extract target BIC from message: "(Header: 'X' vs Doc: 'Y')"
        doc_bic_m = re.search(
            r"(?:Doc|document)[^']*'([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)'", msg, re.I
        )
        target_bic = doc_bic_m.group(1).strip() if doc_bic_m else None

        # Fallback: scan document body for the canonical agent BICFI.
        _BIC_RE = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
        if not target_bic:
            apphdr_nodes = list(apphdr.iter())  # keep alive: ids must stay
            apphdr_ids = {id(el) for el in apphdr_nodes}  # valid for the loop below
            for el in root.iter():
                if not isinstance(el.tag, str) or id(el) in apphdr_ids:
                    continue
                if etree.QName(el.tag).localname != "BICFI":
                    continue
                if f"/{doc_agent}/" in self._xpath_of(el).lower():
                    target_bic = (el.text or "").strip()
                    break

        # Second fallback: any valid BICFI in the body when the agent-specific
        # scan found nothing (e.g. message format doesn't quote the BIC inline).
        if not target_bic:
            apphdr_nodes2 = list(apphdr.iter())  # keep alive: ids must stay
            apphdr_ids = {id(el) for el in apphdr_nodes2}  # valid for the loop below
            for el in root.iter():
                if not isinstance(el.tag, str) or id(el) in apphdr_ids:
                    continue
                if etree.QName(el.tag).localname != "BICFI":
                    continue
                txt = (el.text or "").strip()
                if txt and _BIC_RE.match(txt):
                    target_bic = txt
                    break

        if not target_bic:
            return None

        # Find AppHdr/<header_role>/.../BICFI
        header_bicfi_el = None
        for hr_el in apphdr.iter():
            if not isinstance(hr_el.tag, str):
                continue
            if etree.QName(hr_el.tag).localname == header_role:
                for desc in hr_el.iter():
                    if isinstance(desc.tag, str) and etree.QName(desc.tag).localname == "BICFI":
                        header_bicfi_el = desc
                        break
                break

        if header_bicfi_el is None or (header_bicfi_el.text or "").strip() == target_bic:
            return None

        xpath = self._xpath_of(header_bicfi_el)
        original_fragment = self._serialize(header_bicfi_el)
        el_copy = self._copy(header_bicfi_el)
        el_copy.text = target_bic
        return FixSuggestion(
            xpath=xpath,
            original_fragment=original_fragment,
            fragment_xml=self._serialize(el_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _fix_nb_of_txs(
        self, root: "etree._Element", code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """
        Fix NBOFTXS_MISMATCH / PACS008_NBOFTXS_EQ_TX_COUNT: update NbOfTxs to
        match actual transaction element count. Preferred over _fix_value because
        the path is a line number so the normal path-walk never finds NbOfTxs.

        Counting is SCOPED to the message container that owns this NbOfTxs (the
        parent of its GrpHdr). A multi-message bulk wrapper (e.g. <BulkMessages>
        holding several <Document>s) has one NbOfTxs per message; a whole-tree
        count would sum every message's transactions and falsely "match", so the
        mismatch must be resolved per message.
        """
        # PmtInf is a container (groups transactions), never a countable
        # transaction unit for NbOfTxs. Including it double-counts pain.001/
        # pain.008 (1 PmtInf + 1 CdtTrfTxInf = 2) and prevents fixing NbOfTxs=2.
        TX_TAGS = {"CdtTrfTxInf", "DrctDbtTxInf", "TxInfAndSts", "TxInf"}

        # Collect every NbOfTxs; in a bulk wrapper there may be several.
        nb_els = [
            el for el in root.iter()
            if isinstance(el.tag, str) and etree.QName(el.tag).localname == "NbOfTxs"
        ]
        if not nb_els:
            return None

        # When the issue carries a source line, pick the NbOfTxs on (or nearest
        # at-or-before) that line so the fix lands on the message that triggered
        # the error rather than the first message in the bulk file.
        nb_el = nb_els[0]
        _hint = getattr(self, "_line_hint", None)
        if _hint is not None and len(nb_els) > 1:
            _candidates = [e for e in nb_els if (e.sourceline or 0) <= _hint]
            nb_el = max(_candidates, key=lambda e: e.sourceline or 0) if _candidates \
                else min(nb_els, key=lambda e: abs((e.sourceline or 0) - _hint))

        # Scope the transaction count to this NbOfTxs's message container: walk
        # up GrpHdr → message root (e.g. FICdtTrf / FIToFICstmrCdtTrf), then count
        # transaction blocks that descend from THAT container only.
        grp_hdr = nb_el.getparent()
        msg_root = grp_hdr.getparent() if grp_hdr is not None else None
        scope = msg_root if msg_root is not None else root
        count = sum(
            1 for n in scope.iter()
            if isinstance(n.tag, str) and etree.QName(n.tag).localname in TX_TAGS
        )
        # The scoped structural count is authoritative. Only fall back to the
        # count stated in the error message when no transaction tag matched this
        # message family (count == 0) — never let a message-stated total override
        # a good per-message count, or a whole-document total (from a single-
        # message-only validator) would revert a correctly scoped fix.
        if count == 0:
            msg_count_m = re.search(r"actually contains (\d+)", msg)
            if msg_count_m:
                count = int(msg_count_m.group(1))

        if count == 0:
            return None
        if str(count) == (nb_el.text or "").strip():
            return None

        xpath = self._xpath_of(nb_el)
        original = self._serialize(nb_el)
        el_copy = self._copy(nb_el)
        el_copy.text = str(count)
        return FixSuggestion(xpath, original, self._serialize(el_copy), code, msg, "high")

    def _fix_wrong_namespace(
        self, xml: str, code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """
        Fix Wrong Namespace. Two distinct shapes:

        A. Extra version components on an ISO 20022 namespace
           (e.g. pacs.004.001.09.12.12.12 → pacs.004.001.09) — truncate to 4.

        B. Body orphaned in the SWIFT envelope namespace. When the
           <Document xmlns="urn:iso:…"> wrapper open+close tags are deleted, the
           payload elements inherit BusMsgEnvlp's default ns
           (urn:swift:xsd:envelope) and the validator flags every body element.
           Rebuild the <Document> wrapper from MsgDefIdr and re-namespace the body.

        Returns a whole-document replacement (xpath="/").
        """
        ns_m = re.search(r"namespace '([^']+)'", msg)
        if not ns_m:
            return None
        bad_ns = ns_m.group(1)

        ISO_PREFIX = "urn:iso:std:iso:20022:tech:xsd:"

        # ── Shape B: orphaned body in the SWIFT envelope namespace ────────────
        # Specific to the deleted-<Document>-wrapper case. Healthy messages always
        # carry <Document>, so the guard below never fires on them.
        if bad_ns == "urn:swift:xsd:envelope":
            return self._rebuild_missing_document_wrapper(xml, bad_ns, code, msg)

        # ── Shape A: extra version components on an ISO namespace ─────────────
        if not bad_ns.startswith(ISO_PREFIX):
            return None

        parts = bad_ns[len(ISO_PREFIX):].split(".")
        if len(parts) <= 4:
            return None  # Already standard length — nothing to truncate.

        good_ns = ISO_PREFIX + ".".join(parts[:4])
        fixed_xml = xml.replace(bad_ns, good_ns)
        if fixed_xml == xml:
            return None

        try:
            etree.fromstring(fixed_xml.encode("utf-8"))
        except etree.XMLSyntaxError:
            return None

        return FixSuggestion("/", xml, fixed_xml, code, msg, "high")

    def _rens_subtree(self, el, old_ns: str, new_ns: str):
        """Deep-copy `el`, moving any element in `old_ns` (or no namespace) into
        `new_ns`. Elements already in some other explicit namespace are kept as-is
        (defensive — the orphaned ISO body never legitimately contains one)."""
        q = etree.QName(el.tag)
        cur_ns = q.namespace or ""
        tgt_ns = new_ns if cur_ns in (old_ns, "") else cur_ns
        new_el = etree.Element(f"{{{tgt_ns}}}{q.localname}" if tgt_ns else q.localname)
        new_el.text = el.text
        new_el.tail = el.tail
        for k, v in el.attrib.items():
            new_el.set(k, v)
        for child in el:
            if isinstance(child.tag, str):  # skip comments / PIs
                new_el.append(self._rens_subtree(child, old_ns, new_ns))
        return new_el

    def _rebuild_missing_document_wrapper(
        self, xml: str, env_ns: str, code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """Reconstruct a deleted <Document> wrapper.

        Fires ONLY when every guard holds, so no healthy document is touched:
          • root is <BusMsgEnvlp>
          • an <AppHdr> child is present
          • NO <Document> child exists (the wrapper really is gone)
          • there are non-AppHdr body elements sitting in the envelope namespace
          • <MsgDefIdr> is present and is a valid ISO message id (pacs.010.001.03)

        The correct ISO namespace is recoverable because MsgDefIdr survives in the
        AppHdr. We wrap the orphaned body in <Document xmlns="urn:iso:…{MsgDefIdr}">
        and re-namespace it so the validator's Layer-1 namespace check passes.
        """
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except Exception:
            return None
        if etree.QName(root.tag).localname != "BusMsgEnvlp":
            return None

        children = [c for c in root if isinstance(c.tag, str)]
        apphdr = next((c for c in children
                       if etree.QName(c.tag).localname == "AppHdr"), None)
        if apphdr is None:
            return None
        if any(etree.QName(c.tag).localname == "Document" for c in children):
            return None  # Document already present — not the deleted-wrapper case.

        mdi = None
        for el in apphdr.iter():
            if isinstance(el.tag, str) and etree.QName(el.tag).localname == "MsgDefIdr":
                mdi = (el.text or "").strip()
                break
        if not mdi or not re.match(r"^[a-z]+\.\d{3}\.\d{3}\.\d{2}$", mdi):
            return None
        iso_ns = "urn:iso:std:iso:20022:tech:xsd:" + mdi

        body_els = [c for c in children
                    if c is not apphdr
                    and (etree.QName(c.tag).namespace or "") == env_ns]
        if not body_els:
            return None

        # Declare the ISO namespace as the DEFAULT (prefix-free) on <Document> so
        # Layer-1's `doc_node.nsmap.get(None)` reads the ISO ns, not the inherited
        # envelope ns. Without nsmap={None: iso_ns} lxml emits <ns0:Document …>,
        # leaving the node's default ns = the envelope ns → check still fails.
        doc = etree.Element(f"{{{iso_ns}}}Document", nsmap={None: iso_ns})
        for be in body_els:
            doc.append(self._rens_subtree(be, env_ns, iso_ns))
        for be in body_els:
            root.remove(be)
        root.insert(list(root).index(apphdr) + 1, doc)

        decl = ""
        m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
        if m:
            decl = m.group(1) + "\n"
        fixed_xml = decl + etree.tostring(root, encoding="unicode")
        if fixed_xml == xml:
            return None
        try:
            etree.fromstring(fixed_xml.encode("utf-8"))
        except etree.XMLSyntaxError:
            return None

        return FixSuggestion("/", xml, fixed_xml, code, msg, "high")

    def _fix_iban_currency_mismatch(
        self, root: "etree._Element", code: str, msg: str, fix_hint: str,
        path: str = "",
    ) -> Optional["FixSuggestion"]:
        """
        Fix CURR_IBAN_MISMATCH.

        Two cases:
          A. IBAN is wrong (dummy/placeholder) — replace the IBAN with one whose
             country matches the actual payment currency.
          B. Currency is wrong — update the Ccy attribute to match the IBAN country.

        We prefer case A when:
          - The offending IBAN is the known dummy default (GB29NWBK60161331926819), OR
          - Another IBAN in the same document already matches the payment currency, which
            means the payment currency is authoritative and only the DbtrAcct IBAN is stale.
        """
        # ── Extract actual currency (the one on the amount element, e.g. EUR) ──
        wrong_ccy_m = re.search(r"Currency\s+([A-Z]{3})\s+does not match", msg, re.I)
        actual_ccy = wrong_ccy_m.group(1).upper() if wrong_ccy_m else None

        # ── Extract IBAN country from message ──
        # "for IBAN country GB" / "country code 'GB'"
        ctry_m = re.search(r"(?:IBAN country|country code[^A-Z]*)([A-Z]{2})\b", msg, re.I)
        iban_country = ctry_m.group(1).upper() if ctry_m else None

        # ── Find the offending IBAN element ──
        _IBAN_PAT = re.compile(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$')
        _KNOWN_DUMMIES = {
            "GB29NWBK60161331926819",
            "DE89370400440532013000",
        }
        offending_iban_el = None
        for _el in root.iter():
            if not isinstance(_el.tag, str):
                continue
            if etree.QName(_el.tag).localname != "IBAN":
                continue
            _iban_val = (_el.text or "").strip()
            if iban_country and _iban_val[:2].upper() == iban_country:
                offending_iban_el = _el
                break

        # ── Decide: fix the IBAN or fix the currency? ──
        fix_iban = False
        if offending_iban_el is not None:
            _iban_val = (offending_iban_el.text or "").strip()
            # Case A1: it's a known dummy IBAN → definitely fix the IBAN
            if _iban_val in _KNOWN_DUMMIES:
                fix_iban = True
            # Case A2: another valid IBAN in the doc already matches actual_ccy
            elif actual_ccy:
                _ccy_by_ctry = _kb_get("dummy_data.currencies_by_country", {}) or {}
                for _other_el in root.iter():
                    if not isinstance(_other_el.tag, str):
                        continue
                    if etree.QName(_other_el.tag).localname != "IBAN":
                        continue
                    if _other_el is offending_iban_el:
                        continue
                    _ot = (_other_el.text or "").strip()
                    if (_ot and _IBAN_PAT.match(_ot)
                            and _ccy_by_ctry.get(_ot[:2].upper()) == actual_ccy):
                        fix_iban = True
                        break

        if fix_iban and offending_iban_el is not None and actual_ccy:
            # Replace the IBAN with one whose country matches actual_ccy
            _new_iban = _iban_for_ccy(root, offending_iban_el)
            _old_text = (offending_iban_el.text or "").strip()
            if _new_iban and _new_iban != _old_text:
                _el_copy = self._copy(offending_iban_el)
                _el_copy.text = _new_iban
                return FixSuggestion(
                    self._xpath_of(offending_iban_el),
                    self._serialize(offending_iban_el),
                    self._serialize(_el_copy),
                    code, msg, "high",
                )

        # ── Case B: fix the currency attribute(s) on the amount element(s) ────
        combined = f"{msg} {fix_hint}"
        ccy_m = re.search(
            r"(?:expected\s+currency|update.*?currency\s+to|currency\s+to)\s+([A-Z]{3})\b",
            combined, re.I
        )
        if not ccy_m:
            ccy_m = re.search(r"\b([A-Z]{3})\b.*?(?:for|IBAN|account|country)", combined, re.I)
        if not ccy_m:
            return None
        expected_ccy = ccy_m.group(1).upper()

        PREF_TAGS = {"IntrBkSttlmAmt", "RtrdIntrBkSttlmAmt", "InstdAmt",
                     "RtrdInstdAmt", "TtlIntrBkSttlmAmt", "EqvtAmt"}

        # Resolve the EXACT element the validator flagged via `path` first —
        # the validator picks one currency field as authoritative (e.g.
        # InstdAmt before IntrBkSttlmAmt), and re-validation re-checks that
        # SAME field. Falling back to a document-order scan can pick a
        # different sibling, leave the flagged field untouched, and the issue
        # re-fires after the fix is "applied".
        target_el = None

        # Multi-message disambiguation: when the issue carries a source line
        # (e.g. a <BulkMessages> wrapper with several messages), the dotted
        # `path` is ambiguous — the same Document.FICdtTrf.CdtTrfTxInf path
        # exists in every message and the walk resolves to the wrong one,
        # producing a no-op fix that re-fires forever. Prefer the amount element
        # whose source line is nearest at-or-before the hinted line.
        _hint = getattr(self, "_line_hint", None)
        if _hint is not None:
            _amts = [
                _el for _el in root.iter()
                if isinstance(_el.tag, str)
                and etree.QName(_el.tag).localname in PREF_TAGS
                and _el.get("Ccy") is not None
            ]
            if len(_amts) > 1:
                _at = [e for e in _amts if (e.sourceline or 0) <= _hint]
                target_el = max(_at, key=lambda e: e.sourceline or 0) if _at \
                    else min(_amts, key=lambda e: abs((e.sourceline or 0) - _hint))

        if target_el is None and path:
            attr_path = re.sub(r"@\w+\s*$", "", path).strip().rstrip(".")
            parts = [p for p in attr_path.replace("/", ".").split(".") if p]
            if parts:
                _candidate = self._walk_dot_path(root, parts)
                if _candidate is not None and _candidate.get("Ccy") is not None:
                    target_el = _candidate

        if target_el is None:
            for _el in root.iter():
                if not isinstance(_el.tag, str):
                    continue
                local = etree.QName(_el.tag).localname
                ccy = _el.get("Ccy")
                if ccy is None:
                    continue
                if local in PREF_TAGS and (not actual_ccy or ccy == actual_ccy):
                    target_el = _el
                    break
        if target_el is None and actual_ccy:
            for _el in root.iter():
                if isinstance(_el.tag, str) and _el.get("Ccy") == actual_ccy:
                    target_el = _el
                    break

        if target_el is None:
            return None

        bad_ccy = target_el.get("Ccy")
        if bad_ccy == expected_ccy:
            return None

        # Walk up to the enclosing transaction container (…TxInf, …PmtInf) so
        # sibling amount fields sharing the same wrong currency (e.g. both
        # IntrBkSttlmAmt AND InstdAmt set to NOK) are corrected together —
        # fixing only one leaves the other stale and the same
        # CURR_IBAN_MISMATCH re-triggers on it after "applying" the fix.
        container = target_el
        for _ in range(4):
            _parent = container.getparent()
            if _parent is None:
                break
            container = _parent
            if (isinstance(container.tag, str)
                    and etree.QName(container.tag).localname.endswith(("TxInf", "PmtInf"))):
                break

        original_fragment = self._serialize(container)
        container_copy = self._copy(container)
        changed = False
        for _el in container_copy.iter():
            if (isinstance(_el.tag, str)
                    and etree.QName(_el.tag).localname in PREF_TAGS
                    and _el.get("Ccy") == bad_ccy):
                _el.set("Ccy", expected_ccy)
                changed = True
        if not changed:
            return None

        return FixSuggestion(
            self._xpath_of(container), original_fragment,
            self._serialize(container_copy), code, msg, "high",
        )

    def _fix_bah_assgnr_assgne_bic(
        self, root: "etree._Element", code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """
        Fix camt.056 BAH/BIC mismatch by directly harvesting the document-body
        Assgnr/Assgne BICFI and writing it into the AppHdr Fr/To BICFI.

        The normal _fix_value path exits early as a no-op because the AppHdr
        BICFI is format-valid — it's just the wrong value.  This method bypasses
        that check by locating both elements directly and patching the header side.
        """
        is_fr = code == "CAMT056_FR_EQ_ASSGNR_BIC"
        header_role = "Fr" if is_fr else "To"
        doc_role = "Assgnr" if is_fr else "Assgne"
        doc_role_lc = doc_role.lower()

        # 1. Find the document-body BICFI under Assgnr or Assgne (source of truth).
        doc_bicfi_el = None
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            if etree.QName(el.tag).localname != "BICFI":
                continue
            xpath_lc = self._xpath_of(el).lower()
            if "apphdr" in xpath_lc:
                continue
            if f"/{doc_role_lc}/" in xpath_lc:
                doc_bicfi_el = el
                break

        if doc_bicfi_el is None or not (doc_bicfi_el.text or "").strip():
            return None
        target_bic = doc_bicfi_el.text.strip()

        # 2. Find AppHdr/Fr (or To)/…/BICFI — the element to fix.
        apphdr = root.find(".//{*}AppHdr")
        if apphdr is None and etree.QName(root.tag).localname == "AppHdr":
            apphdr = root
        if apphdr is None:
            return None

        header_bicfi_el = None
        for hr_el in apphdr.iter():
            if not isinstance(hr_el.tag, str):
                continue
            if etree.QName(hr_el.tag).localname == header_role:
                for desc in hr_el.iter():
                    if isinstance(desc.tag, str) and etree.QName(desc.tag).localname == "BICFI":
                        header_bicfi_el = desc
                        break
                if header_bicfi_el is not None:
                    break

        if header_bicfi_el is None:
            return None

        current_bic = (header_bicfi_el.text or "").strip()
        if current_bic == target_bic:
            return None  # Already aligned — nothing to fix.

        xpath = self._xpath_of(header_bicfi_el)
        original_fragment = self._serialize(header_bicfi_el)
        el_copy = self._copy(header_bicfi_el)
        el_copy.text = target_bic
        return FixSuggestion(
            xpath=xpath,
            original_fragment=original_fragment,
            fragment_xml=self._serialize(el_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _fix_msgdefidr_mismatch(
        self, root: "etree._Element", code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """
        Fix HEAD001_MSGDEFIDR_MISMATCH by updating AppHdr/MsgDefIdr to match
        the Document namespace version.  The Document namespace is authoritative
        (it carries the actual payload); MsgDefIdr is just the declaration.

        Example: MsgDefIdr='camt.056.001.09', Document xmlns ends with
        'camt.056.001.08' → set MsgDefIdr to 'camt.056.001.08'.
        """
        # 1. Find AppHdr/MsgDefIdr — the element to fix.
        apphdr = root.find(".//{*}AppHdr")
        if apphdr is None and etree.QName(root.tag).localname == "AppHdr":
            apphdr = root
        if apphdr is None:
            return None

        mdi_el = None
        for child in apphdr.iter():
            if isinstance(child.tag, str) and etree.QName(child.tag).localname == "MsgDefIdr":
                mdi_el = child
                break
        if mdi_el is None:
            return None

        current_val = (mdi_el.text or "").strip()

        # 2. Find the Document element and read its namespace.
        doc_ns = ""
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            local = etree.QName(el.tag).localname
            ns = etree.QName(el.tag).namespace or ""
            if local == "Document" and "iso:20022" in ns:
                doc_ns = ns
                break

        if not doc_ns:
            return None

        # Namespace ends with the message identifier, e.g.
        # urn:iso:std:iso:20022:tech:xsd:camt.056.001.08
        correct_val = doc_ns.split(":")[-1] if ":" in doc_ns else doc_ns
        if not correct_val or correct_val == current_val:
            return None  # Already aligned — nothing to fix.

        xpath = self._xpath_of(mdi_el)
        original_fragment = self._serialize(mdi_el)
        el_copy = self._copy(mdi_el)
        el_copy.text = correct_val
        return FixSuggestion(
            xpath=xpath,
            original_fragment=original_fragment,
            fragment_xml=self._serialize(el_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _fix_addr_ctry_missing(
        self,
        root: etree._Element,
        xml: str,
        code: str,
        msg: str,
        fix_hint: str,
        ns: str,
        tmap: Optional["_XsdTypeMap"],
        rules_idx: Optional["_RulesIndex"],
        msg_type: str,
    ) -> Optional[FixSuggestion]:
        """
        Fix ADDR_CTRY_MISSING in two branches:

        Branch A — PstlAdr EXISTS but has no <Ctry>:
            Insert <Ctry>XX</Ctry> at the correct position inside the existing
            PstlAdr.  Country code is inferred from the party's BICFI/IBAN where
            possible; otherwise defaults to 'US'.

        Branch B — PstlAdr does NOT EXIST (address block entirely absent):
            Insert a complete dummy <PstlAdr> (with AdrLine + Ctry) into the
            party element so the message is immediately schema-valid.

        The party name is extracted from the message text (e.g. "Dbtr address").
        Both branches use line-nearest matching so the right element is targeted
        when the same party tag appears multiple times (e.g. Dbtr vs CdtTrfTxInf
        in different transaction blocks).
        """
        lh = getattr(self, "_line_hint", None)

        # Extract party name from the message ("missing in Dbtr address" → "Dbtr")
        party_m = re.search(
            r'\b(Dbtr|Cdtr|UltmtDbtr|UltmtCdtr|InitgPty|FwdgAgt|'
            r'InstgAgt|InstdAgt|DbtrAgt|CdtrAgt|IntrmyAgt\d?|Pty)\b',
            msg, re.I,
        )
        party_name = party_m.group(1) if party_m else None

        def _pick_nearest(elements):
            if not elements:
                return None
            if lh is None:
                return elements[0]
            return min(elements, key=lambda e: abs((e.sourceline or 0) - lh))

        def _under_party(el: etree._Element, pname: str) -> bool:
            cur = el.getparent()
            while cur is not None:
                if isinstance(cur.tag, str) and etree.QName(cur.tag).localname == pname:
                    return True
                cur = cur.getparent()
            return False

        # ── Branch A: PstlAdr exists, Ctry is absent ──────────────────────────
        all_pstladr = [
            el for el in root.iter()
            if isinstance(el.tag, str)
            and etree.QName(el.tag).localname == "PstlAdr"
        ]
        if party_name:
            candidates = [p for p in all_pstladr if _under_party(p, party_name)]
        else:
            candidates = all_pstladr

        pstl_adr = _pick_nearest(candidates)
        if pstl_adr is not None:
            already_has_ctry = self._child_exists(pstl_adr, "Ctry") is not None
            if not already_has_ctry:
                ctry_val = (
                    self._infer_country_from_party(root, party_name)
                    or "US"
                )
                pstl_ns = etree.QName(pstl_adr.tag).namespace or ns
                ctry_tag = f"{{{pstl_ns}}}Ctry" if pstl_ns else "Ctry"
                ctry_el = etree.Element(ctry_tag)
                ctry_el.text = ctry_val

                pstl_copy = self._copy(pstl_adr)
                ins = self._find_insert_index(
                    pstl_copy, "Ctry", tmap,
                    parent_path=self._local_name_path(pstl_adr),
                )
                if ins is None:
                    pstl_copy.append(ctry_el)
                else:
                    pstl_copy.insert(ins, ctry_el)

                # Normalise whitespace so Ctry appears on its own line
                base, unit, close = self._derive_child_indent(pstl_copy)
                if base is not None:
                    self._normalize_child_tails(pstl_copy, base, close)

                return FixSuggestion(
                    xpath=self._xpath_of(pstl_adr),
                    original_fragment=self._serialize(pstl_adr),
                    fragment_xml=self._serialize(pstl_copy),
                    issue_code=code,
                    issue_message=msg,
                    confidence="high",
                )

        # ── Branch B: PstlAdr is completely absent — insert a dummy one ───────
        if party_name:
            party_els = [
                el for el in root.iter()
                if isinstance(el.tag, str)
                and etree.QName(el.tag).localname == party_name
            ]
        else:
            # Fall back to any party-like element near the reported line
            party_els = [
                el for el in root.iter()
                if isinstance(el.tag, str)
                and etree.QName(el.tag).localname in self._PARTY_NAMES
            ]

        party_el = _pick_nearest(party_els)
        if party_el is None:
            return None

        party_ns = etree.QName(party_el.tag).namespace or ns
        pstl_el = self._build_child(
            "PstlAdr", fix_hint, party_ns, tmap,
            existing_parent=party_el,
            rules_idx=rules_idx,
            path_parts=[party_name or "Dbtr", "PstlAdr"],
            root=root,
            msg_type=msg_type,
        )
        if pstl_el is None:
            return None

        # Ensure the built PstlAdr contains a Ctry element with a sane value
        ctry_in_new = self._child_exists(pstl_el, "Ctry")
        if ctry_in_new is not None:
            if not (ctry_in_new.text or "").strip():
                ctry_in_new.text = (
                    self._infer_country_from_party(root, party_name) or "US"
                )
        else:
            ctry_val = self._infer_country_from_party(root, party_name) or "US"
            ctry_tag = f"{{{party_ns}}}Ctry" if party_ns else "Ctry"
            ctry_child = etree.Element(ctry_tag)
            ctry_child.text = ctry_val
            pstl_el.append(ctry_child)

        party_copy = self._copy(party_el)
        ins = self._find_insert_index(
            party_copy, "PstlAdr", tmap,
            parent_path=self._local_name_path(party_el),
        )
        if ins is None:
            party_copy.append(pstl_el)
        else:
            party_copy.insert(ins, pstl_el)

        base, unit, close = self._derive_child_indent(party_copy)
        if base is not None:
            self._indent_el(pstl_el, base, unit)
            self._normalize_child_tails(party_copy, base, close)

        return FixSuggestion(
            xpath=self._xpath_of(party_el),
            original_fragment=self._serialize(party_el),
            fragment_xml=self._serialize(party_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _find_deepest_ancestor(self, root: etree._Element,
                                parts: list[str]) -> tuple[Optional[etree._Element], list[str]]:
        """
        Walk parts from left to right with [index] support.
        Return (deepest_existing_element, remaining_stripped_parts).
        """
        if not parts: return root, []
        root_local = etree.QName(root.tag).localname
        first_tag = re.sub(r'\[\d+\]', '', parts[0])
        start = 1 if root_local == first_tag else 0
        
        current = [root]
        for i, part in enumerate(parts[start:], start=start):
            m = re.match(r'^([^\[]+)(?:\[(\d+)\])?$', part)
            if not m:
                # Should not happen if part is valid, fallback to returning current best
                return current[0], [re.sub(r'\[\d+\]', '', p) for p in parts[i:]]
            
            tag_name = m.group(1)
            target_idx = int(m.group(2)) if m.group(2) else None
            
            next_nodes = []
            for node in current:
                count = 0
                for child in node:
                    if isinstance(child.tag, str) and etree.QName(child.tag).localname == tag_name:
                        count += 1
                        if target_idx is None or count == target_idx:
                            next_nodes.append(child)
            
            if not next_nodes:
                lh = getattr(self, "_line_hint", None)
                best = min(current, key=lambda e: abs((e.sourceline or 0) - lh)) if lh is not None else current[0]
                return best, [re.sub(r'\[\d+\]', '', p) for p in parts[i:]]
            current = next_nodes
            
        lh = getattr(self, "_line_hint", None)
        best = min(current, key=lambda e: abs((e.sourceline or 0) - lh)) if lh is not None else current[0]
        return best, []

    def _suggest_missing_subtree(self, anchor_el: etree._Element,
                                  missing_chain: list[str],
                                  leaf_tag: str,
                                  fix_hint: str, ns: str,
                                  tmap: Optional[_XsdTypeMap],
                                  code: str, msg: str,
                                  rules_idx: Optional["_RulesIndex"] = None,
                                  path_parts: Optional[list[str]] = None,
                                  root: Optional[etree._Element] = None,
                                  msg_type: str = "") -> FixSuggestion:
        """
        Build a chain of missing elements from anchor_el down to leaf_tag.

        E.g. anchor=Dbtr, missing_chain=["PstlAdr"], leaf_tag="Ctry"
        → build <PstlAdr><Ctry>US</Ctry></PstlAdr> and append to Dbtr copy.

        Uses _build_child templates/codelists for the leaf, then wraps in
        each intermediate tag from innermost to outermost. The rules_idx is
        consulted so leaf/wrapper structures match the message-specific rules.
        """
        original_fragment = self._serialize(anchor_el)
        xpath             = self._xpath_of(anchor_el)

        # Guard: never try to build an element whose tag isn't a legal XML name
        # (e.g. a line number that slipped through as the target). Building it
        # would raise lxml "Invalid tag name" and, inside suggest_batch, abort
        # the whole batch. Defer to the LLM / decline instead of crashing.
        if not leaf_tag or not _VALID_XML_NAME.match(leaf_tag) or \
           any(not _VALID_XML_NAME.match(t) for t in (missing_chain or [])):
            return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint)

        # Build the leaf element (with KB templates + rules + document harvesting)
        leaf_el = self._build_child(leaf_tag, fix_hint, ns, tmap,
                                     rules_idx=rules_idx,
                                     path_parts=path_parts,
                                     rule_id=code, root=root,
                                     msg_type=msg_type)
        if leaf_el is None:
            return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint)

        # Wrap in intermediate missing elements (innermost first)
        # missing_chain = ["PstlAdr"] → wrap leaf in PstlAdr
        # missing_chain = ["X", "Y"]  → wrap leaf in Y, then wrap that in X
        inner = leaf_el
        for wrapper_tag in reversed(missing_chain):
            wrapper_el: Optional[etree._Element] = None

            # 1. Try the AI knowledge base for a message-specific wrapper template
            kb_tmpl = _kb_tag_template(wrapper_tag, msg_type)
            if kb_tmpl:
                try:
                    resolved = self._resolve_kb_placeholders(kb_tmpl, wrapper_tag, root)
                    wrapper_el = etree.fromstring(resolved.encode("utf-8"))
                    wrapper_el = self._apply_ns(wrapper_el, ns)
                except Exception:
                    wrapper_el = None

            # 2. Try the rules index for a message-specific wrapper structure
            if wrapper_el is None and rules_idx:
                rule = rules_idx.lookup(leaf_tag=wrapper_tag)
                if rule and rule.get("fix"):
                    raw = _extract_xml_from_fix(rule["fix"], wrapper_tag)
                    if raw:
                        raw = self._resolve_placeholders(raw, wrapper_tag, root)
                        try:
                            wrapper_el = etree.fromstring(raw.encode("utf-8"))
                            wrapper_el = self._apply_ns(wrapper_el, ns)
                        except Exception:
                            wrapper_el = None

            # 3. Fall back to the global template
            if wrapper_el is None:
                tmpl_str = _TEMPLATES.get(wrapper_tag)
                if tmpl_str:
                    try:
                        tmpl_str = _inject_fresh_uetrs(tmpl_str)
                        tmpl_str = self._resolve_placeholders(tmpl_str, wrapper_tag, root)
                        wrapper_el = etree.fromstring(tmpl_str.encode("utf-8"))
                        wrapper_el = self._apply_ns(wrapper_el, ns)
                    except Exception:
                        wrapper_el = None

            # If we built a structured wrapper, splice our inner element into it
            if wrapper_el is not None:
                inner_local = etree.QName(inner.tag).localname
                if self._child_exists(wrapper_el, inner_local) is not None:
                    for child in list(wrapper_el):
                        if etree.QName(child.tag).localname == inner_local:
                            wrapper_el.remove(child)
                    wrapper_el.append(inner)
                else:
                    wrapper_el.append(inner)
                inner = wrapper_el
                continue

            # 3. Bare wrapper as last resort
            tag = f"{{{ns}}}{wrapper_tag}" if ns else wrapper_tag
            wrapper = etree.Element(tag)
            wrapper.append(inner)
            inner = wrapper

        # Append the built subtree to a copy of anchor
        anchor_copy = self._copy(anchor_el)
        anchor_path = (path_parts[:-(len(missing_chain) + 1)]
                       if path_parts and len(path_parts) > len(missing_chain)
                       else None)
        _inner_local = etree.QName(inner.tag).localname
        _existing_in_anchor = self._child_exists(anchor_copy, _inner_local)
        if _existing_in_anchor is not None:
            # Replace existing child in-place instead of creating a duplicate
            _idx = list(anchor_copy).index(_existing_in_anchor)
            inner.tail = _existing_in_anchor.tail
            anchor_copy.remove(_existing_in_anchor)
            anchor_copy.insert(_idx, inner)
        else:
            insert_idx = self._find_insert_index(anchor_copy, _inner_local,
                                                  tmap, parent_path=anchor_path)
            if insert_idx is None:
                anchor_copy.append(inner)
            else:
                anchor_copy.insert(insert_idx, inner)

        return FixSuggestion(
            xpath=xpath,
            original_fragment=original_fragment,
            fragment_xml=self._serialize(anchor_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _find_insert_index(self, parent_copy: etree._Element,
                            new_tag: str,
                            tmap: Optional[_XsdTypeMap],
                            parent_path: Optional[list[str]] = None) -> Optional[int]:
        """
        Determine the correct insertion index for new_tag inside parent_copy,
        based on XSD sequence order. The parent's type is resolved via its path
        (ISO XSDs key local elements by parent type, so a name-only lookup
        fails); falls back to a global-element lookup. Returns None if order
        cannot be determined (caller appends).
        """
        if not tmap:
            return None
        parent_type = tmap.type_of_path(parent_path) if parent_path else None
        if not parent_type:
            parent_local = etree.QName(parent_copy.tag).localname
            parent_type  = tmap.element_type.get(parent_local)
        if not parent_type:
            return None
        info = tmap.type_info.get(parent_type, {})
        if info.get("kind") != "sequence":
            return None
        order = [c["name"] for c in info.get("children", [])]
        if new_tag not in order:
            return None
        new_pos = order.index(new_tag)
        # Find the first existing child whose order position is > new_pos
        for idx, child in enumerate(parent_copy):
            if not isinstance(child.tag, str):
                continue
            child_local = etree.QName(child.tag).localname
            if child_local in order and order.index(child_local) > new_pos:
                return idx
        return None  # append

    # ── _fix_attribute ────────────────────────────────────────────────────────

    # ── Field-constraint helpers ──────────────────────────────────────────────

    # Regex patterns matching the algorithms.json definitions
    _CONSTRAINT_REGEX: dict[str, str] = {
        "BICFI":      r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$",
        "AnyBIC":     r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$",
        "IBAN":       r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$",
        "LEI":        r"^[A-Z0-9]{18}[0-9]{2}$",
        "Currency":   r"^[A-Z]{3}$",
        "Country":    r"^[A-Z]{2}$",
        "Amount":     r"^\d{1,13}(\.\d{1,5})?$",
        "Date":       r"^\d{4}-\d{2}-\d{2}$",
        "DateTime":   r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$",
        "UUID":       r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        "Max35Text":  r"^.{1,35}$",
        "Max70Text":  r"^.{1,70}$",
        "Max140Text": r"^.{1,140}$",
        "Max16Text":  r"^.{1,16}$",
        "Numeric15":  r"^[0-9]{1,15}$",
        "ClrSysCd":   r"^[A-Za-z0-9]{1,5}$",
    }

    def _harvest_dependency_partner(self, root: etree._Element, my_xpath: str,
                                       tag_name: str, constraint: dict) -> Optional[str]:
        """
        Look through KB dependencies.equals — if THIS element's path matches
        one side of an equals dependency, harvest the value from the OTHER side.

        E.g. dep fields = [AppHdr.Fr...BICFI, Document.*.InstgAgt...BICFI]
        If my_xpath matches /AppHdr/Fr/..., harvest from /InstgAgt/...

        Distinguisher: a side's "discriminator" is the FIRST token that is
        NOT shared with the other sides (typically Fr/To, InstgAgt/InstdAgt).
        A candidate is only considered when its xpath contains the partner's
        discriminator AND does NOT contain THIS side's discriminator.
        """
        equals_deps = (_kb_get("dependencies.equals", []) or []) + _enterprise_dependencies_all("equals")
        my_xpath_lc = my_xpath.lower()

        def _tokens(field: str) -> list[str]:
            return [t for t in field.replace("*", "").split(".") if t]

        def _path_matches(needle: str, haystack_xpath_lc: str) -> bool:
            """Check that all tokens of `needle` appear (in order) in haystack."""
            tokens = _tokens(needle)
            pos = 0
            for tok in tokens:
                tok_l = tok.lower()
                idx = haystack_xpath_lc.find("/" + tok_l, pos)
                if idx == -1:
                    return False
                pos = idx + len(tok_l) + 1
            return True

        for dep in equals_deps:
            fields = dep.get("fields", []) or []
            if not isinstance(fields, list) or len(fields) < 2:
                continue

            # Find which side matches my_xpath
            my_side = None
            for i, f in enumerate(fields):
                if _path_matches(f, my_xpath_lc):
                    my_side = i
                    break
            if my_side is None:
                continue

            my_tokens   = _tokens(fields[my_side])
            my_token_set = {t.lower() for t in my_tokens}

            # Try to harvest from each OTHER side
            for j, other_f in enumerate(fields):
                if j == my_side:
                    continue
                other_tokens     = _tokens(other_f)
                if not other_tokens:
                    continue
                other_token_set  = {t.lower() for t in other_tokens}
                leaf             = other_tokens[-1]

                # Discriminators: tokens unique to one side vs the other
                my_only_tokens    = my_token_set - other_token_set
                other_only_tokens = other_token_set - my_token_set

                for cand in root.iter():
                    if not isinstance(cand.tag, str):
                        continue
                    if etree.QName(cand.tag).localname != leaf:
                        continue
                    if not cand.text:
                        continue
                    cand_xpath_lc = self._xpath_of(cand).lower()
                    if cand_xpath_lc == my_xpath_lc:
                        continue  # never match the element being fixed

                    # Candidate MUST contain ALL of the partner's discriminators
                    # (the tokens unique to the partner field). Requiring *all*
                    # — not *any* — is essential when two partner candidates
                    # share their container path and differ only by one segment.
                    #
                    # Example: fixing AppHdr/To/...BICFI, the partner field is
                    # Document.*.CdtTrfTxInf.InstdAgt...BICFI. Its unique tokens
                    # are {document, cdttrftxinf, instdagt}. The sibling
                    # InstgAgt candidate ALSO sits under document/cdttrftxinf,
                    # so an "any" test wrongly accepts it; only the "all" test
                    # (which requires `instdagt`) excludes it correctly.
                    if other_only_tokens and not all(
                        "/" + t in cand_xpath_lc for t in other_only_tokens
                    ):
                        continue
                    # Candidate MUST NOT contain any of THIS side's discriminators
                    if my_only_tokens and any(
                        "/" + t in cand_xpath_lc for t in my_only_tokens
                    ):
                        continue

                    txt = cand.text.strip()
                    if txt and not self._violates_constraint(txt, constraint):
                        return txt
        return None

    @staticmethod
    def _is_valid_calendar_date(value: str) -> bool:
        """Return True only if value represents a real calendar date/datetime (not just format-valid).
        Catches cases like 2026-06-00 (day=00) that pass \d{2} regex but are not valid dates."""
        import datetime as _dt
        date_part = value[:10]
        try:
            _dt.datetime.strptime(date_part, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def _violates_constraint(self, value: str, constraint: dict) -> bool:
        """Return True if `value` violates the KB field constraint."""
        if not isinstance(constraint, dict) or not value:
            return False
        ctype = constraint.get("type", "")
        # Codelist constraint
        if ctype == "codelist":
            valid = constraint.get("valid") or []
            if valid and value not in valid:
                return True
            return False
        # ISO code types must be a real code, not merely regex-shaped. 'UK'/'UN'
        # match [A-Z]{2} but are not valid ISO 3166 country codes (UK → GB).
        if ctype == "Country":
            codes = _codelist_codes("country")
            if codes:
                return value.upper() not in codes
        # Regex constraint
        pattern = self._CONSTRAINT_REGEX.get(ctype)
        if pattern and not re.match(pattern, value):
            return True
        # Calendar validation for date/datetime types: regex allows day=00, strptime does not
        if ctype in ("Date", "DateTime") and not self._is_valid_calendar_date(value):
            return True
        # Length constraints
        max_len = constraint.get("max_length")
        min_len = constraint.get("min_length")
        if isinstance(max_len, int) and len(value) > max_len:
            return True
        if isinstance(min_len, int) and len(value) < min_len:
            return True
        return False

    def _repair_country(self, el: Optional[etree._Element], cur_txt: str,
                        root: Optional[etree._Element]) -> str:
        """Return a valid ISO 3166-1 alpha-2 country code for an invalid/empty
        <Ctry>. Resolution order (KB-driven, country_repair in ai_knowledge_base):
          1. keep the current value if it is already a valid code;
          2. alias/name/typo map (UK/UNITEDKINGDOM → GB, USA → US, …);
          3. leading letters if they already form a valid code (GBR → GB);
          4. a valid country code harvested from elsewhere in the document;
          5. the KB `default` (e.g. GB).
        """
        codes = set(_codelist_codes("country"))
        cur = (cur_txt or "").strip().upper()
        if cur and (not codes or cur in codes):
            return cur
        key = re.sub(r"[^A-Z]", "", cur)
        aliases = _kb_get("country_repair.aliases", {}) or {}
        if key in aliases and (not codes or aliases[key] in codes):
            return aliases[key]
        if len(key) >= 2 and key[:2] in codes:
            return key[:2]
        if root is not None:
            for o in root.iter():
                if (isinstance(o.tag, str)
                        and etree.QName(o.tag).localname in ("Ctry", "CtryOfRes", "CtryOfBirth")
                        and o is not el and o.text):
                    t = o.text.strip().upper()
                    if t in codes:
                        return t
        default = _kb_get("country_repair.default", "GB")
        return default if (not codes or default in codes) else "GB"

    def _regenerate_value(self, tag_name: str, el: etree._Element,
                           constraint: dict, fix_hint: str, msg: str) -> Optional[str]:
        """
        Generate a constraint-compliant value for a leaf element.
        Priority:
          1. Harvest from a cross-referenced element via KB `equals` dependency
             (e.g. Fr.BICFI must equal InstgAgt.BICFI → harvest InstgAgt's BICFI).
          2. Harvest a valid same-tag value from elsewhere in the document.
          3. Tag-specific deterministic generators (UUID, BIC, currency...).
          4. Constraint preferred / example.
        """
        try:
            root = el.getroottree().getroot() if el.getroottree() is not None else None
        except Exception:
            root = None

        # Compute this element's full path for matching dependencies
        my_xpath = self._xpath_of(el)

        # 0a. BizSvc — variant-aware CBPR+ business service. A wrong value (e.g.
        #     'swift.cbprplus.02' on a pacs.009) trips the CBPR_P9_R6 enum rule;
        #     replace it with the value mandated for this message family + variant
        #     (.03 / .adv.03 / .cov.03) rather than a generic constraint example.
        if tag_name == "BizSvc" and root is not None:
            _xml = self._serialize(root)
            _bv = _cbpr_bizsvc_value(_detect_msg_type(_xml), _xml)
            if _bv and _bv != (el.text or "").strip():
                return _bv

        # 0. Length overflow → truncate the EXISTING value to KB/schema max_length.
        #    Preserves the user's actual data (just shortened) instead of replacing
        #    it with a dummy. For MaxText types (Nm, AdrLine, Ustrd, etc.) truncation
        #    alone always resolves the violation, so we accept it unconditionally
        #    when the type is a plain text type with no character-set regex.
        cur_txt = (el.text or "").strip()
        max_len = constraint.get("max_length") if isinstance(constraint, dict) else None
        if cur_txt and isinstance(max_len, int) and len(cur_txt) > max_len:
            truncated = cur_txt[:max_len].rstrip() or cur_txt[:max_len]
            # For plain MaxText constraints the truncated value is always valid
            # (only length was wrong); accept without re-checking the full constraint.
            _ctype0 = constraint.get("type", "")
            _is_maxtext = _ctype0.startswith("Max") and "Text" in _ctype0
            if truncated and (_is_maxtext
                              or not self._violates_constraint(truncated, constraint)):
                return truncated

        # 1. Cross-field harvesting via KB equals dependencies
        if root is not None:
            cross_val = self._harvest_dependency_partner(root, my_xpath, tag_name, constraint)
            if cross_val:
                return cross_val

        # 2. Harvest same-tag from elsewhere in the doc.
        # SKIP for Amount-type elements: each transaction carries its own distinct
        # amount; borrowing another transaction's value to "fix" a precision error
        # would silently replace a real amount (e.g. 444911.25) with a different
        # transaction's amount (e.g. 1000.00).  Repair in-place instead (Step 3).
        _ctype_for_harvest = constraint.get("type", "") if isinstance(constraint, dict) else ""
        _is_amount_tag = (
            _ctype_for_harvest == "Amount"
            or tag_name.endswith("Amt") or tag_name == "Amt"
        )
        if root is not None and not _is_amount_tag:
            for other in root.iter():
                if not isinstance(other.tag, str):
                    continue
                if (etree.QName(other.tag).localname == tag_name
                    and other is not el
                    and other.text):
                    txt = other.text.strip()
                    if txt and not self._violates_constraint(txt, constraint):
                        return txt

        # 3. Tag-specific generators
        tn = tag_name.lower()
        ctype = constraint.get("type", "")

        if ctype == "UUID" or "uetr" in tn:
            return str(uuid.uuid4())
        if ctype == "BICFI" or ctype == "AnyBIC" or "bic" in tn:
            banks = (_kb_get("dummy_data.banks", []) or
                     _enterprise_shared("dummy_data.banks", []) or [])
            if banks:
                return banks[0].get("bicfi", "DEUTDEFFXXX")
            return "DEUTDEFFXXX"
        if ctype == "IBAN" or "iban" in tn:
            return _iban_for_ccy(root, el)
        if ctype == "Currency":
            return "USD"
        if ctype == "Country":
            return "US"
        if ctype == "Amount" or tag_name.endswith("Amt") or tag_name == "Amt":
            # Repair the EXISTING value in-place: round to the ISO 4217 currency
            # precision (capped at 5, the XSD fractionDigits=5 maximum).
            # NEVER fall back to a dummy 1000.00 — if the existing value can be
            # parsed, always return a rounded form of it.
            _cur_a = (el.text or "").strip() if el is not None else ""
            if _cur_a:
                try:
                    _ccy_a = (el.get("Ccy") if el is not None else None) or ""
                    _ccy_a = _ccy_a.upper()
                    # Use currency.json precision; cap at 5 (XSD fractionDigits=5)
                    _prec_a = min(_ccy_precision(_ccy_a), 5)
                    # Strip trailing decimal point before parsing: "307845.85." → "307845.85"
                    _parse_a = _cur_a[:-1] if _cur_a.endswith(".") and len(_cur_a) > 1 else _cur_a
                    _num_a = float(_parse_a)
                    _repaired_a = f"{_num_a:.{_prec_a}f}"
                    if re.match(r"^\d{1,13}(\.\d{1,5})?$", _repaired_a):
                        return _repaired_a
                    # Integer part > 13 digits: try with fewer decimal places
                    for _fallback_prec in range(_prec_a - 1, -1, -1):
                        _fallback = f"{_num_a:.{_fallback_prec}f}"
                        if re.match(r"^\d{1,13}(\.\d{1,5})?$", _fallback):
                            return _fallback
                except (ValueError, TypeError):
                    pass
            # Fallback: use KB default amount rather than a country code
            return (_kb_get("dummy_data.amounts.default") or
                    _enterprise_shared("dummy_data.amounts.default") or "1000.00")
        if ctype == "Date":
            import datetime
            val = (_kb_get("dummy_data.dates.today") or
                   _enterprise_shared("dummy_data.dates.today") or "2026-05-27")
            if val == "USE_TODAY": return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
            return val
        if ctype == "DateTime":
            import datetime
            val = (_kb_get("dummy_data.dates.today_iso") or
                   _enterprise_shared("dummy_data.dates.now_offset") or "USE_NOW_OFFSET")
            if val in ("USE_NOW_OFFSET", "2026-05-27T10:00:00Z"):
                return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            return val
        if ctype == "Numeric15":
            return "1"
        if ctype == "LEI":
            return "529900T8BM49AURSDO55"

        # Codelist
        if ctype == "codelist":
            preferred = constraint.get("preferred")
            if preferred:
                return preferred
            valid = constraint.get("valid") or []
            if valid:
                return valid[0]

        # 3a. Per-message KB authoritative value (OFFLINE, no LLM). When the
        #     ai_knowledge_base constraint above produced nothing, the message's
        #     own validation KB — or, failing that, a sibling message's KB —
        #     frequently documents the expected value or an enum allow-list for
        #     this leaf. Consulting it here keeps many enum/value fixes working
        #     WITHOUT the LLM (the main offline-coverage gap). All candidates are
        #     re-checked against the constraint before use.
        if root is not None:
            try:
                _xml_r = self._serialize(root)
                _mt_r = _detect_msg_type(_xml_r)
                _kb_val = _kb_folder_leaf_value(tag_name, _mt_r, _xml_r)
                if (_kb_val and _kb_val != cur_txt
                        and not self._violates_constraint(_kb_val, constraint)):
                    return _kb_val
                _kbc = _KBContext.get(_mt_r)
                _codes = ((_kbc.valid_codes(tag_name) if _kbc else [])
                          or _cross_message_valid_codes(tag_name))
                if _codes:
                    _hint_l = f"{fix_hint} {msg}"
                    # A code named in the hint/message is an explicit signal — use
                    # it. Otherwise only fall back to the allow-list's first entry
                    # when the curated ai_knowledge_base constraint offers no
                    # preferred/example of its own (so a curated default still wins).
                    _hinted = next(
                        (c for c in _codes
                         if re.search(rf"\b{re.escape(c)}\b", _hint_l)), None)
                    _has_curated = bool(constraint.get("preferred")
                                        or constraint.get("example"))
                    _pick = _hinted or (None if _has_curated else _codes[0])
                    if (_pick and _pick != cur_txt
                            and not self._violates_constraint(_pick, constraint)):
                        return _pick
            except Exception:
                pass

        # 3. Constraint preferred / example
        if constraint.get("preferred"):
            return constraint["preferred"]
        if constraint.get("example"):
            return constraint["example"]

        # 4. Truncate to max length if the only issue is length
        max_len = constraint.get("max_length")
        if isinstance(max_len, int) and el.text and len(el.text.strip()) > max_len:
            return el.text.strip()[:max_len]

        return self._placeholder(tag_name)

    def _local_name_path(self, el: etree._Element) -> list:
        """Return the local-name path from the document root to `el`."""
        parts = []
        cur = el
        while cur is not None and isinstance(cur.tag, str):
            parts.append(etree.QName(cur.tag).localname)
            cur = cur.getparent()
        return list(reversed(parts))

    # ── Stray-text / element-only fix ────────────────────────────────────────

    def _fix_elements_inside_simple_leaf(
        self, root: etree._Element, xml: str, code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """Remove element children that were wrongly injected into SIMPLE-type
        leaves (e.g. <Othr><Id>ACCT…<IBAN>dummy</IBAN></Id></Othr>).

        A simple-type leaf (Max34Text, identifiers, …) may carry only text.
        Element children inside one are always repair artifacts — an earlier
        fix or lxml recovery landed elements in the wrong same-named tag
        (the generic-<Id> collision). The leaf's TEXT is the real user data:
        keep it, drop the injected children. All affected leaves are repaired
        in ONE fix targeted at their deepest common ancestor, so the loop
        converges in a single round instead of one round per leaf.
        """
        xsd_path = self._get_xsd_path(xml)
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        if tmap is None:
            return None

        def _is_simple_type(el) -> bool:
            t = tmap.type_of_path(self._local_name_path(el))
            if not t:
                return False
            info = tmap.type_info.get(t)
            if info is not None:
                return info.get("kind") == "simple"
            # Type not parsed locally (xs built-ins / external simple types):
            # recognise the standard ISO 20022 simple-type naming.
            return bool(re.match(
                r"^(Max\d+\w*Text|.*Identifier|.*Code|ISO\w*|.*Indicator"
                r"|.*Amount|.*Rate|.*Number)$", t))

        corrupted = [el for el in root.iter()
                     if isinstance(el.tag, str)
                     and any(isinstance(c.tag, str) for c in el)
                     and (el.text or "").strip()
                     and _is_simple_type(el)]
        if not corrupted:
            return None

        # Deepest common ancestor of all corrupted leaves.
        def _chain(el):
            out = []
            while el is not None:
                out.append(el)
                el = el.getparent()
            return list(reversed(out))

        chains = [_chain(el) for el in corrupted]
        anc = None
        for level in zip(*chains):
            if all(e is level[0] for e in level):
                anc = level[0]
            else:
                break
        if anc is None:
            return None
        # Target the ancestor's PARENT-side fragment only if anc is a corrupted
        # leaf itself (single-leaf case) — fragment must contain the children.
        if anc in corrupted and anc.getparent() is not None:
            anc = anc.getparent()

        original_fragment = self._serialize(anc)
        anc_copy = self._copy(anc)
        changed = False
        for leaf in corrupted:
            idx_path = self._index_path_to(anc, leaf)
            if idx_path is None:
                continue
            leaf_copy = self._navigate_to(anc_copy, idx_path)
            if leaf_copy is None:
                continue
            for ch in list(leaf_copy):
                if isinstance(ch.tag, str):
                    leaf_copy.remove(ch)
                    changed = True
            leaf_copy.text = (leaf_copy.text or "").strip()
        if not changed:
            return None

        return FixSuggestion(
            xpath=self._xpath_of(anc),
            original_fragment=original_fragment,
            fragment_xml=self._serialize(anc_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    def _rewrap_stray_text(self, target: etree._Element,
                           target_copy: etree._Element, xml: str) -> bool:
        """Re-wrap orphaned text inside an element-only container into the XSD
        children MISSING at that position.

        When an element's open+close tags are both deleted, its text value is
        left behind as stray text in the parent (e.g. PmtId carrying bare lines
        'INSTR…', 'E2E…', 'TX…' where InstrId/EndToEndId/TxId used to be).
        Plain stripping discards that user data; instead, map each orphan line
        to the XSD child slots between the surrounding surviving children:
          • exact fit (lines == missing slots in the gap) → assign in sequence
            order;
          • otherwise, assign only when the lines match the gap's MANDATORY
            missing children exactly.
        Lines that can't be placed are left for the caller's strip pass.
        Returns True when at least one line was re-wrapped (in target_copy).
        """
        if not xml:
            try:
                xml = self._serialize(target.getroottree().getroot())
            except Exception:
                return False
        xsd_path = self._get_xsd_path(xml)
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        if tmap is None:
            return False
        t_type = tmap.type_of_path(self._local_name_path(target))
        t_children = tmap.type_info.get(t_type, {}).get("children", [])
        seq = [c["name"] for c in t_children]
        if not seq:
            return False
        seq_pos = {n: i for i, n in enumerate(seq)}
        min_of = {c["name"]: c.get("min", "1") for c in t_children}
        ns = etree.QName(target_copy.tag).namespace or ""

        kids = [c for c in target_copy if isinstance(c.tag, str)]
        exist = [etree.QName(c.tag).localname for c in kids]
        present = set(exist)

        changed = False
        # Slot 0 = target_copy.text (before first child); slot i+1 = kids[i].tail.
        for slot in range(len(kids) + 1):
            raw = target_copy.text if slot == 0 else kids[slot - 1].tail
            if not raw or not raw.strip():
                continue
            lines = [ln.strip() for ln in raw.splitlines()
                     if ln.strip() and len(ln.strip()) <= 140]
            if not lines:
                continue
            # XSD window for this gap: names after the preceding survivor and
            # before the nearest following survivor, not already present.
            lo = seq_pos.get(exist[slot - 1], -1) if slot > 0 else -1
            hi = min((seq_pos[e] for e in exist[slot:] if e in seq_pos),
                     default=len(seq))
            window = [n for n in seq[lo + 1:hi] if n not in present]
            if len(lines) == len(window):
                chosen = window
            else:
                mand = [n for n in window if min_of.get(n, "1") != "0"]
                if mand and len(lines) == len(mand):
                    chosen = mand
                else:
                    continue
            insert_at = (list(target_copy).index(kids[slot - 1]) + 1
                         if slot > 0 else 0)
            for j, (val, name) in enumerate(zip(lines, chosen)):
                tag = f"{{{ns}}}{name}" if ns else name
                new_child = etree.Element(tag)
                new_child.text = val
                target_copy.insert(insert_at + j, new_child)
                present.add(name)
            if slot == 0:
                target_copy.text = None
            else:
                kids[slot - 1].tail = None
            changed = True
        return changed

    def _fix_stray_text_element_only(
        self,
        root: etree._Element,
        container_name: str,
        code: str,
        msg: str,
        xml: str = "",
    ) -> Optional[FixSuggestion]:
        """
        Strip non-whitespace text/tail nodes from every element-only container
        that has stray character content, causing SCHEMA_VAL "Character content
        other than whitespace is not allowed because the content type is
        'element-only'".

        A stray text node can appear in two places inside an element-only
        container `<Parent>…</Parent>`:
          1.  Parent.text      — text directly after <Parent> before first child
          2.  child.tail       — text after </child> and before the next sibling
                                 or </Parent>

        Both are stripped (set to None or a single whitespace) so the document
        remains well-formed and the schema error disappears.

        `container_name` is the local-name extracted from the error message
        ("Field 'Document'" → "Document"). When empty, ALL element-only
        containers in the tree are scanned (broader recovery path).
        """
        lh = getattr(self, "_line_hint", None)

        def _has_stray(el: etree._Element) -> bool:
            """Return True when el carries non-whitespace text/tail on itself or any child tail."""
            if el.text and el.text.strip():
                return True
            for child in el:
                if isinstance(child.tag, str) and child.tail and child.tail.strip():
                    return True
            return False

        def _strip_stray(el_copy: etree._Element) -> bool:
            """In-place strip of non-whitespace text/tail; return True if changed."""
            changed = False
            if el_copy.text and el_copy.text.strip():
                el_copy.text = None
                changed = True
            for child in el_copy:
                if isinstance(child.tag, str) and child.tail and child.tail.strip():
                    child.tail = None
                    changed = True
            return changed

        # Collect candidates: named container first, else all element-only containers.
        # "Element-only" heuristic: any element with at least one child element and
        # no meaningful text of its own (or a name we KNOW is element-only).
        _EO_NAMES = {
            "Document", "BusMsgEnvlp", "AppHdr", "GrpHdr", "SttlmInf",
            "FIToFICstmrCdtTrf", "FIToFIPmtStsRpt", "FIToFICustomerCreditTransfer",
            "FICdtTrf", "CdtTrfTxInf", "TxInfAndSts", "FIToFIPmtStsRpt",
            "OrgnlGrpInfAndSts", "OrgnlPmtInfAndSts", "PmtInf",
            "DrctDbtTxInf", "Ntfctn", "Itm", "Assgnmt", "Undrlyg", "TxInf",
            "CdtInstr", "DrctDbtTxInf", "Strd", "RfrdDocInf",
        }
        candidates: list[etree._Element] = []
        for el in root.iter():
            if not isinstance(el.tag, str):
                continue
            local = etree.QName(el.tag).localname
            if container_name:
                if local != container_name:
                    continue
            else:
                # Broad scan: only elements with child elements (element-only pattern)
                has_child_els = any(isinstance(c.tag, str) for c in el)
                if not has_child_els:
                    continue
                if local not in _EO_NAMES and not local.endswith(("Inf", "Rpt", "Hdr", "Tx", "Sts")):
                    continue
            if _has_stray(el):
                candidates.append(el)

        if not candidates:
            return None

        # Pick the line-nearest candidate when lh is available.
        target = (min(candidates, key=lambda e: abs((e.sourceline or 0) - lh))
                  if lh is not None else candidates[0])

        original_fragment = self._serialize(target)
        target_copy = self._copy(target)

        # Data-preserving pass first: orphan text lines are usually the VALUES
        # of children whose tags were deleted — re-wrap them into the missing
        # XSD children instead of discarding user data. Whatever cannot be
        # placed is then stripped as before.
        try:
            _rw_changed = self._rewrap_stray_text(target, target_copy, xml)
        except Exception:
            _rw_changed = False
        _st_changed = _strip_stray(target_copy)
        if not (_rw_changed or _st_changed):
            return None

        return FixSuggestion(
            xpath=self._xpath_of(target),
            original_fragment=original_fragment,
            fragment_xml=self._serialize(target_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    # Tags that are valid children of an account <Id> element
    # (AccountIdentification4Choice / GenericAccountIdentification1)
    _VALID_ACCT_ID_CHILDREN: frozenset = frozenset({
        "IBAN", "Othr", "PrtryAcct", "BBAN", "UPIC",
    })

    # Account container tags whose <Id> must only hold account identifiers.
    # Any other child absorbed by the balance engine belongs in the parent tx block.
    _ACCT_CONTAINERS: frozenset = frozenset({
        "CdtrAcct", "DbtrAcct", "CdtrAgtAcct", "DbtrAgtAcct",
        "IntrmyAgt1Acct", "IntrmyAgt2Acct", "IntrmyAgt3Acct",
    })

    # Correct child order inside DrctDbtTxInf (pacs.010)
    _DRCT_DBT_TX_INF_ORDER: tuple = (
        "PmtId", "PmtTpInf", "IntrBkSttlmAmt", "IntrBkSttlmDt",
        "SttlmPrty", "SttlmTmIndctn", "SttlmTmReq", "AccptncDtTm",
        "PoolgAdjstmntDt", "InstdAmt", "XchgRate", "ChrgBr", "ChrgsInf",
        "CdtrAgt", "CdtrAgtAcct", "Cdtr", "CdtrAcct",
        "DbtrAgt", "DbtrAgtAcct", "DrctDbtTx", "Dbtr", "DbtrAcct", "RmtInf",
        "SplmtryData",
    )

    # Correct child order inside CdtTrfTxInf (pacs.008/pacs.009)
    _CDT_TRF_TX_INF_ORDER: tuple = (
        "PmtId", "PmtTpInf", "IntrBkSttlmAmt", "IntrBkSttlmDt",
        "SttlmPrty", "SttlmTmIndctn", "SttlmTmReq", "AccptncDtTm",
        "PoolgAdjstmntDt", "InstdAmt", "XchgRate", "ChrgBr", "ChrgsInf",
        "PrvsInstgAgt1", "PrvsInstgAgt1Acct", "PrvsInstgAgt2", "PrvsInstgAgt2Acct",
        "PrvsInstgAgt3", "PrvsInstgAgt3Acct",
        "InstgAgt", "InstdAgt",
        "IntrmyAgt1", "IntrmyAgt1Acct", "IntrmyAgt2", "IntrmyAgt2Acct",
        "IntrmyAgt3", "IntrmyAgt3Acct",
        "Dbtr", "DbtrAcct", "DbtrAgt", "DbtrAgtAcct",
        "CdtrAgt", "CdtrAgtAcct", "Cdtr", "CdtrAcct",
        "InstrForCdtrAgt", "InstrForNxtAgt", "Purp", "RmtInf",
        "UndrlygCstmrCdtTrf", "SplmtryData",
    )

    # Tags that signal a transaction-level container (elements should route here)
    _TX_CONTAINER_TAGS: frozenset = frozenset({
        "CdtTrfTxInf", "DrctDbtTxInf", "CdtInstr", "TxInf", "OrgnlTxRef",
    })

    def _try_rescue_collapsed_account_id(
        self, root: etree._Element, xml: str,
    ) -> Optional[FixSuggestion]:
        """Rescue elements absorbed into any account container's <Id> during
        structural collapse (large deletion → balance engine nests subsequent
        siblings inside an unclosed <Id>).

        Handles: CdtrAcct, DbtrAcct, CdtrAgtAcct, DbtrAgtAcct, IntrmyAgt*Acct.
        Valid children of <Id>: IBAN, Othr, PrtryAcct, BBAN, UPIC.
        Anything else is extracted to the nearest transaction-level parent
        (CdtTrfTxInf, DrctDbtTxInf, …), then re-ordered per XSD sequence.
        """
        root_copy = self._copy(root)
        changed = False

        for acct_el in list(root_copy.iter()):
            if not isinstance(acct_el.tag, str):
                continue
            acct_local = etree.QName(acct_el.tag).localname
            if acct_local not in self._ACCT_CONTAINERS:
                continue
            id_el = next(
                (c for c in acct_el
                 if isinstance(c.tag, str) and etree.QName(c.tag).localname == "Id"),
                None,
            )
            if id_el is None:
                continue

            misplaced = [
                c for c in list(id_el)
                if isinstance(c.tag, str)
                and etree.QName(c.tag).localname not in self._VALID_ACCT_ID_CHILDREN
            ]
            if not misplaced:
                continue

            # Walk up to find the nearest transaction-level container
            target = None
            cur = acct_el.getparent()
            while cur is not None:
                if isinstance(cur.tag, str) and etree.QName(cur.tag).localname in self._TX_CONTAINER_TAGS:
                    target = cur
                    break
                cur = cur.getparent()

            if target is None:
                # Fallback: use direct parent of the account element
                target = acct_el.getparent()
            if target is None:
                continue

            target_local = etree.QName(target.tag).localname if isinstance(target.tag, str) else ""
            order_tuple = (
                self._DRCT_DBT_TX_INF_ORDER if target_local == "DrctDbtTxInf"
                else self._CDT_TRF_TX_INF_ORDER
            )

            for el in misplaced:
                el_local = etree.QName(el.tag).localname
                # Drop empty tx-container shells — balance-engine artefacts
                if (el_local in self._TX_CONTAINER_TAGS
                        and len(el) == 0 and not (el.text or "").strip()):
                    id_el.remove(el)
                    changed = True
                    continue
                # Drop empty UndrlygCstmrCdtTrf shells (COV artefact)
                if el_local == "UndrlygCstmrCdtTrf" and len(el) == 0 and not (el.text or "").strip():
                    id_el.remove(el)
                    changed = True
                    continue
                id_el.remove(el)
                target.append(el)
                changed = True

            if changed:
                order_map = {name: i for i, name in enumerate(order_tuple)}
                children = list(target)
                children.sort(
                    key=lambda c: order_map.get(
                        etree.QName(c.tag).localname if isinstance(c.tag, str) else "", 999
                    )
                )
                for ch in children:
                    target.remove(ch)
                for ch in children:
                    target.append(ch)

        if not changed:
            return None

        fixed_xml = etree.tostring(root_copy, encoding="unicode", pretty_print=True)
        decl_m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
        if decl_m:
            fixed_xml = decl_m.group(1) + "\n" + fixed_xml
        return FixSuggestion(
            xpath="/",
            original_fragment=xml,
            fragment_xml=fixed_xml,
            issue_code="STRUCTURE_ERROR",
            issue_message=(
                "Structural rescue: elements absorbed into account <Id> during deletion "
                "moved to correct transaction container."
            ),
            confidence="high",
        )

    def _try_collapse_choice(self, root: etree._Element, code: str,
                             msg: str) -> Optional[FixSuggestion]:
        """Collapse an over-populated XML Choice container to a single member.

        AccountIdentification4Choice (<Id> under any *Acct) permits EXACTLY ONE
        of <IBAN> or <Othr>. When an edit leaves BOTH, the validator flags the
        second as "element 'X' is not expected at this position". Unlike a blind
        choice-member deletion (which the caller refuses to guess), this is
        unambiguous — one member must go. Keep whichever member is VALID (an
        IBAN passing MOD-97, else an <Othr> with a populated <Id>), preferring
        IBAN when both are valid, and drop the competing member.

        Returns one fix for the single <Id> tied to this error (nearest the
        reported line); the iterative loop handles any further occurrences.
        """
        # Matches both the raw libxml2 phrasing ("element 'X' is not expected")
        # and Layer2Mixin._simplify_error_message's friendly rewrite
        # ("Unexpected field 'X' found here" / "Unexpected field 'X'. It is in
        # the wrong place..." — see layer2_validator.py _simplify_error_message),
        # which SR2026 reuses verbatim (see project_sr2026_header_val memory).
        m = re.search(r"element '([\w:{}.\-]+)' is not expected", msg, re.I) \
            or re.search(r"[Uu]nexpected field '([\w:{}.\-]+)'", msg)
        off_local = m.group(1).split('}')[-1].split(':')[-1] if m else ""
        if off_local not in ("IBAN", "Othr"):
            return None

        # AccountIdentification4Choice == an <Id> carrying BOTH IBAN and Othr.
        cands = []
        for el in root.iter():
            if not isinstance(el.tag, str) or etree.QName(el.tag).localname != "Id":
                continue
            kids = [etree.QName(c.tag).localname for c in el if isinstance(c.tag, str)]
            if "IBAN" in kids and "Othr" in kids:
                cands.append(el)
        if not cands:
            return None

        lh = getattr(self, "_line_hint", None)
        if lh is not None:
            def _near(el):
                lines = [c.sourceline or 0 for c in el] + [el.sourceline or 0]
                return min(abs(l - lh) for l in lines)
            target = min(cands, key=_near)
        else:
            target = cands[0]

        iban_el = next((c for c in target if isinstance(c.tag, str)
                        and etree.QName(c.tag).localname == "IBAN"), None)
        othr_el = next((c for c in target if isinstance(c.tag, str)
                        and etree.QName(c.tag).localname == "Othr"), None)
        iban_ok = iban_el is not None and _verify_iban_mod97((iban_el.text or "").strip())
        othr_ok = othr_el is not None and any(
            isinstance(g.tag, str) and etree.QName(g.tag).localname == "Id"
            and (g.text or "").strip() for g in othr_el)
        # Keep whichever member is valid; prefer IBAN when both are valid;
        # default to IBAN when neither validates cleanly.
        keep = "IBAN" if iban_ok else ("Othr" if othr_ok else "IBAN")

        pcopy = self._copy(target)
        for c in list(pcopy):
            if (isinstance(c.tag, str)
                    and etree.QName(c.tag).localname in ("IBAN", "Othr")
                    and etree.QName(c.tag).localname != keep):
                pcopy.remove(c)
        if self._serialize(pcopy) == self._serialize(target):
            return None
        return FixSuggestion(
            self._xpath_of(target), self._serialize(target),
            self._serialize(pcopy), code, msg, "high",
        )

    def _try_reposition_element(self, root: etree._Element, xml: str, code: str,
                                msg: str, msg_type: str) -> Optional[FixSuggestion]:
        """Repair a "not expected at this position" error with NO expected-list
        when the offending element sits at the WRONG hierarchy level — i.e. it is
        not a valid child of its parent, but one of the parent's child containers
        DOES allow it per the XSD. The element is moved into that container
        (built with its template children), preserving the original value, and
        both levels are reordered to the XSD sequence.

        Example: <FinInstnId><Ctry>GB</Ctry></FinInstnId> → Ctry is invalid
        directly under FinInstnId, but FinInstnId/PstlAdr allows Ctry, so it
        becomes <FinInstnId><PstlAdr>…<Ctry>GB</Ctry></PstlAdr></FinInstnId>.
        Schema-generic (uses the XSD type map), works for all message types.
        """
        # Matches both the raw libxml2 phrasing ("element 'X' is not expected"/
        # "is not allowed") and Layer2Mixin._simplify_error_message's friendly
        # rewrite ("Unexpected field 'X' found here" / "Unexpected field 'X'.
        # It is in the wrong place..."), which SR2026 reuses verbatim (see
        # project_sr2026_header_val memory). The friendly rewrite never embeds
        # the expected-list IN `msg` itself (it lives in the separate fix_hint
        # field — see suggest()'s call site, which still only passes `msg`
        # here), so this function's own "following element" guard below only
        # ever sees that list for the raw-libxml2 message shape, same as before.
        m = (re.search(r"element '([^']+)' is not (?:expected|allowed)", msg, re.I)
             or re.search(r"[Uu]nexpected field '([^']+)'", msg))
        if not m:
            return None
        # If the validator supplied an expected list, the dedicated sequence/
        # sibling handlers own it — only act on the no-list (pure position) case.
        if re.search(r"following element", msg, re.I):
            return None
        offending = m.group(1).split('}')[-1].split(':')[-1].strip().strip("':\" ")
        if not offending:
            return None

        matches = [el for el in root.iter()
                   if isinstance(el.tag, str) and etree.QName(el.tag).localname == offending]
        if not matches:
            return None
        off_el = self._pick_candidate(matches)
        parent = off_el.getparent()
        if parent is None:
            return None

        parent_path = self._local_name_path(parent)
        ns = etree.QName(parent.tag).namespace or ""
        _pa_ah_idx = next((i for i, p in enumerate(parent_path) if p == "AppHdr"), -1)
        _pa_doc_after = any(p == "Document" for p in parent_path[_pa_ah_idx + 1:]) if _pa_ah_idx >= 0 else False
        in_apphdr = _pa_ah_idx >= 0 and not _pa_doc_after
        xsd_path = (self._get_apphdr_xsd_path(xml) if in_apphdr else self._get_xsd_path(xml))
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        if not tmap:
            return None
        parent_type = tmap.type_of_path(parent_path)
        if not parent_type:
            return None
        parent_children = tmap.type_info.get(parent_type, {}).get("children", [])
        valid_names = [c["name"] for c in parent_children]
        if offending in valid_names:
            return None  # genuinely a valid child → ordering case, not wrong-level

        def local(t) -> str:
            return etree.QName(t).localname if isinstance(t, str) else ""

        # Find the parent's child container whose type ALLOWS the offending element.
        for c in parent_children:
            gtype = c.get("type", "")
            gnames = [g["name"] for g in tmap.type_info.get(gtype, {}).get("children", [])]
            if offending not in gnames:
                continue
            wrapper_name = c["name"]
            parent_copy = self._copy(parent)
            copy_off = next((ch for ch in parent_copy if local(ch.tag) == offending), None)
            if copy_off is None:
                return None
            parent_copy.remove(copy_off)
            gorder = [g["name"] for g in tmap.type_info.get(gtype, {}).get("children", [])]
            wrapper = next((ch for ch in parent_copy if local(ch.tag) == wrapper_name), None)
            if wrapper is None:
                wrapper = self._build_child(wrapper_name, "", ns, tmap,
                                            path_parts=parent_path + [wrapper_name],
                                            root=root, msg_type=msg_type)
                if wrapper is None:
                    wrapper = etree.Element(f"{{{ns}}}{wrapper_name}" if ns else wrapper_name)
                # Replace any placeholder occurrence of the same tag with the real one.
                for ch in list(wrapper):
                    if local(ch.tag) == offending:
                        wrapper.remove(ch)
                wrapper.append(copy_off)
                self._reorder_children(wrapper, gorder)
                parent_copy.append(wrapper)
            else:
                for ch in list(wrapper):
                    if local(ch.tag) == offending:
                        wrapper.remove(ch)
                wrapper.append(copy_off)
                self._reorder_children(wrapper, gorder)
            self._reorder_children(parent_copy, valid_names)
            return FixSuggestion(self._xpath_of(parent), self._serialize(parent),
                                 self._serialize(parent_copy), code, msg, "high")
        return None

    def _try_remove_duplicate(self, root: etree._Element, code: str,
                              msg: str, xml: str = "") -> Optional[FixSuggestion]:
        """Resolve duplicate-element errors (rules: auto_fix_rules.duplicate).

        Handles both the explicit "Duplicate tag detected: <BICFI>" error and a
        sequence error that names an element already present earlier in the same
        parent (e.g. a second <BICFI> reported as "not expected here"). Keeps the
        first valid (non-empty) occurrence and removes the extras. Schema-generic.
        """
        rules = _kb_get("auto_fix_rules.duplicate", []) or []
        msg_l = msg.lower()
        is_dup = any(code in (r.get("codes") or [])
                     or (r.get("message_pattern") and re.search(r["message_pattern"], msg, re.I))
                     for r in rules) or ("duplicate" in msg_l)

        # Identify the duplicated tag name from the message. Prefer an explicit
        # <Tag> (e.g. "Duplicate tag detected: <BICFI>"), then a quoted element
        # name from a sequence error ("element 'BICFI' is not expected here").
        tag = None
        m = re.search(r"<(\w+)\s*/?>", msg)
        if m:
            tag = m.group(1)
        if tag is None:
            m2 = re.search(r"element '([^']+)' is not expected here", msg, re.I)
            if m2:
                tag = m2.group(1).strip().strip("':\" ")
        if tag is None and is_dup:
            m3 = re.search(r"duplicate (?:tag|element)\s*[:\-]?\s*['\"]?(\w+)", msg, re.I)
            if m3 and m3.group(1).lower() not in ("tag", "element", "detected"):
                tag = m3.group(1)
        if tag is None:
            return None

        def local(t) -> str:
            return etree.QName(t).localname if isinstance(t, str) else ""

        # Find a parent that actually holds more than one child with this name.
        for parent in root.iter():
            dups = [c for c in parent if isinstance(c.tag, str) and local(c.tag) == tag]
            if len(dups) <= 1:
                continue
            # Only act on a sequence "not expected" message if it's truly a
            # duplicate (handled by the len check above); the explicit duplicate
            # message always proceeds.
            if not (is_dup or len(dups) > 1):
                continue
            parent_copy = self._copy(parent)
            copy_dups = [c for c in parent_copy if isinstance(c.tag, str) and local(c.tag) == tag]
            keep = next((c for c in copy_dups if (c.text or "").strip() or len(c)), copy_dups[0])
            for c in copy_dups:
                if c is not keep:
                    parent_copy.remove(c)
            return FixSuggestion(self._xpath_of(parent), self._serialize(parent),
                                 self._serialize(parent_copy), code, msg, "high")

        # ── Parent-child nesting: tag T nested directly inside another T ──────
        # Handles <FIId><FIId>...</FIId><FinInstnId/></FIId> — the inner T is
        # an extra wrapper layer. Remove it; non-T siblings of the outer T are
        # kept untouched. Only fires when the message explicitly signals a
        # duplicate (is_dup=True) so it never triggers on unrelated sequence errors.
        if is_dup:
            for el in root.iter():
                if not isinstance(el.tag, str) or local(el.tag) != tag:
                    continue
                parent = el.getparent()
                if parent is None or not isinstance(parent.tag, str):
                    continue
                if local(parent.tag) != tag:
                    continue
                # el is tag T directly inside another T — remove the inner wrapper
                parent_copy = self._copy(parent)
                inner_copies = [c for c in parent_copy
                                if isinstance(c.tag, str) and local(c.tag) == tag]
                for inner in inner_copies:
                    parent_copy.remove(inner)
                return FixSuggestion(
                    self._xpath_of(parent), self._serialize(parent),
                    self._serialize(parent_copy), code, msg, "high",
                )

        # ── Cross-parent duplicate: no single parent holds two <tag> children ──
        # The "duplicate" is really a STRAY element at the wrong nesting level
        # (e.g. a bare <BICFI> under CdtTrfTxInf after its agent wrappers were
        # deleted, duplicating the BICFI inside a sibling agent's FinInstnId).
        # Two repairs, data-preserving first:
        #   1. strip repair-artifact children out of simple-type leaves
        #      (the <Othr><Id>ACCT…<IBAN>dummy</IBAN></Id> corruption);
        #   2. wrap the stray into the missing container the XSD expects
        #      (PrvsInstgAgt1 → FinInstnId → BICFI).
        if is_dup:
            if not xml:
                try:
                    xml = self._serialize(root)
                except Exception:
                    return None
            leaf_fix = self._fix_elements_inside_simple_leaf(root, xml, code, msg)
            if leaf_fix is not None:
                return leaf_fix
            # Point the wrap at the STRAY occurrence — the one whose parent
            # does not accept it — rather than whichever same-named element
            # the validator's line number happens to sit closest to. The issue
            # path (if any) describes the duplicate report, not the stray, so
            # it is cleared for the scoped call.
            _saved_lh = getattr(self, "_line_hint", None)
            _saved_ip = getattr(self, "_issue_path", "")
            _xsd_path = self._get_xsd_path(xml)
            _tmap = _XsdTypeMap.get(_xsd_path) if _xsd_path else None
            if _tmap is not None:
                for el in root.iter():
                    if not isinstance(el.tag, str) or local(el.tag) != tag:
                        continue
                    par = el.getparent()
                    if par is None or not isinstance(par.tag, str):
                        continue
                    ptype = _tmap.type_of_path(self._local_name_path(par))
                    if not ptype:
                        continue
                    pkids = {c["name"] for c in
                             _tmap.type_info.get(ptype, {}).get("children", [])}
                    if pkids and tag not in pkids:
                        self._line_hint = el.sourceline or _saved_lh
                        self._issue_path = ""
                        break
            wrap_msg = f"The element '{tag}' is not expected at this position."
            try:
                wrap_fix = self._try_wrap_orphaned_block(root, xml, code, wrap_msg)
            finally:
                self._line_hint = _saved_lh
                self._issue_path = _saved_ip
            if wrap_fix is not None:
                return wrap_fix
        return None

    def _index_path_to(self, ancestor: etree._Element, descendant: etree._Element) -> Optional[list]:
        """Return a navigation path from ancestor to descendant as a list of
        (localname, 0-based-index-among-same-tag-siblings) tuples.
        Returns None if descendant is not under ancestor."""
        path: list = []
        cur = descendant
        while cur is not None and cur is not ancestor:
            parent = cur.getparent()
            if parent is None:
                return None
            local = etree.QName(cur.tag).localname
            same = [c for c in parent if isinstance(c.tag, str)
                    and etree.QName(c.tag).localname == local]
            try:
                idx = same.index(cur)
            except ValueError:
                return None
            path.insert(0, (local, idx))
            cur = parent
        return path if cur is ancestor else None

    def _navigate_to(self, root_copy: etree._Element, path: list) -> Optional[etree._Element]:
        """Navigate a copied subtree using the path returned by _index_path_to."""
        cur = root_copy
        for (local, idx) in path:
            same = [c for c in cur if isinstance(c.tag, str)
                    and etree.QName(c.tag).localname == local]
            if idx >= len(same):
                return None
            cur = same[idx]
        return cur

    def _try_sequence_fix(self, root: etree._Element, xml: str, code: str,
                          msg: str, ns: str, msg_type: str,
                          rules_idx: Optional["_RulesIndex"]) -> Optional[FixSuggestion]:
        """Repair XSD sequence/order violations of the form:

          "The element 'To' is not expected here ... One of the following
           elements is expected: 'CharSet, Fr'."

        Strategy (schema-driven, works for any message type / AppHdr):
          1. Parse the offending element and the expected element list.
          2. Insert the expected element(s) that are missing from the parent —
             preferring mandatory ones (minOccurs >= 1) per the XSD.
          3. Reorder the parent's children to the XSD sequence order.

        The element is reconstructed via the XSD type map, so it is built with
        the correct child structure and valid datatypes for ALL MX schemas.
        """
        if not re.search(r"is not expected here|not expected at this position|following element|missing before", msg, re.I):
            return None
        m_off = re.search(r"element '([^']+)' is not expected(?:\s+here|\s+at this position)", msg, re.I)
        if not m_off:
            return None
        offending = m_off.group(1).strip().strip("':\" ")
        # The expected-element list only appears in lxml's VERBOSE variant
        # ("One of the following elements is expected: 'A, B'"). The TERSE variant
        # ("... or another element was expected before it.") names nothing — there
        # we leave `expected` empty and derive the missing mandatory predecessor(s)
        # from the XSD sequence below (Case 2).
        m_exp = re.search(r"following elements?\s*(?:is|are)?\s*expected\s*:?\s*'?([^'\n]+?)'?\.?\s*$",
                          msg, re.I)
        expected: list[str] = []
        if m_exp:
            expected = [e.strip().strip("':\" ")
                        for e in re.split(r"[,/]| or ", m_exp.group(1))
                        if e.strip().strip("':\" ")]

        _off_cands = [el for el in root.iter()
                      if isinstance(el.tag, str)
                      and etree.QName(el.tag).localname == offending]
        if not _off_cands:
            return None
        off_el = self._pick_candidate(_off_cands)
        parent = off_el.getparent()
        if parent is None:
            return None
        parent_path = self._local_name_path(parent)

        # Fast-path: stray AppHdr-only tag directly inside BusMsgEnvlp.
        # Remove it outright — it has no business being there regardless of
        # what the SWIFT envelope XSD says about cardinality.
        _seq_par_local_early = etree.QName(parent.tag).localname if isinstance(parent.tag, str) else ""
        if (_seq_par_local_early == "BusMsgEnvlp"
                and offending in self._APPHDR_ONLY_TAGS):
            _off_idx = next((i for i, e in enumerate(parent) if e is off_el), None)
            if _off_idx is not None:
                parent_copy = self._copy(parent)
                parent_copy.remove(list(parent_copy)[_off_idx])
                return FixSuggestion(self._xpath_of(parent),
                                     self._serialize(parent),
                                     self._serialize(parent_copy),
                                     code, msg, "high")
        # Build new children in the PARENT's namespace (AppHdr is head.001, the
        # Document body is the message namespace) — not the document root's.
        ns = etree.QName(parent.tag).namespace or ns

        # Pick the schema that defines this parent: AppHdr → head (BAH) XSD,
        # everything else → the Document message XSD.
        # Guard: when Document appears after AppHdr in the path (BusMsgEnvlp
        # envelope nests Document inside AppHdr), the element belongs to the
        # message body — use the message XSD, not head.001.
        _ah_idx2 = next((i for i, p in enumerate(parent_path) if p == "AppHdr"), -1)
        _doc_after_ah2 = any(p == "Document" for p in parent_path[_ah_idx2 + 1:]) if _ah_idx2 >= 0 else False
        in_apphdr = _ah_idx2 >= 0 and not _doc_after_ah2
        xsd_path  = (self._get_apphdr_xsd_path(xml) if in_apphdr
                     else self._get_xsd_path(xml))
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        parent_type = tmap.type_of_path(parent_path) if tmap else None
        # Fallback: try global element lookup when path walk fails (e.g. envelope
        # wrapper roots like RequestPayload not in XSD element_type).
        if not parent_type and tmap:
            _seq_parent_local = parent_path[-1] if parent_path else ""
            parent_type = tmap.element_type.get(_seq_parent_local)
        order = tmap.order_for_type(parent_type) if (tmap and parent_type) else []
        # KB fallback when XSD type resolution fails entirely
        if not order:
            _seq_parent_local = parent_path[-1] if parent_path else ""
            _kb_order = _kb_get(f"tag_insertion_order.{_seq_parent_local}")
            if isinstance(_kb_order, list):
                order = _kb_order

        original_fragment = self._serialize(parent)
        xpath = self._xpath_of(parent)

        # `offending` is not a direct child of `parent` at all (e.g.
        # PrvsInstgAgt2 stranded in FICdtTrf) but IS a valid child of one of
        # parent's OTHER child container types (e.g. CdtTrfTxInf). This is a
        # wrong-nesting-level error — the element belongs inside an EXISTING
        # sibling container. Decline so _try_wrap_orphaned_block /
        # _try_relocate_to_ancestor can move it there, instead of Case 1b
        # deleting it or Case 2 masking the issue with an unrelated insert.
        if tmap and parent_type and offending not in order:
            for _vc in tmap.type_info.get(parent_type, {}).get("children", []):
                _vc_type = tmap.get_child_type(parent_type, _vc["name"])
                if not _vc_type:
                    continue
                _vc_children = {g["name"] for g in
                               tmap.type_info.get(_vc_type, {}).get("children", [])}
                if offending in _vc_children:
                    return None

        # ── Case 1: misnamed element. The offending tag is not a valid child but
        #    is a near-match of one (e.g. <BIC> where <BICFI> is expected) — a
        #    common ISO 20022 mistake. Rename it, preserving its value, then
        #    reorder to the XSD sequence. Skip when the offending tag IS a valid
        #    child (it's merely out of order — handled by the reorder path below),
        #    so we never rename a legitimate element. ──────────────────────────
        if expected and offending not in expected and offending not in order:
            cand = self._closest_expected(offending, expected)
            # The validator's expected list is only the elements valid at THIS
            # position (e.g. just 'Othr'); a misnamed element like <BIC> should
            # map to <BICFI>, which may not be in that slice. Fall back to the
            # parent's full XSD child set so BIC→BICFI still resolves.
            if not cand and order:
                cand = self._closest_expected(offending, order)
            if cand:
                off_idx = list(parent).index(off_el)
                parent_copy = self._copy(parent)
                old = list(parent_copy)[off_idx]
                parent_copy.replace(old, self._rename_element(old, cand, ns))
                if order:
                    self._reorder_children(parent_copy, order)
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(parent_copy), code, msg, "high")

        # ── Case 1b: element NOT permitted here at all. When the parent's XSD type
        #    is known and the offending tag is NOT among its legal children (and
        #    wasn't a renameable misname above), it cannot stay — remove it. This
        #    is the correct fix for a stray element the profile forbids, e.g.
        #    <ClrSys> inside a pacs.008 SettlementInstruction (CBPR+ permits
        #    SttlmAcct / reimbursement agents there, never ClrSys). Removal is
        #    schema-backed (we only delete when the XSD confirms it's not a legal
        #    child), so we never drop a genuinely-valid element.
        if tmap and parent_type:
            valid_children = [c.get("name") for c in
                              tmap.type_info.get(parent_type, {}).get("children", [])]
            if valid_children and offending not in valid_children:
                # SIZE GUARD: only auto-delete a SMALL stray (e.g. a misplaced
                # <BICFI>). A large subtree flagged "not expected here" is almost
                # always MISPLACED data the user wants moved/wrapped, not deleted —
                # silently dropping it looks like "the fix removed most of my
                # code". Decline (fall through) so we never offer a destructive
                # delete of a big block.
                # CONTENT GUARD: never delete structural containers that carry
                # meaningful user data (FinInstnId, PstlAdr, Id, Nm, etc.) — even
                # when they are small. These are almost always misplaced/mis-wrapped,
                # not genuinely stray, and removing them loses real data.
                _NEVER_DELETE = {
                    "FinInstnId", "PstlAdr", "Id", "Nm", "Othr", "BrnchId",
                    "FinInstId", "PrvtId", "OrgId", "Agt", "DbtrAgt", "CdtrAgt",
                    "IntrmyAgt1", "IntrmyAgt2", "InstgAgt", "InstdAgt",
                    "Dbtr", "Cdtr", "UltmtDbtr", "UltmtCdtr",
                    "Acct", "DbtrAcct", "CdtrAcct",
                }
                if offending in _NEVER_DELETE:
                    return None
                subtree_size = sum(1 for _ in off_el.iter())
                if subtree_size > 6:
                    return None
                try:
                    off_idx = list(parent).index(off_el)
                except ValueError:
                    off_idx = None
                if off_idx is not None:
                    parent_copy = self._copy(parent)
                    parent_copy.remove(list(parent_copy)[off_idx])
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(parent_copy), code, msg, "high")

        # ── Case 1c: valid element type but exceeds maxOccurs=1 (true duplicate).
        #    The element IS a legal child but a preceding sibling already satisfies
        #    the single allowed occurrence. XSD-schema backed: only fires when the
        #    type map confirms maxOccurs=1 and a prior same-named sibling exists.
        #    Example: two <FinInstnId> inside <Agt> (BranchAndFinancialInstitutionIdentification6).
        if tmap and parent_type:
            _parent_kids = list(parent)
            # Use identity (is) to locate off_el — lxml __eq__ compares content,
            # not identity, so elements with identical text would collide otherwise.
            _off_el_idx = next((i for i, e in enumerate(_parent_kids) if e is off_el), -1)
            _siblings_before = [
                s for i, s in enumerate(_parent_kids)
                if isinstance(s.tag, str)
                and etree.QName(s.tag).localname == offending
                and i < _off_el_idx
            ]
            if _siblings_before:
                _max_occ = "1"
                for _ci in tmap.type_info.get(parent_type, {}).get("children", []):
                    if _ci["name"] == offending:
                        _max_occ = _ci.get("max", "1")
                        break
                _is_singular = (_max_occ != "unbounded" and
                                (not _max_occ.isdigit() or int(_max_occ) == 1))
                if _is_singular and _off_el_idx >= 0:
                    _pcopy = self._copy(parent)
                    _pcopy.remove(list(_pcopy)[_off_el_idx])
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(_pcopy), code, msg, "high")

        # ── Case 2: a mandatory element is missing before the offending one.
        #    Insert missing element(s) and reorder. For MANDATORY elements we
        #    always reinsert. For OPTIONAL elements we only reinsert when the
        #    validator EXPLICITLY named them in the expected list — that means the
        #    user deleted them and the schema is rejecting the result. We never
        #    speculatively inject optional tags the validator didn't name.
        existing = {etree.QName(c.tag).localname for c in parent if isinstance(c.tag, str)}

        def is_mandatory(child: str) -> bool:
            if not (tmap and parent_type):
                return False  # no schema info → can't assert mandatory; decline
            for c in tmap.type_info.get(parent_type, {}).get("children", []):
                if c["name"] == child:
                    return c.get("min", "1") != "0"
            return False

        def _is_buildable_seq(child: str) -> bool:
            """True when the element is constructable via XSD/template/KB."""
            if tmap and parent_type:
                ctype = tmap.get_child_type(parent_type, child)
                if ctype:
                    return True
            return child in _TEMPLATES or bool(_kb_tag_template(child, msg_type))

        # Terse-variant fallback: lxml named no expected element, so derive the
        # missing mandatory predecessor(s) from the XSD sequence — every mandatory
        # child that must appear BEFORE the offending element but is absent from
        # the parent. Classic case: <EndToEndId> deleted from <PmtId> →
        # "The element 'TxId' is not expected here." with no expected list.
        # Only mandatory ones here — we have no signal that optional ones were removed.
        if not expected and order:
            try:
                off_pos = order.index(offending)
            except ValueError:
                off_pos = len(order)
            expected = [c for c in order[:off_pos]
                        if c not in existing and is_mandatory(c)]

        # Include explicitly-named tags regardless of min=0: if the validator said
        # "expected: X" and X is not present, the user removed X and we must restore
        # it — even when the XSD marks it optional (min=0).
        _explicit_expected = set(expected)  # named by the validator error message
        to_add = [
            e for e in expected
            if e not in existing
            and (is_mandatory(e) or e in _explicit_expected)
            and _is_buildable_seq(e)
        ]
        if not to_add:
            # ── Case 3: pure ORDERING violation. Nothing is missing or misnamed —
            #    every required element is present but the sequence is wrong (e.g.
            #    a manual edit put <CreDt> before <BizSvc> in the AppHdr). When the
            #    offending element is a valid child of the parent, reorder ALL the
            #    parent's children into the XSD sequence. This is deterministic and
            #    schema-correct, and is the case the fixer previously declined
            #    (returning a low-confidence no-op), so reordering never happened.
            if order and offending in order:
                parent_copy = self._copy(parent)
                self._reorder_children(parent_copy, order)
                reordered = self._serialize(parent_copy)
                if reordered != original_fragment:
                    return FixSuggestion(xpath, original_fragment,
                                         reordered, code, msg, "high")
            # Case 3: no mandatory element is missing → this is a pure ordering
            # violation. Reorder the parent's existing children to the XSD
            # sequence (safe, schema-driven). Decline only if order is unknown or
            # already correct.
            if order:
                current = [etree.QName(c.tag).localname for c in parent
                           if isinstance(c.tag, str)]
                desired = sorted(current,
                                 key=lambda n: order.index(n) if n in order else len(order))
                if current != desired:
                    parent_copy = self._copy(parent)
                    self._reorder_children(parent_copy, order)
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(parent_copy), code, msg, "high")
            return None

        _seq_parent_local = parent_path[-1] if parent_path else ""
        parent_copy = self._copy(parent)
        for child in to_add:
            # Never insert AppHdr-only tags directly into BusMsgEnvlp — they
            # belong inside AppHdr; inserting them here creates invalid strays.
            if (_seq_parent_local == "BusMsgEnvlp"
                    and child in self._APPHDR_ONLY_TAGS):
                continue
            child_el = self._build_child(child, "", ns, tmap,
                                         existing_parent=parent_copy,
                                         rules_idx=rules_idx,
                                         path_parts=parent_path + [child],
                                         root=root, msg_type=msg_type)
            if child_el is not None:
                parent_copy.append(child_el)
        if order:
            self._reorder_children(parent_copy, order)

        return FixSuggestion(xpath, original_fragment,
                             self._serialize(parent_copy), code, msg, "high")

    def _closest_expected(self, name: str, expected: list) -> Optional[str]:
        """Best valid-element match for a misnamed element: exact (case-insensitive),
        then prefix containment (BIC→BICFI), then longest shared prefix (>=2)."""
        nl = name.lower()
        for e in expected:
            if e.lower() == nl:
                return e
        for e in expected:
            el = e.lower()
            if el.startswith(nl) or nl.startswith(el):
                return e
        best, best_cp = None, 1
        for e in expected:
            cp = 0
            for a, b in zip(nl, e.lower()):
                if a == b:
                    cp += 1
                else:
                    break
            if cp > best_cp:
                best, best_cp = e, cp
        return best

    def _rename_element(self, old: etree._Element, new_name: str,
                        ns: str) -> etree._Element:
        """Return a copy of `old` with its tag changed to new_name, preserving
        text, attributes and child elements."""
        tag = f"{{{ns}}}{new_name}" if ns else new_name
        new = etree.Element(tag)
        new.text = old.text
        for k, v in old.attrib.items():
            new.set(k, v)
        for ch in old:
            new.append(self._copy(ch))
        return new

    def _try_wrap_orphaned_block(self, root: etree._Element, xml: str, code: str,
                                 msg: str) -> Optional["FixSuggestion"]:
        """Wrap a block of elements that are orphaned at the wrong parent level into
        the correct container.

        Classic case: after </TxInf> in camt.056 Undrlyg, several TxInf-level
        elements (OrgnlGrpInf, OrgnlEndToEndId, OrgnlUETR, …) appear directly
        inside Undrlyg.  The XSD validator flags the first one as "not expected
        here"; subsequent ones generate more errors or, with recover=True, get
        folded back into TxInf causing false duplicate errors.

        Strategy:
          1. Find the offending element (line-hint aware).
          2. Determine its actual parent and load the parent's XSD type.
          3. Find a valid-parent-child container type whose children include the
             offending element (e.g. TxInf whose type accepts OrgnlGrpInf).
          4. Collect the offending element + all consecutive siblings that are NOT
             valid children of the parent.
          5. Wrap them in a new container element appended to the parent.
        """
        m_off = re.search(r"element '([\w]+)' is not expected", msg, re.I)
        if not m_off:
            return None
        offending = m_off.group(1).strip()

        _cands = [el for el in root.iter()
                  if isinstance(el.tag, str)
                  and etree.QName(el.tag).localname == offending]
        if not _cands:
            return None
        off_el = self._pick_candidate(_cands)

        parent = off_el.getparent()
        if parent is None:
            return None

        _sp_path = self._local_name_path(parent)
        _sp_ah_idx = next((i for i, p in enumerate(_sp_path) if p == "AppHdr"), -1)
        _sp_doc_after = any(p == "Document" for p in _sp_path[_sp_ah_idx + 1:]) if _sp_ah_idx >= 0 else False
        in_apphdr = _sp_ah_idx >= 0 and not _sp_doc_after
        xsd_path = (self._get_apphdr_xsd_path(xml) if in_apphdr
                    else self._get_xsd_path(xml))
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        if not tmap:
            return None

        parent_path = self._local_name_path(parent)
        parent_type = tmap.type_of_path(parent_path)
        if not parent_type:
            return None

        valid_parent_children = {c["name"] for c in
                                  tmap.type_info.get(parent_type, {}).get("children", [])}

        # Only proceed when offending element has siblings that are ALSO invalid
        # in the parent — pure ordering issues are better handled by _try_sequence_fix.
        siblings = list(parent)
        try:
            off_idx = siblings.index(off_el)
        except ValueError:
            return None
        trailing = siblings[off_idx:]
        invalid_trailing = [s for s in trailing
                            if isinstance(s.tag, str)
                            and etree.QName(s.tag).localname not in valid_parent_children]
        if not invalid_trailing:
            return None

        # Find the container type: a valid parent child whose XSD type includes
        # the offending element as a child — DIRECTLY, or (deep case) one level
        # down via a single intermediate child. The deep case covers a bare
        # <BICFI> stranded in CdtTrfTxInf after its agent AND FinInstnId
        # wrappers were both ripped: it belongs in
        # PrvsInstgAgt1 → FinInstnId → BICFI, which no direct search finds.
        direct_cands: list = []
        deep_cands: list = []   # (container_name, intermediate_name)
        for child_info in tmap.type_info.get(parent_type, {}).get("children", []):
            cname = child_info["name"]
            ctype = tmap.get_child_type(parent_type, cname)
            if not ctype:
                continue
            ctype_children = {c["name"] for c in
                               tmap.type_info.get(ctype, {}).get("children", [])}
            if offending in ctype_children:
                direct_cands.append(cname)
                continue
            for mid_info in tmap.type_info.get(ctype, {}).get("children", []):
                mtype = tmap.get_child_type(ctype, mid_info["name"])
                if not mtype:
                    continue
                mtype_children = {c["name"] for c in
                                   tmap.type_info.get(mtype, {}).get("children", [])}
                if offending in mtype_children:
                    deep_cands.append((cname, mid_info["name"]))
                    break

        inter_of: dict = {}
        if direct_cands:
            candidates = direct_cands
        elif deep_cands:
            candidates = [c for c, _m in deep_cands]
            inter_of = dict(deep_cands)
        else:
            return None

        # Choose the container slot, occupancy-aware:
        #   1. a preceding sibling instance that does NOT already carry the
        #      offending child → merge into it (classic camt.056 TxInf case);
        #   2. else the first candidate slot still MISSING from the parent →
        #      create it (so consecutive stray fragments land in consecutive
        #      free agent slots instead of piling into the same one);
        #   3. else the first candidate (original behavior).
        present_locals = {etree.QName(s.tag).localname for s in siblings
                          if isinstance(s.tag, str)}
        container_name = None
        preceding_container_idx = None
        for i in range(off_idx - 1, -1, -1):
            sib = siblings[i]
            if not isinstance(sib.tag, str):
                continue
            sib_local = etree.QName(sib.tag).localname
            if sib_local in candidates:
                sib_children = {etree.QName(ch.tag).localname for ch in sib
                                if isinstance(ch.tag, str)}
                if offending not in sib_children and sib_local not in inter_of:
                    container_name = sib_local
                    preceding_container_idx = i
                break  # nearest preceding candidate decides; occupied → create
        if container_name is None:
            container_name = next((c for c in candidates
                                   if c not in present_locals), candidates[0])
        intermediate_name = inter_of.get(container_name)

        container_type = tmap.get_child_type(parent_type, container_name)
        container_order = tmap.order_for_type(container_type) if container_type else []
        inter_type = (tmap.get_child_type(container_type, intermediate_name)
                      if (container_type and intermediate_name) else None)
        inter_children = ({c["name"] for c in
                           tmap.type_info.get(inter_type, {}).get("children", [])}
                          if inter_type else set())

        # Collect all elements from off_idx onwards that are either:
        #   a) the offending element, or
        #   b) not a valid parent child (clearly orphaned TxInf-level fields)
        # Deep case: only take consecutive strays the INTERMEDIATE accepts —
        # a following stray of a different shape (e.g. a complete FinInstnId
        # after a bare BICFI) is a remnant of a DIFFERENT ripped wrapper and
        # must go to its own slot on a later round.
        orphan_indices = []
        for i, sib in enumerate(siblings[off_idx:], start=off_idx):
            sib_local = etree.QName(sib.tag).localname if isinstance(sib.tag, str) else ""
            if intermediate_name:
                if sib_local in inter_children and sib_local not in valid_parent_children:
                    orphan_indices.append(i)
                else:
                    break
            elif sib_local == offending or sib_local not in valid_parent_children:
                orphan_indices.append(i)
            else:
                break

        if not orphan_indices:
            return None

        ns = etree.QName(parent.tag).namespace or ""
        container_tag = f"{{{ns}}}{container_name}" if ns else container_name

        original_fragment = self._serialize(parent)
        parent_copy = self._copy(parent)
        copy_kids = list(parent_copy)

        if preceding_container_idx is not None:
            # Merge into the preceding container — move ONLY the orphans it can
            # actually take (its locals don't clash); the rest stay in place
            # for later rounds instead of being silently dropped.
            prec_copy = copy_kids[preceding_container_idx]
            existing_locals = {etree.QName(ch.tag).localname
                               for ch in prec_copy
                               if isinstance(ch.tag, str)}
            merged_any = False
            for i in orphan_indices:
                el = copy_kids[i]
                el_local = etree.QName(el.tag).localname if isinstance(el.tag, str) else ""
                if el_local and el_local not in existing_locals:
                    parent_copy.remove(el)
                    prec_copy.append(el)
                    existing_locals.add(el_local)
                    merged_any = True
            if not merged_any:
                return None

            if container_order:
                self._reorder_children(prec_copy, container_order)
        else:
            # Detach orphans from the copy (in reverse order to keep indices valid)
            orphan_els = [copy_kids[i] for i in orphan_indices]
            for el in reversed(orphan_els):
                parent_copy.remove(el)

            # Build new container and populate with orphans (already in XML
            # order); deep case nests them inside the intermediate wrapper.
            new_container = etree.SubElement(parent_copy, container_tag)
            receiver = new_container
            if intermediate_name:
                inter_tag = (f"{{{ns}}}{intermediate_name}" if ns
                             else intermediate_name)
                receiver = etree.SubElement(new_container, inter_tag)
            for el in orphan_els:
                receiver.append(el)

            if intermediate_name and inter_type:
                inter_order = tmap.order_for_type(inter_type)
                if inter_order:
                    self._reorder_children(receiver, inter_order)
            elif container_order:
                self._reorder_children(new_container, container_order)

        # Reorder parent so any valid elements appear in XSD sequence
        parent_order = tmap.order_for_type(parent_type)
        if parent_order:
            self._reorder_children(parent_copy, parent_order)

        reordered = self._serialize(parent_copy)
        if reordered == original_fragment:
            return None
        return FixSuggestion(self._xpath_of(parent), original_fragment,
                             reordered, code, msg, "high")

    def _try_relocate_to_ancestor(
        self, root: etree._Element, xml: str, code: str, msg: str
    ) -> Optional["FixSuggestion"]:
        """Lift elements nested too deeply to the closest ancestor where the
        XSD sequence permits them.

        Triggered when 'X is not expected here' and X (plus any trailing
        siblings that also don't belong) is a valid child of an ancestor, not
        the current parent.  Example: camt.052 <TxsSummry> and <Ntry> trapped
        inside <Svcr> when they belong at <Rpt> level — this method moves the
        whole block to the right level and reorders both levels per XSD.

        Distinct from _try_wrap_orphaned_block (which wraps elements DOWN into
        a missing container) — this method moves elements UP.
        """
        m_off = re.search(
            r"element '([^']+)' is not expected(?:\s+here|\s+at this position)", msg, re.I
        )
        if not m_off:
            return None
        offending = m_off.group(1).strip().strip("':\" ")

        off_cands = [el for el in root.iter()
                     if isinstance(el.tag, str) and etree.QName(el.tag).localname == offending]
        if not off_cands:
            return None
        off_el = self._pick_candidate(off_cands)

        current_parent = off_el.getparent()
        if current_parent is None:
            return None

        xsd_path = self._get_xsd_path(xml)
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        if tmap is None:
            return None

        cp_path = self._local_name_path(current_parent)
        cp_type = tmap.type_of_path(cp_path)
        valid_in_cp = set()
        if cp_type:
            valid_in_cp = {c["name"] for c in
                           tmap.type_info.get(cp_type, {}).get("children", [])}

        # Only act when the offending element is genuinely invalid in the current parent.
        # If it IS valid here, this is a pure ordering issue — let _try_sequence_fix handle it.
        if offending in valid_in_cp:
            return None

        # Walk ancestors to find the nearest one that accepts the offending element.
        target_ancestor = None
        anc_type: Optional[str] = None
        ancestor = current_parent.getparent()
        while ancestor is not None and isinstance(ancestor.tag, str):
            anc_path = self._local_name_path(ancestor)
            anc_type = tmap.type_of_path(anc_path)
            if anc_type:
                valid_in_anc = {c["name"] for c in
                                tmap.type_info.get(anc_type, {}).get("children", [])}
                if offending in valid_in_anc:
                    target_ancestor = ancestor
                    break
            ancestor = ancestor.getparent()

        if target_ancestor is None or anc_type is None:
            return None

        valid_in_ta = {c["name"] for c in
                       tmap.type_info.get(anc_type, {}).get("children", [])}

        # Collect the offending element and any following siblings that are
        # also invalid in current_parent but valid in target_ancestor —
        # they are all stranded here together.
        siblings = list(current_parent)
        try:
            off_idx = next(i for i, s in enumerate(siblings) if s is off_el)
        except StopIteration:
            return None

        to_relocate = []
        for sib in siblings[off_idx:]:
            if not isinstance(sib.tag, str):
                continue
            sib_local = etree.QName(sib.tag).localname
            if sib_local not in valid_in_cp and sib_local in valid_in_ta:
                to_relocate.append(sib)
            elif sib_local in valid_in_cp:
                break  # hit a valid sibling — stop collecting

        if not to_relocate:
            return None

        # Find the direct child of target_ancestor that contains current_parent
        # (insertion will happen immediately after it).
        ta_direct_container = current_parent
        while (ta_direct_container.getparent() is not target_ancestor
               and ta_direct_container.getparent() is not None):
            ta_direct_container = ta_direct_container.getparent()
        if ta_direct_container.getparent() is not target_ancestor:
            return None

        ta_children = list(target_ancestor)
        try:
            insert_after_idx = next(i for i, c in enumerate(ta_children)
                                    if c is ta_direct_container)
        except StopIteration:
            return None

        # Build navigation paths so we can find the same elements in the deep copy.
        cp_path_from_ta = self._index_path_to(target_ancestor, current_parent)
        if cp_path_from_ta is None:
            return None

        original_ta_fragment = self._serialize(target_ancestor)
        ta_xpath = self._xpath_of(target_ancestor)

        ta_copy = self._copy(target_ancestor)
        cp_copy = self._navigate_to(ta_copy, cp_path_from_ta)
        if cp_copy is None:
            return None

        # In the copy, find each element to relocate and remove it from cp_copy.
        elements_to_move: list = []
        for reloc_el in to_relocate:
            reloc_path = self._index_path_to(current_parent, reloc_el)
            if reloc_path is None:
                continue
            reloc_copy = self._navigate_to(cp_copy, reloc_path)
            if reloc_copy is not None:
                elements_to_move.append(reloc_copy)

        if not elements_to_move:
            return None

        for el_mv in elements_to_move:
            cp_copy.remove(el_mv)

        # Insert the moved elements into ta_copy after insert_after_idx.
        insert_pos = insert_after_idx + 1
        for j, el_mv in enumerate(elements_to_move):
            ta_copy.insert(insert_pos + j, el_mv)

        # Resolve [maxOccurs=1] collisions introduced by the relocation.
        # When a moved element has the same local-name as an element already
        # present in ta_copy, and the XSD caps that element at 1, keep the
        # occurrence with more child content (the richer one) and drop the
        # other.  This prevents a spurious DUPLICATE_TAG error on the next
        # validation round that would otherwise drop the wrong occurrence.
        if anc_type:
            _max_map = {c["name"]: c.get("max", "1")
                        for c in tmap.type_info.get(anc_type, {}).get("children", [])}
            _moved_locals = {etree.QName(e.tag).localname for e in elements_to_move
                             if isinstance(e.tag, str)}
            for _mv_local in _moved_locals:
                if _max_map.get(_mv_local, "1") not in ("unbounded", "0"):
                    try:
                        _max_val = int(_max_map.get(_mv_local, "1"))
                    except (TypeError, ValueError):
                        _max_val = 1
                    if _max_val == 1:
                        # Collect all occurrences of this tag in ta_copy.
                        _dups = [c for c in list(ta_copy)
                                 if isinstance(c.tag, str)
                                 and etree.QName(c.tag).localname == _mv_local]
                        if len(_dups) > 1:
                            # Keep the richest (most descendant nodes); on tie keep
                            # the one that came from the relocation (has a non-empty
                            # subtree), else keep the first.
                            def _richness(e):
                                return sum(1 for _ in e.iter())
                            _keeper = max(_dups, key=_richness)
                            for _d in _dups:
                                if _d is not _keeper:
                                    ta_copy.remove(_d)

        # Reorder both modified levels per XSD sequence.
        ta_order = tmap.order_for_type(anc_type)
        if ta_order:
            self._reorder_children(ta_copy, ta_order)

        if cp_type:
            cp_order = tmap.order_for_type(cp_type)
            if cp_order:
                self._reorder_children(cp_copy, cp_order)

        new_ta_fragment = self._serialize(ta_copy)
        if new_ta_fragment == original_ta_fragment:
            return None

        return FixSuggestion(ta_xpath, original_ta_fragment, new_ta_fragment, code, msg, "high")

    def _reorder_children(self, parent: etree._Element, order: list) -> None:
        """Reorder parent's children to match the XSD sequence `order` (stable;
        tags not in `order` keep their relative position at the end)."""
        kids = list(parent)
        for k in kids:
            parent.remove(k)
        def _key(k):
            ln = etree.QName(k.tag).localname if isinstance(k.tag, str) else ""
            return order.index(ln) if ln in order else len(order)
        for k in sorted(kids, key=_key):
            parent.append(k)

    def _pick_nearest(self, elems: list, line_hint: Optional[int]):
        """Return the element whose source line is closest to line_hint, else the
        first element. Empty list → None."""
        if not elems:
            return None
        if line_hint is None:
            return elems[0]
        return min(elems, key=lambda e: abs((e.sourceline or 0) - line_hint))

    def _pick_candidate(self, cands: list):
        """Pick the most plausible element among same-named candidates.

        Preference order:
          1. longest ancestor-chain suffix match against the validator's
             slash/dot path for THIS issue (set in suggest()) — immune to the
             line-number drift suggest_batch's roll-forward applies introduce
             (a stale line hint otherwise lands on the WRONG same-named
             element, e.g. Assgnr/Agt/FinInstnId instead of Assgne/FinInstnId);
          2. proximity to the reported line number;
          3. first candidate.
        """
        if not cands:
            return None
        lh = getattr(self, "_line_hint", None)
        p = getattr(self, "_issue_path", "") or ""
        parts = [t.split("[")[0] for t in re.split(r"[/.]", p)
                 if t and not re.fullmatch(r"\d+|\*", t.split("[")[0] or "*")]
        if len(parts) >= 2:
            want = parts[:-1]  # the path's ancestors (last token = the element)

            def _suffix_score(el) -> int:
                chain = self._local_name_path(el)[:-1]
                score = 0
                for a, b in zip(reversed(chain), reversed(want)):
                    if a == b:
                        score += 1
                    else:
                        break
                return score

            best = max((_suffix_score(el) for el in cands), default=0)
            if best > 0:
                top = [el for el in cands if _suffix_score(el) == best]
                if len(top) == 1:
                    return top[0]
                cands = top  # disambiguate further by line proximity
        return self._pick_nearest(cands, lh)

    # Full ISO 20022 element names → their XML tag stems (validator messages
    # sometimes use the long names). Used to normalise dependency-rule wording.
    _ISO_NAME_TO_TAG = {
        "PreviousInstructingAgent": "PrvsInstgAgt",
        "IntermediaryAgent": "IntrmyAgt",
        "InstructingAgent": "InstgAgt",
        "InstructedAgent": "InstdAgt",
        "InstructingReimbursementAgent": "InstgRmbrsmntAgt",
        "InstructedReimbursementAgent": "InstdRmbrsmntAgt",
        "ThirdReimbursementAgent": "ThrdRmbrsmntAgt",
    }

    def _iso_tag(self, name: Optional[str]) -> Optional[str]:
        """Map a (possibly long-form, numbered) ISO element name to its XML tag,
        e.g. 'PreviousInstructingAgent3' → 'PrvsInstgAgt3'. Unknown names pass
        through unchanged (already a tag)."""
        if not name:
            return name
        m = re.match(r"([A-Za-z]+?)(\d*)$", name)
        if not m:
            return name
        base, num = m.group(1), m.group(2)
        return self._ISO_NAME_TO_TAG.get(base, base) + num

    def _fix_missing_predecessor(self, root: etree._Element, xml: str, code: str,
                                 msg: str, msg_type: str) -> Optional[FixSuggestion]:
        """Repair "X cannot exist without Y" ordering rules (e.g. L3_INTRMY_ORDER:
        IntrmyAgt2 requires IntrmyAgt1; IntrmyAgt3 requires IntrmyAgt2) by
        inserting the missing predecessor Y immediately before X and reordering
        to the XSD sequence. Y is built from ai_knowledge_base.json tag_templates
        (e.g. IntrmyAgt1), so it works for every message type that uses the
        element. Generic for any "X cannot exist without Y" message.
        """
        x_tag = y_tag = None
        # 1. KB-driven: read declarative predecessor rules from ai_knowledge_base.json
        for rule in (_kb_get("auto_fix_rules.predecessor", []) or []):
            pat = rule.get("message_pattern")
            if code in (rule.get("codes") or []) or (pat and re.search(pat, msg, re.I)):
                x_tag, y_tag = rule.get("element"), rule.get("requires")
                break
        # 2. Generic message parse: "X cannot exist without Y"
        if not (x_tag and y_tag):
            m = re.search(r"['\"<]?(\w+)['\">]?\s+cannot exist without\s+['\"<]?(\w+)", msg, re.I)
            if m:
                x_tag, y_tag = m.group(1), m.group(2)
        # 2b. "If X is present, then Y must be present" (dependency-chain wording,
        #     often with full ISO names like PreviousInstructingAgent3).
        if not (x_tag and y_tag):
            mb = re.search(r"if\s+([\w]+)\s+is present\s*,?\s*then\s+([\w]+)\s+must be present",
                           msg, re.I)
            if mb:
                x_tag, y_tag = mb.group(1), mb.group(2)
        # 3. Code fallback
        if not (x_tag and y_tag):
            if code == "L3_INTRMY_ORDER_1":
                x_tag, y_tag = "IntrmyAgt2", "IntrmyAgt1"
            elif code == "L3_INTRMY_ORDER_2":
                x_tag, y_tag = "IntrmyAgt3", "IntrmyAgt2"
        # Normalise full ISO element names to their XML tags (PreviousInstructingAgent3
        # → PrvsInstgAgt3, IntermediaryAgent2 → IntrmyAgt2, …).
        x_tag, y_tag = self._iso_tag(x_tag), self._iso_tag(y_tag)
        if not (x_tag and y_tag):
            return None

        def local(t) -> str:
            return etree.QName(t).localname if isinstance(t, str) else ""

        for x_el in root.iter():
            if local(x_el.tag) != x_tag:
                continue
            parent = x_el.getparent()
            if parent is None:
                continue
            if any(local(c.tag) == y_tag for c in parent):
                continue  # predecessor already present in this parent
            ns = etree.QName(parent.tag).namespace or ""
            parent_path = self._local_name_path(parent)
            _fp_ah_idx = next((i for i, p in enumerate(parent_path) if p == "AppHdr"), -1)
            _fp_doc_after = any(p == "Document" for p in parent_path[_fp_ah_idx + 1:]) if _fp_ah_idx >= 0 else False
            _fp_in_apphdr = _fp_ah_idx >= 0 and not _fp_doc_after
            xsd_path = (self._get_apphdr_xsd_path(xml) if _fp_in_apphdr
                        else self._get_xsd_path(xml))
            tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
            y_el = self._build_child(y_tag, "", ns, tmap,
                                     path_parts=parent_path + [y_tag],
                                     root=root, msg_type=msg_type)
            if y_el is None:
                continue
            parent_copy = self._copy(parent)
            x_idx = list(parent).index(x_el)
            parent_copy.insert(x_idx, y_el)
            ptype = tmap.type_of_path(parent_path) if tmap else None
            order = tmap.order_for_type(ptype) if (tmap and ptype) else []
            if order:
                self._reorder_children(parent_copy, order)
            return FixSuggestion(self._xpath_of(parent), self._serialize(parent),
                                 self._serialize(parent_copy), code, msg, "high")
        return None

    def _try_dependency_fix(self, root: etree._Element, path: str, code: str,
                            msg: str, msg_type: str,
                            tmap: Optional[_XsdTypeMap]) -> Optional[FixSuggestion]:
        """Repair CBPR+ cross-field dependency rules that report a line number or
        '/' as the path (so the generic element walk can't act):

          • BICFI / AnyBIC exclusivity (CBPR_COM_R9 / CBPR_COM_R11 / DEP_013):
            if BICFI (or AnyBIC) is present in a block, Nm and PstlAdr are NOT
            allowed → remove Nm and PstlAdr.
          • Name/Address coexistence (NAME_ADDRESS_COEXISTENCE / DEP_014):
            Nm and PstlAdr must both be present → if the block also has BICFI,
            remove the lone one (exclusivity wins); otherwise add the missing
            counterpart.

        KB folder (pacs00x_…_validation_kb.json) is the source of truth for both
        fixes (DEP_013 / DEP_014 / CBPR_AGENT_BICFI_EXCLUSIVE).
        """
        msg_l = msg.lower()

        # Rules are declared in ai_knowledge_base.json (auto_fix_rules); match by
        # code or message_pattern so new rules need only a KB edit, no code change.
        def _kb_match(rule: dict) -> bool:
            pat = rule.get("message_pattern")
            return (code in (rule.get("codes") or [])
                    or (pat is not None and re.search(pat, msg, re.I) is not None))

        excl_rule = next((r for r in (_kb_get("auto_fix_rules.exclusive", []) or [])
                          if _kb_match(r)), None)
        coex_rule = next((r for r in (_kb_get("auto_fix_rules.coexistence", []) or [])
                          if _kb_match(r)), None)

        # KB match (preferred) → fall back to built-in detection for robustness.
        is_anybic_excl = (excl_rule is not None and excl_rule.get("if_present") == "AnyBIC") \
            or (excl_rule is None and (code == "CBPR_COM_R11"
                or ("anybic is present" in msg_l and "not allowed" in msg_l)))
        is_bicfi_excl  = (excl_rule is not None and not is_anybic_excl) \
            or (excl_rule is None and (code in ("CBPR_COM_R9", "CBPR_AGENT_BICFI_EXCLUSIVE", "DEP_013")
                or ("bicfi is present" in msg_l and "not allowed" in msg_l)))
        is_name_addr   = (coex_rule is not None) \
            or (code in ("NAME_ADDRESS_COEXISTENCE", "DEP_014") or "present together" in msg_l
                # CBPR_R56-style wording: "<Nm> and <PstlAdr> must both be
                # present or both absent." Match on the message (codes like
                # R56 are numbered per message family and not stable).
                or ("both be present" in msg_l and "nm" in msg_l
                    and "pstladr" in msg_l))
        # Schema-level manifestation of BICFI exclusivity: a forbidden sibling
        # (Nm/PstlAdr) reported by the XSD as "not expected" while BICFI/AnyBIC is
        # present in the same block (e.g. "The element 'Nm' is not expected here.
        # No child element is expected at this point."). Target-finding below
        # only acts when such a block actually exists, so this is safe.
        # Whether the rule was matched EXPLICITLY (KB rule / dedicated code /
        # unambiguous wording) — as opposed to the schema-message fallback
        # below. Explicit matches own the issue: if no block violates the rule
        # any more (a stale batch issue — an earlier fix already repaired it),
        # we return a no-op instead of declining, so the caller never falls
        # through to value/insert routes that would RE-ADD the very element a
        # previous fix just removed (the <Nm>Sample Name</Nm> resurrection bug).
        explicit_rule = bool(is_bicfi_excl or is_anybic_excl or is_name_addr)
        if (not explicit_rule
                and re.search(r"element '(Nm|PstlAdr|CtryOfRes|CtrySubDvsn)' is not expected", msg, re.I)
                and not re.search(r"following element", msg, re.I)):
            is_bicfi_excl = True
        if not (is_bicfi_excl or is_anybic_excl or is_name_addr):
            return None

        # Parameters from the KB rule (with sensible defaults).
        remove_tags = tuple((excl_rule or {}).get("remove") or ("Nm", "PstlAdr"))
        coex_tags   = tuple((coex_rule or {}).get("elements") or ("Nm", "PstlAdr"))
        excl_with   = (coex_rule or {}).get("exclusive_with", "BICFI")

        line_hint = None
        try:
            line_hint = int(str(path).strip())
        except (TypeError, ValueError):
            line_hint = None

        def local(t) -> str:
            return etree.QName(t).localname if isinstance(t, str) else ""

        def child(el, name):
            for c in el:
                if local(c.tag) == name:
                    return c
            return None

        def has_desc(el, name) -> bool:
            return any(local(d.tag) == name for d in el.iter())

        def emit(target):
            copy = self._copy(target)
            ns = etree.QName(target.tag).namespace or ""
            # Exclusivity (the block carries BICFI/AnyBIC) → remove disallowed tags.
            if is_bicfi_excl or is_anybic_excl or has_desc(target, excl_with) or has_desc(target, "AnyBIC"):
                tags = remove_tags if (is_bicfi_excl or is_anybic_excl) else coex_tags
                removed = False
                for name in tags:
                    c = child(copy, name)
                    while c is not None:
                        copy.remove(c)
                        removed = True
                        c = child(copy, name)
                if not removed:
                    return None
            else:
                # Coexistence → add the missing counterpart, first element first.
                a = coex_tags[0]
                b = coex_tags[1] if len(coex_tags) > 1 else "PstlAdr"
                ca, cb = child(copy, a), child(copy, b)
                if ca is not None and cb is None:
                    built = self._build_child(b, "", ns, tmap, root=root, msg_type=msg_type)
                    if built is not None:
                        copy.insert(list(copy).index(child(copy, a)) + 1, built)
                elif cb is not None and ca is None:
                    built = self._build_child(a, "", ns, tmap, root=root, msg_type=msg_type)
                    if built is not None:
                        copy.insert(list(copy).index(child(copy, b)), built)
                else:
                    return None
            return FixSuggestion(self._xpath_of(target), self._serialize(target),
                                 self._serialize(copy), code, msg, "high")

        # ── BICFI / AnyBIC exclusivity: blocks carrying the id AND a disallowed tag ──
        if is_bicfi_excl or is_anybic_excl:
            id_tag     = (excl_rule or {}).get("if_present") or ("AnyBIC" if is_anybic_excl else "BICFI")
            containers  = tuple((excl_rule or {}).get("containers")
                                or (("OrgId", "PrvtId", "Id") if is_anybic_excl else ("FinInstnId",)))
            targets = [el for el in root.iter()
                       if local(el.tag) in containers and has_desc(el, id_tag)
                       and any(child(el, t) is not None for t in remove_tags)]
            target = self._pick_nearest(targets, line_hint)
            fixed = emit(target) if target is not None else None
            if fixed is not None:
                return fixed
            return (FixSuggestion("", "", "", code, msg, "low")
                    if explicit_rule else None)

        # ── Name/Address coexistence: blocks with exactly one of the pair ─────
        a = coex_tags[0]
        b = coex_tags[1] if len(coex_tags) > 1 else "PstlAdr"
        targets = [el for el in root.iter()
                   if (child(el, a) is not None) != (child(el, b) is not None)]
        target = self._pick_nearest(targets, line_hint)
        fixed = emit(target) if target is not None else None
        if fixed is not None:
            return fixed
        return (FixSuggestion("", "", "", code, msg, "low")
                if explicit_rule else None)

    def _fix_attribute(self, el: etree._Element, attr_name: str, code: str,
                        msg: str, fix_hint: str, _ns: str = "") -> FixSuggestion:
        """
        Fix a wrong attribute value on `el` (e.g. Ccy="EU" → Ccy="EUR").

        Strategy:
          1. If attr is Ccy, harvest a valid currency from the same document
             (e.g. another Amt's Ccy that IS valid).
          2. Otherwise use the field-constraint example or sensible default.
        """
        original_fragment = self._serialize(el)
        xpath             = self._xpath_of(el)
        el_copy           = self._copy(el)

        new_value: Optional[str] = None

        if attr_name.lower() == "ccy":
            # Currency fix uses currency.json as authoritative source.
            valid_currencies = _valid_currency_codes()
            cur_bad = (el.get("Ccy") or "").strip().upper()

            # 1. Explicit expected currency stated in message/fix_hint
            _combined = f"{msg} {fix_hint}"

            # Pattern A: "from 'USD' to 'DKK'" or "from USD to DKK" (quoted or bare)
            # Handles CHRG_CCY_MISMATCH fix_hint style exactly.
            _pair_m = re.search(
                r"from\s+'?([A-Z]{3})'?\s+to\s+'?([A-Z]{3})'?",
                _combined, re.I,
            )
            if _pair_m:
                _candidate = _pair_m.group(2).upper()
                if not valid_currencies or _candidate in valid_currencies:
                    new_value = _candidate

            # Pattern B: "update to DKK" / "expected currency DKK" /
            #            "transaction currency is DKK" / "set ... to 'DKK'"
            if new_value is None:
                _direct_m = re.search(
                    r"(?:expected\s+currency|transaction\s+currency\s+(?:is\s+)?|"
                    r"update\s+(?:\S+\s+)?to\s+|set\s+(?:\S+\s+)?to\s+)\s*'?([A-Z]{3})'?",
                    _combined, re.I,
                )
                if _direct_m:
                    _candidate = _direct_m.group(1).upper()
                    if not valid_currencies or _candidate in valid_currencies:
                        new_value = _candidate

            # 2. Walk the doc — prefer IntrBkSttlmAmt (authoritative tx currency)
            #    then InstdAmt, then any other Ccy that differs from the bad one.
            if new_value is None:
                try:
                    root = el.getroottree().getroot()
                except Exception:
                    root = None
                if root is not None:
                    # Priority 1: IntrBkSttlmAmt Ccy — the canonical settlement currency
                    for sib in root.iter():
                        if not isinstance(sib.tag, str):
                            continue
                        if etree.QName(sib.tag).localname == "IntrBkSttlmAmt":
                            sib_ccy = (sib.get("Ccy") or "").strip().upper()
                            if sib_ccy and (not valid_currencies or sib_ccy in valid_currencies):
                                new_value = sib_ccy
                                break
                    # Priority 2: InstdAmt Ccy
                    if new_value is None:
                        for sib in root.iter():
                            if not isinstance(sib.tag, str):
                                continue
                            if etree.QName(sib.tag).localname == "InstdAmt":
                                sib_ccy = (sib.get("Ccy") or "").strip().upper()
                                if sib_ccy and (not valid_currencies or sib_ccy in valid_currencies):
                                    new_value = sib_ccy
                                    break
                    # Priority 3: any other element with a different, valid Ccy
                    if new_value is None:
                        for sib in root.iter():
                            if not isinstance(sib.tag, str):
                                continue
                            sib_ccy = (sib.get("Ccy") or "").strip().upper()
                            if sib_ccy and sib_ccy != cur_bad:
                                if not valid_currencies or sib_ccy in valid_currencies:
                                    new_value = sib_ccy
                                    break

            # Priority 4: derive from IBAN country prefix in the document
            # e.g. IBAN GB29... → GBP,  DE34... → EUR,  DK50... → DKK
            if new_value is None and root is not None:
                _IBAN_CCY: dict[str, str] = {
                    "GB": "GBP", "US": "USD", "CA": "CAD", "AU": "AUD",
                    "CH": "CHF", "NO": "NOK", "SE": "SEK", "DK": "DKK",
                    "JP": "JPY", "CN": "CNY", "HK": "HKD", "SG": "SGD",
                    "NZ": "NZD", "MX": "MXN", "BR": "BRL", "IN": "INR",
                    "ZA": "ZAR", "TR": "TRY", "PL": "PLN", "CZ": "CZK",
                    "HU": "HUF", "RO": "RON", "BG": "BGN", "HR": "HRK",
                    "IL": "ILS", "AE": "AED", "SA": "SAR", "QA": "QAR",
                    "KW": "KWD", "BH": "BHD", "OM": "OMR", "JO": "JOD",
                }
                # Most euro-zone countries default to EUR
                _EUR_COUNTRIES = {
                    "DE", "FR", "NL", "IT", "ES", "PT", "BE", "AT", "FI",
                    "IE", "GR", "LU", "SK", "SI", "EE", "LT", "LV", "MT",
                    "CY", "HR",
                }
                for sib in root.iter():
                    if not isinstance(sib.tag, str):
                        continue
                    if etree.QName(sib.tag).localname == "IBAN" and sib.text:
                        _ctry_pfx = (sib.text or "")[:2].upper()
                        _ccy_from_iban = (
                            _IBAN_CCY.get(_ctry_pfx)
                            or ("EUR" if _ctry_pfx in _EUR_COUNTRIES else None)
                        )
                        if _ccy_from_iban and (not valid_currencies
                                               or _ccy_from_iban in valid_currencies):
                            new_value = _ccy_from_iban
                            break

            # Priority 5: parse "for country 'XX'" from the validator message
            if new_value is None:
                _ctry_m = re.search(
                    r"(?:for|expected by)\s+country\s+'?([A-Z]{2})'?",
                    f"{msg} {fix_hint}", re.I,
                )
                if _ctry_m:
                    _ctry_key = _ctry_m.group(1).upper()
                    _ccy_for_ctry = (
                        {"GB": "GBP", "US": "USD", "CH": "CHF", "NO": "NOK",
                         "SE": "SEK", "DK": "DKK", "JP": "JPY", "CA": "CAD",
                         "AU": "AUD", "NZ": "NZD", "SG": "SGD", "HK": "HKD"
                         }.get(_ctry_key)
                        or ("EUR" if _ctry_key in {
                            "DE","FR","NL","IT","ES","PT","BE","AT","FI",
                            "IE","GR","LU","SK","SI","EE","LT","LV","MT","CY"
                        } else None)
                    )
                    if _ccy_for_ctry and (not valid_currencies
                                          or _ccy_for_ctry in valid_currencies):
                        new_value = _ccy_for_ctry

            if new_value is None:
                # No currency found anywhere — leave unchanged rather than
                # hardcoding a wrong currency like USD.
                new_value = cur_bad if cur_bad else "EUR"

            # 3. After choosing a valid currency, also fix the amount text
            #    if it has wrong decimal precision for that currency.
            #    e.g. text="15000.088888888888888888888888888888880" → "15000.09"
            amt_text = (el.text or "").strip()
            if amt_text:
                try:
                    _num_a = float(amt_text)
                    _prec_a = _ccy_precision(new_value)
                    _repaired_a = f"{_num_a:.{_prec_a}f}"
                    if re.match(r"^\d{1,13}(\.\d{1,5})?$", _repaired_a):
                        el_copy.text = _repaired_a
                except (ValueError, TypeError):
                    pass
        else:
            constraint = _kb_field_constraint(attr_name)
            if isinstance(constraint, dict):
                new_value = constraint.get("preferred") or constraint.get("example")
            if not new_value:
                new_value = self._placeholder(attr_name)

        el_copy.set(attr_name, str(new_value))
        return FixSuggestion(xpath, original_fragment,
                              self._serialize(el_copy), code, msg, "high")

    def _remove_element_fix(self, el: etree._Element, code: str,
                            msg: str) -> Optional[FixSuggestion]:
        """
        Produce a fix that DELETES `el` from its parent (used for CBPR-removed
        elements like SplmtryData / MsgPgntn). Targets the parent and removes the
        exact offending occurrence, preserving every other sibling.
        """
        import copy as _copy_mod
        parent = el.getparent()
        if parent is None:
            return None
        local = etree.QName(el.tag).localname
        sibs = [c for c in parent if isinstance(c.tag, str)
                and etree.QName(c.tag).localname == local]
        try:
            idx = sibs.index(el)
        except ValueError:
            idx = 0
        parent_copy = _copy_mod.deepcopy(parent)
        parent_copy.tail = None
        csibs = [c for c in parent_copy if isinstance(c.tag, str)
                 and etree.QName(c.tag).localname == local]
        if idx >= len(csibs):
            return None
        parent_copy.remove(csibs[idx])
        return FixSuggestion(
            xpath=self._xpath_of(parent),
            original_fragment=self._serialize(parent),
            fragment_xml=self._serialize(parent_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
        )

    # ── _fix_value ────────────────────────────────────────────────────────────

    def _fix_value(self, el: etree._Element, code: str, msg: str,
                   fix_hint: str, _ns: str = "") -> FixSuggestion:
        """Fix the text value of an existing element."""
        original_fragment = self._serialize(el)
        xpath             = self._xpath_of(el)
        msg_l             = msg.lower()
        el_local          = etree.QName(el.tag).localname
        try:
            _parent = el.getparent()
            parent_local = etree.QName(_parent.tag).localname if _parent is not None and isinstance(_parent.tag, str) else None
        except Exception:
            parent_local = None

        # ── GUARD: never write text into an element-only (complex) container ──
        # _fix_value repairs LEAF text. A complex container (CdtrAcct, DbtrAgt,
        # PstlAdr, FinInstnId, …) has no text value — writing one produces e.g.
        # "<CdtrAcct>GB29…<Id><IBAN>…</IBAN></Id></CdtrAcct>", which the schema
        # rejects ("character content not allowed because the content type is
        # element-only"). This happens when a batch round fires BOTH a structural
        # insert and a stray value fix at the same container. Decline (no-op) so
        # only the structural inserter repairs it. Amt/Cd/leaf types are NOT
        # element-only, so legitimate value/attribute fixes still run.
        # DUPLICATE_TAG deduplication operates on CHILDREN, not text content —
        # skip the element-only guard so it can reach the dedup logic below.
        _is_dup_code = (code == "DUPLICATE_TAG"
                        or "duplicate" in (msg + " " + fix_hint).lower()
                        or "appears more than once" in (msg + " " + fix_hint).lower())
        if self._is_element_only(el_local, None, None) and not _is_dup_code:
            return FixSuggestion(xpath, original_fragment, original_fragment,
                                 code, msg, "low")

        # ── HEADER_VAL / CBPR_DATETIME_NO_TIMEZONE on AppHdr datetime fields ────
        # When the AppHdr carries a datetime field (CreDt, CreDtTm) with a
        # completely invalid value (e.g. "with", "with+00:00", a bare date), or
        # when HEADER_VAL fires because the value doesn't match the ISODateTime
        # pattern, generate a fresh UTC datetime regardless of what the field
        # constraint suffix-heuristic says.  CreDt in head.001 is ALWAYS a
        # dateTime (not a date), so we must not produce a date-only string.
        _is_apphdr_dt_field = el_local in ("CreDt", "CreDtTm") and not list(el)
        _is_hdr_val = code in ("HEADER_VAL", "CBPR_DATETIME_NO_TIMEZONE",
                                "CBPR_DATETIME_Z_FORBIDDEN", "CBPR_DATETIME_MILLISECONDS",
                                "PAST_DATE_ERROR")
        if _is_apphdr_dt_field and _is_hdr_val:
            import datetime as _dt_mod
            _cur = (el.text or "").strip()
            _valid_dt = (re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:\d{2}$", _cur)
                         and self._is_valid_calendar_date(_cur))
            if not _valid_dt:
                _now = _dt_mod.datetime.now(_dt_mod.timezone.utc)
                el_copy = self._copy(el)
                el_copy.text = _now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── BizSvc → variant-aware CBPR+ business service value ───────────────
        # A wrong value like 'swift.cbprplus.02' on a pacs.009 is a perfectly
        # valid Max35Text, so the generic constraint check below never flags it —
        # only the CBPR_P9_R6 enum rule does. Handle it explicitly: replace with
        # the value mandated for this message family + variant (.03 / .adv.03 /
        # .cov.03 for pacs.009; the per-message KB expected_value otherwise).
        if el_local == "BizSvc" and not list(el):
            try:
                _root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                _root = None
            if _root is not None:
                _xml = self._serialize(_root)
                _bv = _cbpr_bizsvc_value(_detect_msg_type(_xml), _xml)
                if _bv and _bv != (el.text or "").strip():
                    el_copy = self._copy(el)
                    el_copy.text = _bv
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── External-code-list <Cd> by parent context ────────────────────────
        # <Cd> is polymorphic, so the generic constraint check can't tell which
        # code list applies. Map the parent (and, for Acct/Tp/Cd, grandparent) to
        # the right list and, when the value isn't valid, replace it with a real
        # code (preferring a widely-recognised one, else the first valid code).
        if el_local == "Cd" and not list(el):
            _par = el.getparent()
            _parname = (etree.QName(_par.tag).localname
                        if (_par is not None and isinstance(_par.tag, str)) else "")
            _gp = _par.getparent() if _par is not None else None
            _gpname = (etree.QName(_gp.tag).localname
                       if (_gp is not None and isinstance(_gp.tag, str)) else "")
            _cl = None
            _prefs: tuple = ()
            if _parname in ("ClrSys", "ClrSysId"):
                _cl, _prefs = "clearing_system", ("TGT", "RTP", "EBA", "STG", "CHP")
            elif _parname == "Tp" and _gpname.endswith("Acct"):
                _cl, _prefs = "account_type", ("CACC", "CASH", "LOAN")
            elif _parname == "LclInstrm":
                _cl, _prefs = "local_instrument", ("CORE", "INST", "B2B")
            elif _parname == "SvcLvl":
                # SEPA omitted from defaults — EUR-only (CBPR_COV_R32); a blind
                # enum repair must not introduce a SEPA/non-EUR violation.
                _cl, _prefs = "service_level", ("SDVA", "NURG", "URGP", "G001")
            elif _parname == "Rsn" and _gpname == "CxlRsnInf":
                _cl, _prefs = "cancellation_reason", ("DUPL", "CUST", "NARR", "FRAD", "CNCL")
            elif _parname == "Rsn" and _gpname in ("RtrInf", "StsRsnInf"):
                _cl, _prefs = "return_reason", ("AC01", "CUST", "NARR", "AM04")
            elif _parname == "Rsn":
                # Generic <Rsn><Cd> — infer from grandparent name
                if "Cxl" in _gpname:
                    _cl, _prefs = "cancellation_reason", ("DUPL", "CUST", "NARR", "CNCL")
                else:
                    _cl, _prefs = "return_reason", ("AC01", "CUST", "NARR")
            elif _parname == "Sts":
                # <Ntry><Sts><Cd> / <TxDtls><Sts><Cd> → EntryStatus1Code (camt.052/053/054)
                # EntryStatus1Code is a closed 4-value enum — apply fix directly
                # rather than relying on the codelist file (may be absent in this path).
                _ENTRY_STATUS_VALID = {"BOOK", "FUTR", "INFO", "PDNG"}
                _cur_sts = (el.text or "").strip()
                if _cur_sts not in _ENTRY_STATUS_VALID:
                    el_copy = self._copy(el)
                    el_copy.text = "BOOK"
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")
            if _cl:
                _codes = _codelist_codes(_cl)
                _cur = (el.text or "").strip()
                if _codes and _cur not in _codes:
                    _pick = next((c for c in _prefs if c in _codes), _codes[0])
                    el_copy = self._copy(el)
                    el_copy.text = _pick
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── Closed-enum violation: message lists the allowed values ───────────
        # e.g. SttlmMtd 'CLRG' on a pacs.008 → "must be one of the following
        # values : INDA, INGA". Set the element to a listed valid value (prefer a
        # sensible settlement default, else the first allowed value). Only fires
        # when the value genuinely isn't in the enumerated set.
        if not list(el):
            _allowed = _parse_allowed_values(f"{msg} {fix_hint}")
            _cur = (el.text or "").strip()
            if _allowed and _cur not in _allowed:
                _pick = next((v for v in ("INGA", "INDA", "COVE") if v in _allowed),
                             _allowed[0])
                el_copy = self._copy(el)
                el_copy.text = _pick
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── Currency code (Ccy attribute) on amount elements ──────────────────
        # "Invalid currency code 'Ccy'" / "Missing currency code" is about the
        # @Ccy ATTRIBUTE, not the element's numeric text. Route to the attribute
        # fixer, which harvests a valid ISO 4217 code from the document (else USD).
        #
        # GUARD: XSD type names like "ActiveCurrencyAndAmount" contain the word
        # "currency" — do NOT route here when the error is about the NUMERIC VALUE
        # (decimal precision, invalid number, etc.).  The Ccy-attribute path is
        # only correct when the error explicitly targets the currency CODE itself.
        # Guard: if the quoted "currency code" value looks like a corrupted amount
        # (digits + letters, e.g. '430182.38ABC38'), the real problem is the amount
        # text — don't route to the Ccy attribute fixer.
        _quoted_val_m = re.search(r"'([^']{3,})'", msg)
        _quoted_is_corrupt_amount = (
            _quoted_val_m is not None
            and (
                # digits + letters mixed → corrupted amount text
                (re.search(r'\d', _quoted_val_m.group(1))
                 and re.search(r'[A-Za-z]', _quoted_val_m.group(1))
                 and len(_quoted_val_m.group(1)) > 3)
                # numeric with multiple dots → amount with extra decimal places
                or (re.match(r'^[\d.]+$', _quoted_val_m.group(1))
                    and _quoted_val_m.group(1).count('.') > 1)
                # numeric string longer than a Ccy code → can't be a currency code
                or (re.match(r'^[\d.,]+$', _quoted_val_m.group(1))
                    and len(_quoted_val_m.group(1)) > 3)
            )
        )
        _ccy_is_the_issue = (
            "ccy" in msg_l                       # explicit @Ccy mention
            or "currency code" in msg_l          # "invalid currency code"
            or "currencycode" in msg_l           # XSD attribute error
            or "@ccy" in msg_l                   # attribute path
            or (el.get("Ccy") is None and "currency" in msg_l)  # Ccy attr absent
        ) and not re.search(r"'[\s\d.]+'\s*(is not|not valid|not a)", msg_l) \
          and not _quoted_is_corrupt_amount
        if _ccy_is_the_issue and \
           (el_local.endswith("Amt") or el_local == "Amt"
            or el.get("Ccy") is not None or "amount" in msg_l):
            return self._fix_attribute(el, "Ccy", code, msg, fix_hint)

        # ── Enum / code value with a KB-documented allow-list ─────────────────
        # e.g. CpyDplctInd → CODU/COPY/DUPL, CdtDbtInd → CRDT/DBIT. Picks a code
        # named in the error text if present, else the first documented code.
        if not list(el) and el.text is not None:
            try:
                _tr = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                _tr = None
            _kb = _KBContext.get(_detect_family_from_tree(_tr)) if _tr is not None else None
            if _kb:
                _codes = _kb.valid_codes(el_local)
                _cur = (el.text or "").strip()
                if _codes and _cur not in _codes:
                    _combined = f"{fix_hint} {msg}"
                    _chosen = next((c for c in _codes
                                    if re.search(rf"\b{re.escape(c)}\b", _combined)), _codes[0])
                    el_copy = self._copy(el)
                    el_copy.text = _chosen
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── DateTime with forbidden 'Z' / milliseconds (CBPR+ needs an offset) ─
        if not list(el) and el.text:
            _cur = el.text.strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", _cur) and (
                el_local.endswith(("DtTm", "Tm", "Dt")) or "datetime" in msg_l
                or "format" in msg_l or "pattern" in msg_l):
                _new = re.sub(r"\.\d+", "", _cur)        # drop milliseconds (forbidden)
                _new = re.sub(r"Z$", "+00:00", _new)      # 'Z' → explicit UTC offset
                el_copy = self._copy(el)
                el_copy.text = _new
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── Garbage value in a datetime field → replace with valid datetime ──────
        # Fires when the value (e.g. 'with', 'abc') is not a datetime at all and
        # the tag name or error message indicates a datetime field.
        if not list(el) and el.text:
            _cur = el.text.strip()
            _is_dt_field = (
                el_local.endswith("DtTm")
                or el_local in ("CreDt", "CreDtTm", "IntrBkSttlmTm", "PmtStpTm",
                                 "SttlmTmReq", "CLSTm", "TillTm", "FrTm", "RjctTm")
                or "date" in msg_l or "datetime" in msg_l
            )
            if _is_dt_field and not re.match(r'^\d{4}-\d{2}-\d{2}', _cur):
                from datetime import datetime as _dt, timezone as _tz
                _date_only = (el_local.endswith("Dt") and not el_local.endswith("DtTm")
                              and el_local not in ("CreDt",))
                _new = (_dt.now(_tz.utc).strftime("%Y-%m-%d") if _date_only
                        else _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"))
                el_copy = self._copy(el)
                el_copy.text = _new
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── Numeric value expected but the value isn't a number ───────────────
        # Fires for explicit "type Number" errors, and for numeric-named fields
        # (…Nb / SeqNb / NbOfTxs / PgNb) whose value isn't numeric — e.g.
        # "Field 'ElctrncSeqNb' has an invalid value: 'ABC'".
        _numeric_field = (
            el_local.endswith("Nb")
            or el_local in {"ElctrncSeqNb", "LglSeqNb", "NbOfTxs", "TtlNbOfTxs",
                            "NbOfNtries", "PgNb", "NbOfDays", "Qty", "Nb"}
            or (isinstance(_kb_field_constraint(el_local), dict)
                and _kb_field_constraint(el_local).get("type") in ("Number", "Numeric15", "Quantity"))
        )
        if not list(el) and el.text and (
                ("number" in msg_l and "type" in msg_l)
                or (_numeric_field and any(k in msg_l for k in
                    ("invalid value", "invalid", "not a valid", "number", "format", "type", "expected")))):
            _cur = el.text.strip()
            if not re.match(r"^-?\d+(\.\d+)?$", _cur):
                _con = _kb_field_constraint(el_local, parent_local)
                _ex = _con.get("example") if isinstance(_con, dict) else None
                _new = str(_ex) if (_ex and re.match(r"^-?\d+(\.\d+)?$", str(_ex))) else "1"
                el_copy = self._copy(el)
                el_copy.text = _new
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── BizMsgIdr empty (EMPTY_BIZSMSGIDR) ──────────────────────────────────
        # BizMsgIdr present but empty (length 0, min_length 1). Generate a value:
        # prefer GrpHdr/MsgId from the document; fall back to date-based dummy.
        if el_local == "BizMsgIdr" and not (el.text or "").strip() and "length" in msg_l:
            _fill_val: Optional[str] = None
            try:
                _biz_root2 = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                _biz_root2 = None
            if _biz_root2 is not None:
                _fill_val = self._harvest_value(_biz_root2, "MsgId")
            if not _fill_val:
                import datetime as _dt
                _fill_val = "MSG-" + _dt.date.today().strftime("%Y%m%d") + "-001"
            el_copy = self._copy(el)
            el_copy.text = _fill_val
            return FixSuggestion(xpath, original_fragment,
                                 self._serialize(el_copy), code, msg, "high")

        # ── BizMsgIdr must equal GrpHdr/MsgId (KB rule DEP_001) ─────────────────
        # Fix direction: if MsgId has invalid CBPR+ chars AND stripping them gives
        # BizMsgIdr's clean value, the root cause is a dirty MsgId — fix MsgId.
        # Only fix BizMsgIdr when BizMsgIdr itself is the wrong value.
        # If harvest fails (non-enveloped XML where MsgId lives in Document which
        # is outside the parsed root), extract MsgId value from the error message.
        if el_local == "BizMsgIdr" and (code == "BIZMSGIDR_NEQ_MSGID" or "msgid" in msg_l):
            try:
                _biz_root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                _biz_root = None
            mv = self._harvest_value(_biz_root, "MsgId") if _biz_root is not None else None
            # Fallback: parse MsgId value from error message
            # e.g. "AppHdr/BizMsgIdr 'CLEAN' must equal GrpHdr/MsgId 'DIRTY'."
            if mv is None:
                _mv_m = re.search(r"GrpHdr/MsgId '([^']+)'", msg)
                if _mv_m:
                    mv = _mv_m.group(1)
            bv = (el.text or "").strip()
            # Guard: if mv still unknown or matches bv, nothing to do here
            if mv and mv != bv:
                _CBPR_ID_INVALID = re.compile(r"[^A-Za-z0-9/\-?:().,'+]")
                mv_clean = _CBPR_ID_INVALID.sub('', mv)
                if mv_clean == bv and _biz_root is not None:
                    # MsgId is the dirty side — strip invalid chars from GrpHdr/MsgId
                    _msgid_el = None
                    for _cand in _biz_root.iter():
                        if (isinstance(_cand.tag, str)
                                and etree.QName(_cand.tag).localname == "MsgId"
                                and (_cand.text or "").strip() == mv):
                            _msgid_el = _cand
                            break
                    if _msgid_el is not None:
                        _msgid_copy = self._copy(_msgid_el)
                        _msgid_copy.text = mv_clean
                        return FixSuggestion(
                            self._xpath_of(_msgid_el), self._serialize(_msgid_el),
                            self._serialize(_msgid_copy), code, msg, "high"
                        )
                # BizMsgIdr is wrong — fix it to the cleaned MsgId value
                _target_val = mv_clean if mv_clean else mv
                el_copy = self._copy(el)
                el_copy.text = _target_val
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")
            elif mv is None or mv == bv:
                # Can't determine MsgId or already matches — return no-op so caller
                # doesn't fall through to LLM which generates a spurious AppHdr block
                return FixSuggestion(xpath, original_fragment, original_fragment,
                                     code, msg, "low")

        # ── Invalid Ccy text element (e.g. <Ccy>15000.08...</Ccy>) ─────────────
        # When el_local IS "Ccy" and its text is not a valid 3-letter code,
        # replace with a valid currency harvested from context or fall back to
        # "USD". Uses currency.json as the authoritative code list.
        if el_local in ("Ccy", "CcyOfTrf", "HomeCcy", "InstrCcy", "TxCcy") \
                and not list(el) and el.text:
            _cur_ccy_txt = (el.text or "").strip().upper()
            _valid_ccys = _valid_currency_codes()
            if _valid_ccys and _cur_ccy_txt not in _valid_ccys:
                # Try to harvest a valid currency from another element in the doc
                _picked_ccy: Optional[str] = None
                try:
                    _ccy_root = el.getroottree().getroot()
                except Exception:
                    _ccy_root = None
                if _ccy_root is not None:
                    for _sib in _ccy_root.iter():
                        if not isinstance(_sib.tag, str):
                            continue
                        _sib_ccy = _sib.get("Ccy")
                        if _sib_ccy and _sib_ccy.upper() in _valid_ccys:
                            _picked_ccy = _sib_ccy.upper()
                            break
                        _sib_local = etree.QName(_sib.tag).localname
                        if (_sib_local in ("Ccy", "CcyOfTrf") and _sib is not el
                                and _sib.text):
                            _c = _sib.text.strip().upper()
                            if _c in _valid_ccys:
                                _picked_ccy = _c
                                break
                if _picked_ccy is None:
                    _picked_ccy = "USD"
                _el_copy = self._copy(el)
                _el_copy.text = _picked_ccy
                return FixSuggestion(xpath, original_fragment,
                                      self._serialize(_el_copy), code, msg, "high")

        # ── Amount decimal precision repair (in-place, never replace) ─────────
        # "4234.000000000000" with Ccy="USD" → "4234.00".
        # Uses currency.json as the authoritative decimal-precision map.
        # Fires on INVALID_DECIMAL_PRECISION, "decimal"/"precision" in message,
        # or on any amount element whose text is numeric with wrong scale.
        _is_amt_el = (el_local.endswith("Amt") or el_local == "Amt"
                      or el.get("Ccy") is not None)
        if (_is_amt_el or "decimal" in msg_l or "precision" in msg_l
                or code in ("INVALID_DECIMAL_PRECISION", "NUM_TRAILING_DOT_OR_BARE")) and not list(el) and el.text:
            _amt_cur = (el.text or "").strip()
            # Trailing decimal point: "307845.85." → "307845.85" (NUM_TRAILING_DOT_OR_BARE)
            # float() rejects this form, so handle it before the generic float() path.
            if _amt_cur.endswith(".") and len(_amt_cur) > 1:
                _stripped_td = _amt_cur[:-1]
                try:
                    _num_td = float(_stripped_td)
                    _ccy_td = (el.get("Ccy") or "").upper()
                    _prec_td = _ccy_precision(_ccy_td)
                    _repaired_td = f"{_num_td:.{_prec_td}f}"
                    if re.match(r"^\d{1,13}(\.\d{1,5})?$", _repaired_td):
                        _el_copy = self._copy(el)
                        _el_copy.text = _repaired_td
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(_el_copy), code, msg, "high")
                except (ValueError, TypeError):
                    pass
            try:
                _num = float(_amt_cur)
                _ccy = (el.get("Ccy") or "").upper()
                # Use currency.json precision; default 2 when unknown
                _prec = _ccy_precision(_ccy)
                _repaired = f"{_num:.{_prec}f}"
                # Only emit a fix when the value actually changes and stays valid
                if (_repaired != _amt_cur
                        and re.match(r"^\d{1,13}(\.\d{1,5})?$", _repaired)):
                    _el_copy = self._copy(el)
                    _el_copy.text = _repaired
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(_el_copy), code, msg, "high")
            except (ValueError, TypeError):
                # float() failed — value likely has multiple decimal points
                # (e.g. "4000.098.09872635"). Recover the leading integer before
                # falling back to a placeholder so we return "4000.00" not "1000.00".
                if _is_amt_el:
                    _ccy_exc2 = (el.get("Ccy") or "").upper()
                    _prec_exc2 = _ccy_precision(_ccy_exc2) if _ccy_exc2 else 2
                    _int_m2 = re.match(r'^(\d+)', _amt_cur.replace(",", "."))
                    if _int_m2:
                        _repaired_exc2 = f"{int(_int_m2.group(1)):.{_prec_exc2}f}"
                        _el_copy_exc2 = self._copy(el)
                        _el_copy_exc2.text = _repaired_exc2
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(_el_copy_exc2), code, msg, "high")
            # ── Non-numeric amount text (e.g. 'GB', 'abc') ───────────────────
            # Reached only when there are no leading digits at all.
            if _is_amt_el and _amt_cur and not re.match(r"^-?\d+(\.\d{0,5})?$", _amt_cur):
                _ccy_n = (el.get("Ccy") or "").upper()
                _prec_n = _ccy_precision(_ccy_n) if _ccy_n else 2
                _placeholder = f"1000.{'0' * _prec_n}" if _prec_n > 0 else "1000"
                _el_copy_n = self._copy(el)
                _el_copy_n.text = _placeholder
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(_el_copy_n), code, msg, "high")

        # ── Text too long: truncate in-place, never replace ─────────────────
        # "Jhon ......(162 chars)" → "Jhon ......(140 chars)" for Max140Text.
        # GUARD: only fires for plain Max*Text types (Nm, AdrLine, Ustrd, etc.).
        # Constrained types like Currency (^[A-Z]{3}$), Country, BICFI, IBAN
        # must NOT be truncated — "150" is not a valid currency code.
        if not list(el) and el.text:
            _con_t = _kb_field_constraint(el_local, parent_local)
            _ctype_t = _con_t.get("type", "") if isinstance(_con_t, dict) else ""
            _is_maxtext_only = _ctype_t.startswith("Max") and "Text" in _ctype_t
            if _is_maxtext_only:
                _max_t = _con_t.get("max_length") if isinstance(_con_t, dict) else None
                _cur_t = (el.text or "").strip()
                if isinstance(_max_t, int) and len(_cur_t) > _max_t:
                    _trimmed = _cur_t[:_max_t].rstrip() or _cur_t[:_max_t]
                    _el_copy = self._copy(el)
                    _el_copy.text = _trimmed
                    return FixSuggestion(xpath, original_fragment,
                                          self._serialize(_el_copy), code, msg, "high")

        # ── Country code (ISO 3166-1 alpha-2) repair ─────────────────────────
        # Handles empty, malformed, or non-ISO <Ctry> values uniformly (the
        # generic constraint path skips empty elements). Validates against the
        # real 250-code list — never emits a regex-shaped-but-invalid code such
        # as 'UK'/'UN'. Works for AppHdr (head.001) and all business messages.
        if el_local in ("Ctry", "CtryOfRes", "CtryOfBirth") and not list(el):
            cur = (el.text or "").strip()
            codes = _codelist_codes("country")
            if codes and cur.upper() not in codes:
                try:
                    root_doc = el.getroottree().getroot()
                except Exception:
                    root_doc = None
                new_val = self._repair_country(el, cur, root_doc)
                if new_val and new_val != cur:
                    el_copy = self._copy(el)
                    el_copy.text = new_val
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── Enumeration repair: value not in the allowed set ─────────────────
        # "The value 'COVE' is not valid. It must be one of the following values:
        # INDA, INGA." → parse the allowed set from the message and replace with
        # the KB-preferred value (auto_fix_rules.enumeration) or the first listed.
        if not list(el):
            m_enum = re.search(r"(?:must be one of|one of the following)(?:\s+the following)?"
                               r"(?:\s+values)?\s*:?\s*(.+)$", msg, re.I | re.S)
            if m_enum:
                allowed = [v.strip().strip("'\".") for v in re.split(r"[,/]| or ", m_enum.group(1))
                           if re.match(r"^[A-Za-z0-9]{1,12}$", v.strip().strip("'\". "))]
                cur = (el.text or "").strip()
                if allowed and cur not in allowed:
                    preferred = None
                    for r in (_kb_get("auto_fix_rules.enumeration", []) or []):
                        if el_local in (r.get("elements") or []):
                            p = r.get("preferred")
                            if p in allowed:
                                preferred = p
                            break
                    new_val = preferred or allowed[0]
                    el_copy = self._copy(el)
                    el_copy.text = new_val
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── Length overflow → shorten the value to the KB/schema max_length ───
        # Retained for explicit "Field X has an invalid length" messages that
        # reach this point without a KB constraint (e.g. unknown tags).
        if ("length" in msg_l) and (not list(el)) and el.text:
            con = _kb_field_constraint(el_local, parent_local)
            max_len = con.get("max_length") if isinstance(con, dict) else None
            cur = el.text.strip()
            if isinstance(max_len, int) and len(cur) > max_len:
                trimmed = cur[:max_len].rstrip() or cur[:max_len]
                el_copy = self._copy(el)
                el_copy.text = trimmed
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── Count / sum aggregates (NbOfTxs, NbOfNtries, CtrlSum, PgNb) ────────
        # These are derived from the document, so there's a single correct
        # value — compute it rather than guessing. Without this they fell
        # through to a low-confidence no-op and silently dropped out of batches.
        # ── CdtDbtInd empty / invalid ─────────────────────────────────────────
        # CreditDebitCode: only "CRDT" or "DBIT" are valid.
        # Infer from context: look for an Amt sibling or ancestor TxAmt/Amt —
        # if the amount path contains a credit indicator in the parent tag name
        # (Bal, TxDtls, Ntry) we inspect the parent element's local name; the
        # sibling RvslInd or a parent tag "Stmt" leans toward CRDT.
        # Safest default when context is ambiguous: "CRDT".
        if el_local == "CdtDbtInd":
            cur = (el.text or "").strip()
            if cur not in ("CRDT", "DBIT"):
                try:
                    _cdi_root = el.getroottree().getroot()
                except Exception:
                    _cdi_root = None
                inferred = "CRDT"  # default
                # Walk up the ancestor chain for context clues
                parent = el.getparent()
                if parent is not None:
                    p_local = etree.QName(parent.tag).localname if isinstance(parent.tag, str) else ""
                    # Ntry (statement entry): look for Amt sibling with CdtDbtInd
                    # TxDtls: same
                    # RvslInd=true → typically DBIT reversal → keep CRDT (original was CRDT)
                    # Bal (balance) → opening/closing balance
                    # No reliable signal beyond doc context → default CRDT
                    if p_local in ("Ntry", "TxDtls", "Bal", "Rpt", "TxInf"):
                        # Look for another CdtDbtInd sibling with a value to borrow
                        for _sib in parent:
                            if (isinstance(_sib.tag, str)
                                    and etree.QName(_sib.tag).localname == "CdtDbtInd"
                                    and _sib is not el
                                    and (_sib.text or "").strip() in ("CRDT", "DBIT")):
                                inferred = (_sib.text or "").strip()
                                break
                el_copy = self._copy(el)
                el_copy.text = inferred
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        if el_local in ("NbOfTxs", "NbOfNtries", "CtrlSum", "PgNb", "LastPgInd"):
            try:
                root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                root = None
            if root is not None:
                TX_TAGS = {"CdtTrfTxInf", "DrctDbtTxInf", "TxInfAndSts", "TxInf"}
                if el_local == "NbOfTxs":
                    count = sum(
                        1 for n in root.iter()
                        if isinstance(n.tag, str)
                        and etree.QName(n.tag).localname in TX_TAGS
                    )
                    if count > 0 and str(count) != (el.text or "").strip():
                        el_copy = self._copy(el)
                        el_copy.text = str(count)
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(el_copy), code, msg, "high")

                elif el_local == "NbOfNtries":
                    # Count actual <Ntry> elements in the document (camt statement entries)
                    ntry_count = sum(
                        1 for n in root.iter()
                        if isinstance(n.tag, str)
                        and etree.QName(n.tag).localname == "Ntry"
                    )
                    # If no entries found, default to 0 (valid per XSD: [0-9]{1,15})
                    new_val = str(ntry_count) if ntry_count > 0 else "0"
                    if new_val != (el.text or "").strip():
                        el_copy = self._copy(el)
                        el_copy.text = new_val
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(el_copy), code, msg, "high")

                elif el_local == "PgNb":
                    # Default to page 1 when empty — the user deleted it
                    new_val = (el.text or "").strip() or "1"
                    if not re.match(r"^[0-9]{1,5}$", new_val):
                        new_val = "1"
                    if new_val != (el.text or "").strip():
                        el_copy = self._copy(el)
                        el_copy.text = new_val
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(el_copy), code, msg, "high")

                elif el_local == "LastPgInd":
                    # YesNoIndicator: must be "true" or "false"; default "true" when empty
                    cur = (el.text or "").strip().lower()
                    new_val = cur if cur in ("true", "false") else "true"
                    if new_val != (el.text or "").strip():
                        el_copy = self._copy(el)
                        el_copy.text = new_val
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(el_copy), code, msg, "high")

                else:  # CtrlSum — sum the interbank settlement (or instructed) amounts
                    total = 0.0
                    found = False
                    for n in root.iter():
                        if not isinstance(n.tag, str):
                            continue
                        ln = etree.QName(n.tag).localname
                        if ln in ("IntrBkSttlmAmt", "InstdAmt") and n.text:
                            try:
                                total += float(n.text.strip())
                                found = True
                            except ValueError:
                                pass
                    if found:
                        formatted = f"{total:.2f}"
                        if formatted != (el.text or "").strip():
                            el_copy = self._copy(el)
                            el_copy.text = formatted
                            return FixSuggestion(xpath, original_fragment,
                                                 self._serialize(el_copy), code, msg, "high")

        # --- Dynamic Date Rule Integration ---
        # Merge: enterprise KB global_rules.date_fix_rules + legacy ai_knowledge_base
        date_rules = {**(_enterprise_date_fix_rules() or {}), **(_kb_get("date_fix_rules", {}) or {})}
        if code in date_rules:
            rule = date_rules[code]
            # The affects_tags whitelist is a hint, not an exhaustive list — there
            # are dozens of ISO 20022 date tags (ReqdColltnDt, IntrBkSttlmDt, …).
            # For PAST_DATE_ERROR, also fire on ANY date-typed element (tag ends
            # in Dt/DtTm/Tm or the value is an ISO date/datetime) so a brand-new
            # date tag isn't a silent no-op just because it's missing from the KB.
            cur_txt = (el.text or "").strip()
            is_date_field = (
                el_local in rule.get("affects_tags", [])
                or (code == "PAST_DATE_ERROR" and (
                    el_local.endswith(("Dt", "DtTm", "Tm"))
                    or bool(re.match(r"\d{4}-\d{2}-\d{2}", cur_txt))
                ))
            )
            if is_date_field:
                import datetime
                now = datetime.datetime.now(datetime.timezone.utc)
                if code == "PAST_DATE_ERROR":
                    # Determine date-only vs dateTime from BOTH the tag name and
                    # the EXISTING value. CreDt in the AppHdr (head.001) is a
                    # dateTime even though its tag has no "Tm" — relying on the
                    # tag name alone stripped the time component and produced an
                    # invalid date-only value, creating a fresh schema error.
                    cur = (el.text or "").strip()
                    DATE_ONLY_TAGS = {
                        "IntrBkSttlmDt", "SttlmDt", "ReqdExctnDt", "ValDt",
                        "DueDt", "ExctnDt", "BirthDt", "Dt", "ReqdColltnDt",
                    }
                    has_time_in_value = "T" in cur
                    is_datetime = (
                        has_time_in_value
                        or "Tm" in el_local
                        or "Time" in el_local
                        or (el_local not in DATE_ONLY_TAGS and el_local.startswith("CreDt"))
                    )
                    new_val = now.strftime("%Y-%m-%dT%H:%M:%S+00:00") if is_datetime else now.strftime("%Y-%m-%d")
                elif code == "FUTURE_DATE_BIRTH_ERROR":
                    new_val = rule.get("dummy_value", "1990-01-15")
                elif code == "CBPR_DATETIME_Z_FORBIDDEN":
                    new_val = (el.text or "").replace("Z", "+00:00")
                elif code == "CBPR_DATETIME_MILLISECONDS":
                    new_val = re.sub(r"\.\d+", "", (el.text or ""))
                elif code == "CBPR_DATETIME_NO_TIMEZONE":
                    txt = (el.text or "").strip()
                    # ISO datetime WITHOUT timezone — append +00:00
                    _iso_dt_no_tz  = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$"
                    # ISO datetime WITH a valid explicit offset — leave unchanged only if calendar date is valid
                    _iso_dt_with_tz = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+\-]\d{2}:\d{2}$"
                    if re.match(_iso_dt_no_tz, txt) and self._is_valid_calendar_date(txt):
                        new_val = re.sub(r"\.\d+", "", txt) + "+00:00"
                    elif re.match(_iso_dt_with_tz, txt) and self._is_valid_calendar_date(txt):
                        new_val = txt  # already has a valid explicit offset and a valid calendar date
                    else:
                        # Value is not a valid ISO datetime or has an invalid calendar date (e.g. day=00) — generate a fresh one
                        new_val = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                else:
                    new_val = el.text

                el_copy = self._copy(el)
                el_copy.text = new_val
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")
        # -----------------------------------

        # ── Empty constrained leaf (e.g. <BICFI/>, <IBAN/>) → generate value ──
        # el.text is None for self-closing tags. The constraint-repair block
        # below requires el.text to be truthy, so it skips empty elements and
        # they fall through to a no-op. Handle them here: regenerate a valid
        # value from the KB constraint so the element is filled, not deleted.
        if not list(el) and not (el.text or "").strip():
            _empty_con = _kb_field_constraint(el_local)
            if _empty_con and isinstance(_empty_con, dict):
                _new_empty = self._regenerate_value(el_local, el, _empty_con, fix_hint, msg)
                if _new_empty is not None and _new_empty.strip():
                    el_copy = self._copy(el)
                    el_copy.text = _new_empty
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # ── Bad text value vs field-constraint regex: regenerate it ───────────
        # Highest-priority deterministic fix for cases like UETR=`UETR`,
        # Amt=`EU`, MsgId=`X` * 50, BICFI=`CREDITMM` (too short).
        if not list(el) and el.text:
            current_text = (el.text or "").strip()
            constraint = _kb_field_constraint(el_local, parent_local)

            # 1. Constraint violation → regenerate
            if constraint and self._violates_constraint(current_text, constraint):
                new_val = self._regenerate_value(el_local, el, constraint, fix_hint, msg)
                if new_val is not None and new_val != current_text:
                    el_copy = self._copy(el)
                    el_copy.text = new_val
                    return FixSuggestion(xpath, original_fragment,
                                          self._serialize(el_copy), code, msg, "high")

            # 2. Dependency mismatch → use the partner's value
            # Triggers when msg/code suggests an equality violation across
            # fields (e.g. "must match", "must equal", "loopback", "mismatch")
            mlc = (msg + " " + fix_hint).lower()
            if any(k in mlc for k in ("must match", "must equal", "doesn't match",
                                       "does not match", "mismatch", "align ")):
                try:
                    root = el.getroottree().getroot() if el.getroottree() is not None else None
                except Exception:
                    root = None
                if root is not None:
                    partner_val = self._harvest_dependency_partner(
                        root, xpath, el_local, constraint or {}
                    )
                    if partner_val and partner_val != current_text:
                        el_copy = self._copy(el)
                        el_copy.text = partner_val
                        return FixSuggestion(xpath, original_fragment,
                                              self._serialize(el_copy), code, msg, "high")

            # 3. Already valid → nothing to fix (no-op).
            # If this leaf's value already satisfies its FORMAT constraint
            # (UUID/BIC/IBAN/date/length/regex), the format/value defect is
            # stale or was resolved by an earlier fix in the batch. Returning a
            # no-op stops the value-guessing branches below from "repairing" a
            # valid value with the INVALID value quoted in the error message —
            # e.g. extracting 'UETR' from "...required format for 'UETR'" and
            # writing it back into <UETR>, which corrupts an already-fixed UUID.
            con = _kb_field_constraint(el_local, parent_local)
            is_format_con = (
                isinstance(con, dict)
                and con.get("type") != "codelist"
                and (con.get("type") in self._CONSTRAINT_REGEX
                     or con.get("max_length") or con.get("min_length"))
            )
            if is_format_con and not self._violates_constraint(current_text, con):
                # The value passes the generic field constraint, but the per-message
                # KB may still mandate a specific literal (e.g. BizSvc must be
                # 'swift.cbprplus.03'). Honour that before treating it as a no-op.
                try:
                    _troot = el.getroottree().getroot() if el.getroottree() is not None else None
                except Exception:
                    _troot = None
                _kbctx = _KBContext.get(_detect_family_from_tree(_troot)) if _troot is not None else None
                if _kbctx:
                    _kbval = _kbctx.literal_value(el_local, code)
                    if _kbval and _kbval != current_text and "{" not in _kbval:
                        el_copy = self._copy(el)
                        el_copy.text = _kbval
                        return FixSuggestion(xpath, original_fragment,
                                             self._serialize(el_copy), code, msg, "high")
                return FixSuggestion(xpath, original_fragment, original_fragment,
                                     code, msg, "low")

        # ── Duplicate element → keep the first occurrence, drop the extras ────
        if (code == "DUPLICATE_TAG" or "duplicate" in msg_l
                or "appears more than once" in msg_l
                or ("appears" in msg_l and "times" in msg_l)
                or "occurs more than allowed" in msg_l):
            # Identify the duplicated tag: <Tag> in text, else 'Field'/"tag X", else the element itself.
            dm = (re.search(r"<(\w+)>", msg + " " + fix_hint)
                  or re.search(r"(?:tag|field|element)\s+'?\"?<?(\w[\w]*)>?\"?", msg, re.I))
            dup = dm.group(1) if dm else el_local

            def _dedupe(parent_el):
                """Return a copy of parent_el with all but the first `dup` child removed."""
                p_copy = self._copy(parent_el)
                seen = False
                removed = False
                for child in list(p_copy):
                    if isinstance(child.tag, str) and etree.QName(child.tag).localname == dup:
                        if seen:
                            p_copy.remove(child)
                            removed = True
                        else:
                            seen = True
                return p_copy, removed

            # Case 1: the located element IS the duplicate → dedupe among siblings.
            if dup == el_local:
                parent = el.getparent()
                if parent is not None:
                    p_copy, removed = _dedupe(parent)
                    if removed:
                        return FixSuggestion(self._xpath_of(parent), self._serialize(parent),
                                             self._serialize(p_copy), code, msg, "high")
                    # Dedup found nothing: the "duplicate" is a cross-parent one caused
                    # by lxml recover=True folding orphaned elements into a sibling
                    # container. Fix by wrapping the orphaned block instead.
                    try:
                        _dup_root = el.getroottree().getroot()
                        _dup_xml = self._serialize(_dup_root)
                    except Exception:
                        _dup_root = _dup_xml = None
                    if _dup_root is not None:
                        # Cross-parent duplicates of IBAN/Id are usually repair
                        # artifacts injected INTO a simple-type leaf (Othr/Id
                        # carrying an <IBAN> child) — clean those first.
                        _leaf_fix = self._fix_elements_inside_simple_leaf(
                            _dup_root, _dup_xml, code, msg)
                        if _leaf_fix is not None:
                            return _leaf_fix
                        _wrap_msg = f"The element '{dup}' is not expected at this position."
                        _wrap = self._try_wrap_orphaned_block(_dup_root, _dup_xml,
                                                              code, _wrap_msg)
                        if _wrap is not None:
                            return _wrap
            # Case 2: the duplicate is a child of the located element.
            el_copy, removed = _dedupe(el)
            if removed:
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        if any(k in msg_l for k in ("must not", "not allowed", "forbidden",
                                     "disallowed", "cannot coexist", "may not")):
            m = re.search(r"<(\w+)>", msg)
            if m:
                el_copy = self._copy(el)
                for child in list(el_copy):
                    if isinstance(child.tag, str) and etree.QName(child.tag).localname == m.group(1):
                        el_copy.remove(child)
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── Enum / codelist fix: use our codelists for exact valid values ─────
        if not list(el):
            el_local = etree.QName(el.tag).localname

            # ── KB-preferred value: highest priority for codelist tags ────────
            kb_constraint = _kb_field_constraint(el_local, parent_local)
            kb_preferred  = kb_constraint.get("preferred") if isinstance(kb_constraint, dict) else None
            kb_valid      = kb_constraint.get("valid", [])  if isinstance(kb_constraint, dict) else []
            if kb_preferred:
                # If the hint or msg mentions a different valid code, prefer that;
                # otherwise use the KB preferred (e.g. SLEV for ChrgBr).
                combined = fix_hint + " " + msg
                chosen = kb_preferred
                for c in kb_valid:
                    if re.search(rf'\b{re.escape(c)}\b', combined):
                        chosen = c
                el_copy = self._copy(el)
                el_copy.text = chosen
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

            # ── Generate a type-aware value for the empty leaf ────────────────
            constraint = _kb_field_constraint(el_local) or {}
            new_val = self._regenerate_value(el_local, el, constraint, fix_hint, msg)
            if new_val is not None and new_val != (el.text or "").strip():
                el_copy = self._copy(el)
                el_copy.text = new_val
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

            enum_m = (re.search(r"must be ['\"]?([A-Z]{2,10})['\"]?\s+\(", msg) or
                      re.search(r"valid.*?code.*?['\"]([A-Z]{2,10})['\"]", msg, re.I) or
                      re.search(r"use ['\"]([A-Z]{2,10})['\"]", msg, re.I) or
                      re.search(r"code[:\s]+['\"]?([A-Z]{2,10})['\"]?", fix_hint, re.I))
            if enum_m:
                el_copy = self._copy(el)
                el_copy.text = enum_m.group(1)
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── Date format ───────────────────────────────────────────────────────
        if "yyyy-mm-dd" in msg_l or ("date" in msg_l and "format" in msg_l):
            el_copy = self._copy(el)
            el_copy.text = "2025-01-15"
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── BIC invalid ───────────────────────────────────────────────────────
        if ("bic" in msg_l or "bicfi" in msg_l) and "invalid" in msg_l:
            # Prefer a valid BIC from fix_hint, then harvest from the document body
            # (so AppHdr Fr matches InstgAgt and L3-PACS-MATCH-FR doesn't fire next round).
            bic_m = re.search(r'\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b', fix_hint)
            _bic_val = bic_m.group(1) if bic_m else None
            if not _bic_val:
                _BIC_PAT = re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$")
                try:
                    _bic_root = el.getroottree().getroot()
                except Exception:
                    _bic_root = None
                if _bic_root is not None:
                    _apphdr = _bic_root.find(".//{*}AppHdr")
                    _apphdr_ids = {id(x) for x in _apphdr.iter()} if _apphdr is not None else set()
                    for _b in _bic_root.iter():
                        if not isinstance(_b.tag, str) or id(_b) in _apphdr_ids:
                            continue
                        if etree.QName(_b.tag).localname == "BICFI":
                            _t = (_b.text or "").strip()
                            if _t and _BIC_PAT.match(_t):
                                _bic_val = _t
                                break
            el_copy = self._copy(el)
            el_copy.text = _bic_val or "DEUTDEFFXXX"
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── IBAN invalid ──────────────────────────────────────────────────────
        if "iban" in msg_l and "invalid" in msg_l:
            try:
                _fv_root = el.getroottree().getroot()
            except Exception:
                _fv_root = None
            el_copy = self._copy(el)
            el_copy.text = _iban_for_ccy(_fv_root, el)
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # A candidate value extracted from free-text is only acceptable if it
        # isn't just the tag name echoed back and doesn't itself violate the
        # element's constraint (guards against e.g. writing 'UETR' into <UETR>).
        def _usable_candidate(val: str) -> bool:
            if not val or val == el_local:
                return False
            con = _kb_field_constraint(el_local, parent_local)
            return not (isinstance(con, dict) and con and self._violates_constraint(val, con))

        # ── Hint contains a direct value (short, no XML) ──────────────────────
        if fix_hint.strip() and "<" not in fix_hint and len(fix_hint.strip()) <= 50:
            val_m = re.search(r"['\"]([A-Z0-9]{2,11})['\"]", fix_hint)
            if val_m and not list(el) and _usable_candidate(val_m.group(1)):
                el_copy = self._copy(el)
                el_copy.text = val_m.group(1)
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── Smart value from hint using codelists ─────────────────────────────
        smart_val = self._extract_value_from_hint(
            etree.QName(el.tag).localname, fix_hint + " " + msg, parent_local
        )
        if smart_val and not list(el) and _usable_candidate(smart_val):
            el_copy = self._copy(el)
            el_copy.text = smart_val
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── KB-documented literal value (resources/KB/<family>.json) ──────────
        # e.g. BizSvc → 'swift.cbprplus.03'. The KB is authoritative, so we trust
        # its value (only guarding against placeholders / the tag name itself).
        kb_fixes: list = []
        if not list(el):
            try:
                tree_root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                tree_root = None
            kb = _KBContext.get(_detect_family_from_tree(tree_root)) if tree_root is not None else None
            if kb:
                kb_fixes = kb.possible_fixes(code, el_local)
                kb_val = kb.literal_value(el_local, code)
                if kb_val and kb_val != el_local and kb_val != (el.text or "").strip():
                    el_copy = self._copy(el)
                    el_copy.text = kb_val
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

        # Cross-message borrow: when THIS message's KB documents no recipe for the
        # error, surface a sibling message family's documented fix (advisory only).
        cross_fixes: list = []
        if not kb_fixes and not list(el):
            cross_fixes = _cross_message_possible_fixes(code, el_local)

        # Parent/sibling context: the ordered child tags already present in the
        # element's parent, so the model places a returned fragment correctly and
        # never re-introduces a sibling that already exists.
        parent_ctx = self._parent_sibling_context(el)

        # Fall through to LLM (enriched with the KB's documented fix recipes)
        return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint,
                                  kb_fixes, cross_fixes, parent_ctx)

    def _parent_sibling_context(self, el: etree._Element) -> str:
        """One-line description of the element's parent and its existing children
        in document order — context that lets the LLM keep sibling ordering and
        avoid duplicating an element that is already present. Empty when there is
        no parent or no siblings to report."""
        try:
            parent = el.getparent()
            if parent is None:
                return ""
            pname = etree.QName(parent.tag).localname
            kids = [etree.QName(c.tag).localname for c in parent
                    if isinstance(c.tag, str)]
            if not kids:
                return ""
            return (f"Parent <{pname}> currently contains these children in order: "
                    + ", ".join(kids))
        except Exception:
            return ""

    # Documents larger than this are never sent for whole-doc LLM repair: the
    # answer would risk output-token truncation, which the 80% size guard in
    # apply() would then reject anyway.
    _WHOLE_DOC_LLM_MAX_CHARS = 20000

    def _llm_whole_doc_repair(self, xml: str, code: str,
                              msg: str) -> Optional[FixSuggestion]:
        """Last-resort LLM repair of a document that cannot be parsed at all.

        Runs only after _try_xml_recovery failed, so no element-level handler
        can operate. Sends the FULL document (never a truncated fragment) and
        accepts the answer only when it parses strictly, keeps the original
        root element, and is not significantly shorter than the original —
        apply() rejects whole-doc replacements that shrink more than 20%.
        Returns None when no acceptable repair was produced.
        """
        if not xml.strip() or len(xml) > self._WHOLE_DOC_LLM_MAX_CHARS:
            return None
        _root_m = re.search(r"<\s*(?:[\w.-]+:)?([A-Za-z][\w.-]*)", xml)
        orig_root = _root_m.group(1) if _root_m else ""
        system = (
            "You are an XML repair expert for ISO 20022 / CBPR+ messages. "
            "The document you receive is malformed and cannot be parsed. "
            "Return the ENTIRE corrected document with every element and text "
            "value preserved, fixing ONLY the markup: balance/close tags, fix "
            "mismatched or misspelled closing tags, escape stray & < > "
            "characters, fix attribute quoting, remove illegal characters. "
            "Do NOT add, remove, reorder or rename elements. Do NOT change "
            "any text values. Return ONLY the XML — no prose, no markdown "
            "fences."
        )
        scrubbed_xml, _pii_map = pii_scrub.scrub(xml)
        user = f"Error ({code}): {msg}\n\nMalformed document:\n{scrubbed_xml}"

        # Same negative cache as _llm_fallback: an unchanged broken doc that
        # already exhausted repair attempts must not re-hit the API on every
        # auto-fix round / batch issue.
        _neg_key = (system, user)
        if _LLM_NEG_CACHE.get(_neg_key):
            _LLM_NEG_CACHE.move_to_end(_neg_key)
            return None

        try:
            fix_metrics.record_llm_invoked()
        except Exception:
            pass

        accepted: Optional[str] = None
        last_available = False
        for _temp in (0.0, 0.4):
            text, available = complete(
                system, user,
                max_tokens=min(12000, max(1000, len(xml) // 2)),
                temperature=_temp,
            )
            last_available = available
            if not available or not text.strip():
                break
            frag = re.sub(r"^```[a-z]*\n?", "", text.strip(), flags=re.I)
            frag = re.sub(r"\n?```$",        "", frag,         flags=re.I).strip()
            frag = pii_scrub.restore(frag, _pii_map)
            try:
                _new_root = etree.fromstring(frag.encode("utf-8"))
            except Exception:
                continue
            if orig_root and etree.QName(_new_root.tag).localname != orig_root:
                continue
            if len(frag.strip()) < len(xml.strip()) * 0.8:
                continue  # content lost — apply() would reject it anyway
            if xml.lstrip().startswith("<?xml") and not frag.lstrip().startswith("<?xml"):
                _decl_m = re.match(r"\s*(<\?xml[^?]*\?>)", xml)
                if _decl_m:
                    frag = _decl_m.group(1) + "\n" + frag
            accepted = frag
            break

        if accepted is not None:
            return FixSuggestion("/", xml, accepted, code, msg, "high")
        if last_available:
            _LLM_NEG_CACHE[_neg_key] = True
            _LLM_NEG_CACHE.move_to_end(_neg_key)
            if len(_LLM_NEG_CACHE) > _LLM_NEG_CACHE_MAX:
                _LLM_NEG_CACHE.popitem(last=False)
            logger.warning(
                f"[FixSuggester] whole-doc LLM repair produced no valid document for {code}")
        return None

    def _llm_fallback(self, xpath: str, original_fragment: str,
                      code: str, msg: str, fix_hint: str = "",
                      kb_fixes: Optional[list] = None,
                      cross_fixes: Optional[list] = None,
                      parent_context: str = "") -> FixSuggestion:
        """Last-resort LLM call with rich context. max_tokens=400, temperature=0."""
        # Build context: include rule hint, codelists, field constraints, deps
        context_lines = []

        # ── Parent/sibling context: existing children + their order ─────────────
        if parent_context:
            context_lines.append(
                parent_context
                + ". Keep the corrected element consistent with this sibling "
                  "order and do NOT re-introduce a sibling that is already present."
            )

        # ── Per-message KB (resources/KB/<family>.json): documented fix recipes ──
        if kb_fixes:
            context_lines.append(
                "CBPR+ KB documented fixes for this field:\n"
                + "\n".join(f"- {fx}" for fx in kb_fixes[:6]))
        # ── Cross-message borrow: same error documented under OTHER families ────
        # Only present when this message's own KB had no recipe. Advisory — the
        # model must confirm it applies to the current message/namespace.
        elif cross_fixes:
            context_lines.append(
                "Documented fixes for this same error in related ISO 20022 "
                "message types (verify it applies to THIS message before using):\n"
                + "\n".join(f"- {fx}" for fx in cross_fixes[:4]))

        # ── Enterprise KB: error-specific fix recipe (highest priority context) ──
        if code:
            err_res = _enterprise_error_resolution_any(code)
            if err_res:
                recipe_parts = []
                if err_res.get("root_cause"):
                    recipe_parts.append(f"Root cause: {err_res['root_cause']}")
                if err_res.get("fix"):
                    recipe_parts.append(f"Fix recipe: {err_res['fix']}")
                if err_res.get("do_not"):
                    recipe_parts.append(f"Do NOT: {err_res['do_not']}")
                if recipe_parts:
                    context_lines.append("Error resolution guide:\n" + "\n".join(recipe_parts))

        if fix_hint:
            context_lines.append(f"Rule fix suggestion: {fix_hint}")

        # Identify which tag the error is about (from xpath)
        target_tag = ""
        if xpath:
            m = re.search(r'/([A-Za-z][A-Za-z0-9]*)(?:\[\d+\])?$', xpath)
            if m:
                target_tag = m.group(1)

        # ── Syntactic / Lexical KB: extra context for syntactic error codes ──────
        try:
            syn_kb = _load_syntactic_kb()
            syn_catalogue = syn_kb.get("error_code_catalogue", {})
            if isinstance(syn_catalogue, dict) and code in syn_catalogue:
                entry = syn_catalogue[code]
                bits = []
                if entry.get("root_cause"):
                    bits.append(f"root_cause: {entry['root_cause']}")
                if entry.get("fix"):
                    bits.append(f"fix: {entry['fix']}")
                if entry.get("do_not"):
                    bits.append(f"do_not: {entry['do_not']}")
                if bits:
                    context_lines.append("Syntactic/Lexical KB:\n" + "\n".join(f"  {b}" for b in bits))
            else:
                scope = syn_kb.get("scope_and_precedence", {})
                owns = scope.get("this_kb_owns", [])
                pipeline = scope.get("fix_pipeline_order", [])
                if owns or pipeline:
                    lines_ctx = [f"  - {o}" for o in owns[:4]]
                    if pipeline:
                        lines_ctx += [f"  {p}" for p in pipeline[:3]]
                    context_lines.append(
                        "Syntactic/Lexical KB (apply BEFORE schema/business fixes):\n"
                        + "\n".join(lines_ctx)
                    )
        except Exception as _e:
            logger.debug(f"[FixSuggester] Syntactic KB context failed: {_e}")

        # ── Per-message KB STRUCTURE (resources/KB/<msg>.json) ────────────────
        try:
            kb_tags = set()
            if target_tag:
                kb_tags.add(target_tag)
            for tok in re.findall(r"'([A-Za-z][\w]*)'|element '([A-Za-z][\w]*)'|<([A-Za-z][\w]*)>",
                                  f"{msg} {fix_hint}"):
                for g in tok:
                    if g:
                        kb_tags.add(g)
            kb_msg_type = _detect_msg_type(original_fragment) or ""
            hints = _kb_folder_structural_hints(list(kb_tags), code, kb_msg_type, original_fragment)
            if hints:
                context_lines.append("Knowledge-base structure (authoritative):\n" + "\n".join(hints))
        except Exception as e:
            logger.debug(f"[FixSuggester] KB structural hints failed: {e}")

        # ── KB field constraints: include length/type/example for the target ──
        if target_tag:
            constraint = _kb_field_constraint(target_tag)
            if isinstance(constraint, dict) and constraint:
                parts = []
                if constraint.get("type"):
                    parts.append(f"type={constraint['type']}")
                if constraint.get("max_length"):
                    parts.append(f"max_length={constraint['max_length']}")
                if constraint.get("pattern"):
                    parts.append(f"pattern={constraint['pattern']}")
                if constraint.get("example"):
                    parts.append(f"example={constraint['example']}")
                if constraint.get("valid"):
                    parts.append(f"valid_values={constraint['valid'][:10]}")
                if parts:
                    context_lines.append(f"Field constraints for <{target_tag}>: " + ", ".join(parts))

        # ── Per-message KB: enum allow-list, dependency/formal rules, child order ──
        # The per-message KB carries more than the possible_fixes already passed
        # in as kb_fixes — surface ALL of it: valid_values enum allow-lists,
        # cross_tag_dependency_rules, formal rules, and tag_insertion_order.
        try:
            _kb_ctx = _KBContext.get(getattr(self, "_kb_family", "") or "")
        except Exception:
            _kb_ctx = None
        if _kb_ctx is not None:
            if target_tag:
                _enum = _kb_ctx.valid_codes(target_tag)
                if _enum:
                    context_lines.append(
                        f"KB allowed values for <{target_tag}> (use ONLY these): "
                        + ", ".join(str(c) for c in _enum[:25]))
                _dep_lines: list = []
                for _d in _kb_ctx.dependency_rules:
                    if not isinstance(_d, dict):
                        continue
                    _aff = _d.get("affected_tags") or []
                    _hit = any(str(a).split("/")[-1] == target_tag for a in _aff) \
                        or target_tag in str(_d.get("rule", ""))
                    if _hit:
                        _txt = str(_d.get("rule", "")).strip()
                        _fx  = str(_d.get("fix", "")).strip()
                        if _txt:
                            _dep_lines.append(
                                f"- {_txt}" + (f" (fix: {_fx})" if _fx else ""))
                    if len(_dep_lines) >= 4:
                        break
                if _dep_lines:
                    context_lines.append(
                        "KB cross-tag dependency rules involving this field "
                        "(the fix must satisfy these):\n" + "\n".join(_dep_lines))
                _frm_lines = [
                    f"- {str(_r.get('description', '')).strip()}"
                    for _r in _kb_ctx.formal_rules
                    if isinstance(_r, dict)
                    and target_tag in str(_r.get("description", ""))
                ][:3]
                if _frm_lines:
                    context_lines.append(
                        "KB formal rules involving this field:\n" + "\n".join(_frm_lines))
            # Correct child order: for the target's parent (from the xpath), or
            # for the target itself when the broken element is a container —
            # prevents the LLM re-introducing sequence errors.
            _ord_tags = []
            _xp_parts = [p.split("[")[0] for p in (xpath or "").split("/") if p]
            if len(_xp_parts) >= 2:
                _ord_tags.append(_xp_parts[-2])
            if target_tag:
                _ord_tags.append(target_tag)
            for _ot in _ord_tags:
                _order = _kb_ctx.insertion_order.get(_ot)
                if _order:
                    context_lines.append(
                        f"KB child element order inside <{_ot}> "
                        "(emit children in exactly this order, skipping absent "
                        "ones): " + ", ".join(_order))
                    break

        # ── Cross-field dependency context ────────────────────────────────────
        try:
            dep_map = _enterprise_shared("cross_field_dependencies", {})
            if isinstance(dep_map, dict) and target_tag in dep_map:
                deps = dep_map[target_tag]
                deps_relevant = []
                for dep in (deps if isinstance(deps, list) else []):
                    dep_kind = dep.get("kind", "")
                    desc     = dep.get("description", "")
                    if desc:
                        deps_relevant.append(f"- ({dep_kind}) {desc}")
                if deps_relevant:
                    context_lines.append("Cross-field invariants to preserve:\n" + "\n".join(deps_relevant[:5]))
        except Exception:
            pass

        # Add relevant codelists to prompt based on error keywords
        msg_l = (msg + " " + fix_hint).lower()
        for cl_name, label in [
            ("charge_bearer",       "Valid ChrgBr codes"),
            ("service_level",       "Valid SvcLvl codes"),
            ("local_instrument",    "Valid LclInstrm codes"),
            ("status_code",         "Valid TxSts/GrpSts codes"),
            ("return_reason",       "Valid return reason codes"),
            ("cancellation_reason", "Valid cancellation reason codes"),
            ("purpose_code",        "Valid purpose codes"),
            ("entry_status",        "Valid Sts/Cd codes"),
        ]:
            if any(kw in msg_l for kw in (cl_name.replace("_", ""), cl_name.split("_")[0],
                                           label.lower().split()[1].lower())):
                codes = _codelist_codes(cl_name)[:20]
                if codes:
                    context_lines.append(f"{label}: {', '.join(codes)}")

        # Inject realistic BICFIs whenever the error touches agents/BIC fields.
        _bic_kws = ("bic", "fininstnid", "agent", "instgagt", "instdagt",
                    "dbtragt", "cdtragt", "intrmyagt", "agt", "fr", "to")
        if any(kw in msg_l for kw in _bic_kws) or any(
                kw in (code or "").lower() for kw in ("bic", "agt", "fr", "to", "agent")):
            _banks = (_kb_get("dummy_data.banks", []) or
                      _enterprise_shared("dummy_data.banks", []) or [])
            if _banks:
                _bic_entries = [
                    f"{b['bicfi']} ({b.get('name','')}, {b.get('country','')})"
                    for b in _banks[:12] if b.get("bicfi")
                ]
                context_lines.append(
                    "Use one of these real BICFIs — do NOT invent placeholders:\n"
                    + ", ".join(_bic_entries)
                )

        # ── Learned-fix context: fixes a user already accepted for this (code, tag) ──
        try:
            examples = fix_feedback.accepted_examples(code, target_tag, limit=3)
            if examples:
                context_lines.append(
                    "Previously-accepted fixes for this error (prefer this shape):\n"
                    + "\n".join(f"- {ex}" for ex in examples)
                )
        except Exception:
            pass

        context = "\n".join(context_lines)
        system = (
            "You are an ISO 20022 / CBPR+ XML expert. "
            "Return ONLY the corrected XML element — same root tag and namespace, "
            "no prose, no markdown fences. "
            "The fix must be a valid well-formed XML fragment. "
            "CRITICAL: inside <FinInstnId> always use <BICFI> — NEVER <BIC>. "
            "Valid children of FinInstnId are: BICFI, ClrSysMmbId, LEI, Nm, PstlAdr, Othr."
        )
        user = f"Rule code: {code}\nError: {msg}"
        if context:
            user += f"\n\nContext:\n{context}"
        # Scrub PII (IBANs / account numbers) from the broken fragment before it
        # leaves the process. No-op unless FIXSUGGESTER_SCRUB_PII is enabled.
        scrubbed_fragment, _pii_map = pii_scrub.scrub(original_fragment)
        user += f"\n\nBroken element:\n{scrubbed_fragment}"

        # ── Negative-cache short-circuit ──────────────────────────────────────
        # This exact prompt already exhausted self-consistency with no valid fix
        # (e.g. an earlier auto-fix round on the same unchanged element). Skip the
        # API round-trips and return the identical low-confidence decline.
        _neg_key = (system, user)
        if _LLM_NEG_CACHE.get(_neg_key):
            _LLM_NEG_CACHE.move_to_end(_neg_key)
            return FixSuggestion(xpath, original_fragment, original_fragment,
                                 code, msg, "low")

        try:
            fix_metrics.record_llm_invoked()
        except Exception:
            pass

        # ── Self-consistency: temperature 0 first (deterministic + cached); if the
        # answer fails structural/constraint validation, resample a couple of times
        # and keep the first candidate that passes. This only ever runs on the path
        # that previously returned a low-confidence decline, so it can raise the
        # hit-rate on hard cases without changing any already-successful outcome.
        _temps = (0.0, 0.4, 0.7)
        last_available = False
        accepted_xml: Optional[str] = None
        for _i, _temp in enumerate(_temps):
            text, available = complete(system, user, max_tokens=400, temperature=_temp)
            last_available = available
            if not available or not text.strip():
                break  # API unreachable — no point resampling
            frag = re.sub(r"^```[a-z]*\n?", "", text.strip(), flags=re.I)
            frag = re.sub(r"\n?```$",        "", frag,         flags=re.I).strip()
            frag = pii_scrub.restore(frag, _pii_map)
            candidate = self._validate_llm_fragment(frag, original_fragment, target_tag)
            if candidate is not None:
                accepted_xml = candidate
                if _i > 0:
                    try:
                        fix_metrics.record_self_consistency(True)
                    except Exception:
                        pass
                break
            if _i == len(_temps) - 1:
                try:
                    fix_metrics.record_self_consistency(False)
                except Exception:
                    pass

        if accepted_xml is not None:
            return FixSuggestion(xpath, original_fragment, accepted_xml,
                                 code, msg, "high")

        if not last_available:
            logger.warning(f"[FixSuggester] LLM unavailable for {code}; returning low-confidence original")
        else:
            # API answered but no candidate passed validation. Remember this exact
            # prompt so a later round on the same unchanged element declines
            # instantly instead of resampling. Only cache genuine exhaustion —
            # never transient unreachability (last_available False above).
            _LLM_NEG_CACHE[_neg_key] = True
            _LLM_NEG_CACHE.move_to_end(_neg_key)
            if len(_LLM_NEG_CACHE) > _LLM_NEG_CACHE_MAX:
                _LLM_NEG_CACHE.popitem(last=False)
            logger.warning(f"[FixSuggester] LLM returned no valid fix for {code}; declining")
        return FixSuggestion(xpath, original_fragment, original_fragment, code, msg, "low")

    def _validate_llm_fragment(self, frag: str, original_fragment: str,
                               target_tag: str) -> Optional[str]:
        """Structurally validate an LLM fragment before trusting it.

        Returns the serialised XML when acceptable, else None. Enforces:
          • well-formed XML,
          • root local-name matches the broken element (no off-topic answer),
          • <BIC> renamed to <BICFI> (CBPR+ requirement),
          • KB field constraints for the target tag (pattern / max_length /
            enum) — the model is checked against the same rules we already know,
            so a fix that violates a known constraint is rejected, not shown.
        """
        try:
            new_el = etree.fromstring(frag.encode("utf-8"))
        except Exception:
            return None
        if original_fragment.strip():
            try:
                orig_local = etree.QName(
                    etree.fromstring(original_fragment.encode("utf-8")).tag
                ).localname
                if etree.QName(new_el.tag).localname != orig_local:
                    return None
            except Exception:
                # original_fragment may be truncated (e.g. root[:1500]) and fail to
                # parse. Fall back to target_tag (derived from xpath last segment) so
                # the root-tag check still fires — prevents LLM from returning a
                # tiny fragment (<FinInstnId>) that replaces the whole BusMsgEnvlp.
                if target_tag and etree.QName(new_el.tag).localname != target_tag:
                    return None
        # Size guard: reject if LLM returned a fragment dramatically smaller than
        # the original. Catches hallucinations where the model returns a single leaf
        # (<FinInstnId>) as a replacement for a large parent (BusMsgEnvlp/AppHdr).
        # Only applies when original is large enough to have real content (>200 chars)
        # — small originals are leaf elements and any valid fix is fine.
        _orig_stripped_len = len(original_fragment.strip())
        _frag_stripped_len = len(frag.strip())
        if _orig_stripped_len > 200 and _frag_stripped_len < _orig_stripped_len * 0.25:
            return None
        for _el in new_el.iter():
            if isinstance(_el.tag, str) and etree.QName(_el.tag).localname == "BIC":
                ns = etree.QName(_el.tag).namespace
                _el.tag = f"{{{ns}}}BICFI" if ns else "BICFI"
        # Reject fragments where the LLM hallucinated envelope-level elements
        # (Document, AppHdr, BusMsgEnvlp) as descendants. These belong at the
        # BusMsgEnvlp root level and must never appear inside party blocks like
        # <Fr>, <To>, <FIId>, etc. Accepting such a fragment would corrupt the
        # whole document structure in a way _normalize_busmsgenvlp can't recover.
        _ENVELOPE_LOCAL = {"Document", "AppHdr", "BusMsgEnvlp", "BusMsg"}
        _root_local = etree.QName(new_el.tag).localname
        for _desc in new_el.iter():
            if _desc is new_el:
                continue
            if (isinstance(_desc.tag, str)
                    and etree.QName(_desc.tag).localname in _ENVELOPE_LOCAL):
                return None
        if target_tag and not self._fragment_satisfies_constraints(new_el, target_tag):
            return None
        return self._serialize(new_el)

    def _fragment_satisfies_constraints(self, new_el: etree._Element,
                                        target_tag: str) -> bool:
        """Check the target tag's leaf value against KB field constraints.
        Conservative: returns True whenever there is nothing concrete to fail on
        (no KB entry, structural element, unparsable pattern)."""
        try:
            constraint = _kb_field_constraint(target_tag)
        except Exception:
            return True
        if not isinstance(constraint, dict) or not constraint:
            return True
        el = new_el if etree.QName(new_el.tag).localname == target_tag else next(
            (e for e in new_el.iter()
             if isinstance(e.tag, str) and etree.QName(e.tag).localname == target_tag),
            None,
        )
        if el is None:
            return True
        val = (el.text or "").strip()
        if not val:
            return True  # structural element — no leaf value to constrain
        pat = constraint.get("pattern")
        if pat:
            try:
                if not re.fullmatch(pat, val):
                    return False
            except re.error:
                pass
        mx = constraint.get("max_length")
        if isinstance(mx, int) and mx > 0 and len(val) > mx:
            return False
        valid = constraint.get("valid")
        if isinstance(valid, list) and valid and val not in valid:
            return False
        return True

    # ── XSD completeness pass ─────────────────────────────────────────────────

    def xsd_completeness_pass(self, xml: str) -> str:
        """Proactively insert missing mandatory elements and remove excess/invalid
        elements by scanning the document against its XSD.

        Uses _XsdTypeMap (already loaded from XSD) to enumerate every sequence
        container's children with minOccurs / maxOccurs.  For each container in the
        live document:
          • missing mandatory child (minOccurs >= 1, absent) → _build_child inserts it
          • excess children beyond maxOccurs → extras removed (keep first N)
          • simple-type element with child elements (mixed content) → children stripped
          • empty optional container where mandatory children can't be built → removed

        Only acts on sequence types for insertion/removal (not choice — can't auto-
        select the right member). Mixed-content and empty-container cleanup is XSD-
        type-driven. Silently returns original xml if XSD unavailable or parse fails.
        """
        try:
            xsd_path = self._get_xsd_path(xml)
            tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
            if not tmap:
                return xml
            parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=False)
            try:
                root = etree.fromstring(xml.encode("utf-8"), parser)
            except etree.XMLSyntaxError:
                return xml  # not well-formed — syntax fixer should run first
            msg_type = _detect_msg_type(xml) or ""
            changed = False

            def local(tag: str) -> str:
                return etree.QName(tag).localname if isinstance(tag, str) else ""

            # ── Pass 1: mixed-content cleanup ────────────────────────────────
            # Simple-type or simpleContent elements must not have child elements.
            # Pattern: <Id>TEXT<IBAN>VALUE</IBAN></Id> inside Othr — strip the
            # child elements, keep the text content (plain account ID string).
            for el in list(root.iter()):
                if not isinstance(el.tag, str):
                    continue
                if len(el) == 0:
                    continue  # no children — nothing to fix
                el_path = self._local_name_path(el)
                el_type = tmap.type_of_path(el_path)
                if not el_type:
                    continue
                kind = tmap.type_info.get(el_type, {}).get("kind", "")
                if kind not in ("simple", "simpleContent"):
                    continue
                # This element should be text-only; child elements are invalid.
                # Strip all child elements — preserve text content.
                for child in list(el):
                    el.remove(child)
                changed = True

            # ── Pass 2: sequence completeness (insert/remove/clean) ──────────
            for el in list(root.iter()):
                if not isinstance(el.tag, str):
                    continue
                el_path = self._local_name_path(el)
                el_type = tmap.type_of_path(el_path)
                if not el_type:
                    continue
                type_info = tmap.type_info.get(el_type, {})
                if type_info.get("kind") != "sequence":
                    continue
                ns = etree.QName(el.tag).namespace or ""
                children_schema = type_info.get("children", [])

                # Collect which child names are mandatory in this container
                mandatory_names: set[str] = set()
                for cs in children_schema:
                    try:
                        if int(cs.get("min", "1")) >= 1:
                            mandatory_names.add(cs["name"])
                    except (ValueError, TypeError):
                        pass

                for cs in children_schema:
                    cname = cs["name"]
                    try:
                        min_int = int(cs.get("min", "1"))
                        max_raw = cs.get("max", "1")
                        max_int = 9999 if max_raw == "unbounded" else int(max_raw)
                    except (ValueError, TypeError):
                        continue

                    existing = [c for c in el if isinstance(c.tag, str) and local(c.tag) == cname]
                    count = len(existing)

                    # Remove excess beyond maxOccurs (keep the first max_int)
                    if max_int < 9999 and count > max_int:
                        for extra in existing[max_int:]:
                            el.remove(extra)
                        changed = True
                        count = max_int

                    # Insert missing mandatory elements
                    if min_int >= 1 and count == 0:
                        built = self._build_child(
                            cname, "", ns, tmap,
                            path_parts=el_path + [cname],
                            root=root, msg_type=msg_type,
                        )
                        if built is None:
                            continue
                        idx = self._find_insert_index(el, cname, tmap, parent_path=el_path)
                        if idx is None:
                            el.append(built)
                        else:
                            el.insert(idx, built)
                        changed = True

            # ── Pass 3: remove empty optional containers ──────────────────────
            # An element that is optional in its parent (min=0) AND completely
            # empty (no children, no text) is a placeholder/artefact — remove it
            # when either:
            #   a) its own type requires mandatory children (incomplete skeleton), OR
            #   b) it is a CashAccount-type element (Acct) — these are always
            #      meaningless when empty (the XSD may declare Id as optional, but
            #      an account with zero identification is invalid in practice).
            for el in list(root.iter()):
                if not isinstance(el.tag, str):
                    continue
                if len(el) > 0 or (el.text or "").strip():
                    continue  # has content — leave alone
                parent = el.getparent()
                if parent is None:
                    continue
                # Check if this element is optional in its parent schema
                parent_path = self._local_name_path(parent)
                parent_type = tmap.type_of_path(parent_path)
                if not parent_type:
                    continue
                parent_info = tmap.type_info.get(parent_type, {})
                if parent_info.get("kind") != "sequence":
                    continue
                el_local = local(el.tag)
                cs_match = next(
                    (c for c in parent_info.get("children", []) if c["name"] == el_local),
                    None,
                )
                if cs_match is None:
                    continue
                try:
                    el_min_in_parent = int(cs_match.get("min", "1"))
                except (ValueError, TypeError):
                    continue
                if el_min_in_parent >= 1:
                    continue  # mandatory in parent — don't remove
                # Condition a): own type requires mandatory children
                el_path = self._local_name_path(el)
                el_type = tmap.type_of_path(el_path)
                el_type_info = tmap.type_info.get(el_type or "", {})
                el_mandatory_children = [
                    c["name"] for c in el_type_info.get("children", [])
                    if c.get("min", "1") not in ("0",) and c.get("min", "1") != 0
                ]
                # Condition b): empty CashAccount-type element (Acct suffix)
                is_empty_acct = (
                    el_local.endswith("Acct")
                    and (el_type or "").startswith("CashAccount")
                )
                if el_mandatory_children or is_empty_acct:
                    parent.remove(el)
                    changed = True

            if not changed:
                return xml
            fixed = etree.tostring(root, encoding="unicode", pretty_print=True)
            decl_m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
            if decl_m:
                fixed = decl_m.group(1) + "\n" + fixed
            return fixed
        except Exception as e:
            logger.debug(f"[xsd_completeness_pass] skipped: {e}")
            return xml

    # ── Batch suggestion ──────────────────────────────────────────────────────

    def suggest_batch(self, xml: str, issues: list[dict], version: str = None) -> list[FixSuggestion]:
        """
        Produce one suggestion per issue. To guarantee that N independent
        issues all land in the final document (and don't overwrite each other
        when two issues target the same element), each suggestion is computed
        against the XML *after* every preceding actionable fix has been
        applied. The returned `original_fragment` / `fragment_xml` therefore
        reflect a coherent sequence that `apply_batch` can replay.

        Overlapping-defect handling: one underlying defect frequently raises
        several rule codes on the SAME element — e.g. a past datetime with no
        offset trips both PAST_DATE_ERROR and CBPR_DATETIME_NO_TIMEZONE on the
        same <CreDtTm>. The first fix corrects the element; later fixes that
        target the same element then come back as no-ops. Those are NOT
        failures — the defect is already resolved — so we tag them with
        confidence "resolved" instead of leaving them looking unfixed.
        """
        if len(issues) > 20:
            raise ValueError("suggest_batch accepts at most 20 issues per call")

        suggestions: list[FixSuggestion] = []
        current_xml = xml
        changed_xpaths: set[str] = set()
        for issue in issues:
            try:
                sug = self.suggest(current_xml, issue, version=version)
            except Exception as e:
                logger.warning(
                    f"[FixSuggester] suggest() failed for "
                    f"{issue.get('code','?')} @ {issue.get('path','?')}: {e}"
                )
                sug = self._unavail(
                    str(issue.get("path", "")),
                    str(issue.get("code", "")),
                    str(issue.get("message", "")),
                )

            is_actionable = (
                sug.confidence in ("high", "low")
                and sug.xpath
                and sug.fragment_xml
                and sug.fragment_xml != sug.original_fragment
            )

            if is_actionable:
                try:
                    current_xml = self.apply(current_xml, sug.xpath, sug.fragment_xml)
                    changed_xpaths.add(sug.xpath)
                except FixApplyError as e:
                    logger.warning(
                        f"[FixSuggester] suggest_batch: skipping rollforward for "
                        f"{sug.xpath} ({sug.issue_code}): {e}"
                    )
            else:
                if sug.xpath and sug.xpath in changed_xpaths:
                    sug = FixSuggestion(
                        sug.xpath, sug.original_fragment, sug.fragment_xml,
                        sug.issue_code, sug.issue_message, "resolved",
                    )

            suggestions.append(sug)

        # Roll-forward coherence: each actionable fragment was built against the
        # XML state at the moment its issue was processed — so an EARLIER fix's
        # fragment does NOT contain the changes of LATER fixes in the same
        # parent/ancestor lineage. apply_batch replays all fragments against the
        # ORIGINAL xml, where replaying a stale ancestor fragment would wipe a
        # later descendant change (lost fix), and replaying a descendant after a
        # rolled-forward ancestor would re-insert content the ancestor already
        # has (DUPLICATE). Fix: re-serialise every actionable fragment from the
        # FINAL fully-rolled-forward tree, so each xpath's fragment reflects ALL
        # nested fixes. Combined with apply_batch's outermost-wins collapse, the
        # replay then reproduces current_xml exactly — no losses, no duplicates.
        try:
            _final_root = self._parse_xml(current_xml)
        except Exception:
            _final_root = None
        if _final_root is not None:
            refreshed: list[FixSuggestion] = []
            for sug in suggestions:
                if (sug.confidence in ("high", "low")
                        and sug.xpath and sug.xpath not in ("/", "")
                        and sug.fragment_xml
                        and sug.fragment_xml != sug.original_fragment):
                    _el = self._find_by_xpath(_final_root, sug.xpath)
                    if _el is not None:
                        sug = FixSuggestion(
                            sug.xpath, sug.original_fragment,
                            self._serialize(_el),
                            sug.issue_code, sug.issue_message, sug.confidence,
                        )
                refreshed.append(sug)
            suggestions = refreshed
        return suggestions

    # ── XML syntax recovery ───────────────────────────────────────────────────

    def _escape_reserved_xml_chars(self, xml: str) -> Optional[str]:
        """
        Escape unescaped XML reserved characters in text content and attribute
        values. Targets the most common causes of "values contain no reserved
        XML characters" errors — principally the unescaped ampersand `&`.
        """
        pattern = r'&(?!(?:amp|lt|gt|apos|quot|#[0-9]+|#x[0-9a-fA-F]+);)'
        fixed = re.sub(pattern, '&amp;', xml)
        return fixed if fixed != xml else None

    def _try_surgical_missing_opening_tag_fix(self, xml: str, msg: str) -> Optional[str]:
        """
        Surgical repair for a missing OPENING tag (the inverse of
        _try_surgical_unclosed_tag_fix, which fixes a missing CLOSING tag).

        lxml reports both situations with the identical message shape:
          "Unclosed tag <PmtId> at line 34. The tag <PmtId> must be closed
           with </PmtId> before the closing tag </EndToEndId> at line 36."

        Here <PmtId> itself is fine — the real defect is that <EndToEndId>
        was never opened, so its orphaned </EndToEndId> forces lxml to close
        the still-open <PmtId> early (and reparents PmtId's remaining
        children as PmtId's siblings, producing duplicate-tag errors on
        later rounds).

        Distinguish from a genuinely-missing closing tag by counting
        <conflict_tag>/</conflict_tag> occurrences in the whole document: if
        there are MORE closes than opens, the orphaned close has no matching
        open anywhere — insert <conflict_tag> immediately before the
        orphaned text on its line, restoring the original structure.
        """
        m = re.search(
            r"Unclosed\s+tag\s+<?([\w:]+)>?.*?"
            r"before\s+the\s+closing\s+tag\s+</?([\w:]+)>?.*?at\s+line\s+(\d+)",
            msg, re.I | re.S,
        )
        if not m:
            return None

        conflict_tag = m.group(2)
        conflict_ln = int(m.group(3))

        n_open = len(re.findall(r"<" + re.escape(conflict_tag) + r"(?=[\s/>])", xml))
        n_close = len(re.findall(r"</" + re.escape(conflict_tag) + r"\s*>", xml))
        if n_close <= n_open:
            return None  # not an orphaned-close situation; let Stage 2 handle it

        lines = xml.splitlines(keepends=True)
        if not (1 <= conflict_ln <= len(lines)):
            return None
        target_line = lines[conflict_ln - 1]

        # [optional preceding tags/whitespace][orphaned text]</conflict_tag>
        m2 = re.match(
            r"^(.*?)([^<>\r\n]+)(</" + re.escape(conflict_tag) + r"\s*>)",
            target_line,
        )
        if not m2 or not m2.group(2).strip():
            return None

        fixed_line = (m2.group(1) + f"<{conflict_tag}>" + m2.group(2) + m2.group(3)
                       + target_line[m2.end():])
        fixed_lines = lines[:conflict_ln - 1] + [fixed_line] + lines[conflict_ln:]
        fixed_xml = "".join(fixed_lines)
        try:
            etree.fromstring(fixed_xml.encode("utf-8"))
            return fixed_xml
        except etree.XMLSyntaxError:
            return None

    def _try_surgical_unclosed_tag_fix(self, xml: str, msg: str) -> Optional[str]:
        """
        Surgical Stage-0 repair for a missing closing tag.

        Handles two message formats:
          A) lxml: "Unclosed tag <FinInstnId> at line 13. The tag <FinInstnId> must be
             closed with </FinInstnId> before the closing tag </FIId> at line 13."
          B) user-facing: "Add </FinInstnId> to close the open tag ... before the
             </FIId> closing tag at line 13"

        Returns the fully-parsed fixed XML if the single insertion resolves all
        parse errors, OR passes the partially-patched XML through _balance_xml_tags
        when other structural errors remain (common when the document has multiple
        simultaneous breakages).
        """
        # Format A: lxml native error (may include "at line N" before "before the closing tag")
        m = re.search(
            r"Unclosed\s+tag\s+<?([\w:]+)>?.*?"
            r"before\s+the\s+closing\s+tag\s+</?([\w:]+)>?.*?at\s+line\s+(\d+)",
            msg, re.I | re.S,
        )
        # Format B: "Add </X> to close the open tag ... before the </Y> closing tag at line N"
        if not m:
            m = re.search(
                r"Add\s+</?([\w:]+)>?\s+to\s+close\s+the\s+open\s+tag.*?"
                r"before\s+the\s+</?([\w:]+)>\s+closing\s+tag\s+at\s+line\s+(\d+)",
                msg, re.I | re.S,
            )
        if not m:
            return None

        missing_tag  = m.group(1)
        conflict_tag = m.group(2)
        conflict_ln  = int(m.group(3))

        lines = xml.splitlines(keepends=True)
        if not (1 <= conflict_ln <= len(lines)):
            return None

        target_line = lines[conflict_ln - 1]
        close_pat = re.compile(
            r"(</" + re.escape(conflict_tag) + r"\s*>|"
            r"</[\w]+:" + re.escape(conflict_tag) + r"\s*>)",
            re.I,
        )
        if not close_pat.search(target_line):
            return None

        indent = re.match(r"(\s*)", target_line).group(1)

        # Try 1: insert BEFORE </conflict_tag> (most common: <FinInstnId></FIId> → <FinInstnId></FinInstnId></FIId>)
        fixed_line_before = close_pat.sub(
            f"</{missing_tag}>" + r"\1",
            target_line, count=1,
        )
        fixed_lines_before = lines[:conflict_ln - 1] + [fixed_line_before] + lines[conflict_ln:]
        fixed_xml_before = "".join(fixed_lines_before)
        try:
            etree.fromstring(fixed_xml_before.encode("utf-8"))
            return fixed_xml_before
        except etree.XMLSyntaxError:
            pass

        # Try 2: insert AFTER </conflict_tag>
        fixed_line_after = close_pat.sub(
            r"\1" + f"\n{indent}</{missing_tag}>",
            target_line, count=1,
        )
        fixed_lines_after = lines[:conflict_ln - 1] + [fixed_line_after] + lines[conflict_ln:]
        fixed_xml_after = "".join(fixed_lines_after)
        try:
            etree.fromstring(fixed_xml_after.encode("utf-8"))
            return fixed_xml_after
        except etree.XMLSyntaxError:
            pass

        # The single insertion didn't produce a fully valid document — other
        # structural errors remain (e.g. truncated tags, mismatched closes).
        # Strip any split/truncated tags first, then run the balance engine
        # on the partially-fixed XML so all breakages are resolved in one pass.
        for _partial in (fixed_xml_before, fixed_xml_after):
            _cleaned = self._strip_split_tags(_partial) or _partial
            _balanced = self._balance_xml_tags(_cleaned)
            if _balanced is not None and _balanced != xml:
                _normalized = self._normalize_busmsgenvlp(_balanced)
                return _normalized if _normalized != _balanced else _balanced

        return None

    @staticmethod
    def _fix_stray_closing_tag(xml: str, msg: str) -> Optional[str]:
        """Thin shim: delegates to the full tag-balance engine."""
        return FixSuggester._balance_xml_tags(xml)

    @staticmethod
    def _strip_empty_apphdr_frto_closes(xml: str) -> Optional[str]:
        """
        Clean up broken Fr/To blocks in the AppHdr header area.

        Handles three sub-patterns that all mean "the Fr or To content was
        deleted and what remains is garbage the balance engine can't reconstruct":

        A) Pure orphaned closes (no opening tags):
               \\n    </FIId>\\n</Fr>
               \\n    </FinInstnId>\\n</FIId>\\n</To>

        B) Stray text + orphaned close (BICFI value leaked out of the deleted
           Fr block, e.g.):
               \\n\\t\\tBNPPGB2LXXX</BICFI>\\n    <To> ...

        C) <Fr> or <To> present but contains a bare <BICFI> as direct child
           (FIId/FinInstnId wrappers deleted).  Strip the To/Fr content; the
           Problem 4 handler will rebuild FIId/FinInstnId/BICFI from scratch.

        In all cases the result is a parseable (or at least better) document
        that _normalize_busmsgenvlp Problem 4 can then fully repair.
        """
        import re as _re

        ah_open = _re.search(r'<AppHdr\b[^>]*>', xml)
        if not ah_open:
            return None

        after_ah = xml[ah_open.end():]
        changed = False

        # ── Pattern B: stray text + orphaned </BICFI> before the first real
        # opening tag inside AppHdr  (e.g. "\n\t\tBNPPGB2LXXX</BICFI>\n<To>")
        # Strip everything from after AppHdr open up to (but not including) the
        # first recognised AppHdr-level open tag or the first <Fr>/<To>/<BizMsgIdr>
        _first_legit_open = _re.search(
            r'<(?:Fr|To|FIId|BizMsgIdr|MsgDefIdr|BizSvc|CreDt)\b', after_ah)
        _first_close_b = _re.search(
            r'</(?:BICFI|FinInstnId|FIId|Fr)\s*>', after_ah)
        if (_first_close_b is not None
                and (_first_legit_open is None
                     or _first_close_b.start() < _first_legit_open.start())):
            # There is an orphaned close before the first real open → strip
            # everything before the first legit open (including stray text)
            if _first_legit_open:
                after_ah = after_ah[_first_legit_open.start():]
            else:
                # No legit open at all — strip up to the end of the close cluster
                after_ah = _re.sub(
                    r'^[\s\S]*?(?:</(?:BICFI|FinInstnId|FIId|Fr|To)\s*>\s*)+',
                    '', after_ah, count=1)
            changed = True

        # ── Pattern A: pure whitespace + orphaned close cluster at start
        _orphan_close_re = _re.compile(
            r'^[\s]*(?:</(?:FIId|Fr|FinInstnId|To|BICFI)>[\s]*)+',
            _re.MULTILINE,
        )
        _first_open_a = _re.search(r'<[A-Za-z]', after_ah)
        _first_close_a = _re.search(r'</(?:FIId|Fr|FinInstnId|To)\s*>', after_ah)
        if (_first_close_a is not None
                and (_first_open_a is None
                     or _first_open_a.start() > _first_close_a.start())):
            cleaned = _orphan_close_re.sub('', after_ah, count=1)
            if cleaned != after_ah:
                after_ah = cleaned
                changed = True

        # ── Pattern C: <Fr> or <To> open but contains bare <BICFI> as direct
        # child (FIId/FinInstnId wrappers missing).  Detect by parsing and
        # checking; if found, strip the inner content so Problem 4 rebuilds it.
        # We do this on the raw text: find <Fr>…</Fr> or <To>…</To> blocks that
        # contain a <BICFI> but no <FIId>.
        for _ft in ('Fr', 'To'):
            _blk = _re.search(
                rf'(<{_ft}>)([\s\S]*?)</{_ft}>',
                after_ah)
            if _blk:
                inner = _blk.group(2)
                has_bicfi = bool(_re.search(r'<BICFI\b', inner))
                has_fiid  = bool(_re.search(r'<FIId\b',  inner))
                has_fininstnid = bool(_re.search(r'<FinInstnId\b', inner))
                # If BICFI is present but the required wrappers are absent,
                # strip the inner content — Problem 4 will rebuild the chain.
                if has_bicfi and (not has_fiid or not has_fininstnid):
                    after_ah = after_ah[:_blk.start(2)] + '\n        ' + after_ah[_blk.end(2):]
                    changed = True

        if not changed:
            return None
        result = xml[:ah_open.end()] + after_ah
        if result == xml:
            return None
        return result

    @staticmethod
    def _strip_split_tags(xml: str) -> Optional[str]:
        """
        Remove truncated/split XML tags where the tag delimiter spans a newline,
        e.g. ``</CdtrAg\n\t\t\t\t\tNm>`` — the tag name was broken across lines
        making it untokenizable by both lxml and _balance_xml_tags.

        Pattern matched: ``</partial-name  WHITESPACE/NEWLINE  non-<>-chars>``
        The entire fragment (from ``</`` to the closing ``>``) is removed so the
        surrounding content (orphaned close tags, text nodes) is left intact for
        the balance engine to reconstruct.

        Returns the cleaned XML string if any split tags were removed, else None.
        """
        import re as _re
        # Match: </ then a partial tag name (word chars), then whitespace that
        # includes at least one newline, then any non-<> chars, then >
        _split_tag_re = _re.compile(r'</[\w:.-]+[ \t]*\n[^<>]*>', _re.DOTALL)
        fixed = _split_tag_re.sub('', xml)
        return fixed if fixed != xml else None

    @staticmethod
    def _balance_xml_tags(xml: str) -> Optional[str]:
        """
        Scan every tag token in the raw XML text and re-insert any missing
        opening tags at the correct position, preserving all existing content.

        Algorithm
        ---------
        1. Tokenise the raw text into a list of (kind, name, raw, char_offset)
           tuples where kind ∈ {'open', 'close', 'self', 'decl', 'text'}.
           Namespace prefixes are stripped for matching; the original raw token
           text (including namespace and attributes) is kept for reconstruction.

        2. Walk tokens left-to-right maintaining an open-tag stack.
           When a closing tag </Y> is found with no matching open <Y> anywhere
           in the current stack → </Y> is an *orphaned close*: its opening tag
           was deleted.

        3. For each orphaned close we insert a synthetic opening tag immediately
           before the first token that logically belongs inside it.
           Heuristic: insert just before the first non-text content token
           that sits between the previous stack frame's open and this close.

        4. Serialise the patched token list back to a string.
           If the result parses cleanly with lxml → return it.
           Otherwise return None (don't corrupt the document).
        """
        import re as _re

        # ── Tokeniser ──────────────────────────────────────────────────────────
        # Matches: XML declaration, comments, CDATA, self-closing, open, close tags
        _TOK = _re.compile(
            r'(<\?[^?]*\?>)'           # XML declaration / PI
            r'|(<!--.*?-->)'            # comment
            r'|(<!\[CDATA\[.*?\]\]>)'  # CDATA
            r'|(<([\w:.-]+)([^>]*?)/\s*>)'  # self-closing  <Tag … />
            r'|(<([\w:.-]+)([^>]*)>)'       # opening tag   <Tag … >
            r'|(</([\w:.-]+)\s*>)',          # closing tag   </Tag>
            _re.DOTALL,
        )

        tokens = []   # list of [kind, localname, raw_text, insert_before_idx]
        pos = 0
        for m in _TOK.finditer(xml):
            if m.start() > pos:
                tokens.append(['text', '', xml[pos:m.start()], -1])
            if m.group(1):
                tokens.append(['decl', '', m.group(0), -1])
            elif m.group(2):
                tokens.append(['comment', '', m.group(0), -1])
            elif m.group(3):
                tokens.append(['cdata', '', m.group(0), -1])
            elif m.group(4):   # self-closing
                local = m.group(5).split(':')[-1]
                tokens.append(['self', local, m.group(0), -1])
            elif m.group(7):   # opening
                local = m.group(8).split(':')[-1]
                tokens.append(['open', local, m.group(0), -1])
            elif m.group(10):  # closing — group(10)=full "</Tag>", group(11)=tag name
                local = m.group(11).split(':')[-1]
                tokens.append(['close', local, m.group(0), -1])
            pos = m.end()
        if pos < len(xml):
            tokens.append(['text', '', xml[pos:], -1])

        # ── Balance pass ───────────────────────────────────────────────────────
        # stack entries: (localname, token_index_of_open_tag)
        stack: list = []
        insertions: list = []   # (insert_before_token_idx, tag_raw_text)
        # last_child_end[depth] tracks the token index of the last close tag
        # that was MATCHED at each stack depth. Used so sibling orphans are
        # inserted AFTER the previous sibling's close, not before it.
        last_child_end: dict = {}   # stack_depth -> last matched-close token idx

        i = 0
        while i < len(tokens):
            kind, local, raw, _ = tokens[i]

            if kind == 'open':
                stack.append((local, i))

            elif kind == 'close':
                # Walk the stack looking for the matching open
                matched = False
                for depth in range(len(stack) - 1, -1, -1):
                    if stack[depth][0] == local:
                        # Any elements above `depth` were opened but not yet
                        # closed — insert synthetic closes for them right before
                        # this closing tag (innermost first).
                        for _auto_local, _auto_open_idx in reversed(stack[depth + 1:]):
                            _auto_indent = ''
                            if _auto_open_idx > 0 and tokens[_auto_open_idx - 1][0] == 'text':
                                _t2 = tokens[_auto_open_idx - 1][2]
                                _nl2 = _t2.rfind('\n')
                                if _nl2 != -1:
                                    _auto_indent = _re.match(r'(\s*)', _t2[_nl2 + 1:]).group(1)
                            insertions.append((i, f'{_auto_indent}</{_auto_local}>'))
                        # Pop everything above
                        stack = stack[:depth]
                        # Record this close as the last child end at parent depth
                        parent_depth = len(stack) - 1
                        last_child_end[parent_depth] = i
                        matched = True
                        break

                if not matched:
                    # Orphaned close: </local> has no open on the stack.
                    # Insert a synthetic <local> open at the correct position.
                    #
                    # Correct insert point = right after the last matched sibling
                    # close at this parent's depth (if any), otherwise right after
                    # the parent's own open tag.  This ensures sibling orphans like
                    # </Fr> and </To> each get their open inserted in the right slot
                    # rather than both collapsing to the parent's first-child position.
                    parent_depth = len(stack) - 1
                    parent_open_tok_idx = stack[-1][1] if stack else 0
                    # insert_at = right after the last matched sibling close
                    # (or right after the parent open if no previous sibling).
                    # Everything between scan_from and i belongs inside the
                    # missing open tag — wrap all of it.
                    scan_from = last_child_end.get(parent_depth, parent_open_tok_idx) + 1
                    insert_at = scan_from  # insert open at start of its content

                    # The orphaned close tells us the indent level of this element.
                    close_indent = ''
                    for j in range(i - 1, -1, -1):
                        if tokens[j][0] == 'text':
                            t = tokens[j][2]
                            nl = t.rfind('\n')
                            if nl != -1:
                                close_indent = _re.match(r'(\s*)', t[nl + 1:]).group(1)
                            break
                    syn_open = f'{close_indent}<{local}>\n'
                    insertions.append((insert_at, syn_open))
                    # The orphaned close IS the matching close for this synthetic
                    # open — record it as matched so last_child_end is updated,
                    # and do NOT push onto the stack (avoids duplicate close from
                    # auto-pop when the parent element later closes).
                    last_child_end[parent_depth] = i

            i += 1

        # ── Pass 2: unclosed open tags ──────────────────────────────────────
        # Any element still on the stack was opened but never closed.
        # Append synthetic closing tags in reverse stack order (innermost first).
        # Skip the document-root element (depth 0) — lxml will reject a bare
        # root close appended after BusMsgEnvlp already has its close.
        for _local, _open_idx in reversed(stack[1:]):
            # Read indent from the text token before the open tag
            _indent = ''
            if _open_idx > 0 and tokens[_open_idx - 1][0] == 'text':
                _t = tokens[_open_idx - 1][2]
                _nl = _t.rfind('\n')
                if _nl != -1:
                    _indent = _re.match(r'(\s*)', _t[_nl + 1:]).group(1)
            tokens.append(['close', _local, f'\n{_indent}</{_local}>', -1])
            insertions.append((-1, ''))  # sentinel so `if not insertions` stays False

        if not insertions:
            return None  # document was already balanced

        # ── Apply insertions (in reverse order so indices stay valid) ──────────
        # Each insertion goes BETWEEN the text-whitespace token and the first
        # child token.  To keep indentation clean we split the preceding text
        # token at its last newline: everything up to and including '\n' stays
        # before the new open tag; the trailing whitespace (the child's indent)
        # is prepended to the child token's existing leading whitespace.
        for ins_idx, ins_text in sorted(insertions, reverse=True):
            # Adjust: if the token just before ins_idx is pure-whitespace text,
            # split it so the open tag lands at the correct column.
            if ins_idx > 0 and tokens[ins_idx - 1][0] == 'text':
                prev_text = tokens[ins_idx - 1][2]
                nl = prev_text.rfind('\n')
                if nl != -1:
                    before_nl = prev_text[:nl + 1]   # keep up to \n
                    after_nl  = prev_text[nl + 1:]   # trailing whitespace (child indent)
                    tokens[ins_idx - 1][2] = before_nl
                    # Insert the open tag then re-prepend the child indent
                    tokens.insert(ins_idx, ['text', '', after_nl, -1])
                    tokens.insert(ins_idx, ['open', '', ins_text, -1])
                    continue
            tokens.insert(ins_idx, ['open', '', ins_text, -1])

        result = ''.join(t[2] for t in tokens)

        # ── Validate ───────────────────────────────────────────────────────────
        try:
            etree.fromstring(result.encode('utf-8'))
            return result
        except etree.XMLSyntaxError:
            pass

        # One more attempt: strip the XML declaration if present (lxml adds one
        # when serialising but the outer caller may not expect it)
        result2 = _re.sub(r'^\s*<\?xml[^?]*\?>\s*', '', result)
        try:
            etree.fromstring(result2.encode('utf-8'))
            return result2
        except etree.XMLSyntaxError:
            pass

        return None

    # ── Apply fixes to XML ────────────────────────────────────────────────────

    @staticmethod
    def _fix_xml_declaration(xml: str) -> Optional[str]:
        """
        Return a corrected copy of *xml* if its XML declaration is malformed,
        otherwise return None (nothing to fix).

        Handles cases like:
          <?xml version="1.0" encoding="UTF-8"!?>   ← stray '!' before '?>'
          <?xml version="1.0" encoding="UTF-8" !?>  ← stray '!' with space
          <?xml version='1.0' encoding='UTF-8'!?>   ← single-quoted variant
          <?xml version="1.0"!encoding="UTF-8"?>    ← '!' between attributes
          <?xml!version="1.0"?>                     ← '!' right after 'xml'

        Strategy: if the prolog line contains forbidden characters outside of
        the quoted attribute values, rebuild it from what we can parse out of it
        (version + encoding) and replace the broken declaration with a clean one.
        """
        import re as _re

        # Match the entire XML declaration — anything from '<?xml' up to the
        # first '?>' on the first non-whitespace content.
        # Use .*? (non-greedy) so we stop at the earliest '?>' regardless of
        # what garbage characters (like '!') appear before it.
        _decl_patt = _re.compile(r'^(\s*)<\?xml(.*?)(\?>)', _re.DOTALL)
        m = _decl_patt.match(xml)
        if m is None:
            return None  # No XML declaration — nothing to fix here

        leading  = m.group(1)
        inner    = m.group(2)   # everything between '<?xml' and '?>'
        close    = m.group(3)   # '?>'
        rest     = xml[m.end():]

        # If the declaration is already well-formed, nothing to do.
        # A well-formed inner section matches: optional space + known attributes
        # (version, encoding, standalone) with no other characters.
        _clean_patt = _re.compile(
            r'''^\s*
                (?:version\s*=\s*["'][^"']*["']\s*)?
                (?:encoding\s*=\s*["'][^"']*["']\s*)?
                (?:standalone\s*=\s*["'][^"']*["']\s*)?
            $''',
            _re.VERBOSE,
        )
        if _clean_patt.match(inner) and close == '?>':
            return None  # Already valid

        # Strip ALL characters that don't belong in the prolog (including '!').
        # Extract version and encoding values (ignore everything else).
        _ver_m = _re.search(r'version\s*=\s*["\']([^"\']*)["\']', inner)
        _enc_m = _re.search(r'encoding\s*=\s*["\']([^"\']*)["\']', inner)
        _std_m = _re.search(r'standalone\s*=\s*["\']([^"\']*)["\']', inner)

        version    = _ver_m.group(1) if _ver_m else "1.0"
        encoding   = _enc_m.group(1) if _enc_m else "UTF-8"
        standalone = _std_m.group(1) if _std_m else None

        clean_decl = f'<?xml version="{version}" encoding="{encoding}"'
        if standalone:
            clean_decl += f' standalone="{standalone}"'
        clean_decl += '?>'

        return leading + clean_decl + rest

    @staticmethod
    def _fix_stray_chars_in_tags(xml: str) -> Optional[str]:
        """Remove illegal characters that appear inside XML tags but outside quoted
        attribute values — e.g. '<BusMsgEnvlp xmlns="urn:swift:xsd:envelope"!>'.

        Strategy: walk the raw text character-by-character. When inside a tag
        (after '<' but before the matching '>'), track whether we are inside a
        quoted attribute value. Any character outside quotes that is not a legal
        XML tag character is stripped.

        Legal characters inside a tag (outside quotes): letters, digits,
        whitespace, =, /, -, _, ., :, "', and of course >.
        """
        if '<' not in xml:
            return None

        legal_in_tag = set('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
                           '0123456789 \t\r\n=/_-.:"\'>/?')
        result = []
        i = 0
        changed = False
        n = len(xml)
        while i < n:
            ch = xml[i]
            if ch != '<':
                result.append(ch)
                i += 1
                continue
            # Inside a tag — collect chars until the matching unquoted '>'
            tag_chars = ['<']
            i += 1
            in_quote = None
            while i < n:
                c = xml[i]
                if in_quote:
                    tag_chars.append(c)
                    if c == in_quote:
                        in_quote = None
                elif c in ('"', "'"):
                    in_quote = c
                    tag_chars.append(c)
                elif c == '>':
                    tag_chars.append('>')
                    i += 1
                    break
                elif c in legal_in_tag:
                    tag_chars.append(c)
                else:
                    # Stray illegal char — strip it
                    changed = True
                i += 1
            result.extend(tag_chars)
        return ''.join(result) if changed else None

    @staticmethod
    def _close_unclosed_text_elements(xml: str) -> Optional[str]:
        """
        Insert missing closing tags for text-only elements whose content runs
        into the next sibling's opening tag without a proper close.

        Handles patterns like:
          <CxlId>CXLIDGW2DI8MWLZ                      ← no </CxlId>
          <Case>...
          <BICFI>CREDDKKHXXX                          ← no </BICFI>
          </FinInstnId>
          <OrgnlIntrBkSttlmAmt Ccy="DKK">378369.51    ← no close
          <OrgnlIntrBkSttlmDt>2026-06-10              ← no close

        Also strips empty/anonymous close tags: </>

        Algorithm: scan line by line. For a line of the form
        ``<Tag [attrs]>TEXT`` where TEXT is non-empty, contains no '<', and
        the line itself has no matching ``</Tag>``, check whether the next
        non-blank line starts with '<' (i.e. a new tag begins there — the
        current element's content should have ended on this line). If so,
        append ``</Tag>`` to the end of the current line.

        Returns fixed XML if changes were made, else None.
        """
        import re as _re

        # Step 0: strip empty close tags </>  (invalid XML, means nothing)
        stripped = _re.sub(r'<\s*/\s*>', '', xml)
        changed = stripped != xml
        xml = stripped

        # Matches the LAST open-tag-then-text at the END of a line, allowing
        # other (complete) tags to precede it on the same line. Examples:
        #   "<MsgId>MSG001"           → tag=MsgId, text=MSG001
        #   "<Dbtr><Nm>John Doe"      → tag=Nm,    text=John Doe  (Dbtr precedes)
        # TEXT has no '<' or '>', is non-empty. Self-closing tags (attrs ending
        # in '/') are excluded via the attrs group.
        _TRAILING_OPEN_TEXT = _re.compile(
            r'<([A-Za-z][\w.\-]*(?::[A-Za-z][\w.\-]*)?)((?:\s[^<>]*)?)>'
            r'([^<>]+)$'
        )

        lines = xml.splitlines(keepends=True)
        result = list(lines)
        modified = False

        for i in range(len(result)):
            line = result[i]
            line_body = line.rstrip('\r\n')
            m = _TRAILING_OPEN_TEXT.search(line_body)
            if not m:
                continue
            tag_name = m.group(1)
            attrs    = m.group(2) or ""
            text_part = m.group(3)
            # Skip self-closing tags (attrs ending in '/')
            if attrs.rstrip().endswith('/'):
                continue
            if not text_part.strip():
                continue
            # Find next non-blank line
            next_line = None
            for k in range(i + 1, len(result)):
                if result[k].strip():
                    next_line = result[k].strip()
                    break
            if next_line is None or not next_line.startswith('<'):
                continue
            # Append the missing closing tag
            nl = line[len(line_body):]  # preserve original line ending
            result[i] = line_body + f'</{tag_name}>' + nl
            modified = True

        if not modified and not changed:
            return None
        fixed = ''.join(result)
        # Validate the result parses
        try:
            etree.fromstring(fixed.encode('utf-8'))
            return fixed
        except etree.XMLSyntaxError:
            # Even if not fully valid yet, return if it's different — subsequent
            # stages (balance engine, surgical fix) will handle remaining issues
            return fixed if fixed != xml else None

    # Header tags whose damage the generic balancer cannot reliably reconstruct,
    # because <Fr> and <To> share identical FIId/FinInstnId/BICFI nesting so an
    # orphaned close is ambiguous, and the AppHdr scalar fields often lose their
    # opening tags together (a cluster of orphaned closes). Used to gate the
    # canonical-rebuild stage so it never touches a body-only syntax error.
    _APPHDR_REBUILD_TAGS = (
        "AppHdr", "Fr", "To", "FIId", "FinInstnId", "BICFI",
        "BizMsgIdr", "MsgDefIdr", "BizSvc", "CreDt", "CreDtTm",
        "Document",
    )

    def _rebuild_canonical_apphdr(self, xml: str, msg: str) -> Optional[str]:
        """Reconstruct a structurally-broken CBPR+ AppHdr from surviving data.

        The CBPR+ Business Application Header is fixed boilerplate:

            <AppHdr xmlns="…head.001…">
              <Fr><FIId><FinInstnId><BICFI>{fr}</BICFI></FinInstnId></FIId></Fr>
              <To><FIId><FinInstnId><BICFI>{to}</BICFI></FinInstnId></FIId></To>
              <BizMsgIdr>…</BizMsgIdr><MsgDefIdr>…</MsgDefIdr>
              <BizSvc>…</BizSvc><CreDt>…</CreDt>
            </AppHdr>

        Only the two BICs and the scalar values carry data; every wrapper tag is
        boilerplate. So when heavy damage leaves the header unparseable (deleted
        Fr/To/FIId opens, or a cluster of orphaned BizMsgIdr/MsgDefIdr closes), we
        extract the surviving values and re-emit a clean canonical header.

        GATED: caller only invokes this for a parse-failure whose error is in the
        header region, and the method itself returns None unless it finds the
        AppHdr open tag, a region boundary, and at least one BIC — so a healthy or
        body-only document is never altered.
        """
        ah_open_m = re.search(r"<AppHdr\b[^>]*>", xml)
        if not ah_open_m:
            # AppHdr open tag was deleted; recover using </AppHdr> as boundary.
            ah_close_m = re.search(r"</AppHdr\s*>", xml)
            if not ah_close_m:
                return None
            envlp_m = re.search(r"<BusMsgEnvlp\b[^>]*>", xml)
            insert_at = envlp_m.end() if envlp_m else 0
            ah_open = '<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">'
            content_start = insert_at
            after = xml[content_start:]
            # Region is everything between BusMsgEnvlp end and </AppHdr>.
            pos_close = after.find("</AppHdr")
            if pos_close == -1:
                return None
            region = after[:pos_close]
            gt = after.find(">", pos_close)
            if gt == -1:
                return None
            splice_end = content_start + gt + 1
        else:
            ah_open = ah_open_m.group(0)
            insert_at = ah_open_m.start()
            content_start = ah_open_m.end()
            after = xml[content_start:]

            # Region boundary: earliest of </AppHdr> or the start of <Document>.
            pos_close = after.find("</AppHdr")
            doc_m = re.search(r"<(?:\w+:)?Document\b", after)
            pos_doc = doc_m.start() if doc_m else -1
            cands = [p for p in (pos_close, pos_doc) if p != -1]
            if not cands:
                # AppHdr open found but no </AppHdr> and no <Document> in what follows.
                # This happens when the entire AppHdr content was collapsed (e.g.
                # "<AppHdr xmlns='...'>  </Agt>\n</Assgne>..."). Use the whole
                # remaining text as the region for BIC/scalar extraction.
                region = after
                # Splice end: consume the stray orphaned close-tag cluster that
                # immediately follows the AppHdr opener (</Agt>\n</Assgne>\n...).
                # Keep consuming lines that are whitespace-only or orphaned closes
                # until we hit a proper open-tag line (real content).
                _lines = after.split("\n")
                _consumed_chars = 0
                for _ln in _lines:
                    _stripped = _ln.strip()
                    if not _stripped or re.match(r"^</[A-Za-z]", _stripped):
                        _consumed_chars += len(_ln) + 1  # +1 for \n
                    else:
                        break
                splice_end = content_start + _consumed_chars if _consumed_chars else content_start
            elif min(cands) == pos_close:
                b = pos_close
                region = after[:b]
                gt = after.find(">", pos_close)
                if gt == -1:
                    return None
                splice_end = content_start + gt + 1  # consume the </AppHdr>
            else:
                b = pos_doc
                region = after[:b]
                splice_end = content_start + b       # boundary is <Document>; we add close

        # ── Extract surviving data ────────────────────────────────────────────
        bics = re.findall(r"<BICFI>\s*([A-Z0-9]{8,11})\s*</BICFI>", region)
        if len(bics) < 2:
            # Tolerate damaged BICFI tags: take any BIC-format token in the region.
            loose = re.findall(r"\b([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", region)
            for tok in loose:
                if tok not in bics:
                    bics.append(tok)
        if not bics:
            return None  # nothing BIC-like survived — don't fabricate a header
        fr_bic = bics[0]
        to_bic = bics[1] if len(bics) > 1 else bics[0]

        def _scalar(tag: str) -> Optional[str]:
            for pat in (rf"<{tag}>\s*([^<>]+?)\s*</{tag}>",   # both tags intact
                        rf"<{tag}>\s*([^<>]+)",               # open only
                        rf"([^<>\n]+?)\s*</{tag}>"):          # close only
                m = re.search(pat, region)
                if m and m.group(1).strip():
                    return m.group(1).strip()
            return None

        biz = _scalar("BizMsgIdr")
        mdi = _scalar("MsgDefIdr") or (re.search(r"<MsgDefIdr>([^<]+)", xml) or [None, None])[1]
        svc = _scalar("BizSvc")
        date_tag = "CreDtTm" if re.search(r"</?CreDtTm\b", region) else "CreDt"
        cre = _scalar("CreDtTm") or _scalar("CreDt")

        # ── Emit canonical header (omit absent optional scalars) ──────────────
        nl = "\n        "
        lines = [
            f"{nl}<Fr><FIId><FinInstnId><BICFI>{fr_bic}</BICFI></FinInstnId></FIId></Fr>",
            f"{nl}<To><FIId><FinInstnId><BICFI>{to_bic}</BICFI></FinInstnId></FIId></To>",
        ]
        if biz:
            lines.append(f"{nl}<BizMsgIdr>{biz}</BizMsgIdr>")
        if mdi:
            lines.append(f"{nl}<MsgDefIdr>{mdi}</MsgDefIdr>")
        if svc:
            lines.append(f"{nl}<BizSvc>{svc}</BizSvc>")
        if cre:
            lines.append(f"{nl}<{date_tag}>{cre}</{date_tag}>")
        new_apphdr = ah_open + "".join(lines) + "\n    </AppHdr>"

        fixed = xml[:insert_at] + new_apphdr + xml[splice_end:]
        return fixed if fixed != xml else None

    def _insert_missing_apphdr(
        self, root, xml: str, code: str, msg: str, msg_type: str
    ) -> Optional[FixSuggestion]:
        """Insert a complete <AppHdr> when it is entirely absent from the XML.

        Placement rules (matching standard CBPR+ structure):
        - BusMsgEnvlp envelope: insert after </Document>, before </BusMsgEnvlp>
        - Bare Document root  : insert immediately before <Document>

        Values are harvested from existing XML elements:
        - BizMsgIdr ← GrpHdr/MsgId
        - MsgDefIdr ← Document namespace (e.g. pain.008.001.08)
        - BizSvc    ← infer from msg_type or default swift.cbprplus.02
        - CreDt     ← GrpHdr/CreDtTm
        - Fr BICFI  ← FwdgAgt/FinInstnId/BICFI (or first BICFI found)
        - To BICFI  ← CdtrAgt or InstgAgt/FinInstnId/BICFI (or second BICFI found)
        """
        AH_NS = 'urn:iso:std:iso:20022:tech:xsd:head.001.001.02'

        # ── Harvest values from the parsed tree ──────────────────────────────────
        def _first_text(tags):
            for tag in tags:
                els = root.findall(f".//*{{*}}{tag}")
                for el in els:
                    if el.text and el.text.strip():
                        return el.text.strip()
            return None

        def _collect_bicfis():
            return [
                el.text.strip()
                for el in root.findall(".//{*}BICFI")
                if el.text and el.text.strip()
            ]

        msg_id = _first_text(["MsgId"])
        cre_dtm = _first_text(["CreDtTm", "CreDt"])

        # Determine MsgDefIdr from Document namespace
        doc_node = next(
            (el for el in root.iter()
             if isinstance(el.tag, str) and etree.QName(el.tag).localname == "Document"),
            None,
        )
        mdi = None
        if doc_node is not None:
            _doc_ns = (etree.QName(doc_node).namespace or "") if isinstance(doc_node.tag, str) else ""
            _ns_m = re.match(
                r'^urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$',
                _doc_ns,
            )
            if _ns_m:
                mdi = _ns_m.group(1)
        if not mdi and msg_type:
            mdi = msg_type

        # BizSvc: use known service codes per message family
        _svc_map = {
            "pain.001": "swift.cbprplus.02",
            "pain.002": "swift.cbprplus.02",
            "pain.008": "swift.cbprplus.02",
            "pacs.008": "swift.cbprplus.02",
            "pacs.002": "swift.cbprplus.02",
            "pacs.004": "swift.cbprplus.02",
            "pacs.009": "swift.cbprplus.adv.02",
            "pacs.010": "swift.cbprplus.02",
            "camt.056": "swift.cbprplus.02",
        }
        _family_key = ".".join((mdi or "").split(".")[:2])
        biz_svc = _svc_map.get(_family_key, "swift.cbprplus.02")

        # Fr BICFI: prefer FwdgAgt, fall back to first BICFI in document
        all_bics = _collect_bicfis()
        fwdg_el = root.find(".//{*}FwdgAgt")
        fr_bic = None
        if fwdg_el is not None:
            _b = fwdg_el.find(".//{*}BICFI")
            if _b is not None and _b.text and _b.text.strip():
                fr_bic = _b.text.strip()
        if not fr_bic:
            fr_bic = all_bics[0] if all_bics else "AAAABBCCXXX"

        # To BICFI: prefer CdtrAgt (pain.008), InstgAgt, or second BICFI in document
        to_bic = None
        for _agent_tag in ("CdtrAgt", "InstgAgt", "DbtrAgt"):
            _agt_el = root.find(f".//*{{*}}{_agent_tag}")
            if _agt_el is not None:
                _b = _agt_el.find(".//{*}BICFI")
                if _b is not None and _b.text and _b.text.strip():
                    to_bic = _b.text.strip()
                    break
        if not to_bic:
            to_bic = next((b for b in all_bics if b != fr_bic), all_bics[0] if all_bics else "AAAABBCCXXX")

        # ── Build the AppHdr XML string ───────────────────────────────────────────
        nl = "\n    "
        ind = "\n        "
        lines = [
            f'{nl}<AppHdr xmlns="{AH_NS}">',
            f'{ind}<Fr><FIId><FinInstnId><BICFI>{fr_bic}</BICFI></FinInstnId></FIId></Fr>',
            f'{ind}<To><FIId><FinInstnId><BICFI>{to_bic}</BICFI></FinInstnId></FIId></To>',
        ]
        if msg_id:
            lines.append(f"{ind}<BizMsgIdr>{msg_id}</BizMsgIdr>")
        if mdi:
            lines.append(f"{ind}<MsgDefIdr>{mdi}</MsgDefIdr>")
        lines.append(f"{ind}<BizSvc>{biz_svc}</BizSvc>")
        if cre_dtm:
            lines.append(f"{ind}<CreDt>{cre_dtm}</CreDt>")
        lines.append(f"{nl}</AppHdr>")
        new_apphdr_str = "".join(lines)

        # ── Determine insertion point ─────────────────────────────────────────────
        root_local = etree.QName(root.tag).localname if isinstance(root.tag, str) else ""
        if root_local == "BusMsgEnvlp":
            # Insert after </Document> and before </BusMsgEnvlp>
            envlp_close = xml.rfind("</BusMsgEnvlp")
            if envlp_close == -1:
                envlp_close = len(xml)
            # Find the last </Document> before the envelope close
            doc_close_m = None
            for _m in re.finditer(r"</(?:\w+:)?Document\s*>", xml[:envlp_close]):
                doc_close_m = _m
            if doc_close_m:
                insert_at = doc_close_m.end()
            else:
                insert_at = envlp_close
            fixed = xml[:insert_at] + "\n" + new_apphdr_str + xml[insert_at:]
        else:
            # Bare Document (or unknown root): insert before the <Document> open tag
            doc_open_m = re.search(r"<(?:\w+:)?Document\b", xml)
            if doc_open_m:
                insert_at = doc_open_m.start()
            else:
                return None
            fixed = xml[:insert_at] + new_apphdr_str + "\n" + xml[insert_at:]

        if fixed == xml:
            return None

        _decl = '<?xml version="1.0" encoding="UTF-8"?>\n'
        if not fixed.lstrip().startswith("<?"):
            fixed = _decl + fixed
        return FixSuggestion("/", xml, fixed, code, msg, "high")

    def _try_xml_recovery(self, xml: str, code: str, msg: str) -> Optional[FixSuggestion]:
        """Attempt document-level XML syntax repairs. Returns FixSuggestion("/", ...) or None."""
        # Stage -2: strip illegal XML control characters (SR2026 ILLEGAL_CONTROL_CHARACTERS).
        # C0 controls except TAB (\x09), LF (\x0a), CR (\x0d) are forbidden in XML 1.0.
        # Must run first — they prevent parsing and can also corrupt the declaration.
        if code == "ILLEGAL_CONTROL_CHARACTERS" or re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', xml):
            fixed = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', xml)
            if fixed != xml:
                return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage -1.5: wrong encoding declaration (SR2026 INVALID_ENCODING) — rewrite to UTF-8.
        if code == "INVALID_ENCODING":
            fixed = re.sub(
                r'(<\?xml[^?]*\bencoding=["\'])[^"\']+(["\'])',
                r'\1UTF-8\2', xml, count=1
            )
            if fixed != xml:
                return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage -1: entirely missing XML declaration — just prepend it.
        # Must run before Stage 0 so that a completely absent prolog is handled
        # without falling through to the LLM path.
        if not re.match(r'^\s*<\?xml', xml):
            fixed = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml.lstrip()
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage -0.5: duplicate XML declaration — keep only the first, drop extras.
        # A second <?xml ...?> anywhere after the first is illegal and causes
        # "XML syntax error at line N" where N > 1. Remove all but the first.
        _decl_all = list(re.finditer(r'<\?xml[^?]*\?>', xml))
        if len(_decl_all) > 1:
            # Build fixed: keep text up through first decl, then skip all subsequent
            # <?xml ...?> occurrences, keeping everything else.
            fixed = xml[:_decl_all[0].end()]
            rest_start = _decl_all[0].end()
            for _dm in _decl_all[1:]:
                fixed += xml[rest_start:_dm.start()]
                rest_start = _dm.end()
            fixed += xml[rest_start:]
            if fixed != xml:
                return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 0: malformed XML declaration (e.g. '<?xml version="1.0" encoding="UTF-8"!?>')
        # Must run FIRST — a broken prolog prevents lxml from parsing at all, so
        # every subsequent stage would silently fail.
        fixed = self._fix_xml_declaration(xml)
        if fixed is not None and fixed != xml:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 0.5: stray illegal chars inside XML tags
        # e.g. '<BusMsgEnvlp xmlns="urn:swift:xsd:envelope"!>' — the '!' before '>'
        # prevents lxml from parsing. Must run before any stage that tries to parse.
        fixed = self._fix_stray_chars_in_tags(xml)
        if fixed is not None and fixed != xml:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 1: escape unescaped & characters
        fixed = self._escape_reserved_xml_chars(xml)
        if fixed is not None:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 1.5: close unclosed text elements + strip empty </> close tags.
        # Handles patterns like <CxlId>TEXT\n<NextSibling> where </CxlId> was
        # omitted, and </>  (anonymous close tags with no tag name).
        _close_fixed = self._close_unclosed_text_elements(xml)
        if _close_fixed is not None and _close_fixed != xml:
            # Run balance engine on result to catch any remaining orphaned tags
            _bal = self._balance_xml_tags(_close_fixed)
            _final_close = _bal if _bal is not None else _close_fixed
            if _final_close != xml:
                _final_close = self._normalize_busmsgenvlp(_final_close)
                return FixSuggestion("/", xml, _final_close, code, msg, "high")

        # Stage 1.7: surgical missing-OPENING-tag repair. Must run before
        # Stage 2, which assumes the "Unclosed tag <X> ... before </Y>"
        # message means X is missing its close — here Y is missing its open.
        fixed = self._try_surgical_missing_opening_tag_fix(xml, msg)
        if fixed is not None:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 2: surgical unclosed-tag repair
        fixed = self._try_surgical_unclosed_tag_fix(xml, msg)
        if fixed is not None:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 2.1: canonical AppHdr rebuild — runs BEFORE Stage 2.2 so that
        # AppHdr-mismatch errors ("Opening and ending tag mismatch: AppHdr")
        # get a proper header reconstruction rather than the Stage 2.2 stripper
        # which may delete real body content along with the orphaned closes.
        _apphdr_tag_re_early = r"(" + "|".join(self._APPHDR_REBUILD_TAGS) + r")"
        if (re.search(r"Unclosed tag <" + _apphdr_tag_re_early + r"\b", msg or "")
                or re.search(r"Opening and ending tag mismatch:\s*" + _apphdr_tag_re_early + r"\b", msg or "")
                or re.search(r"mismatch.*AppHdr", msg or "", re.I)):
            _ah_early = self._rebuild_canonical_apphdr(xml, msg)
            if _ah_early is not None and _ah_early != xml:
                _ah_bal_e = self._balance_xml_tags(_ah_early)
                _ah_fin_e = _ah_bal_e if _ah_bal_e is not None else _ah_early
                _ah_fin_e = self._normalize_busmsgenvlp(_ah_fin_e)
                if _ah_fin_e != xml:
                    try:
                        etree.fromstring(_ah_fin_e.encode("utf-8"))
                        return FixSuggestion("/", xml, _ah_fin_e, code, msg, "high")
                    except etree.XMLSyntaxError:
                        pass  # fall through to other stages

        # Stage 2.2: strip empty AppHdr Fr/To orphaned-close clusters.
        # When the entire content of a <Fr> or <To> block is deleted (both
        # opening tags AND all inner content), the raw XML contains only
        # whitespace-only orphaned closes like:
        #     \n    </FIId>\n</Fr>   or   \n    </FinInstnId>\n</FIId>\n</To>
        # The balance engine cannot reconstruct the correct nesting from closes
        # alone (it sees siblings where it should see nesting).  The safe fix:
        # strip these empty close clusters entirely; _normalize_busmsgenvlp
        # Problem 4 will then rebuild a complete Fr/To/FIId/FinInstnId/BICFI
        # block from scratch on the next round.
        fixed = self._strip_empty_apphdr_frto_closes(xml)
        if fixed is not None and fixed != xml:
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 2.25: bare '<' followed by whitespace/newline then '/TagName>'
        # e.g. '<BICFI>\n    CACRNLAAXXX\n    <\n/Fr>' — the '<' is a stray
        # character; the '/Fr>' on the next line is a valid closing tag fragment
        # separated from '<' by whitespace. Normalize to '</' so the split-tag
        # stripper and balance engine can process it normally.
        _bare_lt_fixed = re.sub(r'<([ \t]*\n[ \t]*/)', r'</', xml)
        if _bare_lt_fixed != xml:
            xml = _bare_lt_fixed

        # Stage 2.26: open tag whose '>' was deleted along with its content and
        # own closing tag, immediately followed by whitespace/newline then
        # '/OtherTag>' (a closing tag missing its '<'). e.g.
        #   <RmtInf>
        #       <Ustrd
        #   /RmtInf></DrctDbtTxInf>
        # lxml fails with "error parsing attribute name" because '<Ustrd' has no
        # '>' and 'RmtInf' (after the stray '/') looks like a bogus attribute.
        # Nothing recoverable remains of <Ustrd>'s content, so collapse it to an
        # empty self-closing element and restore the '<' on the closing tag that
        # follows: '<Ustrd/></RmtInf>'. A later round may flag the now-empty
        # element on schema grounds (e.g. Max140Text minLength) — that is a
        # separate, normal fixable issue, not a well-formedness one.
        _open_tag_eaten_fixed = re.sub(
            r'<([A-Za-z][\w]*)((?:[ \t]*\r?\n[ \t]*)+)/([A-Za-z][\w]*)>',
            r'<\1/></\3>',
            xml,
        )
        if _open_tag_eaten_fixed != xml:
            try:
                etree.fromstring(_open_tag_eaten_fixed.encode("utf-8"))
                return FixSuggestion("/", xml, _open_tag_eaten_fixed, code, msg, "high")
            except etree.XMLSyntaxError:
                xml = _open_tag_eaten_fixed

        # Stage 2.3: strip split/truncated tags whose name spans a newline.
        # e.g. ``</CdtrAg\n\t\t\t\t\tNm>`` — the partial closing tag is not a
        # valid XML token so neither lxml nor _balance_xml_tags can tokenize it.
        # Removing the broken fragment exposes the surrounding orphaned closes
        # (</Id>, </Cdtr>) so Stage 2.5 can reconstruct the correct nesting.
        # After stripping, re-run Stage 2 surgical fix (the unclosed-tag error
        # may still be present) then pass into the balance engine.
        _split_stripped = self._strip_split_tags(xml)
        if _split_stripped is not None and _split_stripped != xml:
            # Try surgical fix on the cleaned XML first
            _surgical_on_stripped = self._try_surgical_unclosed_tag_fix(_split_stripped, msg)
            _base_for_balance = _surgical_on_stripped or _split_stripped
            fixed = self._balance_xml_tags(_base_for_balance)
            if fixed is not None and fixed != xml:
                fixed = self._normalize_busmsgenvlp(fixed)
                return FixSuggestion("/", xml, fixed, code, msg, "high")
            # Even if balance engine couldn't fully fix it, return the stripped
            # version so at least the untokenizable fragment is removed.
            if _split_stripped != xml:
                return FixSuggestion("/", xml, _split_stripped, code, msg, "high")

        # Stage 2.4: canonical AppHdr rebuild.
        # When the unclosed-tag error names a header element (AppHdr/Fr/To/FIId/
        # FinInstnId/BICFI or an AppHdr scalar), the damage is in the CBPR+
        # Business Application Header — fixed boilerplate the generic balancer
        # can't reconstruct (Fr/To share identical nesting; scalar opens are
        # deleted in clusters). Rebuild it from the surviving BICs + scalar values,
        # then hand the result to the balance engine to clean up any body damage.
        # Also triggers on "Opening and ending tag mismatch: AppHdr" (same damage,
        # different lxml error message format when the AppHdr close is swapped with
        # an inner tag like Agt).
        _apphdr_tag_re = r"(" + "|".join(self._APPHDR_REBUILD_TAGS) + r")"
        if (re.search(r"Unclosed tag <" + _apphdr_tag_re + r"\b", msg or "")
                or re.search(r"Opening and ending tag mismatch:\s*" + _apphdr_tag_re + r"\b", msg or "")
                or re.search(r"mismatch.*AppHdr", msg or "", re.I)):
            _ah = self._rebuild_canonical_apphdr(xml, msg)
            if _ah is not None and _ah != xml:
                _ah_bal = self._balance_xml_tags(_ah)
                _ah_final = _ah_bal if _ah_bal is not None else _ah
                _ah_final = self._normalize_busmsgenvlp(_ah_final)
                if _ah_final != xml:
                    return FixSuggestion("/", xml, _ah_final, code, msg, "high")

        # Stage 2.5: full tag-balance engine
        # Scans every tag token across the entire document, detects any orphaned
        # closing tags (whose opening tag was deleted) and re-inserts the missing
        # opening tag at the correct position. Handles all "Opening and ending tag
        # mismatch" errors, missing FIId wrappers, and any other case where an
        # opening tag was manually removed.
        # After balancing, always run _normalize_busmsgenvlp so that Fr/To content
        # that ended up in the wrong place (FIId floating outside shell, BizMsgIdr
        # swallowed inside To, etc.) gets corrected before returning the result.
        fixed = self._balance_xml_tags(xml)
        if fixed is not None and fixed != xml:
            fixed = self._normalize_busmsgenvlp(fixed)
            return FixSuggestion("/", xml, fixed, code, msg, "high")

        # Stage 3: lxml recovery-mode parse.
        # Apply declaration fix before handing to lxml so the parser has a
        # valid prolog to work with even when the body is the real problem.
        # SAFETY: lxml recover=True silently drops unparseable content, which
        # can produce a SHORTER document than the original (truncated output).
        # Only accept the recovered XML when it has at least as many elements
        # as the original — if it's shorter, recovery just discarded content
        # rather than fixing it, so we must decline.
        _xml_for_lxml = self._fix_xml_declaration(xml) or xml
        try:
            _orig_count = xml.count("<") - xml.count("</") - xml.count("/>")
            parser = etree.XMLParser(recover=True)
            recovered_root = etree.fromstring(_xml_for_lxml.encode("utf-8"), parser=parser)
            if recovered_root is not None:
                decl = ""
                import re as _re
                m = _re.match(r"(<\?xml[^?]*\?>)", _xml_for_lxml.strip())
                if m:
                    decl = m.group(1) + "\n"
                recovered_xml = decl + etree.tostring(recovered_root, encoding="unicode", pretty_print=True)
                _recv_count = recovered_xml.count("<") - recovered_xml.count("</") - recovered_xml.count("/>")
                # Decline if recovery lost elements (content was dropped, not fixed)
                if _recv_count < _orig_count:
                    return None
                if recovered_xml.strip() != xml.strip():
                    return FixSuggestion("/", xml, recovered_xml, code, msg, "low")
        except Exception:
            pass

        return None

    def _unavail(self, path: str, code: str, message: str) -> FixSuggestion:
        """Return an 'unavailable' suggestion placeholder."""
        return FixSuggestion(
            xpath=path,
            original_fragment="",
            fragment_xml="",
            issue_code=code,
            issue_message=message,
            confidence="unavailable",
        )

    # ── Closed-loop verification ──────────────────────────────────────────────
    def suggest_verified(self, xml: str, issue: dict, version: str = None) -> FixSuggestion:
        """suggest() plus a synchronous closed-loop self-check.

        Used by the interactive single-issue endpoint. The verification result
        is attached as `.verified`; `.confidence` is deliberately LEFT UNCHANGED
        so every existing consumer (the auto-fix loop, the golden corpus, the
        batch path) behaves exactly as before. Verification is best-effort and
        can never raise out of here.
        """
        sug = self.suggest(xml, issue, version=version)
        try:
            sug.verified = self._self_verify(xml, sug)
        except Exception as e:
            logger.debug(f"[FixSuggester] self-verify failed (ignored): {e}")
            sug.verified = None
        try:
            fix_metrics.record_suggestion(sug.confidence, sug.verified)
        except Exception:
            pass
        return sug

    def _compiled_schema(self, xsd_path: str):
        sch = _XSD_SCHEMA_CACHE.get(xsd_path)
        if sch is None:
            sch = etree.XMLSchema(etree.parse(xsd_path))
            _XSD_SCHEMA_CACHE[xsd_path] = sch
        return sch

    def _xsd_error_count(self, xml_str: str, schema) -> Optional[int]:
        """Validate the Document body and return its XSD error count, or None
        when the doc can't be parsed/located (so the caller can abstain)."""
        try:
            doc = etree.fromstring(xml_str.encode("utf-8"),
                                   parser=etree.XMLParser(recover=True))
        except Exception:
            return None
        body = doc if etree.QName(doc.tag).localname == "Document" \
            else doc.find(".//{*}Document")
        if body is None:
            return None
        schema.validate(body)
        return len(schema.error_log)

    def _self_verify(self, original_xml: str, sug: FixSuggestion) -> Optional[bool]:
        """Apply the suggestion to a throwaway copy and confirm it is safe.

        True  → applies cleanly, stays well-formed, and does NOT increase the
                XSD error count (a duplicate that breaches maxOccurs, or any new
                schema error, shows up here as a higher count → not verified).
        False → fails to apply, breaks well-formedness, or worsens XSD validity.
        None  → not judgeable (no actionable fragment, or no XSD on disk).
        Side-effect-free: operates on returned strings only.
        """
        if (sug.confidence not in ("high", "low")
                or not sug.xpath or not sug.fragment_xml
                or sug.fragment_xml == sug.original_fragment):
            return None
        try:
            patched = self.apply(original_xml, sug.xpath, sug.fragment_xml)
        except Exception:
            return False                      # a fix that won't apply isn't verified
        try:
            etree.fromstring(patched.encode("utf-8"))
        except Exception:
            return False                      # broke well-formedness
        xsd_path = self._get_xsd_path(patched)
        if not xsd_path:
            return True                       # structurally sound; no schema to judge deeper
        try:
            schema = self._compiled_schema(xsd_path)
            before = self._xsd_error_count(original_xml, schema)
            after = self._xsd_error_count(patched, schema)
            if before is None or after is None:
                return True
            return after <= before
        except Exception:
            return None

    def apply(self, xml: str, xpath: str, fragment_xml: str) -> str:
        """
        Apply a single fix: replace the element identified by *xpath* with the
        well-formed *fragment_xml* string.  Returns the updated full XML document.

        Raises FixApplyError when the target element cannot be located or the
        fragment is not well-formed XML.
        """
        _xml_parseable = True
        try:
            root = etree.fromstring(xml.encode("utf-8"))
        except etree.XMLSyntaxError:
            _xml_parseable = False
            try:
                root = etree.fromstring(
                    xml.encode("utf-8"),
                    parser=etree.XMLParser(recover=True),
                )
            except Exception as e:
                raise FixApplyError(f"Cannot parse source XML: {e}") from e

        # xpath="/" means whole-document replacement (used by _try_xml_recovery)
        if xpath in ("/", ""):
            # Safety: never shrink the document significantly. lxml recover=True
            # silently drops content, so a whole-doc replacement that is much
            # shorter than the original means content was lost, not fixed.
            # Allow up to 20% size reduction for valid XML (normalisation / whitespace).
            # When the original was already unparseable, most chars are garbage
            # (orphaned closes, mismatched tags) — not real content. Apply a looser
            # 25% floor so a correct repair that discards the garbage is accepted.
            _orig_len = len(xml.strip())
            _frag_len = len(fragment_xml.strip())
            _floor = 0.8 if _xml_parseable else 0.25
            if _orig_len > 0 and _frag_len < _orig_len * _floor:
                raise FixApplyError(
                    f"Whole-document replacement rejected: "
                    f"fragment ({_frag_len} chars) is more than "
                    f"{int((1 - _floor) * 100)}% shorter than "
                    f"the original ({_orig_len} chars). Content would be lost."
                )
            return fragment_xml

        # Locate target element via the indexed xpath produced by _xpath_of().
        # We strip namespace predicates for matching — lxml namespace handling
        # uses Clark notation ({ns}local) while our xpaths use bare local names.
        target = self._find_by_xpath(root, xpath)
        if target is None:
            raise FixApplyError(f"XPath not found in document: {xpath!r}")

        try:
            new_el = etree.fromstring(fragment_xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            raise FixApplyError(f"Fragment is not valid XML: {e}") from e

        parent = target.getparent()
        if parent is None:
            # Root element replacement — only allow when the new element's local
            # name matches the original root's local name.  An LLM that hallucinated
            # a tiny leaf (<FinInstnId>) as a replacement for the root (<BusMsgEnvlp>)
            # must not be applied — it would wipe the entire document.
            orig_local = etree.QName(target.tag).localname if isinstance(target.tag, str) else ""
            new_local  = etree.QName(new_el.tag).localname  if isinstance(new_el.tag, str)  else ""
            if orig_local and new_local and orig_local != new_local:
                raise FixApplyError(
                    f"Root element tag mismatch: cannot replace <{orig_local}> with <{new_local}>. "
                    f"Fragment rejected to prevent document loss."
                )
            decl = ""
            m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
            if m:
                decl = m.group(1) + "\n"
            return decl + etree.tostring(new_el, encoding="unicode", pretty_print=True)

        idx = list(parent).index(target)
        # Preserve tail whitespace from original element
        new_el.tail = target.tail
        parent.remove(target)
        parent.insert(idx, new_el)

        decl = ""
        m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
        if m:
            decl = m.group(1) + "\n"
        return decl + etree.tostring(root, encoding="unicode", pretty_print=True)

    def _find_by_xpath(self, root: etree._Element, xpath: str) -> Optional[etree._Element]:
        """
        Locate an element in *root* using the indexed local-name xpath produced
        by `_xpath_of()`.  The xpath uses bare local names (no namespace prefix)
        with optional [N] positional predicates, e.g.
          /Document/FIToFICstmrCdtTrf/CdtTrfTxInf[2]/PmtId/EndToEndId

        Traverses the tree step by step matching on local names and indices.
        Falls back to lxml's XPath engine with a wildcard namespace expression
        if the step-by-step walk fails.
        """
        if not xpath:
            return None

        # Remove leading slash(es)
        parts = [p for p in xpath.lstrip("/").split("/") if p]

        # Step-by-step local-name walk
        parts_list = list(parts)
        current: etree._Element = root

        # Handle case where first part matches the root element itself
        if parts_list:
            first_m = re.match(r'^([\w]+)(?:\[(\d+)\])?$', parts_list[0])
            if first_m and isinstance(root.tag, str) and etree.QName(root.tag).localname == first_m.group(1):
                parts_list = parts_list[1:]

        for part in parts_list:
            m = re.match(r'^([\w]+)(?:\[(\d+)\])?$', part)
            if not m:
                return None
            local, idx_str = m.group(1), m.group(2)
            idx = int(idx_str) - 1 if idx_str else 0  # xpath is 1-based

            children = [
                c for c in current
                if isinstance(c.tag, str) and etree.QName(c.tag).localname == local
            ]
            if not children or idx >= len(children):
                return None
            current = children[idx]

        return current

    @staticmethod
    def _xpath_overlaps(a: str, b: str) -> bool:
        """True when xpaths *a* and *b* address the same element or one is an
        ancestor of the other (so replacing one element's subtree would clobber
        or duplicate the other's change).

        Compares on normalised step lists (leading slash stripped) using prefix
        containment: '/A/B' overlaps '/A/B/C' (ancestor) and '/A/B' (equal), but
        not '/A/D'. Positional predicates ([N]) are kept so CdtTrfTxInf[1] and
        CdtTrfTxInf[2] are correctly treated as DISTINCT, non-overlapping.
        """
        if not a or not b:
            return False
        pa = [p for p in a.lstrip("/").split("/") if p]
        pb = [p for p in b.lstrip("/").split("/") if p]
        n = min(len(pa), len(pb))
        return pa[:n] == pb[:n]

    def _collapse_overlapping_fixes(self, fixes: list[dict]) -> list[dict]:
        """Within each group of overlapping xpaths (equal / ancestor / descendant),
        keep ONLY the outermost (ancestor / shortest-path) fix and drop the rest.

        suggest_batch re-serialises every fragment from the FINAL rolled-forward
        tree, so an ancestor's fragment already contains all of its descendants'
        fixes. Applying the ancestor fragment alone therefore reproduces every
        nested change in one replace. Also applying the descendant fragments
        would replace subtrees the ancestor fragment already wrote — at best a
        redundant no-op, at worst (if line-shift moved the target) a DUPLICATE.
        So we keep the ancestor and discard descendants/equals in its lineage.

        A fix is dropped when ANY OTHER kept fix's xpath is a strict ancestor of
        it (or equal, keeping the first seen). Order-independent on overlap.
        """
        def _depth(xp: str) -> int:
            return len([p for p in xp.lstrip("/").split("/") if p])

        # Sort outermost-first so ancestors are decided before their descendants.
        indexed = list(enumerate(fixes))
        indexed.sort(key=lambda iv: _depth(iv[1].get("xpath", "")))

        kept: list[dict] = []
        kept_xpaths: list[str] = []
        for _orig_i, fix in indexed:
            xp = fix.get("xpath", "")
            if not xp or xp in ("/", ""):
                kept.append(fix)
                continue
            # Drop if an already-kept fix overlaps (is ancestor-or-equal of) this one.
            if any(self._xpath_overlaps(xp, k) for k in kept_xpaths):
                continue
            kept.append(fix)
            kept_xpaths.append(xp)
        return kept

    def apply_batch(self, xml: str, fixes: list[dict]) -> str:
        """
        Apply a list of fixes in reverse document order so that earlier
        XPaths are not invalidated by later insertions or deletions.

        Each fix dict must contain 'xpath' and 'fragment_xml'.
        """
        # Collapse overlapping (equal / ancestor / descendant) xpaths to the
        # last roll-forward fragment per lineage BEFORE re-sorting. This must run
        # on the ORIGINAL (suggest_batch) order, where "later == more complete".
        fixes = self._collapse_overlapping_fixes(fixes)

        # Parse once to determine document order for all fixes
        try:
            _order_root = etree.fromstring(xml.encode("utf-8"),
                                           parser=etree.XMLParser(recover=True))
            def _doc_order(fix: dict) -> int:
                xp = fix.get("xpath", "")
                if not xp:
                    return 0
                el = self._find_by_xpath(_order_root, xp)
                return (el.sourceline or 0) if el is not None else 0
        except Exception:
            def _doc_order(fix: dict) -> int:
                return 0

        # Sort in reverse document order: process last elements first
        sorted_fixes = sorted(fixes, key=_doc_order, reverse=True)

        current_xml = xml
        for fix in sorted_fixes:
            xpath        = fix.get("xpath", "")
            fragment_xml = fix.get("fragment_xml", "")
            if not xpath or not fragment_xml:
                continue
            try:
                current_xml = self.apply(current_xml, xpath, fragment_xml)
            except FixApplyError as e:
                logger.warning(f"[FixSuggester] apply_batch: skipping {xpath!r}: {e}")
        return current_xml


# ── Module-level singleton ────────────────────────────────────────────────────

fix_suggester = FixSuggester()                       # default (resolves version per call)
fix_suggester_sr2025 = FixSuggester(version="SR2025")  # SR2025-bound
fix_suggester_sr2026 = FixSuggester(version="SR2026")  # SR2026-bound
