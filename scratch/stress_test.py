# -*- coding: utf-8 -*-
"""Stress test - apply real fixes end-to-end and verify the output XML actually validates."""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester, FixApplyError
from lxml import etree

# Minimal broken pacs.008 - missing several mandatory items
BROKEN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf>
    <GrpHdr>
      <MsgId>MSG-001</MsgId>
      <CreDtTm>2026-05-27T10:00:00Z</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
      <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>
    </GrpHdr>
    <CdtTrfTxInf>
      <PmtId>
        <InstrId>I1</InstrId>
        <EndToEndId>E1</EndToEndId>
      </PmtId>
      <IntrBkSttlmAmt Ccy="USD">1000</IntrBkSttlmAmt>
      <ChrgBr>XXXX</ChrgBr>
      <Dbtr><Nm>John Doe</Nm></Dbtr>
      <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
      <CdtrAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></CdtrAgt>
      <Cdtr><Nm>Jane Smith</Nm></Cdtr>
    </CdtTrfTxInf>
  </FIToFICstmrCdtTrf>
</Document>"""

ISSUES = [
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR", "code": "PACS008_UETR_REQUIRED",
     "message": "UETR is mandatory", "fix_suggestion": "Add UUID v4"},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.ChrgBr", "code": "L3_CHRGBR_001",
     "message": "Invalid Charge Bearer code 'XXXX'", "fix_suggestion": "Valid codes: SLEV"},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.DbtrAcct", "code": "PACS008_DBTRACCT_REQUIRED",
     "message": "DbtrAcct mandatory", "fix_suggestion": ""},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.CdtrAcct", "code": "PACS008_CDTRACCT_REQUIRED",
     "message": "CdtrAcct mandatory", "fix_suggestion": ""},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmDt", "code": "PACS008_INTRBKSTTLMDT",
     "message": "IntrBkSttlmDt is mandatory", "fix_suggestion": ""},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.InstgAgt", "code": "PACS009_003",
     "message": "InstgAgt missing", "fix_suggestion": ""},
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.PstlAdr.Ctry", "code": "L3_COUNTRY_001",
     "message": "Country missing", "fix_suggestion": ""},
]

print("=" * 100)
print("STRESS TEST: apply each fix and check XML validity")
print("=" * 100)

failed = 0
xml = BROKEN_XML
for i, issue in enumerate(ISSUES, 1):
    print(f"\n[{i}] {issue['path']}")
    print(f"    code: {issue['code']}")
    print(f"    msg : {issue['message']}")

    # Step 1: get suggestion
    sugg = fix_suggester.suggest(xml, issue)
    print(f"    -> conf: {sugg.confidence}")
    print(f"    -> xpath: {sugg.xpath}")
    if not sugg.fragment_xml:
        print(f"    -> EMPTY FRAGMENT, skipping")
        failed += 1
        continue

    frag_preview = sugg.fragment_xml.replace('\n','').replace('  ','')[:180]
    print(f"    -> fragment: {frag_preview}")

    # Step 2: apply the suggestion
    try:
        new_xml = fix_suggester.apply(xml, sugg.xpath, sugg.fragment_xml)
        # Verify still valid XML
        try:
            etree.fromstring(new_xml.encode("utf-8"))
            print(f"    -> APPLY OK ({len(new_xml)} chars)")
            xml = new_xml  # chain fixes
        except etree.XMLSyntaxError as e:
            print(f"    -> APPLIED BUT INVALID XML: {e}")
            failed += 1
    except FixApplyError as e:
        print(f"    -> APPLY FAILED: {e}")
        failed += 1
    except Exception as e:
        print(f"    -> CRASH: {type(e).__name__}: {e}")
        failed += 1

print()
print("=" * 100)
print(f"Result: {len(ISSUES)-failed}/{len(ISSUES)} fixes applied cleanly | {failed} failures")
print("=" * 100)
print("\nFinal XML:")
print(xml[:2000])
