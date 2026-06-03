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
        return "0.00"
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
            return _value_for_datatype(dt)
        return None
    return None


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


def _kb_msg_family(msg_type: str) -> str:
    """Reduce 'pacs.008.001.08' → 'pacs.008'."""
    if not msg_type:
        return ""
    parts = msg_type.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else msg_type


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
    if tn.endswith("DtTm"):
        return {"type": "DateTime", "example": "2026-05-27T10:00:00Z"}
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
_TEMPLATES: dict[str, str] = {
    # Identifiers
    "UETR":           "<UETR>f47ac10b-58cc-4372-a567-0e02b2c3d479</UETR>",
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
    "ChrgBr":         "<ChrgBr>SLEV</ChrgBr>",
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
        "<UETR>f47ac10b-58cc-4372-a567-0e02b2c3d479</UETR>"
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
        el.text = "0.00" if "Amount" in type_name else "SMPL"
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

        def _buildable(tag: str) -> bool:
            return bool(_kb_tag_template(tag, msg_type)) or tag in _TEMPLATES

        # 4. Everything we should (re)insert, restricted to those MISSING from
        #    the parent and buildable (so we never inject optional noise like
        #    CharSet, which has no template), ordered by the KB's
        #    tag_insertion_order so they go back in schema sequence.
        #    Implicit mode reinserts the error's expected list UNION the KB's
        #    mandatory children (fix-all from a single error); explicit mode
        #    inserts only the one named field (the validator already enumerated
        #    each missing field as its own issue).
        if explicit_missing:
            candidate_tags = list(exp_candidates)
        else:
            candidate_tags = list(exp_candidates) + self._kb_mandatory_children(parent_local, msg_type)
        wanted: list[str] = []
        for tag in candidate_tags:
            if tag not in wanted and tag not in present and _buildable(tag):
                wanted.append(tag)
        if not wanted:
            return None
        if order:
            wanted.sort(key=lambda t: order.index(t) if t in order else 999)

        # 5. Build each missing element, namespaced to the PARENT (AppHdr lives
        #    in the head.001 namespace, not the Document body namespace).
        parent_ns = etree.QName(parent.tag).namespace or ""
        xsd_path  = self._get_xsd_path(xml)
        tmap      = _XsdTypeMap.get(xsd_path) if xsd_path else None
        rules_idx = _RulesIndex.get(msg_type) if msg_type else None

        parent_copy = self._copy(parent)
        base_cols, unit, close_cols = self._derive_child_indent(parent_copy)

        inserted_any = False
        for tag in wanted:
            child_el = self._build_child(
                tag, fix_hint, parent_ns, tmap,
                existing_parent=parent_copy, rules_idx=rules_idx,
                path_parts=[tag], rule_id=code, root=root, msg_type=msg_type,
            )
            if child_el is None:
                continue
            # Indent the inserted subtree to match the document layout.
            if base_cols is not None:
                self._indent_el(child_el, base_cols, unit)
            insert_idx = self._sibling_insert_index(
                parent_copy, tag, order, found_elem
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

    def _kb_mandatory_children(self, parent_local: str, msg_type: str) -> list[str]:
        """
        Return the DIRECT mandatory child tags of `parent_local` declared in the
        KB's cbpr_plus_rules.mandatory_fields for this message family.

        mandatory_fields lists dotted paths like "AppHdr.Fr" and
        "CdtTrfTxInf.PmtId.UETR"; we keep only the 2-segment paths whose first
        segment is the parent (i.e. the parent's own immediate children).
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
            if len(parts) == 2 and parts[0] == parent_local and parts[1] not in out:
                out.append(parts[1])
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
            codes = _codelist_codes("charge_bearer")
            # Prefer SLEV (most common for CBPR+)
            for preferred in ("SLEV", "SHAR", "CRED", "DEBT"):
                if preferred in codes:
                    return preferred
            return codes[0] if codes else "SLEV"

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
        tag = f"{{{ns}}}{tag_name}" if ns else tag_name
        leaf = etree.Element(tag)
        kb_val = _kb_folder_leaf_value(tag_name, msg_type)
        leaf.text = kb_val if kb_val is not None else self._placeholder(tag_name)
        return leaf

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
            for p in ("SLEV", "SHAR"):
                if p in codes:
                    return p
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

        try:
            root = self._parse_xml(xml)
        except FixApplyError:
            # Invalid XML — only the LLM can help
            return self._llm_fallback("/", xml[:500], code, msg, fix_hint)

        # ── Duplicate element → keep the first valid occurrence, remove extras ─
        # Runs before the ordering/insert guards so a duplicate (e.g. a second
        # <BICFI>, which the schema reports as "not expected here") is removed
        # rather than mis-repaired by inserting the other expected siblings.
        dup_fix = self._try_remove_duplicate(root, code, msg)
        if dup_fix is not None:
            return dup_fix

        # ── Route: missing mandatory AppHdr field (validator completeness scan) ─
        # The header completeness scan emits one clean "/AppHdr/<Tag>" issue per
        # missing mandatory BAH field. The normal missing-child flow can't place
        # these correctly (it would stamp the envelope/Document namespace and,
        # lacking the head.001 XSD order, append at the end). Route them to the
        # namespace-aware, KB-ordered, indented inserter instead.
        _mh = re.search(r"(?:^|/)AppHdr/(\w+)$", path)
        if code == "HEADER_VAL" and _mh:
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
        targets_apphdr = path.replace("/", ".").strip(".").split(".")[:1] == ["AppHdr"]
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

        dep_fix = self._try_dependency_fix(root, path, code, msg, msg_type, tmap)
        if dep_fix is not None:
            return dep_fix

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
            # Build the whole missing subtree bottom-up then nest it
            return self._suggest_missing_subtree(
                anchor_el, missing_chain, missing_tag,
                fix_hint, ns, tmap, code, msg,
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

        child_el = self._build_child(missing_tag, fix_hint, ns, tmap,
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

        # 0. Length overflow only → truncate the EXISTING value to the KB/schema
        #    max_length. This preserves the user's actual data (just shortened to
        #    the limit) instead of replacing it with a generic example, and is the
        #    correct fix for "Field X has an invalid length" per the KB rules.
        cur_txt = (el.text or "").strip()
        max_len = constraint.get("max_length") if isinstance(constraint, dict) else None
        if cur_txt and isinstance(max_len, int) and len(cur_txt) > max_len:
            truncated = cur_txt[:max_len].rstrip()
            # Only accept the truncation if it actually resolves the violation
            # (i.e. length was the SOLE problem — not illegal chars / wrong type).
            if truncated and not self._violates_constraint(truncated, constraint):
                return truncated

        # 1. Cross-field harvesting via KB equals dependencies
        if root is not None:
            cross_val = self._harvest_dependency_partner(root, my_xpath, tag_name, constraint)
            if cross_val:
                return cross_val

        # 2. Harvest same-tag from elsewhere in the doc
        if root is not None:
            for other in root.iter():
                if not isinstance(other.tag, str):
                    continue
                if (etree.QName(other.tag).localname == tag_name
                    and other is not el
                    and other.text):
                    txt = other.text.strip()
                    if txt and not self._violates_constraint(txt, constraint):
                        return txt

        # 2. Tag-specific generators
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
            return self._repair_country(el, cur_txt, root)
        if ctype == "Amount":
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
        if not re.search(r"is not expected here|following element|missing before", msg, re.I):
            return None
        m_off = re.search(r"element '([^']+)' is not expected here", msg, re.I)
        # Tolerate "expected:", "expected :" and surrounding quotes/spaces.
        m_exp = re.search(r"following elements?\s*(?:is|are)?\s*expected\s*:?\s*'?([^'\n]+?)'?\.?\s*$",
                          msg, re.I)
        if not m_off or not m_exp:
            return None
        offending = m_off.group(1).strip().strip("':\" ")
        expected = [e.strip().strip("':\" ")
                    for e in re.split(r"[,/]| or ", m_exp.group(1))
                    if e.strip().strip("':\" ")]
        if not expected:
            return None

        off_el = next((el for el in root.iter()
                       if isinstance(el.tag, str)
                       and etree.QName(el.tag).localname == offending), None)
        if off_el is None:
            return None
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
        #    reorder to the XSD sequence. ──────────────────────────────────────
        if offending not in expected:
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

        to_add = [e for e in expected if e not in existing and is_mandatory(e)]
        if not to_add:
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
            # Find any valid Ccy attribute in the document
            root = el.getroottree().getroot() if el.getroottree() is not None else None
            if root is not None:
                valid_currencies = _codelist_codes("currency")
                if not valid_currencies:
                    valid_currencies = _kb_get("dummy_data.currencies_by_country", {})
                    if isinstance(valid_currencies, dict):
                        valid_currencies = list(valid_currencies.values())
                # Walk the doc for any element with a valid @Ccy
                for sib in root.iter():
                    if not isinstance(sib.tag, str):
                        continue
                    sib_ccy = sib.get("Ccy")
                    if sib_ccy and sib_ccy != el.get("Ccy"):
                        # Validate against currency codelist (if available)
                        if not valid_currencies or sib_ccy in valid_currencies:
                            new_value = sib_ccy
                            break
            if new_value is None:
                new_value = "USD"  # safe default
        else:
            constraint = _kb_field_constraint(attr_name)
            if isinstance(constraint, dict):
                new_value = constraint.get("preferred") or constraint.get("example")
            if not new_value:
                new_value = self._placeholder(attr_name)

        el_copy.set(attr_name, str(new_value))
        return FixSuggestion(xpath, original_fragment,
                              self._serialize(el_copy), code, msg, "high")

    # ── _fix_value ────────────────────────────────────────────────────────────

    def _fix_value(self, el: etree._Element, code: str, msg: str,
                   fix_hint: str, _ns: str = "") -> FixSuggestion:
        """Fix the text value of an existing element."""
        original_fragment = self._serialize(el)
        xpath             = self._xpath_of(el)
        msg_l             = msg.lower()
        el_local          = etree.QName(el.tag).localname

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
        # Highest-priority, deterministic fix for "Field X has an invalid length"
        # errors (e.g. <Nm> with 162 chars when Max140Text allows 140). We keep
        # the user's own text and simply trim it to the allowed length so they
        # can accept or further edit it — exactly what the rule expects.
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
            if el_local in rule.get("affects_tags", []):
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
                    txt = (el.text or "")
                    new_val = txt if ("+" in txt or "-" in txt[-6:]) else txt + "+00:00"
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
                return FixSuggestion(xpath, original_fragment, original_fragment,
                                     code, msg, "low")

        # ── Disallowed / duplicate → remove the offending child ──────────────
        if code == "DUPLICATE_TAG" or "duplicate" in msg_l:
            m = re.search(r"<(\w+)>", msg)
            if m:
                el_copy = self._copy(el)
                seen = False
                for child in list(el_copy):
                    if isinstance(child.tag, str) and etree.QName(child.tag).localname == m.group(1):
                        if seen:
                            el_copy.remove(child)
                        else:
                            seen = True
                return FixSuggestion(xpath, original_fragment, self._serialize(el_copy), code, msg, "high")

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

        # Fall through to LLM
        return self._llm_fallback(xpath, original_fragment, code, msg, fix_hint)

    def _llm_fallback(self, xpath: str, original_fragment: str,
                      code: str, msg: str, fix_hint: str = "") -> FixSuggestion:
        """Last-resort LLM call with rich context. max_tokens=400, temperature=0."""
        # Build context: include rule hint, codelists, field constraints, deps
        context_lines = []

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

        context = "\n".join(context_lines)
        system = (
            "You are an ISO 20022 / CBPR+ XML expert. "
            "Return ONLY the corrected XML element — same root tag and namespace, "
            "no prose, no markdown fences. "
            "The fix must be a valid well-formed XML fragment."
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
            sug = self.suggest(current_xml, issue)

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

    # ── apply ─────────────────────────────────────────────────────────────────

    def apply(self, xml: str, xpath: str, fragment_xml: str) -> str:
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
