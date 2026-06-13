import re
from datetime import datetime
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class NVREngine:
    @staticmethod
    def _is_valid_uuid_v4(uuid_str: str) -> bool:
        # Strict UUID v4 pattern: lowercase hex, version=4, variant=[89ab]
        UUID_V4 = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$', re.IGNORECASE)
        return bool(UUID_V4.match(uuid_str.strip()))

    @staticmethod
    def _is_valid_iban(iban: str) -> bool:
        iban = iban.replace(" ", "").upper()
        if not re.match(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$', iban):
            return False
            
        # Rearrange for Mod-97 check: move first 4 chars to the end
        rearranged = iban[4:] + iban[:4]
        numeric_str = ""
        for char in rearranged:
            if char.isalpha():
                numeric_str += str(ord(char) - 55)
            else:
                numeric_str += char
                
        try:
            return int(numeric_str) % 97 == 1
        except ValueError:
            return False

    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # 1. pacs.009 COV Underlying Customer Credit Transfer checking
        doc_ns = root_element.xpath("//*[local-name()='Document']")
        if doc_ns:
            ns = etree.QName(doc_ns[0]).namespace or ""
            is_pacs009 = "pacs.009" in ns
            
            # Find if there is Prtry = COV or underlying customer credit transfer
            has_underlying = bool(root_element.xpath("//*[local-name()='UndrlygCstmrCdtTrf']"))
            prtry_nodes = root_element.xpath("//*[local-name()='Prtry']")
            is_cov_prtry = any((p.text or "").strip().upper() == "COV" for p in prtry_nodes)

            # SR2026 COV messages use BizSvc='swift.cbprplus.cov.04' for identification
            # — the <Prtry>COV</Prtry> convention only applies to pre-SR2026 messages
            biz_svc_nodes = root_element.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
            biz_svc_val = (biz_svc_nodes[0].text or "").strip() if biz_svc_nodes else ""
            is_sr2026_cov_bizsvc = "cov" in biz_svc_val.lower()

            if is_pacs009 and not is_sr2026_cov_bizsvc:
                if (is_cov_prtry or has_underlying) and not (is_cov_prtry and has_underlying):
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="PACS009_COV_MISMATCH",
                        path="//Document",
                        message="For pacs.009 COV messages, both <Prtry>COV</Prtry> and <UndrlygCstmrCdtTrf> must be present.",
                        line=doc_ns[0].sourceline or 1,
                        fix="Ensure the transaction contains both the Proprietary COV code and the underlying credit transfer block."
                    ))

        # 2. Date rules checking
        today = datetime.utcnow().date()
        date_nodes = root_element.xpath("//*[local-name()='BirthDt' or local-name()='CreDt' or local-name()='CreDtTm' or local-name()='IntrBkSttlmDt' or local-name()='ReqdExctnDt']")
        
        for dt in date_nodes:
            name = dt.tag.split('}')[-1] if isinstance(dt.tag, str) else ""
            val = (dt.text or "").strip()
            if not val:
                continue
                
            try:
                # Extract date part YYYY-MM-DD
                date_part = val[:10]
                parsed_date = datetime.strptime(date_part, "%Y-%m-%d").date()
                
                if name == "BirthDt":
                    # Birth dates must not be in the future
                    if parsed_date > today:
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="FUTURE_BIRTH_DATE",
                            path=f"//{name}",
                            message="Birth date cannot be in the future.",
                            line=dt.sourceline or 1,
                            fix="Change birth date to a valid past date."
                        ))
                elif name in ["ReqdExctnDt"]:
                    # Required execution dates must not be in the past
                    if parsed_date < today:
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="PAST_TRANSACTION_DATE",
                            path=f"//{name}",
                            message=f"{name} cannot be in the past (today is {today}).",
                            line=dt.sourceline or 1,
                            fix="Update the date to today or a future date."
                        ))
            except ValueError:
                pass

        # 4. IBAN Format & Check digit validation
        iban_nodes = root_element.xpath("//*[local-name()='IBAN']")
        for iban in iban_nodes:
            val = (iban.text or "").strip()
            if val and not NVREngine._is_valid_iban(val):
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="INVALID_IBAN_CHECK_DIGIT",
                    path="//IBAN",
                    message=f"IBAN '{val}' has an invalid country structure or fails MOD-97 check digits.",
                    line=iban.sourceline or 1,
                    fix="Verify the IBAN format and check digits."
                ))
