"""
One-shot KB normaliser + enricher.

Converts the 5 "old-structure" CBPR+ SR2025 KB files to the golden structure
used by pain001/camt056/pacs010 (iso_20022_rules -> object, cbpr_plus_formal_rules
-> string array, adds schema_mapping_notes), preserves all existing tags/errors/
possible_fixes, and appends genuinely-missing standard tags + cross-tag rules.

Idempotent: re-running does not duplicate rules, tags, or formal rules.
Consumed by app/services/fix_suggester.py (_KBContext) and read tolerantly via
_as_list, so both list/dict rule shapes load -- this just standardises them.
"""
import json, os, sys

KB = os.path.join(os.path.dirname(__file__), "..", "app", "resources", "KB")
KB = os.path.normpath(KB)
TODAY = "2026-06-04"

ROOT = {
    "pacs003": "FIToFICstmrDrctDbt",
    "pacs004": "PmtRtr",
    "pacs009": "FICdtTrf",
    "pacs009_adv": "FICdtTrf",
    "pacs009_cov": "FICdtTrf",
    "camt057": "NtfctnToRcv",
}
FILES = {
    "pacs003": "pacs003_cbprplus_sr2025_validation_kb.json",
    "pacs004": "pacs004_cbprplus_sr2025_validation_kb.json",
    "pacs009": "pacs009_cbprplus_sr2025_validation_kb.json",
    "pacs009_adv": "pacs009_adv_cbprplus_sr2025_validation_kb.json",
    "pacs009_cov": "pacs009_cov_cbprplus_sr2025_validation_kb.json",
    "camt057": "camt057_cbprplus_sr2025_validation_kb.json",
}
# camt057 is already golden-structured -> reshape is a no-op, only enrich + date.
RESHAPE = {"pacs003", "pacs004", "pacs009", "pacs009_adv", "pacs009_cov"}


def err(eid, code, sev, desc, affected, fixes):
    return {
        "error_id": eid, "error_code": code, "severity": sev,
        "description": desc, "affected_tags": affected, "possible_fixes": fixes,
    }


def tag(msg, path, occ, mand, desc, errors, datatype=None, max_length=None,
        expected_value=None, note=None):
    leaf = path.split("/")[-1]
    xpath = ("/" + path) if path.startswith("AppHdr") else f"/Document/{ROOT[msg]}/{path}"
    d = {"tag": path, "xml_element": leaf, "xpath": xpath,
         "occurrence": occ, "mandatory": mand}
    if datatype is not None:
        d["datatype"] = datatype
    if max_length is not None:
        d["max_length"] = max_length
    if note is not None:
        d["note"] = note
    if expected_value is not None:
        d["expected_value"] = expected_value
    d["description"] = desc
    d["errors"] = errors
    return d


