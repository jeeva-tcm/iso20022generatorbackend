"""
SR2026 Address Validator
========================
Validates postal address rules driven by the SR2026 rule registry.

Rules sourced from: app/services/validation/sr2026/rules/pacs.008.sr2026.json → addressRules
- AdrLine is fully deprecated (all grace periods inactive)
- TwnNm and Ctry are mandatory in every PstlAdr block
"""

from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport
from app.sr2026.rules import rule_registry


class AddressValidator:
    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        addr_rules = rule_registry.get_address_rules()
        adr_line_rule = addr_rules.get("adrLineDeprecated", {})
        party_reqs    = addr_rules.get("partyAddressRequirements", {})

        adr_severity  = adr_line_rule.get("severity", "ERROR")
        adr_code      = adr_line_rule.get("code", "DEPRECATED_UNSTRUCTURED_ADDRESS")
        adr_msg       = adr_line_rule.get("errorMessage", "Postal address contains unstructured <AdrLine> which is deprecated in SR2026.")
        adr_fix       = adr_line_rule.get("fix", "Use structured elements: <StrtNm>, <BldgNb>, <PstCd>, <TwnNm>, <Ctry>.")

        # Resolve per-party requirements once
        def _party_req(party: str, field: str) -> dict:
            return party_reqs.get(party, {}).get("postalAddress", {}).get(field, {})

        postal_addresses = root_element.xpath("//*[local-name()='PstlAdr']")

        for addr in postal_addresses:
            line = addr.sourceline or 1

            # Parent context (Dbtr, Cdtr, DbtrAgt, …)
            parent = addr.getparent()
            parent_name = (
                parent.tag.split("}")[-1]
                if parent is not None and isinstance(parent.tag, str)
                else "Party"
            )

            # ── Rule: AdrLine deprecated ──────────────────────────────────
            adr_lines = addr.xpath("*[local-name()='AdrLine']")
            if adr_lines:
                report.add_issue(ValidationIssue(
                    severity=adr_severity,
                    code=adr_code,
                    path=f"//{parent_name}/PstlAdr",
                    message=f"Address for '{parent_name}' contains unstructured <AdrLine> element(s) which are deprecated in SR2026.",
                    line=adr_lines[0].sourceline or line,
                    fix=adr_fix,
                ))

            # ── Rule: TwnNm mandatory ─────────────────────────────────────
            has_town = bool(addr.xpath("*[local-name()='TwnNm']"))
            if not has_town:
                req = _party_req(parent_name, "townName")
                severity = req.get("severity", "ERROR")
                code     = req.get("code", "MISSING_TOWN_NAME")
                msg      = req.get("errorMessage", f"Postal Address for '{parent_name}' is missing mandatory <TwnNm>.")
                fix      = req.get("fix", "Add <TwnNm> containing the town/city name to the <PstlAdr> block.")
                report.add_issue(ValidationIssue(
                    severity=severity,
                    code=code,
                    path=f"//{parent_name}/PstlAdr",
                    message=msg,
                    line=line,
                    fix=fix,
                ))

            # ── Rule: Ctry mandatory ──────────────────────────────────────
            has_ctry = bool(addr.xpath("*[local-name()='Ctry']"))
            if not has_ctry:
                req = _party_req(parent_name, "country")
                severity = req.get("severity", "ERROR")
                code     = req.get("code", "MISSING_COUNTRY")
                msg      = req.get("errorMessage", f"Postal Address for '{parent_name}' is missing mandatory <Ctry>.")
                fix      = req.get("fix", "Add <Ctry> containing the 2-letter country code (e.g. <Ctry>US</Ctry>).")
                report.add_issue(ValidationIssue(
                    severity=severity,
                    code=code,
                    path=f"//{parent_name}/PstlAdr",
                    message=msg,
                    line=line,
                    fix=fix,
                ))
