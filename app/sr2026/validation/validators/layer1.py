import re
from lxml import etree
from typing import Optional, Tuple
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

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

        # 1.5 Payload Size (FATAL)
        size_kb = len(xml_content.encode('utf-8')) / 1024
        max_size = 2048 # 2MB limit
        if size_kb > max_size:
            report.add_issue(ValidationIssue(
                 severity="ERROR", 
                 code="FILE_TOO_LARGE",
                 path="/", 
                 message=f"Your message is {size_kb:.1f} KB, exceeding the {max_size} KB limit.",
                 line=1,
                 fix=f"Please reduce the message size below {max_size} KB."
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
            error_line = str(e.lineno) if e.lineno else "?"
            error_msg = str(e)

            # Detect if a literal & (unescaped ampersand) is the root cause
            has_raw_amp = bool(re.search(r"&(?![a-zA-Z#][a-zA-Z0-9#]*;)", xml_content))
            if has_raw_amp:
                for i, line in enumerate(xml_content.split("\n"), start=1):
                    if re.search(r"&(?![a-zA-Z#][a-zA-Z0-9#]*;)", line):
                        error_line = str(i)
                        break
                friendly_msg = (
                    f"Invalid character '&' (ampersand) at line {error_line}. "
                    f"The '&' character is reserved in XML and is not allowed in name or address fields."
                )
                fix_hint = (
                    f"Check line {error_line} and remove the '&' character. "
                    f"If you mean 'and', write the word 'and' instead."
                )
            elif "invalid char" in error_msg.lower() or "illegal char" in error_msg.lower():
                friendly_msg = (
                    f"Invalid character at line {error_line}. "
                    f"Name and address fields must only contain letters, digits, spaces and . , ( ) ' -"
                )
                fix_hint = (
                    f"Check line {error_line} for any special characters such as &, @, !, #, $ and remove them."
                )
            else:
                friendly_msg = (
                    f"XML syntax error at line {error_line}: the message cannot be parsed. "
                    f"Check for unclosed tags, missing quotes, or reserved characters like '&'."
                )
                fix_hint = (
                    f"Technical details: {error_msg}. "
                    f"Check near line {error_line} for unclosed tags, invalid characters, or malformed XML."
                )

            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="XML_SYNTAX_ERROR",
                path="/",
                message=friendly_msg,
                line=int(error_line) if error_line.isdigit() else 1,
                fix=fix_hint
            ))
            return None
