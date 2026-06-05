"""
SR2026 Rule Registry
====================
Production-ready centralized rule loader for SR2026 pacs.008 CBPR+.

Loads the comprehensive pacs.008.sr2026.json rule definition and exposes
typed accessors for each category:
  - headerRules
  - crossFieldRules
  - xmlRules
  - mandatoryFields
  - addressRules
  - proxyRules
  - serviceLevelRules
  - dateRules
  - validationRules
  - validationSeverity
  - xmlGenerationRules
  - uiRules
  - changesSincesr2025
"""

import json
import os
from typing import Any, Dict, List, Optional


_RULES_FILE = os.path.join(os.path.dirname(__file__), "messages", "pacs.008", "base.json")
_EXTENDED_RULES_FILE = os.path.join(os.path.dirname(__file__), "messages", "pacs.008", "extended.json")

_cache: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    """Load and cache the SR2026 rule definition JSON, and merge extended rules."""
    global _cache
    if _cache is None:
        if not os.path.exists(_RULES_FILE):
            raise FileNotFoundError(
                f"SR2026 rule definition not found at: {_RULES_FILE}"
            )
        with open(_RULES_FILE, "r", encoding="utf-8") as fh:
            _cache = json.load(fh)
            
        # Merge extended critical rules
        if os.path.exists(_EXTENDED_RULES_FILE):
            with open(_EXTENDED_RULES_FILE, "r", encoding="utf-8") as fh_ext:
                ext_data = json.load(fh_ext)
                ext_rules = ext_data.get("validationRules", {}).get("rules", [])
                if ext_rules:
                    base_rules = _cache.setdefault("validationRules", {}).setdefault("rules", [])
                    base_rules.extend(ext_rules)
                    
    return _cache


# ─── Public typed accessors ────────────────────────────────────────────────

def get_all_rules() -> Dict[str, Any]:
    """Return the full rule definition dictionary."""
    return _load()


def get_rule_engine_meta() -> Dict[str, Any]:
    """Meta-information for the rule engine execution."""
    return _load().get("ruleEngineMeta", {})


def get_header_rules() -> Dict[str, Any]:
    """Rules for BAH (Business Application Header) fields."""
    return _load().get("headerRules", {})


def get_cross_field_rules() -> Dict[str, Any]:
    """Cross-field consistency rules (BizMsgIdr == MsgId, BIC matching, etc.)."""
    return _load().get("crossFieldRules", {})


def get_xml_rules() -> Dict[str, Any]:
    """XML namespace and schema rules."""
    return _load().get("xmlRules", {})


def get_mandatory_fields() -> Dict[str, Any]:
    """Mandatory field definitions grouped by block (groupHeader, transactionBlock)."""
    return _load().get("mandatoryFields", {})


def get_mandatory_group_header_fields() -> Dict[str, Any]:
    """Mandatory fields inside the Group Header block."""
    return _load().get("mandatoryFields", {}).get("groupHeader", {}).get("fields", {})


def get_mandatory_transaction_fields() -> Dict[str, Any]:
    """Mandatory fields inside each CdtTrfTxInf transaction block."""
    return _load().get("mandatoryFields", {}).get("transactionBlock", {}).get("fields", {})


def get_address_rules() -> Dict[str, Any]:
    """Address validation rules — AdrLine deprecation, TwnNm/Ctry requirements."""
    return _load().get("addressRules", {})


def get_proxy_rules() -> Dict[str, Any]:
    """Proxy rules — Tp mandatory when Prxy present."""
    return _load().get("proxyRules", {})


def get_service_level_rules() -> Dict[str, Any]:
    """GPI Service Level allowed/disallowed code lists."""
    return _load().get("serviceLevelRules", {})


def get_date_rules() -> Dict[str, Any]:
    """Date format rules — CBPR_Date (YYYY-MM-DD, no timezone)."""
    return _load().get("dateRules", {})


def get_validation_rules() -> List[Dict[str, Any]]:
    """Flat list of all named validation rules (SR2026_R001 … SR2026_R016+)."""
    return _load().get("validationRules", {}).get("rules", [])


def get_validation_severity() -> Dict[str, str]:
    """Severity map for each validation category."""
    return _load().get("validationSeverity", {})


def get_xml_generation_rules() -> Dict[str, Any]:
    """Default values to use when generating SR2026-compliant XML."""
    return _load().get("xmlGenerationRules", {}).get("defaults", {})


def get_ui_rules() -> Dict[str, Any]:
    """UI-layer field visibility / mandatory hints for the generator form."""
    return _load().get("uiRules", {}).get("fields", {})


def get_changes_since_sr2025() -> List[Dict[str, Any]]:
    """Delta summary — what changed from SR2025 to SR2026."""
    return _load().get("changesSinceSR2025", {}).get("changes", [])


# ─── Convenience helpers ───────────────────────────────────────────────────

def get_biz_svc_value() -> str:
    """Fixed BizSvc value for SR2026 pacs.008."""
    return _load().get("headerRules", {}).get("bizSvc", {}).get("fixedValue", "swift.cbprplus.04")


def get_msg_def_idr_value() -> str:
    """Fixed MsgDefIdr value for SR2026 pacs.008."""
    return _load().get("headerRules", {}).get("msgDefIdr", {}).get("fixedValue", "pacs.008.001.08")


def get_gpi_allowed_codes() -> List[str]:
    """GPI service level codes allowed in SR2026."""
    return _load().get("serviceLevelRules", {}).get("gpiAllowedCodes", ["G001"])


def get_gpi_disallowed_codes() -> List[str]:
    """GPI service level codes disallowed in SR2026."""
    return _load().get("serviceLevelRules", {}).get("gpiDisallowedCodes", [])


def get_biz_msg_idr_regex() -> str:
    """Regex pattern for RestrictedFINXMax35Text (BizMsgIdr in SR2026)."""
    return _load().get("headerRules", {}).get("bizMsgIdr", {}).get("regex", r"^[0-9a-zA-Z/\-\?:\(\)\.,'\+ ]+$")


def is_adr_line_deprecated() -> bool:
    """True — AdrLine is deprecated in SR2026 (grace periods all inactive)."""
    addr = _load().get("addressRules", {})
    gp = addr.get("gracePeriodRules", {})
    return not any(v.get("active", False) for v in gp.values())


def get_date_regex() -> str:
    """Regex for validating CBPR_Date format in SR2026."""
    return _load().get("dateRules", {}).get("regex", r"^\d{4}-\d{2}-\d{2}$")


def get_rule_by_id(rule_id: str) -> Optional[Dict[str, Any]]:
    """Look up a named validation rule by its 'id' field (e.g. 'SR2026_R004')."""
    for rule in get_validation_rules():
        if rule.get("id") == rule_id:
            return rule
    return None


def get_rules_by_category(category: str) -> List[Dict[str, Any]]:
    """Return all validation rules belonging to a given category."""
    return [r for r in get_validation_rules() if r.get("category") == category]
