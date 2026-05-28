"""Comprehensive fix_suggester diagnostic — covers all paths that could return unavailable."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester

XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf>
    <GrpHdr>
      <MsgId>MSG-001</MsgId>
      <CreDtTm>2025-01-15T10:00:00Z</CreDtTm>
      <NbOfTxs>1</NbOfTxs>
      <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>
    </GrpHdr>
    <CdtTrfTxInf>
      <PmtId>
        <InstrId>INSTR-001</InstrId>
        <EndToEndId>E2E-001</EndToEndId>
      </PmtId>
      <IntrBkSttlmAmt Ccy="USD">1000.00</IntrBkSttlmAmt>
      <ChrgBr>XXXX</ChrgBr>
      <Dbtr><Nm>John Doe</Nm></Dbtr>
      <DbtrAcct><Id><IBAN>GB29NWBK60161331926819</IBAN></Id></DbtrAcct>
      <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
      <CdtrAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></CdtrAgt>
      <Cdtr><Nm>Jane Smith</Nm></Cdtr>
    </CdtTrfTxInf>
  </FIToFICstmrCdtTrf>
</Document>"""

ISSUES = [
    # 1. Missing leaf inside existing parent
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR",
     "code": "PACS008_UETR_REQUIRED",
     "message": "UETR is mandatory.",
     "fix_suggestion": "Add a valid UUID v4 (e.g., 4a1a0945-5772-409a-83ba-240e666e0267)."},

    # 2. Wrong value in existing leaf (ChrgBr = XXXX)
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.ChrgBr",
     "code": "L3_CHRGBR_001",
     "message": "Invalid Charge Bearer code 'XXXX'.",
     "fix_suggestion": "Valid codes: SLEV, SHAR, CRED, DEBT."},

    # 3. Missing leaf inside missing parent (PstlAdr doesn't exist in Dbtr)
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.PstlAdr.Ctry",
     "code": "L3_COUNTRY_001",
     "message": "Missing country code in Postal Address.",
     "fix_suggestion": "Add a valid ISO 3166-1 Alpha-2 country code like US, GB."},

    # 4. Missing complex block entirely (InstgAgt)
    {"path": "Document.FIToFICstmrCdtTrf.GrpHdr.InstgAgt",
     "code": "PACS008_INSTGAGT",
     "message": "Instructing Agent is mandatory.",
     "fix_suggestion": "Add <InstgAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></InstgAgt>."},

    # 5. Missing leaf 2 levels deep from existing anchor
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Cdtr.PstlAdr.AdrLine",
     "code": "E001",
     "message": "Address line is required.",
     "fix_suggestion": "Add <AdrLine> with a street address."},

    # 6. Codelist fix — country
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.PstlAdr.Ctry",
     "code": "L3_COUNTRY_001",
     "message": "Invalid country code 'ZZ' in field Ctry.",
     "fix_suggestion": "Use a 2-letter code like 'US', 'GB', 'DE'."},
]

print(f"{'PATH':<55} {'CONF':<12} {'RESULT'}")
print("-" * 120)
unavail = 0
for issue in ISSUES:
    r = fix_suggester.suggest(XML, issue)
    short_path = issue['path'].split('.')[-3:]
    short_path = '.'.join(short_path)
    if r.confidence == 'unavailable':
        unavail += 1
        result = "❌ UNAVAILABLE"
    else:
        frag = r.fragment_xml.replace('\n','').replace('  ','')
        result = f"✅ {frag[:80]}"
    print(f"{short_path:<55} {r.confidence:<12} {result}")

print()
print(f"Result: {len(ISSUES)-unavail}/{len(ISSUES)} fixed  |  {unavail} unavailable")
