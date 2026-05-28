# -*- coding: utf-8 -*-
"""Test against the user's real broken XML - reproduces the actual issues."""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester
from lxml import etree

# The user's actual broken XML - has UETR="UETR", Ccy="EU", Amt="EU", BIC mismatch
USER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
\t<AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
\t\t<Fr>
\t\t\t<FIId>
\t\t\t\t<FinInstnId>
\t\t\t\t\t<BICFI>CREDITMM</BICFI></FinInstnId>
\t\t\t</FIId>
\t\t</Fr>
\t\t<To>
\t\t\t<FIId>
\t\t\t\t<FinInstnId>
\t\t\t\t\t<BICFI>HSBCITMMXXX</BICFI>
\t\t\t\t</FinInstnId>
\t\t\t</FIId>
\t\t</To>
\t\t<BizMsgIdr>MSGEP1TTQKJE0C2HDBC</BizMsgIdr>
\t\t<MsgDefIdr>pacs.008.001.08</MsgDefIdr>
\t\t<BizSvc>swift.cbprplus.02</BizSvc>
\t\t<CreDt>2026-05-27T10:00:00+00:00</CreDt></AppHdr>
\t<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
\t\t<FIToFICstmrCdtTrf>
\t\t\t<GrpHdr>
\t\t\t\t<MsgId>MSGEP1TTQKJE0C2HDBC</MsgId>
\t\t\t\t<CreDtTm>2026-05-27T10:00:00+00:00</CreDtTm>
\t\t\t\t<NbOfTxs>1</NbOfTxs>
\t\t\t\t<SttlmInf>
\t\t\t\t\t<SttlmMtd>INGA</SttlmMtd>
\t\t\t\t</SttlmInf>
\t\t\t</GrpHdr>
\t\t\t<CdtTrfTxInf>
\t\t\t\t<PmtId>
\t\t\t\t\t<InstrId>INSTRI5I4NRRNHBI</InstrId>
\t\t\t\t\t<EndToEndId>E2EMATUR7A85ZF1Z58W</EndToEndId>
\t\t\t\t\t<TxId>TXLRYJM8GI37G8A8LX</TxId>
\t\t\t\t\t<UETR>UETR</UETR></PmtId>
\t\t\t\t<IntrBkSttlmAmt Ccy="EU">EU</IntrBkSttlmAmt><IntrBkSttlmDt>2026-05-28</IntrBkSttlmDt>
\t\t\t\t<InstdAmt Ccy="EUR">416411.54</InstdAmt>
\t\t\t\t<ChrgBr>SHAR</ChrgBr>
\t\t\t\t<InstgAgt>
\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t<BICFI>CREDITMMXXX</BICFI>
\t\t\t\t\t</FinInstnId>
\t\t\t\t</InstgAgt>
\t\t\t\t<InstdAgt>
\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t<BICFI>HSBCITMMXXX</BICFI>
\t\t\t\t\t</FinInstnId>
\t\t\t\t</InstdAgt>
\t\t\t\t<Dbtr>
\t\t\t\t\t<Nm>Pacific Investments SA</Nm>
\t\t\t\t</Dbtr>
\t\t\t\t<DbtrAcct>
\t\t\t\t\t<Id>
\t\t\t\t\t\t<IBAN>PT78832726910351989695125</IBAN>
\t\t\t\t\t</Id>
\t\t\t\t</DbtrAcct>
\t\t\t\t<DbtrAgt>
\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t<BICFI>SOGEPTLLXXX</BICFI>
\t\t\t\t\t</FinInstnId>
\t\t\t\t</DbtrAgt>
\t\t\t\t<CdtrAgt>
\t\t\t\t\t<FinInstnId>
\t\t\t\t\t\t<BICFI>LLOYBEBBXXX</BICFI>
\t\t\t\t\t</FinInstnId>
\t\t\t\t</CdtrAgt>
\t\t\t\t<Cdtr>
\t\t\t\t\t<Nm>Ava Thomas</Nm>
\t\t\t\t</Cdtr>
\t\t\t\t<CdtrAcct>
\t\t\t\t\t<Id>
\t\t\t\t\t\t<IBAN>BE82037081663411</IBAN>
\t\t\t\t\t</Id>
\t\t\t\t</CdtrAcct>
\t\t\t</CdtTrfTxInf>
\t\t</FIToFICstmrCdtTrf>
\t</Document>
</BusMsgEnvlp>"""

# These are the REAL errors the validator finds in the user's XML
REAL_ISSUES = [
    {
        "label": "UETR='UETR' (not a UUID)",
        "issue": {
            "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR",
            "code": "INVALID_FIELD_FORMAT",
            "message": "Field 'UETR' has invalid format: 'UETR'.",
            "fix_suggestion": "Value must match pattern for 'UUID': ^[0-9a-fA-F\\-]{36}$"
        },
        "expected_value_pattern": r"^[0-9a-f]{8}-[0-9a-f]{4}",
    },
    {
        "label": "Currency Ccy='EU' (must be 3-letter)",
        "issue": {
            "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt@Ccy",
            "code": "INVALID_CURRENCY_CODE",
            "message": "Unrecognised Currency Code 'EU'.",
            "fix_suggestion": "The code 'EU' is not a valid ISO 4217 currency. Use standard codes like USD, EUR, GBP, JPY, etc."
        }
    },
    {
        "label": "Amount text='EU' (must be numeric)",
        "issue": {
            "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.IntrBkSttlmAmt",
            "code": "INVALID_FIELD_FORMAT",
            "message": "Field 'IntrBkSttlmAmt' has invalid format: 'EU'.",
            "fix_suggestion": "Value must match pattern for 'Amount': ^\\d{1,13}(\\.\\d{1,5})?$"
        }
    },
    {
        "label": "AppHdr.Fr BICFI='CREDITMM' (only 8 chars, doesn't match InstgAgt)",
        "issue": {
            "path": "AppHdr.Fr.FIId.FinInstnId.BICFI",
            "code": "CBPR_R2",
            "message": "AppHdr <Fr> BICFI must match <InstgAgt> BICFI",
            "fix_suggestion": "Align AppHdr <Fr> BICFI with CdtTrfTxInf <InstgAgt> BICFI."
        }
    },
]

print("=" * 100)
print("REAL USER XML TEST")
print("=" * 100)

xml = USER_XML
for i, t in enumerate(REAL_ISSUES, 1):
    print(f"\n[{i}] {t['label']}")
    issue = t["issue"]
    print(f"    path: {issue['path']}")
    sugg = fix_suggester.suggest(xml, issue)
    print(f"    -> conf: {sugg.confidence}")
    if not sugg.fragment_xml:
        print(f"    -> EMPTY")
        continue
    frag = sugg.fragment_xml.replace('\n','').replace('\t','').replace('  ','')[:250]
    print(f"    -> frag: {frag}")

    # Try applying it
    try:
        new_xml = fix_suggester.apply(xml, sugg.xpath, sugg.fragment_xml)
        # Print the changed portion
        import re
        if "UETR" in t["label"]:
            m = re.search(r'<UETR[^>]*>([^<]+)</UETR>', new_xml)
            print(f"    -> NEW UETR value: {m.group(1) if m else 'NOT FOUND'}")
        elif "Currency" in t["label"]:
            m = re.search(r'<IntrBkSttlmAmt[^>]*Ccy="([^"]+)"', new_xml)
            print(f"    -> NEW Ccy: {m.group(1) if m else 'NOT FOUND'}")
        elif "Amount" in t["label"]:
            m = re.search(r'<IntrBkSttlmAmt[^>]*>([^<]+)</IntrBkSttlmAmt>', new_xml)
            print(f"    -> NEW Amt: {m.group(1) if m else 'NOT FOUND'}")
        elif "BICFI" in t["label"]:
            m = re.search(r'<Fr>.*?<BICFI>([^<]+)</BICFI>', new_xml, re.DOTALL)
            print(f"    -> NEW Fr BICFI: {m.group(1) if m else 'NOT FOUND'}")
        xml = new_xml
    except Exception as e:
        print(f"    -> APPLY FAILED: {e}")
