# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services.fix_suggester import fix_suggester

XML = (
    '<?xml version="1.0"?>'
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf><CdtTrfTxInf>'
    '<PmtId><InstrId>I1</InstrId></PmtId>'
    '</CdtTrfTxInf></FIToFICstmrCdtTrf></Document>'
)
r = fix_suggester.suggest(XML, {
    "path": "AppHdr.MsgDefIdr",
    "code": "MSGDEFIDR",
    "message": "MsgDefIdr missing",
    "fix_suggestion": "",
})
print("conf:", r.confidence)
print(r.fragment_xml)
