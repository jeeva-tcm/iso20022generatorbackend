"""
SR2026 New Mandatory Fields Validator
======================================
Enforces every mandatory-field and cross-field rule defined in
app/sr2026/rules/messages/pacs.008/base.json.

Rule categories:
  1. Mandatory Group Header fields (MsgId, CreDtTm, NbOfTxs, SttlmInf, SttlmMtd)
  2. NbOfTxs vs actual transaction count cross-check
  3. AppHdr presence + BizSvc / MsgDefIdr / BizMsgIdr / CreDt / Fr / To
  4. Cross-field: BizMsgIdr == MsgId, From BIC == InstgAgt BIC, To BIC == InstdAgt BIC
  5. Mandatory Transaction fields (pacs.008 only — all fields in transactionBlock)
  6. Service Level (GPI disallowed codes)
  7. Date fields (no timezone offset)
  8. Proxy rules (Tp mandatory when Prxy present)
  9. Structured Remittance max length
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport
from app.sr2026.rules import rule_registry


def _bic_norm(bic: str) -> str:
    bic = bic.upper()
    return bic + "XXX" if len(bic) == 8 else bic


class NewMandatoryFieldsValidator:

    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        hdr_rules  = rule_registry.get_header_rules()
        xf_rules   = rule_registry.get_cross_field_rules()
        mf_grp     = rule_registry.get_mandatory_group_header_fields()
        mf_tx      = rule_registry.get_mandatory_transaction_fields()
        svc_rules  = rule_registry.get_service_level_rules()
        date_rules = rule_registry.get_date_rules()
        prxy_rules = rule_registry.get_proxy_rules()

        def _text(node) -> str:
            return (node.text or "").strip()

        def _msg(cfg: dict, default: str = "") -> str:
            return (cfg.get("message") or cfg.get("description")
                    or cfg.get("errorMessage") or default)

        # ── Detect message type from XML body ─────────────────────────
        is_pacs008 = bool(root_element.xpath("//*[local-name()='FIToFICstmrCdtTrf']"))
        is_pacs003 = bool(root_element.xpath("//*[local-name()='FIToFICstmrDrctDbt']"))
        is_pacs009 = bool(root_element.xpath("//*[local-name()='FICdtTrf']"))
        is_pacs004 = bool(root_element.xpath("//*[local-name()='PmtRtr']"))

        # NbOfTxs / SttlmInf / SttlmMtd are mandatory only in the interbank
        # *payment* messages whose GroupHeader actually carries them
        # (pacs.008/003/009/004). camt.05x reports and pacs.002 status reports
        # have a GroupHeader but no NbOfTxs/SttlmInf — applying the pacs.008
        # ruleset to them produces false positives, so gate those fields.
        has_grp_settlement = is_pacs008 or is_pacs003 or is_pacs009 or is_pacs004
        _PAYMENT_ONLY_GRP_FIELDS = {"NbOfTxs", "SttlmInf", "SttlmMtd"}

        # ─────────────────────────────────────────────────────────────
        # 1. MANDATORY GROUP HEADER FIELDS
        # ─────────────────────────────────────────────────────────────
        grp_headers = root_element.xpath("//*[local-name()='GrpHdr']")
        for grp in grp_headers:
            src = grp.sourceline or 1
            for field_name, cfg in mf_grp.items():
                # Skip payment-only GrpHdr fields for non-payment messages
                # (camt.05x, pacs.002) that legitimately don't carry them.
                if field_name in _PAYMENT_ONLY_GRP_FIELDS and not has_grp_settlement:
                    continue
                sev  = cfg.get("severity", "ERROR")
                code = cfg.get("code", f"MISSING_{field_name.upper()}")
                msg  = _msg(cfg, f"Group Header is missing mandatory <{field_name}>.")
                fix  = cfg.get("fix", f"Add <{field_name}> inside <GrpHdr>.")

                # SttlmMtd is nested inside SttlmInf — handle separately
                if field_name == "SttlmMtd":
                    sttlm_inf_nodes = grp.xpath("*[local-name()='SttlmInf']")
                    if not sttlm_inf_nodes:
                        continue  # SttlmInf absence already reported above
                    sttlm_mtd_nodes = sttlm_inf_nodes[0].xpath("*[local-name()='SttlmMtd']")
                    if not sttlm_mtd_nodes:
                        report.add_issue(ValidationIssue(
                            severity=sev, code=code, layer=2,
                            path="//GrpHdr/SttlmInf/SttlmMtd",
                            message=msg,
                            line=sttlm_inf_nodes[0].sourceline or src,
                            fix=fix,
                        ))
                    else:
                        val = _text(sttlm_mtd_nodes[0])
                        allowed = cfg.get("allowedValues", [])
                        if allowed and val not in allowed:
                            report.add_issue(ValidationIssue(
                                severity=sev, code=code,
                                path="//GrpHdr/SttlmInf/SttlmMtd",
                                message=f"Settlement Method '{val}' is invalid. Allowed: {', '.join(allowed)}.",
                                line=sttlm_mtd_nodes[0].sourceline or src,
                                fix=fix,
                            ))
                    continue

                if not grp.xpath(f"*[local-name()='{field_name}']"):
                    report.add_issue(ValidationIssue(
                        severity=sev, code=code, layer=2,
                        path=f"//GrpHdr/{field_name}",
                        message=msg, line=src, fix=fix,
                    ))

        # ─────────────────────────────────────────────────────────────
        # 2. NbOfTxs vs ACTUAL TRANSACTION COUNT
        # ─────────────────────────────────────────────────────────────
        all_tx_blocks = root_element.xpath(
            "//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']"
        )
        xf_nb = xf_rules.get("nbOfTxsMatchesCount", {})
        for grp in grp_headers:
            nb_nodes = grp.xpath("*[local-name()='NbOfTxs']")
            if nb_nodes:
                try:
                    declared = int(_text(nb_nodes[0]))
                    actual   = len(all_tx_blocks)
                    if declared != actual:
                        report.add_issue(ValidationIssue(
                            severity=xf_nb.get("severity", "ERROR"),
                            code=xf_nb.get("code", "NB_OF_TXS_COUNT_MISMATCH"),
                            path="//GrpHdr/NbOfTxs",
                            message=_msg(xf_nb,
                                f"Number of Transactions declared ({declared}) does not "
                                f"match actual transaction count ({actual})."),
                            line=nb_nodes[0].sourceline or grp.sourceline or 1,
                            fix=xf_nb.get("fix", f"Change <NbOfTxs> to {actual}."),
                        ))
                except (ValueError, TypeError):
                    pass  # Non-numeric value is caught by XSD

        # ─────────────────────────────────────────────────────────────
        # 3. APPHDR RULES
        # ─────────────────────────────────────────────────────────────
        app_hdr_nodes = root_element.xpath("//*[local-name()='AppHdr']")
        biz_msg_idr_val = None

        # Refine pacs type from MsgDefIdr if present
        if app_hdr_nodes:
            mdi = app_hdr_nodes[0].xpath("*[local-name()='MsgDefIdr']")
            if mdi:
                mdi_val = _text(mdi[0])
                if "pacs.008" in mdi_val:
                    is_pacs008 = True
                if "pacs.003" in mdi_val:
                    is_pacs003 = True

        if not app_hdr_nodes:
            report.add_issue(ValidationIssue(
                severity="ERROR",
                code="MISSING_APP_HDR",
                layer=2,
                path="/AppHdr",
                message=(
                    "The application header 'BusinessApplicationHeaderV02' "
                    "(urn:iso:std:iso:20022:tech:xsd:head.001.001.02) must be present."
                ),
                line=1,
                fix=(
                    "Add an <AppHdr> block conforming to BusinessApplicationHeaderV02 "
                    "(head.001.001.02) before the <Document> element."
                ),
            ))

        if app_hdr_nodes:
            app_hdr = app_hdr_nodes[0]
            biz_svc_nodes     = app_hdr.xpath("*[local-name()='BizSvc']")
            msg_def_idr_nodes = app_hdr.xpath("*[local-name()='MsgDefIdr']")
            biz_msg_idr_nodes = app_hdr.xpath("*[local-name()='BizMsgIdr']")
            cre_dt_nodes      = app_hdr.xpath("*[local-name()='CreDt']")
            fr_nodes          = app_hdr.xpath("*[local-name()='Fr']")
            to_nodes          = app_hdr.xpath("*[local-name()='To']")
            hdr_src           = app_hdr.sourceline or 1

            # ── BAH CreDt: mandatory + timezone offset required ───────
            cre_dt_r = hdr_rules.get("creDt", {})
            if not cre_dt_nodes or not _text(cre_dt_nodes[0]):
                report.add_issue(ValidationIssue(
                    severity=cre_dt_r.get("severity", "ERROR"),
                    code=cre_dt_r.get("code", "MISSING_OR_INVALID_BAH_CREDT"),
                    layer=2,
                    path="//AppHdr/CreDt",
                    message=_msg(cre_dt_r, "BAH Creation Date is missing or invalid."),
                    line=hdr_src,
                    fix=cre_dt_r.get("fix", "Add a valid creation date-time with UTC offset."),
                ))
            else:
                cre_dt_val = _text(cre_dt_nodes[0])
                if not re.search(r"([+-]\d{2}:\d{2}|Z)$", cre_dt_val):
                    report.add_issue(ValidationIssue(
                        severity=cre_dt_r.get("severity", "ERROR"),
                        code=cre_dt_r.get("code", "MISSING_OR_INVALID_BAH_CREDT"),
                        path="//AppHdr/CreDt",
                        message=_msg(cre_dt_r,
                            f"BAH Creation Date '{cre_dt_val}' does not include a UTC timezone offset."),
                        line=cre_dt_nodes[0].sourceline or hdr_src,
                        fix=cre_dt_r.get("fix", "Add UTC offset. Example: 2026-11-15T09:30:00+00:00"),
                    ))

            # ── BAH Fr (From) mandatory ───────────────────────────────
            fr_r = hdr_rules.get("from", {})
            if not fr_nodes:
                report.add_issue(ValidationIssue(
                    severity=fr_r.get("severity", "ERROR"),
                    code=fr_r.get("code", "MISSING_BAH_FROM"),
                    layer=2,
                    path="//AppHdr/Fr",
                    message=_msg(fr_r, "BAH From BIC is missing."),
                    line=hdr_src,
                    fix=fr_r.get("fix", "Enter the sending institution BIC in the AppHdr From field."),
                ))

            # ── BAH To mandatory ─────────────────────────────────────
            to_r = hdr_rules.get("to", {})
            if not to_nodes:
                report.add_issue(ValidationIssue(
                    severity=to_r.get("severity", "ERROR"),
                    code=to_r.get("code", "MISSING_BAH_TO"),
                    layer=2,
                    path="//AppHdr/To",
                    message=_msg(to_r, "BAH To BIC is missing."),
                    line=hdr_src,
                    fix=to_r.get("fix", "Enter the receiving institution BIC in the AppHdr To field."),
                ))

            # ── BizMsgIdr: missing check ──────────────────────────────
            biz_msg_missing_r = hdr_rules.get("bizMsgIdrMissing", {})
            if not biz_msg_idr_nodes:
                report.add_issue(ValidationIssue(
                    severity=biz_msg_missing_r.get("severity", "ERROR"),
                    code=biz_msg_missing_r.get("code", "MISSING_BIZ_MSG_IDR"),
                    layer=2,
                    path="//AppHdr/BizMsgIdr",
                    message=_msg(biz_msg_missing_r, "Business Message ID is missing from the header."),
                    line=hdr_src,
                    fix=biz_msg_missing_r.get("fix", "Add a Business Message ID to the AppHdr."),
                ))

            # ── pacs.003: BizSvc must be swift.cbprplus.03 ────────────
            if is_pacs003 and not is_pacs008:
                expected_003 = "swift.cbprplus.03"
                if biz_svc_nodes:
                    val = _text(biz_svc_nodes[0])
                    if val != expected_003:
                        report.add_issue(ValidationIssue(
                            severity="ERROR", code="INVALID_BIZ_SVC",
                            path="//AppHdr/BizSvc",
                            message=f"BizSvc '{val}' is invalid for pacs.003. Must be: {expected_003}",
                            line=biz_svc_nodes[0].sourceline or hdr_src,
                            fix=f"Set <BizSvc>{expected_003}</BizSvc>.",
                        ))

            # ── pacs.008: BizSvc + MsgDefIdr checks ──────────────────
            if is_pacs008:
                biz_svc_r = hdr_rules.get("bizSvc", {})
                # pacs.008 accepts both non-STP (swift.cbprplus.04) and STP
                # (swift.cbprplus.stp.04) variants. Use allowedValues when present,
                # fall back to fixedValue for backwards-compatibility.
                _allowed_biz = biz_svc_r.get("allowedValues") or []
                if not _allowed_biz:
                    _fv = biz_svc_r.get("fixedValue", "swift.cbprplus.04")
                    _allowed_biz = [_fv]
                if biz_svc_nodes:
                    val = _text(biz_svc_nodes[0])
                    if val not in _allowed_biz:
                        report.add_issue(ValidationIssue(
                            severity=biz_svc_r.get("severity", "ERROR"),
                            code=biz_svc_r.get("code", "INVALID_BIZ_SVC"),
                            path="//AppHdr/BizSvc",
                            message=_msg(biz_svc_r,
                                f"BizSvc '{val}' is invalid for pacs.008. "
                                f"Must be one of: {', '.join(_allowed_biz)}."),
                            line=biz_svc_nodes[0].sourceline or hdr_src,
                            fix=biz_svc_r.get("fix",
                                f"Set <BizSvc>{_allowed_biz[0]}</BizSvc>."),
                        ))

                msg_def_r = hdr_rules.get("msgDefIdr", {})
                expected_msg_def = msg_def_r.get("fixedValue", "pacs.008.001.08")
                if msg_def_idr_nodes:
                    val = _text(msg_def_idr_nodes[0])
                    if val != expected_msg_def:
                        report.add_issue(ValidationIssue(
                            severity=msg_def_r.get("severity", "ERROR"),
                            code=msg_def_r.get("code", "INVALID_MSG_DEF_IDR"),
                            path="//AppHdr/MsgDefIdr",
                            message=_msg(msg_def_r,
                                f"MsgDefIdr must be '{expected_msg_def}' for pacs.008."),
                            line=msg_def_idr_nodes[0].sourceline or hdr_src,
                            fix=msg_def_r.get("fix",
                                f"Set <MsgDefIdr>{expected_msg_def}</MsgDefIdr>."),
                        ))

            # ── BizMsgIdr format check ────────────────────────────────
            biz_msg_r   = hdr_rules.get("bizMsgIdr", {})
            biz_msg_re  = biz_msg_r.get("regex", r"^[0-9a-zA-Z/\-\?:\(\)\.,'\+ ]+$")
            biz_msg_max = biz_msg_r.get("maxLength", 35)
            biz_msg_min = biz_msg_r.get("minLength", 1)

            if biz_msg_idr_nodes:
                biz_msg_idr_val = _text(biz_msg_idr_nodes[0])
                bad = (
                    not re.match(biz_msg_re, biz_msg_idr_val)
                    or len(biz_msg_idr_val) > biz_msg_max
                    or len(biz_msg_idr_val) < biz_msg_min
                )
                if bad:
                    report.add_issue(ValidationIssue(
                        severity=biz_msg_r.get("severity", "ERROR"),
                        code=biz_msg_r.get("code", "INVALID_BIZ_MSG_IDR_FORMAT"),
                        path="//AppHdr/BizMsgIdr",
                        message=_msg(biz_msg_r, "Business Message ID format is invalid."),
                        line=biz_msg_idr_nodes[0].sourceline or hdr_src,
                        fix=biz_msg_r.get("fix",
                            "Only 0-9 a-z A-Z / - ? : ( ) . , ' + space are allowed; 1-35 characters."),
                    ))

            # ─────────────────────────────────────────────────────────
            # 4. CROSS-FIELD: BizMsgIdr == GrpHdr.MsgId
            # ─────────────────────────────────────────────────────────
            xf_consist = (xf_rules.get("bizMsgIdrEqualsMsgId")
                          or xf_rules.get("businessMessageConsistency")
                          or {})
            if biz_msg_idr_val is not None and grp_headers:
                msg_id_nodes = grp_headers[0].xpath("*[local-name()='MsgId']")
                if msg_id_nodes:
                    msg_id_val = _text(msg_id_nodes[0])
                    if biz_msg_idr_val != msg_id_val:
                        report.add_issue(ValidationIssue(
                            severity=xf_consist.get("severity", "ERROR"),
                            code=xf_consist.get("code", "MESSAGE_ID_MISMATCH"),
                            path="//GrpHdr/MsgId",
                            message=_msg(xf_consist,
                                f"Business Message ID '{biz_msg_idr_val}' does not match "
                                f"Group Header Message ID '{msg_id_val}'."),
                            line=msg_id_nodes[0].sourceline or 1,
                            fix=xf_consist.get("fix", "Set <MsgId> to match <BizMsgIdr>."),
                        ))

            # ─────────────────────────────────────────────────────────
            # CROSS-FIELD: BIC matching (Fr/To vs InstgAgt/InstdAgt)
            # ─────────────────────────────────────────────────────────
            from_bic_nodes  = app_hdr.xpath("*[local-name()='Fr']//*[local-name()='BICFI']")
            to_bic_nodes    = app_hdr.xpath("*[local-name()='To']//*[local-name()='BICFI']")
            cpy_dplct_nodes = app_hdr.xpath("*[local-name()='CpyDplct']")
            from_bic      = _text(from_bic_nodes[0]) if from_bic_nodes else None
            to_bic        = _text(to_bic_nodes[0])   if to_bic_nodes   else None
            has_cpy_dplct = bool(cpy_dplct_nodes)

            xf_from = (xf_rules.get("fromBicMatchesInstgAgt")
                       or xf_rules.get("fromBicMustMatchInstgAgt") or {})
            xf_to   = (xf_rules.get("toBicMatchesInstdAgt")
                       or xf_rules.get("toBicMustMatchInstdAgt") or {})

            for tx in all_tx_blocks:
                tx_name = tx.tag.split("}")[-1] if isinstance(tx.tag, str) else "Transaction"

                instg_bic_nodes = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
                if from_bic and instg_bic_nodes:
                    instg_bic = _text(instg_bic_nodes[0])
                    if _bic_norm(from_bic) != _bic_norm(instg_bic):
                        report.add_issue(ValidationIssue(
                            severity=xf_from.get("severity", "ERROR"),
                            code=xf_from.get("code", "FROM_BIC_MISMATCH"),
                            path=f"//{tx_name}/InstgAgt",
                            message=_msg(xf_from,
                                f"BAH From BIC '{from_bic}' does not match "
                                f"Instructing Agent BIC '{instg_bic}'."),
                            line=instg_bic_nodes[0].sourceline or tx.sourceline or 1,
                            fix=xf_from.get("fix",
                                "Use the same BIC for the BAH From field and the Instructing Agent."),
                        ))

                except_copy = xf_to.get("exceptCopyDuplicate", True)
                if not (except_copy and has_cpy_dplct):
                    instd_bic_nodes = tx.xpath("*[local-name()='InstdAgt']//*[local-name()='BICFI']")
                    if to_bic and instd_bic_nodes:
                        instd_bic = _text(instd_bic_nodes[0])
                        if _bic_norm(to_bic) != _bic_norm(instd_bic):
                            report.add_issue(ValidationIssue(
                                severity=xf_to.get("severity", "ERROR"),
                                code=xf_to.get("code", "TO_BIC_MISMATCH"),
                                path=f"//{tx_name}/InstdAgt",
                                message=_msg(xf_to,
                                    f"BAH To BIC '{to_bic}' does not match "
                                    f"Instructed Agent BIC '{instd_bic}'."),
                                line=instd_bic_nodes[0].sourceline or tx.sourceline or 1,
                                fix=xf_to.get("fix",
                                    "Use the same BIC for the BAH To field and the Instructed Agent."),
                            ))

        # ─────────────────────────────────────────────────────────────
        # 5. MANDATORY TRANSACTION FIELDS  (pacs.008 only)
        # ─────────────────────────────────────────────────────────────
        if is_pacs008 and mf_tx:
            # Fields whose XPath cannot be derived by splitting the key on '.':
            # Agent BICs are nested inside FinInstnId — use //* to reach any depth.
            _TX_XPATH_OVERRIDE = {
                "InstgAgt.BICFI": "*[local-name()='InstgAgt']//*[local-name()='BICFI']",
                "InstdAgt.BICFI": "*[local-name()='InstdAgt']//*[local-name()='BICFI']",
                "DbtrAgt.BICFI":  "*[local-name()='DbtrAgt']//*[local-name()='BICFI']",
                "CdtrAgt.BICFI":  "*[local-name()='CdtrAgt']//*[local-name()='BICFI']",
            }

            for tx in all_tx_blocks:
                tx_name = tx.tag.split("}")[-1] if isinstance(tx.tag, str) else "Transaction"
                src     = tx.sourceline or 1

                for field_key, cfg in mf_tx.items():
                    sev   = cfg.get("severity", "ERROR")
                    code  = cfg.get("code", f"MISSING_{field_key.replace('.','_').upper()}")
                    msg   = _msg(cfg, f"Mandatory <{field_key}> is missing.")
                    fix   = cfg.get("fix", f"Add {field_key} to the transaction block.")
                    parts = field_key.split(".")
                    leaf  = parts[-1]
                    fpath = f"//{tx_name}/{'/'.join(parts)}"

                    # ── Currency attribute check (e.g. IntrBkSttlmAmt.Ccy) ──
                    if leaf == "Ccy" and len(parts) >= 2:
                        parent_xpath = "/".join(f"*[local-name()='{p}']" for p in parts[:-1])
                        parent_nodes = tx.xpath(parent_xpath)
                        if parent_nodes:
                            ccy_attr = (parent_nodes[0].get("Ccy") or "").strip()
                            if not ccy_attr:
                                report.add_issue(ValidationIssue(
                                    severity=sev, code=code, layer=2,
                                    path=f"//{tx_name}/{'/'.join(parts[:-1])}/@Ccy",
                                    message=msg,
                                    line=parent_nodes[0].sourceline or src,
                                    fix=fix,
                                ))
                        # If parent is absent, that's caught by the parent field's own check
                        continue

                    # ── Custom XPath override for agent BIC fields ────
                    if field_key in _TX_XPATH_OVERRIDE:
                        nodes = tx.xpath(_TX_XPATH_OVERRIDE[field_key])
                    else:
                        nodes = tx.xpath(
                            "/".join(f"*[local-name()='{p}']" for p in parts)
                        )

                    if not nodes:
                        report.add_issue(ValidationIssue(
                            severity=sev, code=code, layer=2, path=fpath,
                            message=msg, line=src, fix=fix,
                        ))
                    else:
                        node = nodes[0]
                        val  = _text(node)

                        # Blank check (only for text-bearing leaf nodes with no children)
                        if not val and not list(node):
                            report.add_issue(ValidationIssue(
                                severity=sev, code=code, layer=2, path=fpath,
                                message=msg, line=node.sourceline or src, fix=fix,
                            ))

                        # UETR format validation
                        elif field_key == "PmtId.UETR" and val:
                            uetr_pat = (cfg.get("regex")
                                        or r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}"
                                           r"-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
                            if not re.match(uetr_pat, val):
                                fmt_r = (rule_registry.get_all_rules()
                                         .get("formatRules", {}).get("uetr", {}))
                                report.add_issue(ValidationIssue(
                                    severity=fmt_r.get("severity", "ERROR"),
                                    code=fmt_r.get("code", "INVALID_UETR_FORMAT"),
                                    path=fpath,
                                    message=fmt_r.get("message",
                                        f"UETR '{val}' is not a valid lowercase UUID v4."),
                                    line=node.sourceline or src,
                                    fix=fmt_r.get("fix",
                                        "Enter a valid lowercase UUID v4. Example: "
                                        "4a1a0945-5772-409a-83ba-240e666e0267"),
                                ))

        # ─────────────────────────────────────────────────────────────
        # 6. SERVICE LEVEL — GPI disallowed codes
        # The disallowed set loaded here is the pacs.008 set (only G001 allowed).
        # It is only correct for pacs.008 — pacs.009 CORE/ADV require G004 and
        # pacs.009 COV requires G001, with different disallowed sets. The GPI
        # rule for all pacs.009 variants is handled by CBPRFormalRulesValidator
        # (cbpr_formal_rules.py), so restrict this pacs.008-scoped check to
        # pacs.008 to avoid flagging the legitimate G004 code on pacs.009.
        if is_pacs008:
            disallowed_gpi = set(rule_registry.get_gpi_disallowed_codes())
            svc_sev  = svc_rules.get("severity", "ERROR")
            svc_code = svc_rules.get("code", "INVALID_GPI_SERVICE_LEVEL")
            svc_msg  = (svc_rules.get("errorMessage")
                        or "GPI Service Level code is not allowed in SR2026.")
            svc_fix  = svc_rules.get("fix", "Use G001 only.")

            for cd_node in root_element.xpath(
                "//*[local-name()='PmtTpInf']/*[local-name()='SvcLvl']/*[local-name()='Cd']"
            ):
                if _text(cd_node) in disallowed_gpi:
                    report.add_issue(ValidationIssue(
                        severity=svc_sev, code=svc_code,
                        path="//PmtTpInf/SvcLvl/Cd",
                        message=f"{svc_msg} Code '{_text(cd_node)}' is disallowed.",
                        line=cd_node.sourceline or 1,
                        fix=svc_fix,
                    ))

        # ─────────────────────────────────────────────────────────────
        # 7. DATE RULES — CBPR_Date (no timezone offset)
        # ─────────────────────────────────────────────────────────────
        date_sev  = date_rules.get("severity", "ERROR")
        date_code = date_rules.get("code", "INVALID_DATE_TIMEZONE")
        date_msg  = (date_rules.get("errorMessage")
                     or "Date field contains a timezone offset which is not allowed in SR2026.")
        date_fix  = date_rules.get("fix",
                        "Remove timezone offset (e.g. change '2026-11-20Z' to '2026-11-20').")
        tz_pat    = re.compile(r"(Z|[+-]\d{2}:\d{2})$")

        for elem in root_element.iter():
            if not isinstance(elem.tag, str):
                continue
            tag = elem.tag.split("}")[-1]
            if (tag.endswith("Dt") or tag == "Dt") and not tag.endswith("DtTm") and tag != "CreDt":
                val = _text(elem)
                if val and tz_pat.search(val):
                    report.add_issue(ValidationIssue(
                        severity=date_sev, code=date_code,
                        path=f"//{tag}",
                        message=f"{date_msg} Field: <{tag}> value: '{val}'.",
                        line=elem.sourceline or 1,
                        fix=date_fix,
                    ))

        # ─────────────────────────────────────────────────────────────
        # 8. PROXY RULES — Tp mandatory when Prxy present
        # ─────────────────────────────────────────────────────────────
        prxy_sev  = prxy_rules.get("severity", "ERROR")
        prxy_code = prxy_rules.get("code", "MISSING_PROXY_TYPE")
        prxy_msg  = (prxy_rules.get("errorMessage")
                     or "Proxy Type <Tp> is mandatory inside <Prxy> when Proxy is present.")
        prxy_fix  = prxy_rules.get("fix",
                        "Add <Tp><Cd>...</Cd></Tp> inside the <Prxy> block.")

        for prxy in root_element.xpath("//*[local-name()='Prxy']"):
            tp_nodes = prxy.xpath("*[local-name()='Tp']")
            if not tp_nodes or not (tp_nodes[0].xpath("*") or _text(tp_nodes[0])):
                report.add_issue(ValidationIssue(
                    severity=prxy_sev, code=prxy_code, path="//Prxy/Tp",
                    message=prxy_msg,
                    line=prxy.sourceline or 1,
                    fix=prxy_fix,
                ))

        # ─────────────────────────────────────────────────────────────
        # 9. STRUCTURED REMITTANCE — max 9000 business characters
        # ─────────────────────────────────────────────────────────────
        strd_rule = next(
            (r for r in rule_registry.get_validation_rules()
             if r.get("id") == "SR2026_R016"),
            {}
        )
        strd_max  = strd_rule.get("maxLength", 9000)
        strd_sev  = strd_rule.get("severity", "ERROR")
        strd_code = strd_rule.get("code", "STRD_LENGTH_EXCEEDED")
        strd_msg  = (strd_rule.get("errorMessage")
                     or f"Structured Remittance block exceeds the maximum of {strd_max} characters.")
        strd_fix  = strd_rule.get("fix",
                        f"Reduce the content inside <Strd> to within {strd_max} characters.")

        for strd in root_element.xpath("//*[local-name()='Strd']"):
            total_len = sum(
                len(desc.text.strip())
                for desc in strd.iter()
                if desc.text and not list(desc)
            )
            if total_len > strd_max:
                report.add_issue(ValidationIssue(
                    severity=strd_sev, code=strd_code, path="//RmtInf/Strd",
                    message=f"{strd_msg} Current length: {total_len}.",
                    line=strd.sourceline or 1,
                    fix=strd_fix,
                ))
