from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class WarningEngine:
    @staticmethod
    def _extract_tz_offset(val: str) -> str:
        val = val.strip()
        if not val:
            return ""
        if val.endswith('Z'):
            return "+00:00"
        for i in range(1, min(7, len(val) + 1)):
            char = val[-i]
            if char in ['+', '-']:
                offset = val[-i:]
                sign = offset[0]
                rest = offset[1:]
                if ':' in rest:
                    parts = rest.split(':')
                    if len(parts) == 2 and len(parts[0]) == 2 and len(parts[1]) == 2:
                        res = offset
                    else:
                        res = ""
                else:
                    if len(rest) == 2:
                        res = f"{sign}{rest}:00"
                    elif len(rest) == 4:
                        res = f"{sign}{rest[:2]}:{rest[2:]}"
                    else:
                        res = ""
                if res in ["+00:00", "-00:00"]:
                    return "+00:00"
                return res
        return ""

    @staticmethod
    def evaluate(root_element: etree._Element, report: ValidationReport):
        # 1. Warn if corporate parties (Dbtr, Cdtr) are missing LEIs
        # In SR2026, corporate LEIs are highly recommended to prevent routing delays.
        parties = root_element.xpath("//*[local-name()='Dbtr' or local-name()='Cdtr']")
        for party in parties:
            party_name = party.tag.split('}')[-1] if isinstance(party.tag, str) else "Party"
            
            # Check if it has an Id (OrgId)
            org_id = party.xpath(".//*[local-name()='OrgId']")
            if org_id:
                leis = org_id[0].xpath("*[local-name()='LEI']")
                if not leis:
                    report.add_issue(ValidationIssue(
                        severity="WARNING",
                        code="CORPORATE_LEI_RECOMMENDED",
                        path=f"//{party_name}/Id/OrgId",
                        message=f"Corporate party '{party_name}' is missing an LEI. Under SR2026 guidelines, LEIs are recommended for all non-individual clients.",
                        line=party.sourceline or 1,
                        fix="Add the <LEI> tag containing the 20-character LEI under <OrgId>."
                    ))

        # 2. Check for AppHdr timezone offset drift (advisory check)
        app_hdr = root_element.xpath("//*[local-name()='AppHdr']")
        document = root_element.xpath("//*[local-name()='Document']")
        if app_hdr and document:
            cre_dt = app_hdr[0].xpath(".//*[local-name()='CreDt']")
            cre_dt_tm = document[0].xpath(".//*[local-name()='CreDtTm']")
            if cre_dt and cre_dt_tm:
                h_val = (cre_dt[0].text or "").strip()
                d_val = (cre_dt_tm[0].text or "").strip()
                
                # Check timezone offset representation
                h_tz = WarningEngine._extract_tz_offset(h_val)
                d_tz = WarningEngine._extract_tz_offset(d_val)
                
                if h_tz and d_tz and h_tz != d_tz:
                    report.add_issue(ValidationIssue(
                        severity="WARNING",
                        code="TIMEZONE_DRIFT_DETECTED",
                        path="//AppHdr/CreDt",
                        message=f"Header timezone offset '{h_tz}' differs from Payload timezone offset '{d_tz}'.",
                        line=cre_dt[0].sourceline or 1,
                        fix="For consistency, align timezone offsets across the header and payload elements."
                    ))
