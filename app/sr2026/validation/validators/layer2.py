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
    def _validate_apphdr(root_element: etree._Element, report: ValidationReport,
                         xml_content: str = "") -> bool:
        """Validate the <AppHdr> element against head.001.001.02.xsd.
        Returns True if valid, False if XSD errors found or if AppHdr is missing.

        Header errors are routed through the SR2025 translation engine
        (Layer2Mixin._simplify_error_message) so every libxml2 facet/value error —
        empty value (minLength), BIC, pattern, length, enum, date, attribute, … —
        becomes the same friendly, actionable message SR2025 produces, instead of
        leaking raw "Element 'BizMsgIdr': [facet 'minLength'] …" text. Issues are
        emitted under code HEADER_VAL (matching SR2025) so the shared autofix
        engine's header routes (missing-field inserter, Fr/To party repair) fire
        for SR2026 too."""
        apphdr_nodes = root_element.xpath("//*[local-name()='AppHdr']")
        if not apphdr_nodes:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                layer=2,
                code="HEADER_VAL",
                path="/AppHdr",
                message="The standard header block (<AppHdr>) is missing from the file.",
                line=1,
                fix="Please ensure the <AppHdr> block is present in the XML envelope before the main document."
            ))
            return False

        base_dir = os.path.dirname(os.path.abspath(__file__))
        hdr_xsd_path = os.path.normpath(
            os.path.join(base_dir, "../../xsds/head.001.001.02.xsd")
        )
        if not os.path.exists(hdr_xsd_path):
            return True  # no header XSD available — skip silently

        apphdr_node = apphdr_nodes[0]
        h_line_offset = apphdr_node.sourceline or 1

        # Reuse the SR2025 translation engine + XSD-driven mandatory-children list.
        # SR2026's validate() already uses this mixin for the document body, so the
        # bare-instance pattern is established (the Cd-codelist branches fall back
        # to defaults when self.codelists is absent — which is fine here).
        from app.sr2025.validation.validators.layer2_validator import Layer2Mixin
        mixin = Layer2Mixin()
        h_tag_info = mixin._build_tag_info_from_xsd(hdr_xsd_path)

        xsd_ns = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"
        xml_ns = etree.QName(apphdr_node).namespace or ""
        _ns_valid = xml_ns == xsd_ns
        if not _ns_valid:
            displayed_ns = xml_ns if xml_ns else "null"
            report.add_issue(ValidationIssue(
                severity="ERROR",
                layer=2,
                code="HEADER_VAL",
                path="//AppHdr",
                message=(
                    f"The application header's namespace ({displayed_ns}) is not valid. "
                    f"It should be 'BusinessApplicationHeaderV02' ({xsd_ns})."
                ),
                line=h_line_offset,
                fix=f'Add xmlns="{xsd_ns}" to the <AppHdr> element.',
            ))

        try:
            schema = Layer2Validator._get_schema(hdr_xsd_path)

            import copy
            validation_doc = copy.deepcopy(apphdr_node)
            if not _ns_valid:
                Layer2Validator._mask_namespace_in_place(validation_doc, xsd_ns)

            schema.assertValid(validation_doc)
            return _ns_valid  # False if namespace was wrong (even though XSD passed)
        except etree.DocumentInvalid as e:
            # 1. Simplify each raw libxml2 header error (SR2025 parity). libxml2
            #    stops at the first content-model violation, so this list is the
            #    value/format errors plus its single structural complaint.
            hdr_libxml_issues = []
            for error in e.error_log:
                friendly_msg, suggestion = mixin._simplify_error_message(
                    error.message, h_tag_info, xml_content=xml_content)

                # High-precision line + path correction against the real AppHdr tree.
                h_real_line = error.line
                best_n = None
                tag_m = re.search(r"Element '([^']+)'", error.message)
                try:
                    if tag_m:
                        t_name = tag_m.group(1).split('}')[-1]
                        is_empty_h = "Empty" in friendly_msg or "minLength" in error.message
                        h_cands = apphdr_node.xpath(
                            f"descendant-or-self::*[local-name()='{t_name}']")
                        if h_cands:
                            if is_empty_h:
                                h_cands = [c for c in h_cands if not (c.text or "").strip()]
                            if h_cands:
                                best_n = min(h_cands,
                                             key=lambda c: abs((c.sourceline or 0) - error.line))
                                h_real_line = best_n.sourceline
                except Exception:
                    pass

                h_path = "//AppHdr"
                if best_n is not None:
                    h_path = f"//AppHdr/{etree.QName(best_n).localname}"
                elif tag_m:
                    h_path = f"//AppHdr/{tag_m.group(1).split('}')[-1]}"

                hdr_libxml_issues.append(ValidationIssue(
                    severity="ERROR",
                    layer=2,
                    code="HEADER_VAL",
                    path=h_path,
                    message=friendly_msg.strip(),
                    line=h_real_line,
                    fix=suggestion
                ))

            # 2. Completeness scan — report EVERY missing mandatory direct child of
            #    AppHdr at once (read from the head XSD, not a hardcoded list).
            present_children = {etree.QName(c.tag).localname for c in apphdr_node
                                if isinstance(c.tag, str)}
            mandatory = (mixin._mandatory_header_children(hdr_xsd_path)
                         or Layer2Validator._APPHDR_MANDATORY_ELEMENTS)
            hdr_missing_issues = []
            for tag in mandatory:
                if tag not in present_children:
                    label = (h_tag_info.get(tag, {}) or {}).get('label', tag)
                    hdr_missing_issues.append(ValidationIssue(
                        severity="ERROR",
                        layer=2,
                        code="HEADER_VAL",
                        path=f"//AppHdr/{tag}",
                        message=f"The mandatory header element '{tag}' is missing from the Business Application Header (AppHdr).",
                        line=h_line_offset,
                        fix=f"Add a <{tag}> element inside <AppHdr>. {label} is required by the ISO 20022 BAH (head.001) schema."
                    ))

            if hdr_missing_issues:
                # Clear per-field "missing" issues supersede libxml2's confusing
                # single structural complaint — keep genuine value/format errors on
                # fields that ARE present, drop the structural noise.
                def _is_structural(it):
                    m = (it.message or "").lower()
                    return any(s in m for s in (
                        "is not expected", "not expected here",
                        "not expected at this position", "unexpected field",
                        "is expected", "is not complete",
                        "content is incomplete", "not complete",
                    ))
                for it in hdr_missing_issues:
                    report.add_issue(it)
                for it in hdr_libxml_issues:
                    if not _is_structural(it):
                        report.add_issue(it)
            else:
                for it in hdr_libxml_issues:
                    report.add_issue(it)
            return False
        except Exception:
            return True  # non-fatal — don't block the pipeline

    @staticmethod
    def validate(root_element: etree._Element, message_type: str, report: ValidationReport,
                 xml_content: str = ""):
        """Returns True (passed), False (XSD errors - L3 should still run), or None (catastrophic - stop pipeline)."""
        # Always validate AppHdr against head.001.001.02.xsd
        apphdr_passed = Layer2Validator._validate_apphdr(root_element, report, xml_content)

        xsd_path = Layer2Validator._resolve_xsd_path(message_type)
        if not xsd_path:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="SCHEMA_NOT_FOUND",
                path="/",
                message=f"The message type '{message_type}' is not supported or recognized.",
                line=1,
                fix="Please verify the messageType matches a supported ISO 20022 schema (e.g., pacs.008)."
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
                    message="The file is missing the required main wrapper (<Document> or <BusMsg>).",
                    line=1,
                    fix="Make sure the entire payload is wrapped inside a <Document> block."
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
