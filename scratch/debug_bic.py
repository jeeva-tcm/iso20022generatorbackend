# -*- coding: utf-8 -*-
"""Debug why dependency-partner harvesting doesn't kick in for Fr.BICFI."""
import sys, os, json
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester, _kb_get
from lxml import etree

XML = """<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr><FIId><FinInstnId><BICFI>CREDITMM</BICFI></FinInstnId></FIId></Fr>
\t\t<To><FIId><FinInstnId><BICFI>HSBCITMMXXX</BICFI></FinInstnId></FIId></To>
\t</AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
\t\t<FIToFICstmrCdtTrf><CdtTrfTxInf>
\t\t\t<InstgAgt><FinInstnId><BICFI>CREDITMMXXX</BICFI></FinInstnId></InstgAgt>
\t\t\t<InstdAgt><FinInstnId><BICFI>HSBCITMMXXX</BICFI></FinInstnId></InstdAgt>
\t\t</CdtTrfTxInf></FIToFICstmrCdtTrf>
\t</Document>
</BusMsgEnvlp>"""

# Inspect the equals dependencies
deps = _kb_get("dependencies.equals", [])
print("Equals dependencies:")
for d in deps:
    print(f"  - {d.get('id')}: fields = {d.get('fields')}")

# Parse XML and check matching
root = etree.fromstring(XML.encode("utf-8"))
print("\nDocument tree element xpaths (only BICFI):")
for el in root.iter():
    if isinstance(el.tag, str) and etree.QName(el.tag).localname == "BICFI":
        xpath = fix_suggester._xpath_of(el)
        print(f"  {xpath} = {el.text!r}")

# Find the Fr/BICFI element
fr_bicfi = None
for el in root.iter():
    if isinstance(el.tag, str) and etree.QName(el.tag).localname == "BICFI":
        xpath = fix_suggester._xpath_of(el)
        if "/Fr/" in xpath:
            fr_bicfi = el
            print(f"\nFr.BICFI found at xpath: {xpath}")
            break

# Try direct harvest
from app.services.fix_suggester import _kb_field_constraint
constraint = _kb_field_constraint("BICFI")
result = fix_suggester._harvest_dependency_partner(
    root, fix_suggester._xpath_of(fr_bicfi), "BICFI", constraint
)
print(f"\n_harvest_dependency_partner result: {result!r}")

# Now try the real suggest call
issue = {
    "path": "AppHdr.Fr.FIId.FinInstnId.BICFI",
    "code": "CBPR_R2",
    "message": "AppHdr <Fr> BICFI must match <InstgAgt> BICFI",
    "fix_suggestion": "Align AppHdr <Fr> BICFI with CdtTrfTxInf <InstgAgt> BICFI."
}
sugg = fix_suggester.suggest(XML, issue)
print(f"\nFinal suggest() result: conf={sugg.confidence}")
import re
m = re.search(r"<BICFI[^>]*>([^<]+)</BICFI>", sugg.fragment_xml or "")
print(f"New BICFI: {m.group(1) if m else 'NOT FOUND'}")
