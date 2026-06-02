import time
import re
from typing import Optional
from lxml import etree
from .models import ValidationIssue, ValidationReport

class Layer1Mixin:
    async def _run_layer_1(self, xml_content: str, report: ValidationReport, filename: Optional[str] = None) -> bool:
        """
        LAYER 1 — Technical / Payload Validation
        Comprehensive check for well-formedness, illegal characters, and standard ISO envelopes.
        """
        start = time.time()
        
        # 1. Payload Presence (FATAL)
        if not xml_content or not xml_content.strip():
            report.add_issue(ValidationIssue(
                "ERROR", 1, "Empty File", "Line 1",
                "The uploaded file is empty or no content was provided.",
                "Please upload a valid XML message file or paste XML content."
            ))
            report.layer_status["1"] = {"status": "❌", "time": (time.time() - start) * 1000}
            return False

        # 2. File Type & Preliminary XML Structure (NON-FATAL for L1 checks)
        allowed_exts = ('.xml', '.xsd', '.txt')
        if filename and not filename.lower().endswith(allowed_exts):
             report.add_issue(ValidationIssue(
                "ERROR", 1, "Wrong File Type", "File Extension",
                f"The file '{filename}' is not a standard XML extension.",
                "Please use .xml, .xsd, or .txt extension."
            ))

        # Check content structure (must look like XML)
        has_xml_structure = xml_content.lstrip().startswith(('<', '<?xml'))
        if not has_xml_structure:
             report.add_issue(ValidationIssue(
                "ERROR", 1, "Invalid Content", "Line 1",
                "The file content does not appear to be valid XML.",
                "Ensure the file contains XML tags starting with '<'."
            ))

        # 3. Payload Size (FATAL)
        size_kb = len(xml_content.encode('utf-8')) / 1024
        max_size = self.config.get("app_settings", {}).get("max_file_size_kb", 2048)
        if size_kb > max_size:
             report.add_issue(ValidationIssue(
                 "ERROR", 1, "File Too Large", "Size Limit", 
                 f"Your message is {size_kb:.1f} KB, exceeding the {max_size} KB limit.",
                 f"Please reduce the message size below {max_size} KB."
             ))
             report.layer_status["1"] = {"status": "❌", "time": (time.time() - start) * 1000}
             return False

        # 4. UTF-8 Encoding & Header (NON-FATAL for L1 checks)
        header_match = re.search(r'<\?xml[^>]+encoding=["\']([^"\']+)["\']', xml_content, re.IGNORECASE)
        if not header_match:
             report.add_issue(ValidationIssue(
                "ERROR", 1, "Missing Header", "Line 1",
                "Your XML file is missing the required declaration header.",
                "Add this line at the very top: <?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            ))
        else:
            encoding = header_match.group(1).upper()
            if encoding != "UTF-8":
                report.add_issue(ValidationIssue(
                    "ERROR", 1, "Wrong Encoding", "Line 1",
                    f"Your file uses {encoding} encoding, but ISO 20022 messages must use UTF-8.",
                    "Change the encoding in your XML header to UTF-8."
                ))

        # 5. Illegal Characters (NON-FATAL for L1 checks)
        illegal_chars = re.findall(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', xml_content)
        if illegal_chars:
            report.add_issue(ValidationIssue(
                "ERROR", 1, "Invalid Characters", "Line 1",
                "Your message contains invisible control characters that are not allowed.",
                "Remove hidden characters (ASCII 0-31) from your XML."
            ))

        # 5.1 DTD Declaration Rejection (Security — FATAL)
        if re.search(r'<!DOCTYPE', xml_content, re.IGNORECASE):
            report.add_issue(ValidationIssue(
                "ERROR", 1, "DTD_FORBIDDEN", "Line 1",
                "DTD declarations (<!DOCTYPE>) are not allowed in ISO 20022 messages.",
                "Remove any <!DOCTYPE ...> declaration from your XML. ISO 20022 uses XSD validation only."
            ))
            report.layer_status["1"] = {"status": "❌", "time": (time.time() - start) * 1000}
            return False

        # 5.2 Entity Expansion Rejection (Security)
        if re.search(r'<!ENTITY', xml_content, re.IGNORECASE):
            report.add_issue(ValidationIssue(
                "ERROR", 1, "ENTITY_FORBIDDEN", "Line 1",
                "XML entity declarations (<!ENTITY>) are not allowed in ISO 20022 messages.",
                "Remove all <!ENTITY ...> declarations. Inline all values directly."
            ))
            report.layer_status["1"] = {"status": "❌", "time": (time.time() - start) * 1000}
            return False

        # 6. XML Well-Formedness & Identity (FATAL if parse fails)
        try:
            xml_bytes = xml_content.encode('utf-8')
            parser = etree.XMLParser(recover=False, no_network=True, remove_blank_text=True, resolve_entities=False)
            root = etree.fromstring(xml_bytes, parser)
            
            # 7. Envelope Detection (Document / BusMsg / AppHdr)
            iso_nodes = root.xpath("//*[local-name()='Document' or local-name()='BusMsg' or local-name()='AppHdr' or local-name()='BusMsgEnvlp']")
            if not iso_nodes and any(x in root.tag for x in ['Document', 'BusMsg', 'AppHdr', 'BusMsgEnvlp']):
                iso_nodes = [root]
            
            if not iso_nodes:
                report.add_issue(ValidationIssue(
                    "ERROR", 1, "Missing Structure", "Root",
                    "The XML is missing the required ISO 20022 <Document> or <BusMsg> wrapper.",
                    "Ensure your message is wrapped in a standard ISO 20022 container."
                ))
            else:
                # 8. Namespace Validation
                payload_node = root.xpath("//*[local-name()='Document' or local-name()='BusMsg']")
                doc_node = payload_node[0] if payload_node else iso_nodes[0]
                ns = doc_node.nsmap.get(None) or ""
                
                if not re.match(r'^urn:iso:std:iso:20022:tech:xsd:[a-z]{4}\.\d{3}\.\d{3}\.\d{2}$', ns) and "head.001" not in ns:
                    report.add_issue(ValidationIssue(
                        "ERROR", 1, "Wrong Namespace", str(doc_node.sourceline or 1),
                        f"The namespace '{ns}' does not match the ISO 20022 standard format.",
                        "Use the correct URN format (e.g. urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08)."
                    ))
                report.metadata.update({"Namespace": ns})

            # 9. XML Depth Limit Check
            max_depth = self.config.get("app_settings", {}).get("max_xml_depth", 50)
            def _get_depth(elem, depth=1):
                child_depths = [_get_depth(c, depth + 1) for c in elem]
                return max(child_depths) if child_depths else depth
            actual_depth = _get_depth(root)
            if actual_depth > max_depth:
                report.add_issue(ValidationIssue(
                    "ERROR", 1, "XML_DEPTH_EXCEEDED", "Root",
                    f"XML nesting depth is {actual_depth}, which exceeds the maximum of {max_depth} levels.",
                    f"Reduce the nesting depth of your XML to {max_depth} levels or fewer."
                ))

        except etree.XMLSyntaxError as e:
            error_line = str(e.lineno) if e.lineno else "?"
            error_msg = str(e)

            # Detect if a literal & (unescaped ampersand) is the root cause
            has_raw_amp = bool(re.search(r"&(?![a-zA-Z#][a-zA-Z0-9#]*;)", xml_content))
            if has_raw_amp:
                # Find the exact line number of the & in the raw content
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
                "ERROR", 1, "XML Syntax Error", error_line,
                friendly_msg,
                fix_hint
            ))
            report.layer_status["1"] = {"status": "❌", "time": (time.time() - start) * 1000}
            return False

        # Finish Layer 1
        success = report.status != "FAIL"
        report.layer_status["1"] = {"status": "✅" if success else "❌", "time": round((time.time() - start) * 1000, 2)}
        return success
