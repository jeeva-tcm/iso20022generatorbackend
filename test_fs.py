import json
from lxml import etree
import sys
sys.path.append('.')
from app.services.fix_suggester import FixSuggester

xml = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
    <FIToFICstmrCdtTrf>
        <CdtTrfTxInf>
            <PmtId><InstrId>1</InstrId></PmtId>
        </CdtTrfTxInf>
        <CdtTrfTxInf>
            <PmtId><InstrId>2</InstrId></PmtId>
        </CdtTrfTxInf>
    </FIToFICstmrCdtTrf>
</Document>"""

root = etree.fromstring(xml.encode())
fs = FixSuggester()

paths, remaining = fs._find_deepest_ancestor(root, ['Document', 'FIToFICstmrCdtTrf', 'CdtTrfTxInf[2]', 'Dbtr', 'PstlAdr'])
print(etree.tostring(paths))
print(remaining)
