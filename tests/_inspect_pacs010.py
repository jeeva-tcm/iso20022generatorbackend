import sys, os, asyncio, zlib
sys.path.insert(0, ".")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except: pass

import app.services.fix_suggester as fs
from app.services.fix_suggester import fix_suggester
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
from lxml import etree
import importlib.util
_spec = importlib.util.spec_from_file_location("h2026", "tests/test_autofix_sr2026.py")
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)

async def run():
    token = "pacs.010"
    blocks = [b["id"] for b in get_blocks_for_message(token)]
    clean = generate_single_xml(token, blocks, idx=1, version="SR2026")
    mt = H.msg_type_of(clean) or token
    _, base_err, _ = await H.snapshot(clean, mt)
    base = {(e["code"], e["path"]) for e in base_err}
    broken, _ = H.remove_tags(clean, seed=zlib.crc32(token.encode()))
    fs._set_active_sr_version("SR2026")
    cur = broken
    seen = {hash(cur)}
    for rnd in range(3):
        wf, errs, warns = await H.snapshot(cur, mt)
        fixable = errs + [w for w in warns if w["code"] in H._AUTOFIX_WARNING_CODES]
        if not fixable: break
        try:
            comp = fix_suggester.xsd_completeness_pass(cur)
            if comp != cur: cur = comp
        except: pass
        try:
            sugs = fix_suggester.suggest_batch(cur, H.priority_sort(fixable)[:20], version="SR2026")
        except: break
        fixes = [{"xpath": s.xpath, "fragment_xml": s.fragment_xml}
                 for s in sugs if s.confidence in ("high","low") and s.fragment_xml and s.fragment_xml != s.original_fragment]
        if not fixes: break
        new_xml = fix_suggester.apply_batch(cur, fixes)
        if new_xml == cur or hash(new_xml) in seen: break
        seen.add(hash(new_xml)); cur = new_xml

    # Inspect the final XML state
    root = etree.fromstring(cur.encode("utf-8"), etree.XMLParser(recover=True))
    nb = root.xpath("//*[local-name()=\"NbOfTxs\"]")
    cdtinstr = root.xpath("//*[local-name()=\"CdtInstr\"]")
    print("NbOfTxs:", [e.text for e in nb])
    print("CdtInstr count:", len(cdtinstr))
    cdtid = root.xpath("//*[local-name()=\"CdtId\"]")
    print("CdtId count:", len(cdtid), [etree.QName(e.getparent().tag).localname if e.getparent() is not None else "none" for e in cdtid])
    # show FIDrctDbt children
    fidbt = root.xpath("//*[local-name()=\"FIDrctDbt\"]")
    if fidbt:
        print("FIDrctDbt children:", [etree.QName(c.tag).localname for c in fidbt[0] if isinstance(c.tag,str)])
    # show CdtInstr children
    if cdtinstr:
        print("CdtInstr children:", [etree.QName(c.tag).localname for c in cdtinstr[0] if isinstance(c.tag,str)])

asyncio.run(run())
