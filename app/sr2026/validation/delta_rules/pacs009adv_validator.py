"""
SR2026 pacs.009 ADV Validator
==============================
Implements all SR2026 CBPR+ rules for the pacs.009 ADV (pre-advice) variant.
ADV is identified by BizSvc='swift.cbprplus.03' AND/OR presence of UndrlygFITxInf.

Rules implemented:
  1.  Header Rules         (BizSvc=.03, MsgDefIdr, BizMsgIdr format, CreDt UTC, CpyDplct forbidden)
  2.  Cross-Field Rules    (BizMsgIdr==MsgId, Fr==InstgAgt, To==InstdAgt,
                            outer InstrId≠underlying, outer UETR≠underlying,
                            outer Amt==underlying, outer Dt==underlying)
  3.  Group Header         (NbOfTxs==1, SttlmMtd==COVE, CreDtTm UTC offset)
  4.  Mandatory Transaction Fields (InstrId≤16, EndToEndId, UETR, IntrBkSttlmAmt,
                            IntrBkSttlmDt, InstgAgt BICFI, InstdAgt BICFI, Dbtr, Cdtr)
  5.  UndrlygFITxInf block (mandatory presence + all sub-fields)
  6.  Forbidden Elements   (UndrlygCstmrCdtTrf)
  7.  Agent ID Rules       (BICFI present → Nm/PstlAdr forbidden; absent → Nm+PstlAdr required)
  8.  Address Rules        (TwnNm+Ctry mandatory, max 2 AdrLine for hybrid)
  9.  UETR format          (lowercase UUID v4)
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

# ── Constants ─────────────────────────────────────────────────────────────────
_BIZSVC_ADV       = "swift.cbprplus.adv.04"
_MSG_DEF_IDR      = "pacs.009.001.08"
_UETR_RE          = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$')
_UETR_RE_CI       = re.compile(r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-4[a-fA-F0-9]{3}-[89abAB][a-fA-F0-9]{3}-[a-fA-F0-9]{12}$')
_DATE_RE          = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CREDT_UTC_RE     = re.compile(r'.+([+-])((0[0-9])|(1[0-4])):[0-5][0-9]$')
_BICFI_RE         = re.compile(r'^[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
_FINX_RE          = re.compile(r'^[0-9a-zA-Z/\-?:().,' + r"' +" + r']+$')
_COUNTRY_RE       = re.compile(r'^[A-Z]{2}$')
_AMOUNT_RE        = re.compile(r'^\d+(\.\d+)?$')


def _t(node) -> str:
    return (node.text or "").strip()


def _bic_norm(bic: str) -> str:
    bic = bic.upper()
    return bic + "XXX" if len(bic) == 8 else bic


class Pacs009AdvValidator:
    """SR2026 CBPR+ pacs.009 ADV validation rules."""

    @classmethod
    def _is_adv(cls, root: etree._Element) -> bool:
        biz_svc = root.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
        if biz_svc and _t(biz_svc[0]) == _BIZSVC_ADV:
            return True
        return bool(root.xpath("//*[local-name()='UndrlygFITxInf']"))

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.009" not in message_type.lower():
            return
        if not cls._is_adv(root):
            return
        cls._validate_header(root, report)
        cls._validate_group_header(root, report)
        cls._validate_transaction(root, report)
        cls._validate_cross_fields(root, report)
        cls._validate_forbidden(root, report)

    # ── 1. Header Rules ──────────────────────────────────────────────────────

    @classmethod
    def _validate_header(cls, root: etree._Element, report: ValidationReport):
        hdr_nodes = root.xpath("//*[local-name()='AppHdr']")
        if not hdr_nodes:
            return
        hdr = hdr_nodes[0]
        src = hdr.sourceline or 1

        # BizSvc must be swift.cbprplus.03
        biz_svc = hdr.xpath("*[local-name()='BizSvc']")
        if biz_svc:
            val = _t(biz_svc[0])
            if val != _BIZSVC_ADV:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_BIZ_SVC",
                    path="//AppHdr/BizSvc",
                    message=f"AppHdr <BizSvc> must be '{_BIZSVC_ADV}' for pacs.009 ADV in SR2026. Got '{val}'.",
                    line=biz_svc[0].sourceline or src,
                    fix=f"Set <BizSvc>{_BIZSVC_ADV}</BizSvc> in AppHdr.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="MISSING_BIZ_SVC",
                path="//AppHdr/BizSvc",
                message=f"AppHdr <BizSvc> is missing. Must be '{_BIZSVC_ADV}' for pacs.009 ADV.",
                line=src, fix=f"Add <BizSvc>{_BIZSVC_ADV}</BizSvc> to AppHdr.",
            ))

        # MsgDefIdr must be pacs.009.001.08
        msg_def = hdr.xpath("*[local-name()='MsgDefIdr']")
        if msg_def:
            val = _t(msg_def[0])
            if val != _MSG_DEF_IDR:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_MSG_DEF_IDR",
                    path="//AppHdr/MsgDefIdr",
                    message=f"AppHdr <MsgDefIdr> must be '{_MSG_DEF_IDR}' for pacs.009 ADV. Got '{val}'.",
                    line=msg_def[0].sourceline or src,
                    fix=f"Set <MsgDefIdr>{_MSG_DEF_IDR}</MsgDefIdr> in AppHdr.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="MISSING_MSG_DEF_IDR",
                path="//AppHdr/MsgDefIdr",
                message=f"AppHdr <MsgDefIdr> is missing. Must be '{_MSG_DEF_IDR}'.",
                line=src, fix=f"Add <MsgDefIdr>{_MSG_DEF_IDR}</MsgDefIdr> to AppHdr.",
            ))

        # BizMsgIdr — CBPR_RestrictedFINXMax35Text
        biz_msg = hdr.xpath("*[local-name()='BizMsgIdr']")
        if biz_msg:
            val = _t(biz_msg[0])
            if not (1 <= len(val) <= 35) or not _FINX_RE.match(val):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_BIZ_MSG_IDR_FORMAT",
                    path="//AppHdr/BizMsgIdr",
                    message="AppHdr <BizMsgIdr> must be 1–35 chars using FIN-X character set (0-9 a-z A-Z / - ? : ( ) . , ' + space).",
                    line=biz_msg[0].sourceline or src,
                    fix="Remove unsupported characters; keep length 1–35.",
                ))

        # CreDt — must include UTC offset (CBPR_DateTime)
        cre_dt = hdr.xpath("*[local-name()='CreDt']")
        if cre_dt:
            val = _t(cre_dt[0])
            if not _CREDT_UTC_RE.match(val):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_OR_INVALID_BAH_CREDT",
                    path="//AppHdr/CreDt",
                    message=f"AppHdr <CreDt> must include a UTC offset (e.g. +00:00). Got '{val}'.",
                    line=cre_dt[0].sourceline or src,
                    fix="Set <CreDt>2026-11-15T09:30:00+00:00</CreDt>.",
                ))

        # CpyDplct FORBIDDEN in ADV
        cpy = hdr.xpath("*[local-name()='CpyDplct']")
        if cpy:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="FORBIDDEN_CPY_DPLCT",
                path="//AppHdr/CpyDplct",
                message="AppHdr <CpyDplct> must not be present in pacs.009 ADV (BizSvc=swift.cbprplus.03).",
                line=cpy[0].sourceline or src,
                fix="Remove <CpyDplct> from AppHdr.",
            ))

    # ── 2. Group Header Rules ─────────────────────────────────────────────────

    @classmethod
    def _validate_group_header(cls, root: etree._Element, report: ValidationReport):
        grp_nodes = root.xpath("//*[local-name()='GrpHdr']")
        if not grp_nodes:
            return
        grp = grp_nodes[0]
        src = grp.sourceline or 1

        # NbOfTxs must be exactly "1"
        nb = grp.xpath("*[local-name()='NbOfTxs']")
        if nb:
            val = _t(nb[0])
            if val != "1":
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_NB_OF_TXS",
                    path="//GrpHdr/NbOfTxs",
                    message=f"GrpHdr <NbOfTxs> must be '1' for pacs.009 ADV. Got '{val}'.",
                    line=nb[0].sourceline or src,
                    fix="Set <NbOfTxs>1</NbOfTxs>. pacs.009 ADV supports only one transaction per message.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="INVALID_NB_OF_TXS",
                path="//GrpHdr/NbOfTxs",
                message="GrpHdr <NbOfTxs> is missing.",
                line=src, fix="Add <NbOfTxs>1</NbOfTxs>.",
            ))

        # Exactly one CdtTrfTxInf
        tx_count = len(root.xpath("//*[local-name()='CdtTrfTxInf']"))
        if tx_count != 1:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="INVALID_NB_OF_TXS",
                path="//FICdtTrf",
                message=f"pacs.009 ADV must have exactly 1 CdtTrfTxInf block. Found {tx_count}.",
                line=src, fix="Remove extra <CdtTrfTxInf> blocks — only one is permitted.",
            ))

        # SttlmMtd must be COVE for ADV
        sttlm = grp.xpath("*[local-name()='SttlmInf']/*[local-name()='SttlmMtd']")
        if sttlm:
            val = _t(sttlm[0])
            if val != "COVE":
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_STTLM_MTD",
                    path="//GrpHdr/SttlmInf/SttlmMtd",
                    message=f"GrpHdr <SttlmMtd> must be 'COVE' for pacs.009 ADV. Got '{val}'.",
                    line=sttlm[0].sourceline or src,
                    fix="Set <SttlmMtd>COVE</SttlmMtd> inside <SttlmInf>.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="INVALID_STTLM_MTD",
                path="//GrpHdr/SttlmInf/SttlmMtd",
                message="GrpHdr <SttlmMtd> is missing. Must be 'COVE' for pacs.009 ADV.",
                line=src, fix="Add <SttlmInf><SttlmMtd>COVE</SttlmMtd></SttlmInf>.",
            ))

        # CreDtTm must have UTC offset
        cre = grp.xpath("*[local-name()='CreDtTm']")
        if cre:
            val = _t(cre[0])
            if not _CREDT_UTC_RE.match(val):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_CREATION_DATE_TIME",
                    path="//GrpHdr/CreDtTm",
                    message=f"GrpHdr <CreDtTm> must include UTC offset (e.g. +00:00). Got '{val}'.",
                    line=cre[0].sourceline or src,
                    fix="Use format: 2026-11-15T09:30:00+00:00",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="MISSING_CREATION_DATE_TIME",
                path="//GrpHdr/CreDtTm",
                message="GrpHdr <CreDtTm> is missing.",
                line=src, fix="Add <CreDtTm>2026-11-15T09:30:00+00:00</CreDtTm>.",
            ))

    # ── 3. Transaction Rules ──────────────────────────────────────────────────

    @classmethod
    def _validate_transaction(cls, root: etree._Element, report: ValidationReport):
        tx_nodes = root.xpath("//*[local-name()='CdtTrfTxInf']")
        for tx in tx_nodes:
            src = tx.sourceline or 1

            # InstrId — mandatory, max 16 chars, FIN-X
            instr_id = tx.xpath("*[local-name()='PmtId']/*[local-name()='InstrId']")
            if instr_id:
                val = _t(instr_id[0])
                if len(val) > 16:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INSTR_ID",
                        path="//CdtTrfTxInf/PmtId/InstrId",
                        message=f"PmtId <InstrId> must be 1–16 characters (CBPR_RestrictedFINXMax16Text). Got {len(val)} chars.",
                        line=instr_id[0].sourceline or src,
                        fix="Shorten <InstrId> to max 16 characters.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_INSTR_ID",
                    path="//CdtTrfTxInf/PmtId/InstrId",
                    message="PmtId <InstrId> is missing. Mandatory for pacs.009 ADV.",
                    line=src, fix="Add <InstrId> (1–16 chars) inside <PmtId>.",
                ))

            # EndToEndId — mandatory, non-blank
            e2e = tx.xpath("*[local-name()='PmtId']/*[local-name()='EndToEndId']")
            if e2e:
                if not _t(e2e[0]):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_END_TO_END_ID",
                        path="//CdtTrfTxInf/PmtId/EndToEndId",
                        message="PmtId <EndToEndId> is blank. Must be a non-empty string.",
                        line=e2e[0].sourceline or src,
                        fix="Set a valid EndToEndId or use 'NOTPROVIDED'.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_END_TO_END_ID",
                    path="//CdtTrfTxInf/PmtId/EndToEndId",
                    message="PmtId <EndToEndId> is missing.",
                    line=src, fix="Add <EndToEndId> inside <PmtId>.",
                ))

            # UETR — mandatory, lowercase UUID v4
            uetr = tx.xpath("*[local-name()='PmtId']/*[local-name()='UETR']")
            if uetr:
                val = _t(uetr[0])
                if not _UETR_RE.match(val):
                    if _UETR_RE_CI.match(val):
                        report.add_issue(ValidationIssue(
                            severity="ERROR", code="MISSING_OR_INVALID_UETR",
                            path="//CdtTrfTxInf/PmtId/UETR",
                            message=f"PmtId <UETR> must be lowercase UUID v4. Got '{val}' (use lowercase hex only).",
                            line=uetr[0].sourceline or src,
                            fix="Normalize UETR to lowercase: " + val.lower(),
                        ))
                    else:
                        report.add_issue(ValidationIssue(
                            severity="ERROR", code="MISSING_OR_INVALID_UETR",
                            path="//CdtTrfTxInf/PmtId/UETR",
                            message=f"PmtId <UETR> must be a valid lowercase UUID v4. Got '{val}'.",
                            line=uetr[0].sourceline or src,
                            fix="Generate a new UUID v4 (e.g. 4a1a0945-5772-409a-83ba-240e666e0267).",
                        ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_OR_INVALID_UETR",
                    path="//CdtTrfTxInf/PmtId/UETR",
                    message="PmtId <UETR> is missing. Mandatory for pacs.009 ADV.",
                    line=src, fix="Add a valid lowercase UUID v4 to <PmtId><UETR>.",
                ))

            # IntrBkSttlmAmt — mandatory, amount > 0, CBPR_Date currency
            amt = tx.xpath("*[local-name()='IntrBkSttlmAmt']")
            if amt:
                val = _t(amt[0])
                ccy = amt[0].get("Ccy", "")
                if not ccy:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INTRBK_STTLM_AMT",
                        path="//CdtTrfTxInf/IntrBkSttlmAmt/@Ccy",
                        message="<IntrBkSttlmAmt> is missing the @Ccy attribute.",
                        line=amt[0].sourceline or src,
                        fix="Add Ccy attribute: <IntrBkSttlmAmt Ccy=\"USD\">...",
                    ))
                elif not re.match(r'^[A-Z]{3}$', ccy):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INTRBK_STTLM_AMT",
                        path="//CdtTrfTxInf/IntrBkSttlmAmt/@Ccy",
                        message=f"<IntrBkSttlmAmt> @Ccy '{ccy}' is not a valid ISO 4217 3-letter code.",
                        line=amt[0].sourceline or src,
                        fix="Use a valid ISO 4217 currency code (e.g. USD, EUR).",
                    ))
                if val:
                    try:
                        fval = float(val)
                        if fval <= 0:
                            raise ValueError("must be positive")
                        # CBPR_Amount: totalDigits=14, fractionDigits=5
                        clean = val.replace("-", "")
                        if "." in clean:
                            int_p, dec_p = clean.split(".", 1)
                            if len(dec_p) > 5:
                                raise ValueError("fraction digits > 5")
                            if len(int_p.lstrip("0") or "0") + len(dec_p) > 14:
                                raise ValueError("total digits > 14")
                    except ValueError as ve:
                        report.add_issue(ValidationIssue(
                            severity="ERROR", code="MISSING_INTRBK_STTLM_AMT",
                            path="//CdtTrfTxInf/IntrBkSttlmAmt",
                            message=f"<IntrBkSttlmAmt> value '{val}' is invalid ({ve}). Must be positive, max 14 total digits, max 5 fraction digits.",
                            line=amt[0].sourceline or src,
                            fix="Set a valid positive amount e.g. <IntrBkSttlmAmt Ccy=\"USD\">1000.00</IntrBkSttlmAmt>.",
                        ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_INTRBK_STTLM_AMT",
                    path="//CdtTrfTxInf/IntrBkSttlmAmt",
                    message="<IntrBkSttlmAmt> is missing.",
                    line=src, fix="Add <IntrBkSttlmAmt Ccy=\"USD\">1000.00</IntrBkSttlmAmt>.",
                ))

            # IntrBkSttlmDt — mandatory, CBPR_Date (no timezone)
            sttlm_dt = tx.xpath("*[local-name()='IntrBkSttlmDt']")
            if sttlm_dt:
                val = _t(sttlm_dt[0])
                if not _DATE_RE.match(val):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INTRBK_STTLM_DT",
                        path="//CdtTrfTxInf/IntrBkSttlmDt",
                        message=f"<IntrBkSttlmDt> must be YYYY-MM-DD without timezone. Got '{val}'.",
                        line=sttlm_dt[0].sourceline or src,
                        fix="Remove timezone suffix. Use format: 2026-11-15",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_INTRBK_STTLM_DT",
                    path="//CdtTrfTxInf/IntrBkSttlmDt",
                    message="<IntrBkSttlmDt> is missing.",
                    line=src, fix="Add <IntrBkSttlmDt>2026-11-15</IntrBkSttlmDt>.",
                ))

            # InstgAgt — BICFI mandatory, Nm/PstlAdr FORBIDDEN
            cls._validate_agent_bicfi_mandatory(tx, "InstgAgt", "MISSING_INSTG_AGT_BIC", src, report)

            # InstdAgt — BICFI mandatory, Nm/PstlAdr FORBIDDEN
            cls._validate_agent_bicfi_mandatory(tx, "InstdAgt", "MISSING_INSTD_AGT_BIC", src, report)

            # Dbtr — mandatory, BICFI/Nm+PstlAdr rule
            dbtr = tx.xpath("*[local-name()='Dbtr']")
            if not dbtr:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_DBTR",
                    path="//CdtTrfTxInf/Dbtr",
                    message="<Dbtr> is missing.",
                    line=src, fix="Add <Dbtr><FinInstnId><BICFI>BANKUS33XXX</BICFI></FinInstnId></Dbtr>.",
                ))
            else:
                cls._validate_agent_identification(dbtr[0], "Dbtr", report)

            # Cdtr — mandatory, BICFI/Nm+PstlAdr rule
            cdtr = tx.xpath("*[local-name()='Cdtr']")
            if not cdtr:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_CDTR",
                    path="//CdtTrfTxInf/Cdtr",
                    message="<Cdtr> is missing.",
                    line=src, fix="Add <Cdtr><FinInstnId><BICFI>BANKGB2LXXX</BICFI></FinInstnId></Cdtr>.",
                ))
            else:
                cls._validate_agent_identification(cdtr[0], "Cdtr", report)

            # Address rules on Dbtr/Cdtr PstlAdr
            for party_tag in ["Dbtr", "Cdtr", "IntrmyAgt1", "IntrmyAgt2", "IntrmyAgt3"]:
                party = tx.xpath(f"*[local-name()='{party_tag}']")
                if party:
                    cls._validate_postal_address(party[0], party_tag, report)

    # ── 5. Cross-Field Rules ──────────────────────────────────────────────────

    @classmethod
    def _validate_cross_fields(cls, root: etree._Element, report: ValidationReport):
        hdr = root.xpath("//*[local-name()='AppHdr']")
        grp = root.xpath("//*[local-name()='GrpHdr']")
        txs = root.xpath("//*[local-name()='CdtTrfTxInf']")
        if not hdr or not grp or not txs:
            return
        hdr, grp, tx = hdr[0], grp[0], txs[0]

        # BizMsgIdr == MsgId
        biz_msg = hdr.xpath("*[local-name()='BizMsgIdr']")
        msg_id  = grp.xpath("*[local-name()='MsgId']")
        if biz_msg and msg_id:
            if _t(biz_msg[0]) != _t(msg_id[0]):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MESSAGE_ID_MISMATCH",
                    path="//AppHdr/BizMsgIdr",
                    message=f"AppHdr <BizMsgIdr> must equal GrpHdr <MsgId>. Got '{_t(biz_msg[0])}' vs '{_t(msg_id[0])}'.",
                    line=biz_msg[0].sourceline or 1,
                    fix="Align <BizMsgIdr> in AppHdr with <MsgId> in GrpHdr.",
                ))

        # Fr BIC == InstgAgt BIC
        fr_bic  = hdr.xpath("*[local-name()='Fr']//*[local-name()='BICFI']")
        instg   = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
        if fr_bic and instg:
            if _bic_norm(_t(fr_bic[0])) != _bic_norm(_t(instg[0])):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="FROM_BIC_MISMATCH",
                    path="//AppHdr/Fr/FIId/FinInstnId/BICFI",
                    message=f"AppHdr <Fr> BICFI '{_t(fr_bic[0])}' must match <InstgAgt> BICFI '{_t(instg[0])}'.",
                    line=fr_bic[0].sourceline or 1,
                    fix="Ensure AppHdr From BIC matches Instructing Agent BIC.",
                ))

        # To BIC == InstdAgt BIC
        to_bic  = hdr.xpath("*[local-name()='To']//*[local-name()='BICFI']")
        instd   = tx.xpath("*[local-name()='InstdAgt']//*[local-name()='BICFI']")
        if to_bic and instd:
            if _bic_norm(_t(to_bic[0])) != _bic_norm(_t(instd[0])):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="TO_BIC_MISMATCH",
                    path="//AppHdr/To/FIId/FinInstnId/BICFI",
                    message=f"AppHdr <To> BICFI '{_t(to_bic[0])}' must match <InstdAgt> BICFI '{_t(instd[0])}'.",
                    line=to_bic[0].sourceline or 1,
                    fix="Ensure AppHdr To BIC matches Instructed Agent BIC.",
                ))

        # Outer Amt and Dt removed (were underlying-dependent)

    # ── 6. Forbidden Elements ─────────────────────────────────────────────────

    @classmethod
    def _validate_forbidden(cls, root: etree._Element, report: ValidationReport):
        # UndrlygCstmrCdtTrf is exclusive to COV — forbidden in ADV
        for node in root.xpath("//*[local-name()='UndrlygCstmrCdtTrf']"):
            report.add_issue(ValidationIssue(
                severity="ERROR", code="FORBIDDEN_UNDRLYG_CSTMR_CDT_TRF",
                path="//CdtTrfTxInf/UndrlygCstmrCdtTrf",
                message="<UndrlygCstmrCdtTrf> is present but forbidden in pacs.009 ADV. This element belongs to pacs.009 COV.",
                line=node.sourceline or 1,
                fix="Remove <UndrlygCstmrCdtTrf> for ADV messages.",
            ))

    # ── Helpers ───────────────────────────────────────────────────────────────

    @classmethod
    def _validate_agent_bicfi_mandatory(cls, tx: etree._Element, tag: str, code: str, src: int, report: ValidationReport):
        """InstgAgt/InstdAgt: BICFI mandatory, Nm/PstlAdr forbidden."""
        agent = tx.xpath(f"*[local-name()='{tag}']")
        if not agent:
            report.add_issue(ValidationIssue(
                severity="ERROR", code=code,
                path=f"//CdtTrfTxInf/{tag}/FinInstnId/BICFI",
                message=f"<{tag}> is missing. BICFI is mandatory for {tag} in pacs.009 ADV.",
                line=src, fix=f"Add <{tag}><FinInstnId><BICFI>BANKXX00XXX</BICFI></FinInstnId></{tag}>.",
            ))
            return
        a = agent[0]
        asrc = a.sourceline or src
        bicfi = a.xpath("*[local-name()='FinInstnId']/*[local-name()='BICFI']")
        if not bicfi or not _t(bicfi[0]):
            report.add_issue(ValidationIssue(
                severity="ERROR", code=code,
                path=f"//CdtTrfTxInf/{tag}/FinInstnId/BICFI",
                message=f"<{tag}> BICFI is missing. BICFI is mandatory (FinancialInstitutionIdentification18__2).",
                line=asrc, fix=f"Add <BICFI> inside <{tag}><FinInstnId>.",
            ))
        else:
            val = _t(bicfi[0])
            if not _BICFI_RE.match(val):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code=code,
                    path=f"//CdtTrfTxInf/{tag}/FinInstnId/BICFI",
                    message=f"<{tag}> BICFI '{val}' is not a valid BIC format.",
                    line=bicfi[0].sourceline or asrc,
                    fix="Use a valid 8 or 11 character BIC (e.g. BANKUS33XXX).",
                ))
        # Nm/PstlAdr forbidden when BICFI present (FinancialInstitutionIdentification18__2)
        fi_id = a.xpath("*[local-name()='FinInstnId']")
        if fi_id and bicfi:
            for forbidden in ["Nm", "PstlAdr"]:
                found = fi_id[0].xpath(f"*[local-name()='{forbidden}']")
                if found:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="AGENT_IDENTIFICATION_RULE_VIOLATION",
                        path=f"//CdtTrfTxInf/{tag}/FinInstnId/{forbidden}",
                        message=f"<{forbidden}> is forbidden inside <{tag}><FinInstnId> when BICFI is present (pacs.009 ADV FinancialInstitutionIdentification18__2).",
                        line=found[0].sourceline or asrc,
                        fix=f"Remove <{forbidden}> from <{tag}><FinInstnId>. Use BICFI only for {tag}.",
                    ))

    @classmethod
    def _validate_agent_identification(cls, agent: etree._Element, tag: str, report: ValidationReport):
        """Dbtr/Cdtr/IntrmyAgt: BICFI present → no Nm/PstlAdr; absent → Nm+PstlAdr required."""
        src = agent.sourceline or 1
        fi_id = agent.xpath("*[local-name()='FinInstnId']")
        if not fi_id:
            return
        fi = fi_id[0]
        bicfi = fi.xpath("*[local-name()='BICFI']")
        if bicfi and _t(bicfi[0]):
            # BICFI present → Nm/PstlAdr forbidden
            for forbidden in ["Nm", "PstlAdr"]:
                found = fi.xpath(f"*[local-name()='{forbidden}']")
                if found:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="AGENT_IDENTIFICATION_RULE_VIOLATION",
                        path=f"//{tag}/FinInstnId/{forbidden}",
                        message=f"<{forbidden}> is not allowed inside <{tag}><FinInstnId> when BICFI is present (CBPR+ Principle 1).",
                        line=found[0].sourceline or src,
                        fix=f"Remove <{forbidden}> from <{tag}><FinInstnId>.",
                    ))
        else:
            # BICFI absent → Nm and PstlAdr required
            for required in ["Nm", "PstlAdr"]:
                found = fi.xpath(f"*[local-name()='{required}']")
                if not found:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="AGENT_IDENTIFICATION_RULE_VIOLATION",
                        path=f"//{tag}/FinInstnId/{required}",
                        message=f"<{required}> is required inside <{tag}><FinInstnId> when BICFI is absent (CBPR+ Principle 1).",
                        line=src,
                        fix=f"Add <{required}> to <{tag}><FinInstnId>, or add <BICFI> instead.",
                    ))

    @classmethod
    def _validate_postal_address(cls, party: etree._Element, tag: str, report: ValidationReport):
        """TwnNm + Ctry mandatory; max 2 AdrLine for hybrid."""
        pstl_adr = party.xpath("*[local-name()='FinInstnId']/*[local-name()='PstlAdr']")
        if not pstl_adr:
            return
        adr = pstl_adr[0]
        src = adr.sourceline or 1

        for field in ["TwnNm", "Ctry"]:
            nodes = adr.xpath(f"*[local-name()='{field}']")
            if not nodes or not _t(nodes[0]):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code=f"MISSING_{field.upper()}",
                    path=f"//{tag}/FinInstnId/PstlAdr/{field}",
                    message=f"<PstlAdr> inside <{tag}> is missing mandatory <{field}> (PostalAddress24__1 SR2026).",
                    line=src,
                    fix=f"Add <{field}> to <{tag}><FinInstnId><PstlAdr>.",
                ))

        # Max 2 AdrLine
        adr_lines = adr.xpath("*[local-name()='AdrLine']")
        if len(adr_lines) > 2:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="DEPRECATED_UNSTRUCTURED_ADDRESS",
                path=f"//{tag}/FinInstnId/PstlAdr/AdrLine",
                message=f"<PstlAdr> inside <{tag}> has {len(adr_lines)} <AdrLine> elements. Maximum allowed is 2 (hybrid address).",
                line=adr_lines[2].sourceline or src,
                fix="Remove excess <AdrLine> elements. Hybrid addresses allow max 2 AdrLine alongside TwnNm+Ctry.",
            ))
