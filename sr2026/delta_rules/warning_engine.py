from lxml import etree
from sr2026.validators.models import ValidationIssue, ValidationReport

class WarningEngine:
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
                h_tz = h_val[-6:] if len(h_val) >= 6 and (h_val[-6] in ['+', '-']) else ""
                d_tz = d_val[-6:] if len(d_val) >= 6 and (d_val[-6] in ['+', '-']) else ""
                
                if h_tz and d_tz and h_tz != d_tz:
                    report.add_issue(ValidationIssue(
                        severity="WARNING",
                        code="TIMEZONE_DRIFT_DETECTED",
                        path="//AppHdr/CreDt",
                        message=f"Header timezone offset '{h_tz}' differs from Payload timezone offset '{d_tz}'.",
                        line=cre_dt[0].sourceline or 1,
                        fix="For consistency, align timezone offsets across the header and payload elements."
                    ))
