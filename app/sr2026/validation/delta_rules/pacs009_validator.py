import json
import os
import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class Pacs009Validator:
    _rules = None

    @classmethod
    def _load_rules(cls):
        if cls._rules is None:
            rules_path = os.path.join(
                os.path.dirname(__file__),
                "..", "..", "rules", "messages", "pacs.009", "base.json"
            )
            with open(rules_path, "r", encoding="utf-8") as f:
                cls._rules = json.load(f)
        return cls._rules

    @classmethod
    def _is_adv(cls, root_element: etree._Element, message_type: str) -> bool:
        mt = message_type.lower()
        if "_adv" in mt or "pacs.009.adv" in mt or "pacs.009_adv" in mt:
            return True
        biz_svc = root_element.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
        if biz_svc:
            val = (biz_svc[0].text or "").strip()
            if val in ("swift.cbprplus.adv.04", "swift.cbprplus.03"):
                return True
        return bool(root_element.xpath("//*[local-name()='UndrlygFITxInf']"))

    @classmethod
    def _is_cov(cls, root_element: etree._Element, message_type: str) -> bool:
        mt = message_type.lower()
        if "_cov" in mt or "pacs.009.cov" in mt or "pacs.009_cov" in mt:
            return True
        biz_svc = root_element.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
        if biz_svc and (biz_svc[0].text or "").strip() in ("swift.cbprplus.cov.04", "swift.cbprplus.cov.02"):
            return True
        return bool(root_element.xpath("//*[local-name()='UndrlygCstmrCdtTrf']"))

    @classmethod
    def validate(cls, root_element: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.009" not in message_type.lower():
            return

        # ADV and COV have dedicated validators — skip CORE base rules for them
        if cls._is_adv(root_element, message_type):
            return
        if cls._is_cov(root_element, message_type):
            return

        rules = cls._load_rules()

        def _text(node) -> str:
            return (node.text or "").strip()

        # 1. Header Rules
        app_hdr_nodes = root_element.xpath("//*[local-name()='AppHdr']")
        if app_hdr_nodes:
            app_hdr = app_hdr_nodes[0]
            for r in rules.get("headerRules", []):
                if r.get("type") == "FIXED_VALUE":
                    field_path = r["field"].split("/")[-1]
                    nodes = app_hdr.xpath(f"*[local-name()='{field_path}']")
                    if nodes:
                        val = _text(nodes[0])
                        if val != r["expected"]:
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//AppHdr/{field_path}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))
                elif r.get("type") == "ENUM":
                    # Header ENUM rules (e.g. HDR001 BizSvc allowed-value set, CpyDplct) were
                    # previously skipped — only FIXED_VALUE/MATCH were handled here — so a wrong
                    # BizSvc on pacs.009 CORE passed silently. Mirror the groupHeaderRules ENUM path.
                    field_path = r["field"].split("/")[-1]
                    nodes = app_hdr.xpath(f"*[local-name()='{field_path}']")
                    if nodes:
                        val = _text(nodes[0])
                        if val not in r["allowedValues"]:
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//AppHdr/{field_path}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))
                elif r.get("type") == "MATCH":
                    field1 = r["field1"].split("/")[-1] # BizMsgIdr
                    field1_nodes = app_hdr.xpath(f"*[local-name()='{field1}']")
                    
                    grp_hdr_nodes = root_element.xpath("//*[local-name()='GrpHdr']")
                    if field1_nodes and grp_hdr_nodes:
                        val1 = _text(field1_nodes[0])
                        field2 = r["field2"].split("/")[-1] # MsgId
                        field2_nodes = grp_hdr_nodes[0].xpath(f"*[local-name()='{field2}']")
                        if field2_nodes:
                            val2 = _text(field2_nodes[0])
                            if val1 != val2:
                                report.add_issue(ValidationIssue(
                                    severity=r["severity"],
                                    code=r["errorCode"],
                                    path=f"//GrpHdr/{field2}",
                                    message=r["errorMessage"],
                                    line=field2_nodes[0].sourceline or 1,
                                    fix=r.get("fixSuggestion", ""),
                                ))

        # 2. Group Header Rules
        grp_hdr_nodes = root_element.xpath("//*[local-name()='GrpHdr']")
        if grp_hdr_nodes:
            grp_hdr = grp_hdr_nodes[0]
            for r in rules.get("groupHeaderRules", []):
                if r.get("type") == "FIXED_VALUE":
                    field_path = r["field"].split("/")[-1]
                    nodes = grp_hdr.xpath(f"*[local-name()='{field_path}']")
                    if nodes:
                        val = _text(nodes[0])
                        if val != r["expected"]:
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//GrpHdr/{field_path}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))
                elif r.get("type") == "ENUM":
                    field_parts = r["field"].split("/")
                    # GrpHdr/SttlmInf/SttlmMtd -> SttlmInf/SttlmMtd
                    xpath_expr = "/".join(f"*[local-name()='{p}']" for p in field_parts[1:])
                    nodes = grp_hdr.xpath(xpath_expr)
                    if nodes:
                        val = _text(nodes[0])
                        if val not in r["allowedValues"]:
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//GrpHdr/{'/'.join(field_parts[1:])}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))

        # 3. Transaction Rules
        tx_blocks = root_element.xpath("//*[local-name()='CdtTrfTxInf']")
        for tx in tx_blocks:
            src = tx.sourceline or 1
            for r in rules.get("transactionRules", []):
                field_parts = r["field"].split("/")
                xpath_expr = "/".join(f"*[local-name()='{p}']" for p in field_parts[1:])
                nodes = tx.xpath(xpath_expr)
                
                if r.get("mandatory") and not nodes:
                    report.add_issue(ValidationIssue(
                        severity=r["severity"],
                        code=r["errorCode"],
                        path=f"//CdtTrfTxInf/{'/'.join(field_parts[1:])}",
                        message=r["errorMessage"],
                        line=src,
                        fix=r.get("fixSuggestion", ""),
                    ))
                    continue
                
                if nodes:
                    val = _text(nodes[0])
                    if r.get("type") == "UUID_V4":
                        if not re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', val, re.I):
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//CdtTrfTxInf/{'/'.join(field_parts[1:])}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))
                    elif r.get("type") == "DATE":
                        # Check format
                        if r.get("format") == "yyyy-MM-dd":
                            if not re.match(r'^\d{4}-\d{2}-\d{2}$', val):
                                report.add_issue(ValidationIssue(
                                    severity=r["severity"],
                                    code=r["errorCode"],
                                    path=f"//CdtTrfTxInf/{'/'.join(field_parts[1:])}",
                                    message=r["errorMessage"],
                                    line=nodes[0].sourceline or 1,
                                    fix=r.get("fixSuggestion", ""),
                                ))
                    elif r.get("type") == "AMOUNT":
                        try:
                            fval = float(val)
                            if r.get("positiveOnly") and fval <= 0:
                                raise ValueError("Must be positive")
                            if "." in val:
                                int_part, dec_part = val.split(".")
                                if len(dec_part) > r.get("fractionDigits", 5):
                                    raise ValueError("Too many decimals")
                                if len(int_part) + len(dec_part) > r.get("maxDigits", 14):
                                    raise ValueError("Too many digits")
                            else:
                                if len(val) > r.get("maxDigits", 14):
                                    raise ValueError("Too many digits")
                        except ValueError:
                            report.add_issue(ValidationIssue(
                                severity=r["severity"],
                                code=r["errorCode"],
                                path=f"//CdtTrfTxInf/{'/'.join(field_parts[1:])}",
                                message=r["errorMessage"],
                                line=nodes[0].sourceline or 1,
                                fix=r.get("fixSuggestion", ""),
                            ))

        # 4. Address Rules
        for r in rules.get("addressRules", []):
            field = r["field"]
            nodes = root_element.xpath(f"//*[local-name()='{field}']")
            for node in nodes:
                for mand in r.get("mandatoryFields", []):
                    if not node.xpath(f"*[local-name()='{mand}']"):
                        report.add_issue(ValidationIssue(
                            severity=r["severity"],
                            code=r["errorCode"],
                            path=f"//{field}/{mand}",
                            message=r["errorMessage"],
                            line=node.sourceline or 1,
                            fix=r.get("fixSuggestion", ""),
                        ))

        # 5. BIC Rules
        for r in rules.get("bicRules", []):
            field_parts = r["field"].split("/")
            xpath_expr = "//" + "/".join(f"*[local-name()='{p}']" for p in field_parts)
            nodes = root_element.xpath(xpath_expr)
            
            # Since mandatory, we could check if parent exists then this must exist.
            # But the rule says "Invalid instructing agent BIC" so maybe we just check format if it exists.
            parent_xpath = "//" + "/".join(f"*[local-name()='{p}']" for p in field_parts[:-2])
            parents = root_element.xpath(parent_xpath)
            for parent in parents:
                child_nodes = parent.xpath(f"*[local-name()='{field_parts[-2]}']/*[local-name()='{field_parts[-1]}']")
                if not child_nodes:
                    if r.get("mandatory"):
                        report.add_issue(ValidationIssue(
                            severity=r["severity"],
                            code=r["errorCode"],
                            path="//" + "/".join(field_parts),
                            message=r["errorMessage"],
                            line=parent.sourceline or 1,
                            fix=r.get("fixSuggestion", ""),
                        ))
                else:
                    val = _text(child_nodes[0])
                    # basic BICFI regex
                    if not re.match(r'^[A-Z]{6}[A-Z2-9][A-NP-Z0-9]([A-Z0-9]{3})?$', val):
                        report.add_issue(ValidationIssue(
                            severity=r["severity"],
                            code=r["errorCode"],
                            path="//" + "/".join(field_parts),
                            message=r["errorMessage"],
                            line=child_nodes[0].sourceline or 1,
                            fix=r.get("fixSuggestion", ""),
                        ))

        # 6. CBPR+ Rules
        for r in rules.get("cbprPlusRules", []):
            if r.get("type") == "SUPPLEMENTARY_DATA_NOT_ALLOWED":
                nodes = root_element.xpath("//*[local-name()='SplmtryData']")
                for n in nodes:
                    report.add_issue(ValidationIssue(
                        severity=r["severity"],
                        code=r["errorCode"],
                        path="//SplmtryData",
                        message=r["errorMessage"],
                        line=n.sourceline or 1,
                        fix=r.get("fixSuggestion", ""),
                    ))
            elif r.get("type") == "SERVICE_LEVEL":
                field_parts = r["field"].split("/")
                xpath_expr = "//" + "/".join(f"*[local-name()='{p}']" for p in field_parts)
                nodes = root_element.xpath(xpath_expr)
                for n in nodes:
                    if _text(n) != r["expected"]:
                        report.add_issue(ValidationIssue(
                            severity=r["severity"],
                            code=r["errorCode"],
                            path="//" + "/".join(field_parts),
                            message=r["errorMessage"],
                            line=n.sourceline or 1,
                            fix=r.get("fixSuggestion", ""),
                        ))
