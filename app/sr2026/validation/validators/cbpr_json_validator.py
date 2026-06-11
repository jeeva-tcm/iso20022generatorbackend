"""
CBPR+ JSON Schema Validator (Layer 4)
=====================================
Runs alongside Layer 1/2/3. Loads the CBPR+ MyStandards JSON schema for the detected
message type, extracts every leaf-type constraint (pattern, maxLength, minLength,
enum), then walks the XML and validates each leaf element's text against the
constraint for its ISO 20022 tag.

The XML→type mapping is by tag name (consistent across all ISO 20022 messages).
We don't reconstruct the full structural JSON shape — we apply the type-level
rules where they bite: BICFI, IBAN, MmbId, identifiers, addresses, names, etc.

Drop new MyStandards JSON files into app/resources/cbpr_json_schemas/ and they
get auto-discovered by message type substring.
"""
import os
import re
import json
import glob
from typing import Dict, Optional, Tuple
from lxml import etree

from .models import ValidationIssue, ValidationReport


# ISO 20022 leaf-element tag → expected JSON-schema type definition.
# These mappings are stable across every CBPR+/ISO 20022 message family.
# When a tag carries a specific semantic type (BIC, LEI, UETR, etc.) the JSON
# schema always references the same definition name.
TAG_TO_REF: Dict[str, str] = {
    # Identifiers
    "BICFI": "BICFIDec2014Identifier",
    "AnyBIC": "AnyBICDec2014Identifier",
    "LEI": "LEIIdentifier",
    "UETR": "UUIDv4Identifier",
    "IBAN": "IBAN2007Identifier",
    "Ctry": "CountryCode",
    "CtryOfRes": "CountryCode",
    "CtryOfBirth": "CountryCode",
    "CtryOfBrth": "CountryCode",
    # Generic max-N text identifiers
    "MsgId": "CBPR_RestrictedFINXMax35Text",
    "BizMsgIdr": "CBPR_RestrictedFINXMax35Text",
    "InstrId": "CBPR_RestrictedFINXMax35Text",
    "EndToEndId": "CBPR_RestrictedFINXMax35Text",
    "TxId": "CBPR_RestrictedFINXMax35Text",
    "OrgnlMsgId": "CBPR_RestrictedFINXMax35Text",
    "OrgnlEndToEndId": "CBPR_RestrictedFINXMax35Text",
    "OrgnlInstrId": "CBPR_RestrictedFINXMax35Text",
    "OrgnlTxId": "CBPR_RestrictedFINXMax35Text",
    "ClrSysRef": "CBPR_RestrictedFINXMax35Text",
    "MndtId": "CBPR_RestrictedFINXMax35Text",
    "Issr": "CBPR_RestrictedFINXMax35Text",
    # ClrSysMmbId/MmbId  — schema caps at 28
    "MmbId": "CBPR_RestrictedFINXMax28Text",
    # Account identifier
    # (only when used inside Othr/Id, IBAN already separate)
    # Codes
    "Cd": None,  # Cd is polymorphic — too ambiguous to enforce without parent context
    "Prtry": "CBPR_RestrictedFINXMax35Text",
    # Names
    "Nm": "CBPR_RestrictedFINXMax140Text_Extended",
    # Postal address fields
    "AdrLine": "CBPR_RestrictedFINXMax70Text_Extended",
    "StrtNm": "CBPR_RestrictedFINXMax70Text_Extended",
    "Dept": "CBPR_RestrictedFINXMax70Text_Extended",
    "SubDept": "CBPR_RestrictedFINXMax70Text_Extended",
    "Flr": "CBPR_RestrictedFINXMax70Text_Extended",
    "Room": "CBPR_RestrictedFINXMax70Text_Extended",
    "BldgNm": "CBPR_RestrictedFINXMax35Text_Extended",
    "TwnNm": "CBPR_RestrictedFINXMax35Text_Extended",
    "TwnLctnNm": "CBPR_RestrictedFINXMax35Text_Extended",
    "DstrctNm": "CBPR_RestrictedFINXMax35Text_Extended",
    "CtrySubDvsn": "CBPR_RestrictedFINXMax35Text_Extended",
    "BldgNb": "CBPR_RestrictedFINXMax16Text_Extended",
    "PstBx": "CBPR_RestrictedFINXMax16Text_Extended",
    "PstCd": "CBPR_RestrictedFINXMax16Text_Extended",
    # Currency (also lives on @Ccy attribute, handled separately)
    "Ccy": "ActiveOrHistoricCurrencyCode",
    # Date / DateTime
    "CreDtTm": "CBPR_DateTime",
    "IntrBkSttlmDt": "ISODate",
    "SttlmDt": "ISODate",
    "ReqdExctnDt": "ISODate",
    # Remittance free-text
    "Ustrd": "CBPR_RestrictedFINXMax140Text_Extended",
    "AddtlRmtInf": "CBPR_RestrictedFINXMax140Text_Extended",
    # Instructions
    "InstrInf": "CBPR_RestrictedFINXMax140Text_Extended",
}


