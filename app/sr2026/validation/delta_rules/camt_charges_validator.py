"""
SR2026 camt.105 / camt.106 Charges Validator
=============================================
Implements FormalRules for ChargesPaymentNotification (camt.105)
and ChargesPaymentRequest (camt.106).

camt.105 SingleCharge rules:
  R13: ChargesIdentification slash rules
  R14: MsgNameId=pacs.008/009/MT103/202/205 → InstructionIdentification mandatory
  R15: Record/TotalChargesPerRecord/TotalChargesAmount.Currency must equal all
       Record/ChargesBreakdown/Amount.Currency values
  R16: MsgNameId=pacs.008/009/MT103/202/205 → UETR mandatory
  R17: TotalChargesAmount = sum of all ChargesBreakdown/Amount
  R18: NumberOfChargesBreakdownItems = count of ChargesBreakdown elements
  R19: UnderlyingTransaction must provide at least one ID besides MsgNameId
  R20: UnderlyingTransaction/InstructionIdentification slash rules

camt.105 MultipleCharges additionally:
  R6:  GroupHeader/TotalCharges/TotalChargesAmount = sum of all
       Record/TotalChargesPerRecord/TotalChargesAmount
  R7:  GroupHeader/TotalCharges/NumberOfChargesRecords = count of Record elements
  R8:  GroupHeader/TotalChargesAmount.Currency must equal all
       Record/TotalChargesPerRecord/TotalChargesAmount.Currency values

camt.106 SingleCharge rules:
  R6:  DebtorAgent and ChargesAccountAgent are mutually exclusive, one must be present
  R20: ChargesIdentification slash rules
  R21-R26: Same arithmetic rules as camt.105

camt.106 MultipleCharges additionally:
  R6-R8: Same GroupHeader total rules as camt.105 MultipleCharges
"""

import re
from decimal import Decimal, InvalidOperation
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

_SLASH_RE = re.compile(r'^/|/$|//')
_PACS_MT_RE = re.compile(r'^pacs\.00[89]\.001\.[0-9]{2}$|^MT(103|202|205)$')
_UETR_RE = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$'
)

# Other identification elements (besides MsgNameId) in UnderlyingTransaction
_UNDERLYING_TX_IDS = {
    "MsgId", "AcctSvcrRef", "PmtInfId", "InstrId", "EndToEndId",
    "UETR", "AcctOwnrTxId", "AcctSvcrTxId",
}


def _t(node) -> str:
    return (node.text or "").strip() if node is not None else ""


def _add(report: ValidationReport, severity: str, code: str, path: str,
         message: str, fix: str, line: int = 1):
    report.add_issue(ValidationIssue(
        severity=severity, code=code, layer=3,
        path=path, message=message, fix=fix, line=line or 1,
    ))


def _dec(val: str) -> Decimal:
    try:
        return Decimal(val)
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _ccy(node) -> str:
    return node.get("Ccy", "").upper() if node is not None else ""


