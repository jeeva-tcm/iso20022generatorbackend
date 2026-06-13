"""
SR2026 pacs.002 (FI-to-FI Payment Status Report) Delta Validator
=================================================================
FormalRules from CBPRPlus_SR2026 pacs.002 rules JSON:

  R9:  TxSts = RJCT → StatusReasonInformation/Reason mandatory
  R10: TxSts = RJCT → EffectiveInterbankSettlementDate NOT allowed
  R12: OriginalMessageNameIdentification must be pacs.008/009/004/010/003
  R14: OriginalInstructionIdentification slash rules (no //, no leading/trailing /)
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_SLASH_RE = re.compile(r'^/|/$|//')
_VALID_ORIG_MSG_NAMES = re.compile(
    r'^(pacs\.008|pacs\.009|pacs\.004|pacs\.010|pacs\.003)\.\d{3}\.\d{2}$'
)


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


class Pacs002Validator:
    """SR2026 pacs.002 business rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.002" not in (message_type or "").lower():
            return
        cls._validate_bizsvc(root, report)
        for tx in root.xpath("//*[local-name()='TxInfAndSts']"):
            cls._validate_tx(tx, report)

    @classmethod
    def _validate_bizsvc(cls, root: etree._Element, report: ValidationReport):
        # SR2026 pacs.002: BizSvc is mandatory [1..1] with fixed value swift.cbprplus.04.
        hdr = root.xpath("//*[local-name()='AppHdr']")
        if not hdr:
            return
        biz = hdr[0].xpath("*[local-name()='BizSvc']")
        if not biz or not _t(biz[0]):
            _add(report, "ERROR", "MISSING_BIZ_SVC", "//AppHdr/BizSvc",
                 "BizSvc is missing. pacs.002 SR2026 requires BizSvc = 'swift.cbprplus.04'.",
                 "Add <BizSvc>swift.cbprplus.04</BizSvc> to AppHdr.",
                 hdr[0].sourceline or 1)
            return
        val = _t(biz[0])
        if val != "swift.cbprplus.04":
            _add(report, "ERROR", "INVALID_BIZ_SVC", "//AppHdr/BizSvc",
                 f"BizSvc '{val}' is invalid for pacs.002 under SR2026. "
                 "It must be 'swift.cbprplus.04' (CBPR+ SR2026 fixed value).",
                 "Change BizSvc to swift.cbprplus.04.",
                 biz[0].sourceline or 1)

    @classmethod
    def _validate_tx(cls, tx: etree._Element, report: ValidationReport):
        src = tx.sourceline or 1

        # ── R9 / R10: TransactionStatus = RJCT consequences ──────────────────
        tx_sts_nodes = tx.xpath("*[local-name()='TxSts']")
        if tx_sts_nodes and _t(tx_sts_nodes[0]) == "RJCT":
            tx_src = tx_sts_nodes[0].sourceline or src

            # R9: StatusReasonInformation/Reason mandatory for RJCT
            rsn = tx.xpath("*[local-name()='StsRsnInf']/*[local-name()='Rsn']")
            if not rsn:
                _add(report, "ERROR", "RJCT_MISSING_STATUS_REASON",
                     "//TxInfAndSts/StsRsnInf/Rsn",
                     "TransactionStatus is 'RJCT' but <StsRsnInf><Rsn> is missing. "
                     "Reason is mandatory for rejected transactions "
                     "(CBPR_Transaction_Status_Reject_Reason_FormalRule R9).",
                     "Add <StsRsnInf><Rsn><Cd>AC01</Cd></Rsn></StsRsnInf> with a valid rejection reason code.",
                     tx_src)

            # R10: EffectiveInterbankSettlementDate forbidden for RJCT
            eff_sttlm = tx.xpath("*[local-name()='FctvIntrBkSttlmDt']")
            if eff_sttlm:
                _add(report, "ERROR", "RJCT_EFFECTIVE_SETTLEMENT_DATE_FORBIDDEN",
                     "//TxInfAndSts/FctvIntrBkSttlmDt",
                     "<EffectiveInterbankSettlementDate> is not allowed when TransactionStatus is 'RJCT'. "
                     "A rejected transaction has no effective settlement date "
                     "(CBPR_Transaction_Status_Reject_Effective_Sett_Date_FormalRule R10).",
                     "Remove <FctvIntrBkSttlmDt> from the rejected transaction status block.",
                     eff_sttlm[0].sourceline or src)

        # ── R12: OriginalMessageNameIdentification ────────────────────────────
        # Searched in both TxInfAndSts/OrgnlGrpInf and the group-level GrpHdr/OrgnlGrpInfAndSts
        for node in tx.xpath(
            "*[local-name()='OrgnlGrpInf']/*[local-name()='OrgnlMsgNmId']"
            " | *[local-name()='OrgnlMsgNmId']"
        ):
            val = _t(node)
            if val and not _VALID_ORIG_MSG_NAMES.match(val):
                _add(report, "ERROR", "INVALID_ORIGINAL_MSG_NAME_ID",
                     "//OrgnlMsgNmId",
                     f"OriginalMessageNameIdentification '{val}' is not valid. "
                     "Must be pacs.008, pacs.009, pacs.004, pacs.010, or pacs.003 "
                     "with version suffix (e.g. pacs.008.001.08) "
                     "(CBPR_OriginalMessageNameIdentification_FormalRule R12).",
                     "Use a valid CBPR+ message name identifier such as 'pacs.008.001.08'.",
                     node.sourceline or src)

        # ── R14: OriginalInstructionIdentification slash rules ────────────────
        for node in tx.xpath("*[local-name()='OrgnlInstrId']"):
            val = _t(node)
            if val and _SLASH_RE.search(val):
                _add(report, "ERROR", "INVALID_ORIGINAL_INSTR_ID_FORMAT",
                     "//TxInfAndSts/OrgnlInstrId",
                     f"<OrgnlInstrId> '{val}' must not start or end with '/' and must not contain '//' "
                     "(CBPR_Original_Instruction_Identification_FormalRule R14).",
                     "Remove leading/trailing slashes and consecutive '//' from OriginalInstructionIdentification.",
                     node.sourceline or src)
