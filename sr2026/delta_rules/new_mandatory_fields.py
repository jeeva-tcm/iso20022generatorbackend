import re
from lxml import etree
from sr2026.validators.models import ValidationIssue, ValidationReport

class NewMandatoryFieldsValidator:
    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # 1. Enforce UETR presence on all transaction elements in payment flows (pacs.008, pacs.009, pacs.004)
        doc_ns = root_element.xpath("//*[local-name()='Document']")
        if doc_ns:
            ns = etree.QName(doc_ns[0]).namespace or ""
            is_payment_flow = any(x in ns for x in ["pacs.008", "pacs.009", "pacs.004", "pacs.003", "pain.001", "pain.008"])
            
            if is_payment_flow:
                # Find all transaction details blocks
                tx_blocks = root_element.xpath("//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']")
                
                for tx in tx_blocks:
                    tx_name = tx.tag.split('}')[-1] if isinstance(tx.tag, str) else "Transaction"
                    # Check for UETR inside PmtId
                    pmt_id = tx.xpath("*[local-name()='PmtId']")
                    uetr_present = False
                    if pmt_id:
                        uetr = pmt_id[0].xpath("*[local-name()='UETR']")
                        if uetr and (uetr[0].text or "").strip():
                            uetr_present = True
                            
                    if not uetr_present:
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="MANDATORY_UETR_MISSING",
                            path=f"//{tx_name}/PmtId",
                            message=f"Mandatory transaction tracking identifier <UETR> is missing inside '{tx_name}'. UETR is strictly required in SR2026.",
                            line=tx.sourceline or 1,
                            fix="Add <UETR> with a valid UUID v4 tracking code inside the <PmtId> container block."
                        ))
                        
        # 2. Check that GrpHdr has CreDtTm and NbOfTxs
        grp_headers = root_element.xpath("//*[local-name()='GrpHdr']")
        for grp in grp_headers:
            line = grp.sourceline or 1
            has_cre_dt = bool(grp.xpath("*[local-name()='CreDtTm']"))
            has_nb_txs = bool(grp.xpath("*[local-name()='NbOfTxs']"))
            
            if not has_cre_dt:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MANDATORY_CREATION_TIME_MISSING",
                    path="//GrpHdr",
                    message="Group Header is missing mandatory Creation Date Time <CreDtTm>.",
                    line=line,
                    fix="Add <CreDtTm> tag with current timestamp (e.g. 2026-06-03T14:47:33+00:00)."
                ))
            if not has_nb_txs:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MANDATORY_TX_COUNT_MISSING",
                    path="//GrpHdr",
                    message="Group Header is missing mandatory Number of Transactions <NbOfTxs>.",
                    line=line,
                    fix="Add <NbOfTxs> tag containing the count of transactions in the message."
                ))

        # --- SR2026 Delta Rules Implementation ---

        app_hdr = root_element.xpath("//*[local-name()='AppHdr']")
        if app_hdr:
            biz_svc_nodes = app_hdr[0].xpath("*[local-name()='BizSvc']")
            msg_def_idr_nodes = app_hdr[0].xpath("*[local-name()='MsgDefIdr']")
            biz_msg_idr_nodes = app_hdr[0].xpath("*[local-name()='BizMsgIdr']")
            
            # Check if it is pacs.008
            is_pacs008 = False
            if msg_def_idr_nodes and "pacs.008" in (msg_def_idr_nodes[0].text or ""):
                is_pacs008 = True
            elif root_element.xpath("//*[local-name()='FIToFICstmrCdtTrf']"):
                is_pacs008 = True
                
            if is_pacs008:
                # Rule 1.1: BizSvc must be swift.cbprplus.04
                if biz_svc_nodes:
                    biz_svc_val = (biz_svc_nodes[0].text or "").strip()
                    if biz_svc_val != "swift.cbprplus.04":
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="INVALID_BIZ_SVC",
                            path="//AppHdr/BizSvc",
                            message="Business Service <BizSvc> must be 'swift.cbprplus.04' for pacs.008 in SR2026.",
                            line=biz_svc_nodes[0].sourceline or 1,
                            fix="Set <BizSvc> to 'swift.cbprplus.04'."
                        ))
                # Rule 1.2: MsgDefIdr must be pacs.008.001.08
                if msg_def_idr_nodes:
                    msg_def_val = (msg_def_idr_nodes[0].text or "").strip()
                    if msg_def_val != "pacs.008.001.08":
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="INVALID_MSG_DEF_IDR",
                            path="//AppHdr/MsgDefIdr",
                            message="Message Definition Identifier <MsgDefIdr> must be 'pacs.008.001.08' for pacs.008 in SR2026.",
                            line=msg_def_idr_nodes[0].sourceline or 1,
                            fix="Set <MsgDefIdr> to 'pacs.008.001.08'."
                        ))
                        
            # Rule 1.3: BizMsgIdr must match RestrictedFINXMax35Text
            if biz_msg_idr_nodes:
                biz_msg_idr_val = (biz_msg_idr_nodes[0].text or "").strip()
                pattern = r"^[0-9a-zA-Z/\-\?:\(\)\.,'\+ ]+$"
                if not re.match(pattern, biz_msg_idr_val) or len(biz_msg_idr_val) > 35 or len(biz_msg_idr_val) < 1:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_BIZ_MSG_IDR_FORMAT",
                        path="//AppHdr/BizMsgIdr",
                        message=f"Business Message Identifier <BizMsgIdr> value '{biz_msg_idr_val}' does not match CBPR+ RestrictedFINXMax35Text pattern.",
                        line=biz_msg_idr_nodes[0].sourceline or 1,
                        fix="Ensure <BizMsgIdr> contains only allowed character set (0-9 a-z A-Z / - ? : ( ) . , ' + space) and is between 1 and 35 characters long."
                    ))

            # Rule 2: BAH Business Message ID must equal Group Header Message ID
            if biz_msg_idr_nodes:
                biz_msg_idr_val = (biz_msg_idr_nodes[0].text or "").strip()
                grp_hdr_nodes = root_element.xpath("//*[local-name()='GrpHdr']")
                if grp_hdr_nodes:
                    msg_id_nodes = grp_hdr_nodes[0].xpath("*[local-name()='MsgId']")
                    if msg_id_nodes:
                        msg_id_val = (msg_id_nodes[0].text or "").strip()
                        if biz_msg_idr_val != msg_id_val:
                            report.add_issue(ValidationIssue(
                                severity="ERROR",
                                code="MESSAGE_ID_MISMATCH",
                                path="//GrpHdr/MsgId",
                                message=f"Group Header Message ID <MsgId> '{msg_id_val}' must equal Business Message Identifier <BizMsgIdr> '{biz_msg_idr_val}' in BAH.",
                                line=msg_id_nodes[0].sourceline or grp_hdr_nodes[0].sourceline or 1,
                                fix="Set <MsgId> to match <BizMsgIdr>."
                            ))

            # Rule 3: BIC Matching Rules Tightened
            from_bic_nodes = app_hdr[0].xpath("*[local-name()='Fr']//*[local-name()='BICFI']")
            to_bic_nodes = app_hdr[0].xpath("*[local-name()='To']//*[local-name()='BICFI']")
            cpy_dplct_nodes = app_hdr[0].xpath("*[local-name()='CpyDplct']")
            
            from_bic = (from_bic_nodes[0].text or "").strip() if from_bic_nodes else None
            to_bic = (to_bic_nodes[0].text or "").strip() if to_bic_nodes else None
            has_cpy_dplct = bool(cpy_dplct_nodes)
            
            tx_blocks = root_element.xpath("//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']")
            for tx in tx_blocks:
                tx_name = tx.tag.split('}')[-1] if isinstance(tx.tag, str) else "Transaction"
                
                # Rule 3.1: BAH From BIC = InstgAgt BIC (Always required)
                instg_bic_nodes = tx.xpath("*[local-name()='InstgAgt']//*[local-name()='BICFI']")
                if from_bic and instg_bic_nodes:
                    instg_bic = (instg_bic_nodes[0].text or "").strip()
                    def normalize_bic(b):
                        b_up = b.upper()
                        return b_up + "XXX" if len(b_up) == 8 else b_up
                    if normalize_bic(from_bic) != normalize_bic(instg_bic):
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="FROM_BIC_MISMATCH",
                            path=f"//{tx_name}/InstgAgt",
                            message=f"Instructing Agent BIC '{instg_bic}' must match BAH From BIC '{from_bic}'.",
                            line=instg_bic_nodes[0].sourceline or tx.sourceline or 1,
                            fix=f"Change Instructing Agent BIC to match BAH From BIC '{from_bic}'."
                        ))
                        
                # Rule 3.2: BAH To BIC = InstdAgt BIC (Required if CopyDuplicate is absent)
                if not has_cpy_dplct:
                    instd_bic_nodes = tx.xpath("*[local-name()='InstdAgt']//*[local-name()='BICFI']")
                    if to_bic and instd_bic_nodes:
                        instd_bic = (instd_bic_nodes[0].text or "").strip()
                        def normalize_bic(b):
                            b_up = b.upper()
                            return b_up + "XXX" if len(b_up) == 8 else b_up
                        if normalize_bic(to_bic) != normalize_bic(instd_bic):
                            report.add_issue(ValidationIssue(
                                severity="ERROR",
                                code="TO_BIC_MISMATCH",
                                path=f"//{tx_name}/InstdAgt",
                                message=f"Instructed Agent BIC '{instd_bic}' must match BAH To BIC '{to_bic}'.",
                                line=instd_bic_nodes[0].sourceline or tx.sourceline or 1,
                                fix=f"Change Instructed Agent BIC to match BAH To BIC '{to_bic}'."
                            ))

        # Rule 4: Instructed Amount Becomes Mandatory
        tx_blocks = root_element.xpath("//*[local-name()='CdtTrfTxInf' or local-name()='DrctDbtTxInf' or local-name()='TxInf']")
        for tx in tx_blocks:
            tx_name = tx.tag.split('}')[-1] if isinstance(tx.tag, str) else "Transaction"
            instd_amt_nodes = tx.xpath("*[local-name()='InstdAmt']")
            if not instd_amt_nodes or not (instd_amt_nodes[0].text or "").strip():
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MANDATORY_INSTD_AMT_MISSING",
                    path=f"//{tx_name}/InstdAmt",
                    message=f"Mandatory Instructed Amount <InstdAmt> is missing inside '{tx_name}'.",
                    line=tx.sourceline or 1,
                    fix="Add <InstdAmt> element with amount and Ccy attribute."
                ))

        # Rule 8: EndToEndId NOTPROVIDED Handling
        for tx in tx_blocks:
            tx_name = tx.tag.split('}')[-1] if isinstance(tx.tag, str) else "Transaction"
            e2e_nodes = tx.xpath("*[local-name()='PmtId']/*[local-name()='EndToEndId']")
            if e2e_nodes:
                e2e_val = (e2e_nodes[0].text or "").strip()
                if not e2e_val:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="INVALID_END_TO_END_ID",
                        path=f"//{tx_name}/PmtId/EndToEndId",
                        message="End-to-End Identification <EndToEndId> cannot be empty. If not provided, 'NOTPROVIDED' must be used.",
                        line=e2e_nodes[0].sourceline or tx.sourceline or 1,
                        fix="Set <EndToEndId> to 'NOTPROVIDED' or provide a valid identification string."
                    ))

        # Rule 9: GPI Service Level Restriction
        svclvl_cd_nodes = root_element.xpath("//*[local-name()='PmtTpInf']/*[local-name()='SvcLvl']/*[local-name()='Cd']")
        for cd_node in svclvl_cd_nodes:
            cd_val = (cd_node.text or "").strip()
            if cd_val in ["G002", "G003", "G004", "G005", "G006", "G007", "G009"]:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="INVALID_GPI_SERVICE_LEVEL",
                    path="//PmtTpInf/SvcLvl/Cd",
                    message=f"GPI Service Level code '{cd_val}' is not allowed in SR2026. Only 'G001' is allowed.",
                    line=cd_node.sourceline or 1,
                    fix="Use 'G001' or a valid non-GPI service level code."
                ))

        # Rule 10: Date Datatype Restrictions (CBPR_Date)
        for elem in root_element.iter():
            if not isinstance(elem.tag, str):
                continue
            tag = elem.tag.split('}')[-1]
            is_date_field = (tag.endswith("Dt") or tag == "Dt") and tag != "CreDt" and not tag.endswith("DtTm")
            if is_date_field:
                val = (elem.text or "").strip()
                if val:
                    if re.search(r'(Z|[+-]\d{2}:\d{2})$', val):
                        report.add_issue(ValidationIssue(
                            severity="ERROR",
                            code="INVALID_DATE_TIMEZONE",
                            path=f"//{tag}",
                            message=f"Date field <{tag}> contains timezone offset '{val}', which is not allowed in SR2026. Use YYYY-MM-DD format without timezone.",
                            line=elem.sourceline or 1,
                            fix=f"Remove timezone offset (e.g. change '{val}' to '{val[:10]}')."
                        ))

        # Rule 11: Proxy Type Fields Become Mandatory
        prxy_nodes = root_element.xpath("//*[local-name()='Prxy']")
        for prxy in prxy_nodes:
            tp_nodes = prxy.xpath("*[local-name()='Tp']")
            if not tp_nodes or not (tp_nodes[0].xpath("*") or (tp_nodes[0].text or "").strip()):
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="MANDATORY_PROXY_TYPE_MISSING",
                    path="//Prxy/Tp",
                    message="Proxy Type <Tp> is mandatory inside <Prxy> when Proxy is present in SR2026.",
                    line=prxy.sourceline or 1,
                    fix="Add <Tp> element (with <Cd> or <Prtry>) inside the <Prxy> container block."
                ))

        # Rule 12: Structured Remittance Rule (Maximum 9000 business characters)
        strd_nodes = root_element.xpath("//*[local-name()='Strd']")
        for strd in strd_nodes:
            total_len = 0
            for desc in strd.iter():
                if desc.text and not list(desc):
                    total_len += len(desc.text.strip())
            if total_len > 9000:
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="STRD_LENGTH_EXCEEDED",
                    path="//RmtInf/Strd",
                    message=f"Structured Remittance block character count ({total_len}) exceeds the maximum limit of 9000 business characters.",
                    line=strd.sourceline or 1,
                    fix="Reduce the content inside the <Strd> block to be within 9000 characters."
                ))

