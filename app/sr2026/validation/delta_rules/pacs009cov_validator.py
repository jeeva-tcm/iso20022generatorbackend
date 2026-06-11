"""
SR2026 pacs.009 COV Validator
==============================
Implements all SR2026 CBPR+ rules for the pacs.009 COV (cover payment) variant.
COV is identified by BizSvc='swift.cbprplus.cov.04' AND/OR presence of UndrlygCstmrCdtTrf.

Rules implemented:
  1.  Header Rules         (BizSvc=cov.04, MsgDefIdr, BizMsgIdr format, CreDt UTC, CpyDplct forbidden)
  2.  Group Header         (NbOfTxs==1, SttlmMtd==INDA or INGA, CreDtTm UTC offset)
  3.  Transaction          (InstrId<=16, EndToEndId, UETR lowercase UUID v4, IntrBkSttlmAmt,
                            IntrBkSttlmDt, InstgAgt BICFI mandatory, InstdAgt BICFI mandatory)
  4.  UndrlygCstmrCdtTrf   (mandatory; FATF Rec 16: Dbtr.Nm + Cdtr.Nm mandatory)
  5.  Forbidden Elements   (UndrlygFITxInf forbidden)
  6.  Cross-Field Rules    (BizMsgIdr==MsgId, Fr==InstgAgt, To==InstdAgt)
  7.  Agent ID Rules       (BICFI present -> Nm/PstlAdr forbidden)
  8.  Address Rules        (TwnNm+Ctry mandatory, max 2 AdrLine)
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

# ── Constants ─────────────────────────────────────────────────────────────────
_BIZSVC_COV   = "swift.cbprplus.cov.04"
_MSG_DEF_IDR  = "pacs.009.001.08"
_STTLM_COV    = {"INDA", "INGA"}
_UETR_RE      = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$')
_UETR_RE_CI   = re.compile(r'^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-4[a-fA-F0-9]{3}-[89abAB][a-fA-F0-9]{3}-[a-fA-F0-9]{12}$')
_DATE_RE      = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CREDT_UTC_RE = re.compile(r'.+([+-])((0[0-9])|(1[0-4])):[0-5][0-9]$')
_BICFI_RE     = re.compile(r'^[A-Z0-9]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$')
_FINX_RE      = re.compile(r'^[0-9a-zA-Z/\-?:().,' + r"' +" + r']+$')


def _t(node) -> str:
    return (node.text or "").strip()


def _bic_norm(bic: str) -> str:
    bic = bic.upper()
    return bic + "XXX" if len(bic) == 8 else bic


class Pacs009CovValidator:
    """SR2026 CBPR+ pacs.009 COV validation rules."""

    @classmethod
    def _is_cov(cls, root: etree._Element, message_type: str = "") -> bool:
        mt = message_type.lower()
        if "_cov" in mt or "pacs.009.cov" in mt or "pacs.009_cov" in mt:
            return True
        biz_svc = root.xpath("//*[local-name()='AppHdr']/*[local-name()='BizSvc']")
        if biz_svc and _t(biz_svc[0]) == _BIZSVC_COV:
            return True
        return bool(root.xpath("//*[local-name()='UndrlygCstmrCdtTrf']"))

    @classmethod
    def validate(cls, root: etree._Element, report: ValidationReport, message_type: str):
        if "pacs.009" not in message_type.lower():
            return
        if not cls._is_cov(root, message_type):
            return
        cls._validate_header(root, report)
        cls._validate_group_header(root, report)
        cls._validate_transaction(root, report)
        cls._validate_underlying(root, report)
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

        # BizSvc must be swift.cbprplus.cov.04
        biz_svc = hdr.xpath("*[local-name()='BizSvc']")
        if biz_svc:
            val = _t(biz_svc[0])
            if val != _BIZSVC_COV:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_BIZ_SVC",
                    path="//AppHdr/BizSvc",
                    message=f"AppHdr <BizSvc> must be '{_BIZSVC_COV}' for pacs.009 COV in SR2026. Got '{val}'.",
                    line=biz_svc[0].sourceline or src,
                    fix=f"Set <BizSvc>{_BIZSVC_COV}</BizSvc> in AppHdr.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="MISSING_BIZ_SVC", layer=2,
                path="//AppHdr/BizSvc",
                message=f"AppHdr <BizSvc> is missing. Must be '{_BIZSVC_COV}' for pacs.009 COV.",
                line=src, fix=f"Add <BizSvc>{_BIZSVC_COV}</BizSvc> to AppHdr.",
            ))

        # MsgDefIdr must be pacs.009.001.08
        msg_def = hdr.xpath("*[local-name()='MsgDefIdr']")
        if msg_def:
            val = _t(msg_def[0])
            if val != _MSG_DEF_IDR:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_MSG_DEF_IDR",
                    path="//AppHdr/MsgDefIdr",
                    message=f"AppHdr <MsgDefIdr> must be '{_MSG_DEF_IDR}' for pacs.009 COV. Got '{val}'.",
                    line=msg_def[0].sourceline or src,
                    fix=f"Set <MsgDefIdr>{_MSG_DEF_IDR}</MsgDefIdr> in AppHdr.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="MISSING_MSG_DEF_IDR", layer=2,
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
                    message="AppHdr <BizMsgIdr> must be 1-35 chars using FIN-X character set.",
                    line=biz_msg[0].sourceline or src,
                    fix="Remove unsupported characters; keep length 1-35.",
                ))

        # CreDt — must include UTC offset
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

        # CpyDplct FORBIDDEN in COV
        cpy = hdr.xpath("*[local-name()='CpyDplct']")
        if cpy:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="FORBIDDEN_CPY_DPLCT",
                path="//AppHdr/CpyDplct",
                message="AppHdr <CpyDplct> must not be present in pacs.009 COV.",
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
                    message=f"GrpHdr <NbOfTxs> must be '1' for pacs.009 COV. Got '{val}'.",
                    line=nb[0].sourceline or src,
                    fix="Set <NbOfTxs>1</NbOfTxs>. pacs.009 COV supports only one transaction per message.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="INVALID_NB_OF_TXS", layer=2,
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
                message=f"pacs.009 COV must have exactly 1 CdtTrfTxInf block. Found {tx_count}.",
                line=src, fix="Remove extra <CdtTrfTxInf> blocks — only one is permitted.",
            ))

        # SttlmMtd must be INDA or INGA for COV (COVE is forbidden)
        sttlm = grp.xpath("*[local-name()='SttlmInf']/*[local-name()='SttlmMtd']")
        if sttlm:
            val = _t(sttlm[0])
            if val not in _STTLM_COV:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="INVALID_STTLM_MTD",
                    path="//GrpHdr/SttlmInf/SttlmMtd",
                    message=f"GrpHdr <SttlmMtd> must be 'INDA' or 'INGA' for pacs.009 COV. Got '{val}'. COVE is not permitted in COV messages.",
                    line=sttlm[0].sourceline or src,
                    fix="Set <SttlmMtd>INDA</SttlmMtd> or <SttlmMtd>INGA</SttlmMtd> inside <SttlmInf>.",
                ))
        else:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="INVALID_STTLM_MTD", layer=2,
                path="//GrpHdr/SttlmInf/SttlmMtd",
                message="GrpHdr <SttlmMtd> is missing. Must be 'INDA' or 'INGA' for pacs.009 COV.",
                line=src, fix="Add <SttlmInf><SttlmMtd>INDA</SttlmMtd></SttlmInf>.",
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
                severity="ERROR", code="MISSING_CREATION_DATE_TIME", layer=2,
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

            # InstrId — mandatory, max 16 chars
            instr_id = tx.xpath("*[local-name()='PmtId']/*[local-name()='InstrId']")
            if instr_id:
                val = _t(instr_id[0])
                if len(val) > 16:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INSTR_ID",
                        path="//CdtTrfTxInf/PmtId/InstrId",
                        message=f"PmtId <InstrId> must be 1-16 characters. Got {len(val)} chars.",
                        line=instr_id[0].sourceline or src,
                        fix="Shorten <InstrId> to max 16 characters.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_INSTR_ID", layer=2,
                    path="//CdtTrfTxInf/PmtId/InstrId",
                    message="PmtId <InstrId> is missing. Mandatory for pacs.009 COV.",
                    line=src, fix="Add <InstrId> (1-16 chars) inside <PmtId>.",
                ))

            # EndToEndId — mandatory
            e2e = tx.xpath("*[local-name()='PmtId']/*[local-name()='EndToEndId']")
            if e2e:
                if not _t(e2e[0]):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_END_TO_END_ID",
                        path="//CdtTrfTxInf/PmtId/EndToEndId",
                        message="PmtId <EndToEndId> is blank.",
                        line=e2e[0].sourceline or src,
                        fix="Set a valid EndToEndId or use 'NOTPROVIDED'.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_END_TO_END_ID", layer=2,
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
                    severity="ERROR", code="MISSING_OR_INVALID_UETR", layer=2,
                    path="//CdtTrfTxInf/PmtId/UETR",
                    message="PmtId <UETR> is missing. Mandatory for pacs.009 COV.",
                    line=src, fix="Add a valid lowercase UUID v4 to <PmtId><UETR>.",
                ))

            # IntrBkSttlmAmt — mandatory, amount > 0
            amt = tx.xpath("*[local-name()='IntrBkSttlmAmt']")
            if amt:
                val = _t(amt[0])
                ccy = amt[0].get("Ccy", "")
                if not ccy:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="MISSING_INTRBK_STTLM_AMT", layer=2,
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
                            message=f"<IntrBkSttlmAmt> value '{val}' is invalid ({ve}).",
                            line=amt[0].sourceline or src,
                            fix="Set a valid positive amount e.g. <IntrBkSttlmAmt Ccy=\"USD\">1000.00</IntrBkSttlmAmt>.",
                        ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_INTRBK_STTLM_AMT", layer=2,
                    path="//CdtTrfTxInf/IntrBkSttlmAmt",
                    message="<IntrBkSttlmAmt> is missing.",
                    line=src, fix="Add <IntrBkSttlmAmt Ccy=\"USD\">1000.00</IntrBkSttlmAmt>.",
                ))

            # IntrBkSttlmDt — mandatory, CBPR_Date
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
                    severity="ERROR", code="MISSING_INTRBK_STTLM_DT", layer=2,
                    path="//CdtTrfTxInf/IntrBkSttlmDt",
                    message="<IntrBkSttlmDt> is missing.",
                    line=src, fix="Add <IntrBkSttlmDt>2026-11-15</IntrBkSttlmDt>.",
                ))

            # InstgAgt — BICFI mandatory, Nm/PstlAdr FORBIDDEN
            cls._validate_agent_bicfi_mandatory(tx, "InstgAgt", "MISSING_INSTG_AGT_BIC", src, report)

            # InstdAgt — BICFI mandatory, Nm/PstlAdr FORBIDDEN
            cls._validate_agent_bicfi_mandatory(tx, "InstdAgt", "MISSING_INSTD_AGT_BIC", src, report)

            # Address rules on any agents with PstlAdr
            for party_tag in ["Dbtr", "Cdtr", "IntrmyAgt1", "IntrmyAgt2", "IntrmyAgt3"]:
                party = tx.xpath(f"*[local-name()='{party_tag}']")
                if party:
                    cls._validate_postal_address(party[0], party_tag, report)

    # ── 4. UndrlygCstmrCdtTrf Rules ──────────────────────────────────────────

    @classmethod
    def _validate_underlying(cls, root: etree._Element, report: ValidationReport):
        tx_nodes = root.xpath("//*[local-name()='CdtTrfTxInf']")
        for tx in tx_nodes:
            src = tx.sourceline or 1
            undrlyg = tx.xpath("*[local-name()='UndrlygCstmrCdtTrf']")

            if not undrlyg:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="MISSING_UNDRLYG_CSTMR_CDT_TRF", layer=2,
                    path="//CdtTrfTxInf/UndrlygCstmrCdtTrf",
                    message="<UndrlygCstmrCdtTrf> is missing. This block is mandatory for pacs.009 COV.",
                    line=src,
                    fix="Add <UndrlygCstmrCdtTrf> block containing Dbtr, DbtrAgt, CdtrAgt, Cdtr, and InstdAmt.",
                ))
                continue

            u = undrlyg[0]
            usrc = u.sourceline or src

            # FATF Rec 16: Debtor Name mandatory in underlying
            dbtr = u.xpath("*[local-name()='Dbtr']")
            if dbtr:
                nm = dbtr[0].xpath("*[local-name()='Nm']")
                if not nm or not _t(nm[0]):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="FATF_REC16_MISSING_DBTR_NM",
                        path="//CdtTrfTxInf/UndrlygCstmrCdtTrf/Dbtr/Nm",
                        message="<UndrlygCstmrCdtTrf><Dbtr><Nm> is mandatory (FATF Recommendation 16).",
                        line=dbtr[0].sourceline or usrc,
                        fix="Add <Nm> with the debtor's full name inside <UndrlygCstmrCdtTrf><Dbtr>.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="FATF_REC16_MISSING_DBTR_NM", layer=2,
                    path="//CdtTrfTxInf/UndrlygCstmrCdtTrf/Dbtr",
                    message="<UndrlygCstmrCdtTrf><Dbtr> is missing. Debtor information is mandatory for pacs.009 COV (FATF Rec 16).",
                    line=usrc,
                    fix="Add <Dbtr><Nm>...</Nm></Dbtr> inside <UndrlygCstmrCdtTrf>.",
                ))

            # FATF Rec 16: Creditor Name mandatory in underlying
            cdtr = u.xpath("*[local-name()='Cdtr']")
            if cdtr:
                nm = cdtr[0].xpath("*[local-name()='Nm']")
                if not nm or not _t(nm[0]):
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="FATF_REC16_MISSING_CDTR_NM",
                        path="//CdtTrfTxInf/UndrlygCstmrCdtTrf/Cdtr/Nm",
                        message="<UndrlygCstmrCdtTrf><Cdtr><Nm> is mandatory (FATF Recommendation 16).",
                        line=cdtr[0].sourceline or usrc,
                        fix="Add <Nm> with the creditor's full name inside <UndrlygCstmrCdtTrf><Cdtr>.",
                    ))
            else:
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="FATF_REC16_MISSING_CDTR_NM", layer=2,
                    path="//CdtTrfTxInf/UndrlygCstmrCdtTrf/Cdtr",
                    message="<UndrlygCstmrCdtTrf><Cdtr> is missing. Creditor information is mandatory for pacs.009 COV (FATF Rec 16).",
                    line=usrc,
                    fix="Add <Cdtr><Nm>...</Nm></Cdtr> inside <UndrlygCstmrCdtTrf>.",
                ))

            # InstdAmt presence/format is enforced by the COV XSD (L2) — no duplicate L3 check here

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
        fr_bic = hdr.xpath("*[local-name()='Fr']//*[local-name()='BICFI']")
        instg  = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
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
        to_bic = hdr.xpath("*[local-name()='To']//*[local-name()='BICFI']")
        instd  = tx.xpath("*[local-name()='InstdAgt']//*[local-name()='BICFI']")
        if to_bic and instd:
            if _bic_norm(_t(to_bic[0])) != _bic_norm(_t(instd[0])):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code="TO_BIC_MISMATCH",
                    path="//AppHdr/To/FIId/FinInstnId/BICFI",
                    message=f"AppHdr <To> BICFI '{_t(to_bic[0])}' must match <InstdAgt> BICFI '{_t(instd[0])}'.",
                    line=to_bic[0].sourceline or 1,
                    fix="Ensure AppHdr To BIC matches Instructed Agent BIC.",
                ))

    # ── 6. Forbidden Elements ─────────────────────────────────────────────────

    @classmethod
    def _validate_forbidden(cls, root: etree._Element, report: ValidationReport):
        # UndrlygFITxInf is exclusive to ADV — forbidden in COV
        for node in root.xpath("//*[local-name()='UndrlygFITxInf']"):
            report.add_issue(ValidationIssue(
                severity="ERROR", code="FORBIDDEN_UNDRLYG_FI_TX_INF",
                path="//CdtTrfTxInf/UndrlygFITxInf",
                message="<UndrlygFITxInf> is present but forbidden in pacs.009 COV. This element belongs to pacs.009 ADV.",
                line=node.sourceline or 1,
                fix="Remove <UndrlygFITxInf> for COV messages. Use <UndrlygCstmrCdtTrf> instead.",
            ))

    # ── Helpers ───────────────────────────────────────────────────────────────

    @classmethod
    def _validate_agent_bicfi_mandatory(cls, tx: etree._Element, tag: str, code: str, src: int, report: ValidationReport):
        """InstgAgt/InstdAgt: BICFI mandatory, Nm/PstlAdr forbidden."""
        agent = tx.xpath(f"*[local-name()='{tag}']")
        if not agent:
            report.add_issue(ValidationIssue(
                severity="ERROR", code=code, layer=2,
                path=f"//CdtTrfTxInf/{tag}/FinInstnId/BICFI",
                message=f"<{tag}> is missing. BICFI is mandatory for {tag} in pacs.009 COV.",
                line=src, fix=f"Add <{tag}><FinInstnId><BICFI>BANKXX00XXX</BICFI></FinInstnId></{tag}>.",
            ))
            return
        a = agent[0]
        asrc = a.sourceline or src
        bicfi = a.xpath("*[local-name()='FinInstnId']/*[local-name()='BICFI']")
        if not bicfi or not _t(bicfi[0]):
            report.add_issue(ValidationIssue(
                severity="ERROR", code=code, layer=2,
                path=f"//CdtTrfTxInf/{tag}/FinInstnId/BICFI",
                message=f"<{tag}> BICFI is missing. BICFI is mandatory for {tag} in pacs.009 COV.",
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
        # Nm/PstlAdr forbidden when BICFI present
        fi_id = a.xpath("*[local-name()='FinInstnId']")
        if fi_id and bicfi:
            for forbidden in ["Nm", "PstlAdr"]:
                found = fi_id[0].xpath(f"*[local-name()='{forbidden}']")
                if found:
                    report.add_issue(ValidationIssue(
                        severity="ERROR", code="AGENT_IDENTIFICATION_RULE_VIOLATION",
                        path=f"//CdtTrfTxInf/{tag}/FinInstnId/{forbidden}",
                        message=f"<{forbidden}> is forbidden inside <{tag}><FinInstnId> when BICFI is present.",
                        line=found[0].sourceline or asrc,
                        fix=f"Remove <{forbidden}> from <{tag}><FinInstnId>. Use BICFI only for {tag}.",
                    ))

    @classmethod
    def _validate_postal_address(cls, party: etree._Element, tag: str, report: ValidationReport):
        """TwnNm + Ctry mandatory; max 2 AdrLine."""
        pstl_adr = party.xpath("*[local-name()='FinInstnId']/*[local-name()='PstlAdr']")
        if not pstl_adr:
            return
        adr = pstl_adr[0]
        src = adr.sourceline or 1

        for field in ["TwnNm", "Ctry"]:
            nodes = adr.xpath(f"*[local-name()='{field}']")
            if not nodes or not _t(nodes[0]):
                report.add_issue(ValidationIssue(
                    severity="ERROR", code=f"MISSING_{field.upper()}", layer=2,
                    path=f"//{tag}/FinInstnId/PstlAdr/{field}",
                    message=f"<PstlAdr> inside <{tag}> is missing mandatory <{field}> (PostalAddress24__1 SR2026).",
                    line=src,
                    fix=f"Add <{field}> to <{tag}><FinInstnId><PstlAdr>.",
                ))

        adr_lines = adr.xpath("*[local-name()='AdrLine']")
        if len(adr_lines) > 2:
            report.add_issue(ValidationIssue(
                severity="ERROR", code="DEPRECATED_UNSTRUCTURED_ADDRESS",
                path=f"//{tag}/FinInstnId/PstlAdr/AdrLine",
                message=f"<PstlAdr> inside <{tag}> has {len(adr_lines)} <AdrLine> elements. Maximum allowed is 2.",
                line=adr_lines[2].sourceline or src,
                fix="Remove excess <AdrLine> elements. Hybrid addresses allow max 2 AdrLine alongside TwnNm+Ctry.",
            ))
