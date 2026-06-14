"""
SR2026 CAMT General Validator
================================
Implements FormalRules from the SR2026 rules JSON files for camt message types
that are not message-type-specific complex arithmetic rules.

Rules implemented by message type:

camt.025 (Receipt):
  R6:  OriginalMessageNameIdentification must be camt.106/057/058
  R7:  RequestHandlingStatus=RJCT → StatusReason mandatory

camt.029 (ResolutionOfInvestigation):
  R8:  Status/Confirmation=RJCR → CancellationStatusReasonInformation/Reason mandatory
  R9:  CancellationStatusIdentification slash rules
  R10: Case/Identification slash rules
  R28: Reason/Code=ARDT → AdditionalInformation must be present
  R29: Reason/Code=PTNA → AdditionalInformation must be BIC8 or BIC11

camt.052 (BankToCustomerAccountReport):
  R1:  BAH CopyDuplicate == document CopyDuplicateIndicator when both present
  R7:  PageNumber must be greater than zero

camt.054 (BankToCustomerDebitCreditNotification):
  R1:  BAH CopyDuplicate == document CopyDuplicateIndicator when both present
  R8:  PageNumber must be greater than zero
  R10: Entry/Status=BOOK → BookingDate OR ValueDate must be present

camt.055 (CustomerPaymentCancellationRequest):
  R8:  OriginalRequestedExecutionDate and OriginalRequestedCollectionDate mutually exclusive
  R9:  Case/Identification slash rules
  R16: CancellationReason/Reason/Code=NARR → AdditionalInformation mandatory

camt.056 (FIToFIPaymentCancellationRequest):
  R8:  CancellationIdentification slash rules
  R9:  Case/Identification slash rules

camt.057 (NotificationToReceive):
  R14: Notification/ExpectedValueDate absent → Item/ExpectedValueDate must be present
  R19: TotalAmount/Currency must not be commodity (XAU/XAG/XPD/XPT)
  R28: Item/Amount/Currency must not be commodity

camt.058 (NotificationToReceiveCancellationAdvice):
  R5:  GroupHeader/MessageIdentification slash rules
  R6:  OriginalItemIdentification slash rules
  R24: CancellationReason/Reason/Code=NARR → AdditionalInformation mandatory

camt.107 (ChequePresentmentNotification):
  R5:  MessageIdentification slash rules
  R6:  ChequeNumber slash rules (same rule)

camt.108 (ChequeCancellationOrStopRequest):
  R5:  MessageIdentification slash rules
  R6:  ChequeNumber slash rules
  R12: Reason/Code=NARR → AdditionalInformation mandatory

camt.109 (ChequeCancellationOrStopReport):
  R6:  Status=RJCT → Reason/AdditionalInformation mandatory
  R7:  MessageIdentification slash rules
  R9:  ChequeNumber slash rules
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_SLASH_RE = re.compile(r'^/|/$|//')
_COMMODITY_CCY = {"XAU", "XAG", "XPD", "XPT"}
_BIC_RE = re.compile(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')

_CAMT025_MSG_NAMES = re.compile(r'^camt\.(106|05[78])\.001\.[0-9]{2}$')
_CAMT029_ORIG_TYPES = {"RJCR"}


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


def _slash(val: str) -> bool:
    return bool(_SLASH_RE.search(val)) if val else False


class CamtGeneralValidator:
    """General CAMT message business rules."""

    # SR2026: BizSvc is mandatory [1..1] with a fixed value per message
    # (per CAMT SR2026 difference docs). camt.055 uses .03; the rest use .04.
    _CAMT_BIZSVC = {
        "camt.052": "swift.cbprplus.04",
        "camt.053": "swift.cbprplus.04",
        "camt.054": "swift.cbprplus.04",
        "camt.055": "swift.cbprplus.03",
        "camt.056": "swift.cbprplus.04",
        "camt.057": "swift.cbprplus.04",
    }

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        cls._check_camt_bizsvc(root, report, msg)
        if "camt.025" in msg:
            cls._camt025(root, report)
        elif "camt.029" in msg:
            cls._camt029(root, report)
        elif "camt.052" in msg:
            cls._camt052(root, report)
        elif "camt.053" in msg:
            cls._camt053_basic(root, report)
        elif "camt.054" in msg:
            cls._camt054(root, report)
        elif "camt.055" in msg:
            cls._camt055(root, report)
        elif "camt.056" in msg:
            cls._camt056(root, report)
        elif "camt.057" in msg:
            cls._camt057(root, report)
        elif "camt.058" in msg:
            cls._camt058(root, report)
        elif "camt.107" in msg:
            cls._camt107(root, report)
        elif "camt.108" in msg:
            cls._camt108(root, report)
        elif "camt.109" in msg:
            cls._camt109(root, report)

    @classmethod
    def _check_camt_bizsvc(cls, root: etree._Element, report: ValidationReport, msg: str):
        expected = next((v for k, v in cls._CAMT_BIZSVC.items() if k in msg), None)
        if not expected:
            return
        hdr = root.xpath("//*[local-name()='AppHdr']")
        if not hdr:
            return
        biz = hdr[0].xpath("*[local-name()='BizSvc']")
        if not biz or not _t(biz[0]):
            _add(report, "ERROR", "MISSING_BIZ_SVC", "//AppHdr/BizSvc",
                 f"BizSvc is missing. SR2026 requires BizSvc = '{expected}' for this message.",
                 f"Add <BizSvc>{expected}</BizSvc> to AppHdr.",
                 hdr[0].sourceline or 1)
            return
        val = _t(biz[0])
        if val != expected:
            _add(report, "ERROR", "INVALID_BIZ_SVC", "//AppHdr/BizSvc",
                 f"BizSvc '{val}' is invalid under SR2026. It must be '{expected}' "
                 "(CBPR+ SR2026 fixed value).",
                 f"Change BizSvc to {expected}.",
                 biz[0].sourceline or 1)

    # ─── camt.025 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt025(cls, root: etree._Element, report: ValidationReport):
        for det in root.xpath("//*[local-name()='ReceiptDetails']"):
            src = det.sourceline or 1

            # R6: OriginalMessageNameIdentification pattern
            for node in det.xpath(
                "*[local-name()='OrgnlMsgId']/*[local-name()='MsgNmId']"
                " | *[local-name()='OriginalMessageIdentification']/*[local-name()='MessageNameIdentification']"
            ):
                val = _t(node)
                if val and not _CAMT025_MSG_NAMES.match(val):
                    _add(report, "ERROR", "INVALID_ORIG_MSG_NAME_ID",
                         "//ReceiptDetails/OrgnlMsgId/MsgNmId",
                         f"OriginalMessageNameIdentification '{val}' must be "
                         "camt.106.001.xx, camt.057.001.xx, or camt.058.001.xx "
                         "(CBPR_Message_Name_Identification_FormalRule R6).",
                         "Use one of: camt.106.001.03, camt.057.001.06, camt.058.001.08",
                         node.sourceline or src)

            # R7: RequestHandlingStatus=RJCT → Reason mandatory
            for rh in det.xpath("*[local-name()='ReqHndlg']"):
                sts_nodes = rh.xpath("*[local-name()='Sts']/*[local-name()='Cd']")
                if sts_nodes and _t(sts_nodes[0]) == "RJCT":
                    rsn = rh.xpath("*[local-name()='StsRsn']/*[local-name()='Rsn']")
                    if not rsn:
                        _add(report, "ERROR", "RJCT_MISSING_STATUS_REASON",
                             "//RequestHandling/StatusReason/Reason",
                             "RequestHandlingStatus is 'RJCT' but StatusReason/Reason is missing "
                             "(CBPR_Request_Handling_Status_Reject_Reason_FormalRule R7).",
                             "Add <StsRsn><Rsn><Cd>...</Cd></Rsn></StsRsn> with a valid reason code.",
                             sts_nodes[0].sourceline or src)

    # ─── camt.029 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt029(cls, root: etree._Element, report: ValidationReport):
        for doc in root.xpath("//*[local-name()='RsltnOfInvstgtn']"):
            src = doc.sourceline or 1

            # R8: Confirmation=RJCR → CancellationStatusReasonInformation/Reason mandatory
            conf_nodes = doc.xpath("*[local-name()='Sts']/*[local-name()='Conf']")
            if conf_nodes and _t(conf_nodes[0]) == "RJCR":
                rsn = doc.xpath(
                    ".//*[local-name()='CxlStsRsnInf']/*[local-name()='Rsn']"
                )
                if not rsn:
                    _add(report, "ERROR", "RJCR_MISSING_CANCELLATION_REASON",
                         "//RsltnOfInvstgtn/CxlStsRsnInf/Rsn",
                         "Status/Confirmation is 'RJCR' but CancellationStatusReasonInformation/Reason "
                         "is missing (CBPR_Cancellation_Reason_FormalRule R8).",
                         "Add <CxlStsRsnInf><Rsn><Cd>...</Cd></Rsn></CxlStsRsnInf>.",
                         conf_nodes[0].sourceline or src)

        # R9: CancellationStatusIdentification slash rules
        for node in root.xpath("//*[local-name()='CxlStsId']"):
            val = _t(node)
            if val and _slash(val[:16]):
                _add(report, "ERROR", "INVALID_CANCELLATION_STATUS_ID",
                     "//CxlStsId",
                     f"<CancellationStatusIdentification> '{val}': first 16 chars must not "
                     "start/end with '/' or contain '//' "
                     "(CBPR_Cancellation_Status_Identification_FormalRule R9).",
                     "Remove leading/trailing slashes and '//' from CancellationStatusIdentification.",
                     node.sourceline or 1)

        # R10: Case/Identification slash rules
        for node in root.xpath("//*[local-name()='Case']/*[local-name()='Id']"):
            val = _t(node)
            if val and _slash(val[:16]):
                _add(report, "ERROR", "INVALID_CASE_ID",
                     "//Case/Id",
                     f"<Case/Identification> '{val}': first 16 chars must not "
                     "start/end with '/' or contain '//' "
                     "(CBPR_Case_Identification_FormalRule R10).",
                     "Remove leading/trailing slashes and '//' from Case/Identification.",
                     node.sourceline or 1)

        # R28/R29: Cancellation status reason codes
        for rsn_inf in root.xpath("//*[local-name()='CxlStsRsnInf']"):
            src = rsn_inf.sourceline or 1
            cd_nodes = rsn_inf.xpath("*[local-name()='Rsn']/*[local-name()='Cd']")
            if not cd_nodes:
                continue
            cd = _t(cd_nodes[0])
            addl = rsn_inf.xpath("*[local-name()='AddtlInf']")

            # R28: ARDT → AdditionalInformation must be present (should contain original UETR)
            if cd == "ARDT" and not addl:
                _add(report, "ERROR", "ARDT_MISSING_ADDITIONAL_INFO",
                     "//CxlStsRsnInf/AddtlInf",
                     "Reason code 'ARDT' (Already Returned) requires AdditionalInformation "
                     "containing the UETR of the returned payment "
                     "(CBPR_Reason_Code_ARDT_AdditionalInformation_FormalRule R28).",
                     "Add <AddtlInf>UETR-of-returned-payment</AddtlInf>.",
                     cd_nodes[0].sourceline or src)

            # R29: PTNA → AdditionalInformation must be BIC8 or BIC11 if present
            if cd == "PTNA" and addl:
                addl_val = _t(addl[0]).upper()
                if addl_val and not _BIC_RE.match(addl_val):
                    _add(report, "ERROR", "PTNA_INVALID_ADDITIONAL_INFO",
                         "//CxlStsRsnInf/AddtlInf",
                         f"Reason code 'PTNA': AdditionalInformation '{addl_val}' must be "
                         "a valid BIC8 or BIC11 (CBPR_Reason_Code_PTNA_AdditionalInformation_FormalRule R29).",
                         "Provide a valid BIC8 (e.g. BARCGB2D) or BIC11 (BARCGB2DXXX).",
                         addl[0].sourceline or src)

    # ─── camt.052 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt052(cls, root: etree._Element, report: ValidationReport):
        cls._check_copy_duplicate(root, report,
                                  "//Rpt/CpyDplct",
                                  "*[local-name()='Rpt']/*[local-name()='CpyDplct']")
        cls._check_page_number(root, report,
                               "//*[local-name()='RptPgntn']/*[local-name()='PgNb']")

    # ─── camt.053 basic (page number — complex balance rules in camt_statement_validator.py)

    @classmethod
    def _camt053_basic(cls, root: etree._Element, report: ValidationReport):
        cls._check_copy_duplicate(root, report,
                                  "//Stmt/CpyDplct",
                                  "*[local-name()='Stmt']/*[local-name()='CpyDplct']")
        cls._check_page_number(root, report,
                               "//*[local-name()='StmtPgntn']/*[local-name()='PgNb']")

    # ─── camt.054 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt054(cls, root: etree._Element, report: ValidationReport):
        cls._check_copy_duplicate(root, report,
                                  "//Ntfctn/CpyDplct",
                                  "*[local-name()='Ntfctn']/*[local-name()='CpyDplct']")
        cls._check_page_number(root, report,
                               "//*[local-name()='NtfctnPgntn']/*[local-name()='PgNb']")

        # R10: Entry Status=BOOK → BookingDate OR ValueDate must be present
        for entry in root.xpath("//*[local-name()='Ntry']"):
            src = entry.sourceline or 1
            sts = entry.xpath("*[local-name()='Sts']/*[local-name()='Cd']")
            if sts and _t(sts[0]) == "BOOK":
                bk_dt = entry.xpath("*[local-name()='BookgDt']")
                val_dt = entry.xpath("*[local-name()='ValDt']")
                if not bk_dt and not val_dt:
                    _add(report, "ERROR", "BOOK_MISSING_DATE",
                         "//Ntry/BookgDt or //Ntry/ValDt",
                         "Entry Status is 'BOOK' but neither BookingDate nor ValueDate is present. "
                         "At least one must be provided "
                         "(CBPR_BookingDate_ValueDate_FormalRule R10).",
                         "Add <BookgDt><Dt>YYYY-MM-DD</Dt></BookgDt> or <ValDt><Dt>YYYY-MM-DD</Dt></ValDt>.",
                         sts[0].sourceline or src)

    # ─── camt.055 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt055(cls, root: etree._Element, report: ValidationReport):
        for tx in root.xpath("//*[local-name()='TxInf']"):
            src = tx.sourceline or 1

            # R8: OriginalRequestedExecutionDate and OriginalRequestedCollectionDate mutually exclusive
            exec_dt = tx.xpath("*[local-name()='OrgnlReqdExctnDt']")
            coll_dt = tx.xpath("*[local-name()='OrgnlReqdColltnDt']")
            if exec_dt and coll_dt:
                _add(report, "ERROR", "EXEC_COLL_DATE_MUTUALLY_EXCLUSIVE",
                     "//TxInf/OrgnlReqdExctnDt",
                     "OriginalRequestedExecutionDate and OriginalRequestedCollectionDate are "
                     "mutually exclusive — both cannot be present at the same time "
                     "(CBPR_OriginalRequestedExecutionDate_OriginalRequestedCollectionDate_FormalRule R8).",
                     "Remove one of OriginalRequestedExecutionDate or OriginalRequestedCollectionDate.",
                     exec_dt[0].sourceline or src)

            # R9: Case/Identification slash rules
            for node in tx.xpath("*[local-name()='Case']/*[local-name()='Id']"):
                val = _t(node)
                if val and _slash(val[:16]):
                    _add(report, "ERROR", "INVALID_CASE_ID",
                         "//Case/Id",
                         f"<Case/Identification> '{val}': first 16 chars must not "
                         "start/end with '/' or contain '//' (CBPR_Case_Identification_FormalRule R9).",
                         "Remove leading/trailing slashes and '//' from Case/Identification.",
                         node.sourceline or src)

            # R16: CancellationReason/Code=NARR → AdditionalInformation mandatory
            for cxl_rsn in tx.xpath("*[local-name()='CxlRsnInf']"):
                cls._check_narr_additional_info(cxl_rsn, report, "CxlRsnInf", "R16")

    # ─── camt.056 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt056(cls, root: etree._Element, report: ValidationReport):
        for tx in root.xpath("//*[local-name()='TxInf']"):
            src = tx.sourceline or 1

            # R8: CancellationIdentification slash rules
            for node in tx.xpath("*[local-name()='CxlId']"):
                val = _t(node)
                if val and _slash(val[:16]):
                    _add(report, "ERROR", "INVALID_CANCELLATION_ID",
                         "//TxInf/CxlId",
                         f"<CancellationIdentification> '{val}': first 16 chars must not "
                         "start/end with '/' or contain '//' "
                         "(CBPR_Cancellation_Identification_FormalRule R8).",
                         "Remove leading/trailing slashes and '//' from CancellationIdentification.",
                         node.sourceline or src)

            # R9: Case/Identification slash rules
            for node in tx.xpath("*[local-name()='Case']/*[local-name()='Id']"):
                val = _t(node)
                if val and _slash(val[:16]):
                    _add(report, "ERROR", "INVALID_CASE_ID",
                         "//Case/Id",
                         f"<Case/Identification> '{val}': first 16 chars must not "
                         "start/end with '/' or contain '//' (CBPR_Case_Identification_FormalRule R9).",
                         "Remove leading/trailing slashes and '//' from Case/Identification.",
                         node.sourceline or src)

    # ─── camt.057 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt057(cls, root: etree._Element, report: ValidationReport):
        for ntf in root.xpath("//*[local-name()='Ntfctn']"):
            src = ntf.sourceline or 1

            # R14: Notification/ExpectedValueDate absent → Item/ExpectedValueDate must be present
            exp_vd = ntf.xpath("*[local-name()='XpctdValDt']")
            item_exp_vd = ntf.xpath("*[local-name()='Itm']/*[local-name()='XpctdValDt']")
            if not exp_vd and not item_exp_vd:
                _add(report, "ERROR", "MISSING_EXPECTED_VALUE_DATE",
                     "//Ntfctn/Itm/XpctdValDt",
                     "Notification/ExpectedValueDate is absent but no Item/ExpectedValueDate "
                     "is present either. When the notification-level date is absent, "
                     "each item must carry its own ExpectedValueDate "
                     "(CBPR_Expected_Value_Date_FormalRule R14).",
                     "Add <XpctdValDt>YYYY-MM-DD</XpctdValDt> at Notification or Item level.",
                     src)

            # R19: TotalAmount/Currency must not be commodity
            for amt in ntf.xpath("*[local-name()='TtlAmt']"):
                ccy = amt.get("Ccy", "").upper()
                if ccy in _COMMODITY_CCY:
                    _add(report, "ERROR", "INVALID_TOTAL_AMOUNT_CURRENCY",
                         "//Ntfctn/TtlAmt/@Ccy",
                         f"TotalAmount currency '{ccy}' is not allowed. "
                         "XAU, XAG, XPD, XPT are commodity codes and are forbidden "
                         "(CBPR_Total_Amount_Currency_FormalRule R19).",
                         f"Replace '{ccy}' with a valid ISO 4217 currency code.",
                         amt.sourceline or src)

            # R28: Item/Amount/Currency must not be commodity
            for itm_amt in ntf.xpath("*[local-name()='Itm']/*[local-name()='Amt']"):
                ccy = itm_amt.get("Ccy", "").upper()
                if ccy in _COMMODITY_CCY:
                    _add(report, "ERROR", "INVALID_ITEM_AMOUNT_CURRENCY",
                         "//Ntfctn/Itm/Amt/@Ccy",
                         f"Item/Amount currency '{ccy}' is not allowed. "
                         "XAU, XAG, XPD, XPT are commodity codes and are forbidden "
                         "(CBPR_Amount_Currency_FormalRule R28).",
                         f"Replace '{ccy}' with a valid ISO 4217 currency code.",
                         itm_amt.sourceline or src)

    # ─── camt.058 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt058(cls, root: etree._Element, report: ValidationReport):
        # R5: GroupHeader/MessageIdentification slash rules
        for node in root.xpath("//*[local-name()='GrpHdr']/*[local-name()='MsgId']"):
            val = _t(node)
            if val and _slash(val):
                _add(report, "ERROR", "INVALID_MSG_ID",
                     "//GrpHdr/MsgId",
                     f"GroupHeader/MessageIdentification '{val}' must not start/end with '/' "
                     "or contain '//' (CBPR_Message_Identification_FormalRule R5).",
                     "Remove slashes from MessageIdentification.",
                     node.sourceline or 1)

        # R6: OriginalItemIdentification slash rules
        for node in root.xpath("//*[local-name()='OrgnlItmId']"):
            val = _t(node)
            if val and _slash(val):
                _add(report, "ERROR", "INVALID_ORIGINAL_ITEM_ID",
                     "//OrgnlItmId",
                     f"OriginalItemIdentification '{val}' must not start/end with '/' "
                     "or contain '//' (CBPR_Original_Item_Identification_FormalRule R6).",
                     "Remove slashes from OriginalItemIdentification.",
                     node.sourceline or 1)

        # R24: CancellationReason/Code=NARR → AdditionalInformation mandatory
        for cxl_rsn in root.xpath("//*[local-name()='CxlRsn']"):
            cls._check_narr_additional_info(cxl_rsn, report, "CxlRsn", "R24")

    # ─── camt.107 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt107(cls, root: etree._Element, report: ValidationReport):
        cls._check_grp_hdr_msg_id_slash(root, report, "R5")
        cls._check_cheque_number_slash(root, report, "R6")

    # ─── camt.108 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt108(cls, root: etree._Element, report: ValidationReport):
        cls._check_grp_hdr_msg_id_slash(root, report, "R5")
        cls._check_cheque_number_slash(root, report, "R6")
        # R12: NARR → AdditionalInformation
        for rsn in root.xpath("//*[local-name()='Rsn']"):
            cls._check_narr_additional_info(rsn, report, "Rsn", "R12")

    # ─── camt.109 ─────────────────────────────────────────────────────────────

    @classmethod
    def _camt109(cls, root: etree._Element, report: ValidationReport):
        # R6: Status=RJCT → Reason/AdditionalInformation mandatory
        for entry in root.xpath("//*[local-name()='Ntry']"):
            src = entry.sourceline or 1
            sts = entry.xpath("*[local-name()='Sts']/*[local-name()='Cd']")
            if sts and _t(sts[0]) == "RJCT":
                addl = entry.xpath(".//*[local-name()='AddtlInf']")
                rsn_cd = entry.xpath(".//*[local-name()='Rsn']/*[local-name()='Cd']")
                if not addl and not rsn_cd:
                    _add(report, "ERROR", "RJCT_MISSING_REASON",
                         "//Ntry",
                         "Entry Status is 'RJCT' but Reason or AdditionalInformation is missing "
                         "(CBPR_Cheque_Rejected_AdditionalInformation_FormalRule R6).",
                         "Add a reason code or additional information explaining the rejection.",
                         sts[0].sourceline or src)
        cls._check_grp_hdr_msg_id_slash(root, report, "R7")
        cls._check_cheque_number_slash(root, report, "R9")

    # ─── Shared helpers ───────────────────────────────────────────────────────

    @classmethod
    def _check_copy_duplicate(cls, root: etree._Element, report: ValidationReport,
                              doc_path: str, relative_xpath: str):
        bah_cd = root.xpath("//*[local-name()='AppHdr']/*[local-name()='CpyDplct']")
        if not bah_cd:
            return
        bah_val = _t(bah_cd[0])
        for doc_elem in root.xpath("//*[local-name()='Document']"):
            doc_cd_nodes = doc_elem.xpath(f".//{relative_xpath}", namespaces={})
            # Try a broad XPath
            all_cd = doc_elem.xpath("//*[local-name()='CpyDplct']")
            for cd in all_cd:
                doc_val = _t(cd)
                if doc_val and bah_val and doc_val != bah_val:
                    _add(report, "ERROR", "COPY_DUPLICATE_MISMATCH",
                         doc_path,
                         f"BAH CopyDuplicate '{bah_val}' does not match document "
                         f"CopyDuplicateIndicator '{doc_val}'. They must be identical when both present "
                         "(CBPR_Copy_Duplicate_FormalRule R1).",
                         "Align BAH CopyDuplicate with document CopyDuplicateIndicator.",
                         cd.sourceline or 1)

    @classmethod
    def _check_page_number(cls, root: etree._Element, report: ValidationReport, xpath: str):
        ZERO_VALS = {"0", "00", "000", "0000", "00000"}
        for node in root.xpath(xpath):
            val = _t(node)
            if val in ZERO_VALS:
                _add(report, "ERROR", "INVALID_PAGE_NUMBER",
                     xpath,
                     f"PageNumber '{val}' is not allowed. The page number must be greater than zero "
                     "(CBPR_PageNumber_FormalRule).",
                     "Set PageNumber to a positive integer (e.g. 1).",
                     node.sourceline or 1)

    @classmethod
    def _check_narr_additional_info(cls, container: etree._Element,
                                    report: ValidationReport, tag: str, rule_ref: str):
        cd_nodes = container.xpath("*[local-name()='Rsn']/*[local-name()='Cd']")
        if not cd_nodes or _t(cd_nodes[0]) != "NARR":
            return
        addl = container.xpath("*[local-name()='AddtlInf']")
        if not addl:
            _add(report, "ERROR", "NARR_MISSING_ADDITIONAL_INFO",
                 f"//{tag}/AddtlInf",
                 f"Reason code is 'NARR' (Narrative) but AdditionalInformation is missing. "
                 "When NARR is used, a narrative reason must be provided in AdditionalInformation "
                 f"(CBPR_Reason_Code_NARR_Additional_Information_FormalRule {rule_ref}).",
                 "Add <AddtlInf>Narrative reason text here</AddtlInf>.",
                 cd_nodes[0].sourceline or container.sourceline or 1)

    @classmethod
    def _check_grp_hdr_msg_id_slash(cls, root: etree._Element,
                                    report: ValidationReport, rule_ref: str):
        for node in root.xpath("//*[local-name()='GrpHdr']/*[local-name()='MsgId']"):
            val = _t(node)
            if val and _slash(val):
                _add(report, "ERROR", "INVALID_MSG_ID",
                     "//GrpHdr/MsgId",
                     f"GroupHeader/MessageIdentification '{val}' must not start/end with '/' "
                     f"or contain '//' (CBPR_Message_Identification_FormalRule {rule_ref}).",
                     "Remove slashes from MessageIdentification.",
                     node.sourceline or 1)

    @classmethod
    def _check_cheque_number_slash(cls, root: etree._Element,
                                   report: ValidationReport, rule_ref: str):
        for node in root.xpath("//*[local-name()='ChqNb']"):
            val = _t(node)
            if val and _slash(val):
                _add(report, "ERROR", "INVALID_CHEQUE_NUMBER",
                     "//ChqNb",
                     f"ChequeNumber '{val}' must not start/end with '/' "
                     f"or contain '//' (CBPR_Cheque_Number_FormalRule {rule_ref}).",
                     "Remove slashes from ChequeNumber.",
                     node.sourceline or 1)
