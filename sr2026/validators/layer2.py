import os
import re
from lxml import etree
from typing import Optional, Dict, Tuple
from sr2026.validators.models import ValidationIssue, ValidationReport

class Layer2Validator:
    _schema_cache: Dict[str, etree.XMLSchema] = {}

    @staticmethod
    def _resolve_xsd_path(message_type: str) -> Optional[str]:
        # Path configuration
        base_dir = os.path.dirname(os.path.abspath(__file__))
        backend_root = os.path.normpath(os.path.join(base_dir, "../../"))
        xsds_dir = os.path.join(backend_root, "xsds sr2026")
        
        if not os.path.exists(xsds_dir):
            return None

        # Clean message type
        msg_type_clean = message_type.strip().lower()
        
        # 1. Gather all files in the directory
        files = os.listdir(xsds_dir)
        xsd_files = [f for f in files if f.endswith('.xsd')]

        # Helper mapping for specific message types
        # Input message types to unique search terms
        if "pacs.008.stp" in msg_type_clean or "pacs.008_stp" in msg_type_clean:
            match_term = "pacs_008_001_08_stp"
        elif "pacs.008" in msg_type_clean:
            # We want standard pacs.008, not STP
            match_term = "pacs_008_001_08_fito"  # Matches pacs_008_001_08_FIToFICustomer
        elif "pacs.009.cov" in msg_type_clean or "pacs.009_cov" in msg_type_clean:
            match_term = "pacs_009_001_08_cov"
        elif "pacs.009.adv" in msg_type_clean or "pacs.009_adv" in msg_type_clean:
            match_term = "pacs_009_001_08_adv"
        elif "pacs.009" in msg_type_clean:
            match_term = "pacs_009_001_08_financial"  # Matches pacs_009_001_08_FinancialInstitutionCreditTransfer
        elif "camt.105.multiple" in msg_type_clean or "camt.105_multiple" in msg_type_clean:
            match_term = "camt_105_001_03_chargespaymentnotification_multiple"
        elif "camt.105" in msg_type_clean:
            match_term = "camt_105_001_03_chargespaymentnotification_20"  # Avoid multiple
        elif "camt.106.multiple" in msg_type_clean or "camt.106_multiple" in msg_type_clean:
            match_term = "camt_106_001_03_chargespaymentrequest_multiple"
        elif "camt.106" in msg_type_clean:
            match_term = "camt_106_001_03_chargespaymentrequest_20"  # Avoid multiple
        else:
            # General search by replacing dot with underscore (e.g. camt.053 -> camt_053)
            parts = msg_type_clean.split('.')
            match_term = "_".join(parts[:2]) if len(parts) >= 2 else msg_type_clean

        for f in xsd_files:
            f_clean = f.lower()
            if match_term in f_clean:
                return os.path.join(xsds_dir, f)

        # Fallback to general prefix matching
        parts = msg_type_clean.split('.')
        family_prefix = "_".join(parts[:2]) if len(parts) >= 2 else msg_type_clean
        for f in xsd_files:
            if family_prefix.lower() in f.lower():
                return os.path.join(xsds_dir, f)

        return None

    @staticmethod
    def _get_schema(xsd_path: str) -> etree.XMLSchema:
        if xsd_path not in Layer2Validator._schema_cache:
            xsd_doc = etree.parse(xsd_path)
            schema = etree.XMLSchema(xsd_doc)
            Layer2Validator._schema_cache[xsd_path] = schema
        return Layer2Validator._schema_cache[xsd_path]

    @staticmethod
    def validate(root_element: etree._Element, message_type: str, report: ValidationReport) -> bool:
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
            return False

        # Extract target payload node (<Document> or <BusMsg>)
        target_node = root_element.xpath("//*[local-name()='Document' or local-name()='BusMsg']")
        if not target_node:
            if any(x in root_element.tag for x in ['Document', 'BusMsg']):
                target_node = [root_element]
        
        if not target_node:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="MISSING_DOCUMENT",
                path="/",
                message="Cannot find the main <Document> or <BusMsg> container in your XML.",
                line=1,
                fix="Ensure your message contains a valid <Document> or <BusMsg> element."
            ))
            return False
            
        main_node = target_node[0]

        try:
            schema = Layer2Validator._get_schema(xsd_path)
            
            # Retrieve schema namespace
            xsd_doc = etree.parse(xsd_path)
            xsd_ns = xsd_doc.getroot().get("targetNamespace") or ""

            # Check if elements are namespaces
            xml_ns = etree.QName(main_node).namespace or ""
            
            # Mask namespace if mismatch (common in testing/custom payloads)
            import copy
            validation_doc = copy.deepcopy(main_node)
            if xsd_ns and xml_ns != xsd_ns:
                Layer2Validator._mask_namespace_in_place(validation_doc, xsd_ns)

            schema.assertValid(validation_doc)
            return True
        except etree.DocumentInvalid as e:
            for error in e.error_log:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="SCHEMA_VALIDATION_ERROR",
                    path=f"//{error.domain or ''}",
                    message=f"XSD Validation Error: {error.message}",
                    line=error.line,
                    fix="Verify element schema properties and tags."
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
            return False

    @staticmethod
    def _mask_namespace_in_place(element, new_ns: str):
        if not isinstance(element.tag, str):
            return
        local = etree.QName(element).localname
        element.tag = f"{{{new_ns}}}{local}"
        for child in element:
            Layer2Validator._mask_namespace_in_place(child, new_ns)
