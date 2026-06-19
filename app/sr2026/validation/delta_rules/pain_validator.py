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

    # SR2026 fixed BizSvc per pain message (matches the certified generator):
    #   pain.001 = .04, pain.002 = .04, pain.008 = .03 (direct-debit family, like pacs.003).
    _PAIN_BIZSVC = {
        "pain.008": "swift.cbprplus.03",
        "pain.001": "swift.cbprplus.04",
        "pain.002": "swift.cbprplus.04",
    }

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        cls._check_bizsvc(root, report, msg)
        if "pain.001" in msg:
            cls._pain001(root, report)
        elif "pain.002" in msg:
            cls._pain002(root, report)
        elif "pain.008" in msg:
            cls._pain008(root, report)

    @classmethod
    def _check_bizsvc(cls, root: etree._Element, report: ValidationReport, msg: str):
        # SR2026: AppHdr/BizSvc must equal the fixed value for pain messages. Mirrors the per-message
        # L3 BizSvc enforcement already present for PACS/CAMT (and replaces the removed L2 format
        # warning), so pain.* gets a single, clear Layer-3 error instead of a generic format warning.
        expected = next((v for k, v in cls._PAIN_BIZSVC.items() if k in msg), None)
        if not expected:
            return
        biz = root.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
        if not biz:
            return  # presence handled by header/mandatory-field rules; don't double-report
        val = _t(biz[0])
        if val and val != expected:
            _add(report, "ERROR", "INVALID_BIZ_SVC", "//AppHdr/BizSvc",
                 f"BizSvc '{val}' is invalid under SR2026. It must be '{expected}' "
                 "(CBPR+ SR2026 fixed value).",
                 f"Change BizSvc to {expected}.",
                 biz[0].sourceline or 1)

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

        # PmtTpInf XOR rule: PmtTpInf at PmtInf level and CdtTrfTxInf level are mutually exclusive.
        # If PmtInf/PmtTpInf is present, CdtTrfTxInf/PmtTpInf must not be present.
        for pmt_inf in root.xpath("//*[local-name()='PmtInf']"):
            src = pmt_inf.sourceline or 1
            pmt_tp_inf = pmt_inf.xpath("*[local-name()='PmtTpInf']")
            tx_tp_inf = pmt_inf.xpath(
                "*[local-name()='CdtTrfTxInf']/*[local-name()='PmtTpInf']"
            )
            if pmt_tp_inf and tx_tp_inf:
                _add(report, "ERROR", "PMT_TP_INF_EXCLUSIVE",
                     "//PmtInf/PmtTpInf",
                     "Invalid message content for payment type information. "
                     "If PaymentTypeInformation is present at the PaymentInformation level, "
                     "then CreditTransferTransactionInformation/PaymentTypeInformation is not allowed.",
                     "Remove PaymentTypeInformation from either the PaymentInformation level "
                     "or the CreditTransferTransactionInformation level — not both.",
                     pmt_tp_inf[0].sourceline or src)

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

        # UETR mandatory in pain.008 DrctDbtTxInf/PmtId per CBPR+ SR2026
        for tx in root.xpath("//*[local-name()='DrctDbtTxInf']"):
            pmtid_nodes = tx.xpath("*[local-name()='PmtId']")
            if not pmtid_nodes:
                continue
            pmtid = pmtid_nodes[0]
            uetr_nodes = pmtid.xpath("*[local-name()='UETR']")
            if not uetr_nodes or not (uetr_nodes[0].text or "").strip():
                _add(report, "ERROR", "MISSING_UETR",
                     "//DrctDbtTxInf/PmtId/UETR",
                     "DirectDebitTransactionInformation/PaymentIdentification/UETR is mandatory "
                     "in pain.008 under CBPR+ SR2026.",
                     "Add a <UETR> element with a valid lowercase UUID v4. "
                     "Example: 4a1a0945-5772-409a-83ba-240e666e0267",
                     pmtid.sourceline or tx.sourceline or 1)
