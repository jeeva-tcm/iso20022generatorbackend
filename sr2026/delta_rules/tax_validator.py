from lxml import etree
from sr2026.validators.models import ValidationIssue, ValidationReport

class TaxValidator:
    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # Find all Tax elements in the message
        tax_elements = root_element.xpath("//*[local-name()='Tax']")
        
        for tax in tax_elements:
            line = tax.sourceline or 1
            
            # Find the parent tag for context (e.g. CdtTrfTxInf)
            parent = tax.getparent()
            parent_name = parent.tag.split('}')[-1] if parent is not None and isinstance(parent.tag, str) else "Transaction"

            # 1. Tax Amount Check
            tax_amts = tax.xpath(".//*[local-name()='TaxAmt' or local-name()='TtlTaxAmt']")
            for tax_amt in tax_amts:
                try:
                    val = float(tax_amt.text)
                    if val <= 0:
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="INVALID_TAX_AMOUNT",
                            path=f"//{parent_name}/Tax",
                            message="Tax amount must be greater than zero.",
                            line=tax_amt.sourceline or line,
                            fix="Provide a positive value for the tax amount."
                        ))
                except (ValueError, TypeError):
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_TAX_AMOUNT_FORMAT",
                        path=f"//{parent_name}/Tax",
                        message="Tax amount must be a valid numeric decimal.",
                        line=tax_amt.sourceline or line,
                        fix="Ensure the tax amount has a valid decimal format."
                    ))

                # Currency check
                ccy = tax_amt.get("Ccy")
                if ccy:
                    # Find transaction amount currency to compare
                    tx_ccy_nodes = root_element.xpath("//*[local-name()='IntrBkSttlmAmt' or local-name()='InstdAmt']")
                    if tx_ccy_nodes:
                        tx_ccy = tx_ccy_nodes[0].get("Ccy")
                        if tx_ccy and ccy != tx_ccy:
                            report.add_issue(ValidationIssue(
                                severity="ERROR",
                                code="TAX_CURRENCY_MISMATCH",
                                path=f"//{parent_name}/Tax",
                                message=f"Tax currency '{ccy}' does not match the transaction currency '{tx_ccy}'.",
                                line=tax_amt.sourceline or line,
                                fix=f"Update the tax currency to match the main currency '{tx_ccy}'."
                            ))

            # 2. Tax Rate Check (if present, must be realistic, e.g. 0 to 100%)
            tax_rates = tax.xpath(".//*[local-name()='Rate']")
            for rate in tax_rates:
                try:
                    val = float(rate.text)
                    if not (0 <= val <= 100):
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="INVALID_TAX_RATE",
                            path=f"//{parent_name}/Tax",
                            message=f"Tax rate '{val}' must be between 0 and 100 percent.",
                            line=rate.sourceline or line,
                            fix="Change the tax rate value to be within the 0 to 100 range."
                        ))
                except (ValueError, TypeError):
                    pass

            # 3. Tax Record checks
            records = tax.xpath(".//*[local-name()='Rcrd']")
            for record in records:
                # In SR2026, a tax record should carry a Type (Tp) and a Category (Ctgy)
                tps = record.xpath("*[local-name()='Tp']")
                ctgys = record.xpath("*[local-name()='Ctgy']")
                
                if not tps:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="MISSING_TAX_RECORD_TYPE",
                        path=f"//{parent_name}/Tax/Rcrd",
                        message="Tax record is missing the required Type <Tp> classification.",
                        line=record.sourceline or line,
                        fix="Add the <Tp> tag to categorize the tax record."
                    ))
                if not ctgys:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="MISSING_TAX_RECORD_CATEGORY",
                        path=f"//{parent_name}/Tax/Rcrd",
                        message="Tax record is missing the required Category <Ctgy> classification.",
                        line=record.sourceline or line,
                        fix="Add the <Ctgy> tag to categorize the tax record."
                    ))
