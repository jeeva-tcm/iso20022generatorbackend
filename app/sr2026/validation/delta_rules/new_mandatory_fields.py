"""
SR2026 New Mandatory Fields Validator
======================================
Production-ready validator that enforces every mandatory-field and
cross-field rule defined in app/services/validation/sr2026/rules/pacs.008.sr2026.json.

Rule categories implemented here:
  1. Header Rules         (BizSvc, MsgDefIdr, BizMsgIdr format)
  2. Cross-Field Rules    (BizMsgIdr == MsgId, Fr/To BIC matching)
  3. Mandatory Fields     (UETR, GrpHdr fields, InstdAmt, EndToEndId)
  4. Service Level Rules  (GPI code allow/disallow list)
  5. Date Rules           (CBPR_Date — no timezone offset)
  6. Proxy Rules          (Prxy.Tp mandatory when Prxy present)
  7. Remittance Rules     (Structured Remittance ≤ 9000 chars)

Address rules are handled separately by AddressValidator.
"""

import re
from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport
from app.sr2026.rules import rule_registry


def _bic_norm(bic: str) -> str:
    """Normalise BIC to 11 chars: pad 8-char BIC with 'XXX'."""
    bic = bic.upper()
    return bic + "XXX" if len(bic) == 8 else bic


class NewMandatoryFieldsValidator:

    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # ── Load rule definitions once ─────────────────────────────────────
        hdr_rules   = rule_registry.get_header_rules()
        xf_rules    = rule_registry.get_cross_field_rules()
        mf_grp      = rule_registry.get_mandatory_group_header_fields()
        mf_tx       = rule_registry.get_mandatory_transaction_fields()
        svc_rules   = rule_registry.get_service_level_rules()
        date_rules  = rule_registry.get_date_rules()
        prxy_rules  = rule_registry.get_proxy_rules()

        # ── Helper shortcuts ──────────────────────────────────────────────
        def _text(node) -> str:
            return (node.text or "").strip()

        # ─────────────────────────────────────────────────────────────────
        # 1. MANDATORY GROUP HEADER FIELDS
        # ─────────────────────────────────────────────────────────────────
        grp_headers = root_element.xpath("//*[local-name()='GrpHdr']")
        for grp in grp_headers:
            src = grp.sourceline or 1

            # CreDtTm
            if not grp.xpath("*[local-name()='CreDtTm']"):
                r = mf_grp.get("CreDtTm", {})
                report.add_issue(ValidationIssue(
                    severity=r.get("severity", "ERROR"),
                    code=r.get("code", "MISSING_CREATION_DATE_TIME"),
                    path="//GrpHdr/CreDtTm",
                    message=r.get("description", "Group Header is missing mandatory <CreDtTm>."),
                    line=src,
                    fix=r.get("fix", "Add <CreDtTm> with an ISO 8601 timestamp."),
                ))

            # NbOfTxs
            if not grp.xpath("*[local-name()='NbOfTxs']"):
                r = mf_grp.get("NbOfTxs", {})
                report.add_issue(ValidationIssue(
                    severity=r.get("severity", "ERROR"),
                    code=r.get("code", "MISSING_NB_OF_TXS"),
                    path="//GrpHdr/NbOfTxs",
                    message=r.get("description", "Group Header is missing mandatory <NbOfTxs>."),
                    line=src,
                    fix=r.get("fix", "Add <NbOfTxs> with the count of transactions."),
                ))

        # ─────────────────────────────────────────────────────────────────
        # 2. HEADER RULES (AppHdr — BizSvc, MsgDefIdr, BizMsgIdr)
        # ─────────────────────────────────────────────────────────────────
        app_hdr_nodes = root_element.xpath("//*[local-name()='AppHdr']")
        biz_msg_idr_val = None   # captured for cross-field check later

        # Determine if this is a pacs.008 message (even if AppHdr is missing)
        is_pacs008 = False
        if root_element.xpath("//*[local-name()='FIToFICstmrCdtTrf']"):
            is_pacs008 = True

        if app_hdr_nodes:
            app_hdr = app_hdr_nodes[0]
            biz_svc_nodes      = app_hdr.xpath("*[local-name()='BizSvc']")
            msg_def_idr_nodes  = app_hdr.xpath("*[local-name()='MsgDefIdr']")
            biz_msg_idr_nodes  = app_hdr.xpath("*[local-name()='BizMsgIdr']")

            if msg_def_idr_nodes and "pacs.008" in _text(msg_def_idr_nodes[0]):
                is_pacs008 = True

            if is_pacs008:
                # ── BizSvc must be swift.cbprplus.04 ──────────────────────
                biz_svc_r = hdr_rules.get("bizSvc", {})
                expected_biz_svc = biz_svc_r.get("fixedValue", "swift.cbprplus.04")
                if biz_svc_nodes:
                    val = _text(biz_svc_nodes[0])
                    if val != expected_biz_svc:
                        report.add_issue(ValidationIssue(
                            severity=biz_svc_r.get("severity", "ERROR"),
                            code=biz_svc_r.get("code", "INVALID_BIZ_SVC"),
                            path="//AppHdr/BizSvc",
                            message=biz_svc_r.get(
                                "errorMessage",
                                f"<BizSvc> must be '{expected_biz_svc}' for pacs.008 in SR2026."
                            ),
                            line=biz_svc_nodes[0].sourceline or 1,
                            fix=biz_svc_r.get("fix", f"Set <BizSvc>{expected_biz_svc}</BizSvc>."),
                        ))

                # ── MsgDefIdr must be pacs.008.001.08 ─────────────────────
                msg_def_r = hdr_rules.get("msgDefIdr", {})
                expected_msg_def = msg_def_r.get("fixedValue", "pacs.008.001.08")
                if msg_def_idr_nodes:
                    val = _text(msg_def_idr_nodes[0])
                    if val != expected_msg_def:
                        report.add_issue(ValidationIssue(
                            severity=msg_def_r.get("severity", "ERROR"),
                            code=msg_def_r.get("code", "INVALID_MSG_DEF_IDR"),
                            path="//AppHdr/MsgDefIdr",
                            message=msg_def_r.get(
                                "errorMessage",
                                f"<MsgDefIdr> must be '{expected_msg_def}' for pacs.008 in SR2026."
                            ),
                            line=msg_def_idr_nodes[0].sourceline or 1,
                            fix=msg_def_r.get("fix", f"Set <MsgDefIdr>{expected_msg_def}</MsgDefIdr>."),
                        ))

            # ── BizMsgIdr — RestrictedFINXMax35Text ───────────────────────
            biz_msg_r = hdr_rules.get("bizMsgIdr", {})
            biz_msg_regex   = biz_msg_r.get("regex", r"^[0-9a-zA-Z/\-\?:\(\)\.,'\+ ]+$")
            biz_msg_max_len = biz_msg_r.get("maxLength", 35)
            biz_msg_min_len = biz_msg_r.get("minLength", 1)

            if biz_msg_idr_nodes:
                biz_msg_idr_val = _text(biz_msg_idr_nodes[0])
                bad_format = (
                    not re.match(biz_msg_regex, biz_msg_idr_val)
                    or len(biz_msg_idr_val) > biz_msg_max_len
                    or len(biz_msg_idr_val) < biz_msg_min_len
                )
                if bad_format:
                    report.add_issue(ValidationIssue(
                        severity=biz_msg_r.get("severity", "ERROR"),
                        code=biz_msg_r.get("code", "INVALID_BIZ_MSG_IDR_FORMAT"),
                        path="//AppHdr/BizMsgIdr",
                        message=biz_msg_r.get(
                            "errorMessage",
                            f"<BizMsgIdr> value '{biz_msg_idr_val}' does not match RestrictedFINXMax35Text."
                        ),
                        line=biz_msg_idr_nodes[0].sourceline or 1,
                        fix=biz_msg_r.get(
                            "fix",
                            "Only 0-9 a-z A-Z / - ? : ( ) . , ' + space are allowed; 1-35 characters."
                        ),
                    ))

            # ─────────────────────────────────────────────────────────────
            # 3. CROSS-FIELD: BizMsgIdr == GrpHdr.MsgId
            # ─────────────────────────────────────────────────────────────
            xf_consistency = xf_rules.get("businessMessageConsistency", {})
            if biz_msg_idr_val is not None:
                grp_hdr_nodes = root_element.xpath("//*[local-name()='GrpHdr']")
                if grp_hdr_nodes:
                    msg_id_nodes = grp_hdr_nodes[0].xpath("*[local-name()='MsgId']")
                    if msg_id_nodes:
                        msg_id_val = _text(msg_id_nodes[0])
                        if biz_msg_idr_val != msg_id_val:
                            report.add_issue(ValidationIssue(
                                severity=xf_consistency.get("severity", "ERROR"),
                                code=xf_consistency.get("code", "MESSAGE_ID_MISMATCH"),
                                path="//GrpHdr/MsgId",
                                message=xf_consistency.get(
                                    "errorMessage",
                                    f"GrpHdr <MsgId> '{msg_id_val}' must equal BAH <BizMsgIdr> '{biz_msg_idr_val}'."
                                ),
                                line=msg_id_nodes[0].sourceline or grp_hdr_nodes[0].sourceline or 1,
                                fix=xf_consistency.get("fix", "Set <MsgId> to match <BizMsgIdr>."),
                            ))

            # ─────────────────────────────────────────────────────────────
            # 4. CROSS-FIELD: BIC matching (Fr/To vs InstgAgt/InstdAgt)
            # ─────────────────────────────────────────────────────────────
            from_bic_nodes   = app_hdr.xpath("*[local-name()='Fr']//*[local-name()='BICFI']")
            to_bic_nodes     = app_hdr.xpath("*[local-name()='To']//*[local-name()='BICFI']")
            cpy_dplct_nodes  = app_hdr.xpath("*[local-name()='CpyDplct']")

            from_bic    = _text(from_bic_nodes[0]) if from_bic_nodes else None
            to_bic      = _text(to_bic_nodes[0])   if to_bic_nodes   else None
            has_cpy_dplct = bool(cpy_dplct_nodes)

            xf_from = xf_rules.get("fromBicMustMatchInstgAgt", {})
            xf_to   = xf_rules.get("toBicMustMatchInstdAgt", {})

            tx_blocks = root_element.xpath(
                "//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']"
            )

            for tx in tx_blocks:
                tx_name = tx.tag.split("}")[-1] if isinstance(tx.tag, str) else "Transaction"

                # Rule: Fr BIC == InstgAgt BIC
                instg_bic_nodes = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
                if from_bic and instg_bic_nodes:
                    instg_bic = _text(instg_bic_nodes[0])
                    if _bic_norm(from_bic) != _bic_norm(instg_bic):
                        report.add_issue(ValidationIssue(
                            severity=xf_from.get("severity", "ERROR"),
                            code=xf_from.get("code", "FROM_BIC_MISMATCH"),
                            path=f"//{tx_name}/InstgAgt",
                            message=xf_from.get(
                                "errorMessage",
                                f"Instructing Agent BIC '{instg_bic}' must match BAH From BIC '{from_bic}'."
                            ),
                            line=instg_bic_nodes[0].sourceline or tx.sourceline or 1,
                            fix=xf_from.get("fix", f"Set Instructing Agent BIC to match BAH From BIC '{from_bic}'."),
                        ))

                # Rule: To BIC == InstdAgt BIC (skip when CpyDplct present)
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
                                message=xf_to.get(
                                    "errorMessage",
                                    f"Instructed Agent BIC '{instd_bic}' must match BAH To BIC '{to_bic}'."
                                ),
                                line=instd_bic_nodes[0].sourceline or tx.sourceline or 1,
                                fix=xf_to.get("fix", f"Set Instructed Agent BIC to match BAH To BIC '{to_bic}'."),
                            ))

        # ─────────────────────────────────────────────────────────────────
        # 5. MANDATORY FIELDS — per-transaction block
        # ─────────────────────────────────────────────────────────────────
        # Re-fetch tx_blocks if AppHdr was not present (edge case)
        tx_blocks = root_element.xpath(
            "//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']"
        )

        for tx in tx_blocks:
            tx_name = tx.tag.split("}")[-1] if isinstance(tx.tag, str) else "Transaction"
            src     = tx.sourceline or 1

            # ── UETR ──────────────────────────────────────────────────────
            pmt_id = tx.xpath("*[local-name()='PmtId']")
            uetr_present = False
            if pmt_id:
                uetr = pmt_id[0].xpath("*[local-name()='UETR']")
                if uetr and _text(uetr[0]):
                    uetr_present = True
            if not uetr_present:
                r = mf_tx.get("PmtId.UETR", {})
                report.add_issue(ValidationIssue(
                    severity=r.get("severity", "ERROR"),
                    code=r.get("code", "MANDATORY_UETR_MISSING"),
                    path=f"//{tx_name}/PmtId",
                    message=r.get("description", "Mandatory <UETR> is missing in pacs.008."),
                    line=src,
                    fix=r.get("fix", "Add a valid UUID v4 to <PmtId><UETR>."),
                ))

            # ── InstdAmt (Only mandatory for pacs.008 in SR2026) ───────────
            if is_pacs008:
                r = mf_tx.get("InstdAmt", {})
                instd_amt_nodes = tx.xpath("*[local-name()='InstdAmt']")
                if not instd_amt_nodes or not _text(instd_amt_nodes[0]):
                    report.add_issue(ValidationIssue(
                        severity=r.get("severity", "ERROR"),
                        code=r.get("code", "MANDATORY_INSTD_AMT_MISSING"),
                        path=f"//{tx_name}/InstdAmt",
                        message=r.get(
                            "description",
                            f"Mandatory Instructed Amount <InstdAmt> is missing inside '{tx_name}'."
                        ),
                        line=src,
                        fix=r.get("fix", "Add <InstdAmt Ccy=\"XXX\">amount</InstdAmt> inside the transaction block."),
                    ))

            # ── EndToEndId cannot be blank ────────────────────────────────
            r = mf_tx.get("PmtId.EndToEndId", {})
            e2e_nodes = tx.xpath("*[local-name()='PmtId']/*[local-name()='EndToEndId']")
            if e2e_nodes:
                e2e_val = _text(e2e_nodes[0])
                if not e2e_val:
                    report.add_issue(ValidationIssue(
                        severity=r.get("severity", "ERROR"),
                        code=r.get("code", "INVALID_END_TO_END_ID"),
                        path=f"//{tx_name}/PmtId/EndToEndId",
                        message=r.get(
                            "description",
                            "End-to-End ID <EndToEndId> cannot be empty. Use 'NOTPROVIDED' if not applicable."
                        ),
                        line=e2e_nodes[0].sourceline or src,
                        fix=r.get("fix", "Set <EndToEndId> to 'NOTPROVIDED' or a valid ID string."),
                    ))

        # ─────────────────────────────────────────────────────────────────
        # 6. SERVICE LEVEL — GPI code restrictions
        # ─────────────────────────────────────────────────────────────────
        disallowed_gpi = set(rule_registry.get_gpi_disallowed_codes())
        svc_severity   = svc_rules.get("severity", "ERROR")
        svc_code       = svc_rules.get("code", "INVALID_GPI_SERVICE_LEVEL")
        svc_msg        = svc_rules.get("errorMessage", "GPI Service Level code is not allowed in SR2026.")
        svc_fix        = svc_rules.get("fix", "Use 'G001' or a valid non-GPI service level code.")

        svc_lvl_cd_nodes = root_element.xpath(
            "//*[local-name()='PmtTpInf']/*[local-name()='SvcLvl']/*[local-name()='Cd']"
        )
        for cd_node in svc_lvl_cd_nodes:
            cd_val = _text(cd_node)
            if cd_val in disallowed_gpi:
                report.add_issue(ValidationIssue(
                    severity=svc_severity,
                    code=svc_code,
                    path="//PmtTpInf/SvcLvl/Cd",
                    message=f"GPI Service Level code '{cd_val}' is not allowed in SR2026. Only 'G001' is allowed.",
                    line=cd_node.sourceline or 1,
                    fix=svc_fix,
                ))

        # ─────────────────────────────────────────────────────────────────
        # 7. DATE RULES — CBPR_Date (no timezone offset)
        # ─────────────────────────────────────────────────────────────────
        date_severity = date_rules.get("severity", "ERROR")
        date_code     = date_rules.get("code", "INVALID_DATE_TIMEZONE")
        date_msg      = date_rules.get("errorMessage",
                            "Date field contains timezone offset which is not allowed in SR2026.")
        date_fix      = date_rules.get("fix",
                            "Remove timezone offset (e.g. change '2026-11-20Z' to '2026-11-20').")
        tz_pattern    = re.compile(r"(Z|[+-]\d{2}:\d{2})$")

        for elem in root_element.iter():
            if not isinstance(elem.tag, str):
                continue
            tag = elem.tag.split("}")[-1]
            # Match date-type fields: end in Dt but NOT DtTm, and not CreDt
            is_date_field = (
                (tag.endswith("Dt") or tag == "Dt")
                and not tag.endswith("DtTm")
                and tag != "CreDt"
            )
            if is_date_field:
                val = _text(elem)
                if val and tz_pattern.search(val):
                    report.add_issue(ValidationIssue(
                        severity=date_severity,
                        code=date_code,
                        path=f"//{tag}",
                        message=f"Date field <{tag}> contains timezone offset '{val}', which is not allowed in SR2026.",
                        line=elem.sourceline or 1,
                        fix=f"Remove timezone offset (e.g. change '{val}' to '{val[:10]}').",
                    ))

        # ─────────────────────────────────────────────────────────────────
        # 8. PROXY RULES — Prxy.Tp mandatory when Prxy present
        # ─────────────────────────────────────────────────────────────────
        prxy_severity = prxy_rules.get("severity", "ERROR")
        prxy_code     = prxy_rules.get("code", "MANDATORY_PROXY_TYPE_MISSING")
        prxy_msg      = prxy_rules.get("errorMessage",
                            "Proxy Type <Tp> is mandatory inside <Prxy> when Proxy is present in SR2026.")
        prxy_fix      = prxy_rules.get("fix",
                            "Add <Tp><Cd>...</Cd></Tp> or <Tp><Prtry>...</Prtry></Tp> inside <Prxy>.")

        prxy_nodes = root_element.xpath("//*[local-name()='Prxy']")
        for prxy in prxy_nodes:
            tp_nodes = prxy.xpath("*[local-name()='Tp']")
            if not tp_nodes or not (tp_nodes[0].xpath("*") or _text(tp_nodes[0])):
                report.add_issue(ValidationIssue(
                    severity=prxy_severity,
                    code=prxy_code,
                    path="//Prxy/Tp",
                    message=prxy_msg,
                    line=prxy.sourceline or 1,
                    fix=prxy_fix,
                ))

        # ─────────────────────────────────────────────────────────────────
        # 9. STRUCTURED REMITTANCE — max 9000 business characters
        # ─────────────────────────────────────────────────────────────────
        strd_rule = next(
            (r for r in rule_registry.get_validation_rules() if r.get("id") == "SR2026_R016"),
            {}
        )
        strd_max  = strd_rule.get("maxLength", 9000)
        strd_sev  = strd_rule.get("severity", "ERROR")
        strd_code = strd_rule.get("code", "STRD_LENGTH_EXCEEDED")
        strd_msg  = strd_rule.get("errorMessage",
                        f"Structured Remittance block exceeds the maximum of {strd_max} business characters.")
        strd_fix  = strd_rule.get("fix",
                        f"Reduce the content inside <Strd> to within {strd_max} characters.")

        strd_nodes = root_element.xpath("//*[local-name()='Strd']")
        for strd in strd_nodes:
            total_len = sum(
                len(desc.text.strip())
                for desc in strd.iter()
                if desc.text and not list(desc)
            )
            if total_len > strd_max:
                report.add_issue(ValidationIssue(
                    severity=strd_sev,
                    code=strd_code,
                    path="//RmtInf/Strd",
                    message=f"Structured Remittance block character count ({total_len}) exceeds the maximum limit of {strd_max} business characters.",
                    line=strd.sourceline or 1,
                    fix=strd_fix,
                ))
