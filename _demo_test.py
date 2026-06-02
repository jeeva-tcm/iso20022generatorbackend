import sys, re, importlib
sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
import app.services.fix_suggester as fs_mod
importlib.reload(fs_mod)
fix_suggester = fs_mod.fix_suggester
from lxml import etree

ok = True
def chk(name, cond, detail=""):
    global ok; ok = ok and cond
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {name}" + (f" | {detail}" if detail else ""))

def issue(msg="Malformed XML at line 43 -- invalid structure or unclosed tags.",
          code="XML_SYNTAX"):
    return {"code": code, "path": "/", "message": msg,
            "fix_suggestion": "Check line 43 and ensure all tags are properly opened and closed.",
            "line": 43}

# ─── 1. Unclosed tag at line 43 (realistic CBPR+ pacs.008 + BAH) ──────────
XML_L43 = '''<?xml version="1.0" encoding="UTF-8"?>
<BusMsgEnvlp xmlns="urn:swift:xsd:envelope">
    <AppHdr xmlns="urn:iso:std:iso:20022:tech:xsd:head.001.001.02">
        <Fr><FIId><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></FIId></Fr>
        <To><FIId><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></FIId></To>
        <BizMsgIdr>BMS-DEMO-001</BizMsgIdr>
        <MsgDefIdr>pacs.008.001.08</MsgDefIdr>
        <BizSvc>swift.cbprplus.02</BizSvc>
        <CreDt>2026-06-01T08:00:00+00:00</CreDt>
    </AppHdr>
    <Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
        <FIToFICstmrCdtTrf>
            <GrpHdr>
                <MsgId>BMS-DEMO-001</MsgId>
                <CreDtTm>2026-06-01T08:00:00+00:00</CreDtTm>
                <NbOfTxs>1</NbOfTxs>
                <SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf>
            </GrpHdr>
            <CdtTrfTxInf>
                <PmtId>
                    <InstrId>INSTR-001</InstrId>
                    <EndToEndId>E2E-001</EndToEndId>
                    <UETR>a1aa60d7-4d7f-447e-827c-7a688828a135</UETR>
                </PmtId>
                <IntrBkSttlmAmt Ccy="USD">1000.00</IntrBkSttlmAmt>
                <IntrBkSttlmDt>2026-06-01</IntrBkSttlmDt>
                <ChrgBr>SLEV</ChrgBr>
                <InstgAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></InstgAgt>
                <InstdAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></InstdAgt>
                <Dbtr>
                    <Nm>Demo Debtor GmbH</Nm>
                    <PstlAdr>
                        <AdrLine>Kaiserstrasse 29</AdrLine>
                        <Ctry>DE</Ctry>
                    </PstlAdr>
                </Dbtr>
                <DbtrAcct><Id><IBAN>DE89370400440532013000</IBAN></Id></DbtrAcct>
                <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
                <CdtrAgt><FinInstnId><BICFI>CHASUS33XXX</BICFI></FinInstnId></CdtrAgt>
                <Cdtr>
                    <Nm>Demo Creditor Inc
                    <PstlAdr>
                        <AdrLine>100 Park Avenue</AdrLine>
                        <Ctry>US</Ctry>
                    </PstlAdr>
                </Cdtr>
                <CdtrAcct><Id><IBAN>US64SVBKUS6S3300958879</IBAN></Id></CdtrAcct>
            </CdtTrfTxInf>
        </FIToFICstmrCdtTrf>
    </Document>
</BusMsgEnvlp>'''

print(f"Original XML lines: {len(XML_L43.splitlines())} | well-formed: ", end="")
try: etree.fromstring(XML_L43.encode()); print("YES (unexpected)")
except: print("NO (expected - unclosed Nm at ~line 43)")

# Suggest
sug = fix_suggester.suggest(XML_L43, issue())
print(f"\nsuggest() result:")
print(f"  confidence : {sug.confidence}")
print(f"  xpath      : {sug.xpath}")
print(f"  changed    : {sug.fragment_xml != sug.original_fragment}")

chk("suggest: high confidence", sug.confidence == "high")
chk("suggest: changed", sug.fragment_xml != sug.original_fragment)
chk("suggest: xpath='/'", sug.xpath == "/")

# Apply
print("\napply() result:")
try:
    out = fix_suggester.apply(XML_L43, sug.xpath, sug.fragment_xml)
    etree.fromstring(out.encode())
    chk("apply: well-formed output", True)
    chk("apply: key data preserved (BMS-DEMO-001)", "BMS-DEMO-001" in out)
    chk("apply: key data preserved (UETR)", "a1aa60d7" in out)
    chk("apply: key data preserved (DE89)", "DE89370400440532013000" in out)
    chk("apply: declaration preserved", out.startswith("<?xml"))
    print(f"  output is {len(out.splitlines())} lines, well-formed XML")
except Exception as e:
    chk("apply: well-formed output", False, str(e)[:80])

# ─── 2. Layer-1 code "XML Syntax Error" (space) ───────────────────────────
print("\n--- Layer-1 code variant ---")
sug2 = fix_suggester.suggest(XML_L43, {
    "code": "XML Syntax Error", "path": "/", "line": 43,
    "message": "XML syntax error at line 43: the message cannot be parsed. Check for unclosed tags.",
    "fix_suggestion": "Check near line 43 for unclosed tags."
})
chk("L1 code: high confidence", sug2.confidence == "high")
if sug2.fragment_xml != sug2.original_fragment:
    out2 = fix_suggester.apply(XML_L43, sug2.xpath, sug2.fragment_xml)
    try: etree.fromstring(out2.encode()); chk("L1 code: well-formed", True)
    except Exception as e: chk("L1 code: well-formed", False, str(e)[:60])

# ─── 3. Unescaped & in value (the other common XML_SYNTAX case) ────────────
print("\n--- Unescaped & variant ---")
XML_AMP = XML_L43.replace(
    "Demo Debtor GmbH", "Müller & Schmidt GmbH"
).replace(
    "Demo Creditor Inc\n                    <PstlAdr>",
    "Demo Creditor Inc</Nm>\n                    <PstlAdr>"
).replace("Demo Debtor GmbH", "Müller & Schmidt GmbH")

# Actually build a simpler amp case to avoid combining two bugs
XML_AMP_ONLY = '''<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf>
    <CdtTrfTxInf>
      <Dbtr><Nm>Smith & Jones</Nm><PstlAdr><Ctry>GB</Ctry></PstlAdr></Dbtr>
      <Cdtr><Nm>Receiver Corp</Nm><PstlAdr><Ctry>US</Ctry></PstlAdr></Cdtr>
    </CdtTrfTxInf>
  </FIToFICstmrCdtTrf>
</Document>'''

sug3 = fix_suggester.suggest(XML_AMP_ONLY, issue("Invalid character '&' at line 5.", "XML Syntax Error"))
chk("amp: high confidence", sug3.confidence == "high")
if sug3.fragment_xml != sug3.original_fragment:
    out3 = fix_suggester.apply(XML_AMP_ONLY, sug3.xpath, sug3.fragment_xml)
    try: etree.fromstring(out3.encode()); chk("amp: well-formed", True)
    except Exception as e: chk("amp: well-formed", False, str(e)[:60])
    chk("amp: data preserved", "Jones" in out3)
    chk("amp: escaped correctly", "&amp;" in out3)

print()
print("OVERALL:", "ALL PASS" if ok else "SOME FAILED")
