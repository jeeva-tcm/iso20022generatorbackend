"""
CBPR+ SR2026 Cross-Cutting Formal Rules
=========================================
FormalRules from the official SR2026 rules JSON files that apply across
multiple message types.  They supplement Layer2 XSD validation with
business-logic checks that XSD cannot express.

Implemented rules (rule IDs reference the per-message rules JSON):
  CBPR_Interbank_Settlement_Currency        — XAU/XAG/XPD/XPT forbidden
  CBPR_GPI_ServiceLevel_Code               — G001 (pacs.008/cov), G004 (pacs.009/adv)
  CBPR_Instruction_Identification           — no leading/trailing/consecutive slashes
  CBPR_End_To_End_Identification            — first-16-char slash restriction
  CBPR_Instruction_for_Creditor_Agent1      — HOLD forbidden when CHQB present
  CBPR_Instruction_for_Creditor_Agent2      — TELB forbidden when PHOB present
  CBPR_Instruction_For_Creditor_Presence    — each InstrForCdtrAgt code only once
  CBPR_Priority_Instruction_Priority        — BAH Prty must match tx InstrPrty
  CBPR_Remittance_Mutually_Exclusive        — Strd and Ustrd cannot coexist in RmtInf
  CBPR_Related_Remit_Info_Mutually_Exclusive — RltdRmtInf and RmtInf cannot coexist
  CBPR_DEBT                                 — ChrgBr=DEBT → max 1 ChrgsInf
  CBPR_CRED                                 — ChrgBr=CRED → at least 1 ChrgsInf mandatory
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_SLASH_RE = re.compile(r'^/|/$|//')
_COMMODITY_CURRENCIES = {"XAU", "XAG", "XPD", "XPT"}

# GPI ServiceLevel rule is a FORBIDDEN-code rule, not a must-equal rule.
# Per message variant, ServiceLevel/Code must NOT be one of these GPI codes.
# Any non-GPI service level (SDVA, NURG, INST, etc.) is always allowed because
# it does not match the forbidden set. (Source: CBPR_GPI_ServiceLevel_Code_FormalRule)
#   pacs.008 / pacs.009.cov : G001 allowed  → forbid G002-G009 (except G001)
#   pacs.009 core / adv     : G004 allowed  → forbid G001-G009 (except G004)
_GPI_FORBIDDEN_CODES = {
    "pacs.008":     {"G002", "G003", "G004", "G005", "G006", "G007", "G009"},
    "pacs.009.cov": {"G002", "G003", "G004", "G005", "G006", "G007", "G009"},
    "pacs.009.adv": {"G001", "G002", "G003", "G005", "G006", "G007", "G009"},
    "pacs.009":     {"G001", "G002", "G003", "G005", "G006", "G007", "G009"},
}

# Tags that carry Ccy on themselves (amount elements with @Ccy attribute)
_CCY_AMOUNT_TAGS = {
    "IntrBkSttlmAmt", "OrgnlIntrBkSttlmAmt", "RtrdIntrBkSttlmAmt",
    "InstdAmt", "RtrdInstdAmt", "Amt", "TtlChrgsAndTaxAmt",
    "EquivAmt", "CntrValAmt",
}


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


class CBPRFormalRulesValidator:
    """Cross-cutting CBPR+ SR2026 formal rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        cls._check_commodity_currencies(root, report)
        cls._check_slash_rules(root, report)
        cls._check_instr_for_creditor(root, report)
        cls._check_priority_match(root, report)
        cls._check_remittance_exclusive(root, report)
        cls._check_related_remittance_exclusive(root, report)
        cls._check_debt_single_charge(root, report)
        cls._check_cred_charges_mandatory(root, report)
        cls._check_gpi_service_level(root, report, msg)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Interbank_Settlement_Currency
    # XAU, XAG, XPD, XPT not allowed in any payment currency field.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_commodity_currencies(cls, root: etree._Element, report: ValidationReport):
        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if local not in _CCY_AMOUNT_TAGS:
                continue
            ccy = elem.get("Ccy", "").upper()
            if ccy in _COMMODITY_CURRENCIES:
                _add(report, "ERROR", "INVALID_SETTLEMENT_CURRENCY",
                     f"//{local}/@Ccy",
                     f"Currency '{ccy}' is not allowed in <{local}>. "
                     "Commodity codes XAU, XAG, XPD, XPT are forbidden in CBPR+ payment messages "
                     "(CBPR_Interbank_Settlement_Currency_FormalRule).",
                     f"Replace '{ccy}' with a valid ISO 4217 currency code (e.g. USD, EUR, GBP).",
                     elem.sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Instruction_Identification / CBPR_End_To_End_Identification
    # Must not start/end with '/' and must not contain '//'.
    # For EndToEndId the restriction applies to the first 16 characters.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_slash_rules(cls, root: etree._Element, report: ValidationReport):
        E2E_TAGS = {"EndToEndId", "OrgnlEndToEndId"}
        FULL_TAGS = {"InstrId", "OrgnlInstrId"}
        ALL_TAGS = E2E_TAGS | FULL_TAGS

        for elem in root.iter():
            if not isinstance(elem.tag, str):
                continue
            local = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            if local not in ALL_TAGS:
                continue
            val = _t(elem)
            if not val:
                continue
            check = val[:16] if local in E2E_TAGS else val
            if _SLASH_RE.search(check):
                scope = "first 16 characters of" if local in E2E_TAGS else ""
                _add(report, "ERROR", "INVALID_IDENTIFICATION_FORMAT",
                     f"//{local}",
                     f"<{local}> '{val}': the {scope} value must not start or end with '/' "
                     "and must not contain '//' (CBPR_Instruction_Identification_FormalRule).",
                     f"Remove leading/trailing slashes and consecutive '//' from <{local}>.",
                     elem.sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Instruction_For_Creditor_Presence_Code
    # CBPR_Instruction_for_Creditor_Agent1 / Agent2
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_instr_for_creditor(cls, root: etree._Element, report: ValidationReport):
        for container in root.xpath("//*[local-name()='InstrForCdtrAgt']"):
            src = container.sourceline or 1
            codes = [_t(c) for c in container.xpath("*[local-name()='Cd']") if _t(c)]

            seen: set = set()
            for cd in codes:
                if cd in seen:
                    _add(report, "ERROR", "DUPLICATE_INSTR_FOR_CDTR_AGT_CODE",
                         "//InstrForCdtrAgt/Cd",
                         f"InstructionForCreditorAgent code '{cd}' appears more than once. "
                         "Each code can only be used once per transaction "
                         "(CBPR_Instruction_For_Creditor_Presence_Code_FormalRule).",
                         "Remove the duplicate instruction code.",
                         src)
                seen.add(cd)

            if "PHOB" in seen and "TELB" in seen:
                _add(report, "ERROR", "INVALID_INSTR_FOR_CDTR_AGT",
                     "//InstrForCdtrAgt/Cd",
                     "Code 'TELB' (Telecom) is not allowed when 'PHOB' (PhoneBeneficiary) is present "
                     "in InstructionForCreditorAgent (CBPR_Instruction_for_Creditor_Agent2_FormalRule).",
                     "Remove either PHOB or TELB from InstructionForCreditorAgent.",
                     src)

            if "CHQB" in seen and "HOLD" in seen:
                _add(report, "ERROR", "INVALID_INSTR_FOR_CDTR_AGT",
                     "//InstrForCdtrAgt/Cd",
                     "Code 'HOLD' (HoldCashForCreditor) is not allowed when 'CHQB' (PayCreditorByCheque) is present "
                     "in InstructionForCreditorAgent (CBPR_Instruction_for_Creditor_Agent1_FormalRule).",
                     "Remove either CHQB or HOLD from InstructionForCreditorAgent.",
                     src)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Priority_Instruction_Priority
    # BAH <Prty> must equal PaymentTypeInformation <InstrPrty> when both present.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_priority_match(cls, root: etree._Element, report: ValidationReport):
        hdr = root.xpath("//*[local-name()='AppHdr']")
        if not hdr:
            return
        bah_prty_nodes = hdr[0].xpath("*[local-name()='Prty']")
        if not bah_prty_nodes:
            return
        bah_prty = _t(bah_prty_nodes[0])

        for instr_prty in root.xpath("//*[local-name()='PmtTpInf']/*[local-name()='InstrPrty']"):
            tx_prty = _t(instr_prty)
            if tx_prty and bah_prty and tx_prty != bah_prty:
                _add(report, "ERROR", "PRIORITY_MISMATCH",
                     "//AppHdr/Prty",
                     f"BAH <Prty> '{bah_prty}' does not match PaymentTypeInformation "
                     f"<InstrPrty> '{tx_prty}'. When both are present they must be identical "
                     "(CBPR_Priority_Instruction_Priority_FormalRule).",
                     "Align AppHdr <Prty> with PaymentTypeInformation <InstrPrty>, or remove one of them.",
                     bah_prty_nodes[0].sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Remittance_Mutually_Exclusive
    # <Strd> and <Ustrd> cannot coexist inside the same <RmtInf>.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_remittance_exclusive(cls, root: etree._Element, report: ValidationReport):
        for rmt in root.xpath("//*[local-name()='RmtInf']"):
            strd = rmt.xpath("*[local-name()='Strd']")
            ustrd = rmt.xpath("*[local-name()='Ustrd']")
            if strd and ustrd:
                _add(report, "ERROR", "REMITTANCE_MUTUALLY_EXCLUSIVE",
                     "//RmtInf",
                     "<RemittanceInformation> cannot contain both <Strd> (structured) and "
                     "<Ustrd> (unstructured). Use one or the other "
                     "(CBPR_Remittance_Mutually_Exclusive_FormalRule).",
                     "Remove either <Strd> or <Ustrd> from <RmtInf>.",
                     rmt.sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_Related_Remit_Info_Remit_Info_Mutually_Exclusive
    # <RltdRmtInf> and <RmtInf> cannot coexist in the same transaction block.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_related_remittance_exclusive(cls, root: etree._Element, report: ValidationReport):
        for tx_tag in ("CdtTrfTxInf", "DrctDbtTxInf", "TxInf"):
            for tx in root.xpath(f"//*[local-name()='{tx_tag}']"):
                rlt_rmt = tx.xpath("*[local-name()='RltdRmtInf']")
                rmt = tx.xpath("*[local-name()='RmtInf']")
                if rlt_rmt and rmt:
                    _add(report, "ERROR", "RELATED_REMITTANCE_MUTUALLY_EXCLUSIVE",
                         f"//{tx_tag}",
                         "<RelatedRemittanceInformation> and <RemittanceInformation> cannot both be "
                         "present in the same transaction block in the interbank space "
                         "(CBPR_Related_Remit_Info_Remit_Info_Mutually_Exclusive_FormalRule).",
                         "Remove either <RltdRmtInf> or <RmtInf> from the transaction.",
                         tx.sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_DEBT
    # When ChargeBearer = DEBT, only one ChrgsInf block is allowed.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_debt_single_charge(cls, root: etree._Element, report: ValidationReport):
        for tx_tag in ("CdtTrfTxInf", "DrctDbtTxInf", "TxInf"):
            for tx in root.xpath(f"//*[local-name()='{tx_tag}']"):
                chrg_br = tx.xpath("*[local-name()='ChrgBr']")
                if not chrg_br or _t(chrg_br[0]) != "DEBT":
                    continue
                chrgs = tx.xpath("*[local-name()='ChrgsInf']")
                if len(chrgs) > 1:
                    _add(report, "ERROR", "DEBT_MULTIPLE_CHARGES",
                         "//ChrgsInf",
                         f"ChargeBearer is 'DEBT' but {len(chrgs)} <ChrgsInf> blocks are present. "
                         "Only one charge information block is allowed when ChrgBr=DEBT "
                         "(CBPR_DEBT_FormalRule).",
                         "Remove extra <ChrgsInf> blocks — only one is permitted when ChrgBr=DEBT.",
                         chrg_br[0].sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_CRED
    # When ChargeBearer = CRED, at least one ChrgsInf block is mandatory.
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_cred_charges_mandatory(cls, root: etree._Element, report: ValidationReport):
        for tx_tag in ("CdtTrfTxInf", "DrctDbtTxInf", "TxInf"):
            for tx in root.xpath(f"//*[local-name()='{tx_tag}']"):
                chrg_br = tx.xpath("*[local-name()='ChrgBr']")
                if not chrg_br or _t(chrg_br[0]) != "CRED":
                    continue
                chrgs = tx.xpath("*[local-name()='ChrgsInf']")
                if not chrgs:
                    _add(report, "ERROR", "CRED_MISSING_CHARGES_INFO",
                         "//ChrgsInf",
                         "ChargeBearer is 'CRED' but no <ChrgsInf> block is present. "
                         "Charge information is mandatory when ChrgBr=CRED — if no charges "
                         "are taken, use zero amount (CBPR_CRED_FormalRule).",
                         "Add <ChrgsInf><Amt Ccy=\"...\">0</Amt><Agt>...</Agt></ChrgsInf> "
                         "or set ChrgBr to SHAR if charges are shared.",
                         chrg_br[0].sourceline or 1)

    # ─────────────────────────────────────────────────────────────────────────
    # CBPR_GPI_ServiceLevel_Code
    # ServiceLevel/Code must NOT be one of the forbidden GPI codes for the
    # message variant. Non-GPI codes (SDVA, NURG, INST, ...) are always allowed.
    #   pacs.008 / pacs.009.cov : G001 is the only permitted GPI code
    #   pacs.009 core / adv     : G004 is the only permitted GPI code
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def _check_gpi_service_level(cls, root: etree._Element, report: ValidationReport, msg: str):
        if "pacs.009" in msg and "cov" in msg:
            forbidden = _GPI_FORBIDDEN_CODES["pacs.009.cov"]
            allowed = "G001"
        elif "pacs.009" in msg and "adv" in msg:
            forbidden = _GPI_FORBIDDEN_CODES["pacs.009.adv"]
            allowed = "G004"
        elif "pacs.009" in msg:
            forbidden = _GPI_FORBIDDEN_CODES["pacs.009"]
            allowed = "G004"
        elif "pacs.008" in msg:
            forbidden = _GPI_FORBIDDEN_CODES["pacs.008"]
            allowed = "G001"
        else:
            return

        for svc_cd in root.xpath("//*[local-name()='SvcLvl']/*[local-name()='Cd']"):
            val = _t(svc_cd).upper()
            if val and val in forbidden:
                _add(report, "ERROR", "INVALID_GPI_SERVICE_LEVEL_CODE",
                     "//PmtTpInf/SvcLvl/Cd",
                     f"ServiceLevel Code '{val}' is not allowed for {msg.upper()}. "
                     f"The only GPI code permitted for this message is '{allowed}' "
                     "(CBPR_GPI_ServiceLevel_Code_FormalRule).",
                     f"Use '{allowed}' for a GPI payment, or a non-GPI service level code "
                     "(e.g. SDVA, NURG).",
                     svc_cd.sourceline or 1)
