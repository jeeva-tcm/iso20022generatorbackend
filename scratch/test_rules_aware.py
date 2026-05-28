# -*- coding: utf-8 -*-
"""Verify the rules-aware fix suggester uses message-specific tag structures."""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))

from app.services.fix_suggester import fix_suggester, _RulesIndex, _detect_msg_type

# ── 1. Verify rules index loads pacs.008 rules ────────────────────────────────
print("=== Rules Index Sanity ===")
idx = _RulesIndex.get("pacs.008.001.08")
print(f"  pacs.008 rules loaded: {len(idx.by_rule_id)} rules")
print(f"  by_leaf_tag covers: {len(idx.by_leaf_tag)} unique tags")
print(f"  Sample rule_ids: {list(idx.by_rule_id.keys())[:5]}")

# Lookup a known rule
r = idx.lookup(rule_id="PACS008_UETR_REQUIRED")
print(f"\n  UETR rule fix: {r['fix'] if r else 'NOT FOUND'}")

r = idx.lookup(leaf_tag="DbtrAcct")
print(f"  DbtrAcct rule fix: {r['fix'] if r and r.get('fix') else 'NO FIX STRING'}")

# ── 2. Test: building a missing DbtrAcct should use the rule's exact fix ──────
print("\n=== Rules-aware fix tests ===")
XML_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">'
    '<FIToFICstmrCdtTrf>'
    '<GrpHdr><MsgId>MSG-DEMO-001</MsgId><CreDtTm>2025-01-15T10:00:00Z</CreDtTm>'
    '<NbOfTxs>1</NbOfTxs><SttlmInf><SttlmMtd>INGA</SttlmMtd></SttlmInf></GrpHdr>'
    '<CdtTrfTxInf>'
    '<PmtId><InstrId>I1</InstrId><EndToEndId>E1</EndToEndId></PmtId>'
    '<IntrBkSttlmAmt Ccy="USD">1000</IntrBkSttlmAmt>'
    '<DbtrAgt><FinInstnId><BICFI>BARCGB22XXX</BICFI></FinInstnId></DbtrAgt>'
    '<Cdtr><Nm>Jane</Nm></Cdtr>'
    '</CdtTrfTxInf>'
    '</FIToFICstmrCdtTrf>'
    '</Document>'
)

# Case 1: DbtrAcct missing — rule fix is "<DbtrAcct><Id><IBAN>...</IBAN></Id></DbtrAcct>"
# Expect the result to contain THAT exact nested structure, NOT the generic template
issue = {
    "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.DbtrAcct",
    "code": "PACS008_DBTRACCT_REQUIRED",
    "message": "DbtrAcct is mandatory in pacs.008.",
    "fix_suggestion": "",  # Empty — the suggester should pull `fix` from the rule
}
r = fix_suggester.suggest(XML_DOC, issue)
print(f"\nCase 1: Missing DbtrAcct (empty fix_hint)")
print(f"  Conf: {r.confidence}")
print(f"  Has <Id>: {'<Id>' in r.fragment_xml}")
print(f"  Has <IBAN>: {'<IBAN>' in r.fragment_xml}")
preview = r.fragment_xml.replace('\n','').replace('  ','')[:200]
print(f"  Frag: {preview}")

# Case 2: UETR missing — rule says use UUID v4
issue2 = {
    "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.UETR",
    "code": "PACS008_UETR_REQUIRED",
    "message": "UETR is mandatory.",
    "fix_suggestion": "",
}
r2 = fix_suggester.suggest(XML_DOC, issue2)
print(f"\nCase 2: Missing UETR (empty fix_hint, rule has UUID hint)")
print(f"  Conf: {r2.confidence}")
preview2 = r2.fragment_xml.replace('\n','').replace('  ','')[:250]
print(f"  Frag: {preview2}")
import re
uuid_m = re.search(r'<UETR[^>]*>([0-9a-f-]+)</UETR>', preview2)
print(f"  UETR value: {uuid_m.group(1) if uuid_m else 'NOT FOUND'}")

# Case 3: BICFI missing — should reuse an existing BICFI from the document
issue3 = {
    "path": "Document.FIToFICstmrCdtTrf.CdtTrfTxInf.CdtrAgt.FinInstnId.BICFI",
    "code": "L3_BIC_DIRECTORY_001",
    "message": "CdtrAgt BIC missing",
    "fix_suggestion": "",
}
r3 = fix_suggester.suggest(XML_DOC, issue3)
print(f"\nCase 3: Missing CdtrAgt/FinInstnId/BICFI (should reuse existing BICFI)")
print(f"  Conf: {r3.confidence}")
preview3 = r3.fragment_xml.replace('\n','').replace('  ','')[:300]
print(f"  Frag: {preview3}")
bic_m = re.search(r'<CdtrAgt[^>]*>.*?<BICFI[^>]*>([A-Z0-9]+)</BICFI>', preview3)
print(f"  CdtrAgt BICFI: {bic_m.group(1) if bic_m else 'NOT FOUND'}")
print(f"  Used existing BARCGB22XXX (Dbtr's BIC): {'BARCGB22XXX' in (bic_m.group(1) if bic_m else '')}")

# Case 4: MsgId reuse — building anything that contains <MsgId> should reuse MSG-DEMO-001
issue4 = {
    "path": "AppHdr.BizMsgIdr",
    "code": "BIZMSGIDR",
    "message": "BizMsgIdr must equal GrpHdr/MsgId",
    "fix_suggestion": "Set <BizMsgIdr> equal to <GrpHdr><MsgId>.",
}
r4 = fix_suggester.suggest(XML_DOC, issue4)
print(f"\nCase 4: Missing AppHdr.BizMsgIdr (no AppHdr exists)")
print(f"  Conf: {r4.confidence}")
preview4 = r4.fragment_xml.replace('\n','').replace('  ','')[:200]
print(f"  Frag: {preview4}")
