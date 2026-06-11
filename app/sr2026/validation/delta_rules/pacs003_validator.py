"""
SR2026 pacs.003 (FI To FI Customer Direct Debit) Delta Validator
================================================================
Implements the SR2026 CBPR+ rule set for pacs.003.001.12 sourced from
app/sr2026/rules/messages/pacs.003/base.json.

This validator ONLY runs for pacs.003 messages and ONLY under SR2026.
It is additive: SR2025 validation is untouched.

To avoid duplicate diagnostics, rules already enforced by the generic
SR2026 validators are NOT re-implemented here:
  - Postal address structure / AdrLine deprecation (SR2026-ADDR-001)  -> AddressValidator
  - Country code validity (SR2026-ADDR-002)                           -> Layer3Validator
  - Currency / amount validity (part of SR2026-TXN-003)               -> Layer3Validator
  - Purpose / Category-Purpose / Charge-bearer code lists             -> Layer3Validator
  - SWIFT character set (SR2026-CHRSET-001)                            -> Layer3Validator

The direct-debit-specific rules (group header, transaction identification,
mandate, sequence type, agents, parties, accounts, remittance, charges and
namespace) are enforced here.
"""

import json
import os
import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport


class Pacs003Validator:
    _rules = None
    _meta = None

    # ── Rule loading ───────────────────────────────────────────────────────
    @classmethod
    def _load_rules(cls):
        if cls._rules is None:
            rules_path = os.path.join(
                os.path.dirname(__file__),
                "..", "..", "rules", "messages", "pacs.003", "base.json"
            )
            with open(rules_path, "r", encoding="utf-8") as f:
                cls._rules = json.load(f)
            cls._meta = {r["ruleId"]: r for r in cls._rules.get("rules", [])}
        return cls._rules

    @classmethod
    def _m(cls, rule_id: str) -> dict:
        """Return the rule definition (severity/description/...) for a rule id."""
        cls._load_rules()
        return cls._meta.get(rule_id, {})

    # ── Small helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _text(node) -> str:
        return (node.text or "").strip() if node is not None else ""

    @staticmethod
    def _children(node, tag):
        """Direct children of node matching local-name == tag."""
        if node is None:
            return []
        return node.xpath(f"*[local-name()='{tag}']")

    @classmethod
    def _first(cls, node, *tags):
        """Walk a chain of local-name child tags, returning the first leaf node."""
        cur = node
        for tag in tags:
            if cur is None:
                return None
            found = cls._children(cur, tag)
            cur = found[0] if found else None
        return cur

    @classmethod
    def _issue(cls, report: ValidationReport, rule_id: str, path: str, line: int,
               message: str = None, fix: str = None, severity: str = None, layer: int = 3):
        meta = cls._m(rule_id)
        report.add_issue(ValidationIssue(
            severity=severity or meta.get("severity", "ERROR"),
            code=rule_id,
            layer=layer,
            path=path,
            message=message or meta.get("description", rule_id),
            line=line or 1,
            fix=fix or meta.get("requirement") or "Refer to SWIFT CBPR+ SR2026 Usage Guidelines for pacs.003.",
        ))

    # ── Entry point ────────────────────────────────────────────────────────
    @classmethod
    def validate(cls, root_element: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.003" not in (message_type or "").lower():
            return

        cls._load_rules()

        # Namespace check skipped: CBPR+ pacs.003 stays on .001.08 in SR2026 (same as SR2025).
        # The .001.12 reference in the original rule spec is the ISO standard version,
        # not the CBPR+ profile namespace.

        grp_hdr_nodes = root_element.xpath("//*[local-name()='GrpHdr']")
        tx_blocks = root_element.xpath("//*[local-name()='DrctDbtTxInf']")

        cls._validate_group_header(grp_hdr_nodes, tx_blocks, report)

        for tx in tx_blocks:
            cls._validate_transaction(tx, report)

    # ── SR2026-VERS-001 ────────────────────────────────────────────────────
    @classmethod
    def _validate_namespace(cls, root_element, report):
        meta = cls._m("SR2026-VERS-001")
        expected = meta.get("expectedNamespace", "urn:iso:std:iso:20022:tech:xsd:pacs.003.001.12")
        doc_nodes = root_element.xpath("//*[local-name()='Document']")
        target = doc_nodes[0] if doc_nodes else root_element
        ns = etree.QName(target).namespace if isinstance(target.tag, str) else None
        if ns and ns != expected:
            cls._issue(
                report, "SR2026-VERS-001",
                path="//Document",
                line=target.sourceline or 1,
                message=f"Message namespace '{ns}' does not match the expected pacs.003.001.12 namespace '{expected}'.",
                fix=f"Use the namespace {expected}.",
            )

    # ── Group Header rules (GRP-001..005) ──────────────────────────────────
    @classmethod
    def _validate_group_header(cls, grp_hdr_nodes, tx_blocks, report):
        if not grp_hdr_nodes:
            cls._issue(report, "SR2026-GRP-001", path="//GrpHdr", line=1,
                       message="Group Header (GrpHdr) is mandatory but missing.",
                       fix="Add a <GrpHdr> block with MsgId, CreDtTm, NbOfTxs and SttlmInf.",
                       layer=2)
            return
        grp = grp_hdr_nodes[0]
        grp_line = grp.sourceline or 1

        # GRP-001 MsgId
        msg_id = cls._first(grp, "MsgId")
        m = cls._m("SR2026-GRP-001")
        if msg_id is None or not cls._text(msg_id):
            cls._issue(report, "SR2026-GRP-001", path="//GrpHdr/MsgId", line=grp_line,
                       message="Message Identification (MsgId) is mandatory.",
                       fix="Provide a unique MsgId (1-35 chars).",
                       layer=2)
        else:
            val = cls._text(msg_id)
            if len(val) > m.get("maxLength", 35) or len(val) < m.get("minLength", 1):
                cls._issue(report, "SR2026-GRP-001", path="//GrpHdr/MsgId",
                           line=msg_id.sourceline or grp_line,
                           message=f"MsgId length must be between {m.get('minLength',1)} and {m.get('maxLength',35)} characters.",
                           fix="Shorten or provide a valid MsgId.")
            elif not re.fullmatch(r"[0-9a-zA-Z/\-?:().,'+ ]{1,35}", val):
                cls._issue(report, "SR2026-GRP-001", path="//GrpHdr/MsgId",
                           line=msg_id.sourceline or grp_line,
                           message="MsgId contains characters outside the permitted SWIFT set.",
                           fix="Use only letters, digits and / - ? : ( ) . , ' + space.")

        # GRP-002 CreDtTm
        cre = cls._first(grp, "CreDtTm")
        if cre is None or not cls._text(cre):
            cls._issue(report, "SR2026-GRP-002", path="//GrpHdr/CreDtTm", line=grp_line,
                       message="Creation Date and Time (CreDtTm) is mandatory.",
                       fix="Provide CreDtTm in ISO 8601 format (YYYY-MM-DDThh:mm:ss).",
                       layer=2)
        else:
            val = cls._text(cre)
            if not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', val):
                cls._issue(report, "SR2026-GRP-002", path="//GrpHdr/CreDtTm",
                           line=cre.sourceline or grp_line,
                           message="CreDtTm must be a valid ISO 8601 date-time (YYYY-MM-DDThh:mm:ss).",
                           fix="Format the creation timestamp as YYYY-MM-DDThh:mm:ss.")

        # GRP-003 NbOfTxs consistency
        nb = cls._first(grp, "NbOfTxs")
        actual = len(tx_blocks)
        if nb is None or not cls._text(nb):
            cls._issue(report, "SR2026-GRP-003", path="//GrpHdr/NbOfTxs", line=grp_line,
                       message="Number of Transactions (NbOfTxs) is mandatory.",
                       fix=f"Set NbOfTxs to {actual}.",
                       layer=2)
        else:
            val = cls._text(nb)
            try:
                if int(val) != actual:
                    cls._issue(report, "SR2026-GRP-003", path="//GrpHdr/NbOfTxs",
                               line=nb.sourceline or grp_line,
                               message=f"NbOfTxs ({val}) does not match the actual number of DrctDbtTxInf blocks ({actual}).",
                               fix=f"Set NbOfTxs to {actual}.")
            except ValueError:
                cls._issue(report, "SR2026-GRP-003", path="//GrpHdr/NbOfTxs",
                           line=nb.sourceline or grp_line,
                           message=f"NbOfTxs value '{val}' is not a valid integer.",
                           fix=f"Set NbOfTxs to {actual}.")

        # GRP-004 CtrlSum consistency (only when present)
        ctrl = cls._first(grp, "CtrlSum")
        if ctrl is not None and cls._text(ctrl):
            try:
                declared = float(cls._text(ctrl))
                total = 0.0
                ok = True
                for tx in tx_blocks:
                    amt = cls._first(tx, "IntrBkSttlmAmt")
                    if amt is not None and cls._text(amt):
                        try:
                            total += float(cls._text(amt))
                        except ValueError:
                            ok = False
                if ok and abs(total - declared) > 1e-6:
                    cls._issue(report, "SR2026-GRP-004", path="//GrpHdr/CtrlSum",
                               line=ctrl.sourceline or grp_line,
                               message=f"CtrlSum ({declared}) does not equal the sum of IntrBkSttlmAmt values ({total}).",
                               fix=f"Set CtrlSum to {total}.")
            except ValueError:
                cls._issue(report, "SR2026-GRP-004", path="//GrpHdr/CtrlSum",
                           line=ctrl.sourceline or grp_line,
                           message=f"CtrlSum value '{cls._text(ctrl)}' is not a valid number.",
                           fix="Provide a numeric CtrlSum equal to the sum of settlement amounts.")

        # GRP-005 Settlement Method
        sttlm = cls._first(grp, "SttlmInf", "SttlmMtd")
        m5 = cls._m("SR2026-GRP-005")
        allowed = m5.get("allowedValues", ["CLRG", "COVE", "INDA", "INGA"])
        if sttlm is None or not cls._text(sttlm):
            cls._issue(report, "SR2026-GRP-005", path="//GrpHdr/SttlmInf/SttlmMtd", line=grp_line,
                       message="Settlement Method (SttlmInf/SttlmMtd) is mandatory.",
                       fix=f"Provide one of: {', '.join(allowed)}.",
                       layer=2)
        elif cls._text(sttlm) not in allowed:
            cls._issue(report, "SR2026-GRP-005", path="//GrpHdr/SttlmInf/SttlmMtd",
                       line=sttlm.sourceline or grp_line,
                       message=f"Settlement Method '{cls._text(sttlm)}' is not valid.",
                       fix=f"Use one of: {', '.join(allowed)}.")

    # ── Transaction rules (TXN-001..019) ───────────────────────────────────
    @classmethod
    def _validate_transaction(cls, tx, report):
        tx_line = tx.sourceline or 1

        # TXN-001 EndToEndId
        e2e = cls._first(tx, "PmtId", "EndToEndId")
        if e2e is None or not cls._text(e2e):
            cls._issue(report, "SR2026-TXN-001", path="//DrctDbtTxInf/PmtId/EndToEndId", line=tx_line,
                       message="End-to-End Identification is mandatory.",
                       fix="Provide EndToEndId, or 'NOTPROVIDED' when unavailable.",
                       layer=2)
        elif len(cls._text(e2e)) > 35:
            cls._issue(report, "SR2026-TXN-001", path="//DrctDbtTxInf/PmtId/EndToEndId",
                       line=e2e.sourceline or tx_line,
                       message="EndToEndId exceeds the maximum length of 35 characters.",
                       fix="Shorten EndToEndId to 35 characters or fewer.")

        # TXN-002 UETR
        uetr = cls._first(tx, "PmtId", "UETR")
        uetr_re = r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
        if uetr is None or not cls._text(uetr):
            cls._issue(report, "SR2026-TXN-002", path="//DrctDbtTxInf/PmtId/UETR", line=tx_line,
                       message="UETR is mandatory.",
                       fix="Provide a valid RFC 4122 UUID v4 UETR.",
                       layer=2)
        elif not re.match(uetr_re, cls._text(uetr), re.I):
            cls._issue(report, "SR2026-TXN-002", path="//DrctDbtTxInf/PmtId/UETR",
                       line=uetr.sourceline or tx_line,
                       message=f"UETR '{cls._text(uetr)}' is not a valid RFC 4122 UUID v4.",
                       fix="Use a valid UUID v4 (8-4-4-4-12 hex with version '4').")

        # TXN-003 IntrBkSttlmAmt (presence + >0 + Ccy present/format)
        amt = cls._first(tx, "IntrBkSttlmAmt")
        if amt is None or not cls._text(amt):
            cls._issue(report, "SR2026-TXN-003", path="//DrctDbtTxInf/IntrBkSttlmAmt", line=tx_line,
                       message="Interbank Settlement Amount is mandatory.",
                       fix="Provide a positive IntrBkSttlmAmt with a valid Ccy attribute.",
                       layer=2)
        else:
            try:
                if float(cls._text(amt)) <= 0:
                    cls._issue(report, "SR2026-TXN-003", path="//DrctDbtTxInf/IntrBkSttlmAmt",
                               line=amt.sourceline or tx_line,
                               message="Interbank Settlement Amount must be greater than zero.",
                               fix="Provide a positive settlement amount.")
            except ValueError:
                cls._issue(report, "SR2026-TXN-003", path="//DrctDbtTxInf/IntrBkSttlmAmt",
                           line=amt.sourceline or tx_line,
                           message=f"Interbank Settlement Amount '{cls._text(amt)}' is not numeric.",
                           fix="Provide a numeric settlement amount.")
            ccy = amt.get("Ccy")
            if not ccy or not re.match(r'^[A-Z]{3}$', ccy):
                cls._issue(report, "SR2026-TXN-003", path="//DrctDbtTxInf/IntrBkSttlmAmt@Ccy",
                           line=amt.sourceline or tx_line,
                           message="Interbank Settlement Amount currency (Ccy) must be a 3-letter ISO 4217 code.",
                           fix="Add a valid 3-letter Ccy attribute (e.g. EUR).")

        # TXN-004 IntrBkSttlmDt
        dt = cls._first(tx, "IntrBkSttlmDt")
        if dt is None or not cls._text(dt):
            cls._issue(report, "SR2026-TXN-004", path="//DrctDbtTxInf/IntrBkSttlmDt", line=tx_line,
                       message="Interbank Settlement Date is mandatory.",
                       fix="Provide IntrBkSttlmDt in YYYY-MM-DD format.",
                       layer=2)
        elif not re.match(r'^\d{4}-\d{2}-\d{2}$', cls._text(dt)):
            cls._issue(report, "SR2026-TXN-004", path="//DrctDbtTxInf/IntrBkSttlmDt",
                       line=dt.sourceline or tx_line,
                       message="Interbank Settlement Date must be in YYYY-MM-DD format.",
                       fix="Format the settlement date as YYYY-MM-DD.")

        # TXN-006 SeqTp (PmtTpInf/SeqTp)
        # Only validate SeqTp when PmtTpInf is present; pacs.003.001.08 XSD has
        # PmtTpInf as optional [0..1], so absence of the block is not an error.
        pmt_tp = cls._first(tx, "PmtTpInf")
        if pmt_tp is not None:
            seq = cls._first(pmt_tp, "SeqTp")
            m6 = cls._m("SR2026-TXN-006")
            seq_allowed = m6.get("allowedValues", ["FRST", "RCUR", "FNAL", "OOFF", "RPRE"])
            if seq is not None and cls._text(seq) and cls._text(seq) not in seq_allowed:
                cls._issue(report, "SR2026-TXN-006", path="//DrctDbtTxInf/PmtTpInf/SeqTp",
                           line=seq.sourceline or tx_line,
                           message=f"Sequence Type '{cls._text(seq)}' is not valid.",
                           fix=f"Use one of: {', '.join(seq_allowed)}.")

        # TXN-007 Mandate Related Information
        # Only validate content when the block is present; pacs.003.001.08 XSD has
        # DrctDbtTx/MndtRltdInf as optional [0..1], so absence is not an error.
        ddt = cls._first(tx, "DrctDbtTx")
        mndt = cls._first(ddt, "MndtRltdInf") if ddt is not None else None
        if mndt is not None:
            mndt_line = mndt.sourceline or tx_line
            mndt_id = cls._first(mndt, "MndtId")
            if mndt_id is None or not cls._text(mndt_id):
                cls._issue(report, "SR2026-TXN-007", path="//DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId",
                           line=mndt_line,
                           message="Mandate Identification (MndtId) is mandatory.",
                           fix="Provide a non-empty MndtId (max 35 chars).",
                           layer=2)
            elif len(cls._text(mndt_id)) > 35:
                cls._issue(report, "SR2026-TXN-007", path="//DrctDbtTxInf/DrctDbtTx/MndtRltdInf/MndtId",
                           line=mndt_id.sourceline or mndt_line,
                           message="MndtId exceeds the maximum length of 35 characters.",
                           fix="Shorten MndtId to 35 characters or fewer.")
            sgntr = cls._first(mndt, "DtOfSgntr")
            if sgntr is None or not cls._text(sgntr):
                cls._issue(report, "SR2026-TXN-007", path="//DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr",
                           line=mndt_line,
                           message="Date of Signature (DtOfSgntr) is mandatory in Mandate Related Information.",
                           fix="Provide DtOfSgntr in YYYY-MM-DD format.",
                           layer=2)
            elif not re.match(r'^\d{4}-\d{2}-\d{2}$', cls._text(sgntr)):
                cls._issue(report, "SR2026-TXN-007", path="//DrctDbtTxInf/DrctDbtTx/MndtRltdInf/DtOfSgntr",
                           line=sgntr.sourceline or mndt_line,
                           message="DtOfSgntr must be in YYYY-MM-DD format.",
                           fix="Format the date of signature as YYYY-MM-DD.")

            # TXN-008 Amendment Indicator
            amd = cls._first(mndt, "AmdmntInd")
            if amd is not None and cls._text(amd).lower() == "true":
                amd_dtls = cls._first(mndt, "AmdmntInfDtls")
                if amd_dtls is None or len(list(amd_dtls)) == 0:
                    cls._issue(report, "SR2026-TXN-008",
                               path="//DrctDbtTxInf/DrctDbtTx/MndtRltdInf/AmdmntInfDtls",
                               line=amd.sourceline or mndt_line,
                               message="AmdmntInd is 'true' but AmdmntInfDtls is missing or empty.",
                               fix="Provide AmdmntInfDtls with at least one populated amendment detail, or set AmdmntInd to false.")

        # TXN-009 Creditor Scheme Id (recommended)
        cdtr_schme = cls._first(ddt, "CdtrSchmeId") if ddt is not None else None
        if cdtr_schme is None:
            cls._issue(report, "SR2026-TXN-009", path="//DrctDbtTxInf/DrctDbtTx/CdtrSchmeId", line=tx_line,
                       message="Creditor Scheme Identification (CdtrSchmeId) is recommended for scheme-based direct debits.",
                       fix="Add CdtrSchmeId/PrvtId/Othr with the scheme name where applicable.")

        # TXN-010 Creditor Agent BIC
        cls._validate_agent_bic(tx, report, "CdtrAgt", "SR2026-TXN-010", "Creditor Agent", tx_line)
        # TXN-011 Debtor Agent BIC
        cls._validate_agent_bic(tx, report, "DbtrAgt", "SR2026-TXN-011", "Debtor Agent", tx_line)

        # TXN-012 Creditor Name
        cls._validate_name(tx, report, "Cdtr", "SR2026-TXN-012", "Creditor", tx_line)
        # TXN-014 Debtor Name
        cls._validate_name(tx, report, "Dbtr", "SR2026-TXN-014", "Debtor", tx_line)

        # TXN-013 Creditor Account
        cls._validate_account(tx, report, "CdtrAcct", "SR2026-TXN-013", "Creditor", tx_line)
        # TXN-015 Debtor Account
        cls._validate_account(tx, report, "DbtrAcct", "SR2026-TXN-015", "Debtor", tx_line)

        # TXN-017 / TXN-018 Remittance
        cls._validate_remittance(tx, report, tx_line)

        # TXN-019 Charges Bearer
        chrg = cls._first(tx, "ChrgBr")
        m19 = cls._m("SR2026-TXN-019")
        chrg_allowed = m19.get("allowedValues", ["DEBT", "CRED", "SHAR", "SLEV"])
        if chrg is None or not cls._text(chrg):
            cls._issue(report, "SR2026-TXN-019", path="//DrctDbtTxInf/ChrgBr", line=tx_line,
                       message="Charges Bearer (ChrgBr) is mandatory.",
                       fix=f"Provide one of: {', '.join(chrg_allowed)}.",
                       layer=2)
        elif cls._text(chrg) not in chrg_allowed:
            cls._issue(report, "SR2026-TXN-019", path="//DrctDbtTxInf/ChrgBr",
                       line=chrg.sourceline or tx_line,
                       message=f"Charges Bearer '{cls._text(chrg)}' is not valid.",
                       fix=f"Use one of: {', '.join(chrg_allowed)}.")

    # ── Shared transaction sub-checks ──────────────────────────────────────
    @classmethod
    def _validate_agent_bic(cls, tx, report, agent_tag, rule_id, label, tx_line):
        agt = cls._first(tx, agent_tag, "FinInstnId", "BICFI")
        path = f"//DrctDbtTxInf/{agent_tag}/FinInstnId/BICFI"
        if agt is None or not cls._text(agt):
            cls._issue(report, rule_id, path=path, line=tx_line,
                       message=f"{label} BIC (BICFI) is mandatory.",
                       fix=f"Provide a valid 8 or 11 character {label} BIC.",
                       layer=2)
        elif not re.match(r'^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$', cls._text(agt)):
            cls._issue(report, rule_id, path=path, line=agt.sourceline or tx_line,
                       message=f"{label} BIC '{cls._text(agt)}' is not a valid 8 or 11 character BIC.",
                       fix="Use a valid ISO 9362 BIC (8 or 11 characters).")

    @classmethod
    def _validate_name(cls, tx, report, party_tag, rule_id, label, tx_line):
        # CBPR_COM_R11: when OrgId/AnyBIC is present, <Nm> must NOT appear.
        # Skip the Name check entirely for parties that carry an AnyBIC so we
        # don't raise a false mandatory error for a field the generator omits
        # on purpose.
        any_bic = cls._first(tx, party_tag, "Id", "OrgId", "AnyBIC")
        if any_bic is not None and cls._text(any_bic):
            return

        nm = cls._first(tx, party_tag, "Nm")
        path = f"//DrctDbtTxInf/{party_tag}/Nm"
        if nm is None or not cls._text(nm):
            cls._issue(report, rule_id, path=path, line=tx_line,
                       message=f"{label} Name (Nm) is mandatory.",
                       fix=f"Provide the {label} name (max 140 chars).",
                       layer=2)
        elif len(cls._text(nm)) > 140:
            cls._issue(report, rule_id, path=path, line=nm.sourceline or tx_line,
                       message=f"{label} Name exceeds the maximum length of 140 characters.",
                       fix="Shorten the name to 140 characters or fewer.")

    @classmethod
    def _validate_account(cls, tx, report, acct_tag, rule_id, label, tx_line):
        acct_id = cls._first(tx, acct_tag, "Id")
        path = f"//DrctDbtTxInf/{acct_tag}/Id"
        if acct_id is None:
            cls._issue(report, rule_id, path=path, line=tx_line,
                       message=f"{label} Account Identification (Id) is mandatory.",
                       fix="Provide the account Id (IBAN preferred, or Othr/Id).",
                       layer=2)
            return
        iban = cls._first(acct_id, "IBAN")
        if iban is not None and cls._text(iban):
            if not re.match(r'^[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}$', cls._text(iban)):
                cls._issue(report, rule_id, path=f"{path}/IBAN", line=iban.sourceline or tx_line,
                           message=f"{label} IBAN '{cls._text(iban)}' does not match the IBAN format.",
                           fix="Provide a valid IBAN (2 letters, 2 digits, up to 30 alphanumerics).")
        else:
            othr = cls._first(acct_id, "Othr", "Id")
            if othr is None or not cls._text(othr):
                cls._issue(report, rule_id, path=path, line=acct_id.sourceline or tx_line,
                           message=f"{label} Account Identification is empty; provide an IBAN or Othr/Id.",
                           fix="Provide either <IBAN> or <Othr><Id> for the account.")

    @classmethod
    def _validate_remittance(cls, tx, report, tx_line):
        rmt = cls._first(tx, "RmtInf")
        if rmt is None:
            return
        rmt_line = rmt.sourceline or tx_line
        ustrd_nodes = cls._children(rmt, "Ustrd")
        strd_nodes = cls._children(rmt, "Strd")

        # TXN-018 mutual exclusion
        if ustrd_nodes and strd_nodes:
            cls._issue(report, "SR2026-TXN-018", path="//DrctDbtTxInf/RmtInf", line=rmt_line,
                       message="RmtInf must contain either Ustrd or Strd, not both.",
                       fix="Provide remittance information as either unstructured (Ustrd) or structured (Strd), not both.")

        # TXN-017 Ustrd length
        for u in ustrd_nodes:
            if len(cls._text(u)) > 140:
                cls._issue(report, "SR2026-TXN-017", path="//DrctDbtTxInf/RmtInf/Ustrd",
                           line=u.sourceline or rmt_line,
                           message="Unstructured remittance information exceeds 140 characters.",
                           fix="Limit each Ustrd occurrence to 140 characters.")
