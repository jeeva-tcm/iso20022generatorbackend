import re
from lxml import etree
from typing import Optional, Tuple
from sr2026.validators.models import ValidationIssue, ValidationReport

class Layer1Validator:
    @staticmethod
    def validate(xml_content: str, report: ValidationReport) -> Optional[etree._Element]:
        # 1. Payload presence
        if not xml_content or not xml_content.strip():
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="EMPTY_PAYLOAD",
                path="/",
                message="The XML message content is empty.",
                line=1,
                fix="Provide valid XML content."
            ))
            return None

        # 2. Basic XML structure
        if not xml_content.lstrip().startswith(('<', '<?xml')):
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="INVALID_XML_STRUCTURE",
                path="/",
                message="The content does not appear to be valid XML structure.",
                line=1,
                fix="Ensure XML starts with '<' or '<?xml'."
            ))
            return None

        # 3. UTF-8 header check
        header_match = re.search(r'<\?xml[^>]+encoding=["\']([^"\']+)["\']', xml_content, re.IGNORECASE)
        if not header_match:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="MISSING_XML_DECLARATION",
                path="/",
                message="The XML header declaration is missing.",
                line=1,
                fix="Add <?xml version=\"1.0\" encoding=\"UTF-8\"?> at the top of the file."
            ))
        else:
            encoding = header_match.group(1).upper()
            if encoding != "UTF-8":
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="INVALID_ENCODING",
                    path="/",
                    message=f"XML uses '{encoding}' encoding. ISO 20022 requires UTF-8.",
                    line=1,
                    fix="Change the XML encoding declaration to UTF-8."
                ))

        # 4. Illegal characters check
        if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', xml_content):
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="ILLEGAL_CONTROL_CHARACTERS",
                path="/",
                message="XML content contains disallowed control characters (ASCII 0-31).",
                line=1,
                fix="Remove any hidden control characters."
            ))

        # 5. Security: DTD and Entity Rejections
        if re.search(r'<!DOCTYPE', xml_content, re.IGNORECASE):
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="DTD_FORBIDDEN",
                path="/",
                message="DOCTYPE DTD declarations are forbidden for security reasons.",
                line=1,
                fix="Remove the DOCTYPE declaration."
            ))
            return None

        if re.search(r'<!ENTITY', xml_content, re.IGNORECASE):
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="ENTITY_FORBIDDEN",
                path="/",
                message="ENTITY XML expansion declarations are forbidden for security reasons.",
                line=1,
                fix="Remove all ENTITY declarations."
            ))
            return None

        # 6. Parse XML
        try:
            parser = etree.XMLParser(recover=False, no_network=True, remove_blank_text=True, resolve_entities=False)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
            
            # 7. Envelope check
            iso_nodes = root.xpath("//*[local-name()='Document' or local-name()='BusMsg' or local-name()='AppHdr']")
            if not iso_nodes and any(x in root.tag for x in ['Document', 'BusMsg', 'AppHdr']):
                iso_nodes = [root]
                
            if not iso_nodes:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MISSING_ISO_ROOT",
                    path="/",
                    message="The message is missing the required ISO 20022 root elements (<Document> or <BusMsg>).",
                    line=1,
                    fix="Wrap the message in a standard <Document> or <BusMsg> element."
                ))
            else:
                # 8. Namespace verification
                payload_node = root.xpath("//*[local-name()='Document' or local-name()='BusMsg']")
                doc_node = payload_node[0] if payload_node else iso_nodes[0]
                ns = doc_node.nsmap.get(None) or ""
                if not re.match(r'^urn:iso:std:iso:20022:tech:xsd:[a-z]{4}\.\d{3}\.\d{3}\.\d{2}$', ns) and "head.001" not in ns:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_NAMESPACE_FORMAT",
                        path="/",
                        message=f"The namespace '{ns}' does not follow the standard urn:iso URN format.",
                        line=doc_node.sourceline or 1,
                        fix="Update namespace to match urn:iso:std:iso:20022:tech:xsd:<message_type>."
                    ))

            # 9. XML Depth check
            max_depth = 50
            def get_depth(elem, depth=1):
                child_depths = [get_depth(c, depth + 1) for c in elem if isinstance(c.tag, str)]
                return max(child_depths) if child_depths else depth
            actual_depth = get_depth(root)
            if actual_depth > max_depth:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="XML_DEPTH_EXCEEDED",
                    path="/",
                    message=f"XML depth exceeds the maximum allowed limit of {max_depth} levels (actual: {actual_depth}).",
                    line=1,
                    fix="Simplify the XML structure to reduce element nesting."
                ))

            return root
        except etree.XMLSyntaxError as e:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="XML_SYNTAX_ERROR",
                path="/",
                message=f"XML syntax error: {str(e)}",
                line=e.lineno or 1,
                fix="Ensure all XML tags are properly closed and attribute values are quoted."
            ))
            return None
