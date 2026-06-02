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
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" | {detail}" if detail else ""))

def issue(msg="Country <Ctry> is missing in Dbtr address.", line=10):
    return {"code":"ADDR_CTRY_MISSING","path":"/","message":msg,
            "fix_suggestion":"Add a valid 2-character ISO country code.","line":line}

# 1. PstlAdr present, Ctry absent
XML1 = '''<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf><CdtTrfTxInf>
    <Dbtr><Nm>ACME</Nm><PstlAdr><AdrLine>123 Main St</AdrLine></PstlAdr></Dbtr>
    <Cdtr><Nm>Rec</Nm><PstlAdr><Ctry>US</Ctry></PstlAdr></Cdtr>
  </CdtTrfTxInf></FIToFICstmrCdtTrf>
</Document>'''
s1 = fix_suggester.suggest(XML1, issue())
chk("1a Dbtr Ctry insert: changed", s1.fragment_xml != s1.original_fragment, f"conf={s1.confidence}")
if s1.fragment_xml != s1.original_fragment:
    out1 = fix_suggester.apply(XML1, s1.xpath, s1.fragment_xml)
    try: etree.fromstring(out1.encode()); chk("1b well-formed", True)
    except Exception as e: chk("1b well-formed", False, str(e))
    chk("1c Ctry inserted", "<Ctry>" in out1)
    chk("1d AdrLine preserved", "123 Main St" in out1)
    chk("1e Cdtr Ctry untouched (2 total)", out1.count("<Ctry>") == 2)

# 2. PstlAdr entirely absent - should insert full dummy address
XML2 = '''<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf><CdtTrfTxInf>
    <Dbtr><Nm>ACME Corp</Nm></Dbtr>
  </CdtTrfTxInf></FIToFICstmrCdtTrf>
</Document>'''
s2 = fix_suggester.suggest(XML2, issue())
chk("2a no PstlAdr: changed", s2.fragment_xml != s2.original_fragment, f"conf={s2.confidence}")
if s2.fragment_xml != s2.original_fragment:
    out2 = fix_suggester.apply(XML2, s2.xpath, s2.fragment_xml)
    try: etree.fromstring(out2.encode()); chk("2b well-formed", True)
    except Exception as e: chk("2b well-formed", False, str(e))
    chk("2c PstlAdr inserted", "<PstlAdr>" in out2)
    chk("2d Ctry in new PstlAdr", "<Ctry>" in out2)
    chk("2e Nm preserved", "ACME Corp" in out2)

# 3. BICFI country inference: DEUTDEFFXXX -> DE
XML3 = '''<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf><CdtTrfTxInf>
    <DbtrAgt><FinInstnId><BICFI>DEUTDEFFXXX</BICFI></FinInstnId></DbtrAgt>
    <Dbtr><Nm>German Co</Nm><PstlAdr><AdrLine>Frankfurt 1</AdrLine></PstlAdr></Dbtr>
  </CdtTrfTxInf></FIToFICstmrCdtTrf>
</Document>'''
s3 = fix_suggester.suggest(XML3, issue())
if s3.fragment_xml != s3.original_fragment:
    out3 = fix_suggester.apply(XML3, s3.xpath, s3.fragment_xml)
    m = re.search(r"<Ctry>([^<]+)</Ctry>", out3)
    chk("3 BICFI->country DE", m and m.group(1)=="DE", f"got={m.group(1) if m else '?'}")
else:
    chk("3 BICFI->country DE", False, "no change")

# 4. Cdtr message variant
XML4 = '''<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
  <FIToFICstmrCdtTrf><CdtTrfTxInf>
    <Dbtr><Nm>D</Nm><PstlAdr><Ctry>GB</Ctry></PstlAdr></Dbtr>
    <Cdtr><Nm>Rec</Nm><PstlAdr><AdrLine>1 Wall St</AdrLine></PstlAdr></Cdtr>
  </CdtTrfTxInf></FIToFICstmrCdtTrf>
</Document>'''
s4 = fix_suggester.suggest(XML4, issue("Country <Ctry> is missing in Cdtr address.", 8))
chk("4a Cdtr: changed", s4.fragment_xml != s4.original_fragment, f"conf={s4.confidence}")
if s4.fragment_xml != s4.original_fragment:
    out4 = fix_suggester.apply(XML4, s4.xpath, s4.fragment_xml)
    try: etree.fromstring(out4.encode()); chk("4b well-formed", True)
    except Exception as e: chk("4b well-formed", False, str(e))
    chk("4c Dbtr GB untouched", "GB" in out4)
    chk("4d Cdtr gets Ctry", out4.count("<Ctry>") == 2)

# 5. Normal suggest regression (valid XML, different error code unaffected)
XML5 = '<?xml version="1.0"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"><FIToFICstmrCdtTrf><GrpHdr><MsgId>M</MsgId><CreDtTm>2025-01-01T00:00:00+00:00</CreDtTm><NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf></GrpHdr></FIToFICstmrCdtTrf></Document>'
s5 = fix_suggester.suggest(XML5, {"code":"PAST_DATE_ERROR","path":"//CreDtTm","message":"Date cannot be in the past.","fix_suggestion":"","line":1})
chk("5 regression: normal path works", s5.confidence in ("high","low"), f"conf={s5.confidence}")

print()
print("OVERALL:", "ALL PASS" if ok else "SOME FAILED")