def _extract_catalog(schema: dict) -> Dict[str, dict]:
    """Walk schema.definitions and surface every primitive type's constraints."""
    catalog: Dict[str, dict] = {}
    defs = schema.get("definitions", {})
    for name, d in defs.items():
        # We only want primitive leaf types (strings with patterns / enums)
        if d.get("type") in ("string", "number", "integer") or "enum" in d:
            catalog[name] = {
                "pattern": d.get("pattern"),
                "minLength": d.get("minLength"),
                "maxLength": d.get("maxLength"),
                "enum": d.get("enum"),
                "type": d.get("type"),
                "description": (d.get("description") or "")[:120],
            }
    return catalog


def _local_name(tag: str) -> str:
    """Strip namespace from an lxml tag like '{urn:...}MsgId' → 'MsgId'."""
    return tag.split("}")[-1] if "}" in tag else tag


class CBPRJsonValidator:
    def __init__(self):
        self._cbpr_schema_cache: Dict[str, Tuple[Dict[str, dict], str]] = {}
    """Mixin added to ISOValidator. Provides one entry point: _run_cbpr_json_schema_check."""

    # Cache: message_type → (catalog, schema_filename) so we only parse each file once.
    _cbpr_schema_cache: Dict[str, Tuple[Dict[str, dict], str]] = {}

    def _cbpr_schemas_dir(self) -> str:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(base_dir, "..", "..", "..", "sr2025", "resources", "cbpr_json_schemas"))

    def _find_cbpr_schema_for(self, message_type: str) -> Optional[str]:
        """Locate a JSON schema file matching the message type.

        Examples of expected inputs and what they should resolve to:
          'pacs.008.001.08'        -> ...pacs_008_001_08_FIToFI...
          'pacs.009.001.08'        -> ...pacs_009_001_08_Financial...  (core, NOT _ADV/_COV)
          'pacs.009.001.08_ADV'    -> ...pacs_009_001_08_ADV_...
          'pacs.009.cov'           -> ...pacs_009_001_08_COV_...
          'camt.057.001.06'        -> ...camt_057_001_06_Notification...
        """
        if not message_type or message_type == "Unknown":
            return None
        d = self._cbpr_schemas_dir()
        if not os.path.isdir(d):
            return None

        msg = message_type.lower()
        # Extract variant (cov/adv) if present anywhere in the type string.
        variant = None
        if re.search(r'(^|[\._])cov($|[\._])', msg):
            variant = "cov"
        elif re.search(r'(^|[\._])adv($|[\._])', msg):
            variant = "adv"

        # Strip variant tokens to get the bare family/version (e.g. 'pacs_009_001_08').
        bare = re.sub(r'[\._](cov|adv)([\._]|$)', '_', msg)  # remove embedded variant
        bare = re.sub(r'[\._](cov|adv)$', '', bare)          # remove trailing variant
        bare = bare.replace(".", "_")

        # If the bare path has no version digits (e.g. 'pacs_009'), try to expand
        # using the first matching file's full identifier. Otherwise use as-is.
        candidates = sorted(glob.glob(os.path.join(d, "*.json")))
        if not candidates:
            return None

        # Helper: file matches a needle if needle appears as a contiguous token.
        def file_contains(path: str, needle: str) -> bool:
            return needle in os.path.basename(path).lower()

        # Pass 1: variant-aware exact match. Need both the family token AND the variant token.
        if variant:
            for path in candidates:
                name = os.path.basename(path).lower()
                if bare in name and f"_{variant}_" in name:
                    return path
            # If no variant-specific file exists, fall back to core.

        # Pass 2: core match — pick a file containing the family token but NEITHER variant token.
        for path in candidates:
            name = os.path.basename(path).lower()
            if bare in name and "_cov_" not in name and "_adv_" not in name:
                return path

        # Pass 3: last-resort substring match on the family token alone.
        for path in candidates:
            if file_contains(path, bare):
                return path
        return None

    def _load_cbpr_catalog(self, message_type: str) -> Optional[Dict[str, dict]]:
        if message_type in self._cbpr_schema_cache:
            return self._cbpr_schema_cache[message_type][0]
        path = self._find_cbpr_schema_for(message_type)
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            print(f"[CBPR JSON] Failed to load {path}: {e}")
            return None
        catalog = _extract_catalog(schema)
        self._cbpr_schema_cache[message_type] = (catalog, os.path.basename(path))
        return catalog

    def _run_cbpr_json_schema_check(
        self,
        xml_content: str,
        report: ValidationReport,
        message_type: str,
    ) -> None:
        """Layer 4: type-level CBPR+ JSON Schema check. Adds ValidationIssues to report."""
        catalog = self._load_cbpr_catalog(message_type)
        if not catalog:
            # No schema available for this message type — silently skip.
            return

        try:
            parser = etree.XMLParser(remove_blank_text=False)
            root = etree.fromstring(xml_content.encode("utf-8"), parser=parser)
        except Exception:
            # Layer 1 would have already reported any parse error; don't double-report.
            return

        seen = set()  # de-dupe (code, path, message-prefix) to avoid noise

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            local = _local_name(elem.tag)
            text = (elem.text or "").strip()

            # --- 1. Tag-based leaf type check ---
            ref = TAG_TO_REF.get(local)
            if local.startswith("AdrLine"):
                ref = "CBPR_RestrictedFINXMax70Text_Extended"

            if ref and text:
                rule = catalog.get(ref)
                if rule:
                    issue = self._cbpr_check_value(local, text, rule, ref)
                    if issue:
                        key = (issue.code, issue.path, issue.message[:60])
                        if key not in seen:
                            seen.add(key)
                            report.add_issue(issue)

            # --- 2. Attribute checks: Ccy attribute on amount elements ---
            ccy = elem.get("Ccy")
            if ccy:
                rule = catalog.get("ActiveOrHistoricCurrencyCode")
                if rule and rule.get("pattern") and not re.match(rule["pattern"], ccy):
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        layer=3,
                        code="CBPR_JSON_CCY_PATTERN",
                        path=f"{local}/@Ccy",
                        message=f"@Ccy '{ccy}' does not match CBPR+ ActiveOrHistoricCurrencyCode pattern (ISO 4217 3-letter).",
                        fix_suggestion="Use a valid ISO 4217 currency code (e.g. USD, EUR).",
                    ))
        # No separate layer_status entry — Layer 3's existing status absorbs these issues.

    @staticmethod
    def _cbpr_check_value(
        local: str,
        value: str,
        rule: dict,
        ref: str,
    ) -> Optional[ValidationIssue]:
        """Check a single value against a single rule. Returns one ValidationIssue or None."""
        # maxLength
        max_len = rule.get("maxLength")
        if max_len is not None and len(value) > max_len:
            return ValidationIssue(
                severity="ERROR",
                layer=3,
                code="CBPR_JSON_MAX_LENGTH",
                path=local,
                message=f"<{local}> length {len(value)} exceeds CBPR+ '{ref}' max of {max_len}.",
                fix_suggestion=f"Shorten the value to at most {max_len} characters.",
            )
        # minLength
        min_len = rule.get("minLength")
        if min_len is not None and len(value) < min_len:
            return ValidationIssue(
                severity="ERROR",
                layer=3,
                code="CBPR_JSON_MIN_LENGTH",
                path=local,
                message=f"<{local}> length {len(value)} below CBPR+ '{ref}' minimum of {min_len}.",
                fix_suggestion=f"Provide at least {min_len} characters.",
            )
        # pattern
        pat = rule.get("pattern")
        if pat:
            try:
                if not re.match(pat, value):
                    return ValidationIssue(
                        severity="ERROR",
                        layer=3,
                        code="CBPR_JSON_PATTERN",
                        path=local,
                        message=f"<{local}> value '{value[:40]}…' does not match CBPR+ '{ref}' pattern.",
                        fix_suggestion=(rule.get("description") or "Refer to the CBPR+ MyStandards definition for the allowed character set.")[:200],
                    )
            except re.error:
                # Malformed pattern in schema — skip silently
                return None
        # enum
        enum_vals = rule.get("enum")
        if enum_vals and value not in enum_vals:
            return ValidationIssue(
                severity="ERROR",
                layer=3,
                code="CBPR_JSON_ENUM",
                path=local,
                message=f"<{local}> value '{value}' is not one of CBPR+ '{ref}' allowed values: {enum_vals}.",
                fix_suggestion=f"Use one of: {', '.join(enum_vals)}.",
            )
        return None