# ---- schema_mapping_notes per message --------------------------------------
SMN = {
 "pacs003": {
  "root_element": "pacs.003.001.08 root is /Document/FIToFICstmrDrctDbt (FIToFICustomerDirectDebit); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must be 'pacs.003.001.08' matching urn:iso:std:iso:20022:tech:xsd:pacs.003.001.08.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ SR2025 FI-to-FI customer direct debit is 'swift.cbprplus.03' (SR2024 and earlier used 'swift.cbprplus.02').",
  "structure": "GrpHdr [1..1] + DrctDbtTxInf [1..n]; CBPR+ restricts the message to a single DrctDbtTxInf. Mandate data is carried under DrctDbtTxInf/DrctDbtTx/MndtRltdInf.",
  "amount_location": "The settlement amount is IntrBkSttlmAmt directly under DrctDbtTxInf (with a Ccy attribute). InstdAmt is the optional instructed amount and requires XchgRate when its currency differs from IntrBkSttlmAmt.",
  "party_roles": "In a direct debit the Cdtr is the party collecting funds and the Dbtr is the party being debited - the opposite cash-flow direction to a credit transfer. CdtrAgt collects from the debtor held at DbtrAgt.",
  "settlement_method": "GrpHdr/SttlmInf/SttlmMtd is mandatory; INDA/INGA settle across the agents' accounts, CLRG requires a clearing system, COVE requires reimbursement agents.",
  "datetime_format": "All ISODateTime fields (AppHdr/CreDt, GrpHdr/CreDtTm) use YYYY-MM-DDTHH:MM:SS+HH:MM; the 'Z' suffix and milliseconds are forbidden under CBPR+.",
  "nb_of_txs_scope": "GrpHdr/NbOfTxs must equal the number of DrctDbtTxInf occurrences (1 under CBPR+)."
 },
 "pacs004": {
  "root_element": "pacs.004.001.10 root is /Document/PmtRtr (PaymentReturn); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must be 'pacs.004.001.10' matching urn:iso:std:iso:20022:tech:xsd:pacs.004.001.10.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ payment return is 'swift.cbprplus.03'.",
  "structure": "GrpHdr [1..1] + TxInf [1..n] (single TxInf under CBPR+). The original payment is referenced by TxInf/OrgnlGrpInf (OrgnlMsgId, OrgnlMsgNmId) and the Orgnl* identifiers; the parties of the returned payment are mirrored in TxInf/RtrChain.",
  "amount_location": "TxInf/RtrdIntrBkSttlmAmt is the returned settlement amount; OrgnlIntrBkSttlmAmt echoes the original; CompstnAmt carries interest compensation. RtrdIntrBkSttlmAmt must not exceed OrgnlIntrBkSttlmAmt.",
  "return_reason": "TxInf/RtrRsnInf/Rsn/Cd uses ExternalReturnReason1Code (e.g. AC04 closed account, AM09 wrong amount, CUST requested by customer, DUPL duplicate, RR04 missing regulatory info); free text goes in AddtlInf.",
  "return_chain": "TxInf/RtrChain carries the parties of the payment being returned (Dbtr, DbtrAgt, Cdtr, CdtrAgt, intermediaries) so funds can be routed back.",
  "datetime_format": "ISODateTime fields use YYYY-MM-DDTHH:MM:SS+HH:MM; no 'Z' suffix, no milliseconds.",
  "nb_of_txs_scope": "GrpHdr/NbOfTxs must equal the number of TxInf occurrences."
 },
 "pacs009": {
  "root_element": "pacs.009.001.08 root is /Document/FICdtTrf (FinancialInstitutionCreditTransfer, CORE variant); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must be 'pacs.009.001.08' matching urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ FI credit transfer (CORE) is 'swift.cbprplus.03'. The COV variant uses 'swift.cbprplus.cov.03'.",
  "structure": "GrpHdr [1..1] + CdtTrfTxInf [1..n] (single CdtTrfTxInf under CBPR+). Both Dbtr and Cdtr are financial institutions (BICFI). There is no underlying customer transfer in the CORE variant (that is the COV variant).",
  "amount_location": "The settlement amount is IntrBkSttlmAmt directly under CdtTrfTxInf with a Ccy attribute (not wrapped in an Amt container as in pain.001).",
  "agent_chain": "Routing uses InstgAgt/InstdAgt plus the optional IntrmyAgt1..3 and PrvsInstgAgt1..3 chains; each agent is identified by BICFI. Branch identification (BrnchId) was removed under CBPR+.",
  "fi_parties": "Because Dbtr and Cdtr are financial institutions they are identified by FinInstnId/BICFI, never by name+address like a customer.",
  "datetime_format": "ISODateTime fields use YYYY-MM-DDTHH:MM:SS+HH:MM; no 'Z' suffix, no milliseconds.",
  "nb_of_txs_scope": "GrpHdr/NbOfTxs must equal the number of CdtTrfTxInf occurrences."
 },
 "pacs009_adv": {
  "root_element": "pacs.009.001.08 ADV root is /Document/FICdtTrf (FinancialInstitutionCreditTransfer, advice variant); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must be 'pacs.009.001.08' matching urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ pacs.009 ADV is 'swift.cbprplus.03'.",
  "structure": "GrpHdr [1..1] + CdtTrfTxInf [1..n]. The ADV (advice) variant may carry CdtTrfTxInf/UndrlygCstmrCdtTrf describing the advised underlying customer credit transfer, plus instruction elements (InstrForCdtrAgt, InstrForNxtAgt).",
  "amount_location": "IntrBkSttlmAmt directly under CdtTrfTxInf carries the FI settlement amount; UndrlygCstmrCdtTrf/InstdAmt carries the underlying customer amount when present.",
  "agent_chain": "InstgAgt/InstdAgt plus IntrmyAgt1..3 and PrvsInstgAgt1..3; agents identified by BICFI; BrnchId removed under CBPR+.",
  "underlying_transfer": "CdtTrfTxInf/UndrlygCstmrCdtTrf mirrors the customer-level Dbtr/Cdtr and remittance of the advised payment; it is informational and must stay consistent with the FI-level transfer.",
  "datetime_format": "ISODateTime fields use YYYY-MM-DDTHH:MM:SS+HH:MM; no 'Z' suffix, no milliseconds.",
  "nb_of_txs_scope": "GrpHdr/NbOfTxs must equal the number of CdtTrfTxInf occurrences."
 },
 "pacs009_cov": {
  "root_element": "pacs.009.001.08 COV root is /Document/FICdtTrf (FinancialInstitutionCreditTransfer, cover variant); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must be 'pacs.009.001.08' matching urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08. Do NOT use a pacs.008 identifier here.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ pacs.009 COV is 'swift.cbprplus.cov.03' (NOT 'swift.cbprplus.03', which is for the CORE / pacs.008 services).",
  "structure": "GrpHdr [1..1] + CdtTrfTxInf [1..n], where each CdtTrfTxInf MUST contain UndrlygCstmrCdtTrf carrying the underlying customer credit transfer being covered (the cover for a pacs.008). The FI-level transfer moves the cover funds between correspondents.",
  "amount_location": "FI-level IntrBkSttlmAmt sits under CdtTrfTxInf; the underlying customer amounts (IntrBkSttlmAmt / InstdAmt) sit under UndrlygCstmrCdtTrf.",
  "cover_propagation": "PmtId/UETR and EndToEndId at the cover level must match the underlying customer transfer (UETR propagation rule CBPR_COV_UETR_PROPAGATION).",
  "reimbursement_agents": "Cover settlement uses reimbursement agents: InstgRmbrsmntAgt and/or InstdRmbrsmntAgt are required when SttlmMtd is COVE; ThrdRmbrsmntAgt is optional.",
  "datetime_format": "ISODateTime fields use YYYY-MM-DDTHH:MM:SS+HH:MM; no 'Z' suffix, no milliseconds.",
  "nb_of_txs_scope": "GrpHdr/NbOfTxs must equal the number of CdtTrfTxInf occurrences."
 },
 "camt057": {
  "root_element": "camt.057.001.08 root is /Document/NtfctnToRcv (NotificationToReceive); the AppHdr (BAH) precedes the Document.",
  "msg_def_idr": "AppHdr/MsgDefIdr must equal 'camt.057.001.08' matching urn:iso:std:iso:20022:tech:xsd:camt.057.001.08.",
  "biz_svc": "AppHdr/BizSvc for CBPR+ camt.057 is 'swift.cbprplus.02' (NOT 'swift.cbprplus.03'); rule CBPR_R8 enforces this.",
  "datetime_format": "ISODateTime fields (AppHdr/CreDt, GrpHdr/CreDtTm) use YYYY-MM-DDTHH:MM:SS+HH:MM; no 'Z' suffix, no milliseconds."
 },
}


