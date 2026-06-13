"""
SR2026 pain.001 / pain.002 / pain.008 Validator
=================================================
FormalRules from the SR2026 rules JSON files for PAIN message types
(rules not already covered by cross-cutting validators).

pain.001 (CustomerCreditTransferInitiation):
  R8:  PaymentInformation/PaymentInformationIdentification must equal
       GroupHeader/MessageIdentification

pain.002 (CustomerPaymentStatusReport):
  R6:  OriginalPaymentInformationIdentification must equal
       OriginalGroupInformationAndStatus/OriginalMessageIdentification
  R9:  OriginalMessageNameIdentification must be pain.001.001.xx or pain.008.001.xx
  R10: TransactionStatus=RJCT → StatusReasonInformation/Reason mandatory
  R14: Originator/AnyBIC absent → Originator/Name mandatory
  R15: Originator/PostalAddress present → Originator/Name mandatory

pain.008 (CustomerDirectDebitInitiation):
  R6:  GroupHeader/NumberOfTransactions = count of DirectDebitTransactionInformation
  R7:  PaymentInformation/PaymentInformationIdentification must equal
       GroupHeader/MessageIdentification
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_PAIN_MSG_NAME_RE = re.compile(r'^pain\.00[18]\.001\.[0-9]{2}$')


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


class PainValidator:
    """pain.001 / pain.002 / pain.008 formal rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        if "pain.001" in msg:
            cls._pain001(root, report)
        elif "pain.002" in msg:
            cls._pain002(root, report)
        elif "pain.008" in msg:
            cls._pain008(root, report)

    # ─── pain.001 ─────────────────────────────────────────────────────────────

    @classmethod
    def _pain001(cls, root: etree._Element, report: ValidationReport):
        # R8: PaymentInformation/PaymentInformationIdentification == GrpHdr/MsgId
        grp_msg_id_nodes = root.xpath(
            "//*[local-name()='GrpHdr']/*[local-name()='MsgId']"
        )
        if not grp_msg_id_nodes:
            return
        grp_msg_id = _t(grp_msg_id_nodes[0])

        for pmt_inf in root.xpath("//*[local-name()='PmtInf']"):
            src = pmt_inf.sourceline or 1
            pmt_inf_id_nodes = pmt_inf.xpath("*[local-name()='PmtInfId']")
            if not pmt_inf_id_nodes:
                continue
            pmt_inf_id = _t(pmt_inf_id_nodes[0])
            if pmt_inf_id and grp_msg_id and pmt_inf_id != grp_msg_id:
                _add(report, "ERROR", "PMT_INF_ID_MISMATCH",
                     "//PmtInf/PmtInfId",
                     f"PaymentInformation/PaymentInformationIdentification '{pmt_inf_id}' "
                     f"must equal GroupHeader/MessageIdentification '{grp_msg_id}' "
                     "(CBPR_MessageIdentification_PaymentInformationIdentification_FormalRule R8).",
                     "Set PaymentInformationIdentification to the same value as GroupHeader/MessageIdentification.",
                     pmt_inf_id_nodes[0].sourceline or src)

    # ─── pain.002 ─────────────────────────────────────────────────────────────

    @classmethod
    def _pain002(cls, root: etree._Element, report: ValidationReport):
        # R6: OriginalPaymentInformationIdentification == OriginalGroupInfo/OriginalMessageIdentification
        orig_grp_msg_id_nodes = root.xpath(
            "//*[local-name()='OrgnlGrpInfAndSts']/*[local-name()='OrgnlMsgId']"
        )
        if orig_grp_msg_id_nodes:
            orig_grp_msg_id = _t(orig_grp_msg_id_nodes[0])
            for pmt_sts in root.xpath("//*[local-name()='OrgnlPmtInfAndSts']"):
                src = pmt_sts.sourceline or 1
                pmt_inf_id_nodes = pmt_sts.xpath("*[local-name()='OrgnlPmtInfId']")
                if not pmt_inf_id_nodes:
                    continue
                pmt_inf_id = _t(pmt_inf_id_nodes[0])
                if pmt_inf_id and orig_grp_msg_id and pmt_inf_id != orig_grp_msg_id:
                    _add(report, "ERROR", "ORIG_PMT_INF_ID_MISMATCH",
                         "//OrgnlPmtInfAndSts/OrgnlPmtInfId",
                         f"OriginalPaymentInformationIdentification '{pmt_inf_id}' must equal "
                         f"OriginalGroupInformationAndStatus/OriginalMessageIdentification '{orig_grp_msg_id}' "
                         "(CBPR_MessageIdentification_PaymentInformationIdentification_FormalRule R6).",
                         "Set OriginalPaymentInformationIdentification to match the original GroupHeader/MessageIdentification.",
                         pmt_inf_id_nodes[0].sourceline or src)

        # R9: OriginalMessageNameIdentification must be pain.001.001.xx or pain.008.001.xx
        for node in root.xpath(
            "//*[local-name()='OrgnlGrpInfAndSts']/*[local-name()='OrgnlMsgNmId']"
        ):
            val = _t(node)
            if val and not _PAIN_MSG_NAME_RE.match(val):
                _add(report, "ERROR", "INVALID_ORIG_MSG_NAME_ID",
                     "//OrgnlGrpInfAndSts/OrgnlMsgNmId",
                     f"OriginalMessageNameIdentification '{val}' must be pain.001.001.xx "
                     "or pain.008.001.xx (CBPR_OriginalMessageNameIdentification_FormalRule R9).",
                     "Use 'pain.001.001.09' or 'pain.008.001.08' as the original message name.",
                     node.sourceline or 1)

        # R10: TxSts=RJCT → StatusReasonInformation/Reason mandatory
        for tx in root.xpath("//*[local-name()='TxInfAndSts']"):
            src = tx.sourceline or 1
            tx_sts = tx.xpath("*[local-name()='TxSts']")
            if tx_sts and _t(tx_sts[0]) == "RJCT":
                rsn = tx.xpath("*[local-name()='StsRsnInf']/*[local-name()='Rsn']")
                if not rsn:
                    _add(report, "ERROR", "RJCT_MISSING_STATUS_REASON",
                         "//TxInfAndSts/StsRsnInf/Rsn",
                         "TransactionStatus is 'RJCT' but StatusReasonInformation/Reason is missing. "
                         "Reason is mandatory for rejected transactions "
                         "(CBPR_Transaction_Status_Reject_Reason_FormalRule R10).",
                         "Add <StsRsnInf><Rsn><Cd>AC01</Cd></Rsn></StsRsnInf>.",
                         tx_sts[0].sourceline or src)

                # R14 / R15: Originator rules (when Reason is present)
                for rsn_inf in tx.xpath("*[local-name()='StsRsnInf']"):
                    cls._check_originator(rsn_inf, report)

    @classmethod
    def _check_originator(cls, rsn_inf: etree._Element, report: ValidationReport):
        for orig in rsn_inf.xpath("*[local-name()='Orgtr']"):
            src = orig.sourceline or 1

            # R14: AnyBIC absent → Name mandatory
            any_bic = orig.xpath(
                "*[local-name()='Id']/*[local-name()='OrgId']/*[local-name()='AnyBIC']"
            )
            name = orig.xpath("*[local-name()='Nm']")
            if not any_bic and not name:
                _add(report, "ERROR", "ORIGINATOR_MISSING_NAME",
                     "//Orgtr/Nm",
                     "StatusReasonInformation/Originator: AnyBIC is absent so Name is mandatory "
                     "(CBPR_Originator_Option_1_FormalRule R14).",
                     "Add <Nm>Originator name</Nm> to the Originator block.",
                     src)

            # R15: PostalAddress present → Name mandatory
            postal = orig.xpath("*[local-name()='PstlAdr']")
            if postal and not name:
                _add(report, "ERROR", "ORIGINATOR_MISSING_NAME_WITH_ADDRESS",
                     "//Orgtr/Nm",
                     "StatusReasonInformation/Originator: PostalAddress is present so Name is mandatory "
                     "(CBPR_Originator_Option_2_FormalRule R15).",
                     "Add <Nm>Originator name</Nm> to the Originator block.",
                     postal[0].sourceline or src)

    # ─── pain.008 ─────────────────────────────────────────────────────────────

    @classmethod
    def _pain008(cls, root: etree._Element, report: ValidationReport):
        # R6: GroupHeader/NumberOfTransactions = count of DirectDebitTransactionInformation
        nb_of_tx_nodes = root.xpath(
            "//*[local-name()='GrpHdr']/*[local-name()='NbOfTxs']"
        )
        dd_tx_nodes = root.xpath(
            "//*[local-name()='DrctDbtTxInf']"
        )
        if nb_of_tx_nodes:
            nb_val = _t(nb_of_tx_nodes[0])
            try:
                if int(nb_val) != len(dd_tx_nodes):
                    _add(report, "ERROR", "NB_OF_TXS_MISMATCH",
                         "//GrpHdr/NbOfTxs",
                         f"GroupHeader/NumberOfTransactions is '{nb_val}' but "
                         f"{len(dd_tx_nodes)} DirectDebitTransactionInformation elements found "
                         "(CBPR_NumberOfTransactions_FormalRule R6).",
                         f"Set NumberOfTransactions to {len(dd_tx_nodes)}.",
                         nb_of_tx_nodes[0].sourceline or 1)
            except (ValueError, TypeError):
                pass

        # R7: PaymentInformation/PaymentInformationIdentification == GrpHdr/MsgId
        grp_msg_id_nodes = root.xpath(
            "//*[local-name()='GrpHdr']/*[local-name()='MsgId']"
        )
        if not grp_msg_id_nodes:
            return
        grp_msg_id = _t(grp_msg_id_nodes[0])

        for pmt_inf in root.xpath("//*[local-name()='PmtInf']"):
            src = pmt_inf.sourceline or 1
            pmt_inf_id_nodes = pmt_inf.xpath("*[local-name()='PmtInfId']")
            if not pmt_inf_id_nodes:
                continue
            pmt_inf_id = _t(pmt_inf_id_nodes[0])
            if pmt_inf_id and grp_msg_id and pmt_inf_id != grp_msg_id:
                _add(report, "ERROR", "PMT_INF_ID_MISMATCH",
                     "//PmtInf/PmtInfId",
                     f"PaymentInformation/PaymentInformationIdentification '{pmt_inf_id}' "
                     f"must equal GroupHeader/MessageIdentification '{grp_msg_id}' "
                     "(CBPR_MessageIdentification_PaymentInformationIdentification_FormalRule R7).",
                     "Set PaymentInformationIdentification to the same value as GroupHeader/MessageIdentification.",
                     pmt_inf_id_nodes[0].sourceline or src)
