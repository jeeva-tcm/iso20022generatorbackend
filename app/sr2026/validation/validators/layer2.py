import os
import re
from lxml import etree
from typing import Optional, Dict, Tuple
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class Layer2Validator:
    _schema_cache: Dict[str, etree.XMLSchema] = {}

    # Mandatory direct children of <AppHdr> per head.001.001.02 (BusinessApplicationHeaderV02).
    # libxml2's schema validator stops at the first sequence violation, so when several of
    # these are missing at once it only ever reports the first one. We check completeness
    # ourselves so every missing mandatory element is reported.
    _APPHDR_MANDATORY_ELEMENTS = ["Fr", "To", "BizMsgIdr", "MsgDefIdr", "CreDt"]

    @staticmethod
    def _resolve_xsd_path(message_type: str) -> Optional[str]:
        # Path configuration
        base_dir = os.path.dirname(os.path.abspath(__file__))
        xsds_dir = os.path.normpath(os.path.join(base_dir, "../../xsds"))
        
        if not os.path.exists(xsds_dir):
            return None

        # Clean message type
        msg_type_clean = message_type.strip().lower()
        
        # 1. Gather all files in the directory
        files = os.listdir(xsds_dir)
        xsd_files = [f for f in files if f.endswith('.xsd')]

        # Helper mapping for specific message types
        # Input message types to unique search terms
        match_term_alt = None
        
        # Determine base type first (e.g., pacs.008, pacs.009)
        if "pacs.008" in msg_type_clean:
            if "stp" in msg_type_clean:
                match_term = "pacs_008_001_08_stp"
            else:
                match_term = "pacs_008_001_08_fito"
                
        elif "pacs.009" in msg_type_clean:
            if "cov" in msg_type_clean:
                match_term = "pacs_009_001_08_cov"
            elif "adv" in msg_type_clean:
                match_term = "pacs_009_001_08_adv"
            else:
                match_term = "pacs_009_001_08_financial"
                
        elif "camt.105" in msg_type_clean:
            if "multiple" in msg_type_clean:
                match_term = "camt_105_001_03_chargespaymentnotification_multiple"
            else:
                match_term = "camt_105_001_03_chargespaymentnotification_20"
                
        elif "camt.106" in msg_type_clean:
            if "multiple" in msg_type_clean:
                match_term = "camt_106_001_03_chargespaymentrequest_multiple"
            else:
                match_term = "camt_106_001_03_chargespaymentrequest_20"
                
        else:
            # General search by replacing dot with underscore (e.g. camt.053 -> camt_053)
            parts = msg_type_clean.split('.')
            match_term = "_".join(parts[:2]) if len(parts) >= 2 else msg_type_clean
            match_term_alt = ".".join(parts[:2]) if len(parts) >= 2 else msg_type_clean

        for f in xsd_files:
            f_clean = f.lower()
            if match_term in f_clean or (match_term_alt and match_term_alt in f_clean):
                return os.path.join(xsds_dir, f)

        # Fallback to general prefix matching
        parts = msg_type_clean.split('.')
        family_prefix = "_".join(parts[:2]) if len(parts) >= 2 else msg_type_clean
        family_prefix_alt = ".".join(parts[:2]) if len(parts) >= 2 else msg_type_clean
        for f in xsd_files:
            if family_prefix.lower() in f.lower() or family_prefix_alt.lower() in f.lower():
                return os.path.join(xsds_dir, f)

        return None

    @staticmethod
    def _get_schema(xsd_path: str) -> etree.XMLSchema:
        if xsd_path not in Layer2Validator._schema_cache:
            with open(xsd_path, "rb") as _f:
                _raw = _f.read().lstrip()
            xsd_doc = etree.fromstring(_raw)
            schema = etree.XMLSchema(xsd_doc)
            Layer2Validator._schema_cache[xsd_path] = schema
        return Layer2Validator._schema_cache[xsd_path]

    @staticmethod
    def _validate_apphdr(root_element: etree._Element, report: ValidationReport) -> bool:
        """Validate the <AppHdr> element against head.001.001.02.xsd.
        Returns True if valid, False if XSD errors found or if AppHdr is missing."""
        apphdr_nodes = root_element.xpath("//*[local-name()='AppHdr']")
        if not apphdr_nodes:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                layer=2,
                code="APPHDR_SCHEMA_ERROR",
                path="/AppHdr",
                message="AppHdr XSD Validation Error: Element 'AppHdr': This element is missing. Expected is ( AppHdr ).",
                line=1,
                fix="Ensure the <AppHdr> block is present in the XML envelope."
            ))
            return False

        base_dir = os.path.dirname(os.path.abspath(__file__))
        hdr_xsd_path = os.path.normpath(
            os.path.join(base_dir, "../../xsds/head.001.001.02.xsd")
        )
        if not os.path.exists(hdr_xsd_path):
            return True  # no header XSD available — skip silently

        apphdr_node = apphdr_nodes[0]

        # Completeness check: report EVERY missing mandatory element, not just the
        # first one libxml2's sequence validator would stop on.
        actual_children = {
            etree.QName(c).localname for c in apphdr_node if isinstance(c.tag, str)
        }
        missing_elements = [
            name for name in Layer2Validator._APPHDR_MANDATORY_ELEMENTS
            if name not in actual_children
        ]
        if missing_elements:
            for name in missing_elements:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    layer=2,
                    code="APPHDR_SCHEMA_ERROR",
                    path=f"//AppHdr/{name}",
                    message=f"AppHdr XSD Validation Error: Element '{name}' is missing from AppHdr.",
                    line=apphdr_node.sourceline or 1,
                    fix=f"Add the mandatory <{name}> element to AppHdr."
                ))
            return False

        try:
            schema = Layer2Validator._get_schema(hdr_xsd_path)
            xsd_ns = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"
            xml_ns = etree.QName(apphdr_node).namespace or ""

            import copy
            validation_doc = copy.deepcopy(apphdr_node)
            if xml_ns != xsd_ns:
                Layer2Validator._mask_namespace_in_place(validation_doc, xsd_ns)

            schema.assertValid(validation_doc)
            return True
        except etree.DocumentInvalid as e:
            import re
            for error in e.error_log:
                clean_msg = re.sub(r'\{[^\}]+\}', '', error.message)

                # "Element 'X'[: ]is not expected. Expected is [one of] ( A, B )."
                # → pick the first mandatory candidate (skip optional CharSet), rewrite as
                #   "Element 'Y' is missing from AppHdr (found 'X' at this position)."
                not_expected_match = re.search(
                    r"Element '([^']+)'[:\s]+[Tt]his element is not expected.*?Expected is(?:\s+one of)?\s*\(\s*([^\)]+?)\s*\)",
                    clean_msg
                )
                if not_expected_match:
                    found_tag = not_expected_match.group(1).split('}')[-1]
                    candidates = [c.strip().split('}')[-1] for c in not_expected_match.group(2).split(',')]
                    # prefer mandatory fields (skip CharSet which is optional)
                    optional_known = {'CharSet', 'MktPrctc', 'BizPrcgDt', 'CpyDplct', 'PssblDplct', 'Prty', 'Sgntr', 'Rltd'}
                    mandatory_candidates = [c for c in candidates if c not in optional_known]
                    missing_tag = mandatory_candidates[0] if mandatory_candidates else candidates[0]
                    clean_msg = f"Element '{missing_tag}' is missing from AppHdr (found '{found_tag}' at this position)."
                    path_str = f"//AppHdr/{missing_tag}"
                    fix_str = f"Add the mandatory <{missing_tag}> element before <{found_tag}> in AppHdr."
                else:
                    elem_match = re.search(r"Element '([^']+)'", clean_msg)
                    path_str = "//AppHdr"
                    if elem_match:
                        tag_name = elem_match.group(1).split('}')[-1]
                        path_str = f"//AppHdr/{tag_name}"
                    fix_str = "Verify the AppHdr element structure against head.001.001.02 schema."

                # "The content of element 'AppHdr' is not complete. Expected is [one of] ( X )."
                # → rewrite as "Element 'X' is missing from AppHdr."
                incomplete_match = re.search(
                    r"content of element 'AppHdr' is not complete.*?Expected is(?:\s+one of)?\s*\(\s*([^\)]+?)\s*\)",
                    clean_msg
                )
                if incomplete_match:
                    candidates = [c.strip().split('}')[-1] for c in incomplete_match.group(1).split(',')]
                    optional_known = {'CharSet', 'MktPrctc', 'BizPrcgDt', 'CpyDplct', 'PssblDplct', 'Prty', 'Sgntr', 'Rltd'}
                    mandatory_candidates = [c for c in candidates if c not in optional_known]
                    missing_tag = mandatory_candidates[0] if mandatory_candidates else candidates[0]
                    clean_msg = f"Element '{missing_tag}' is missing from AppHdr."
                    path_str = f"//AppHdr/{missing_tag}"
                    fix_str = f"Add the mandatory <{missing_tag}> element to AppHdr."

                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    layer=2,
                    code="APPHDR_SCHEMA_ERROR",
                    path=path_str,
                    message=f"AppHdr XSD Validation Error: {clean_msg}",
                    line=error.line,
                    fix=fix_str
                ))
            return False
        except Exception:
            return True  # non-fatal — don't block the pipeline

    @staticmethod
    def validate(root_element: etree._Element, message_type: str, report: ValidationReport):
        """Returns True (passed), False (XSD errors - L3 should still run), or None (catastrophic - stop pipeline)."""
        # Always validate AppHdr against head.001.001.02.xsd
        apphdr_passed = Layer2Validator._validate_apphdr(root_element, report)

        xsd_path = Layer2Validator._resolve_xsd_path(message_type)
        if not xsd_path:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="SCHEMA_NOT_FOUND",
                path="/",
                message=f"SR2026 schema validation template not found for message type '{message_type}'.",
                line=1,
                fix="Verify the messageType and try again."
            ))
            return None  # catastrophic - no schema to validate against

        # Extract target payload node (<Document> or <BusMsg>)
        target_node = root_element.xpath("//*[local-name()='Document' or local-name()='BusMsg']")
        if not target_node:
            if any(x in root_element.tag for x in ['Document', 'BusMsg']):
                target_node = [root_element]
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    layer=2,
                    code="DOCUMENT_NODE_MISSING",
                    path="/",
                    message="Payload validation failed: No <Document> or <BusMsg> element found.",
                    line=1,
                    fix="Ensure the payload is wrapped in <Document>."
                ))
                return False

        try:
            import copy
            schema = Layer2Validator._get_schema(xsd_path)
            # We copy the element because we need to strip/force the namespace exactly as the XSD expects
            validation_target = copy.deepcopy(target_node[0])
            
            # The SR2026 CBPR+ XSDs expect elements to be in the base ISO namespace
            # e.g., urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08
            target_ns = etree.QName(target_node[0]).namespace
            if target_ns:
                Layer2Validator._mask_namespace_in_place(validation_target, target_ns)

            schema.assertValid(validation_target)
            return apphdr_passed
        except etree.DocumentInvalid as e:
            from app.sr2025.validation.validators.layer2_validator import Layer2Mixin
            import re
            
            mixin = Layer2Mixin()
            tag_info = mixin._build_tag_info_from_xsd(xsd_path)
            
            for error in e.error_log:
                friendly_msg, suggestion = mixin._simplify_error_message(error.message, tag_info)
                
                # Extract element name for better path highlighting in frontend
                elem_match = re.search(r"Element '([^']+)'", error.message)
                path_str = "/"
                if elem_match:
                    raw_tag = elem_match.group(1)
                    tag_name = raw_tag.split('}')[-1] if '}' in raw_tag else raw_tag
                    path_str = f"//{tag_name}"

                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    layer=2,
                    code="SCHEMA_VAL",
                    path=path_str,
                    message=friendly_msg,
                    line=error.line,
                    fix=suggestion
                ))
            return False
        except Exception as e:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="SCHEMA_ENGINE_FAILURE",
                path="/",
                message=f"Failed to run schema validation engine: {str(e)}",
                line=1,
                fix="Contact support."
            ))
            return None  # catastrophic — engine failed, cannot proceed

    @staticmethod
    def _mask_namespace_in_place(element, new_ns: str):
        if not isinstance(element.tag, str):
            return
        local = etree.QName(element).localname
        element.tag = f"{{{new_ns}}}{local}"
        for child in element:
            Layer2Validator._mask_namespace_in_place(child, new_ns)
