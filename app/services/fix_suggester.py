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
from dataclasses import dataclass
from typing import Optional, Dict, Any
from lxml import etree

from app.services.openai_client import complete

logger = logging.getLogger(__name__)

XS = "http://www.w3.org/2001/XMLSchema"

# ── Codelists loader (cached) ─────────────────────────────────────────────────

_CODELISTS_CACHE: Optional[Dict[str, Any]] = None

def _load_codelists() -> Dict[str, Any]:
    global _CODELISTS_CACHE
    if _CODELISTS_CACHE is not None:
        return _CODELISTS_CACHE
    base = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "resources", "codelists")
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

_KB_CACHE: Optional[Dict[str, Any]] = None


def _load_knowledge_base() -> Dict[str, Any]:
    global _KB_CACHE
    if _KB_CACHE is not None:
        return _KB_CACHE
    base = os.path.join(os.path.dirname(__file__), "..", "resources")
    # Resources were reorganised into a `KB/` subfolder; keep both paths so
    # the loader works regardless of layout.
    candidates = [
        os.path.normpath(os.path.join(base, "KB", "ai_knowledge_base.json")),
        os.path.normpath(os.path.join(base, "ai_knowledge_base.json")),
    ]
    last_err: Optional[Exception] = None
    for kb_path in candidates:
        try:
            with open(kb_path, "r", encoding="utf-8-sig") as f:
                _KB_CACHE = json.load(f)
            break
        except FileNotFoundError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            break
    if _KB_CACHE is None:
        logger.warning(f"[FixSuggester] ai_knowledge_base.json load failed: {last_err}")
        _KB_CACHE = {}
    return _KB_CACHE


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
        return ("pacs009_cov_cbprplus_sr2025_validation_kb.json" if is_cov
                else "pacs009_cbprplus_sr2025_validation_kb.json")
    if msg_type.startswith("pacs.003"):
        return "pacs003_cbprplus_sr2025_validation_kb.json"
    # pacs.008 ships its KB under the plain "pacs.008.json" name (same schema:
    # a top-level "tags" array). It was previously unmapped, so the fixer never
    # consulted it for the most common message type — add it explicitly.
    if msg_type.startswith("pacs.008"):
        return "pacs.008.json"
    # Per-message auto-fix KBs (one file per message family). The family prefix
    # is enough — a single file covers all variants of that message.
    if msg_type.startswith("pacs.010"):
        return "pacs010_cbprplus_sr2025_validation_kb.json"
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
    if msg_type.startswith("pacs.004"):
        return "pacs004_cbprplus_sr2025_validation_kb.json"
    return None


def _load_kb_folder(msg_type: str, xml: str = "") -> Dict[str, Any]:
    name = _kb_folder_name(msg_type, xml)
    if not name:
        return {}
    if name in _KB_FOLDER_CACHE:
        return _KB_FOLDER_CACHE[name]
    path = os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "resources", "KB", name)
    )
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            _KB_FOLDER_CACHE[name] = json.load(f)
    except Exception as e:
        logger.warning(f"[FixSuggester] KB folder load failed for {name}: {e}")
        _KB_FOLDER_CACHE[name] = {}
    return _KB_FOLDER_CACHE[name]


def _value_for_datatype(dt: str) -> Optional[str]:
    """Generate a schema-valid leaf value for a KB-declared datatype."""
    from datetime import date, datetime, timezone
    dt = (dt or "").lower()
    if "datetime" in dt:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    if "date" in dt:
        return date.today().isoformat()
    if "decimal" in dt or "amount" in dt:
        return "1000.00"
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
    """The correct CBPR+ SR2025 AppHdr/BizSvc value for this message + variant.

    pacs.009 splits into three business services that the generic
    ai_knowledge_base template ('swift.cbprplus.02') does NOT cover:
      • CORE → swift.cbprplus.03
      • ADV  → swift.cbprplus.adv.03   (carries <Prtry>ADV</Prtry>)
      • COV  → swift.cbprplus.cov.03   (carries UndrlygCstmrCdtTrf)
    For every other family the per-message KB 'expected_value' is authoritative
    (pacs.008/pacs.003 → .03; pain.*/camt.*/pacs.010 → .02).
    """
    low = f"{msg_type} {xml}".lower()
    fam = (msg_type or "").lower()
    if fam.startswith("pacs.009") or "financialinstitutioncredittransfer" in low:
        if "cov" in fam or "undrlygcstmrcdttrf" in low or "swift.cbprplus.cov" in low:
            return "swift.cbprplus.cov.03"
        if "adv" in fam or "<prtry>adv</prtry>" in low or "swift.cbprplus.adv" in low:
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
    base = os.path.join(os.path.dirname(__file__), "..", "resources")
    # Resources may live under `KB/` or directly under `resources/` depending
    # on layout; check both.
    candidates = [
        os.path.normpath(os.path.join(base, "KB", "swift_mx_enterprise_llm_kb.json")),
        os.path.normpath(os.path.join(base, "swift_mx_enterprise_llm_kb.json")),
    ]
    last_err: Optional[Exception] = None
    for kb_path in candidates:
        try:
            with open(kb_path, "r", encoding="utf-8-sig") as f:
                _ENTERPRISE_KB_CACHE = json.load(f)
            for mod in _ENTERPRISE_KB_CACHE.get("modules", []):
                name = mod.get("module_name")
                if name:
                    _ENTERPRISE_MODULE_INDEX[name] = mod
            break
        except FileNotFoundError as e:
            last_err = e
            continue
        except Exception as e:
            last_err = e
            break
    if _ENTERPRISE_KB_CACHE is None:
        logger.warning(f"[FixSuggester] swift_mx_enterprise_llm_kb.json load failed: {last_err}")
        _ENTERPRISE_KB_CACHE = {}
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
# resources/KB/CBPRPlus_Syntactic_Lexical_Validation_KB.json is a companion
# knowledge base for detecting and repairing SYNTACTIC errors (character encoding,
# well-formedness, whitespace, numeric/date lexical form) that are checked BEFORE
# schema/business validation can even run. It is loaded once and consulted by
# the LLM fallback and any deterministic syntactic repair paths.

_SYNTACTIC_KB_CACHE: Optional[Dict[str, Any]] = None


def _load_syntactic_kb() -> Dict[str, Any]:
    global _SYNTACTIC_KB_CACHE
    if _SYNTACTIC_KB_CACHE is not None:
        return _SYNTACTIC_KB_CACHE
    path = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "resources", "KB",
        "CBPRPlus_Syntactic_Lexical_Validation_KB.json",
    ))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            _SYNTACTIC_KB_CACHE = json.load(f)
    except Exception as e:
        logger.warning(f"[FixSuggester] Syntactic KB load failed: {e}")
        _SYNTACTIC_KB_CACHE = {}
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
        kb_dir = os.path.normpath(os.path.join(
            os.path.dirname(__file__), "..", "resources", "KB"))
        path = self._find_file(kb_dir, self.family)
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
        if family not in cls._cache:
            cls._cache[family] = _KBContext(family)
        ctx = cls._cache[family]
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

    # 2. Quoted literal (prefer ones that look like real values: codes / dotted ids)
    for qm in re.finditer(r"'([^']{2,40})'|\"([^\"]{2,40})\"", fix_text):
        cand = (qm.group(1) or qm.group(2) or "").strip()
        if _ok(cand) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._\-]*", cand):
            return cand

    # 3. "one of: A, B, C" enumeration → first option
    m = re.search(r"one of:\s*([A-Z0-9]{2,}(?:\s*,\s*[A-Z0-9]{2,})+)", fix_text)
    if m:
        first = m.group(1).split(",")[0].strip()
        if _ok(first):
            return first
    return None


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


