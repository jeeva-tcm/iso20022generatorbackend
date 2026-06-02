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

def issue(code="XML_SYNTAX", line=8):
    return {"code": code, "path": "/", "line": line,
            "message": f"Malformed XML at line {line} -- invalid structure or unclosed tags.",
            "fix_suggestion": ""}

MSGS = [
    ("pacs.008", "urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08",
     '<FIToFICstmrCdtTrf><GrpHdr><MsgId>KEY-PACS008</MsgId></GrpHdr></FIToFICstmrCdtTrf>'),
    ("pacs.009", "urn:iso:std:iso:20022:tech:xsd:pacs.009.001.08",
     '<FIToFICdtTrf><GrpHdr><MsgId>KEY-PACS009</MsgId></GrpHdr></FIToFICdtTrf>'),
    ("pacs.002", "urn:iso:std:iso:20022:tech:xsd:pacs.002.001.10",
     '<FIToFIPmtStsRpt><GrpHdr><MsgId>KEY-PACS002</MsgId></GrpHdr></FIToFIPmtStsRpt>'),
    ("pacs.004", "urn:iso:std:iso:20022:tech:xsd:pacs.004.001.09",
     '<PmtRtr><GrpHdr><MsgId>KEY-PACS004</MsgId></GrpHdr></PmtRtr>'),
    ("pacs.010", "urn:iso:std:iso:20022:tech:xsd:pacs.010.001.03",
     '<FIDrctDbt><GrpHdr><MsgId>KEY-PACS010</MsgId></GrpHdr></FIDrctDbt>'),
    ("pain.001", "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09",
     '<CstmrCdtTrfInitn><GrpHdr><MsgId>KEY-PAIN001</MsgId></GrpHdr></CstmrCdtTrfInitn>'),
    ("pain.002", "urn:iso:std:iso:20022:tech:xsd:pain.002.001.10",
     '<CstmrPmtStsRpt><GrpHdr><MsgId>KEY-PAIN002</MsgId></GrpHdr></CstmrPmtStsRpt>'),
    ("pain.008", "urn:iso:std:iso:20022:tech:xsd:pain.008.001.08",
     '<CstmrDrctDbtInitn><GrpHdr><MsgId>KEY-PAIN008</MsgId></GrpHdr></CstmrDrctDbtInitn>'),
    ("camt.053", "urn:iso:std:iso:20022:tech:xsd:camt.053.001.08",
     '<BkToCstmrStmt><GrpHdr><MsgId>KEY-CAMT053</MsgId></GrpHdr></BkToCstmrStmt>'),
    ("camt.055", "urn:iso:std:iso:20022:tech:xsd:camt.055.001.09",
     '<CstmrPmtCxlReq><Assgnmt><Id>KEY-CAMT055</Id><CreDtTm>2026-06-01T08:00:00+00:00</CreDtTm></Assgnmt></CstmrPmtCxlReq>'),
    ("camt.056", "urn:iso:std:iso:20022:tech:xsd:camt.056.001.08",
     '<FIToFIPmtCxlReq><Assgnmt><Id>KEY-CAMT056</Id><CreDtTm>2026-06-01T08:00:00+00:00</CreDtTm></Assgnmt></FIToFIPmtCxlReq>'),
    ("camt.057", "urn:iso:std:iso:20022:tech:xsd:camt.057.001.08",
     '<NtfctnToRcv><GrpHdr><MsgId>KEY-CAMT057</MsgId></GrpHdr><Ntfctn><Id>N1</Id></Ntfctn></NtfctnToRcv>'),
]

def break_xml(ns_uri, body, break_type):
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{ns_uri}">\n  {body}\n</Document>'
    # Find the KEY value embedded in this xml (anything starting with KEY-)
    key_m = re.search(r'>(KEY-\w+)<', xml)
    key = key_m.group(1) if key_m else "KEY"

    if break_type == "unclosed":
        # Remove a non-first, non-last closing tag to leave structure open
        closes = list(re.finditer(r'</\w+>', xml))
        if len(closes) >= 3:
            target = closes[len(closes)//2]  # middle close tag
            broken = xml[:target.start()] + xml[target.end():]
            return broken, key
    elif break_type == "amp":
        broken = xml.replace(key, f"A&B-{key[4:]}")  # inject raw &
        return broken, f"B-{key[4:]}"
    return xml, key

print(f"Testing XML_SYNTAX recovery: {len(MSGS)} message types x 2 break types")
print("="*64)

for msg_name, ns_uri, body in MSGS:
    for break_type, label in [("unclosed", "unclosed tag"), ("amp", "& in value")]:
        broken, key = break_xml(ns_uri, body, break_type)

        # Confirm actually broken
        try:
            etree.fromstring(broken.encode())
            continue  # still valid - skip
        except:
            pass

        sug = fix_suggester.suggest(broken, issue())
        changed = bool(sug.fragment_xml and sug.fragment_xml != sug.original_fragment)
        if not changed:
            chk(f"{msg_name} [{label}]", False, f"conf={sug.confidence}")
            continue

        try:
            out = fix_suggester.apply(broken, sug.xpath, sug.fragment_xml)
            etree.fromstring(out.encode())
            chk(f"{msg_name} [{label}]: well-formed + key preserved",
                key in out, f"key={key!r}")
        except Exception as e:
            chk(f"{msg_name} [{label}]", False, str(e)[:60])

print("\n--- Error code variants ---")
BROKEN = '<?xml version="1.0" encoding="UTF-8"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"><FIToFICstmrCdtTrf><GrpHdr><MsgId>KEEPME'
for code, msg_text in [
    ("XML_SYNTAX",       "Malformed XML at line 5 -- invalid structure or unclosed tags."),
    ("XML Syntax Error", "XML syntax error at line 5: the message cannot be parsed."),
    ("XML Markup Error", "XML Markup Error: Opening and ending tag mismatch."),
]:
    s = fix_suggester.suggest(BROKEN, {"code":code,"path":"/","line":5,"message":msg_text,"fix_suggestion":""})
    if s.fragment_xml and s.fragment_xml != s.original_fragment:
        out = fix_suggester.apply(BROKEN, s.xpath, s.fragment_xml)
        try:
            etree.fromstring(out.encode())
            chk(f'code "{code}"', "KEEPME" in out, f"conf={s.confidence}")
        except Exception as e:
            chk(f'code "{code}"', False, str(e)[:60])
    else:
        chk(f'code "{code}"', False, f"conf={s.confidence}")

print()
print("OVERALL:", "ALL PASS" if ok else "SOME FAILED")
