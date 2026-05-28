# -*- coding: utf-8 -*-
"""Final test: cover all cases that previously returned 'unavailable'."""
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

EDGE_ISSUES = [
    # Previously unavailable: path="/" with message mentioning a tag
    {"path": "/", "code": "R1", "message": "Issue with <ChrgBr>",
     "fix_suggestion": "Set ChrgBr to SLEV"},

    # Previously unavailable: empty path
    {"path": "", "code": "R2", "message": "MsgId is too long",
     "fix_suggestion": "Use a shorter MsgId"},

    # Previously unavailable: path with no anchor in document
    {"path": "AppHdr.Fr.FIId.FinInstnId.BICFI", "code": "R3",
     "message": "Header BIC mismatch", "fix_suggestion": "Match to InstgAgt"},

    # Previously unavailable: nested missing with no fix_hint
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.Dbtr.PstlAdr.Ctry",
     "code": "R4", "message": "Missing country", "fix_suggestion": ""},

    # Previously unavailable: rule-level path that doesn't exist
    {"path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.RmtInf.Strd.RfrdDocInf.Tp.CdOrPrtry.Cd",
     "code": "R5", "message": "Remittance doc code invalid",
     "fix_suggestion": "Use CINV or other valid code"},
]

print(f"{'CASE':<70} {'CONF'}")
print("-" * 90)
unavail = 0
for issue in EDGE_ISSUES:
    r = fix_suggester.suggest(XML_DOC, issue)
    label = f"path='{issue['path']}' code={issue['code']}"[:68]
    if r.confidence == 'unavailable':
        unavail += 1
    print(f"  {label:<68} {r.confidence}")

print()
print(f"Result: {len(EDGE_ISSUES)-unavail}/{len(EDGE_ISSUES)} resolved, {unavail} unavailable")