def _kb_field_constraint(tag_name: str) -> Dict[str, Any]:
    """
    Return the field_constraints entry for a tag, or a derived one.

    Falls back via heuristic for tags not explicitly listed:
      *Amt        → Amount type
      *Dt         → Date type
      *DtTm       → DateTime type
      *Id (Max35) → Max35Text
    """
    direct = _kb_get(f"field_constraints.{tag_name}", None)
    if isinstance(direct, dict) and direct:
        return direct

    tn = tag_name
    # Suffix-based heuristics — return a synthesized constraint
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
    "Undrlyg": (
        "<Undrlyg>"
        "<TxInf>"
        f"<OrgnlUETR>{_UETR_SENTINEL}</OrgnlUETR>"
        "<CxlRsnInf><Rsn><Cd>DUPL</Cd></Rsn></CxlRsnInf>"
        "</TxInf>"
        "</Undrlyg>"
    ),
    "TxInf": (
        "<TxInf>"
        f"<OrgnlUETR>{_UETR_SENTINEL}</OrgnlUETR>"
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
        "<Pty><Nm>Party Name</Nm>"
        "<PstlAdr><StrtNm>Main Street</StrtNm><BldgNb>1</BldgNb>"
        "<TwnNm>Amsterdam</TwnNm><Ctry>NL</Ctry></PstlAdr>"
        "</Pty>"
    ),
    "AcctSvcr": "<AcctSvcr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></AcctSvcr>",
    "XpctdValDt": "<XpctdValDt>2026-01-15</XpctdValDt>",

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
    el.text = f"SMPL-{name}"
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

    def _walk_dot_path(self, root: etree._Element, parts: list[str]) -> Optional[etree._Element]:
        """
        Walk root following dot-path parts by local-name.
        E.g. ["Document","FIToFICstmrCdtTrf","CdtTrfTxInf","PmtId"]
        Returns the deepest element found, or None.
        """
        cur = root
        start = 1 if parts and etree.QName(root.tag).localname == parts[0] else 0
        for part in parts[start:]:
            found = None
            for child in cur:
                if isinstance(child.tag, str) and etree.QName(child.tag).localname == part:
                    found = child
                    break
            if found is None:
                return None
            cur = found
        return cur

    def _child_exists(self, parent: etree._Element, local_name: str) -> Optional[etree._Element]:
        for child in parent:
            if isinstance(child.tag, str) and etree.QName(child.tag).localname == local_name:
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
                "ISO", "CBPR", "And", "Or", "Not", "For", "BIC", "IBAN", "UETR",
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
        else:
            # 1. The element the validator stumbled on ("not expected").
            m_found = re.search(r"element '([\w:{}.\-]+)' is not expected", text, re.I)
            if not m_found:
                return None
            found_elem = m_found.group(1).split('}')[-1].split(':')[-1]
            if not found_elem:
                return None

            # 2. The candidate elements the schema expected at that slot. They are
            #    quoted after "expected", e.g. expected : 'CharSet, Fr'  →  the
            #    quoted blob may itself be a comma-separated list.
            exp_candidates = []
            m_exp = re.search(r"expected\s*:?\s*(.+)$", text, re.I | re.S)
            if m_exp:
                for blob in re.findall(r"'([^']+)'", m_exp.group(1)):
                    for tok in re.split(r"[,\s|]+", blob):
                        tok = tok.strip().split('}')[-1].split(':')[-1]
                        if tok and tok not in exp_candidates:
                            exp_candidates.append(tok)

            # 3. Locate the offending element in the live document (line-nearest).
            matches = [el for el in root.iter()
                       if isinstance(el.tag, str)
                       and etree.QName(el.tag).localname == found_elem]
            if not matches:
                return None
            if line_hint is None:
                found_el = matches[0]
            else:
                found_el = min(matches, key=lambda e: abs((e.sourceline or 0) - line_hint))

            parent = found_el.getparent()
            if parent is None:
                return None

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
        xsd_path  = self._get_xsd_path(xml)
        tmap      = _XsdTypeMap.get(xsd_path) if xsd_path else None
        rules_idx = _RulesIndex.get(msg_type) if msg_type else None
        parent_path = self._local_name_path(parent)
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
        if tmap is not None and _parent_type:
            _pinfo = tmap.type_info.get(_parent_type, {})
            if _pinfo.get("kind") == "sequence":
                _xsd_children = _pinfo.get("children", []) or []
                _xsd_order_names = [c["name"] for c in _xsd_children]
        _xsd_mandatory_absent = [
            c["name"] for c in _xsd_children
            if c.get("min", "1") != "0" and c["name"] not in present
        ]

        if explicit_missing:
            candidate_tags = list(exp_candidates)
        else:
            candidate_tags = (list(exp_candidates)
                              + self._kb_mandatory_children(parent_local, msg_type)
                              + _xsd_mandatory_absent)
        # XSD-OPTIONAL children must NEVER be force-inserted in implicit mode: the
        # XSD "expected: A, B, C" list enumerates every element ALLOWED next —
        # including optional ones (min=0). Inserting those (now that the gate
        # builds any XSD-known tag) spuriously injects e.g. TtlIntrBkSttlmAmt=0.00
        # into GrpHdr, creating a NON_POSITIVE_AMOUNT error from thin air. Only
        # mandatory elements (and KB/XSD-mandatory ones) belong here. CBPR-mandatory
        # but XSD-optional agents are handled separately by _fix_missing_cbpr_mandatory.
        _xsd_optional = {c["name"] for c in _xsd_children if c.get("min", "1") == "0"}
        wanted: list[str] = []
        for tag in candidate_tags:
            if tag in wanted or tag in present or not _buildable(tag):
                continue
            if not explicit_missing and tag in _xsd_optional:
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
        if found_elem and wanted and not _found_is_valid_child:
            _misnamed_target = self._closest_expected(found_elem, wanted)
            if _misnamed_target:
                wanted = [t for t in wanted if t != _misnamed_target]
        if not wanted:
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
        xsd_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "xsds", "extracted")
        )
        if not os.path.isdir(xsd_dir):
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
        the newest head.001.001.* on disk."""
        xsd_dir = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "xsds", "extracted")
        )
        if not os.path.isdir(xsd_dir):
            return None
        m = re.search(r"head\.001\.001\.\d{2}", xml)
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

    def _extract_value_from_hint(self, tag_name: str, fix_hint: str) -> Optional[str]:
        """
        Extract a concrete value from the fix_hint using the rules and codelists.
        Returns a plain string value (not XML), or None if not found.
        """
        if not fix_hint:
            return None
        tag_l = tag_name.lower()

        # 1. Explicit quoted value in hint like 'SLEV' or "INGA"
        val_m = re.search(r"['\"]([A-Z0-9]{2,11})['\"]", fix_hint)
        if val_m:
            candidate = val_m.group(1)
            # Validate against known codelists
            for cl_name in ("charge_bearer", "service_level", "local_instrument",
                             "status_code", "purpose_code", "return_reason",
                             "cancellation_reason", "ctgyPurp", "purp"):
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
            for preferred in ("SEPA", "SDVA", "NURG"):
                if preferred in codes:
                    return preferred
            return codes[0] if codes else "SEPA"

        if tag_l == "cd" and "lcl" in fix_hint.lower():
            codes = _codelist_codes("local_instrument")
            return codes[0] if codes else "CORE"

        if tag_l in ("txsts", "grpsts"):
            codes = _codelist_codes("status_code")
            return codes[0] if codes else "ACCP"

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
            smart_val = self._extract_value_from_hint(tag_name, fix_hint)
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

    def suggest(self, xml: str, issue: dict) -> FixSuggestion:
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

        # ── SWIFT Forbidden Character Removal ────────────────────────────────────
        # Layer 2 detects forbidden SWIFT chars in inter-element text (mixed
        # content on parent elements or tail text after child closing tags).
        # Auto-fix: strip those chars from .text / .tail using lxml, then
        # re-serialise — so leaf content like amounts/dates is never touched.
        if code == "SWIFT_FORBIDDEN_CHAR":
            try:
                FORBIDDEN = set(',#;@{}[]()')

                def _strip_forbidden(text: str) -> str:
                    return ''.join(c for c in text if c not in FORBIDDEN)

                _parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
                _root = etree.fromstring(xml.encode("utf-8"), _parser)
                _changed = False

                for _el in _root.iter():
                    if not isinstance(_el.tag, str):
                        continue
                    # Only strip parent.text when the element has children
                    if len(_el) > 0 and _el.text:
                        _cleaned = _strip_forbidden(_el.text)
                        if _cleaned != _el.text:
                            _el.text = _cleaned
                            _changed = True
                    # Always strip tail text (inter-element junk)
                    if _el.tail:
                        _cleaned = _strip_forbidden(_el.tail)
                        if _cleaned != _el.tail:
                            _el.tail = _cleaned
                            _changed = True

                if _changed:
                    _fixed_xml = etree.tostring(_root, encoding="unicode", pretty_print=False)
                    # Restore the XML declaration if the original had one
                    _decl_m = re.match(r'<\?xml[^?]*\?>\s*', xml)
                    if _decl_m:
                        _fixed_xml = _decl_m.group(0) + _fixed_xml
                    return FixSuggestion("/", xml, _fixed_xml, code, msg, "high")
            except Exception:
                pass

        # ── XML Syntax / reserved-character issues ────────────────────────────
        # Layer 1 emits code "XML Syntax Error" (with space), Layer 2 emits
        # "XML_SYNTAX". Both mean the document can't be parsed. Route to
        # recovery BEFORE attempting to parse so that even a code-only signal
        # (e.g. the validator caught the error but lxml happens to be lenient
        # enough to parse it anyway) gets the dedicated repair path.
        _is_syntax_code = code in ("XML_SYNTAX", "XML Syntax Error",
                                   "XML Markup Error", "Invalid Characters")
        # Only route to XML recovery when there are ACTUAL unescaped ampersands
        # (e.g. "Smith & Jones").  Valid XML entities like &gt; &amp; &lt; are
        # already correct and must NOT trigger recovery — doing so would return
        # the document unchanged and skip every schema-error handler below.
        _has_unescaped_amp = bool(
            re.search(r'&(?!(?:amp|lt|gt|apos|quot|#[0-9]+|#x[0-9a-fA-F]+);)', xml)
        )
        if _is_syntax_code or _has_unescaped_amp or "reserved" in msg.lower() or "unclosed" in msg.lower():
            recovered = self._try_xml_recovery(xml, code, msg)
            if recovered is not None:
                return recovered
            # Fall through to normal parse + suggest for anything the recovery
            # couldn't handle — it may still be partially actionable.

        try:
            root = self._parse_xml(xml)
        except FixApplyError:
            # Structural parse failure that recovery also couldn't fix —
            # last resort is the LLM.
            return self._llm_fallback("/", xml[:500], code, msg, fix_hint)

        # ── Duplicate element → keep the first valid occurrence, remove extras ─
        # Runs before the ordering/insert guards so a duplicate (e.g. a second
        # <BICFI>, which the schema reports as "not expected here") is removed
        # rather than mis-repaired by inserting the other expected siblings.
        dup_fix = self._try_remove_duplicate(root, code, msg)
        if dup_fix is not None:
            return dup_fix

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
        if code in ("HEADER_VAL", "HEAD001_BIZSVC_MISSING") and _mh:
            _apphdr = root.find(".//{*}AppHdr")
            if _apphdr is None and etree.QName(root.tag).localname == "AppHdr":
                _apphdr = root
            if _apphdr is not None and self._child_exists(_apphdr, _mh.group(1)) is None:
                _res = self._try_insert_missing_sibling(
                    root, xml, code, msg, fix_hint,
                    explicit_parent=_apphdr, explicit_missing=_mh.group(1),
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
            _ns_pty = etree.QName(root.tag).namespace or ""
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
                            _pty_new = etree.fromstring(_pty_tmpl.encode("utf-8"))
                            _pty_new = self._apply_ns(_pty_new, _ns_pty)
                            return FixSuggestion(
                                _pty_xpath, _pty_orig,
                                self._serialize(_pty_new),
                                code, msg, "high"
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
            _eo_fix = self._fix_stray_text_element_only(root, _eo_m.group(1), code, msg)
            if _eo_fix is not None:
                return _eo_fix

        # Broader catch: even without a named element in the message, strip
        # non-whitespace text/tail from any element-only container when the
        # error fingerprint matches.
        if code == "SCHEMA_VAL" and "element-only" in msg.lower() and "character content" in msg.lower():
            _eo_fix2 = self._fix_stray_text_element_only(root, "", code, msg)
            if _eo_fix2 is not None:
                return _eo_fix2

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
        )):
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
        # True when the path targets any element inside AppHdr (direct or wrapped in
        # an envelope like /BusMsgEnvlp/AppHdr/CreDt).  The old check only matched
        # paths that started with AppHdr itself and missed the envelope-wrapped form.
        targets_apphdr = "AppHdr" in [p for p in path.replace("/", ".").split(".") if p]
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

        # ── Route: camt.056 BAH/BIC mismatch (Fr≠Assgnr or To≠Assgne) ──────────
        # These rules report via check_bic_match whose path is a line number, not
        # an XPath, so the normal path-walk fails. The AppHdr BICFI is format-valid
        # (a real BIC) so _fix_value would exit early with a no-op. Route directly.
        if code in ("CAMT056_FR_EQ_ASSGNR_BIC", "CAMT056_TO_EQ_ASSGNE_BIC"):
            _bic_fix = self._fix_bah_assgnr_assgne_bic(root, code, msg)
            if _bic_fix is not None:
                return _bic_fix

        # If the issue's fix_hint is empty, try to pull `fix` from the matching
        # rule so downstream value-extraction has something to work with.
        if not fix_hint and rules_idx:
            rule = rules_idx.lookup(rule_id=code,
                                     path_parts=[p for p in path.replace("/", ".").split(".") if p],
                                     leaf_tag=(path.replace("/", ".").split(".") or [""])[-1])
            if rule:
                fix_hint = rule.get("fix") or rule.get("errorMessage") or ""

        # ── Parse dot-path ────────────────────────────────────────────────────
        # Detect attribute-target paths like 'IntrBkSttlmAmt@Ccy' or '...Amt@Ccy'.
        # These mean: fix the @Ccy attribute on the IntrBkSttlmAmt element.
        attr_target = ""
        attr_m = re.search(r"@(\w+)\s*$", path)
        if attr_m:
            attr_target = attr_m.group(1)
            path = re.sub(r"@\w+\s*$", "", path).strip().rstrip(".")

        parts = [p.strip() for p in path.replace("/", ".").split(".") if p.strip()]
        # Strip index notation [1] and any embedded @Attr from parts
        parts = [re.sub(r'\[\d+\]', '', p) for p in parts if not p.startswith("@")]
        parts = [re.sub(r'@\w+$', '', p) for p in parts]
        parts = [p for p in parts if p]

        # Drop segments that aren't legal XML element names — most commonly a
        # validator reported path="7" (a line number) or "Line: 7". Keeping such
        # a segment would make the missing-element builder try to create <7/> and
        # crash with "Invalid tag name". When this empties the path we fall through
        # to message-based target recovery below (which reads "<InstrId>" etc.).
        parts = [p for p in parts if _VALID_XML_NAME.match(p)]

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

        missing_tag  = parts[-1]
        parent_parts = parts[:-1]

        # ── Walk to parent ────────────────────────────────────────────────────
        parent_el = self._walk_dot_path(root, parent_parts) if parent_parts else None

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
                    bad_m = re.search(r"value '([^']*)'", f"{msg} {fix_hint}", re.I)
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
                rules_idx=rules_idx, path_parts=parts, root=root,
                msg_type=msg_type
            )

        # ── Check if child already exists ─────────────────────────────────────
        existing = self._child_exists(parent_el, missing_tag)
        if existing is not None:
            # Child exists but has wrong value — fix its value
            return self._fix_value(existing, code, msg, fix_hint, ns)

        # ── Add missing child ─────────────────────────────────────────────────
        original_fragment = self._serialize(parent_el)
        xpath             = self._xpath_of(parent_el)

        # Build in the PARENT's namespace, not the document root's — see note
        # above: enveloped messages have a different root (envelope) ns than the
        # body (pacs.008/etc.), and stamping the wrong ns corrupts the insert.
        child_ns = etree.QName(parent_el.tag).namespace or ns
        child_el = self._build_child(missing_tag, fix_hint, child_ns, tmap,
                                     existing_parent=parent_el,
                                     rules_idx=rules_idx, path_parts=parts,
                                     rule_id=code, root=root, msg_type=msg_type)
        if child_el is None:
            return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint)

        # Insert the new child in the correct position based on XSD sequence order.
        # Fallback: append (safe default).
        parent_copy = self._copy(parent_el)
        insert_idx  = self._find_insert_index(parent_copy, missing_tag, tmap,
                                              parent_path=parts[:-1])
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
        Walk parts from left to right. Return (deepest_existing_element, remaining_parts).
        E.g. parts=[Document, FIToFI, CdtTrfTxInf, Dbtr, PstlAdr]
             if Dbtr exists but PstlAdr does not →
             returns (Dbtr_element, ["PstlAdr"])
        """
        cur = root
        start = 1 if parts and etree.QName(root.tag).localname == parts[0] else 0
        for i, part in enumerate(parts[start:], start=start):
            found = None
            for child in cur:
                if isinstance(child.tag, str) and etree.QName(child.tag).localname == part:
                    found = child
                    break
            if found is None:
                return cur, list(parts[i:])
            cur = found
        return cur, []

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
        insert_idx  = self._find_insert_index(anchor_copy,
                                               etree.QName(inner.tag).localname,
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
            ibans = (_kb_get("dummy_data.ibans", {}) or
                     _enterprise_shared("dummy_data.ibans", {}) or {})
            if isinstance(ibans, dict):
                return ibans.get("default", "GB29NWBK60161331926819")
            return "GB29NWBK60161331926819"
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
                    _num_a = float(_cur_a)
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
            return self._repair_country(el, cur_txt, root)
        if ctype == "Amount":
            # Only emit a dummy when there is genuinely no numeric value to repair
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

    def _fix_stray_text_element_only(
        self,
        root: etree._Element,
        container_name: str,
        code: str,
        msg: str,
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
        if not _strip_stray(target_copy):
            return None

        return FixSuggestion(
            xpath=self._xpath_of(target),
            original_fragment=original_fragment,
            fragment_xml=self._serialize(target_copy),
            issue_code=code,
            issue_message=msg,
            confidence="high",
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
        m = re.search(r"element '([^']+)' is not (?:expected|allowed)", msg, re.I)
        if not m:
            return None
        # If the validator supplied an expected list, the dedicated sequence/
        # sibling handlers own it — only act on the no-list (pure position) case.
        if re.search(r"following element", msg, re.I):
            return None
        offending = m.group(1).split('}')[-1].split(':')[-1].strip().strip("':\" ")
        if not offending:
            return None

        line_hint = getattr(self, "_line_hint", None)
        matches = [el for el in root.iter()
                   if isinstance(el.tag, str) and etree.QName(el.tag).localname == offending]
        if not matches:
            return None
        off_el = (matches[0] if line_hint is None
                  else min(matches, key=lambda e: abs((e.sourceline or 0) - line_hint)))
        parent = off_el.getparent()
        if parent is None:
            return None

        parent_path = self._local_name_path(parent)
        ns = etree.QName(parent.tag).namespace or ""
        in_apphdr = "AppHdr" in parent_path
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
                              msg: str) -> Optional[FixSuggestion]:
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
        _lh = getattr(self, "_line_hint", None)
        off_el = (min(_off_cands, key=lambda e: abs((e.sourceline or 0) - _lh))
                  if _lh is not None else _off_cands[0])
        parent = off_el.getparent()
        if parent is None:
            return None
        parent_path = self._local_name_path(parent)
        # Build new children in the PARENT's namespace (AppHdr is head.001, the
        # Document body is the message namespace) — not the document root's.
        ns = etree.QName(parent.tag).namespace or ns

        # Pick the schema that defines this parent: AppHdr → head (BAH) XSD,
        # everything else → the Document message XSD.
        in_apphdr = "AppHdr" in parent_path
        xsd_path  = (self._get_apphdr_xsd_path(xml) if in_apphdr
                     else self._get_xsd_path(xml))
        tmap = _XsdTypeMap.get(xsd_path) if xsd_path else None
        parent_type = tmap.type_of_path(parent_path) if tmap else None
        order = tmap.order_for_type(parent_type) if (tmap and parent_type) else []

        original_fragment = self._serialize(parent)
        xpath = self._xpath_of(parent)

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
        #    Insert the missing mandatory expected element(s) and reorder. We add
        #    ONLY mandatory elements — never optional ones — so a pure ordering
        #    issue is not "fixed" by injecting unwanted optional tags. ──────────
        existing = {etree.QName(c.tag).localname for c in parent if isinstance(c.tag, str)}

        def is_mandatory(child: str) -> bool:
            if not (tmap and parent_type):
                return False  # no schema info → can't assert mandatory; decline
            for c in tmap.type_info.get(parent_type, {}).get("children", []):
                if c["name"] == child:
                    return c.get("min", "1") != "0"
            return False

        # Terse-variant fallback: lxml named no expected element, so derive the
        # missing mandatory predecessor(s) from the XSD sequence — every mandatory
        # child that must appear BEFORE the offending element but is absent from
        # the parent. Classic case: <EndToEndId> deleted from <PmtId> →
        # "The element 'TxId' is not expected here." with no expected list.
        if not expected and order:
            try:
                off_pos = order.index(offending)
            except ValueError:
                off_pos = len(order)
            expected = [c for c in order[:off_pos]
                        if c not in existing and is_mandatory(c)]

        to_add = [e for e in expected if e not in existing and is_mandatory(e)]
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

        parent_copy = self._copy(parent)
        for child in to_add:
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

        _lh = getattr(self, "_line_hint", None)
        _cands = [el for el in root.iter()
                  if isinstance(el.tag, str)
                  and etree.QName(el.tag).localname == offending]
        if not _cands:
            return None
        off_el = (min(_cands, key=lambda e: abs((e.sourceline or 0) - _lh))
                  if _lh is not None else _cands[0])

        parent = off_el.getparent()
        if parent is None:
            return None

        in_apphdr = "AppHdr" in self._local_name_path(parent)
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
        # the offending element as a child.
        container_name = None
        for child_info in tmap.type_info.get(parent_type, {}).get("children", []):
            cname = child_info["name"]
            ctype = tmap.get_child_type(parent_type, cname)
            if not ctype:
                continue
            ctype_children = {c["name"] for c in
                               tmap.type_info.get(ctype, {}).get("children", [])}
            if offending in ctype_children:
                container_name = cname
                break
        if not container_name:
            return None

        # Collect all elements from off_idx onwards that are either:
        #   a) the offending element, or
        #   b) not a valid parent child (clearly orphaned TxInf-level fields)
        orphan_indices = []
        for i, sib in enumerate(siblings[off_idx:], start=off_idx):
            sib_local = etree.QName(sib.tag).localname if isinstance(sib.tag, str) else ""
            if sib_local == offending or sib_local not in valid_parent_children:
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

        # Detach orphans from the copy (in reverse order to keep indices valid)
        orphan_els = [copy_kids[i] for i in orphan_indices]
        for el in reversed(orphan_els):
            parent_copy.remove(el)

        # Build new container and populate with orphans (already in XML order)
        new_container = etree.SubElement(parent_copy, container_tag)
        for el in orphan_els:
            new_container.append(el)

        # Reorder new container's children to XSD sequence
        container_type = tmap.get_child_type(parent_type, container_name)
        container_order = tmap.order_for_type(container_type) if container_type else []
        if container_order:
            self._reorder_children(new_container, container_order)

        # Reorder parent so any valid OrgnlGrpInf etc. appear before TxInf
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
        lh = getattr(self, "_line_hint", None)
        off_el = (min(off_cands, key=lambda e: abs((e.sourceline or 0) - lh))
                  if lh is not None else off_cands[0])

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
            xsd_path = (self._get_apphdr_xsd_path(xml) if "AppHdr" in parent_path
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
            or (code in ("NAME_ADDRESS_COEXISTENCE", "DEP_014") or "present together" in msg_l)
        # Schema-level manifestation of BICFI exclusivity: a forbidden sibling
        # (Nm/PstlAdr) reported by the XSD as "not expected" while BICFI/AnyBIC is
        # present in the same block (e.g. "The element 'Nm' is not expected here.
        # No child element is expected at this point."). Target-finding below
        # only acts when such a block actually exists, so this is safe.
        if (not (is_bicfi_excl or is_anybic_excl or is_name_addr)
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
            return emit(target) if target is not None else None

        # ── Name/Address coexistence: blocks with exactly one of the pair ─────
        a = coex_tags[0]
        b = coex_tags[1] if len(coex_tags) > 1 else "PstlAdr"
        targets = [el for el in root.iter()
                   if (child(el, a) is not None) != (child(el, b) is not None)]
        target = self._pick_nearest(targets, line_hint)
        return emit(target) if target is not None else None

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
            _iban_ccy_m = re.search(
                r"(?:expected currency|transaction currency to|update.*to)\s+([A-Z]{3})\b",
                _combined, re.I,
            )
            if _iban_ccy_m:
                _candidate = _iban_ccy_m.group(1).upper()
                if not valid_currencies or _candidate in valid_currencies:
                    new_value = _candidate

            # 2. Walk the doc for another element with a valid @Ccy attribute
            if new_value is None:
                try:
                    root = el.getroottree().getroot()
                except Exception:
                    root = None
                if root is not None:
                    for sib in root.iter():
                        if not isinstance(sib.tag, str):
                            continue
                        sib_ccy = (sib.get("Ccy") or "").strip().upper()
                        if sib_ccy and sib_ccy != cur_bad:
                            if not valid_currencies or sib_ccy in valid_currencies:
                                new_value = sib_ccy
                                break

            if new_value is None:
                new_value = "USD"  # safe ISO 4217 default

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
            _valid_dt = re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+\-]\d{2}:\d{2}$", _cur)
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
        _ccy_is_the_issue = (
            "ccy" in msg_l                       # explicit @Ccy mention
            or "currency code" in msg_l          # "invalid currency code"
            or "currencycode" in msg_l           # XSD attribute error
            or "@ccy" in msg_l                   # attribute path
            or (el.get("Ccy") is None and "currency" in msg_l)  # Ccy attr absent
        ) and not re.search(r"'[\s\d.]+'\s*(is not|not valid|not a)", msg_l)
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
                            "PgNb", "NbOfDays", "Qty", "Nb"}
            or (isinstance(_kb_field_constraint(el_local), dict)
                and _kb_field_constraint(el_local).get("type") in ("Number", "Numeric15", "Quantity"))
        )
        if not list(el) and el.text and (
                ("number" in msg_l and "type" in msg_l)
                or (_numeric_field and any(k in msg_l for k in
                    ("invalid value", "invalid", "not a valid", "number", "format", "type", "expected")))):
            _cur = el.text.strip()
            if not re.match(r"^-?\d+(\.\d+)?$", _cur):
                _con = _kb_field_constraint(el_local)
                _ex = _con.get("example") if isinstance(_con, dict) else None
                _new = str(_ex) if (_ex and re.match(r"^-?\d+(\.\d+)?$", str(_ex))) else "1"
                el_copy = self._copy(el)
                el_copy.text = _new
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── BizMsgIdr must equal GrpHdr/MsgId → harvest MsgId (KB rule DEP_001) ──
        if el_local == "BizMsgIdr" and (code == "BIZMSGIDR_NEQ_MSGID" or "msgid" in msg_l):
            try:
                root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                root = None
            mv = self._harvest_value(root, "MsgId") if root is not None else None
            if mv and mv != (el.text or "").strip():
                el_copy = self._copy(el)
                el_copy.text = mv
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

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
                or code == "INVALID_DECIMAL_PRECISION") and not list(el) and el.text:
            _amt_cur = (el.text or "").strip()
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
                pass

        # ── Text too long: truncate in-place, never replace ─────────────────
        # "Jhon ......(162 chars)" → "Jhon ......(140 chars)" for Max140Text.
        # GUARD: only fires for plain Max*Text types (Nm, AdrLine, Ustrd, etc.).
        # Constrained types like Currency (^[A-Z]{3}$), Country, BICFI, IBAN
        # must NOT be truncated — "150" is not a valid currency code.
        if not list(el) and el.text:
            _con_t = _kb_field_constraint(el_local)
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
            con = _kb_field_constraint(el_local)
            max_len = con.get("max_length") if isinstance(con, dict) else None
            cur = el.text.strip()
            if isinstance(max_len, int) and len(cur) > max_len:
                trimmed = cur[:max_len].rstrip() or cur[:max_len]
                el_copy = self._copy(el)
                el_copy.text = trimmed
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

        # ── Count / sum aggregates (NbOfTxs, CtrlSum) ─────────────────────────
        # These are derived from the document, so there's a single correct
        # value — compute it rather than guessing. Without this they fell
        # through to a low-confidence no-op and silently dropped out of batches.
        if el_local in ("NbOfTxs", "CtrlSum"):
            try:
                root = el.getroottree().getroot() if el.getroottree() is not None else None
            except Exception:
                root = None
            if root is not None:
                TX_TAGS = {"CdtTrfTxInf", "DrctDbtTxInf", "TxInfAndSts", "PmtInf"}
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
                    # ISO datetime WITH a valid explicit offset — leave unchanged
                    _iso_dt_with_tz = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?[+\-]\d{2}:\d{2}$"
                    if re.match(_iso_dt_no_tz, txt):
                        new_val = re.sub(r"\.\d+", "", txt) + "+00:00"
                    elif re.match(_iso_dt_with_tz, txt):
                        new_val = txt  # already has a valid explicit offset
                    else:
                        # Value is not a valid ISO datetime at all — generate a fresh one
                        new_val = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                    txt = (el.text or "").strip()
                    if "+" in txt or (len(txt) > 6 and "-" in txt[-6:]):
                        new_val = txt  # already has offset
                    elif re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', txt):
                        new_val = re.sub(r'Z$', '', txt) + "+00:00"
                    else:
                        # Garbage value — generate a fresh valid datetime
                        new_val = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
                else:
                    new_val = el.text

                el_copy = self._copy(el)
                el_copy.text = new_val
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")
        # -----------------------------------

        # ── Bad text value vs field-constraint regex: regenerate it ───────────
        # Highest-priority deterministic fix for cases like UETR=`UETR`,
        # Amt=`EU`, MsgId=`X` * 50, BICFI=`CREDITMM` (too short).
        if not list(el) and el.text:
            current_text = (el.text or "").strip()
            constraint = _kb_field_constraint(el_local)

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
            con = _kb_field_constraint(el_local)
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
            kb_constraint = _kb_field_constraint(el_local)
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
                        break
                el_copy = self._copy(el)
                el_copy.text = chosen
                return FixSuggestion(xpath, original_fragment,
                                     self._serialize(el_copy), code, msg, "high")

            # Check codelists in priority order based on tag name
            codelist_map = {
                "ChrgBr":    "charge_bearer",
                "SvcLvl":    "service_level",
                "LclInstrm": "local_instrument",
                "TxSts":     "status_code",
                "GrpSts":    "status_code",
                "Ctry":      "country",
                "Cd":        None,  # context-dependent — try multiple
            }
            cl_name = codelist_map.get(el_local)

            if cl_name:
                # 1. Use _extract_value_from_hint — it has tag-specific preferred
                #    values (e.g. SLEV for ChrgBr) before falling back to list order
                smart = self._extract_value_from_hint(el_local, fix_hint + " " + msg)
                if smart:
                    el_copy = self._copy(el)
                    el_copy.text = smart
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

                codes = _codelist_codes(cl_name)
                if codes:
                    # 2. Find first code explicitly mentioned in hint/msg (exact word boundary)
                    combined = fix_hint + " " + msg
                    for c in codes:
                        if re.search(rf'\b{re.escape(c)}\b', combined):
                            el_copy = self._copy(el)
                            el_copy.text = c
                            return FixSuggestion(xpath, original_fragment,
                                                 self._serialize(el_copy), code, msg, "high")
                    # 3. Default: first valid code in the list
                    el_copy = self._copy(el)
                    el_copy.text = codes[0]
                    return FixSuggestion(xpath, original_fragment,
                                         self._serialize(el_copy), code, msg, "high")

            if el_local == "Cd":
                # Infer context from fix_hint
                for hint_cl in ("charge_bearer", "service_level", "local_instrument",
                                "status_code", "return_reason", "cancellation_reason",
                                "ctgyPurp", "purp"):
                    codes = _codelist_codes(hint_cl)
                    for c in codes:
                        if c in fix_hint:
                            el_copy = self._copy(el)
                            el_copy.text = c
                            return FixSuggestion(xpath, original_fragment,
                                                 self._serialize(el_copy), code, msg, "high")

            # Generic enum match from message text
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
            # Try to extract a valid BIC from fix_hint
            bic_m = re.search(r'\b([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b', fix_hint)
            el_copy = self._copy(el)
            el_copy.text = bic_m.group(1) if bic_m else "DEUTDEFFXXX"
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # ── IBAN invalid ──────────────────────────────────────────────────────
        if "iban" in msg_l and "invalid" in msg_l:
            el_copy = self._copy(el)
            el_copy.text = "GB29NWBK60161331926819"
            return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

        # A candidate value extracted from free-text is only acceptable if it
        # isn't just the tag name echoed back and doesn't itself violate the
        # element's constraint (guards against e.g. writing 'UETR' into <UETR>).
        def _usable_candidate(val: str) -> bool:
            if not val or val == el_local:
                return False
            con = _kb_field_constraint(el_local)
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
            etree.QName(el.tag).localname, fix_hint + " " + msg
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

        # Fall through to LLM (enriched with the KB's documented fix recipes)
        return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint, kb_fixes)

    def _llm_fallback(self, xpath: str, original_fragment: str,
                      code: str, msg: str, fix_hint: str = "",
                      kb_fixes: Optional[list] = None) -> FixSuggestion:
        """Last-resort LLM call with rich context. max_tokens=400, temperature=0."""
        # Build context: include rule hint, codelists, field constraints, deps
        context_lines = []

        # ── Per-message KB (resources/KB/<family>.json): documented fix recipes ──
        if kb_fixes:
            context_lines.append(
                "CBPR+ KB documented fixes for this field:\n"
                + "\n".join(f"- {fx}" for fx in kb_fixes[:6]))

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
        # When the error code appears in the syntactic KB's error_code_catalogue,
        # surface its field_class policy and deterministic fix so the LLM is guided
        # by the lexical KB rather than inventing a fix. This covers codes like
        # WF_UNESCAPED_AMPERSAND, NUMERIC_THOUSANDS_SEP, DATETIME_SPACE_SEPARATOR,
        # INVISIBLE_CHAR, etc. that the global business KB does NOT catalogue.
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
                # Code not in catalogue — surface the scope/pipeline guidance so the
                # LLM knows to apply syntactic repairs before business-rule fixes.
                scope = syn_kb.get("scope_and_precedence", {})
                owns = scope.get("this_kb_owns", [])
                pipeline = scope.get("fix_pipeline_order", [])
                if owns or pipeline:
                    lines = [f"  - {o}" for o in owns[:4]]
                    if pipeline:
                        lines += [f"  {p}" for p in pipeline[:3]]
                    context_lines.append(
                        "Syntactic/Lexical KB (apply BEFORE schema/business fixes):\n"
                        + "\n".join(lines)
                    )
        except Exception as _e:
            logger.debug(f"[FixSuggester] Syntactic KB context failed: {_e}")

        # ── Per-message KB STRUCTURE (resources/KB/<msg>.json) ────────────────
        # "Check the KB before fixing": hand the model the authoritative shape of
        # every element named in the error — its canonical xpath (correct
        # nesting/parentage), cardinality, and documented fix — so structural
        # repairs (missing wrapper, misplaced element) follow the KB, not a guess.
        try:
            kb_tags = set()
            if target_tag:
                kb_tags.add(target_tag)
            for tok in re.findall(r"'([A-Za-z][\w]*)'|element '([A-Za-z][\w]*)'|<([A-Za-z][\w]*)>",
                                  f"{msg} {fix_hint}"):
                for g in tok:
                    if g:
                        kb_tags.add(g)
            # The serialized fragment carries the message namespace, so message
            # type can be detected from it to pick the right per-message KB file.
            kb_msg_type = _detect_msg_type(original_fragment) or ""
            hints = _kb_folder_structural_hints(list(kb_tags), code, kb_msg_type, original_fragment)
            if hints:
                context_lines.append("Knowledge-base structure (authoritative):\n" + "\n".join(hints))
        except Exception as e:
            logger.debug(f"[FixSuggester] KB structural hints failed: {e}")

        # ── KB field constraints: include length/type/example for the target ──
        if target_tag:
            constraint = _kb_field_constraint(target_tag)
            if constraint:
                bits = []
                if constraint.get("type"):       bits.append(f"type={constraint['type']}")
                if constraint.get("max_length"): bits.append(f"max_length={constraint['max_length']}")
                if constraint.get("min_length"): bits.append(f"min_length={constraint['min_length']}")
                if constraint.get("example"):    bits.append(f"example={constraint['example']}")
                if constraint.get("preferred"):  bits.append(f"preferred_value={constraint['preferred']}")
                if constraint.get("valid"):      bits.append(f"valid_values={constraint['valid']}")
                if constraint.get("notes"):      bits.append(f"notes={constraint['notes']}")
                if bits:
                    context_lines.append(f"Field <{target_tag}>: {' | '.join(bits)}")

        # ── KB dependencies relevant to this tag ──────────────────────────────
        if target_tag:
            deps_relevant = []
            for dep_kind in ("equals", "not_equal", "conditional_required", "exclusive"):
                for dep in _kb_get(f"dependencies.{dep_kind}", []) or []:
                    dep_text = json.dumps(dep)
                    if target_tag in dep_text:
                        desc = dep.get("description", "")
                        if desc:
                            deps_relevant.append(f"- ({dep_kind}) {desc}")
            if deps_relevant:
                context_lines.append("Cross-field invariants to preserve:\n" + "\n".join(deps_relevant[:5]))

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
        ]:
            if any(kw in msg_l for kw in (cl_name.replace("_", ""), cl_name.split("_")[0],
                                           label.lower().split()[1].lower())):
                codes = _codelist_codes(cl_name)[:20]
                if codes:
                    context_lines.append(f"{label}: {', '.join(codes)}")

        # Inject realistic BICFIs whenever the error touches agents/BIC fields.
        # Without this the LLM falls back to placeholder strings like "YOURBICCODE".
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
        user += f"\n\nBroken element:\n{original_fragment}"

        text, available = complete(system, user, max_tokens=400)
        if not available or not text.strip():
            # LLM unreachable — return the original fragment with low confidence
            # so the frontend can still surface guidance (rule's fix_suggestion)
            # instead of marking the issue as completely unavailable.
            logger.warning(f"[FixSuggester] LLM unavailable for {code}; returning low-confidence original")
            return FixSuggestion(xpath, original_fragment, original_fragment, code, msg, "low")

        frag = re.sub(r"^```[a-z]*\n?", "", text.strip(), flags=re.I)
        frag = re.sub(r"\n?```$",        "", frag,         flags=re.I).strip()

        try:
            new_el = etree.fromstring(frag.encode("utf-8"))
            # If original_fragment is non-empty, verify the root tag matches
            if original_fragment.strip():
                orig_local = etree.QName(
                    etree.fromstring(original_fragment.encode("utf-8")).tag
                ).localname
                if etree.QName(new_el.tag).localname != orig_local:
                    raise ValueError("root tag mismatch")
            # Rename any <BIC> elements to <BICFI>: LLM occasionally emits the
            # wrong tag name; <BIC> is not a valid FinInstnId child in ISO 20022.
            for el in new_el.iter():
                if etree.QName(el.tag).localname == "BIC":
                    ns = etree.QName(el.tag).namespace
                    el.tag = f"{{{ns}}}BICFI" if ns else "BICFI"
            frag = etree.tostring(new_el, encoding="unicode")
        except Exception as e:
            logger.warning(f"[FixSuggester] LLM returned invalid fragment for {code}: {e}")
            return FixSuggestion(xpath, original_fragment, original_fragment, code, msg, "low")

        return FixSuggestion(xpath, original_fragment, frag, code, msg, "high")

    def _unavail(self, path: str, code: str, msg: str) -> FixSuggestion:
        return FixSuggestion(xpath=path, original_fragment="", fragment_xml="",
                             issue_code=code, issue_message=msg, confidence="unavailable")

    def suggest_batch(self, xml: str, issues: list[dict]) -> list[FixSuggestion]:
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
            # Isolate failures per issue: a single issue that trips an unexpected
            # error inside suggest() must NOT abort the whole batch (which would
            # leave the caller — e.g. the iterative auto-fixer — applying ZERO
            # fixes across the entire document). Degrade that one issue to an
            # unavailable suggestion and carry on with the rest.
            try:
                sug = self.suggest(current_xml, issue)
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
                # No change produced. If this issue's target element was ALREADY
                # corrected by an earlier fix in this batch, the defect is
                # resolved — surface that distinctly so the UI doesn't show it
                # as an unfixed item.
                if sug.xpath and sug.xpath in changed_xpaths:
                    sug = FixSuggestion(
                        sug.xpath, sug.original_fragment, sug.fragment_xml,
                        sug.issue_code, sug.issue_message, "resolved",
                    )

            suggestions.append(sug)
        return suggestions

    # ── XML syntax recovery ───────────────────────────────────────────────────

    def _escape_reserved_xml_chars(self, xml: str) -> Optional[str]:
        """
        Escape unescaped XML reserved characters in text content and attribute
        values. Targets the most common causes of "values contain no reserved
        XML characters" errors — principally the unescaped ampersand `&`.

        Uses a negative-lookahead regex so that already-valid entity references
        (&amp; &lt; &gt; &apos; &quot; &#NNN; &#xHHH;) are left untouched.
        ISO 20022 field values containing '&' (e.g. "Smith & Jones") should be
        written as '&amp;' — this fix applies that escaping with zero data loss,
        unlike lxml's recover mode which can silently strip content.
        """
        # Match & NOT already followed by a valid XML entity reference ending in ;
        pattern = r'&(?!(?:amp|lt|gt|apos|quot|#[0-9]+|#x[0-9a-fA-F]+);)'
        fixed = re.sub(pattern, '&amp;', xml)
        return fixed if fixed != xml else None

    def _try_surgical_unclosed_tag_fix(self, xml: str, msg: str) -> Optional[str]:
        """
        Surgical Stage-0 repair for a missing closing tag.

        Handles the exact error format emitted by the XML validator:
          "Unclosed tag <Fr> at line 4. The tag <Fr> must be closed with
           </Fr> before the closing tag </FIId> at line 6."

        Extracts the unclosed tag name and the line of the conflicting
        closing tag, then inserts </missing_tag> immediately before that
        closing tag.  The result is validated; on failure returns None so
        the caller can fall through to lxml structural recovery.

        Works for ANY tag name — 'Fr', 'To', 'GrpHdr', 'CdtTrfTxInf', etc.
        """
        m = re.search(
            r"Unclosed\s+tag\s+<([\w:]+)>.*?"
            r"before\s+the\s+closing\s+tag\s+</([\w:]+)>.*?at\s+line\s+(\d+)",
            msg, re.I | re.S,
        )
        if not m:
            return None

        missing_tag  = m.group(1)   # e.g. "Fr"
        conflict_tag = m.group(2)   # e.g. "FIId"
        conflict_ln  = int(m.group(3))  # e.g. 6

        lines = xml.splitlines(keepends=True)
        if not (1 <= conflict_ln <= len(lines)):
            return None

        target_line = lines[conflict_ln - 1]

        # Match </conflict_tag> with or without an optional namespace prefix
        close_pat = re.compile(
            r"(</" + re.escape(conflict_tag) + r"\s*>|"
            r"</[\w]+:" + re.escape(conflict_tag) + r"\s*>)",
            re.I,
        )
        if not close_pat.search(target_line):
            return None

        # Use the same indentation as the conflict line for the inserted tag
        indent = re.match(r"(\s*)", target_line).group(1)
        fixed_line = close_pat.sub(
            f"</{missing_tag}>\n{indent}" + r"\1",
            target_line, count=1,
        )
        fixed_lines = lines[:conflict_ln - 1] + [fixed_line] + lines[conflict_ln:]
        fixed_xml = "".join(fixed_lines)

        try:
            etree.fromstring(fixed_xml.encode("utf-8"))
            return fixed_xml
        except etree.XMLSyntaxError:
            return None  # Surgical fix insufficient; fall through to lxml recovery

    def _repair_apphdr_agents(self, xml_str: str) -> str:
        """
        Post-lxml-recovery repair: ensure AppHdr/Fr and AppHdr/To each
        contain a properly nested FIId/FinInstnId/BICFI element.

        When structural recovery collapses a mangled Fr/To block the tree
        can have an empty <Fr/> and no <To> at all.  This method:
          1. Finds BICFI values in the document body (not inside AppHdr).
          2. Uses semantic xpath hints (Assgnr→Fr, Assgne→To for camt.056;
             InstgAgt→Fr, InstdAgt→To for pacs.*) to pick the right BIC.
          3. Falls back to first/second body BICFI when hints don't match.
          4. Creates missing elements at the correct sequence position.

        Returns the (possibly modified) XML string; returns the original on
        any parse failure so the caller can still offer the partial fix.
        """
        try:
            root = etree.fromstring(xml_str.encode("utf-8"))
        except etree.XMLSyntaxError:
            return xml_str

        apphdr = root.find(".//{*}AppHdr")
        if apphdr is None:
            return xml_str

        ns = etree.QName(apphdr.tag).namespace or ""
        apphdr_ids = {id(el) for el in apphdr.iter()}

        def _q(local: str) -> str:
            return f"{{{ns}}}{local}" if ns else local

        def _has_bicfi(container) -> bool:
            for el in container.iter():
                if isinstance(el.tag, str) and etree.QName(el.tag).localname == "BICFI":
                    if (el.text or "").strip():
                        return True
            return False

        def _find_body_bic(xpath_hint: str, skip: Optional[str] = None) -> Optional[str]:
            """First body BICFI whose xpath contains xpath_hint (case-insensitive)."""
            for el in root.iter():
                if not isinstance(el.tag, str) or id(el) in apphdr_ids:
                    continue
                if etree.QName(el.tag).localname != "BICFI":
                    continue
                v = (el.text or "").strip()
                if not v or v == skip:
                    continue
                if xpath_hint and f"/{xpath_hint.lower()}/" not in self._xpath_of(el).lower():
                    continue
                return v
            return None

        def _any_body_bic(skip: Optional[str] = None) -> Optional[str]:
            """First body BICFI not equal to skip."""
            for el in root.iter():
                if not isinstance(el.tag, str) or id(el) in apphdr_ids:
                    continue
                if etree.QName(el.tag).localname != "BICFI":
                    continue
                v = (el.text or "").strip()
                if v and v != skip:
                    return v
            return None

        def _repair_role(role: str, hints: list[str], skip_bic: Optional[str] = None) -> Optional[str]:
            """
            Ensure AppHdr/<role> has a BICFI; create/populate if needed.
            Returns the BIC that was installed (or already present), or None.
            """
            role_el = None
            for child in apphdr:
                if isinstance(child.tag, str) and etree.QName(child.tag).localname == role:
                    role_el = child
                    break

            if role_el is not None and _has_bicfi(role_el):
                # Already correct — return existing BIC without touching anything
                for el in role_el.iter():
                    if isinstance(el.tag, str) and etree.QName(el.tag).localname == "BICFI":
                        return (el.text or "").strip() or None
                return None

            # Select the best BIC from the document body
            bic = None
            for hint in hints:
                bic = _find_body_bic(hint, skip=skip_bic)
                if bic:
                    break
            if not bic:
                bic = _any_body_bic(skip=skip_bic)
            if not bic:
                return None

            # Create element if absent
            if role_el is None:
                role_el = etree.Element(_q(role))
                children_local = [
                    etree.QName(c.tag).localname
                    for c in apphdr if isinstance(c.tag, str)
                ]
                if role == "Fr":
                    insert_pos = 0
                else:  # "To" — after Fr
                    insert_pos = next(
                        (i + 1 for i, ln in enumerate(children_local) if ln == "Fr"), 0
                    )
                apphdr.insert(insert_pos, role_el)
            else:
                # Clear broken content
                for c in list(role_el):
                    role_el.remove(c)

            # Build FIId/FinInstnId/BICFI
            fiid_el   = etree.SubElement(role_el, _q("FIId"))
            finstn_el = etree.SubElement(fiid_el,  _q("FinInstnId"))
            bicfi_el  = etree.SubElement(finstn_el, _q("BICFI"))
            bicfi_el.text = bic
            return bic

        # camt.05x: Assgnr→Fr, Assgne→To
        # pacs.*  : InstgAgt→Fr, InstdAgt→To
        # General fallback: first body BIC→Fr, second (different)→To
        fr_bic = _repair_role("Fr", ["Assgnr", "InstgAgt", "IntrstAgt"])
        _repair_role("To", ["Assgne", "InstdAgt", "FwdgAgt"], skip_bic=fr_bic)

        try:
            result = etree.tostring(root, encoding="unicode", pretty_print=True)
            etree.fromstring(result.encode("utf-8"))  # confirm still well-formed
            return result
        except Exception:
            return xml_str

    def _try_xml_recovery(self, xml: str, code: str, msg: str) -> Optional["FixSuggestion"]:
        """
        Repair malformed XML in three stages, returning the first that
        produces a well-formed document.

        STAGE 0 — Surgical unclosed-tag insert (zero data loss):
          When the error message identifies exactly which tag is unclosed and
          where, insert the missing closing tag at that position. Preferred
          over lxml recover=True which can silently collapse the tree.

        STAGE 1 — Character escaping (zero data loss):
          Unescaped `&` in field values (e.g. "Smith & Jones") is the most
          frequent cause of "reserved XML characters" errors.  Replacing `&`
          with `&amp;` always preserves the original text.  lxml's recovery
          mode would silently strip the content after the `&` — unacceptable
          for financial data — so we always try escaping FIRST.

        STAGE 2 — Structural recovery (lxml recover=True):
          Closes unclosed tags, removes extra closing tags, and repairs other
          structural problems that char escaping cannot address.

        Returns a whole-document replacement FixSuggestion (xpath="/") with
        high confidence, or None when all stages fail or produce no change.
        """
        def _make_suggestion(fixed: str) -> "FixSuggestion":
            return FixSuggestion(
                xpath="/",
                original_fragment=xml,
                fragment_xml=fixed,
                issue_code=code,
                issue_message=msg,
                confidence="high",
            )

        # ── Stage 0: surgical unclosed-tag insert ────────────────────────────
        if "unclosed" in msg.lower():
            surgical = self._try_surgical_unclosed_tag_fix(xml, msg)
            if surgical is not None:
                return _make_suggestion(surgical)

        # ── Stage 1: escape unescaped reserved characters ────────────────────
        escaped = None  # initialise so Stage 2 can safely reference it
        try:
            escaped = self._escape_reserved_xml_chars(xml)
            if escaped:
                try:
                    etree.fromstring(escaped.encode("utf-8"))
                    return _make_suggestion(escaped)
                except etree.XMLSyntaxError:
                    # Escaping alone didn't fix everything (e.g. there are also
                    # unclosed tags); fall through to structural recovery.
                    pass
        except Exception as e:
            logger.debug(f"[FixSuggester] char-escape stage failed: {e}")

        # ── Stage 2: lxml structural recovery ────────────────────────────────
        try:
            # Preserve the XML declaration so the re-serialised output looks
            # identical to the original apart from the structural repair.
            decl = ""
            decl_m = re.match(r"(<\?xml[^?]*\?>)\s*", xml)
            if decl_m:
                decl = decl_m.group(1) + "\n"

            # If Stage 1 produced a partially-fixed string, recover from that
            # rather than the original (combines both fixes in one pass).
            source = escaped if escaped else xml

            parser = etree.XMLParser(
                recover=True,
                remove_blank_text=False,
                no_network=True,
            )
            root = etree.fromstring(source.encode("utf-8"), parser)
            if root is None:
                return None

            raw_recovered = etree.tostring(root, encoding="unicode", pretty_print=True)

            # ── Post-recovery: restore missing/broken AppHdr Fr and To ──────
            # lxml collapse of a mangled Fr/To block produces an empty <Fr/>
            # with no BICFI and no <To> at all. Reconstruct them from the
            # document body before offering the fix to the user.
            raw_recovered = self._repair_apphdr_agents(raw_recovered)

            recovered = decl + raw_recovered

            # Only return a suggestion when the recovery actually changed
            # something — if the output is identical to the input the XML was
            # already valid enough and no fix is needed here.
            if recovered.strip() == xml.strip():
                return None

            # Validate that the recovered XML is now well-formed.
            try:
                etree.fromstring(recovered.encode("utf-8"))
            except etree.XMLSyntaxError:
                return None  # Recovery produced invalid output — don't suggest it

            # Content-preservation guard: lxml recover=True can COLLAPSE the tree
            # when the break is high up (e.g. an unclosed tag near the root),
            # silently dropping most elements. Never offer a "fix" that deletes a
            # large chunk of the user's data — count opening tags and bail if the
            # recovery lost more than 30% of them.
            def _open_tags(s: str) -> int:
                return len(re.findall(r"<[A-Za-z][\w:.\-]*", s))
            src_n = _open_tags(source)
            rec_n = _open_tags(recovered)
            if src_n and rec_n < src_n * 0.7:
                logger.warning(
                    f"[FixSuggester] XML recovery dropped {src_n - rec_n}/{src_n} "
                    f"elements — declining destructive fix."
                )
                return None

            return _make_suggestion(recovered)

        except Exception as e:
            logger.debug(f"[FixSuggester] XML recovery failed: {e}")
            return None

    # ── apply ─────────────────────────────────────────────────────────────────

    def apply(self, xml: str, xpath: str, fragment_xml: str) -> str:
        # ── Whole-document replacement (xpath="/") ─────────────────────────
        # xpath="/" is produced only by _try_xml_recovery (XML_SYNTAX fixes).
        # fragment_xml IS the complete repaired document — return it directly.
        # We must NOT fall through to the normal parse+find+replace path because:
        #   1. The original XML may be malformed (cannot be parsed at all), and
        #   2. Even when lxml tolerantly parses it, fromstring(fragment_xml) can
        #      fail or produce wrong output when fragment_xml contains an XML
        #      declaration (<?xml …?>) or has a different structure post-repair.
        if xpath == "/" and fragment_xml:
            decl = ""
            dm = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
            if dm and not fragment_xml.lstrip().startswith("<?xml"):
                decl = dm.group(1) + "\n"
            return decl + fragment_xml

        root  = self._parse_xml(xml)
        nsmap = self._build_nsmap(root)

        target = self._find_target(root, xpath, nsmap)
        if target is None:
            raise FixApplyError(f"Element not found for xpath: {xpath}")

        try:
            new_el = etree.fromstring(fragment_xml.encode("utf-8"))
        except etree.XMLSyntaxError as e:
            raise FixApplyError(f"Invalid fragment XML: {e}")

        orig_local = etree.QName(target.tag).localname
        new_local  = etree.QName(new_el.tag).localname
        if orig_local != new_local:
            raise FixApplyError(f"Root tag mismatch: <{orig_local}> vs <{new_local}>")

        # Preserve original namespace on all descendant elements
        target_ns = etree.QName(target.tag).namespace
        if target_ns:
            for desc in new_el.iter():
                if isinstance(desc.tag, str) and "{" not in desc.tag:
                    desc.tag = f"{{{target_ns}}}{desc.tag}"
            new_el.tag = f"{{{target_ns}}}{new_local}"

        parent = target.getparent()
        if parent is None:
            body = self._serialize(new_el)
            decl = ""
            m = re.match(r"(<\?xml[^?]*\?>)", xml.strip())
            if m:
                decl = m.group(1) + "\n"
            return decl + body

        # Preserve the replaced element's tail (the whitespace/newline that
        # follows its closing tag) so the element after it keeps its place on
        # its own line — otherwise the next sibling jams onto the same line.
        if new_el.tail is None:
            new_el.tail = target.tail

        idx = list(parent).index(target)
        parent.remove(target)
        parent.insert(idx, new_el)
        return self._serialize_tree(root, xml)

    def _find_target(self, root: etree._Element, xpath: str,
                     nsmap: dict) -> Optional[etree._Element]:
        """
        Find the target element for xpath using:
        1. Direct lxml xpath (handles indexed paths like /A/B[2]/C)
        2. Fallback: walk the slash-separated path respecting [n] indices
        3. Last resort: first element matching the final tag name
        """
        # 1. Try lxml xpath with namespace map
        if xpath.startswith("/") or "::" in xpath:
            try:
                # Build a namespace-aware xpath: replace bare local-name path with
                # local-name() predicates so it works regardless of namespace prefix.
                res = root.xpath(xpath, namespaces=nsmap)
                if res and isinstance(res[0], etree._Element):
                    return res[0]
            except Exception:
                pass

            # 2. Walk path manually respecting [n] indices
            target = self._walk_indexed_xpath(root, xpath)
            if target is not None:
                return target

        # 3. Last resort: match by final tag name (least precise, avoids wrong element)
        tag = xpath.split("/")[-1].strip()
        # Strip index e.g. "CdtTrfTxInf[2]" → "CdtTrfTxInf", index=2
        idx_m = re.match(r'^(\w+)(?:\[(\d+)\])?$', tag)
        if idx_m:
            tag_name = idx_m.group(1)
            tag_idx  = int(idx_m.group(2)) if idx_m.group(2) else 1
            count = 0
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue  # skip comment / processing-instruction nodes
                if etree.QName(el.tag).localname == tag_name:
                    count += 1
                    if count == tag_idx:
                        return el
        return None

    def _walk_indexed_xpath(self, root: etree._Element, xpath: str) -> Optional[etree._Element]:
        """
        Walk a slash-separated xpath like /Document/FIToFICstmrCdtTrf/CdtTrfTxInf[2]/PmtId
        by local-name, respecting [n] 1-based indices.
        """
        parts = [p for p in xpath.split("/") if p]
        cur = root
        # Skip root tag if it matches the first part
        root_local = etree.QName(root.tag).localname
        idx_m = re.match(r'^(\w+)(?:\[(\d+)\])?$', parts[0])
        start = 1 if idx_m and idx_m.group(1) == root_local else 0

        for part in parts[start:]:
            m = re.match(r'^(\w+)(?:\[(\d+)\])?$', part)
            if not m:
                return None
            tag_name = m.group(1)
            want_idx = int(m.group(2)) if m.group(2) else 1
            count = 0
            found = None
            for child in cur:
                if not isinstance(child.tag, str):
                    continue  # skip comment / processing-instruction nodes
                if etree.QName(child.tag).localname == tag_name:
                    count += 1
                    if count == want_idx:
                        found = child
                        break
            if found is None:
                return None
            cur = found
        return cur

    def apply_batch(self, xml: str, fixes: list[dict]) -> str:
        """
        Apply fixes in the order they were produced by `suggest_batch`. The
        suggester already rolled the document forward between issues, so the
        fragments form a coherent sequence — re-ordering them here would
        reintroduce the same overwrite/stale-target bugs we fixed in the
        suggester.

        If `apply_batch` is called with an externally curated list (the user
        selected only a subset in the UI), it still replays the chosen subset
        in input order, which matches the order they were shown.
        """
        cur = xml
        for fix in fixes:
            xp, frag = fix.get("xpath", ""), fix.get("fragment_xml", "")
            if not xp or not frag:
                continue
            try:
                cur = self.apply(cur, xp, frag)
            except FixApplyError as e:
                logger.warning(f"[FixSuggester] apply_batch skipped {xp}: {e}")
        return cur

    def _serialize_tree(self, root: etree._Element, original_xml: str) -> str:
        decl = ""
        m = re.match(r"(<\?xml[^?]*\?>)", original_xml.strip())
        if m:
            decl = m.group(1) + "\n"
        return decl + etree.tostring(root, encoding="unicode", pretty_print=True)


fix_suggester = FixSuggester()
