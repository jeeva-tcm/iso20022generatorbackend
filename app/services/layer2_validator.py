import time
import re
import os
import copy
from lxml import etree
from typing import Optional
from .models import ValidationIssue, ValidationReport

class Layer2Mixin:
    # Cache: xsd_path → {tag_name: {label, mandatory, repeatable}}
    _xsd_tag_cache: dict = {}
    # Cache: xsd_path → parsed (xsd_doc, schema) — avoids re-parsing the XSD on every
    # validation call. XSD files are large and parsing them dominated the L2 wall time.
    _xsd_schema_cache: dict = {}
    # Cache: head xsd_path → ordered list of mandatory direct children of AppHdr
    _hdr_mandatory_cache: dict = {}

    def _mandatory_header_children(self, xsd_path: str) -> list:
        """
        Parse a head.001 (BAH) XSD and return the ORDERED list of mandatory
        direct children of AppHdr — e.g. ['Fr','To','BizMsgIdr','MsgDefIdr',
        'CreDt']. Used to report EVERY missing mandatory header field at once,
        instead of relying on libxml2 which stops at the first sequence error.
        """
        if xsd_path in Layer2Mixin._hdr_mandatory_cache:
            return Layer2Mixin._hdr_mandatory_cache[xsd_path]
        XS = 'http://www.w3.org/2001/XMLSchema'
        result: list = []
        try:
            root = etree.parse(xsd_path).getroot()
            # <xs:element name="AppHdr" type="BusinessApplicationHeaderVxx"/>
            type_name = None
            for el in root.iter(f'{{{XS}}}element'):
                if el.get('name') == 'AppHdr':
                    type_name = (el.get('type') or '').split(':')[-1]
                    break
            ctype = None
            if type_name:
                for ct in root.iter(f'{{{XS}}}complexType'):
                    if ct.get('name') == type_name:
                        ctype = ct
                        break
            seq = ctype.find(f'{{{XS}}}sequence') if ctype is not None else None
            if seq is not None:
                for el in seq.findall(f'{{{XS}}}element'):
                    nm = el.get('name')
                    if nm and el.get('minOccurs', '1') != '0':
                        result.append(nm)
        except Exception as ex:
            print(f'[DEBUG] header mandatory parse failed: {ex}')
        Layer2Mixin._hdr_mandatory_cache[xsd_path] = result
        return result

    def _validate_swift_characters(self, xml_content: str) -> list:
        """
        Check for forbidden SWIFT characters (,#;@{}[]()!) that appear:
          1. In element.text when the element HAS child elements (mixed-content junk)
          2. In element.tail (text after a closing tag — always inter-element junk)
          3. In leaf element text for CBPR+ RestrictedFINXMax35Text fields
             (MsgId, BizMsgIdr, EndToEndId, InstrId, TxId, etc.) where '!' and
             other forbidden chars are not allowed even inside the tag value.
        """
        # Characters genuinely forbidden in SWIFT/ISO 20022 CBPR+ messages.
        # Excludes . : ' because those appear legitimately in amounts, timestamps,
        # BizSvc values (swift.cbprplus.02), and address content.
        FORBIDDEN = set(',#;@{}[]()!')
        issues = []

        # Leaf ID fields that use CBPR+ RestrictedFINXMax35Text / Max16Text.
        # Address/name tags (Nm, StrtNm, TwnNm, AdrLine, etc.) are already
        # covered by _validate_charsets_in_xml (Step 4.16, code INVALID_CHARSET)
        # so they are intentionally excluded here to avoid double-reporting.
        LEAF_CHECKED_TAGS = {
            "MsgId", "BizMsgIdr", "EndToEndId", "InstrId", "TxId", "ClrSysRef",
            "OrgnlMsgId", "OrgnlEndToEndId", "OrgnlInstrId", "PmtInfId",
            "Ustrd", "AddtlInf",
        }

        try:
            parser = etree.XMLParser(remove_blank_text=False, no_network=True, recover=True)
            root = etree.fromstring(xml_content.encode('utf-8'), parser)
        except Exception:
            # If the XML can't be parsed at all, skip — XML_SYNTAX will catch it.
            return issues

        def _bad_chars(text: str) -> list:
            """Return the unique forbidden characters found in text (ignoring whitespace)."""
            non_ws = ''.join(c for c in text if not c.isspace())
            return sorted({c for c in non_ws if c in FORBIDDEN})

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            local = etree.QName(elem.tag).localname
            line_num = getattr(elem, 'sourceline', None) or 0

            # 1. Leaf element text for CBPR+ restricted fields.
            #    Only for known tags where FORBIDDEN chars are never valid.
            if len(elem) == 0 and elem.text and local in LEAF_CHECKED_TAGS:
                bad = _bad_chars(elem.text)
                if bad:
                    chars_str = ', '.join(f"'{c}'" for c in bad)
                    issues.append(ValidationIssue(
                        "ERROR", 2, "SWIFT_FORBIDDEN_CHAR", str(line_num),
                        f"Element <{local}> contains forbidden character(s) {chars_str}. "
                        f"SWIFT/CBPR+ RestrictedFINXText fields must not contain these characters.",
                        f"Remove the character(s) {chars_str} from <{local}>. "
                        f"Allowed characters: A-Z a-z 0-9 space / - ? : ( ) . , ' +",
                        line=line_num
                    ))

            # 2. parent.text — text before the first child element.
            #    Only flag when the element HAS children (mixed content = junk).
            if len(elem) > 0 and elem.text:
                bad = _bad_chars(elem.text)
                if bad:
                    chars_str = ', '.join(f"'{c}'" for c in bad)
                    issues.append(ValidationIssue(
                        "ERROR", 2, "SWIFT_FORBIDDEN_CHAR", str(line_num),
                        f"Element <{local}> contains forbidden character(s) {chars_str} "
                        f"in text content before its child elements. "
                        f"SWIFT/ISO 20022 messages must not contain these characters outside of tag values.",
                        f"Remove the character(s) {chars_str} from the text content "
                        f"inside <{local}> that appears before its child elements.",
                        line=line_num
                    ))

            # 3. child.tail — text after a closing tag (</Child>) before the
            #    next sibling or parent close.  This is always inter-element junk.
            if elem.tail:
                bad = _bad_chars(elem.tail)
                if bad:
                    parent = elem.getparent()
                    parent_name = etree.QName(parent.tag).localname if parent is not None and isinstance(parent.tag, str) else 'root'
                    tail_line = getattr(elem, 'sourceline', None) or line_num
                    chars_str = ', '.join(f"'{c}'" for c in bad)
                    issues.append(ValidationIssue(
                        "ERROR", 2, "SWIFT_FORBIDDEN_CHAR", str(tail_line),
                        f"Forbidden character(s) {chars_str} found after closing tag </{local}> "
                        f"inside <{parent_name}>. "
                        f"SWIFT/ISO 20022 messages must not contain these characters between elements.",
                        f"Remove the character(s) {chars_str} that appear after </{local}> "
                        f"inside <{parent_name}>.",
                        line=tail_line
                    ))

        return issues

    async def _run_layer_2(self, xml_content: str, report: ValidationReport, message_type: str) -> bool:
        """
        LAYER 2 — ISO Structure Validation (XSD)
        Strict implementation of the 10-Step Execution Order.

        A <BulkMessages> file bundles several messages; EVERY <Document> and
        every <AppHdr> is validated independently (validating only the first let
        a broken 2nd+ message — missing namespace, misplaced element — pass with
        zero errors, which MyStandards correctly flags).
        """
        start = time.time()
        issues = []

        try:
            # IMPORTANT: remove_blank_text MUST be False to preserve user's original line numbers
            parser = etree.XMLParser(remove_blank_text=False, no_network=True)
            full_xml_doc = etree.fromstring(xml_content.encode('utf-8'), parser)

            # Step 2 — Locate ALL payload <Document>/<BusMsg> containers.
            target_node = full_xml_doc.xpath("//*[local-name()='Document' or local-name()='BusMsg']")
            if not target_node:
                # Check if root itself is Document/BusMsg — use exact localname,
                # NOT substring, so 'BusMsgEnvlp' never false-matches 'BusMsg'.
                root_local = etree.QName(full_xml_doc.tag).localname if isinstance(full_xml_doc.tag, str) else ""
                if root_local in ('Document', 'BusMsg'):
                    target_node = [full_xml_doc]

            if not target_node:
                report.add_issue(ValidationIssue("ERROR", 2, "Missing Document", "No Root Element", "Cannot find the main <Document> or <BusMsg> container in your XML.", "Your message structure is incorrect. ISO 20022 messages must have a <Document> wrapper element."))
                report.layer_status["2"] = {"status": "", "time": (time.time() - start) * 1000}
                return False

            # Step 3 to 9 — validate every payload document against its own schema.
            for _doc_node in target_node:
                issues.extend(self._validate_payload_node(_doc_node, message_type, xml_content))

            # Step 11 — validate every Business Application Header (head.001).
            for _app_hdr_node in full_xml_doc.findall(".//{*}AppHdr"):
                issues.extend(self._validate_header_node(_app_hdr_node, xml_content))

        except etree.XMLSyntaxError as e:
             issues.append(ValidationIssue("ERROR", 2, "XML_SYNTAX", "/", f"XML Markup Error: {str(e)}", line=e.lineno))
        except Exception as e:
             issues.append(ValidationIssue("ERROR", 2, "VAL_ERR", "/", f"Internal Layer 2 Error: {str(e)}"))

        # Step 10 — SWIFT Character Validation (forbidden chars outside tags)
        try:
            swift_issues = self._validate_swift_characters(xml_content)
            issues.extend(swift_issues)
        except Exception as e:
            pass  # Non-blocking; SWIFT char validation doesn't fail validation

        # Final Collection & Success Assessment
        for issue in issues:
            report.add_issue(issue)

        success = not any(i.severity == "ERROR" for i in issues)
        report.layer_status["2"] = {"status": "PASS" if success else "FAIL", "time": round((time.time() - start) * 1000, 2)}
        return success

    def _validate_payload_node(self, doc_node, message_type: str, xml_content: str) -> list:
        """XSD-validate ONE <Document>/<BusMsg> payload node and return its issues.

        Each message in a bulk container is validated independently: the message
        type comes from THIS node's own namespace (so a bulk mixing families is
        each checked against the right schema). A node with no / non-ISO
        namespace is flagged ("a valid document must be present" — what
        MyStandards reports) and then STILL masked to the fallback schema so any
        structural error inside it (e.g. a misplaced element) also surfaces.
        Never raises — internal failures are turned into issues.
        """
        issues = []
        try:
            # Step 1 — message type + schema for THIS document.
            doc_ns = (etree.QName(doc_node).namespace or "") if isinstance(doc_node.tag, str) else ""
            _ns_m = re.match(r'^urn:iso:std:iso:20022:tech:xsd:([a-z]{4}\.\d{3}\.\d{3}\.\d{2})$', doc_ns)
            if _ns_m:
                effective_type = _ns_m.group(1)
            else:
                # Missing/non-ISO namespace on a bare <Document> — flag it, then
                # fall back to the bulk's detected type so the structural check
                # below still runs and reveals any other defects in this message.
                effective_type = message_type
                _ns_line = doc_node.sourceline or 1
                issues.append(ValidationIssue(
                    "ERROR", 2, "DOC_NAMESPACE_MISSING", str(_ns_line),
                    f"The <Document> at line {_ns_line} has no valid ISO 20022 namespace "
                    f"(found '{doc_ns or 'none'}'). A valid document must declare its namespace, "
                    f"e.g. xmlns=\"urn:iso:std:iso:20022:tech:xsd:{effective_type}\".",
                    f"Add the namespace to the <Document>: "
                    f"<Document xmlns=\"urn:iso:std:iso:20022:tech:xsd:{effective_type}\">.",
                    line=_ns_line,
                ))

            # Step 1b — Load Schema Set for the resolved type.
            xsd_full_path = self._get_xsd_path(effective_type)
            if not xsd_full_path or not os.path.exists(xsd_full_path):
                issues.append(ValidationIssue("ERROR", 2, "Schema Not Found", "Missing Validation Template", f"Cannot find the validation template for message type '{effective_type}'.", "The schema file (.xsd) for this message type is not available in the system. Contact support if this message type should be supported."))
                return issues

            main_node = doc_node
            # CRITICAL: Use deepcopy to preserve original sourcelines for exact error mapping.
            # Re-parsing via tostring/fromstring was shifting lines by 1 and losing blank lines.
            validation_doc = copy.deepcopy(main_node)

            # Use cached parsed XSD if available — XSD files are large (often
            # hundreds of KB) and parsing them on every validation call dominated
            # the bulk-generate wall time. XMLSchema objects are reusable.
            cached = Layer2Mixin._xsd_schema_cache.get(xsd_full_path)
            if cached is None:
                xsd_doc = etree.parse(xsd_full_path)
                schema = etree.XMLSchema(xsd_doc)
                Layer2Mixin._xsd_schema_cache[xsd_full_path] = (xsd_doc, schema)
            else:
                xsd_doc, schema = cached

            # Build dynamic tag info for rich error messages (cached per XSD)
            tag_info = self._build_tag_info_from_xsd(xsd_full_path)
            
            # Extract namespacing carefully
            xml_ns = etree.QName(validation_doc).namespace or ""
            # Robust XSD Namespace Detection
            xsd_ns = xsd_doc.getroot().get("targetNamespace")
            if not xsd_ns:
                raw_xsd = open(xsd_full_path, 'r', encoding='utf-8', errors='ignore').read()
                match = re.search(r'targetNamespace=["\']([^"\']+)["\']', raw_xsd)
                xsd_ns = match.group(1) if match else None

            # Step 3 to 9 — Automated Structural Validation
            try:
                # To support line-exactness while fixing namespace mismatches:
                # We validate the deep-copied version which allows us to mask namespaces 
                # in-place without destroying the original document's source line metadata.
                if xsd_ns and xml_ns != xsd_ns:
                    self._mask_namespace_in_place(validation_doc, xsd_ns)
                
                schema.assertValid(validation_doc)
            except etree.DocumentInvalid as e:
                for error in e.error_log:
                    # 1. Simplify the message FIRST so we know the context (tag name, empty vs invalid value)
                    friendly_msg, suggestion = self._simplify_error_message(error.message, tag_info, xml_content=xml_content, error_line=error.line)

                    # 2. Calculate initial absolute line (estimate)
                    # lxml reports lines in its error log. Since we use deepcopy on main_node,
                    # which preserves absolute sourcelines, error.line is already absolute.
                    # We no longer add line_offset to avoid double-counting.
                    real_line = error.line
                    best_node = None
                    
                    # 3. HIGH-PRECISION LINE CORRECTION
                    # We use the friendly message context to find the EXACT line in the original XML
                    try:
                        # Extract components from error
                        tag_match  = re.search(r"Element '([^']+)'", error.message)
                        val_match  = re.search(r"[Vv]alue '([^']*)'", error.message)
                        
                        found_line = None
                        
                        if tag_match:
                            raw_tag = tag_match.group(1)
                            tag_name = raw_tag.split('}')[-1] if '}' in raw_tag else raw_tag
                            
                            # CRITICAL: Detect Empty Error from multiple signals
                            # lxml error log: ... [facet 'minLength'] The value '' has a length of '0' ...
                            # Or missing child element errors which point to an incomplete parent.
                            is_empty_err = (
                                "Empty" in friendly_msg or 
                                "mandatory elements are missing" in friendly_msg or
                                "missing" in friendly_msg.lower() or
                                "missing mandatory" in friendly_msg.lower() or
                                "content is incomplete" in error.message or
                                "fails to occur" in error.message or
                                "minLength" in error.message or 
                                "facet 'pattern'The value ''" in error.message.replace(" ","") or
                                (val_match and val_match.group(1).strip() == "")
                            )
                            
                            # Strategy A: XPath Search in the original full document
                            candidates = main_node.xpath(f"descendant-or-self::*[local-name()='{tag_name}']")
                            if candidates:
                                if is_empty_err:
                                    # STRIKE 1: Only consider nodes that are REALLY empty in the original tree
                                    candidates = [c for c in candidates if not (c.text or "").strip()]
                                elif val_match:
                                    bad_val = val_match.group(1)
                                    val_c = [c for c in candidates if (c.text or "").strip() == bad_val]
                                    if val_c: candidates = val_c
                                
                                # Pick the one closest to our estimate (real_line)
                                if candidates:
                                    best_node = min(candidates, key=lambda c: abs((c.sourceline or 0) - real_line))
                                    found_line = best_node.sourceline

                            # Strategy B: Regex fallback (Regex is often more reliable for empty tags like <Nm/>)
                            if not found_line or (is_empty_err and abs(found_line - real_line) > 1):
                                search_pattern = None
                                if is_empty_err:
                                    # Matches <Nm/> or <Nm>  </Nm>
                                    search_pattern = re.compile(f'<{tag_name}[^>]*/>|<{tag_name}[^>]*>\s*</{tag_name}>')
                                elif val_match:
                                    bad_val = val_match.group(1)
                                    search_pattern = re.compile(f'<{tag_name}[^>]*>\s*{re.escape(bad_val)}\s*</{tag_name}>')
                                
                                if search_pattern:
                                    matches = list(search_pattern.finditer(xml_content))
                                    if matches:
                                        # Pick the match closest to estimated line
                                        est_off = 0
                                        xml_lns = xml_content.splitlines(keepends=True)
                                        for i in range(min(len(xml_lns), int(real_line)-1)):
                                            est_off += len(xml_lns[i])
                                        
                                        best_m = min(matches, key=lambda m: abs(m.start() - est_off))
                                        found_line = xml_content.count('\n', 0, best_m.start()) + 1

                        if found_line:
                            real_line = found_line

                    except Exception as _ex:
                        # Fallback to estimate if correction fails
                        pass

                    # 4. Blank-line guard (Editor highlight helper)
                    try:
                        xml_lines = xml_content.splitlines()
                        ln = int(real_line) - 1
                        if 0 <= ln < len(xml_lines) and not xml_lines[ln].strip():
                            for offset in range(1, 10):
                                prev = ln - offset
                                if prev >= 0 and xml_lines[prev].strip():
                                    real_line = prev + 1
                                    break
                    except Exception:
                        pass

                    effective_layer = 2
                    # If this is a scheme identifier error, treat it as Layer 3 Business Rule
                    if "SchmeNm" in error.message or "Invalid data" in friendly_msg or "<Cd>" in friendly_msg:
                        effective_layer = 3
                    
                    node_path = "/"
                    try:
                        if best_node is not None:
                            node_path = self._get_xpath_for_element(best_node)
                        elif tag_match:
                            raw_tag = tag_match.group(1)
                            tag_name = raw_tag.split('}')[-1] if '}' in raw_tag else raw_tag
                            node_path = f"//{tag_name}"
                    except:
                        pass

                    issues.append(ValidationIssue("ERROR", effective_layer, "SCHEMA_VAL", node_path, friendly_msg.strip(), suggestion, line=real_line))
        except etree.XMLSyntaxError as e:
            issues.append(ValidationIssue("ERROR", 2, "XML_SYNTAX", "/", f"XML Markup Error: {str(e)}", line=getattr(e, "lineno", None)))
        except Exception as e:
            issues.append(ValidationIssue("ERROR", 2, "VAL_ERR", "/", f"Internal Layer 2 Error: {str(e)}"))
        return issues

    def _validate_header_node(self, app_hdr_node, xml_content) -> list:
        """XSD-validate ONE Business Application Header (<AppHdr>) and return its
        issues. Called once per message in a bulk so a broken header on the 2nd+
        message is caught too. Never raises."""
        issues = []
        try:
            if app_hdr_node is not None:
                h_line_offset = app_hdr_node.sourceline or 1
                h_ns = etree.QName(app_hdr_node).namespace or ""
                h_type = self.config.get("validation_rules", {}).get("default_header_type", "head.001.001.01")
                partial_ns = self.config.get("validation_rules", {}).get("header_namespace_partial", "head.001.001")
                
                if partial_ns in h_ns:
                    h_type = h_ns.split(":")[-1]
                
                h_path = self._get_xsd_path(h_type)
                if h_path:
                    try:
                        # 1. Prepare clean header for validation (Deepcopy to keep lines)
                        h_val_doc = copy.deepcopy(app_hdr_node)

                        # Cache header XSD parsing too — same hot-path optimization
                        # as the payload XSD above.
                        h_cached = Layer2Mixin._xsd_schema_cache.get(h_path)
                        if h_cached is None:
                            h_xsd_raw = etree.parse(h_path)
                            h_schema = etree.XMLSchema(h_xsd_raw)
                            Layer2Mixin._xsd_schema_cache[h_path] = (h_xsd_raw, h_schema)
                        else:
                            h_xsd_raw, h_schema = h_cached
                        h_xsd_ns = h_xsd_raw.getroot().get("targetNamespace")

                        #  Build tag_info from the HEAD.001 XSD (NOT payload XSD)
                        # This ensures CharSet is seen as optional and Fr as mandatory
                        h_tag_info = self._build_tag_info_from_xsd(h_path)
                        
                        if h_xsd_ns and h_ns != h_xsd_ns:
                            self._mask_namespace_in_place(h_val_doc, h_xsd_ns)

                        # 2. Validate
                        h_schema.assertValid(h_val_doc)
                    except etree.DocumentInvalid as deh:
                        # Collect libxml2's own header errors first. libxml2 stops at
                        # the FIRST content-model violation, so on a missing Fr it only
                        # complains that the NEXT element "is not expected" — hiding any
                        # other missing mandatory fields.
                        hdr_libxml_issues = []
                        for error in deh.error_log:
                            # 1. Simplify
                            friendly_msg, suggestion = self._simplify_error_message(error.message, h_tag_info, xml_content=xml_content)

                            # 2. Estimate
                            h_real_line = h_line_offset + error.line - 1
                            best_n = None

                            # 3. High-Precision correction for AppHdr
                            try:
                                tag_m = re.search(r"Element '([^']+)'", error.message)
                                val_m = re.search(r"[Vv]alue '([^']*)'", error.message)

                                if tag_m:
                                    t_full = tag_m.group(1)
                                    t_name = t_full.split('}')[-1] if '}' in t_full else t_full

                                    is_empty_h = "Empty" in friendly_msg or "minLength" in error.message

                                    # Find in AppHdr specifically
                                    h_candidates = app_hdr_node.xpath(f"descendant-or-self::*[local-name()='{t_name}']")
                                    if h_candidates:
                                        if is_empty_h:
                                            h_candidates = [c for c in h_candidates if not (c.text or "").strip()]

                                        if h_candidates:
                                            best_n = min(h_candidates, key=lambda c: abs((c.sourceline or 0) - h_real_line))
                                            h_real_line = best_n.sourceline
                            except:
                                pass

                            h_node_path = "/AppHdr"
                            try:
                                if best_n is not None:
                                    h_node_path = self._get_xpath_for_element(best_n)
                                elif tag_m:
                                    t_full = tag_m.group(1)
                                    t_name = t_full.split('}')[-1] if '}' in t_full else t_full
                                    h_node_path = f"/AppHdr/{t_name}"
                            except:
                                pass

                            hdr_libxml_issues.append(ValidationIssue("ERROR", 2, "HEADER_VAL", h_node_path, friendly_msg, suggestion, line=h_real_line))

                        # Completeness scan — report EVERY missing mandatory direct
                        # child of AppHdr at once, so the user sees all of them in a
                        # single pass instead of one-per-revalidation.
                        present_children = {etree.QName(c.tag).localname for c in app_hdr_node
                                            if isinstance(c.tag, str)}
                        hdr_missing_issues = []
                        for tag in self._mandatory_header_children(h_path):
                            if tag not in present_children:
                                label = (h_tag_info.get(tag, {}) or {}).get('label', tag)
                                hdr_missing_issues.append(ValidationIssue(
                                    "ERROR", 2, "HEADER_VAL", f"/AppHdr/{tag}",
                                    f"The mandatory header element '{tag}' is missing from the Business Application Header (AppHdr).",
                                    f"Add a <{tag}> element inside <AppHdr>. {label} is required by the ISO 20022 BAH (head.001) schema.",
                                    line=h_line_offset))

                        if hdr_missing_issues:
                            # The clear per-field "missing" issues supersede libxml2's
                            # confusing single "X is not expected / is expected" error,
                            # so drop those structural complaints but keep genuine
                            # value/format errors on header fields that ARE present.
                            def _is_structural(it):
                                m = (it.message or "").lower()
                                return any(s in m for s in (
                                    "is not expected", "not expected here",
                                    "not expected at this position",
                                    "is expected", "is not complete",
                                    "content is incomplete", "not complete",
                                ))
                            issues.extend(hdr_missing_issues)
                            issues.extend(it for it in hdr_libxml_issues if not _is_structural(it))
                        else:
                            issues.extend(hdr_libxml_issues)
                    except Exception as eh:
                         issues.append(ValidationIssue("WARNING", 2, "HEADER_ERR", "/", f"AppHdr Warning: {str(eh)}"))
        except Exception as _he:
            issues.append(ValidationIssue("WARNING", 2, "HEADER_ERR", "/", f"AppHdr check error: {str(_he)}"))
        return issues

    # ──────────────────────────────────────────────────────────────────────────
    # DYNAMIC XSD TAG ANALYSER
    # Reads any ISO 20022 XSD at runtime and builds a rich tag dictionary.
    # Result is cached so each XSD is only parsed once per server session.
    # ──────────────────────────────────────────────────────────────────────────

    def _camel_to_words(self, name: str) -> str:
        """Convert ISO 20022 CamelCase names to readable English words."""
        # Strip trailing version digits (e.g. GroupHeader131 → GroupHeader)
        name = re.sub(r'\d+$', '', name)
        # Split: uppercase letters that start a new word
        words = re.findall(r'[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|$)|[a-z]+|[A-Z]', name)
        return ' '.join(words) if words else name

    def _build_tag_info_from_xsd(self, xsd_path: str) -> dict:
        """
        Dynamically parse an XSD file and return a dict:
          { element_name: { 'label': str, 'mandatory': bool, 'repeatable': bool } }
        Cached per xsd_path so it runs only once per XSD file.
        """
        if xsd_path in Layer2Mixin._xsd_tag_cache:
            return Layer2Mixin._xsd_tag_cache[xsd_path]

        tag_info: dict = {}
        XS = 'http://www.w3.org/2001/XMLSchema'

        try:
            tree  = etree.parse(xsd_path)
            root  = tree.getroot()

            for elem in root.iter(f'{{{XS}}}element'):
                name = elem.get('name')
                if not name:
                    continue

                min_occ = elem.get('minOccurs', '1')  # default is '1' = mandatory
                max_occ = elem.get('maxOccurs', '1')
                type_nm = elem.get('type', name)       # fallback to element name

                is_mandatory  = min_occ != '0'
                is_repeatable = (max_occ == 'unbounded' or
                                 (max_occ.isdigit() and int(max_occ) > 1))

                # Generate plain-English label from the xs:complexType or xs:simpleType
                # that is referenced by this element — more descriptive than the tag alone
                label = self._camel_to_words(type_nm) if type_nm else self._camel_to_words(name)

                if name not in tag_info:  # initialize
                    tag_info[name] = {
                        'label':      label,
                        'mandatory':  is_mandatory,
                        'repeatable': is_repeatable,
                        'min':        min_occ,
                        'max':        max_occ,
                    }
                else:
                    # If this tag appears elsewhere in the XSD with a higher maxOccurs,
                    # we must respect the highest limit to avoid false-positive duplicate errors
                    # in our flat dictionary lookup.
                    curr_max = tag_info[name]['max']
                    if curr_max != 'unbounded':
                        if max_occ == 'unbounded':
                            tag_info[name]['max'] = 'unbounded'
                            tag_info[name]['repeatable'] = True
                        elif max_occ.isdigit() and curr_max.isdigit() and int(max_occ) > int(curr_max):
                            tag_info[name]['max'] = max_occ
                            tag_info[name]['repeatable'] = True
        except Exception as ex:
            print(f'[DEBUG] XSD tag parse failed: {ex}')

        Layer2Mixin._xsd_tag_cache[xsd_path] = tag_info
        return tag_info

    def _mask_namespace_in_place(self, element, new_ns: str):
        """Recursively updates the tag of an element and its children to use a new namespace."""
        if not isinstance(element.tag, str):
            return
        local = etree.QName(element).localname
        element.tag = f"{{{new_ns}}}{local}"
        for child in element:
            self._mask_namespace_in_place(child, new_ns)

    def _simplify_error_message(self, message: str, tag_info: dict = None, xml_content: str = None, error_line: int = None) -> tuple:
        """
        Dynamic Translation Engine: Converts technical XSD/lxml jargon into
        clear, human-readable, actionable English for end users.
        Uses tag_info (built from the actual XSD) for context-aware messages.
        Works for ALL ISO 20022 MX message types dynamically.
        """
        # Strip namespace URIs like {urn:iso:...:xsd:...} from message
        msg = re.sub(r'\{[^}]+\}', '', message)
        if tag_info is None:
            tag_info = {}

        # ── Static Tag Name Dictionary (Cross-Family coverage) ────────────
        # Priority 3 fallback after: 1. live XSD label, 2. this dict, 3. CamelCase split
        # Covers all major ISO 20022 MX message families dynamically.
        _STATIC_TAG_NAMES = {
            # ── COMMON ACROSS ALL FAMILIES ──────────────────────────────────
            "GrpHdr":       "Group Header",
            "MsgId":        "Message ID",
            "CreDtTm":      "Creation Date & Time",
            "CreDt":        "Creation Date",
            "NbOfTxs":      "Number of Transactions",
            "CtrlSum":      "Control Sum",
            "Id":           "Identification",
            "Nm":           "Name",
            "Cd":           "Code",
            "Prtry":        "Proprietary",
            "Issr":         "Issuer",
            "Ref":          "Reference",
            "Dt":           "Date",
            "Amt":          "Amount",
            "Ccy":          "Currency",
            "Ctry":         "Country",
            "Tp":           "Type",
            "Sts":          "Status",
            "Rsn":          "Reason",
            "Inf":          "Information",
            "AddtlInf":     "Additional Information",
            "Fr":           "From",
            "To":           "To",
            "OrgnlMsgId":   "Original Message ID",
            # ── PARTIES ─────────────────────────────────────────────────────
            "Dbtr":         "Debtor",
            "Cdtr":         "Creditor",
            "UltmtDbtr":    "Ultimate Debtor",
            "UltmtCdtr":    "Ultimate Creditor",
            "InitgPty":     "Initiating Party",
            "CdtrAgt":      "Creditor Agent",
            "DbtrAgt":      "Debtor Agent",
            "InstgAgt":     "Instructing Agent",
            "InstdAgt":     "Instructed Agent",
            "IntrmyAgt1":   "Intermediary Agent 1",
            "IntrmyAgt2":   "Intermediary Agent 2",
            "IntrmyAgt3":   "Intermediary Agent 3",
            "PrvsInstgAgt1":"Previous Instructing Agent 1",
            "FwdgAgt":      "Forwarding Agent",
            "Agt":          "Agent",
            "Pty":          "Party",
            # ── FINANCIAL INSTITUTION ───────────────────────────────────────
            "FinInstnId":   "Financial Institution Identification",
            "BICFI":        "BIC (Financial Institution)",
            "ClrSysMmbId":  "Clearing System Member ID",
            "FIId":         "Financial Institution ID",
            "BrnchId":      "Branch ID",
            "LEI":          "Legal Entity Identifier",
            # ── IDENTIFICATION ───────────────────────────────────────────────
            "OrgId":        "Organisation ID",
            "PrvtId":       "Private ID",
            "BirthDt":      "Date of Birth",
            "CityOfBirth":  "City of Birth",
            "CtryOfBirth":  "Country of Birth",
            "TaxId":        "Tax ID",
            "AnyBIC":       "Any BIC",
            # ── ACCOUNTS ────────────────────────────────────────────────────
            "DbtrAcct":     "Debtor Account",
            "CdtrAcct":     "Creditor Account",
            "DbtrAgtAcct":  "Debtor Agent Account",
            "CdtrAgtAcct":  "Creditor Agent Account",
            "Acct":         "Account",
            "IBAN":         "IBAN",
            "AcctId":       "Account ID",
            "AcctOwnr":     "Account Owner",
            "AcctSvcr":     "Account Servicer",
            # ── ADDRESS ─────────────────────────────────────────────────────
            "PstlAdr":      "Postal Address",
            "AdrLine":      "Address Line",
            "StrtNm":       "Street Name",
            "BldgNb":       "Building Number",
            "PstCd":        "Post Code",
            "TwnNm":        "Town Name",
            "CtrySubDvsn":  "Country Sub-Division",
            # ── PAYMENT ─────────────────────────────────────────────────────
            "PmtId":        "Payment Identification",
            "InstrId":      "Instruction ID",
            "EndToEndId":   "End-to-End ID",
            "TxId":         "Transaction ID",
            "UETR":         "Unique End-to-End Transaction Reference",
            "PmtTpInf":     "Payment Type Information",
            "SvcLvl":       "Service Level",
            "LclInstrm":    "Local Instrument",
            "CtgyPurp":     "Category Purpose",
            "Purp":         "Purpose",
            "InstrPrty":    "Instruction Priority",
            "CdtTrfTxInf":  "Credit Transfer Transaction",
            "DrctDbtTxInf": "Direct Debit Transaction",
            "TxInf":        "Transaction Information",
            "IntrBkSttlmAmt": "Interbank Settlement Amount",
            "IntrBkSttlmDt":  "Interbank Settlement Date",
            "InstdAmt":     "Instructed Amount",
            "TtlIntrBkSttlmAmt": "Total Interbank Settlement Amount",
            "XchgRate":     "Exchange Rate",
            "ChrgBr":       "Charge Bearer",
            "ChrgsInf":     "Charges Information",
            # ── SETTLEMENT ──────────────────────────────────────────────────
            "SttlmInf":     "Settlement Information",
            "SttlmMtd":     "Settlement Method",
            "SttlmAcct":    "Settlement Account",
            "SttlmPrty":    "Settlement Priority",
            "SttlmTmIndctn":"Settlement Time Indication",
            "SttlmTmReq":   "Settlement Time Request",
            "ClrSys":       "Clearing System",
            # ── REMITTANCE ──────────────────────────────────────────────────
            "RmtInf":       "Remittance Information",
            "Ustrd":        "Unstructured Remittance",
            "Strd":         "Structured Remittance",
            "CdtrRefInf":   "Creditor Reference Information",
            # ── CAMT (Cash Management) ──────────────────────────────────────
            "Stmt":         "Statement",
            "Ntry":         "Entry",
            "NtryDtls":     "Entry Details",
            "TxDtls":       "Transaction Details",
            "Bal":          "Balance",
            "Acct":         "Account",
            "AcctRpt":      "Account Report",
            "Rpt":          "Report",
            "FlrLmt":       "Floor Limit",
            "LqdtyMgmt":    "Liquidity Management",
            "Lmt":          "Limit",
            "ReqdAmt":      "Requested Amount",
            "ReqdLqdtyTfr": "Requested Liquidity Transfer",
            # ── PAIN (Payment Initiation) ────────────────────────────────────
            "PmtInf":       "Payment Information",
            "PmtMtd":       "Payment Method",
            "NbOfTxs":      "Number of Transactions",
            "ReqdExctnDt":  "Requested Execution Date",
            "ReqdColltnDt": "Requested Collection Date",
            "CdtTrfTxInf":  "Credit Transfer Transaction",
            "DrctDbtTxInf": "Direct Debit Transaction",
            "DrctDbtTx":    "Direct Debit Transaction",
            "MndtRltdInf":  "Mandate Related Information",
            "MndtId":       "Mandate ID",
            # ── PACS (Payment Clearing & Settlement) ────────────────────────
            "OrgnlGrpInfAndSts": "Original Group Information & Status",
            "OrgnlMsgNmId": "Original Message Name ID",
            "OrgnlCreDtTm": "Original Creation Date & Time",
            "TxInfAndSts":  "Transaction Information & Status",
            "OrgnlEndToEndId": "Original End-to-End ID",
            "OrgnlTxId":    "Original Transaction ID",
            "TxSts":        "Transaction Status",
            "StsRsnInf":    "Status Reason Information",
            "AddtlTxInf":   "Additional Transaction Information",
            # ── SESE (Securities Settlement) ────────────────────────────────
            "TradDtls":     "Trade Details",
            "FinInstrmId":  "Financial Instrument ID",
            "ISIN":         "ISIN",
            "PlcOfTrad":    "Place of Trade",
            "SttlmParams":  "Settlement Parameters",
            "DlvrgSttlmPties": "Delivering Settlement Parties",
            "RcvgSttlmPties":  "Receiving Settlement Parties",
            "SfkpgAcct":    "Safekeeping Account",
            # ── REGULATORY ──────────────────────────────────────────────────
            "RgltryRptg":   "Regulatory Reporting",
            "Tax":          "Tax",
            "TaxRcrd":      "Tax Record",
            "TaxAmt":       "Tax Amount",
            # ── head.001 APPLICATION HEADER ─────────────────────────────────
            "BizMsgIdr":    "Business Message Identifier",
            "MsgDefIdr":    "Message Definition Identifier",
            "BizSvc":       "Business Service",
            "CharSet":      "Character Set",
            "Sgntr":        "Signature",
            "DplctRef":     "Duplicate Reference",
            "Prty":         "Priority",
        }

        def tag_label(tag: str) -> str:
            """Returns 'TagName (Full English Name)' using 3-tier lookup:
            1. Live XSD-derived label  2. Static dict  3. CamelCase split"""
            # 1. Live XSD info (most accurate — derived from the actual validated XSD)
            if tag in tag_info:
                lbl = tag_info[tag].get('label')
                # Only use it if it adds value (not just the tag name itself)
                if lbl and lbl.strip().lower() != tag.strip().lower():
                    return f"{tag} ({lbl})"

            # 2. Static fallback table (covers common abbreviations across all families)
            full = _STATIC_TAG_NAMES.get(tag)
            if full:
                return f"{tag} ({full})"

            # 3. CamelCase split — skip if tag is too short to be meaningful
            if len(tag) > 2:
                words = self._camel_to_words(tag)
                if words and words.lower() != tag.lower():
                    return f"{tag} ({words})"

            return tag  # Return as-is for very short tags like 'Fr', 'To', 'Id'

        def tag_mandatory(tag: str) -> bool:
            """Returns True if the XSD says this tag is mandatory."""
            clean = tag.split('}')[-1] if '}' in tag else tag
            return tag_info.get(clean, {}).get('mandatory', True)

        def tag_repeatable(tag: str) -> bool:
            """Returns True if the XSD allows this tag to repeat."""
            clean = tag.split('}')[-1] if '}' in tag else tag
            return tag_info.get(clean, {}).get('repeatable', False)

        # ── Helper: extract element name ──────────────────────────────────
        def elem_name(default="A field"):
            m = re.search(r"Element '([^']+)'", msg)
            if not m: return default
            raw = m.group(1)
            return raw.split('}')[-1] if '}' in raw else raw

        def attr_name(default="attribute"):
            m = re.search(r"[Aa]ttribute '([^']+)'", msg)
            if not m: return default
            raw = m.group(1)
            return raw.split('}')[-1] if '}' in raw else raw

        def bad_value(default=""):
            m = re.search(r"(?:[Vv]alue|The\s+value)\s+'([^']*)'", msg)
            if m:
                val = m.group(1)
                # If the matched value is a small digit and there are other quotes, keep searching.
                # BUT if it's alphanumeric and matches 'AAA' or 'ID123', return it immediately!
                if not (val.isdigit() and len(val) < 4):
                     return val
            
            # 1b. Enhanced: Look for "'...' is not" pattern
            m2 = re.search(r"'([^']*)' (?:is not valid|is not a valid value|is either too|exceeds|fails to occur)", msg, re.IGNORECASE)
            if m2: return m2.group(1)

            # 2. Secondary: Advanced Quote Filtering (PRIORITIZE USER DATA)
            quotes = re.findall(r"'([^']*)'", msg)
            if quotes:
                tn_m = re.search(r"Element '([^']+)'", msg)
                tag_name = tn_m.group(1).split('}')[-1] if tn_m else ""
                
                # Technical metadata to ignore
                technical = {
                    'facet', 'enumeration', 'maxLength', 'minLength', 'pattern', 'base', 'type', 
                    'atomic', 'element', 'attribute', 'length', 'exceeds', 'allowed', 
                    'maximum', 'Identifier', 'value', 'data', 'tag', 'field', tag_name
                }
                
                candidates = []
                for q in quotes:
                    clean = q.split('}')[-1] if '}' in q else q
                    if clean not in technical and clean.lower() not in technical:
                        candidates.append(q)
                
                if candidates:
                    # Prefer strings containing letters (user data) over purely numeric ones (facets)
                    words = [c for c in candidates if any(x.isalpha() for x in c)]
                    if words:
                         # Filter out known lxml words that might be quoted in some versions
                         filtered_words = [w for w in words if w.lower() not in technical]
                         
                         # NEW: If we are in a 'Cd' context, ignore the standard valid codes!
                         # This prevents picking up 'LEI' or 'CUST' from the "Expected" list in the error msg.
                         if tag_name == "Cd":
                             standard_valid = {"LEI", "CUST", "BANK", "TXID", "COID", "TXNR", "DUNS", "GIIN"}
                             best_bad = [w for w in filtered_words if w.upper() not in standard_valid]
                             if best_bad:
                                 # Pick the one that appears EARLIEST in the msg - the bad value is usually first
                                 best_bad.sort(key=lambda x: msg.find(f"'{x}'"))
                                 return best_bad[0]

                         if filtered_words:
                             # Still prefer earlier occurrences
                             filtered_words.sort(key=lambda x: msg.find(f"'{x}'"))
                             return filtered_words[0]
                    
                    # --- LINE-AWARE XML FALLBACK ---
                    # If extraction is failing (e.g. only numbers found), use the error line to get the value
                    if tag_name == "Cd" and xml_content and error_line:
                        lines = xml_content.splitlines()
                        # Adjust for 0-indexing and search slightly around the line (lxml lines can be +/- 1)
                        for idx in [error_line-1, error_line, error_line-2]:
                            if 0 <= idx < len(lines):
                                line_text = lines[idx]
                                # Match <Cd>Passport</Cd> or <Cd>Passport  </Cd>
                                xml_m = re.search(f"<{tag_name}[^>]*>([^<]+)</{tag_name}>", line_text)
                                if xml_m:
                                    val = xml_m.group(1).strip()
                                    if val: return val

                    return max(candidates, key=len)

            # 3. Fallback: first quote after a colon
            m = re.search(r":\s*'([^']*)'", msg)
            if m: return m.group(1)
            
            return default

        # ── 1. EMPTY MANDATORY FIELD ──────────────────────────────────────
        raw_val = bad_value(default="___NOT_EMPTY___")
        is_truly_empty = (
            raw_val == "" or 
            "The value ''" in msg or 
            'The value ""' in msg or 
            "value '' is not accepted" in msg or
            "value is ''" in msg
        )
        
        if is_truly_empty:
            name = elem_name("A required field")
            # Specific hint for scheme names inside this block too
            if name == "Cd" and ("SchmeNm" in msg or "Othr" in msg or "OrgId" in msg or "Id" in msg.upper()):
                 return (
                    f"Missing or empty identifier code in <{name}>.",
                    "This tag is required. Please provide a valid code such as 'LEI', 'CUST', or 'BANK'."
                 )

            return (
                f" Empty elements found in '{name}'",
                f"The field '{name}' cannot be left blank. Please enter a valid value before submitting."
            )

        # ── 2. BIC / BICFI ────────────────────────────────────────────────
        if any(x in msg.upper() for x in ["BICFI", "BICBE", "ANYBIC", "BIC"]):
            bic_val = bad_value().strip()  # strip whitespace — XSD reports raw element text incl. newlines
            if "pattern" in msg.lower() or "facet" in msg.lower():
                # Whitespace-only difference: stripped value is a valid BIC but raw had whitespace
                _BIC_PAT = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
                if bic_val and _BIC_PAT.match(bic_val):
                    return (
                        f"BIC '{bic_val}' contains leading/trailing whitespace.",
                        f"Remove the whitespace inside the <BICFI> element. The value should be exactly '{bic_val}' with no spaces or newlines."
                    )
                if bic_val and len(bic_val) >= 6 and not (bic_val[4].isalpha() and bic_val[5].isalpha()):
                    return (
                        f"Invalid BIC — bad country code: '{bic_val[4:6]}'.",
                        f"Characters 5–6 of a BIC must be a valid 2-letter ISO country code (e.g. 'GB', 'US', 'DE'). "
                        f"Found '{bic_val[4:6]}' in BIC '{bic_val}'. Correct this before resubmitting."
                    )
                if bic_val and len(bic_val) not in (8, 11):
                    return (
                        f"BIC '{bic_val}' has wrong length ({len(bic_val)} chars).",
                        "A valid BIC must be exactly 8 or 11 characters: "
                        "4-char Bank Code + 2-letter Country + 2-char Location + optional 3-char Branch (e.g. BNKGB2LXXX)."
                    )
                return (
                    f"Invalid BIC code format: '{bic_val}'.",
                    "The BIC must follow ISO 9362: 4-letter bank code, 2-letter country code, 2-char location code, "
                    "and optional 3-char branch code. Example: 'BNKGB2LXXX'."
                )
            if "length" in msg.lower() or "atomic type" in msg.lower():
                if bic_val and re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$').match(bic_val):
                    return (
                        f"BIC '{bic_val}' contains leading/trailing whitespace.",
                        f"Remove the whitespace inside the <BICFI> element. The value should be exactly '{bic_val}' with no spaces or newlines."
                    )
                return (
                    "BIC has incorrect length.",
                    "A BIC (Bank Identifier Code) must be exactly 8 or 11 characters long."
                )

        # ── 3. CURRENCY CODE (attribute) ──────────────────────────────────
        an = attr_name()
        if an.lower() in ("ccy", "currency") or "currency" in msg.lower():
            ccy_val = bad_value()
            if ccy_val:
                return (
                    f"Invalid currency code '{ccy_val}'.",
                    f"'{ccy_val}' is not a recognised ISO 4217 currency code. "
                    f"Use a standard 3-letter code such as 'USD', 'EUR', 'GBP', 'JPY', 'INR', 'AED', etc."
                )
            return (
                "Missing currency code (Ccy attribute).",
                "Add a valid ISO 4217 3-letter currency code as an attribute, e.g. Ccy=\"USD\"."
            )

        # ── 4. GENERIC ATTRIBUTE ERRORS ───────────────────────────────────
        if "attribute" in msg.lower():
            aname = attr_name()
            aval  = bad_value()
            if aname and aval:
                return (
                    f"Invalid value '{aval}' for attribute '{aname}'.",
                    f"The attribute '{aname}' does not accept the value '{aval}'. "
                    f"Check the ISO 20022 standard for the list of allowed values for this attribute."
                )
            if aname:
                return (
                    f"Missing required attribute '{aname}'.",
                    f"The attribute '{aname}' is required but not present in this element. "
                    f"For currency amounts, add Ccy=\"USD\" (or the appropriate currency code)."
                )
            return (
                "A required attribute is missing or invalid.",
                "One or more required attributes (such as Ccy for amount fields) are missing or contain an invalid value."
            )

        # ── 5. DUPLICATE ELEMENT ──────────────────────────────────────────
        if "occurs more than allowed" in msg:
            name = elem_name("A field")
            return (
                f"Duplicate field '{name}'.",
                f"The field '{name}' appears more than once in this section, which is not allowed. "
                f"Remove the extra copy and keep only one."
            )

        # ── 6. MISSING TAG or WRONG ORDER ─────────────────────────────────
        if "is not expected" in msg:
            # 1. Resolve 'found' element (the one that triggered the error)
            found_elem = elem_name("field")

            # 2. Extract 'expected' list (e.g. "Expected is (TagA, TagB)")
            expected_str = None
            # Look for "Expected is ( ... )" or similar
            m_exp = re.search(r"expected(?: is)?(?: one of)?\s*\(([^)]+)\)", msg, re.IGNORECASE)
            if not m_exp:
                 # Look for any tag-like word after "expected is"
                 m_exp = re.search(r"expected(?: is)?(?: one of)?\s+['\"]?([\w:]+)['\"]?", msg, re.IGNORECASE)
            
            if m_exp:
                expected_str = m_exp.group(1).strip()

            if expected_str:
                # Possible separators: ',' for sequence, '|' for choice
                all_expected = [t.strip().strip('()').split('}')[-1] for t in re.split(r'[,|]', expected_str)]
                
                # Check for duplication (X found where X was expected)
                is_dupe = found_elem in all_expected
                if not is_dupe and xml_content:
                    # Generic ISO 20022 element names (Id, Cd, Nm, etc.) are legitimately
                    # reused at different hierarchy levels in the same message.
                    # A global document search always returns > 1 for these, producing
                    # false-positive "Tag <Id> is duplicated" errors (e.g. Assgnmt/Id,
                    # Case/Id, Pty/Id, Othr/Id are all valid and distinct).
                    # Fix: use a ±15-line context window when error_line is available;
                    # only flag as duplicate if the same tag appears twice nearby.
                    _MULTI_LEVEL_TAGS = {
                        # Short generic tags re-used at every hierarchy level
                        'Id', 'Cd', 'Nm', 'Tp', 'Ref', 'Dt', 'Amt', 'Inf',
                        'Issr', 'Sts', 'Rsn', 'Agt', 'Pty', 'Acct', 'Ctry', 'Ccy',
                        # Address-component tags — appear in every party's PstlAdr
                        # (Dbtr / Cdtr / each Agent / UltmtDbtr / UltmtCdtr / etc.)
                        'TwnNm', 'StrtNm', 'BldgNb', 'BldgNm', 'PstCd', 'AdrLine',
                        'CtrySubDvsn', 'TwnLctnNm', 'DstrctNm', 'Flr', 'PstBx', 'Room',
                        'Dept', 'SubDept', 'AdrTp',
                        # Identification tags appearing per party/agent
                        'BICFI', 'AnyBIC', 'LEI', 'MmbId', 'IBAN', 'BBAN',
                        'FinInstnId', 'OrgId', 'PrvtId', 'BrnchId', 'ClrSysMmbId',
                        # Timing / amount sub-tags
                        'CreDtTm', 'IntrBkSttlmDt', 'ReqdExctnDt', 'XchgRate',
                        # Misc multi-level
                        'BizMsgIdr', 'EndToEndId', 'InstrId', 'TxId', 'UETR',
                    }
                    tag_pattern = re.compile(f'<{re.escape(found_elem)}[\\s/>]', re.IGNORECASE)
                    if found_elem in _MULTI_LEVEL_TAGS:
                        if error_line:
                            # Check only within a ±15-line window around the failing element
                            _ctx_lines = xml_content.splitlines()
                            _start = max(0, error_line - 15)
                            _end   = min(len(_ctx_lines), error_line + 15)
                            _window = '\n'.join(_ctx_lines[_start:_end])
                            if len(tag_pattern.findall(_window)) > 1:
                                is_dupe = True
                        # If no error_line available, skip global search to avoid false positives
                    else:
                        # For non-generic tags a global search is reliable
                        if len(tag_pattern.findall(xml_content)) > 1:
                            is_dupe = True

                if is_dupe:
                    label = tag_label(found_elem)
                    return (
                        f"Tag <{found_elem}> is duplicated.",
                        f"The tag <{label}> appears more than once in this section. "
                        f"Remove the extra copy and keep only one."
                    )

                # --- Missing or Wrong Order: user's requested wording ---
                all_expected = [t.strip().strip('()').split('}')[-1] for t in expected_str.split(',')]
                expected_list = "'" + ", ".join(all_expected) + "'"
                
                # Special Conflict: SchmeNm Cd vs Prtry
                if found_elem in ["Cd", "Prtry"] and ("Cd" in all_expected or "Prtry" in all_expected):
                     return (
                         "/OrgId/Othr → Mutually exclusive elements conflict",
                         "You cannot provide both <Cd> and <Prtry> in the same <SchmeNm> block. Please remove one. ISO rules require either a standardized code OR a proprietary name, never both."
                     )

                return (
                    f"The element '{found_elem}' is not expected here. Either it is not allowed in this specification, or another mandatory element is missing before this one. One of the following elements is expected : {expected_list}",
                    f"To fix this, ensure that one of the following elements is present before '{found_elem}': {expected_list}. Review the ISO 20022 schema sequence requirements for this message type."
                )

            # FALLBACK
            return (
                f"The element '{found_elem}' is not expected at this position. Either it is not allowed here or a mandatory field is missing before it.",
                f"Check the ISO 20022 schema for the correct field sequence. Often this happens when you skip a mandatory field."
            )

        # ── 7. MISSING MANDATORY CHILD FIELD ─────────────────────────────
        if any(x in msg for x in ["Missing child element", "content is incomplete", "fails to occur"]):
            m = re.search(r"Element '([^']+)':.*Expected is(?: one of)? \(([^)]+)\)\.", msg)
            if not m:
                m = re.search(r"Element '([^']+)':.*'([^']+)' fails to occur", msg)
            if m:
                parent        = m.group(1).split('}')[-1]
                missing_all   = m.group(2)
                all_missing = [t.strip().strip('()').split('}')[-1] for t in missing_all.split(',')]
                # Quote each expected element individually so the wording matches
                # SWIFT MyStandards format: "expected: 'A', 'B'" (each name quoted).
                expected_list = ", ".join(f"'{t}'" for t in all_missing)

                # Special Case: SchmeNm must have a child
                if parent == "SchmeNm":
                     return (
                         "/OrgId/Othr → <SchmeNm> must contain either <Cd> or <Prtry>",
                         "The <SchmeNm> block is incomplete. It must contain exactly one sub-element: <Cd> for ISO codes or <Prtry> for local identifiers."
                     )

                return (
                    f"The content of element '{parent}' is not complete. One of the following elements is expected: {expected_list}.",
                    f"The element '{parent}' requires specific child elements to be present. Add {expected_list} inside your '{parent}' block to satisfy the ISO 20022 / SWIFT MyStandards schema."
                )
            return (
                "One or more mandatory elements are missing. Review the schema for required fields.",
                "Mandatory elements (child tags) are absent from a container. Check the ISO 20022 standard for which fields are required in this section."
            )

        # ── 8. DATE FORMAT ────────────────────────────────────────────────
        if "date" in msg.lower() and "is not a valid value" in msg.lower():
            dv = bad_value()
            if "datetime" in msg.lower():
                return (
                    f"Invalid date/time value: '{dv}'.",
                    "Date-time fields must use the format YYYY-MM-DDThh:mm:ss+HH:MM with a timezone offset "
                    "(e.g. 2026-11-20T14:30:00+00:00). Ensure the 'T' separator is present and append "
                    "an explicit offset such as +00:00 or +05:30."
                )
            return (
                f"Invalid date value: '{dv}'.",
                "Date fields must use the format YYYY-MM-DD (e.g. 2026-11-20). "
                "Do not include time or timezone information in plain date fields."
            )

        # ── 9. AMOUNT / DECIMAL FORMAT ────────────────────────────────────
        if "decimal" in msg.lower() or ("amount" in msg.lower() and "is not a valid" in msg.lower()):
            av = bad_value()
            return (
                f"Invalid amount value: '{av}'.",
                "Amount fields must contain a plain decimal number without currency symbols or commas "
                "(e.g. 1500.00). Do not add 'USD' or ',' inside the amount tag."
            )

        # ── 10. GENERIC ATOMIC TYPE / DATA FORMAT ─────────────────────────
        if "is not a valid value of the atomic type" in msg or "datatype" in msg:
            tv = bad_value()
            tn_m = re.search(r"atomic type '([^']+)'", msg)
            tn = tn_m.group(1).split('}')[-1] if tn_m else None
            name = elem_name()
            
            # Special case for Scheme Code (Cd)
            if name == "Cd":
                lv = tv
                if lv.isdigit() and len(lv) < 3:
                     # Re-scan for alpha strings
                     qs = re.findall(r"'([^']*)'", msg)
                     ignore = {name, tn, 'maxLength', 'minLength', 'facet', 'length', 'type', 'atomic'}
                     alts = [q for q in qs if any(x.isalpha() for x in q) and q not in ignore and q.lower() not in ignore]
                     if alts: lv = max(alts, key=len)
                
                valid_codes = ["LEI", "CUST", "BANK", "TXID", "COID", "TXNR", "DUNS", "GIIN"]
                invalid_reason = None
                invalid_fix = None
                
                if hasattr(self, 'codelists') and 'schme_nm' in self.codelists:
                    cfg = self.codelists['schme_nm'].get('schmeNm_validation', {})
                    valid_list = cfg.get('valid_cd_codes', [])
                    if valid_list: valid_codes = [c['code'] for c in valid_list]
                    for item in cfg.get('invalid_cd_codes', []):
                        if item['code'].upper() == str(lv).upper():
                            invalid_reason = item['reason']
                            invalid_fix = item.get('fix')
                            break
                
                codes_str = ", ".join(valid_codes)
                if invalid_reason:
                    f_msg = f"The code '{lv}' is not valid for this field because it is {invalid_reason.lower()}."
                    if invalid_fix: f_msg += f" {invalid_fix}."
                    f_msg += f" Recommended codes: {codes_str}."
                    return (f" Invalid data '{lv}' for <{name}>.", f_msg)

                return (
                    f" Invalid data '{lv}' for <{name}>.",
                    f"For this tag, only these approved ISO codes are valid: {codes_str}. "
                    f"Any other value, including '{lv}', is strictly invalid and will cause rejection. "
                    "Ensure the value matches the required data format."
                )

            if tn:
                return (
                    f"Field '{name}' has an invalid value: '{tv}'.",
                    f"This field requires type '{tn}'. Please verify the value '{tv}' matches the expected format."
                )
            
            return (
                f"Invalid value '{tv}' in field '{name}'.",
                "The value does not match the required data type for this field. "
                "Check for extra spaces, wrong number format, or unsupported characters."
            )

        # ── 11. PATTERN / FORMAT MISMATCH ─────────────────────────────────
        if "pattern" in msg.lower() or "is not accepted by the pattern" in msg.lower():
            pv = bad_value()
            name = elem_name()
            field_hints = {
                "IBAN": "An IBAN starts with a 2-letter country code followed by 2 check digits and up to 30 digits (e.g. GB29NWBK60161331926819).",
                "MIC":  "A MIC (Market Identifier Code) must be exactly 4 uppercase letters (e.g. XLON).",
                "LEI":  "An LEI must be exactly 20 alphanumeric characters.",
                "ISIN": "An ISIN must be exactly 12 alphanumeric characters starting with a 2-letter country code.",
                "CBPR_DateTime": "CBPR+ requires a full timezone offset (e.g., +05:30) instead of 'Z'. Use the format: YYYY-MM-DDThh:mm:ss+hh:mm."
            }
            for key, hint in field_hints.items():
                if key in name.upper() or key in msg.upper():
                    return (f"Invalid {key} format: '{pv}'.", hint)
            return (
                f"Invalid format for field '{name}': '{pv}'.",
                f"The value '{pv}' does not match the required format for '{name}'. "
                "Check for illegal characters, wrong length, or an unsupported pattern."
            )

        # ── 12. ENUMERATION (code not in allowed set) ─────────────────────
        if "enumeration" in msg.lower() or "is not an element of the set" in msg.lower() or "not a valid value of the atomic type" in msg.lower():
            ev = bad_value()
            name = elem_name()
            
            # Special case for Scheme Name (SchmeNm)
            if name == "Cd":
                valid_codes = ["LEI", "CUST", "BANK", "TXID", "COID", "TXNR", "DUNS", "GIIN"]
                invalid_reason = None
                invalid_fix = None
                
                # Try to load from codelist
                if hasattr(self, 'codelists') and 'schme_nm' in self.codelists:
                    cfg = self.codelists['schme_nm'].get('schmeNm_validation', {})
                    valid_list = cfg.get('valid_cd_codes', [])
                    if valid_list:
                         valid_codes = [c['code'] for c in valid_list]
                    for item in cfg.get('invalid_cd_codes', []):
                        if item['code'].upper() == str(ev).upper():
                            invalid_reason = item['reason']
                            invalid_fix = item.get('fix')
                            break
                
                codes_str = ", ".join(valid_codes)
                if invalid_reason:
                    f_msg = f"The code '{ev}' is not valid for this field because it is {invalid_reason.lower()}."
                    if invalid_fix: f_msg += f" {invalid_fix}."
                    f_msg += f" Recommended codes: {codes_str}."
                    return (f"Invalid code '{ev}' in <{name}>.", f_msg)
                    
                return (
                    f"Invalid code '{ev}' in <{name}>.",
                    f"The data '{ev}' is not an approved identifier code. Only the following are valid: {codes_str}. "
                    "Everything else is strictly invalid."
                )

            return (
                f"Invalid code '{ev}' for field '{name}'.",
                f"'{ev}' is not a valid code for '{name}'. "
                "This field only accepts specific ISO 20022 codes. "
                "Check the standard for the list of allowed values (e.g. SLEV, SHAR, CRED, DEBT for ChrgBr)."
            )

        # ── 13. LENGTH CONSTRAINT ───────────────────────-─────────────────
        if "length" in msg.lower() and ("maxlength" in msg.lower() or "minlength" in msg.lower() or "facet" in msg.lower()):
            name = elem_name()
            lv = bad_value()
            
            # Double check for Cd fields: verify 'lv' isn't just the numeric facet '8' or '4'
            if name == "Cd" and (lv.isdigit() and len(lv) < 3):
                # 1. Re-scan for alpha strings in message
                qs = re.findall(r"'([^']*)'", msg)
                technical = {name, 'maxLength', 'minLength', 'facet', 'length', 'maximum', 'exceeds', 'allowed', 'value'}
                alts = [q for q in qs if any(x.isalpha() for x in q) and q not in technical and q.lower() not in technical]
                if alts:
                     lv = max(alts, key=len)
                
                # 2. If still a digit, check the XML line directly! (High-Precision Fallback)
                if lv.isdigit() and xml_content and error_line:
                    lines = xml_content.splitlines()
                    for idx in [error_line-1, error_line, error_line-2]:
                        if 0 <= idx < len(lines):
                            xml_m = re.search(f"<{name}[^>]*>([^<]+)</{name}>", lines[idx])
                            if xml_m:
                                lv = xml_m.group(1).strip()
                                break

            # Special case for Scheme Name (Cd) - even length errors should show recommended codes
            if name == "Cd":
                valid_codes = ["LEI", "CUST", "BANK", "TXID", "COID", "TXNR", "DUNS", "GIIN"]
                invalid_reason = None
                invalid_fix = None
                
                if hasattr(self, 'codelists') and 'schme_nm' in self.codelists:
                    cfg = self.codelists['schme_nm'].get('schmeNm_validation', {})
                    valid_list = cfg.get('valid_cd_codes', [])
                    if valid_list: valid_codes = [c['code'] for c in valid_list]
                    for item in cfg.get('invalid_cd_codes', []):
                        if item['code'].upper() == str(lv).upper():
                            invalid_reason = item['reason']
                            invalid_fix = item.get('fix')
                            break
                
                codes_str = ", ".join(valid_codes)
                if invalid_reason:
                    f_msg = f"The code '{lv}' is not valid for this field because it is {invalid_reason.lower()}."
                    if invalid_fix: f_msg += f" {invalid_fix}."
                    f_msg += f" Recommended codes: {codes_str}."
                    return (f" Invalid data '{lv}' for <{name}>.", f_msg)

                return (
                    f" Invalid data '{lv}' for <{name}>.",
                    f"Only the following ISO codes are accepted: {codes_str}. "
                    f"The value '{lv}' is strictly invalid. "
                    "Ensure the code length is within the 4-character limit and exists in the approved list."
                )

            if lv == "" or not lv.strip():
                return (
                    f" Empty elements found in '{name}'",
                    f"The field '{name}' cannot be left blank. Please enter a valid value before submitting."
                )
            m_max = re.search(r"allowed maximum length of '(\d+)'", msg)
            m_min = re.search(r"allowed minimum length of '(\d+)'", msg)
            m_act = re.search(r"length of '(\d+)'", msg)

            # lv may be a length digit extracted from the XSD error (e.g. '179') rather
            # than the actual field text. Try to recover the real value from the XML line.
            if lv.isdigit() and xml_content and error_line:
                lines = xml_content.splitlines()
                for idx in [error_line - 1, error_line, error_line - 2]:
                    if 0 <= idx < len(lines):
                        xml_m = re.search(f"<{re.escape(name)}[^>]*>([^<]+)</{re.escape(name)}>", lines[idx])
                        if xml_m:
                            lv = xml_m.group(1).strip()
                            break

            if m_max and m_act:
                max_len = m_max.group(1)
                act_len = m_act.group(1)
                # If lv is still a pure digit (couldn't find actual text), omit it from message
                if lv.isdigit():
                    return (
                        f"Field '{name}' has an invalid length.",
                        f"This field must be maximum {max_len} characters long. "
                        f"The current value is {act_len} characters long, which exceeds the limit by "
                        f"{int(act_len) - int(max_len)} characters. Please shorten the value."
                    )
                return (
                    f"Field '{name}' has an invalid length.",
                    f"This field must be maximum {max_len} characters long. "
                    f"The current value is {act_len} characters long. Please shorten it."
                )
            elif m_min and m_act:
                min_len = m_min.group(1)
                act_len = m_act.group(1)
                if lv.isdigit():
                    return (
                        f"Field '{name}' has an invalid length.",
                        f"This field must be at least {min_len} characters long. "
                        f"The current value is only {act_len} characters long."
                    )
                return (
                    f"Field '{name}' has an invalid length.",
                    f"This field must be at least {min_len} characters long. "
                    f"The current value is only {act_len} characters long."
                )

            return (
                f"Field '{name}' has an invalid length: '{lv}'.",
                f"The value '{lv}' is either too long or too short for '{name}'. "
                "Check the maximum and minimum character length allowed by the schema."
            )

        # ── 14. ELEMENT NOT ALLOWED / MISPLACED ───────────────────────────
        if "not allowed here" in msg or "not permitted" in msg.lower():
            name = elem_name()
            return (
                f"Field '{name}' is not allowed in this location.",
                f"'{name}' cannot appear at this position in the message. "
                "Remove it or move it to the correct parent element as per the ISO 20022 schema."
            )

        # ── 15. SMART FALLBACK ────────────────────────────────────────────
        clean = msg
        clean = re.sub(r"Element '([^']+)':", r"Field '\1':", clean)
        clean = re.sub(r"\[facet '[^']+'\]\s*", "", clean)
        clean = re.sub(r"is not accepted by the pattern '[^']+'\.?", "has an invalid format.", clean)
        clean = re.sub(r"is not a valid value of the atomic type '[^']+'\.?", "contains an invalid value.", clean)
        clean = re.sub(r"is not an element of the set \{[^}]+\}\.?", "contains an invalid code.", clean)
        clean = clean.strip()

        return (
            f"Validation error: {clean}",
            "Please review this field. Ensure the value follows the ISO 20022 standard format, "
            "uses allowed codes, and does not contain unsupported characters."
        )


