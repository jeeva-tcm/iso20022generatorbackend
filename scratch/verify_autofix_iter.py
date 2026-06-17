import sys, asyncio
sys.path.insert(0, ".")
from app.services.fix_suggester import fix_suggester
from app.services.validator import ISOValidator

xml = '''<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
    <AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
        <Fr><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></Fr>
        <To><FIId><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></FIId></To>
        <BizMsgIdr/>
        <MsgDefIdr>pacs.009.001.08</MsgDefIdr>
        <BizSvc>swift.cbprplus.adv.04</BizSvc>
        <CreDt>2026-06-16T10:00:00+00:00</CreDt>
    </AppHdr>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08">
        <FICdtTrf>
            <GrpHdr>
                <MsgId>MSG5BIGAMWXDFOO32W5</MsgId>
                <CreDtTm>2026-06-16T10:00:00+00:00</CreDtTm>
                <NbOfTxs>1</NbOfTxs>
                <SttlmInf><SttlmMtd>COVE</SttlmMtd>
                    <InstdRmbrsmntAgt><FinInstnId><BICFI>RABOSESSXXX</BICFI></FinInstnId></InstdRmbrsmntAgt>
                </SttlmInf>
            </GrpHdr>
            <CdtTrfTxInf>
                <PmtId><InstrId>INSTRGFR9HX1346S</InstrId><EndToEndId>E2EPS11I0PQVZYC32J9</EndToEndId>
                    <TxId>TXV1W78R40F0X00GPP</TxId><UETR>98d87d91-18f2-4b26-b746-f0b54bb32084</UETR></PmtId>
                <PmtTpInf><SvcLvl><Cd>URGP</Cd></SvcLvl><LclInstrm><Prtry>ADV</Prtry></LclInstrm></PmtTpInf>
                <IntrBkSttlmAmt Ccy="SEK">234878.37</IntrBkSttlmAmt>
                <IntrBkSttlmDt>2026-06-17</IntrBkSttlmDt>
                <InstgAgt><FinInstnId><BICFI>DBSSSESSXXX</BICFI></FinInstnId></InstgAgt>
                <InstdAgt><FinInstnId><BICFI>NWBKSESSXXX</BICFI></FinInstnId></InstdAgt>
                <Dbtr><FinInstnId><BICFI>CACRSESSXXX</BICFI></FinInstnId></Dbtr>
                <DbtrAcct><Id><IBAN>SE7325073674878071707939</IBAN></Id></DbtrAcct>
                <DbtrAgt><FinInstnId><BICFI>UBSWSESSXXX</BICFI></FinInstnId></DbtrAgt>
                <CdtrAgt><FinInstnId><BICFI>BNPPSESSXXX</BICFI></FinInstnId></CdtrAgt>
                <Cdtr><FinInstnId><BICFI>SOGESESSXXX</BICFI></FinInstnId></Cdtr>
                <CdtrAcct><Id><IBAN>SE9699246684848024492892</IBAN></Id></CdtrAcct>
            </CdtTrfTxInf>
        </FICdtTrf>
    </Document>
</BusMsgEnvlp>'''

validator = ISOValidator()

async def main():
    report = await validator.validate(xml, mode="Full 1-3", message_type="pacs.009.001.08_ADV")
    rd = report.to_dict()
    errors = [d for d in rd.get("details", []) if d.get("severity") in ("ERROR", "CRITICAL")]
    print("initial errors:", len(errors))
    sugs = fix_suggester.suggest_batch(xml, errors[:20])
    fixes = [{"xpath": s.xpath, "fragment_xml": s.fragment_xml} for s in sugs
             if s.confidence in ("high", "low") and s.fragment_xml and s.fragment_xml != s.original_fragment]
    new_xml = fix_suggester.apply_batch(xml, fixes)
    print("BizMsgIdr in new_xml:", "<BizMsgIdr>" in new_xml, "<BizMsgIdr/>" in new_xml)
    import re
    m = re.search(r"<BizMsgIdr[^>]*>.*?</BizMsgIdr>|<BizMsgIdr/>", new_xml)
    print("BizMsgIdr tag:", m.group(0) if m else None)

asyncio.run(main())
