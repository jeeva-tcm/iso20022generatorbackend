"""
SR2026 pacs.004 (Payment Return) Delta Validator
=================================================
Enforces SR2026 CBPR+ rules for pacs.004.001.09 that are NOT already
caught by the generic validators (Layer2 XSD, Layer3 code lists, etc.).

Rules enforced here:
  - BizSvc must be swift.cbprplus.04
  - OrgnlUETR must be lowercase UUID v4
  - RtrRsnInf/Rsn/Cd must be a valid ISO ExternalReturnReason1Code
  - ChrgBr must be CRED or SHAR (DEBT and SLEV are not permitted in pacs.004)
  - NbOfTxs must equal 1 (CBPR+ allows exactly one return per message)
  - OrgnlEndToEndId mandatory
  - RtrdIntrBkSttlmAmt mandatory + > 0
  - RtrdInstdAmt mandatory + > 0
  - IntrBkSttlmDt mandatory (no timezone)
  - InstgAgt and InstdAgt mandatory in TxInf
  - RtrChain mandatory
  - RtrRsnInf mandatory
"""

import json
import os
import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_UETR_RE = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$'
)
_BIZSVC_PACS004 = "swift.cbprplus.04"
_CHRG_BR_ALLOWED = {"CRED", "SHAR"}
_VALID_ORIG_MSG_NAMES_PACS004 = re.compile(
    r'^(pacs\.002|pacs\.008|pacs\.009|pacs\.010)\.\d{3}\.\d{2}$'
)


