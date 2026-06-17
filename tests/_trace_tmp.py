import sys, os, asyncio, zlib
sys.path.insert(0, ".")
try:
    from dotenv import load_dotenv; load_dotenv(".env")
except: pass

import app.services.fix_suggester as fs
from app.services.fix_suggester import fix_suggester
from app.services.generation.bulk_generator import generate_single_xml, get_blocks_for_message
import importlib.util
_spec = importlib.util.spec_from_file_location("h2026", "tests/test_autofix_sr2026.py")
H = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(H)

CASES = ["pacs.009", "pacs.010", "camt.055", "pain.002"]

async def run():
    for token in CASES:
        blocks = [b["id"] for b in get_blocks_for_message(token)]
        clean = generate_single_xml(token, blocks, idx=1, version="SR2026")
        mt = H.msg_type_of(clean) or token
        _, base_err, _ = await H.snapshot(clean, mt)
        base = {(e["code"], e["path"]) for e in base_err}
        broken, removed = H.remove_tags(clean, seed=zlib.crc32(token.encode()))
        print(f"\n=== {token} removed={removed} ===")
        _, brk_err, _ = await H.snapshot(broken, mt)
        inj = [(e["code"],e["path"],e["message"][:70]) for e in brk_err if (e["code"],e["path"]) not in base]
        print(f"  injected: {inj}")
        fs._set_active_sr_version("SR2026")
        cur = broken
        seen = {hash(cur)}
        for rnd in range(8):
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
            print(f"  rnd {rnd}: fixable={len(fixable)} fixes={len(fixes)}")
            if not fixes: break
            new_xml = fix_suggester.apply_batch(cur, fixes)
            if new_xml == cur or hash(new_xml) in seen: break
            seen.add(hash(new_xml)); cur = new_xml
        _, final_errs, _ = await H.snapshot(cur, mt)
        residuals = [(e["code"], e["path"], e["message"][:90]) for e in final_errs if (e["code"],e["path"]) not in base]
        print(f"  RESIDUALS: {residuals}")

asyncio.run(run())
