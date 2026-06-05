import os
import re
import json
from lxml import etree
from typing import Dict, Any, Tuple, List, Optional
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class Layer3Validator:
    @staticmethod
    def normalize(root_element: etree._Element) -> Tuple[Dict[str, Any], Dict[str, int]]:
        canonical = {}
        line_map = {}
        
        def get_clean_tag(tag):
            return tag.split('}')[-1] if '}' in tag else tag

        def flatten(element, path=""):
            if path:
                line_map[path] = element.sourceline or 1

            # 1. Attributes
            for k, v in element.attrib.items():
                attr_name = get_clean_tag(k)
                attr_path = f"{path}@{attr_name}" if path else f"@{attr_name}"
                canonical[attr_path] = v
                line_map[attr_path] = element.sourceline or 1

            # 2. Text value
            if element.text and element.text.strip():
                canonical[path] = element.text.strip()

            # 3. Children with indexing for repeats
            element_children = [c for c in element if isinstance(c.tag, str)]
            tag_counts = {}
            for child in element_children:
                tag = get_clean_tag(child.tag)
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
            
            current_counts = {}
            for child in element_children:
                tag = get_clean_tag(child.tag)
                if tag_counts[tag] > 1:
                    idx = current_counts.get(tag, 0)
                    indexed_tag = f"{tag}[{idx}]"
                    current_counts[tag] = idx + 1
                else:
                    indexed_tag = tag
                    
                new_path = f"{path}.{indexed_tag}" if path else indexed_tag
                flatten(child, new_path)

        for part in ["AppHdr", "Document"]:
            # Find in element
            node = None
            if get_clean_tag(root_element.tag) == part:
                node = root_element
            else:
                nodes = root_element.xpath(f".//*[local-name()='{part}']")
                if nodes:
                    node = nodes[0]
            if node is not None:
                flatten(node, part)

        if not canonical:
            flatten(root_element, get_clean_tag(root_element.tag))

        return canonical, line_map

    @staticmethod
    def _load_code_set(list_name: str) -> List[str]:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        code_set_path = os.path.normpath(os.path.join(base_dir, "../../rules", f"{list_name}.json"))
        if os.path.exists(code_set_path):
            try:
                with open(code_set_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data.get("codes", [])
                    elif isinstance(data, list):
                        return data
            except Exception:
                pass
        return []

    @staticmethod
    def _load_currency_decimals() -> Dict[str, int]:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        currency_path = os.path.normpath(os.path.join(base_dir, "../../rules/currency.json"))
        if os.path.exists(currency_path):
            try:
                with open(currency_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data.get("currencies", {})
            except Exception:
                pass
        return {}

    @staticmethod
    def validate(canonical_data: Dict[str, Any], line_map: Dict[str, int], message_type: str, report: ValidationReport, supported_bics: set = None):
        if supported_bics is None:
            supported_bics = set()

        # 0. Validate BICFI against official entities database
        for key, value in canonical_data.items():
            if not isinstance(value, str):
                continue
            if key.endswith('.BICFI') or key == 'BICFI' or key.endswith('.AnyBIC') or key == 'AnyBIC':
                bic_val = value.strip().upper()
                if supported_bics and bic_val not in supported_bics:
                    report.add_issue(ValidationIssue(
                        severity="WARNING",
                        code="BIC_NOT_FOUND",
                        path=key,
                        message=f"BIC '{bic_val}' not found in official directory.",
                        line=line_map.get(key, 1),
                        fix="Verify if the BIC is correct or recently decommissioned."
                    ))

        # 1. SWIFT character set check on unstructured remittance fields
        ustrd_keys = [k for k in canonical_data.keys() if k.endswith(".Ustrd")]
        swift_charset = re.compile(r'^[0-9a-zA-Z/\-\?:\(\)\.,\'\+ !#$%&\*=^_`\{\|\}~\x22;<>@\[\\\]]+$')
        for key in ustrd_keys:
            val = canonical_data[key]
            if val and not swift_charset.match(val):
                report.add_issue(ValidationIssue(
                    severity="WARNING",
                    code="SWIFT_CHARSET_VIOLATION",
                    path=key,
                    message=f"Remittance field contains characters outside the permitted SWIFT set.",
                    line=line_map.get(key, 1),
                    fix="Use standard letters, digits, and spaces or allowed special characters."
                ))

        # 2. Currency Decimal Precision and Validation
        currencies = Layer3Validator._load_currency_decimals()
        amt_keys = [k for k in canonical_data.keys() if any(k.endswith(f".{x}") for x in ["Amt", "IntrBkSttlmAmt", "InstdAmt", "ChrgAmt", "EqvtAmt"])]
        for key in amt_keys:
            ccy_key = f"{key}@Ccy"
            if ccy_key in canonical_data:
                ccy = canonical_data[ccy_key]
                if ccy not in currencies:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_CURRENCY",
                        path=ccy_key,
                        message=f"Currency '{ccy}' is not a valid ISO 4217 code.",
                        line=line_map.get(ccy_key, 1),
                        fix="Use a valid 3-letter currency code (e.g. USD, EUR, GBP)."
                    ))
                else:
                    max_decimals = currencies[ccy]
                    val_str = str(canonical_data[key])
                    try:
                        # Check amount greater than zero
                        val_float = float(val_str)
                        if val_float <= 0:
                            report.add_issue(ValidationIssue(
                                severity="ERROR",
                                code="INVALID_AMOUNT",
                                path=key,
                                message="Amount must be greater than zero.",
                                line=line_map.get(key, 1),
                                fix="Provide a positive amount value."
                            ))
                        
                        # Check decimal points
                        if '.' in val_str:
                            decimals = len(val_str.split('.')[1].rstrip('0'))
                            if decimals > max_decimals:
                                report.add_issue(ValidationIssue(
                                    severity="ERROR",
                                    code="INVALID_DECIMAL_PRECISION",
                                    path=key,
                                    message=f"Amount decimal precision exceeds {max_decimals} decimal places allowed for {ccy}.",
                                    line=line_map.get(key, 1),
                                    fix=f"Round the amount to maximum {max_decimals} decimal places."
                                ))

                        # Check for high value transaction warning
                        hv_thresholds = {"USD": 100000, "EUR": 100000, "GBP": 100000, "JPY": 10000000}
                        hv_limit = hv_thresholds.get(ccy)
                        if hv_limit is not None and val_float > hv_limit:
                            report.add_issue(ValidationIssue(
                                severity="WARNING",
                                code="HIGH_VALUE_TRANSACTION",
                                path=key,
                                message=f"Notice: This {ccy} {val_float:,.2f} payment is flagged as HIGH_VALUE_TRANSACTION.",
                                line=line_map.get(key, 1),
                                fix="Risk monitoring is active for this high-value transfer."
                            ))
                    except ValueError:
                        pass

        # 3. Country code validation
        ctry_keys = [k for k in canonical_data.keys() if k.endswith(".Ctry")]
        countries = Layer3Validator._load_code_set("country")
        if countries:
            for key in ctry_keys:
                ctry = canonical_data[key]
                if ctry and ctry not in countries:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_COUNTRY_CODE",
                        path=key,
                        message=f"Country code '{ctry}' is not a valid ISO 3166-1 country code.",
                        line=line_map.get(key, 1),
                        fix="Use a valid 2-letter uppercase ISO country code (e.g. US, GB, DE)."
                    ))

        # 4. Purpose code validation
        purp_keys = [k for k in canonical_data.keys() if k.endswith(".Purp.Cd") or k.endswith(".CtgyPurp.Cd")]
        purposes = Layer3Validator._load_code_set("purpose_code")
        if purposes:
            for key in purp_keys:
                purp = canonical_data[key]
                if purp and purp not in purposes:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_PURPOSE_CODE",
                        path=key,
                        message=f"Purpose code '{purp}' is not a valid ExternalPurpose1Code.",
                        line=line_map.get(key, 1),
                        fix="Use a valid ISO 20022 Purpose Code (e.g. SALA, CORT, PENS)."
                    ))

        # 5. Charge bearer code validation
        chrg_keys = [k for k in canonical_data.keys() if k.endswith(".ChrgBr")]
        charge_bearers = Layer3Validator._load_code_set("charge_bearer")
        if charge_bearers:
            for key in chrg_keys:
                chrg = canonical_data[key]
                if chrg and chrg not in charge_bearers:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_CHARGE_BEARER",
                        path=key,
                        message=f"Charge Bearer code '{chrg}' is not valid.",
                        line=line_map.get(key, 1),
                        fix="Use one of: SHAR, CRED, DEBT, SLEV."
                    ))

        # 6. Sanctions Screening (Fail-fast)
        sanctioned_codes = {'AF', 'RU', 'KP'}
        sanctioned_names = {'russia', 'iran', 'north korea'}
        
        for path, value in canonical_data.items():
            if not isinstance(value, str):
                continue
            str_val = value.lower()
            
            # Check sanctioned country codes
            if 'Ctry' in path and value in sanctioned_codes:
                report.add_issue(ValidationIssue(
                    severity="ERROR", 
                    code="SANCTIONS_BLOCKED", 
                    path=path,
                    message=f"Fail-fast: Party from sanctioned country code '{value}' detected.",
                    line=line_map.get(path, 1),
                    fix="Transaction rejected due to sanctions compliance."
                ))
                
            # Check sanctioned keywords/names anywhere in the payload
            if any(name in str_val for name in sanctioned_names):
                hit = next(name for name in sanctioned_names if name in str_val)
                report.add_issue(ValidationIssue(
                    severity="ERROR", 
                    code="SANCTIONS_BLOCKED", 
                    path=path,
                    message=f"Fail-fast: Party from sanctioned country '{hit.title()}' detected.",
                    line=line_map.get(path, 1),
                    fix="Transaction rejected due to sanctions compliance."
                ))
