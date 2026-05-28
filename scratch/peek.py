# -*- coding: utf-8 -*-
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
from app.services.fix_suggester import fix_suggester

XML = (
    '<?xml version="1.0"?>'
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf><CdtTrfTxInf>'
    '<DbtrAgt><FinInstnId><BICFI>BARCGB22XXX</BICFI></FinInstnId></DbtrAgt>'
    '</CdtTrfTxInf></FIToFICstmrCdtTrf></Document>'
)
r = fix_suggester.suggest(XML, {
    'path': 'Document.FIToFICstmrCdtTrf.CdtTrfTxInf.CdtrAgt.FinInstnId.BICFI',
    'code': 'X', 'message': 'CdtrAgt BICFI missing',
    'fix_suggestion': ''
})
print(r.fragment_xml)
