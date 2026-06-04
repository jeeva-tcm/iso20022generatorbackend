import re
from lxml import etree
from sr2026.validators.models import ValidationIssue, ValidationReport

class LEIValidator:
    @staticmethod
    def _is_valid_iso17442(lei: str) -> bool:
        lei = lei.strip().upper()
        if not re.match(r'^[A-Z0-9]{20}$', lei):
            return False
            
        # ISO 17442 Modulo 97 check digit algorithm
        # Convert letters to digits (A=10, B=11, ..., Z=35)
        numeric_str = ""
        for char in lei:
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
        # 1. Check all LEI tags for format validity
        lei_elements = root_element.xpath("//*[local-name()='LEI']")
        
        for lei_el in lei_elements:
            lei_val = (lei_el.text or "").strip()
            line = lei_el.sourceline or 1
            
            # Find the grandparent to see context (e.g. Dbtr, Cdtr, DbtrAgt)
            parent = lei_el.getparent()
            grandparent_name = "Party"
            if parent is not None:
                gp = parent.getparent()
                if gp is not None and isinstance(gp.tag, str):
                    grandparent_name = gp.tag.split('}')[-1]
            
            if not lei_val:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="EMPTY_LEI",
                    path=f"//{grandparent_name}/LEI",
                    message="Legal Entity Identifier (LEI) cannot be empty.",
                    line=line,
                    fix="Provide a valid 20-character LEI code."
                ))
            elif not LEIValidator._is_valid_iso17442(lei_val):
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="INVALID_LEI_FORMAT",
                    path=f"//{grandparent_name}/LEI",
                    message=f"LEI '{lei_val}' violates the ISO 17442 standard (MOD-97 check digit failure).",
                    line=line,
                    fix="Provide a valid 20-character Legal Entity Identifier conforming to ISO 17442."
                ))

        # 2. Enforce new SR2026 rules where LEI is mandatory/recommended
        # For instance, under SR2026, for Financial Institution identifications (agents),
        # having an LEI is highly recommended and in some settlement flows mandatory.
        # Let's add a warning if an agent (DbtrAgt, CdtrAgt) has a FinInstnId but is missing an LEI.
        agents = root_element.xpath("//*[local-name()='DbtrAgt' or local-name()='CdtrAgt' or local-name()='IntrmyAgt1' or local-name()='IntrmyAgt2']")
        for agent in agents:
            agent_name = agent.tag.split('}')[-1] if isinstance(agent.tag, str) else "Agent"
            fin_inst = agent.xpath("*[local-name()='FinInstnId']")
            if fin_inst:
                # Check if it has an LEI child
                leis = fin_inst[0].xpath("*[local-name()='LEI']")
                if not leis:
                    report.add_issue(ValidationIssue(
                        severity="WARNING",
                        code="MISSING_LEI_ADVISORY",
                        path=f"//{agent_name}/FinInstnId",
                        message=f"Agent '{agent_name}' is missing a Legal Entity Identifier (LEI). Under SR2026, LEI is highly recommended for all financial institutions.",
                        line=agent.sourceline or 1,
                        fix="Add the <LEI> tag containing the 20-character LEI under <FinInstnId>."
                    ))
