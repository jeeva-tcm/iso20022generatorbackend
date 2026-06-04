import sys, os
sys.path.insert(0, os.path.abspath("iso20022generatorbackend"))
try:
    from app.services.fix_suggester import _KBContext, _kb_folder_name
except Exception as e:
    print("IMPORT_FAIL:", type(e).__name__, e); sys.exit(0)
cases = [
    ("pacs.003.001.08", ""),
    ("pacs.004.001.10", ""),
    ("pacs.009.001.08", ""),
    ("pacs.009.001.08_ADV", ""),
    ("pacs.009.001.08_COV", ""),
    ("camt.057.001.08", ""),
]
for mt, xml in cases:
    fn = _kb_folder_name(mt, xml)
    ctx = _KBContext.get(mt)
    if ctx is None:
        print(f"{mt:24} -> {fn:48} ctx=None")
    else:
        print(f"{mt:24} -> {fn:48} by_tag={len(ctx.by_tag)} by_code={len(ctx.by_code)} dep={len(ctx.dependency_rules)} formal={len(ctx.formal_rules)}")