class CamtChargesValidator:
    """camt.105 and camt.106 charges rule validation."""

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        msg = (message_type or "").lower()
        if "camt.105" in msg:
            cls._validate_charges_doc(root, report, "camt.105")
        elif "camt.106" in msg:
            cls._validate_charges_doc(root, report, "camt.106")

    @classmethod
    def _validate_charges_doc(cls, root: etree._Element, report: ValidationReport, msg: str):
        is_106 = msg == "camt.106"

        # camt.106 R6: DebtorAgent / ChargesAccountAgent mutual exclusion
        if is_106:
            for doc in root.xpath("//*[local-name()='ChrgsRqst'] | //*[local-name()='ChrgsNtfctn']"):
                src = doc.sourceline or 1
                debtor_agt = doc.xpath(
                    "*[local-name()='GrpHdr']/*[local-name()='ChrgsAcctAgt']"
                )
                per_tx_debtor = doc.xpath(
                    ".//*[local-name()='Rcrd']/*[local-name()='DbtAgt']"
                )
                if debtor_agt and per_tx_debtor:
                    _add(report, "ERROR", "DEBTOR_CHARGESACCT_AGENT_MUTUALLY_EXCLUSIVE",
                         "//GrpHdr/ChrgsAcctAgt",
                         "DebtorAgent (per-record) and ChargesAccountAgent (group header) "
                         "are mutually exclusive — both cannot be present "
                         "(CBPR_DebtorAgent_ChargeAccountAgent_MutuallyExclusive_FormalRule R6).",
                         "Use either GroupHeader/ChargesAccountAgent or per-record DebtorAgent, not both.",
                         src)
                elif not debtor_agt and not per_tx_debtor:
                    _add(report, "ERROR", "MISSING_DEBTOR_OR_CHARGESACCT_AGENT",
                         "//GrpHdr/ChrgsAcctAgt",
                         "Either DebtorAgent (per-record) or ChargesAccountAgent (group header) "
                         "must be present (CBPR_DebtorAgent_ChargeAccountAgent_MutuallyExclusive_FormalRule R6).",
                         "Add <ChrgsAcctAgt> in GroupHeader or <DbtAgt> in each Record.",
                         src)

        # Validate group-level totals
        for per_tx in root.xpath("//*[local-name()='PerTx']"):
            cls._validate_group_totals(per_tx, report, msg)
            # Validate each record
            for rcrd in per_tx.xpath("*[local-name()='Rcrd']"):
                cls._validate_record(rcrd, report, per_tx, is_106)

        # ChargesIdentification slash rules
        slash_tag = "ChrgsId"
        for node in root.xpath(f"//*[local-name()='{slash_tag}']"):
            val = _t(node)
            if val and _SLASH_RE.search(val):
                _add(report, "ERROR", "INVALID_CHARGES_ID",
                     f"//{slash_tag}",
                     f"<ChargesIdentification> '{val}' must not start/end with '/' "
                     "or contain '//' (CBPR_ChargeIdentification_FormalRule).",
                     "Remove slashes from ChargesIdentification.",
                     node.sourceline or 1)

        # InstructionIdentification slash rules (underlying transaction)
        for node in root.xpath("//*[local-name()='UndrlygTx']/*[local-name()='InstrId']"):
            val = _t(node)
            if val and _SLASH_RE.search(val):
                _add(report, "ERROR", "INVALID_UNDERLYING_INSTR_ID",
                     "//UndrlygTx/InstrId",
                     f"UnderlyingTransaction/InstructionIdentification '{val}' must not "
                     "start/end with '/' or contain '//' "
                     "(CBPR_Instruction_Identification_FormalRule).",
                     "Remove slashes from InstructionIdentification.",
                     node.sourceline or 1)

    @classmethod
    def _validate_group_totals(cls, per_tx: etree._Element,
                               report: ValidationReport, msg: str):
        """Validate GroupHeader TotalCharges totals against sum of Records."""
        # Find GroupHeader/TotalCharges (navigate up to the document level)
        parent = per_tx.getparent()
        if parent is None:
            return

        total_amt_nodes = parent.xpath(
            "*[local-name()='GrpHdr']/*[local-name()='TtlChrgs']/*[local-name()='TtlChrgsAmt']"
        )
        total_nb_nodes = parent.xpath(
            "*[local-name()='GrpHdr']/*[local-name()='TtlChrgs']/*[local-name()='NbOfChrgsRcrds']"
        )
        records = per_tx.xpath("*[local-name()='Rcrd']")
        if not records:
            return

        # R7/R8: NumberOfChargesRecords = count of Records
        if total_nb_nodes:
            nb_val = _t(total_nb_nodes[0])
            try:
                if int(nb_val) != len(records):
                    _add(report, "ERROR", "CHARGES_RECORD_COUNT_MISMATCH",
                         "//GrpHdr/TtlChrgs/NbOfChrgsRcrds",
                         f"GroupHeader/TotalCharges/NumberOfChargesRecords is '{nb_val}' "
                         f"but {len(records)} Record elements found "
                         "(CBPR_GroupHeader_NumberOfChargesRecords_FormalRule).",
                         f"Set NumberOfChargesRecords to {len(records)}.",
                         total_nb_nodes[0].sourceline or 1)
            except (ValueError, TypeError):
                pass

        # R6/R7: TotalChargesAmount = sum of all Record/TotalChargesPerRecord/TotalChargesAmount
        if total_amt_nodes:
            total_amt_node = total_amt_nodes[0]
            total_amt_ccy = _ccy(total_amt_node)
            total_amt_val = _dec(_t(total_amt_node))

            record_totals: list = []
            ccys: set = set()
            for rcrd in records:
                rcrd_total = rcrd.xpath(
                    "*[local-name()='TtlChrgsPerRcrd']/*[local-name()='TtlChrgsAmt']"
                )
                if rcrd_total:
                    record_totals.append(_dec(_t(rcrd_total[0])))
                    ccys.add(_ccy(rcrd_total[0]))

            # R8: Currency must match
            if ccys and total_amt_ccy and ccys != {total_amt_ccy}:
                _add(report, "ERROR", "CHARGES_TOTAL_CURRENCY_MISMATCH",
                     "//GrpHdr/TtlChrgs/TtlChrgsAmt/@Ccy",
                     f"GroupHeader TotalChargesAmount currency '{total_amt_ccy}' does not match "
                     f"Record currencies {sorted(ccys)} "
                     "(CBPR_GroupHeader_TotalChargesAmount_Currency_FormalRule).",
                     "Ensure all records and the group total use the same currency.",
                     total_amt_node.sourceline or 1)

            # R6: Sum check
            if record_totals:
                record_sum = sum(record_totals)
                if record_sum != total_amt_val:
                    _add(report, "ERROR", "CHARGES_TOTAL_AMOUNT_MISMATCH",
                         "//GrpHdr/TtlChrgs/TtlChrgsAmt",
                         f"GroupHeader TotalChargesAmount '{total_amt_val}' does not equal "
                         f"sum of Record totals '{record_sum}' "
                         "(CBPR_GroupHeader_TotalChargesAmount_Sum_FormalRule).",
                         f"Set GroupHeader TotalChargesAmount to {record_sum}.",
                         total_amt_node.sourceline or 1)

    @classmethod
    def _validate_record(cls, rcrd: etree._Element, report: ValidationReport,
                         per_tx: etree._Element, is_106: bool):
        src = rcrd.sourceline or 1

        # Get all ChargesBreakdown entries
        breakdowns = rcrd.xpath("*[local-name()='ChrgsBreakdown']")

        # R18/R23: NumberOfChargesBreakdownItems = count of ChargesBreakdown
        nb_items_nodes = rcrd.xpath(
            "*[local-name()='TtlChrgsPerRcrd']/*[local-name()='NbOfChrgsBreakdownItms']"
        )
        if nb_items_nodes:
            nb_val = _t(nb_items_nodes[0])
            try:
                if int(nb_val) != len(breakdowns):
                    _add(report, "ERROR", "BREAKDOWN_COUNT_MISMATCH",
                         "//TtlChrgsPerRcrd/NbOfChrgsBreakdownItms",
                         f"NumberOfChargesBreakdownItems is '{nb_val}' but "
                         f"{len(breakdowns)} ChargesBreakdown elements found "
                         "(CBPR_Record_NumberOfChargesBreakdownItems_FormalRule).",
                         f"Set NumberOfChargesBreakdownItems to {len(breakdowns)}.",
                         nb_items_nodes[0].sourceline or src)
            except (ValueError, TypeError):
                pass

        # Get total charges amount for this record
        total_nodes = rcrd.xpath(
            "*[local-name()='TtlChrgsPerRcrd']/*[local-name()='TtlChrgsAmt']"
        )

        if total_nodes and breakdowns:
            total_node = total_nodes[0]
            total_val = _dec(_t(total_node))
            total_ccy = _ccy(total_node)

            # R15/R17/R24: TotalChargesAmount = sum of ChargesBreakdown/Amount
            bd_amounts = []
            bd_ccys: set = set()
            for bd in breakdowns:
                amt_nodes = bd.xpath("*[local-name()='Amt']")
                if amt_nodes:
                    bd_amounts.append(_dec(_t(amt_nodes[0])))
                    bd_ccys.add(_ccy(amt_nodes[0]))

            # Currency consistency check
            if bd_ccys and total_ccy and bd_ccys != {total_ccy}:
                _add(report, "ERROR", "CHARGES_BREAKDOWN_CURRENCY_MISMATCH",
                     "//ChrgsBreakdown/Amt/@Ccy",
                     f"Record TotalChargesAmount currency '{total_ccy}' does not match "
                     f"ChargesBreakdown currencies {sorted(bd_ccys)} "
                     "(CBPR_Record_ChargesBreakdownAmount_Currency_FormalRule).",
                     "Ensure all ChargesBreakdown amounts use the same currency as the record total.",
                     total_node.sourceline or src)

            # Sum check
            if bd_amounts:
                bd_sum = sum(bd_amounts)
                if bd_sum != total_val:
                    _add(report, "ERROR", "CHARGES_BREAKDOWN_SUM_MISMATCH",
                         "//TtlChrgsPerRcrd/TtlChrgsAmt",
                         f"Record TotalChargesAmount '{total_val}' does not equal "
                         f"sum of ChargesBreakdown amounts '{bd_sum}' "
                         "(CBPR_Record_TotalChargeAmount_Sum_FormalRule).",
                         f"Set Record TotalChargesAmount to {bd_sum}.",
                         total_node.sourceline or src)

        # R14/R16/R19/R22: UnderlyingTransaction conditions
        for undrlg in rcrd.xpath("*[local-name()='UndrlygTx']"):
            cls._validate_underlying_tx(undrlg, report)

    @classmethod
    def _validate_underlying_tx(cls, undrlg: etree._Element, report: ValidationReport):
        src = undrlg.sourceline or 1

        msg_nm_nodes = undrlg.xpath("*[local-name()='MsgNmId']")
        msg_nm = _t(msg_nm_nodes[0]) if msg_nm_nodes else ""
        is_pacs_mt = bool(msg_nm and _PACS_MT_RE.match(msg_nm))

        if is_pacs_mt:
            # R14: InstructionIdentification mandatory
            instr_id = undrlg.xpath("*[local-name()='InstrId']")
            if not instr_id or not _t(instr_id[0]):
                _add(report, "ERROR", "MISSING_UNDERLYING_INSTR_ID",
                     "//UndrlygTx/InstrId",
                     f"UnderlyingTransaction MessageNameIdentification is '{msg_nm}' "
                     "which requires InstructionIdentification to be present "
                     "(CBPR_Record_MessageNameIdentification_InstructionIdentification_FormalRule).",
                     "Add <InstrId>...</InstrId> to the UnderlyingTransaction.",
                     src)

            # R16: UETR mandatory
            uetr = undrlg.xpath("*[local-name()='UETR']")
            if not uetr or not _t(uetr[0]):
                _add(report, "ERROR", "MISSING_UNDERLYING_UETR",
                     "//UndrlygTx/UETR",
                     f"UnderlyingTransaction MessageNameIdentification is '{msg_nm}' "
                     "which requires UETR to be present "
                     "(CBPR_Record_MessageNameIdentification_UETR_FormalRule).",
                     "Add <UETR>xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx</UETR> to UnderlyingTransaction.",
                     src)
            elif uetr:
                uetr_val = _t(uetr[0])
                if not _UETR_RE.match(uetr_val):
                    _add(report, "ERROR", "INVALID_UNDERLYING_UETR",
                         "//UndrlygTx/UETR",
                         f"UnderlyingTransaction UETR '{uetr_val}' is not a valid lowercase UUID v4.",
                         "Use a lowercase UUID v4 format.",
                         uetr[0].sourceline or src)

        # R19/R22: UnderlyingTransaction must have at least one other ID besides MsgNameId
        other_ids = [
            child for child in undrlg
            if isinstance(child.tag, str)
            and (child.tag.split('}')[-1] if '}' in child.tag else child.tag) in _UNDERLYING_TX_IDS
            and (child.tag.split('}')[-1] if '}' in child.tag else child.tag) != "MsgNmId"
            and _t(child)
        ]
        if msg_nm and not other_ids:
            _add(report, "ERROR", "MISSING_UNDERLYING_TX_ID",
                 "//UndrlygTx",
                 "UnderlyingTransaction must provide at least one identification besides "
                 "MessageNameIdentification (e.g. InstructionId, EndToEndId, UETR, AccountServicerRef) "
                 "(CBPR_Record_UnderlyingTransaction_Presence_FormalRule).",
                 "Add InstructionIdentification, UETR, or another identifier to UnderlyingTransaction.",
                 src)
