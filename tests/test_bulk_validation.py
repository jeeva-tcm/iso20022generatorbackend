"""Regression tests — bulk BulkMessages validation.

Asserts that every <Document> in a <BulkMessages> file is validated
independently, not just the first one.

Run with:
    cd iso20022generatorbackend
    $env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/test_bulk_validation.py -v
"""
import asyncio
import pytest
from app.main import validator

PACS010_NS = "urn:iso:std:iso:20022:tech:xsd:pacs.010.001.03"
HEAD_NS    = "urn:iso:std:iso:20022:tech:xsd:head.001.001.02"

# ── helpers ──────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _validate(xml: str):
    return _run(validator.validate(xml, mode="Full 1-3", message_type="Auto-detect"))


def _codes(report) -> set:
    return {i.get("code") for i in (report.to_dict().get("details") or [])}


def _errors(report) -> list:
    return [i for i in (report.to_dict().get("details") or []) if i.get("severity") == "ERROR"]

# ── bulk2 regression ─────────────────────────────────────────────────────────

BULK2_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<BulkMessages>
    <BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
        <AppHdr xmlns="{head_ns}">
            <Fr><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></Fr>
            <To><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></To>
            <BizMsgIdr>MSG-001</BizMsgIdr>
            <MsgDefIdr>pacs.010.001.03</MsgDefIdr>
            <BizSvc>swift.cbprplus.02</BizSvc>
            <CreDt>2026-06-12T04:59:31+00:00</CreDt>
        </AppHdr>
        <Document xmlns="{pacs010_ns}">
            <FIDrctDbt>
                <GrpHdr><MsgId>MSG-001</MsgId><CreDtTm>2026-06-12T10:00:00+00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
                <CdtInstr>
                    <CdtId>CDT-001</CdtId>
                    <InstgAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></InstgAgt>
                    <InstdAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></InstdAgt>
                    <CdtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></CdtrAgt>
                    <Cdtr><FinInstnId><BICFI>NWBKGB2LXXX</BICFI></FinInstnId></Cdtr>
                    <CdtrAcct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></CdtrAcct>
                    <DrctDbtTxInf>
                        <PmtId>
                            <InstrId>INSTR-001</InstrId>
                            <EndToEndId>E2E-001</EndToEndId>
                            <TxId>TXN-001</TxId>
                            <UETR>19df0b83-c8ce-4fa6-86d4-afad734faed2</UETR>
                        </PmtId>
                        <IntrBkSttlmAmt Ccy="GBP">100.00</IntrBkSttlmAmt>
                        <IntrBkSttlmDt>2026-06-13</IntrBkSttlmDt>
                        <Dbtr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Dbtr>
                        <DbtrAcct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></DbtrAcct>
                        <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
                    </DrctDbtTxInf>
                </CdtInstr>
            </FIDrctDbt>
        </Document>
    </BusMsgEnvlp>
    <!-- 2nd message: bare Document, no namespace, SttlmInf misplaced inside CdtInstr -->
    <Document>
        <FIDrctDbt>
            <GrpHdr><MsgId>MSG-002</MsgId><CreDtTm>2026-06-12T10:00:00+00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
            <CdtInstr>
                <CdtId>CDT-002</CdtId>
                <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>
                <Cdtr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Cdtr>
                <DrctDbtTxInf>
                    <IntrBkSttlmAmt Ccy="GBP">1000.00</IntrBkSttlmAmt>
                    <Dbtr><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></Dbtr>
                    <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
                    <PmtId>
                        <InstrId>INSTR-002</InstrId>
                        <EndToEndId>E2E-002</EndToEndId>
                        <UETR>9a145434-d657-47b8-af2e-97f95d03c6b6</UETR>
                    </PmtId>
                </DrctDbtTxInf>
            </CdtInstr>
        </FIDrctDbt>
    </Document>
</BulkMessages>
""".format(head_ns=HEAD_NS, pacs010_ns=PACS010_NS)


def test_bulk_second_doc_namespace_missing():
    """DOC_NAMESPACE_MISSING must be flagged for the bare 2nd Document."""
    report = _validate(BULK2_XML)
    codes = _codes(report)
    assert "DOC_NAMESPACE_MISSING" in codes, f"expected DOC_NAMESPACE_MISSING, got codes={codes}"


def test_bulk_second_doc_sttlminf_misplaced():
    """SttlmInf misplaced inside CdtInstr must be flagged via SCHEMA_VAL."""
    report = _validate(BULK2_XML)
    codes = _codes(report)
    assert "SCHEMA_VAL" in codes, f"expected SCHEMA_VAL (SttlmInf not expected), got codes={codes}"


def test_bulk_first_doc_is_clean():
    """First (enveloped, namespaced) Document must not generate its own errors."""
    report = _validate(BULK2_XML)
    errs = _errors(report)
    # Errors must all be attributable to the 2nd doc (line 95+) or bare-doc rules.
    # First doc starts at line 4 — no error should reference lines before ~90.
    first_doc_errs = [e for e in errs if isinstance(e.get("line"), int) and e["line"] < 90]
    assert not first_doc_errs, f"unexpected errors on first Document: {first_doc_errs}"
