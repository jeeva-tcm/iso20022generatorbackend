# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester

XML_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf>'
    '<GrpHdr><MsgId>M1</MsgId><CreDtTm>2025-01-15T10:00:00Z</CreDtTm>'
    '<NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf></GrpHdr>'
    '<CdtTrfTxInf>'
    '<PmtId><InstrId>I1</InstrId><EndToEndId>E1</EndToEndId></PmtId>'
    '<IntrBkSttlmAmt Ccy="USD">1000</IntrBkSttlmAmt>'
    '<ChrgBr>XXXX</ChrgBr>'
    '<Dbtr><Nm>John</Nm></Dbtr>'
    '<Cdtr><Nm>Jane</Nm></Cdtr>'
    '</CdtTrfTxInf>'
    '</FIToFICstmrCdtTrf>'
    '</Document>'
)

ISSUES = [
    # 1. Missing leaf inside existing parent
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR",
     "code": "UETR", "message": "UETR missing",
     "fix_suggestion": "Add UUID e.g. 4a1a0945-5772-409a-83ba-240e666e0267"},

    # 2. Wrong value in existing leaf
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.ChrgBr",
     "code": "CHRGBR", "message": "Invalid Charge Bearer code XXXX",
     "fix_suggestion": "Valid codes: SLEV, SHAR, CRED, DEBT"},

    # 3. Missing leaf inside missing intermediate parent (PstlAdr not in Dbtr)
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.PstlAdr.Ctry",
     "code": "CTRY", "message": "Missing country in postal address",
     "fix_suggestion": "Use US or GB"},

    # 4. Missing complex block (InstgAgt doesn't exist in GrpHdr)
    {"path": "Document.FIToFICstmrCdtTrf.GrpHdr.InstgAgt",
     "code": "INSTGAGT", "message": "Instructing agent is mandatory",
     "fix_suggestion": "Add InstgAgt with BICFI"},

    # 5. Missing leaf inside missing parent (Cdtr has no PstlAdr)
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Cdtr.PstlAdr.AdrLine",
     "code": "ADRLINE", "message": "Address line missing",
     "fix_suggestion": "Add AdrLine with street address"},

    # 6. Path doesn't exist at all (completely new element chain)
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.DbtrAgt.FinInstnId.BICFI",
     "code": "BICFI", "message": "Debtor agent BIC missing",
     "fix_suggestion": "Add BICFI like DEUTDEFFXXX"},
]

print(f"{'LAST TAG':<22} {'CONF':<14} {'RESULT'}")
print("-" * 100)
unavail = 0
for issue in ISSUES:
    r = fix_suggester.suggest(XML_DOC, issue)
    tag = issue['path'].split('.')[-1]
    if r.confidence == 'unavailable':
        unavail += 1
        result = "UNAVAILABLE"
    else:
        frag = (r.fragment_xml or '').replace('\n', '').replace('  ', '')[:80]
        result = frag
    print(f"  {tag:<20} {r.confidence:<14} {result}")

print()
print(f"Result: {len(ISSUES)-unavail}/{len(ISSUES)} fixed, {unavail} unavailable")
