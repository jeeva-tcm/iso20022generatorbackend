from lxml import etree
from app.sr2026.validation.validators.models import ValidationIssue, ValidationReport

class GuidelineValidator:
    @staticmethod
    def validate(root_element: etree._Element, report: ValidationReport):
        # 1. Enforce that known containers are not empty
        # These are allowed to be empty by base XSD but forbidden in CBPR+
        EMPTY_NOT_ALLOWED = {
            'FinInstnId': "BICFI, ClrSysMmbId, LEI, Nm, or Othr",
            'FIId': "a FinInstnId block with at least one identifier",
            'ClrSysMmbId': "MmbId",
            'OrgId': "AnyBIC, LEI, or Othr/Id",
            'PrvtId': "DtAndPlcOfBirth or Othr/Id",
            'SchmeNm': "<Cd> or <Prtry>",
            'Othr': "an identifier value <Id>",
            'ClrSys': "<Cd> or <Prtry>",
            'PstlAdr': "Ctry, TwnNm, StrtNm, BldgNb, PstCd, or AdrLine",
            'PmtId': "EndToEndId (and ideally InstrId, TxId, UETR)",
            'GrpHdr': "MsgId, CreDtTm, NbOfTxs and SttlmInf",
            'SttlmInf': "SttlmMtd",
            'PmtTpInf': "SvcLvl, LclInstrm, CtgyPurp, InstrPrty, or ClrChanl",
            'SvcLvl': "<Cd> or <Prtry>",
            'LclInstrm': "<Cd> or <Prtry>",
            'CtgyPurp': "<Cd> or <Prtry>",
            'Purp': "<Cd> or <Prtry>",
            'SttlmTmIndctn': "DbtDtTm or CdtDtTm",
            'SttlmTmReq': "CLSTm, TillTm, FrTm, or RjctTm",
            'RmtInf': "<Ustrd> or <Strd>",
            'Strd': "RfrdDocInf, RfrdDocAmt, CdtrRefInf, Invcr, Invcee, TaxRmt, or AddtlRmtInf",
            'CdtrRefInf': "Tp or Ref",
            'Tax': "Cdtr, Dbtr, AdmstnZn, RefNb, Mtd, TtlTaxblBaseAmt, TtlTaxAmt, Dt, SeqNb, or Rcrd",
            'TaxRcrd': "Tp, Ctgy, CtgyDtls, DbtrSts, CertId, FrmsCd, Prd, TaxAmt, or AddtlInf",
            'Chrgs': "Amt and Agt",
            'ChrgsInf': "Amt and Agt",
            'InstrForCdtrAgt': "Cd or InstrInf",
            'InstrForNxtAgt': "Cd or InstrInf",
            'AcctId': "<IBAN> or <Othr> with an identifier"
        }

        def get_clean_tag(tag):
            return tag.split('}')[-1] if isinstance(tag, str) and '}' in tag else tag

        def is_effectively_empty(elem):
            for child in elem:
                if isinstance(child.tag, str):
                    return False
            text = (elem.text or "").strip()
            return not text

        for elem in root_element.iter():
            if not isinstance(elem.tag, str):
                continue
            name = get_clean_tag(elem.tag)
            
            if name in EMPTY_NOT_ALLOWED and is_effectively_empty(elem):
                report.add_issue(ValidationIssue(
                    severity="ERROR",
                    code="EMPTY_REQUIRED_CONTAINER",
                    path=f"//{name}",
                    message=f"Element <{name}> is present in the message but is empty.",
                    line=elem.sourceline or 1,
                    fix=f"Provide required details inside <{name}>: {EMPTY_NOT_ALLOWED[name]}."
                ))

        # 2. AppHdr MsgDefIdr vs Document namespace payload check
        app_hdrs = root_element.xpath("//*[local-name()='AppHdr']")
        documents = root_element.xpath("//*[local-name()='Document']")
        if app_hdrs and documents:
            msg_def_idr_nodes = app_hdrs[0].xpath(".//*[local-name()='MsgDefIdr']")
            if msg_def_idr_nodes:
                msg_def_idr = (msg_def_idr_nodes[0].text or "").strip()
                doc_ns = etree.QName(documents[0]).namespace or ""
                doc_msg_id = doc_ns.split(":")[-1] if ":" in doc_ns else doc_ns
                
                if msg_def_idr and doc_msg_id and msg_def_idr != doc_msg_id:
                    report.add_issue(ValidationIssue(
                        severity="ERROR",
                        code="HEAD001_MSGDEFIDR_MISMATCH",
                        path="//AppHdr/MsgDefIdr",
                        message=f"AppHdr.MsgDefIdr '{msg_def_idr}' does not match the Document namespace '{doc_msg_id}'.",
                        line=msg_def_idr_nodes[0].sourceline or 1,
                        fix=f"Update MsgDefIdr in the AppHdr or update the Document namespace to reference the same identifier '{doc_msg_id}'."
                    ))