# ---- enrichment tags per message -------------------------------------------
def enrich_tags(m):
    if m == "pacs003":
        return [
          tag(m, "GrpHdr/TtlIntrBkSttlmAmt", "[0..1]", False,
              "Total interbank settlement amount for the group. When present must equal the sum of all DrctDbtTxInf/IntrBkSttlmAmt and share their currency.",
              [err("TTLAMT_MISMATCH","CTRL_SUM_MISMATCH","Fatal",
                   "GrpHdr/TtlIntrBkSttlmAmt does not equal the sum of DrctDbtTxInf/IntrBkSttlmAmt.",
                   ["GrpHdr/TtlIntrBkSttlmAmt","DrctDbtTxInf/IntrBkSttlmAmt"],
                   ["Recompute the sum of all DrctDbtTxInf/IntrBkSttlmAmt and set TtlIntrBkSttlmAmt to that value.",
                    "Ensure the Ccy attribute matches the per-transaction settlement currency."]),
               err("TTLAMT_CCY","INVALID_CHARSETS","Fatal",
                   "TtlIntrBkSttlmAmt Ccy attribute is missing or not a valid ISO 4217 code.",
                   ["GrpHdr/TtlIntrBkSttlmAmt"],
                   ["Add a valid Ccy attribute (e.g. Ccy=\"EUR\") matching the transaction currency."])],
              datatype="ActiveCurrencyAndAmount"),
          tag(m, "DrctDbtTxInf/PmtId/InstrId", "[0..1]", False,
              "Point-to-point instruction identification assigned by the instructing party. Max 35 chars.",
              [err("INSTRID_LEN","ID_LENGTH_ERROR","Fatal",
                   "InstrId exceeds 35 characters.",
                   ["DrctDbtTxInf/PmtId/InstrId"],
                   ["Truncate InstrId to a maximum of 35 characters."])],
              datatype="Max35Text", max_length=35),
          tag(m, "DrctDbtTxInf/IntrmyAgt2", "[0..1]", False,
              "Second intermediary agent in the settlement chain. May only be present when IntrmyAgt1 is present.",
              [err("INTRMY2_NO_AGT1","MISSING_EXPECTED_ELEMENT","Fatal",
                   "IntrmyAgt2 present without IntrmyAgt1.",
                   ["DrctDbtTxInf/IntrmyAgt1","DrctDbtTxInf/IntrmyAgt2"],
                   ["Insert IntrmyAgt1 before IntrmyAgt2, or remove IntrmyAgt2."]),
               err("INTRMY2_BIC","INVALID_BICFI","Fatal",
                   "IntrmyAgt2 BICFI is not a valid 8 or 11 character ISO 9362 BIC.",
                   ["DrctDbtTxInf/IntrmyAgt2"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC; remove BrnchId (CBPR+ branch removed)."])]),
        ]
    if m == "pacs004":
        return [
          tag(m, "GrpHdr/GrpRtr", "[0..1]", False,
              "Group return indicator. When 'true' the whole original group is returned and transaction-level original references are not required.",
              [err("GRPRTR_BOOL","SCHEMA_ERR","Fatal",
                   "GrpHdr/GrpRtr must be a boolean ('true' or 'false').",
                   ["GrpHdr/GrpRtr"],
                   ["Set GrpRtr to 'true' or 'false'."])],
              datatype="YesNoIndicator"),
          tag(m, "TxInf/RtrRsnInf/Orgtr", "[0..1]", False,
              "Party that issued the return reason (BICFI or Name). Identifies who initiated the return.",
              [err("ORGTR_BIC","INVALID_BICFI","Warning",
                   "RtrRsnInf/Orgtr BICFI is not a valid ISO 9362 BIC.",
                   ["TxInf/RtrRsnInf/Orgtr"],
                   ["Set Orgtr/Id/OrgId/AnyBIC (or FinInstnId/BICFI) to a valid 8 or 11 character BIC."])]),
          tag(m, "TxInf/OrgnlGrpInf/OrgnlMsgNmId", "[1..1]", True,
              "Message name identifier of the original message being returned (e.g. 'pacs.008.001.08').",
              [err("ORGNLMSGNMID_MISSING","MISSING_MANDATORY_FIELD","Fatal",
                   "OrgnlMsgNmId is absent from OrgnlGrpInf.",
                   ["TxInf/OrgnlGrpInf/OrgnlMsgNmId"],
                   ["Insert OrgnlMsgNmId with the original message identifier, e.g. <OrgnlMsgNmId>pacs.008.001.08</OrgnlMsgNmId>."]),
               err("ORGNLMSGNMID_FMT","SCHEMENAME_INVALID","Warning",
                   "OrgnlMsgNmId is not a recognised ISO 20022 message identifier.",
                   ["TxInf/OrgnlGrpInf/OrgnlMsgNmId"],
                   ["Use the full dotted message identifier of the returned message (pacs.008.001.08, pacs.003.001.08, pacs.009.001.08, ...)."])],
              datatype="Max35Text", max_length=35),
        ]
    if m == "pacs009":
        return [
          tag(m, "CdtTrfTxInf", "[1..1]", True,
              "Credit transfer transaction information block. CBPR+ permits exactly one CdtTrfTxInf per pacs.009 CORE message.",
              [err("CDTTRFTXINF_MISSING","MISSING_MANDATORY_FIELD","Fatal",
                   "CdtTrfTxInf block is absent.",
                   ["CdtTrfTxInf"],
                   ["Insert a CdtTrfTxInf block carrying PmtId, IntrBkSttlmAmt, IntrBkSttlmDt and the agent chain."]),
               err("CDTTRFTXINF_MULTI","CBPR_SINGLE_TX","Fatal",
                   "More than one CdtTrfTxInf present; CBPR+ allows a single transaction per message.",
                   ["CdtTrfTxInf"],
                   ["Split additional transactions into separate messages so each carries exactly one CdtTrfTxInf."])]),
          tag(m, "CdtTrfTxInf/PmtId/InstrId", "[0..1]", False,
              "Point-to-point instruction identification. Max 35 chars.",
              [err("INSTRID_LEN","ID_LENGTH_ERROR","Fatal",
                   "InstrId exceeds 35 characters.",
                   ["CdtTrfTxInf/PmtId/InstrId"],
                   ["Truncate InstrId to a maximum of 35 characters."])],
              datatype="Max35Text", max_length=35),
          tag(m, "CdtTrfTxInf/IntrmyAgt2", "[0..1]", False,
              "Second intermediary agent. Only valid when IntrmyAgt1 is present.",
              [err("INTRMY2_NO_AGT1","MISSING_EXPECTED_ELEMENT","Fatal",
                   "IntrmyAgt2 present without IntrmyAgt1.",
                   ["CdtTrfTxInf/IntrmyAgt1","CdtTrfTxInf/IntrmyAgt2"],
                   ["Insert IntrmyAgt1 before IntrmyAgt2, or remove IntrmyAgt2."]),
               err("INTRMY2_BIC","INVALID_BICFI","Fatal",
                   "IntrmyAgt2 BICFI is invalid.",
                   ["CdtTrfTxInf/IntrmyAgt2"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC."])]),
          tag(m, "CdtTrfTxInf/IntrmyAgt3", "[0..1]", False,
              "Third intermediary agent. Only valid when IntrmyAgt2 is present.",
              [err("INTRMY3_NO_AGT2","MISSING_EXPECTED_ELEMENT","Fatal",
                   "IntrmyAgt3 present without IntrmyAgt2.",
                   ["CdtTrfTxInf/IntrmyAgt2","CdtTrfTxInf/IntrmyAgt3"],
                   ["Insert IntrmyAgt2 before IntrmyAgt3, or remove IntrmyAgt3."])]),
          tag(m, "CdtTrfTxInf/PrvsInstgAgt1", "[0..1]", False,
              "Previous instructing agent 1, used to trace the agent that previously instructed in the chain. Identified by BICFI.",
              [err("PRVAGT1_BIC","INVALID_BICFI","Fatal",
                   "PrvsInstgAgt1 BICFI is invalid.",
                   ["CdtTrfTxInf/PrvsInstgAgt1"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC; remove BrnchId (CBPR+ branch removed)."])]),
        ]
    if m == "pacs009_adv":
        return [
          tag(m, "CdtTrfTxInf/PmtId/InstrId", "[0..1]", False,
              "Point-to-point instruction identification. Max 35 chars.",
              [err("INSTRID_LEN","ID_LENGTH_ERROR","Fatal",
                   "InstrId exceeds 35 characters.",
                   ["CdtTrfTxInf/PmtId/InstrId"],
                   ["Truncate InstrId to a maximum of 35 characters."])],
              datatype="Max35Text", max_length=35),
          tag(m, "CdtTrfTxInf/IntrmyAgt2", "[0..1]", False,
              "Second intermediary agent. Only valid when IntrmyAgt1 is present.",
              [err("INTRMY2_NO_AGT1","MISSING_EXPECTED_ELEMENT","Fatal",
                   "IntrmyAgt2 present without IntrmyAgt1.",
                   ["CdtTrfTxInf/IntrmyAgt1","CdtTrfTxInf/IntrmyAgt2"],
                   ["Insert IntrmyAgt1 before IntrmyAgt2, or remove IntrmyAgt2."]),
               err("INTRMY2_BIC","INVALID_BICFI","Fatal",
                   "IntrmyAgt2 BICFI is invalid.",
                   ["CdtTrfTxInf/IntrmyAgt2"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC."])]),
          tag(m, "CdtTrfTxInf/IntrmyAgt3", "[0..1]", False,
              "Third intermediary agent. Only valid when IntrmyAgt2 is present.",
              [err("INTRMY3_NO_AGT2","MISSING_EXPECTED_ELEMENT","Fatal",
                   "IntrmyAgt3 present without IntrmyAgt2.",
                   ["CdtTrfTxInf/IntrmyAgt2","CdtTrfTxInf/IntrmyAgt3"],
                   ["Insert IntrmyAgt2 before IntrmyAgt3, or remove IntrmyAgt3."])]),
        ]
    if m == "pacs009_cov":
        return [
          tag(m, "CdtTrfTxInf/InstgRmbrsmntAgt", "[0..1]", False,
              "Instructing reimbursement agent for the cover. Required (together with or instead of InstdRmbrsmntAgt) when GrpHdr/SttlmInf/SttlmMtd is COVE.",
              [err("INSTGRMB_REQ_COVE","X00301","Fatal",
                   "SttlmMtd is COVE but neither InstgRmbrsmntAgt nor InstdRmbrsmntAgt is present.",
                   ["GrpHdr/SttlmInf/SttlmMtd","CdtTrfTxInf/InstgRmbrsmntAgt","CdtTrfTxInf/InstdRmbrsmntAgt"],
                   ["Add InstgRmbrsmntAgt and/or InstdRmbrsmntAgt with a valid BICFI when SttlmMtd is COVE.",
                    "If reimbursement agents do not apply, change SttlmMtd to INDA/INGA/CLRG as appropriate."]),
               err("INSTGRMB_BIC","INVALID_BICFI","Fatal",
                   "InstgRmbrsmntAgt BICFI is invalid.",
                   ["CdtTrfTxInf/InstgRmbrsmntAgt"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC."])]),
          tag(m, "CdtTrfTxInf/InstdRmbrsmntAgt", "[0..1]", False,
              "Instructed reimbursement agent for the cover. Required (with or instead of InstgRmbrsmntAgt) when SttlmMtd is COVE.",
              [err("INSTDRMB_BIC","INVALID_BICFI","Fatal",
                   "InstdRmbrsmntAgt BICFI is invalid.",
                   ["CdtTrfTxInf/InstdRmbrsmntAgt"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC."])]),
          tag(m, "CdtTrfTxInf/UndrlygCstmrCdtTrf/IntrmyAgt1", "[0..1]", False,
              "Intermediary agent 1 of the underlying customer credit transfer. Identified by BICFI.",
              [err("UND_INTRMY1_BIC","INVALID_BICFI","Fatal",
                   "UndrlygCstmrCdtTrf/IntrmyAgt1 BICFI is invalid.",
                   ["CdtTrfTxInf/UndrlygCstmrCdtTrf/IntrmyAgt1"],
                   ["Set FinInstnId/BICFI to a valid 8 or 11 character BIC; remove BrnchId (CBPR+ branch removed)."])]),
          tag(m, "CdtTrfTxInf/PmtId/InstrId", "[0..1]", False,
              "Point-to-point instruction identification. Max 35 chars.",
              [err("INSTRID_LEN","ID_LENGTH_ERROR","Fatal",
                   "InstrId exceeds 35 characters.",
                   ["CdtTrfTxInf/PmtId/InstrId"],
                   ["Truncate InstrId to a maximum of 35 characters."])],
              datatype="Max35Text", max_length=35),
        ]
    if m == "camt057":
        return [
          tag(m, "GrpHdr/MsgRcpt", "[0..1]", False,
              "Party authorised to receive the notification. Identified by BICFI under CBPR+.",
              [err("MSGRCPT_BIC","INVALID_BICFI","Warning",
                   "GrpHdr/MsgRcpt BICFI is not a valid ISO 9362 BIC.",
                   ["GrpHdr/MsgRcpt"],
                   ["Set MsgRcpt/Id/OrgId/AnyBIC (or FinInstnId/BICFI) to a valid 8 or 11 character BIC."])]),
          tag(m, "Ntfctn/AcctOwnr", "[0..1]", False,
              "Owner of the account to be credited by the notified items. Identified by name or BICFI.",
              [err("ACCTOWNR_BIC","INVALID_BICFI","Warning",
                   "Ntfctn/AcctOwnr BICFI is invalid.",
                   ["Ntfctn/AcctOwnr"],
                   ["Set AcctOwnr/Id/OrgId/AnyBIC to a valid 8 or 11 character BIC, or supply Nm."])]),
          tag(m, "Ntfctn/Itm/Acct", "[0..1]", False,
              "Account to be credited for this notified item; account identification is a choice of IBAN or Othr.",
              [err("ITM_ACCT_CHOICE","IBAN_XOR_OTHR","Fatal",
                   "Ntfctn/Itm/Acct/Id carries both IBAN and Othr, or neither.",
                   ["Ntfctn/Itm/Acct"],
                   ["Provide exactly one of IBAN or Othr under Acct/Id."]),
               err("ITM_ACCT_IBAN","IBAN_INVALID","Fatal",
                   "Ntfctn/Itm/Acct IBAN fails ISO 13616 check digits.",
                   ["Ntfctn/Itm/Acct"],
                   ["Correct the IBAN so its MOD-97 check digits are valid, or use Othr/Id."])]),
        ]
    return []


# ---- enrichment cross-tag dependency rules per message ---------------------
def enrich_cross(m):
    if m == "pacs003":
        return [{"rule_id":"DEP_TTLAMT","rule":"GrpHdr/TtlIntrBkSttlmAmt, when present, must equal the sum of all DrctDbtTxInf/IntrBkSttlmAmt and share their currency.",
                 "affected_tags":["GrpHdr/TtlIntrBkSttlmAmt","DrctDbtTxInf/IntrBkSttlmAmt"],
                 "fix":"Recompute the total from the transaction amounts and set TtlIntrBkSttlmAmt (with matching Ccy)."}]
    if m == "pacs004":
        return [{"rule_id":"DEP_GRPRTR","rule":"When GrpHdr/GrpRtr = true the whole group is returned; transaction-level OrgnlInstrId/OrgnlEndToEndId are not required, but OrgnlGrpInf must identify the original message.",
                 "affected_tags":["GrpHdr/GrpRtr","TxInf/OrgnlGrpInf"],
                 "fix":"If GrpRtr is true, ensure OrgnlGrpInf/OrgnlMsgId and OrgnlMsgNmId are present; otherwise populate the per-transaction original references."}]
    if m == "pacs009_cov":
        return [{"rule_id":"DEP_COVE_RMBAGT","rule":"When GrpHdr/SttlmInf/SttlmMtd is COVE, at least one of CdtTrfTxInf/InstgRmbrsmntAgt or CdtTrfTxInf/InstdRmbrsmntAgt must be present.",
                 "affected_tags":["GrpHdr/SttlmInf/SttlmMtd","CdtTrfTxInf/InstgRmbrsmntAgt","CdtTrfTxInf/InstdRmbrsmntAgt"],
                 "fix":"Add InstgRmbrsmntAgt and/or InstdRmbrsmntAgt with a valid BICFI, or change SttlmMtd to INDA/INGA/CLRG."}]
    if m == "camt057":
        return [{"rule_id":"DEP_ITM_AMT_CCY","rule":"Each Ntfctn/Itm/Amt must carry a valid ISO 4217 Ccy attribute consistent with the notified account currency.",
                 "affected_tags":["Ntfctn/Itm/Amt","Ntfctn/Acct"],
                 "fix":"Add or correct the Ccy attribute on Itm/Amt to a valid ISO 4217 code matching the account currency."}]
    return []


# ---- reshape helpers --------------------------------------------------------
def to_obj_rules(v):
    """iso_20022_rules: list[{name,description,...}] -> ordered dict{name: description}."""
    if isinstance(v, dict):
        return v
    out = {}
    for r in (v or []):
        if not isinstance(r, dict):
            continue
        key = (r.get("name") or r.get("rule_id") or "Rule").strip()
        desc = (r.get("description") or r.get("error_text") or "").strip()
        k, i = key, 2
        while k in out:
            k = f"{key}_{i}"; i += 1
        out[k] = desc
    return out


def to_str_rules(v):
    """cbpr_plus_formal_rules: list[{description,...}] -> list[str] (deduped, ordered)."""
    if isinstance(v, list) and (not v or isinstance(v[0], str)):
        # already string array -> dedupe preserving order
        seen, out = set(), []
        for s in v:
            if s not in seen:
                seen.add(s); out.append(s)
        return out
    seen, out = set(), []
    for r in (v or []):
        s = r.get("description") if isinstance(r, dict) else r
        s = (s or "").strip()
        if s and s not in seen:
            seen.add(s); out.append(s)
    return out


KEY_ORDER = ["domain", "message_variant", "version", "last_updated",
             "xsd_version", "xsd_namespace", "purpose", "reference_documents",
             "iso_20022_rules", "cbpr_plus_formal_rules", "tags",
             "cross_tag_dependency_rules", "pacs008_to_pacs009_cov_field_mapping",
             "tag_insertion_order", "schema_mapping_notes"]


def reorder(d):
    out = {}
    for k in KEY_ORDER:
        if k in d:
            out[k] = d[k]
    for k in d:               # any leftover keys keep their data
        if k not in out:
            out[k] = d[k]
    return out


def main():
    report = []
    for m, fname in FILES.items():
        path = os.path.join(KB, fname)
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)

        before_tags = len(d.get("tags", []))
        before_cross = len(d.get("cross_tag_dependency_rules", []))

        if m in RESHAPE:
            d["iso_20022_rules"] = to_obj_rules(d.get("iso_20022_rules"))
            d["cbpr_plus_formal_rules"] = to_str_rules(d.get("cbpr_plus_formal_rules"))

        # schema_mapping_notes: merge, never clobber existing keys
        smn = d.get("schema_mapping_notes") or {}
        if not isinstance(smn, dict):
            smn = {}
        for k, v in SMN.get(m, {}).items():
            smn.setdefault(k, v)
        d["schema_mapping_notes"] = smn

        # enrich tags (skip existing paths)
        existing = {t.get("tag") for t in d.get("tags", []) if isinstance(t, dict)}
        added_t = 0
        for t in enrich_tags(m):
            if t["tag"] not in existing:
                d.setdefault("tags", []).append(t); existing.add(t["tag"]); added_t += 1

        # enrich cross rules (skip existing rule_id)
        ex_rid = {r.get("rule_id") for r in d.get("cross_tag_dependency_rules", []) if isinstance(r, dict)}
        added_c = 0
        for r in enrich_cross(m):
            if r["rule_id"] not in ex_rid:
                d.setdefault("cross_tag_dependency_rules", []).append(r); ex_rid.add(r["rule_id"]); added_c += 1

        d["version"] = d.get("version") or "SR2025"
        d["last_updated"] = TODAY
        d = reorder(d)

        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

        iso = d["iso_20022_rules"]
        report.append(
            f"{m:12} iso={'obj' if isinstance(iso, dict) else 'arr'}({len(iso)}) "
            f"cbpr=str[{len(d['cbpr_plus_formal_rules'])}] "
            f"tags {before_tags}->{len(d['tags'])} (+{added_t}) "
            f"cross {before_cross}->{len(d['cross_tag_dependency_rules'])} (+{added_c}) "
            f"smn={len(d['schema_mapping_notes'])}")
    print("\n".join(report))


if __name__ == "__main__":
    main()