class Pacs004Validator:
    _return_reasons: list = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    @classmethod
    def _load_return_reasons(cls) -> list:
        if cls._return_reasons is None:
            path = os.path.join(
                os.path.dirname(__file__),
                "..", "..", "rules", "return_reason.json"
            )
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                cls._return_reasons = data.get("codes", [])
            except Exception:
                cls._return_reasons = []
        return cls._return_reasons

    @staticmethod
    def _text(node) -> str:
        return (node.text or "").strip() if node is not None else ""

    @staticmethod
    def _first(parent, *tags):
        cur = parent
        for tag in tags:
            if cur is None:
                return None
            found = cur.xpath(f"*[local-name()='{tag}']")
            cur = found[0] if found else None
        return cur

    @staticmethod
    def _issue(report: ValidationReport, severity: str, code: str, path: str,
               message: str, fix: str, line: int = 1, layer: int = 3):
        report.add_issue(ValidationIssue(
            severity=severity, code=code, layer=layer,
            path=path, message=message, fix=fix, line=line or 1,
        ))

    # ── Entry point ────────────────────────────────────────────────────────────

    @classmethod
    def validate(cls, root_element: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.004" not in (message_type or "").lower():
            return

        cls._validate_bizsvc(root_element, report)
        cls._validate_group_header(root_element, report)

        tx_nodes = root_element.xpath("//*[local-name()='TxInf']")
        for tx in tx_nodes:
            cls._validate_tx(tx, report)

    # ── BizSvc ────────────────────────────────────────────────────────────────

    @classmethod
    def _validate_bizsvc(cls, root: etree._Element, report: ValidationReport):
        hdr_nodes = root.xpath("//*[local-name()='AppHdr']")
        if not hdr_nodes:
            return
        hdr = hdr_nodes[0]
        biz_svc_nodes = hdr.xpath("*[local-name()='BizSvc']")
        if not biz_svc_nodes:
            cls._issue(report, "ERROR", "MISSING_BIZ_SVC", "//AppHdr/BizSvc",
                       f"BizSvc is missing. pacs.004 requires {_BIZSVC_PACS004}.",
                       f"Add <BizSvc>{_BIZSVC_PACS004}</BizSvc> to AppHdr.",
                       hdr.sourceline or 1, layer=2)
            return

        val = cls._text(biz_svc_nodes[0])
        if val != _BIZSVC_PACS004:
            cls._issue(report, "ERROR", "INVALID_BIZ_SVC", "//AppHdr/BizSvc",
                       f"BizSvc '{val}' is invalid for pacs.004. Must be {_BIZSVC_PACS004}.",
                       f"Change BizSvc to {_BIZSVC_PACS004}.",
                       biz_svc_nodes[0].sourceline or 1, layer=2)

    # ── Group Header ──────────────────────────────────────────────────────────

    @classmethod
    def _validate_group_header(cls, root: etree._Element, report: ValidationReport):
        grp_nodes = root.xpath("//*[local-name()='GrpHdr']")
        if not grp_nodes:
            return
        grp = grp_nodes[0]
        line = grp.sourceline or 1

        # NbOfTxs must be present and equal 1
        nb_nodes = grp.xpath("*[local-name()='NbOfTxs']")
        if nb_nodes:
            nb_val = cls._text(nb_nodes[0])
            if nb_val != "1":
                cls._issue(report, "ERROR", "INVALID_NB_OF_TXS", "//GrpHdr/NbOfTxs",
                           f"Number of Transactions must be 1 for pacs.004 (found '{nb_val}'). "
                           "CBPR+ allows exactly one return transaction per message.",
                           "Set <NbOfTxs>1</NbOfTxs>.",
                           nb_nodes[0].sourceline or line, layer=2)

        # SttlmMtd must be INDA or INGA
        sttlm_nodes = grp.xpath(
            "*[local-name()='SttlmInf']/*[local-name()='SttlmMtd']"
        )
        if sttlm_nodes:
            sttlm_val = cls._text(sttlm_nodes[0])
            if sttlm_val not in ("INDA", "INGA"):
                cls._issue(report, "ERROR", "MISSING_OR_INVALID_STTLM_MTD",
                           "//GrpHdr/SttlmInf/SttlmMtd",
                           f"Settlement Method '{sttlm_val}' is not valid for pacs.004. "
                           "Allowed: INDA, INGA.",
                           "Set SttlmMtd to INDA or INGA.",
                           sttlm_nodes[0].sourceline or line)

    # ── Transaction block ──────────────────────────────────────────────────────

    @classmethod
    def _validate_tx(cls, tx: etree._Element, report: ValidationReport):
        tx_line = tx.sourceline or 1
        return_reasons = cls._load_return_reasons()

        # ── OrgnlEndToEndId mandatory ──────────────────────────────────────
        e2e_nodes = tx.xpath("*[local-name()='OrgnlEndToEndId']")
        if not e2e_nodes or not cls._text(e2e_nodes[0]):
            cls._issue(report, "ERROR", "MISSING_ORIGINAL_END_TO_END_ID",
                       "//TxInf/OrgnlEndToEndId",
                       "Original End-to-End ID is missing.",
                       "Enter the EndToEndId from the original pacs.008 or pacs.009.",
                       tx_line, layer=2)

        # ── OrgnlUETR mandatory + lowercase UUID4 ─────────────────────────
        uetr_nodes = tx.xpath("*[local-name()='OrgnlUETR']")
        if not uetr_nodes or not cls._text(uetr_nodes[0]):
            cls._issue(report, "ERROR", "MISSING_OR_INVALID_ORIGINAL_UETR",
                       "//TxInf/OrgnlUETR",
                       "Original UETR is missing.",
                       "Enter the UETR from the original payment as a lowercase UUID v4.",
                       tx_line, layer=2)
        else:
            uetr_val = cls._text(uetr_nodes[0])
            if not _UETR_RE.match(uetr_val):
                cls._issue(report, "ERROR", "INVALID_UETR_FORMAT",
                           "//TxInf/OrgnlUETR",
                           f"Original UETR '{uetr_val}' is not a valid lowercase UUID v4. "
                           "SR2026 requires lowercase hex digits only.",
                           "Use a lowercase UUID v4. Example: 4a1a0945-5772-409a-83ba-240e666e0267",
                           uetr_nodes[0].sourceline or tx_line)

        # ── RtrdIntrBkSttlmAmt mandatory + > 0 ───────────────────────────
        rtrd_amt_nodes = tx.xpath("*[local-name()='RtrdIntrBkSttlmAmt']")
        if not rtrd_amt_nodes:
            cls._issue(report, "ERROR", "MISSING_RTRD_INTRBK_STTLM_AMT",
                       "//TxInf/RtrdIntrBkSttlmAmt",
                       "Returned Interbank Settlement Amount is missing.",
                       "Enter the amount being returned with a valid ISO 4217 currency code.",
                       tx_line, layer=2)
        else:
            amt_node = rtrd_amt_nodes[0]
            amt_val = cls._text(amt_node)
            try:
                if float(amt_val) <= 0:
                    cls._issue(report, "ERROR", "INVALID_AMOUNT",
                               "//TxInf/RtrdIntrBkSttlmAmt",
                               f"Returned Interbank Settlement Amount '{amt_val}' must be greater than zero.",
                               "Enter a positive return amount.",
                               amt_node.sourceline or tx_line)
            except (ValueError, TypeError):
                pass  # non-numeric caught by XSD

        # ── RtrdInstdAmt mandatory + > 0 ──────────────────────────────────
        rtrd_instd_nodes = tx.xpath("*[local-name()='RtrdInstdAmt']")
        if not rtrd_instd_nodes:
            cls._issue(report, "ERROR", "MISSING_RTRD_INSTD_AMT",
                       "//TxInf/RtrdInstdAmt",
                       "Returned Instructed Amount is missing. Required by pacs.004.001.09 XSD.",
                       "Add <RtrdInstdAmt Ccy=\"...\">...</RtrdInstdAmt> before <ChrgBr>.",
                       tx_line, layer=2)
        else:
            ri_node = rtrd_instd_nodes[0]
            ri_val = cls._text(ri_node)
            try:
                if float(ri_val) <= 0:
                    cls._issue(report, "ERROR", "INVALID_AMOUNT",
                               "//TxInf/RtrdInstdAmt",
                               f"Returned Instructed Amount '{ri_val}' must be greater than zero.",
                               "Enter a positive instructed amount.",
                               ri_node.sourceline or tx_line)
            except (ValueError, TypeError):
                pass

        # ── IntrBkSttlmDt mandatory ───────────────────────────────────────
        sttlm_dt_nodes = tx.xpath("*[local-name()='IntrBkSttlmDt']")
        if not sttlm_dt_nodes or not cls._text(sttlm_dt_nodes[0]):
            cls._issue(report, "ERROR", "MISSING_INTRBK_STTLM_DT",
                       "//TxInf/IntrBkSttlmDt",
                       "Return Settlement Date is missing.",
                       "Enter a valid settlement date in YYYY-MM-DD format.",
                       tx_line, layer=2)

        # ── ChrgBr: CRED or SHAR only ─────────────────────────────────────
        chrg_br_nodes = tx.xpath("*[local-name()='ChrgBr']")
        if chrg_br_nodes:
            chrg_val = cls._text(chrg_br_nodes[0])
            if chrg_val not in _CHRG_BR_ALLOWED:
                cls._issue(report, "ERROR", "MISSING_OR_INVALID_CHRG_BR",
                           "//TxInf/ChrgBr",
                           f"Charge Bearer '{chrg_val}' is not valid for pacs.004. "
                           f"Allowed: {', '.join(sorted(_CHRG_BR_ALLOWED))}. "
                           "DEBT and SLEV are not permitted in return messages.",
                           "Set ChrgBr to CRED or SHAR.",
                           chrg_br_nodes[0].sourceline or tx_line)

        # ── InstgAgt mandatory ────────────────────────────────────────────
        instg_nodes = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
        if not instg_nodes:
            cls._issue(report, "ERROR", "MISSING_TX_INSTG_AGT",
                       "//TxInf/InstgAgt",
                       "Transaction Instructing Agent BIC is missing.",
                       "Enter the Instructing Agent BIC in the TxInf block.",
                       tx_line, layer=2)

        # ── InstdAgt mandatory ────────────────────────────────────────────
        instd_nodes = tx.xpath("*[local-name()='InstdAgt']//*[local-name()='BICFI']")
        if not instd_nodes:
            cls._issue(report, "ERROR", "MISSING_TX_INSTD_AGT",
                       "//TxInf/InstdAgt",
                       "Transaction Instructed Agent BIC is missing.",
                       "Enter the Instructed Agent BIC in the TxInf block.",
                       tx_line, layer=2)

        # ── RtrChain mandatory ────────────────────────────────────────────
        chain_nodes = tx.xpath("*[local-name()='RtrChain']")
        if not chain_nodes:
            cls._issue(report, "ERROR", "MISSING_RETURN_CHAIN",
                       "//TxInf/RtrChain",
                       "Return Chain block is missing. RtrChain is mandatory in pacs.004.",
                       "Add the <RtrChain> block identifying the parties in the return.",
                       tx_line, layer=2)

        # ── RtrRsnInf + return reason code ────────────────────────────────
        rsn_nodes = tx.xpath("*[local-name()='RtrRsnInf']")
        if not rsn_nodes:
            cls._issue(report, "ERROR", "MISSING_RETURN_REASON",
                       "//TxInf/RtrRsnInf",
                       "Return Reason Information is missing.",
                       "Add a <RtrRsnInf> block with a valid ISO 20022 return reason code.",
                       tx_line, layer=2)
        else:
            rsn_inf = rsn_nodes[0]
            cd_nodes = rsn_inf.xpath(
                "*[local-name()='Rsn']/*[local-name()='Cd']"
            )
            if cd_nodes:
                cd_val = cls._text(cd_nodes[0])
                if return_reasons and cd_val not in return_reasons:
                    cls._issue(report, "ERROR", "MISSING_OR_INVALID_RETURN_REASON_CODE",
                               "//TxInf/RtrRsnInf/Rsn/Cd",
                               f"Return reason code '{cd_val}' is not a valid "
                               "ISO ExternalReturnReason1Code. "
                               "Common codes: DUPL, CUST, TECH, FRAD, AC01, AC04, NARR.",
                               "Use a valid ISO 20022 return reason code.",
                               cd_nodes[0].sourceline or rsn_inf.sourceline or tx_line)
            elif not rsn_inf.xpath("*[local-name()='Rsn']"):
                cls._issue(report, "ERROR", "MISSING_OR_INVALID_RETURN_REASON_CODE",
                           "//TxInf/RtrRsnInf/Rsn",
                           "Return Reason code block (Rsn/Cd or Rsn/Prtry) is missing.",
                           "Add <Rsn><Cd>NARR</Cd></Rsn> with a valid ISO return reason code.",
                           rsn_inf.sourceline or tx_line)

        # ── R9: OrgnlIntrBkSttlmDt and OrgnlTxRef/IntrBkSttlmDt mutually exclusive ─
        # If OriginalInterbankSettlementDate is present, then
        # OriginalTransactionReference/InterbankSettlementDate must NOT be used.
        orig_sttlm_dt = tx.xpath("*[local-name()='OrgnlIntrBkSttlmDt']")
        if orig_sttlm_dt and cls._text(orig_sttlm_dt[0]):
            ref_dt = tx.xpath(
                "*[local-name()='OrgnlTxRef']/*[local-name()='IntrBkSttlmDt']"
            )
            if ref_dt:
                cls._issue(report, "ERROR", "DUPLICATE_ORIG_STTLM_DT",
                           "//TxInf/OrgnlTxRef/IntrBkSttlmDt",
                           "OriginalInterbankSettlementDate is present, so "
                           "OriginalTransactionReference/InterbankSettlementDate must not be used. "
                           "They are mutually exclusive "
                           "(CBPR_Interbank_Settlement_Date_FormalRule R9).",
                           "Remove <OrgnlTxRef><IntrBkSttlmDt> — keep only the "
                           "transaction-level <OrgnlIntrBkSttlmDt>.",
                           ref_dt[0].sourceline or tx_line)

        # ── R10: OrgnlIntrBkSttlmAmt and OrgnlTxRef/IntrBkSttlmAmt mutually exclusive ─
        # If OriginalInterbankSettlementAmount is present, then
        # OriginalTransactionReference/InterbankSettlementAmount must NOT be used.
        orig_sttlm_amt = tx.xpath("*[local-name()='OrgnlIntrBkSttlmAmt']")
        if orig_sttlm_amt:
            ref_amt = tx.xpath(
                "*[local-name()='OrgnlTxRef']/*[local-name()='IntrBkSttlmAmt']"
            )
            if ref_amt:
                cls._issue(report, "ERROR", "DUPLICATE_ORIG_STTLM_AMT",
                           "//TxInf/OrgnlTxRef/IntrBkSttlmAmt",
                           "OriginalInterbankSettlementAmount is present, so "
                           "OriginalTransactionReference/InterbankSettlementAmount must not be used. "
                           "They are mutually exclusive "
                           "(CBPR_Interbank_Settlement_Amount_FormalRule R10).",
                           "Remove <OrgnlTxRef><IntrBkSttlmAmt> — keep only the "
                           "transaction-level <OrgnlIntrBkSttlmAmt>.",
                           ref_amt[0].sourceline or tx_line)

        # ── R13: OriginalMessageNameIdentification — valid message types ────
        orig_msg_nm = tx.xpath(
            "*[local-name()='OrgnlGrpInf']/*[local-name()='OrgnlMsgNmId']"
        )
        for node in orig_msg_nm:
            val = cls._text(node)
            if val and not _VALID_ORIG_MSG_NAMES_PACS004.match(val):
                cls._issue(report, "ERROR", "INVALID_ORIG_MSG_NAME_ID",
                           "//TxInf/OrgnlGrpInf/OrgnlMsgNmId",
                           f"OriginalMessageNameIdentification '{val}' is not valid for pacs.004. "
                           "Must be pacs.002, pacs.008, pacs.009, or pacs.010 with version suffix "
                           "(e.g. pacs.008.001.08) "
                           "(CBPR_Original_Message_Name_Identification_FormalRule R13).",
                           "Use a valid CBPR+ original message name such as 'pacs.008.001.08'.",
                           node.sourceline or tx_line)
