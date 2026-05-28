# -*- coding: utf-8 -*-
"""Verify ai_knowledge_base.json is being consulted by the fix suggester."""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import (
    fix_suggester, _load_knowledge_base, _kb_tag_template, _kb_field_constraint
)
import re

# ── 1. KB loaded ──────────────────────────────────────────────────────────────
print("=== Knowledge Base Sanity ===")
kb = _load_knowledge_base()
print(f"  KB version       : {kb.get('_meta', {}).get('version', '?')}")
print(f"  field_constraints: {len(kb.get('field_constraints', {}))} entries")
print(f"  tag_templates    : {len(kb.get('tag_templates', {}))} entries")
print(f"  dependencies     : equals={len(kb.get('dependencies', {}).get('equals', []))} "
      f"not_equal={len(kb.get('dependencies', {}).get('not_equal', []))} "
      f"conditional={len(kb.get('dependencies', {}).get('conditional_required', []))} "
      f"exclusive={len(kb.get('dependencies', {}).get('exclusive', []))}")
print(f"  dummy banks      : {len(kb.get('dummy_data', {}).get('banks', []))}")

# ── 2. Per-message templates ──────────────────────────────────────────────────
print("\n=== Per-message templates ===")
for tag in ("MsgDefIdr", "Dbtr", "PmtId"):
    print(f"\n  <{tag}>")
    for mt in ("pacs.008.001.08", "pacs.009.001.08", "pain.001.001.09"):
        tmpl = _kb_tag_template(tag, mt)
        family = ".".join(mt.split(".")[:2])
        print(f"    [{family:9s}] {tmpl[:90] if tmpl else 'None'}")

# ── 3. Field constraints ──────────────────────────────────────────────────────
print("\n=== Field constraints ===")
for tag in ("BICFI", "MsgId", "ChrgBr", "Ctry", "UETR"):
    c = _kb_field_constraint(tag)
    print(f"  <{tag}> : {c}")

# ── 4. End-to-end: fix uses KB templates + placeholder resolution ─────────────
XML_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf>'
    '<GrpHdr><MsgId>MSG-LIVE-20260527-001</MsgId>'
    '<CreDtTm>2026-05-27T14:30:00Z</CreDtTm>'
    '<NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf></GrpHdr>'
    '<CdtTrfTxInf>'
    '<PmtId><InstrId>INSTR-LIVE-001</InstrId><EndToEndId>E2E-LIVE-001</EndToEndId></PmtId>'
    '<IntrBkSttlmAmt Ccy="EUR">5000.00</IntrBkSttlmAmt>'
    '<DbtrAgt><FinInstnId><BICFI>BARCGB22XXX</BICFI></FinInstnId></DbtrAgt>'
    '<Cdtr><Nm>Live Creditor</Nm></Cdtr>'
    '</CdtTrfTxInf>'
    '</FIToFICstmrCdtTrf>'
    '</Document>'
)

print("\n=== End-to-end fixes (placeholder resolution from live XML) ===")

TESTS = [
    ("PmtId.UETR - reuse harvested values",
     "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR",
     "UETR missing"),
    ("DbtrAcct - exact pacs.008 structure",
     "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.DbtrAcct",
     "DbtrAcct missing"),
    ("CdtrAgt - distinct BIC (loopback avoidance)",
     "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.CdtrAgt",
     "CdtrAgt missing"),
    ("AppHdr.BizMsgIdr - must equal GrpHdr.MsgId",
     "AppHdr.BizMsgIdr",
     "BizMsgIdr missing"),
    ("AppHdr.MsgDefIdr - exact message namespace",
     "AppHdr.MsgDefIdr",
     "MsgDefIdr missing"),
    ("AppHdr.BizSvc - swift.cbprplus.02",
     "AppHdr.BizSvc",
     "BizSvc missing"),
    ("ChrgBr - prefer SLEV",
     "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.ChrgBr",
     "ChrgBr missing"),
]

for label, path, message in TESTS:
    r = fix_suggester.suggest(XML_DOC, {
        "path": path, "code": "TEST", "message": message, "fix_suggestion": ""
    })
    print(f"\n  {label}")
    print(f"    conf: {r.confidence}")
    frag = (r.fragment_xml or "").replace("\n", "").replace("  ", "")
    print(f"    {frag[:200]}")

# ── 5. Specific assertion: BizMsgIdr should harvest MsgId from live XML ───────
print("\n=== Critical assertion: BizMsgIdr harvests live MsgId ===")
r = fix_suggester.suggest(XML_DOC, {
    "path": "AppHdr.BizMsgIdr",
    "code": "BIZMSGIDR_EQ_MSGID",
    "message": "BizMsgIdr must equal GrpHdr.MsgId",
    "fix_suggestion": "",
})
match = re.search(r"<BizMsgIdr[^>]*>([^<]+)</BizMsgIdr>", r.fragment_xml or "")
harvested = match.group(1) if match else "(not found)"
expected  = "MSG-LIVE-20260527-001"
print(f"  Expected (live MsgId): {expected}")
print(f"  Got:                   {harvested}")
print(f"  {'PASS' if harvested == expected else 'FAIL'}")

# ── 6. Critical assertion: MsgDefIdr returns 'pacs.008.001.08' ────────────────
print("\n=== Critical assertion: MsgDefIdr is exact for pacs.008 ===")
r = fix_suggester.suggest(XML_DOC, {
    "path": "AppHdr.MsgDefIdr",
    "code": "MSGDEFIDR",
    "message": "MsgDefIdr missing",
    "fix_suggestion": "",
})
match = re.search(r"<MsgDefIdr[^>]*>([^<]+)</MsgDefIdr>", r.fragment_xml or "")
harvested = match.group(1) if match else "(not found)"
expected  = "pacs.008.001.08"
print(f"  Expected: {expected}")
print(f"  Got:      {harvested}")
print(f"  {'PASS' if harvested == expected else 'FAIL'}")

# ── 7. Critical assertion: ChrgBr uses preferred SLEV (not first codelist) ────
print("\n=== Critical assertion: ChrgBr uses KB preferred SLEV ===")
XML_BAD_CHRGBR = XML_DOC.replace("</Cdtr>", "</Cdtr><ChrgBr>XXXX</ChrgBr>")
r = fix_suggester.suggest(XML_BAD_CHRGBR, {
    "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.ChrgBr",
    "code": "L3_CHRGBR_001",
    "message": "Invalid Charge Bearer code 'XXXX'",
    "fix_suggestion": "",
})
match = re.search(r"<ChrgBr[^>]*>([^<]+)</ChrgBr>", r.fragment_xml or "")
got = match.group(1) if match else "(not found)"
print(f"  Expected: SLEV")
print(f"  Got:      {got}")
print(f"  {'PASS' if got == 'SLEV' else 'FAIL'}")
