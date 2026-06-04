from lxml import etree
from sr2026.validators.models import ValidationIssue, ValidationReport

class AddressValidator:
    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # Scan for all Postal Address elements
        postal_addresses = root_element.xpath("//*[local-name()='PstlAdr']")
        
        for addr in postal_addresses:
            line = addr.sourceline or 1
            
            # Find the parent tag for context (e.g. Dbtr, Cdtr, DbtrAgt)
            parent = addr.getparent()
            parent_name = parent.tag.split('}')[-1] if parent is not None and isinstance(parent.tag, str) else "Party"
            
            # Check for unstructured AdrLine elements
            adr_lines = addr.xpath("*[local-name()='AdrLine']")
            if adr_lines:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="DEPRECATED_UNSTRUCTURED_ADDRESS",
                    path=f"//{parent_name}/PstlAdr",
                    message=f"Address for '{parent_name}' contains unstructured <AdrLine> element(s) which are deprecated in SR2026.",
                    line=adr_lines[0].sourceline or line,
                    fix="Use structured elements: <StrtNm>, <BldgNb>, <PstCd>, <TwnNm>, and <Ctry> instead of <AdrLine>."
                ))
            
            # Verify structured fields
            has_town = bool(addr.xpath("*[local-name()='TwnNm']"))
            has_ctry = bool(addr.xpath("*[local-name()='Ctry']"))
            
            # If the address is provided, Town Name and Country are strictly mandatory in SR2026
            if not has_town:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MISSING_TOWN_NAME",
                    path=f"//{parent_name}/PstlAdr",
                    message=f"Postal Address for '{parent_name}' is missing the mandatory structured Town Name <TwnNm>.",
                    line=line,
                    fix="Add <TwnNm> containing the town/city name to the <PstlAdr> block."
                ))
                
            if not has_ctry:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MISSING_COUNTRY",
                    path=f"//{parent_name}/PstlAdr",
                    message=f"Postal Address for '{parent_name}' is missing the mandatory Country <Ctry>.",
                    line=line,
                    fix="Add <Ctry> containing the 2-letter country code (e.g. <Ctry>US</Ctry>) to the <PstlAdr> block."
                ))
