"""Validate all per-message KB files and replay the fix_suggester._KBContext loader."""
import json, os

KB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app", "resources", "KB"))
FILES = [
    "pacs002_cbprplus_sr2025_validation_kb.json",
    "pacs003_cbprplus_sr2025_validation_kb.json",
    "pacs004_cbprplus_sr2025_validation_kb.json",
    "pacs008_cbprplus_sr2025_validation_kb.json",
    "pacs009_cbprplus_sr2025_validation_kb.json",
    "pacs009_adv_cbprplus_sr2025_validation_kb.json",
    "pacs009_cov_cbprplus_sr2025_validation_kb.json",
    "pacs010_cbprplus_sr2025_validation_kb.json",
    "camt052_cbprplus_sr2025_validation_kb.json",
    "camt053_cbprplus_sr2025_validation_kb.json",
    "camt054_cbprplus_sr2025_validation_kb.json",
    "camt055_cbprplus_sr2025_validation_kb.json",
    "camt056_cbprplus_sr2025_validation_kb.json",
    "camt057_cbprplus_sr2025_validation_kb.json",
    "pain001_cbprplus_sr2025_validation_kb.json",
    "pain002_cbprplus_sr2025_validation_kb.json",
    "pain008_cbprplus_sr2025_validation_kb.json",
]


def _as_list(v):  # mirror of fix_suggester._KBContext._as_list
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return list(v.values())
    return []


ok = True
for f in FILES:
    p = os.path.join(KB, f)
    with open(p, "r", encoding="utf-8") as fh:
        d = json.load(fh)  # raises on malformed JSON
    problems = []

    # golden structure assertions
    if not isinstance(d.get("iso_20022_rules"), dict):
        problems.append("iso_20022_rules not object")
    cbpr = d.get("cbpr_plus_formal_rules")
    if not (isinstance(cbpr, list) and all(isinstance(x, str) for x in cbpr)):
        problems.append("cbpr_plus_formal_rules not string[]")
    if not isinstance(d.get("schema_mapping_notes"), dict) or not d["schema_mapping_notes"]:
        problems.append("schema_mapping_notes missing/empty")

    # replay loader (fix_suggester lines 595-636)
    by_code, by_tag, valid_by_tag = {}, {}, {}
    REQ = {"tag", "xml_element", "xpath", "occurrence", "mandatory", "description", "errors"}
    bad_tags = 0
    for t in _as_list(d.get("tags")):
        if not isinstance(t, dict):
            continue
        if not REQ.issubset(t.keys()):
            bad_tags += 1
        leaf = t.get("xml_element") or (t.get("tag", "").split("/")[-1])
        for e in _as_list(t.get("errors")):
            if not isinstance(e, dict):
                continue
            rec = {"error_code": e.get("error_code", ""),
                   "possible_fixes": e.get("possible_fixes", []) or [], "leaf": leaf}
            if leaf:
                by_tag.setdefault(leaf, []).append(rec)
            if rec["error_code"]:
                by_code.setdefault(rec["error_code"], []).append(rec)
    if bad_tags:
        problems.append(f"{bad_tags} tags missing required fields")
    dep = _as_list(d.get("cross_tag_dependency_rules"))
    formal = _as_list(d.get("cbpr_plus_formal_rules")) + _as_list(d.get("iso_20022_rules"))
    # _KBContext.get returns the ctx only if by_tag or dependency_rules is non-empty
    usable = bool(by_tag or dep)
    if not usable:
        problems.append("loader would discard KB (empty by_tag and dependency_rules)")

    status = "OK " if not problems else "FAIL"
    if problems:
        ok = False
    print(f"[{status}] {f:48} tags={len(d.get('tags', []))} "
          f"by_code={len(by_code)} by_tag={len(by_tag)} dep={len(dep)} "
          f"formal={len(formal)} smn={len(d.get('schema_mapping_notes', {}))}"
          + ("" if not problems else "  -> " + "; ".join(problems)))

print("\nALL VALID" if ok else "\nVALIDATION FAILURES PRESENT")
